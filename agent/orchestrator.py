"""Orchestrateur : propose les paramètres de l'itération suivante (Master Doc §3.5).

    python3 agent/orchestrator.py --propose          # écrit design_params.yaml
    python3 agent/orchestrator.py --propose --dry-run --explain

Deux stratégies, un seul contrat de sortie :

- **llm** — un agent Claude lit l'historique et raisonne sur la forme. C'est le
  cœur du système décrit par le Master Document.
- **local** — recherche par motif sans gradient, purement déterministe. Elle
  n'exige ni clé d'API, ni réseau, et sert de repli automatique dès que le LLM
  est indisponible ou propose l'invalide.

Le repli n'est pas un pis-aller : sans lui, une clé absente ou une coupure
réseau arrête une optimisation de plusieurs heures. La boucle continue, et le
compte rendu dit toujours quelle stratégie a décidé.

**Frontière stricte** : quelle que soit la stratégie, la proposition passe par
la validation de la Phase 0 (bornes ET max_delta_pct contre la dernière
itération réussie) AVANT d'être écrite. Une proposition invalide n'atteint
jamais le disque, et l'orchestrateur n'écrit que `configs/design_params.yaml`.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline import master_pipeline as mp  # noqa: E402
from pipeline.utils import (  # noqa: E402
    allowed_range,
    load_yaml,
    max_abs_delta,
    save_design_params,
    validate_design_params,
)

SYSTEM_PROMPT_PATH = REPO_ROOT / "agent" / "prompts" / "system_prompt.md"
DEFAULT_CONFIG = REPO_ROOT / "configs" / "design_params.yaml"
DEFAULT_ITERATIONS = REPO_ROOT / "data" / "iterations"

STRATEGY_LLM = "llm"
STRATEGY_LOCAL = "local"
STRATEGY_AUTO = "auto"
STRATEGIES = (STRATEGY_AUTO, STRATEGY_LLM, STRATEGY_LOCAL)

DEFAULT_MODEL = "claude-opus-5"
MAX_LLM_ATTEMPTS = 3

# Un paramètre dont les bornes l'enferment dans moins de 5 % de sa propre
# grandeur est considéré comme tenu fixe par le concepteur : le sonder
# gaspillerait une itération pour un changement de forme imperceptible. C'est
# le cas de `span`, immobilisé parce qu'il n'a aucun effet en quasi-2D.
FROZEN_BOUNDS_RATIO = 0.05


class ProposalError(Exception):
    """Aucune proposition valide n'a pu être construite."""


# ─────────────────────────────────────────────────────────────────────────────
# Lecture de l'état de l'optimisation
# ─────────────────────────────────────────────────────────────────────────────


def parameters_of(record: Mapping[str, Any], iterations_root: Path) -> dict | None:
    """Paramètres ayant produit une itération, relus dans son archive."""
    directory = Path(iterations_root) / f"iter_{int(record['iteration']):04d}"
    config = directory / mp.ARCHIVED_CONFIG
    if not config.is_file():
        return None
    try:
        return load_yaml(config).get("parameters")
    except Exception:
        return None


def evaluated_points(
    history: Sequence[Mapping[str, Any]], iterations_root: Path
) -> list[dict]:
    """Itérations réussies, avec leurs paramètres et leur score."""
    points: list[dict] = []
    for record in history:
        if not record.get("success"):
            continue
        objective = record.get("objective")
        if not isinstance(objective, (int, float)):
            continue
        parameters = parameters_of(record, iterations_root)
        if parameters is None:
            continue
        points.append(
            {
                "iteration": int(record["iteration"]),
                "objective": float(objective),
                "values": {
                    name: float(spec["value"]) for name, spec in parameters.items()
                },
                "Cd": record.get("Cd"),
                "Cl": record.get("Cl"),
                "Cl_Cd": record.get("Cl_Cd"),
                "converged": record.get("converged"),
            }
        )
    return points


def best_point(points: Sequence[Mapping[str, Any]]) -> dict | None:
    return max(points, key=lambda p: p["objective"]) if points else None


def free_parameters(parameters: Mapping[str, Any]) -> list[str]:
    """Paramètres réellement manœuvrables à la prochaine itération.

    Exclut ceux que les bornes immobilisent — `span` est tenu fixe parce qu'il
    n'a aucun effet en quasi-2D. Les laisser dans le jeu ferait dépenser des
    itérations pour rien.
    """
    free: list[str] = []
    for name, spec in parameters.items():
        try:
            value = float(spec["value"])
            low, high = float(spec["min"]), float(spec["max"])
        except (KeyError, TypeError, ValueError):
            continue
        width = high - low
        if width <= 0:
            continue
        # L'échelle de référence tient compte des paramètres qui valent zéro —
        # une incidence nulle est parfaitement manœuvrable.
        reference = max(abs(value), (abs(low) + abs(high)) / 2.0, 1e-12)
        if width / reference > FROZEN_BOUNDS_RATIO:
            free.append(name)
    return free


# ─────────────────────────────────────────────────────────────────────────────
# Stratégie locale — recherche par motif
# ─────────────────────────────────────────────────────────────────────────────


def propose_local(
    design: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    iterations_root: Path,
) -> tuple[dict[str, float], str]:
    """Recherche par motif, un paramètre à la fois, sans gradient.

    L'état est reconstruit à partir de l'historique archivé plutôt que gardé en
    mémoire : la boucle peut être interrompue et reprise sans rien perdre, et
    l'orchestrateur reste sans état — il ne doit écrire que design_params.yaml.

    Le principe : depuis le meilleur point connu, sonder un paramètre dans une
    direction. Si ça paye, le meilleur point se déplace et on repart au pas
    maximal. Sinon on essaie l'autre sens, puis le paramètre suivant ; quand
    tous ont été sondés dans les deux sens sans gain, le pas est divisé par
    deux — c'est ainsi qu'on resserre autour d'un optimum.
    """
    parameters = design["parameters"]
    free = free_parameters(parameters)
    if not free:
        raise ProposalError(
            "aucun paramètre manœuvrable : toutes les bornes sont figées"
        )

    points = evaluated_points(history, iterations_root)
    best = best_point(points)
    exploratory = best is None

    if exploratory:
        # Aucun point mesuré : soit rien n'a encore tourné, soit toutes les
        # tentatives ont échoué. On repart de la configuration courante avec un
        # pas réduit — s'arrêter ici priverait la boucle de toute chance de
        # sortir d'un mauvais départ.
        best = {
            "iteration": -1,
            "objective": float("-inf"),
            "values": {
                name: float(spec["value"]) for name, spec in parameters.items()
            },
        }

    # Combien d'essais depuis le meilleur point — succès ET échecs, car un
    # échec consomme une itération et informe autant qu'un résultat médiocre.
    attempts = sum(
        1 for record in history if int(record.get("iteration", -1)) > best["iteration"]
    )
    failures = sum(
        1
        for record in history
        if int(record.get("iteration", -1)) > best["iteration"]
        and not record.get("success")
    )

    # Le budget max_delta_pct se mesure depuis la DERNIÈRE itération réussie,
    # pas depuis la meilleure. Quand les deux diffèrent — une sonde qui a
    # déçu —, on ne peut pas revenir au meilleur point d'un bond : on s'en
    # rapproche autant que le budget l'autorise. La géométrie se déplace, elle
    # ne se téléporte pas.
    last = _last_point(points, parameters)

    # Un paramètre qu'on a fait varier sans que l'objectif bouge ne mérite plus
    # d'évaluation : chacune coûte plusieurs minutes de CFD.
    inert = _inert_parameters(points, parameters, free)
    active = [name for name in free if name not in inert] or free

    n = len(active)
    free = active
    # Deux sens épuisés sur un paramètre avant de passer au suivant : essayer
    # +7 % de corde puis +8 % d'épaisseur avant de seulement tenter -7 % de
    # corde gaspillerait des évaluations.
    index = (attempts // 2) % n
    direction = 1.0 if attempts % 2 == 0 else -1.0
    shrink = 0.5 ** (attempts // (2 * n))
    # Un échec signale une forme trop agressive : on resserre franchement.
    shrink *= 0.5 ** failures

    # Si la dernière sonde a payé, on poursuit dans la même direction plutôt
    # que de repartir du début du cycle : c'est une recherche linéaire, et
    # c'est ce qui permet à un paramètre de parcourir sa plage utile au lieu
    # d'avancer d'un pas toutes les 2n itérations.
    continuation = _last_improving_probe(points, best, parameters, free)
    if continuation is not None:
        forced_name, forced_direction = continuation
        index = free.index(forced_name)
        direction = forced_direction
        shrink = 0.5 ** failures

    probe_name = ""
    target = dict(best["values"])
    for offset in range(n):
        name = free[(index + offset) % n]
        spec = parameters[name]
        base_value = best["values"].get(name, float(spec["value"]))
        budget = max_abs_delta(base_value, spec)
        lo, hi = allowed_range(base_value, spec)
        candidate = min(max(base_value + direction * shrink * budget, lo), hi)
        if abs(candidate - base_value) <= abs(budget) * 1e-6:
            continue  # buté sur une borne : passer au paramètre suivant
        target[name] = candidate
        probe_name = name
        break

    if not probe_name:
        raise ProposalError(
            "aucune variation possible : tous les paramètres sont butés sur "
            "leurs bornes"
        )

    # Projection de la cible dans ce que le contrat autorise à cette itération.
    proposal: dict[str, float] = {}
    clamped: list[str] = []
    for name, spec in parameters.items():
        previous_value = last.get(name, float(spec["value"]))
        wanted = target.get(name, previous_value)
        lo, hi = allowed_range(previous_value, spec)
        value = min(max(wanted, lo), hi)
        if abs(value - wanted) > abs(wanted) * 1e-9 + 1e-12:
            clamped.append(name)
        proposal[name] = round(value, 9)

    if all(
        abs(proposal[name] - last.get(name, float(spec["value"])))
        <= 1e-12
        for name, spec in parameters.items()
    ):
        raise ProposalError(
            "aucune variation possible : la cible coïncide avec l'itération "
            "précédente"
        )

    origine = (
        "la configuration courante (aucune itération réussie)"
        if exploratory
        else f"le meilleur point (itération {best['iteration']}, "
             f"objectif {best['objective']:.4f})"
    )
    reason = (
        f"recherche par motif : {probe_name} "
        f"{best['values'].get(probe_name, 0.0):g} -> {target[probe_name]:g} "
        f"({'+' if direction > 0 else '-'}{shrink * 100:.0f} % du budget) "
        f"depuis {origine}"
    )
    if clamped:
        reason += (
            f" ; retour progressif vers le meilleur point, "
            f"{', '.join(clamped)} limité(s) par le budget de variation"
        )
    if failures:
        reason += f" ; pas resserré après {failures} échec(s)"

    return proposal, reason


INERT_RELATIVE_EFFECT = 1e-4
INERT_MIN_OBSERVATIONS = 2


def _inert_parameters(
    points: Sequence[Mapping[str, Any]],
    parameters: Mapping[str, Any],
    free: Sequence[str],
) -> set[str]:
    """Paramètres qu'on a fait varier sans que l'objectif bouge.

    Chaque évaluation coûte plusieurs minutes de CFD : continuer à sonder un
    paramètre dont deux essais n'ont rien changé, c'est dépenser le budget pour
    rien. La mesure ne porte que sur les paires de points qui ne diffèrent que
    par CE paramètre — sinon l'effet ne lui serait pas attribuable.
    """
    if len(points) < 2:
        return set()

    observations: dict[str, list[float]] = {name: [] for name in free}
    ordered = sorted(points, key=lambda p: p["iteration"])

    for i, later in enumerate(ordered):
        for earlier in ordered[:i]:
            moved = [
                name
                for name in free
                if abs(later["values"].get(name, 0.0) - earlier["values"].get(name, 0.0))
                > abs(
                    max_abs_delta(earlier["values"].get(name, 0.0), parameters[name])
                )
                * 1e-3
            ]
            if len(moved) != 1:
                continue
            reference = abs(earlier["objective"]) or 1.0
            observations[moved[0]].append(
                abs(later["objective"] - earlier["objective"]) / reference
            )

    return {
        name
        for name, effects in observations.items()
        if len(effects) >= INERT_MIN_OBSERVATIONS
        and max(effects) < INERT_RELATIVE_EFFECT
    }


def _last_improving_probe(
    points: Sequence[Mapping[str, Any]],
    best: Mapping[str, Any],
    parameters: Mapping[str, Any],
    free: Sequence[str],
) -> tuple[str, float] | None:
    """Paramètre et sens de la dernière sonde, si elle a amélioré l'objectif.

    Permet de continuer une direction qui paye au lieu de recommencer le cycle
    — sans cela, un paramètre n'avance que d'un pas toutes les 2n itérations et
    n'atteint jamais sa plage utile dans un budget réaliste.
    """
    if len(points) < 2:
        return None
    latest = max(points, key=lambda p: p["iteration"])
    if latest["iteration"] != best["iteration"]:
        return None  # la dernière évaluation n'est pas la meilleure

    earlier = [p for p in points if p["iteration"] < latest["iteration"]]
    if not earlier:
        return None
    previous = max(earlier, key=lambda p: p["iteration"])

    moved: list[tuple[str, float]] = []
    for name in free:
        spec = parameters.get(name)
        if not isinstance(spec, Mapping):
            continue
        before = previous["values"].get(name)
        after = latest["values"].get(name)
        if before is None or after is None:
            continue
        delta = after - before
        if abs(delta) > abs(max_abs_delta(before, spec)) * 1e-3:
            moved.append((name, 1.0 if delta > 0 else -1.0))

    # Une seule direction identifiable : sinon on ne saurait pas à quoi
    # attribuer le gain.
    return moved[0] if len(moved) == 1 else None


def _last_point(
    points: Sequence[Mapping[str, Any]], parameters: Mapping[str, Any]
) -> dict[str, float]:
    """Valeurs de la dernière itération RÉUSSIE — référence du budget de variation."""
    if points:
        return dict(max(points, key=lambda p: p["iteration"])["values"])
    return {name: float(spec["value"]) for name, spec in parameters.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Stratégie LLM
# ─────────────────────────────────────────────────────────────────────────────


def build_context(
    design: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    iterations_root: Path,
) -> dict:
    """Assemble ce que l'agent doit savoir pour décider.

    On lui fournit l'intervalle admissible déjà calculé pour chaque paramètre :
    lui faire recalculer bornes et budget de variation serait lui demander de
    refaire, en langage naturel, un travail que le code fait exactement.
    """
    parameters = design["parameters"]
    points = evaluated_points(history, iterations_root)
    best = best_point(points)
    base = best["values"] if best else {
        name: float(spec["value"]) for name, spec in parameters.items()
    }

    ranges: dict[str, Any] = {}
    for name, spec in parameters.items():
        value = base.get(name, float(spec["value"]))
        lo, hi = allowed_range(value, spec)
        ranges[name] = {
            "current": value,
            "allowed_range": [round(lo, 9), round(hi, 9)],
            "bounds": [float(spec["min"]), float(spec["max"])],
            "max_delta_pct": float(spec["max_delta_pct"]),
            "unit": spec.get("unit"),
            "frozen": name not in free_parameters(parameters),
        }

    return {
        "objective": mp.objective_label(design),
        "objective_note": "à MAXIMISER (les objectifs sont déjà normalisés)",
        "design_id": design.get("design_id"),
        "iteration": design.get("iteration"),
        "constraints": design.get("constraints"),
        "parameters": ranges,
        "best_so_far": best,
        "history": [
            {
                "iteration": record.get("iteration"),
                "success": record.get("success"),
                "status": record.get("status"),
                "Cd": record.get("Cd"),
                "Cl": record.get("Cl"),
                "Cl_Cd": record.get("Cl_Cd"),
                "objective": record.get("objective"),
                "converged": record.get("converged"),
                "error_message": record.get("error_message"),
                "values": (parameters_of(record, iterations_root) or {})
                and {
                    name: float(spec["value"])
                    for name, spec in (parameters_of(record, iterations_root) or {}).items()
                },
            }
            for record in history[-12:]
        ],
    }


def _extract_json(text: str) -> dict:
    """Récupère l'objet JSON d'une réponse, même entourée de texte."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ProposalError(f"réponse sans objet JSON : {text[:200]}")
    return json.loads(cleaned[start:end + 1])


def propose_llm(
    design: Mapping[str, Any],
    history: Sequence[Mapping[str, Any]],
    iterations_root: Path,
    model: str | None = None,
    api_key: str | None = None,
    client: Any = None,
) -> tuple[dict[str, float], str]:
    """Demande à Claude les valeurs de la prochaine itération.

    En cas de proposition invalide, l'erreur de validation lui est renvoyée
    telle quelle : les messages de la Phase 0 nomment le paramètre fautif et
    donnent l'intervalle admissible, ce qui suffit à se corriger.
    """
    if client is None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProposalError(
                "ANTHROPIC_API_KEY absente — impossible d'interroger l'agent"
            )
        try:
            import anthropic
        except ImportError as exc:
            raise ProposalError(
                "le paquet `anthropic` n'est pas installé"
            ) from exc
        client = anthropic.Anthropic(api_key=key)

    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    context = build_context(design, history, iterations_root)
    messages = [
        {
            "role": "user",
            "content": (
                "État de l'optimisation :\n\n"
                + json.dumps(context, indent=2, ensure_ascii=False, default=str)
                + "\n\nPropose les paramètres de la prochaine itération."
            ),
        }
    ]

    problems: list[str] = []
    for attempt in range(MAX_LLM_ATTEMPTS):
        try:
            response = client.messages.create(
                model=model or os.environ.get("AGENT_MODEL") or DEFAULT_MODEL,
                max_tokens=1500,
                system=system_prompt,
                messages=messages,
            )
            text = "".join(
                block.text for block in response.content
                if getattr(block, "type", "text") == "text"
            )
        except Exception as exc:
            raise ProposalError(f"appel à l'agent impossible : {exc}") from exc

        try:
            payload = _extract_json(text)
            proposed = payload.get("parameters")
            if not isinstance(proposed, dict):
                raise ProposalError("champ 'parameters' absent ou mal formé")
            values = {str(k): float(v) for k, v in proposed.items()}
        except (ProposalError, TypeError, ValueError, json.JSONDecodeError) as exc:
            problems.append(str(exc))
            messages += [
                {"role": "assistant", "content": text},
                {"role": "user", "content":
                    f"Réponse inexploitable : {exc}. Renvoie UNIQUEMENT l'objet "
                    f"JSON décrit dans les instructions."},
            ]
            continue

        candidate = apply_values(design, values)
        report = validate_design_params(candidate, previous=_previous_config(
            history, iterations_root, design))
        if report.ok:
            reasoning = str(payload.get("reasoning", "")).strip()
            expected = str(payload.get("expected_effect", "")).strip()
            confidence = str(payload.get("confidence", "")).strip()
            summary = " ".join(x for x in (reasoning, expected) if x)
            if confidence:
                summary += f" (confiance : {confidence})"
            return values, summary or "proposition de l'agent"

        problems.extend(report.errors)
        messages += [
            {"role": "assistant", "content": text},
            {"role": "user", "content":
                "Proposition refusée par la validation :\n- "
                + "\n- ".join(report.errors)
                + "\n\nCorrige et renvoie uniquement le JSON."},
        ]

    raise ProposalError(
        f"{MAX_LLM_ATTEMPTS} propositions invalides : " + " | ".join(problems[-4:])
    )


def _previous_config(
    history: Sequence[Mapping[str, Any]],
    iterations_root: Path,
    design: Mapping[str, Any],
) -> dict | None:
    """Configuration de la dernière itération réussie, référence du max_delta."""
    for record in reversed(list(history)):
        if not record.get("success"):
            continue
        directory = Path(iterations_root) / f"iter_{int(record['iteration']):04d}"
        config = directory / mp.ARCHIVED_CONFIG
        if config.is_file():
            try:
                return load_yaml(config)
            except Exception:
                return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Proposition
# ─────────────────────────────────────────────────────────────────────────────


def apply_values(
    design: Mapping[str, Any], values: Mapping[str, float]
) -> dict:
    """Applique des valeurs à une copie de la configuration, et incrémente l'itération.

    Seules les `value` bougent : bornes, unités et budgets restent ceux du
    concepteur.
    """
    candidate = copy.deepcopy(dict(design))
    candidate["iteration"] = int(design.get("iteration", 0)) + 1
    for name, value in values.items():
        if name not in candidate["parameters"]:
            raise ProposalError(f"paramètre inconnu proposé : {name!r}")
        candidate["parameters"][name]["value"] = float(value)
    return candidate


def propose(
    config_path: Path = DEFAULT_CONFIG,
    iterations_root: Path = DEFAULT_ITERATIONS,
    strategy: str = STRATEGY_AUTO,
    write: bool = True,
    model: str | None = None,
    client: Any = None,
) -> dict:
    """Construit, valide et écrit la configuration de l'itération suivante.

    Returns:
        Un compte rendu : stratégie retenue, valeurs proposées, justification,
        et la configuration complète.
    """
    config_path = Path(config_path)
    iterations_root = Path(iterations_root)
    design = load_yaml(config_path)
    records = mp.history(iterations_root)

    chosen = strategy
    notes: list[str] = []
    values: dict[str, float] | None = None
    rationale = ""

    if strategy in (STRATEGY_AUTO, STRATEGY_LLM):
        try:
            values, rationale = propose_llm(
                design, records, iterations_root, model=model, client=client
            )
            chosen = STRATEGY_LLM
        except ProposalError as exc:
            if strategy == STRATEGY_LLM:
                raise
            notes.append(f"agent indisponible ({exc}) — repli sur la recherche locale")

    if values is None:
        values, rationale = propose_local(design, records, iterations_root)
        chosen = STRATEGY_LOCAL

    candidate = apply_values(design, values)
    previous = _previous_config(records, iterations_root, design)
    report = validate_design_params(candidate, previous=previous, path=config_path)
    if not report.ok:
        raise ProposalError(
            f"proposition {chosen} invalide : " + " | ".join(report.errors)
        )
    notes.extend(report.warnings)

    if write:
        save_design_params(candidate, config_path)

    changed = {
        name: {
            "from": float(design["parameters"][name]["value"]),
            "to": float(candidate["parameters"][name]["value"]),
        }
        for name in candidate["parameters"]
        if float(candidate["parameters"][name]["value"])
        != float(design["parameters"][name]["value"])
    }

    return {
        "strategy": chosen,
        "iteration": candidate["iteration"],
        "rationale": rationale,
        "changed": changed,
        "values": {n: float(s["value"]) for n, s in candidate["parameters"].items()},
        "notes": notes,
        "written": bool(write),
        "config": candidate,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent/orchestrator.py",
        description="Propose les paramètres de l'itération suivante.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--iterations-dir", default=str(DEFAULT_ITERATIONS))
    parser.add_argument("--strategy", choices=STRATEGIES, default=STRATEGY_AUTO)
    parser.add_argument("--model", default=None)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="calcule la proposition sans écrire design_params.yaml",
    )
    parser.add_argument("--explain", action="store_true")
    parser.add_argument("--propose", action="store_true", help="(implicite)")
    args = parser.parse_args(argv)

    try:
        result = propose(
            Path(args.config), Path(args.iterations_dir),
            strategy=args.strategy, write=not args.dry_run, model=args.model,
        )
    except ProposalError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    if args.explain:
        print(f"stratégie : {result['strategy']}")
        print(f"itération : {result['iteration']}")
        print(f"raison    : {result['rationale']}")
        for name, change in result["changed"].items():
            print(f"  {name}: {change['from']:g} -> {change['to']:g}")
        for note in result["notes"]:
            print(f"  [note] {note}")
    else:
        print(json.dumps(
            {k: v for k, v in result.items() if k != "config"},
            indent=2, ensure_ascii=False, default=str,
        ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
