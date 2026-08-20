"""Utilitaires Core — chargement et validation stricte de design_params.yaml.

Ce module est le gardien du contrat défini au §3.1 du Master Document.
Il est utilisé :
  - par `pipeline/master_pipeline.py` avant toute exécution (étape 1) ;
  - par l'agent / orchestrateur pour vérifier une proposition AVANT de
    l'écrire sur disque ;
  - en ligne de commande :

        python3 pipeline/utils.py configs/design_params.yaml
        python3 pipeline/utils.py configs/design_params.yaml --previous data/iterations/iter_0003/design_params.yaml

Trois familles de règles sont vérifiées :
  1. STRUCTURE   — clés obligatoires, types, aucune clé inconnue.
  2. BORNES      — min < max et min <= value <= max.
  3. MAX_DELTA   — |value - value_precedent| <= max_delta_pct % de la
                   dernière itération RÉUSSIE (vérifié seulement si une
                   configuration précédente est fournie).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# ─────────────────────────────────────────────────────────────────────────────
# Constantes du contrat
# ─────────────────────────────────────────────────────────────────────────────

TOP_LEVEL_KEYS: tuple[str, ...] = (
    "iteration",
    "design_id",
    "parameters",
    "constraints",
    "objectives",
)

PARAMETER_KEYS: tuple[str, ...] = (
    "value",
    "min",
    "max",
    "max_delta_pct",
    "unit",
)

ALLOWED_OBJECTIVES: tuple[str, ...] = (
    "maximize_Cl_Cd",
    "maximize_downforce",
    "minimize_Cd",
)

# Un nom de paramètre doit être un identifiant Fusion 360 valide
# (userParameters.itemByName).
PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DESIGN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Tolérance sur les comparaisons flottantes (bornes et deltas), afin qu'une
# valeur écrite exactement à la borne ne soit pas rejetée pour une erreur
# d'arrondi de représentation.
TOL = 1e-9

# En-dessous de ce seuil, une valeur précédente est considérée nulle et le
# delta relatif est indéfini : on retombe alors sur un pourcentage de
# l'amplitude (max - min). Indispensable pour les paramètres qui traversent
# zéro, par exemple aoa_deg.
ZERO_EPS = 1e-9


def load_env(path: str | Path | None = None) -> dict[str, str]:
    """Charge `.env` dans l'environnement, sans écraser ce qui est déjà défini.

    Appelé par les points d'entrée. Sans cela, `.env` serait un fichier
    documenté que rien ne lit — les chemins Fusion, `FOAM_BASHRC` et la clé
    d'API resteraient sans effet, et le diagnostic serait déroutant.

    Une variable déjà présente dans l'environnement gagne : elle vient d'un
    appel explicite, qui doit primer sur un fichier de configuration.
    """
    target = Path(path) if path else REPO_ROOT / ".env"
    if not target.is_file():
        return {}

    loaded: dict[str, str] = {}
    try:
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value
                loaded[key] = value
    except OSError:
        return {}
    return loaded


class ConfigValidationError(Exception):
    """Levée quand design_params.yaml viole le contrat.

    L'attribut `errors` contient la liste complète des violations, pour que
    l'agent reçoive un feedback exploitable en une seule passe.
    """

    def __init__(self, errors: list[str], path: str | Path | None = None) -> None:
        self.errors = list(errors)
        self.path = str(path) if path is not None else None
        header = "design_params invalide"
        if self.path:
            header += f" ({self.path})"
        header += f" — {len(self.errors)} erreur(s) :"
        super().__init__("\n".join([header, *(f"  - {e}" for e in self.errors)]))


@dataclass
class ValidationReport:
    """Résultat d'une validation : erreurs bloquantes et avertissements."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    path: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def raise_if_invalid(self) -> "ValidationReport":
        if self.errors:
            raise ConfigValidationError(self.errors, self.path)
        return self

    def format(self) -> str:
        lines: list[str] = []
        target = self.path or "design_params"
        if self.ok:
            lines.append(f"OK — {target} respecte le contrat.")
        else:
            lines.append(f"ECHEC — {target} : {len(self.errors)} erreur(s).")
            lines.extend(f"  [ERREUR] {e}" for e in self.errors)
        lines.extend(f"  [AVERTISSEMENT] {w}" for w in self.warnings)
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Aides de typage
# ─────────────────────────────────────────────────────────────────────────────


def _is_number(v: Any) -> bool:
    """Vrai pour un int/float fini. Les booléens sont explicitement rejetés
    (en Python, `True` est un `int`, ce qui masquerait une faute de frappe)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return math.isfinite(float(v))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _fmt(v: Any) -> str:
    return f"{v:g}" if _is_number(v) else repr(v)


# ─────────────────────────────────────────────────────────────────────────────
# Chargement
# ─────────────────────────────────────────────────────────────────────────────


def load_yaml(path: str | Path) -> dict:
    """Charge un YAML et garantit qu'il contient un mapping à la racine."""
    p = Path(path)
    if not p.is_file():
        raise ConfigValidationError([f"fichier introuvable : {p}"], p)
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ConfigValidationError([f"YAML illisible : {exc}"], p) from exc
    if data is None:
        raise ConfigValidationError(["fichier vide"], p)
    if not isinstance(data, dict):
        raise ConfigValidationError(
            [f"la racine doit être un mapping, obtenu {type(data).__name__}"], p
        )
    return data


def load_design_params(
    path: str | Path,
    previous: str | Path | Mapping[str, Any] | None = None,
) -> dict:
    """Charge et valide design_params.yaml. Lève ConfigValidationError si invalide.

    `previous` : configuration de la dernière itération RÉUSSIE (chemin ou
    dict). Si fournie, la règle max_delta_pct est appliquée.
    """
    data = load_yaml(path)
    prev_data: Mapping[str, Any] | None = None
    if previous is not None:
        prev_data = load_yaml(previous) if isinstance(previous, (str, Path)) else previous
    validate_design_params(data, previous=prev_data, path=path).raise_if_invalid()
    return data


def save_design_params(data: Mapping[str, Any], path: str | Path) -> Path:
    """Écrit design_params.yaml après validation structurelle.

    Refuse d'écrire une configuration invalide : c'est le dernier rempart
    avant que l'agent ne corrompe le seul fichier qu'il a le droit de toucher.
    """
    validate_design_params(data, path=path).raise_if_invalid()
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(dict(data), fh, sort_keys=False, allow_unicode=True)
    return p


# ─────────────────────────────────────────────────────────────────────────────
# Validation — 1. structure et bornes
# ─────────────────────────────────────────────────────────────────────────────


def _validate_parameter(name: str, spec: Any, report: ValidationReport) -> None:
    """Valide un bloc de paramètre : structure, types, bornes."""
    where = f"parameters.{name}"

    if not PARAM_NAME_RE.match(name):
        report.errors.append(
            f"{where} : nom invalide — doit être un identifiant Fusion "
            f"(lettres, chiffres, '_' ; ne commence pas par un chiffre)"
        )

    if not isinstance(spec, dict):
        report.errors.append(
            f"{where} : doit être un mapping, obtenu {type(spec).__name__}"
        )
        return

    missing = [k for k in PARAMETER_KEYS if k not in spec]
    if missing:
        report.errors.append(f"{where} : clé(s) obligatoire(s) manquante(s) : {missing}")
    unknown = [k for k in spec if k not in PARAMETER_KEYS]
    if unknown:
        report.errors.append(
            f"{where} : clé(s) inconnue(s) : {unknown} — attendu {list(PARAMETER_KEYS)}"
        )

    # Types numériques
    numeric_ok = True
    for key in ("value", "min", "max", "max_delta_pct"):
        if key not in spec:
            numeric_ok = False
            continue
        if not _is_number(spec[key]):
            report.errors.append(
                f"{where}.{key} : doit être un nombre fini, obtenu {spec[key]!r}"
            )
            numeric_ok = False

    if "unit" in spec:
        if not isinstance(spec["unit"], str) or not spec["unit"].strip():
            report.errors.append(
                f"{where}.unit : doit être une chaîne non vide, obtenu {spec['unit']!r}"
            )

    if not numeric_ok:
        return  # inutile de tester les bornes sur des types cassés

    value = float(spec["value"])
    vmin = float(spec["min"])
    vmax = float(spec["max"])
    delta_pct = float(spec["max_delta_pct"])

    # 2. BORNES
    if vmin >= vmax:
        report.errors.append(
            f"{where} : min ({_fmt(vmin)}) doit être strictement inférieur "
            f"à max ({_fmt(vmax)})"
        )
    if value < vmin - TOL:
        report.errors.append(
            f"{where} : value ({_fmt(value)}) est hors bornes, en dessous de "
            f"min ({_fmt(vmin)})"
        )
    if value > vmax + TOL:
        report.errors.append(
            f"{where} : value ({_fmt(value)}) est hors bornes, au dessus de "
            f"max ({_fmt(vmax)})"
        )

    # max_delta_pct
    if delta_pct <= 0:
        report.errors.append(
            f"{where}.max_delta_pct : doit être strictement positif, "
            f"obtenu {_fmt(delta_pct)}"
        )
    elif delta_pct > 100.0:
        report.errors.append(
            f"{where}.max_delta_pct : doit être <= 100, obtenu {_fmt(delta_pct)}"
        )
    elif delta_pct > 25.0:
        report.warnings.append(
            f"{where}.max_delta_pct = {_fmt(delta_pct)} % : pas très large, "
            f"risque d'échec du remaillage — 5 à 15 % est le régime sûr"
        )


def _validate_structure(data: Mapping[str, Any], report: ValidationReport) -> None:
    """Valide la structure de premier niveau et délègue aux paramètres."""
    missing = [k for k in TOP_LEVEL_KEYS if k not in data]
    if missing:
        report.errors.append(f"clé(s) racine obligatoire(s) manquante(s) : {missing}")
    unknown = [k for k in data if k not in TOP_LEVEL_KEYS]
    if unknown:
        report.errors.append(
            f"clé(s) racine inconnue(s) : {unknown} — attendu {list(TOP_LEVEL_KEYS)}"
        )

    # iteration
    if "iteration" in data:
        if not _is_int(data["iteration"]):
            report.errors.append(
                f"iteration : doit être un entier, obtenu {data['iteration']!r}"
            )
        elif data["iteration"] < 0:
            report.errors.append(
                f"iteration : doit être >= 0, obtenu {data['iteration']}"
            )

    # design_id
    if "design_id" in data:
        did = data["design_id"]
        if not isinstance(did, str) or not did.strip():
            report.errors.append(
                f"design_id : doit être une chaîne non vide, obtenu {did!r}"
            )
        elif not DESIGN_ID_RE.match(did):
            report.errors.append(
                f"design_id : caractères non autorisés dans {did!r} — "
                f"attendu lettres, chiffres, '.', '_', '-'"
            )

    # parameters
    if "parameters" in data:
        params = data["parameters"]
        if not isinstance(params, dict):
            report.errors.append(
                f"parameters : doit être un mapping, obtenu {type(params).__name__}"
            )
        elif not params:
            report.errors.append("parameters : doit contenir au moins un paramètre")
        else:
            for name, spec in params.items():
                _validate_parameter(str(name), spec, report)

    # constraints
    if "constraints" in data:
        cons = data["constraints"]
        if not isinstance(cons, dict):
            report.errors.append(
                f"constraints : doit être un mapping, obtenu {type(cons).__name__}"
            )
        else:
            if "topology_preserving" not in cons:
                report.errors.append("constraints.topology_preserving : manquant")
            elif not isinstance(cons["topology_preserving"], bool):
                report.errors.append(
                    f"constraints.topology_preserving : doit être un booléen, "
                    f"obtenu {cons['topology_preserving']!r}"
                )
            if "min_wall_thickness_mm" not in cons:
                report.errors.append("constraints.min_wall_thickness_mm : manquant")
            elif not _is_number(cons["min_wall_thickness_mm"]):
                report.errors.append(
                    f"constraints.min_wall_thickness_mm : doit être un nombre, "
                    f"obtenu {cons['min_wall_thickness_mm']!r}"
                )
            elif float(cons["min_wall_thickness_mm"]) <= 0:
                report.errors.append(
                    f"constraints.min_wall_thickness_mm : doit être strictement "
                    f"positif, obtenu {_fmt(cons['min_wall_thickness_mm'])}"
                )

    # objectives
    if "objectives" in data:
        obj = data["objectives"]
        if not isinstance(obj, dict):
            report.errors.append(
                f"objectives : doit être un mapping, obtenu {type(obj).__name__}"
            )
        elif "primary" not in obj:
            report.errors.append("objectives.primary : manquant")
        elif obj["primary"] not in ALLOWED_OBJECTIVES:
            report.errors.append(
                f"objectives.primary : {obj['primary']!r} inconnu — "
                f"attendu l'un de {list(ALLOWED_OBJECTIVES)}"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Validation — 3. max_delta_pct
# ─────────────────────────────────────────────────────────────────────────────


def max_abs_delta(previous_value: float, spec: Mapping[str, Any]) -> float:
    """Variation absolue maximale autorisée depuis `previous_value`.

    Règle nominale : max_delta_pct % de |previous_value|.

    Deux cas imposent de la mesurer sur l'amplitude `max - min` :

    1. **`previous_value` est nul.** Le pourcentage relatif est indéfini ; sans
       ce repli, un paramètre valant 0 serait figé à jamais.

    2. **Les bornes encadrent zéro** (`min < 0 < max`). Un pourcentage d'une
       grandeur qui change de signe ne contraint rien de sensé : près de zéro
       il autorise des pas infinitésimaux, loin de zéro des pas énormes. Pire,
       il est ASYMÉTRIQUE — pour une incidence bornée à [-2, 12], passer de 0 à
       -1,68° coûte une itération, mais revenir de -1,68° à 0 en coûte huit,
       chaque pas étant limité à 12 % de la valeur courante. Une optimisation
       s'y piège : elle explore une direction et ne peut plus en sortir.
       Rapporter le budget à l'amplitude rend la règle symétrique, ce qu'une
       contrainte de sécurité doit être.

    Le garde-fou visé par le Master Document — empêcher un saut qui casserait
    le maillage — est préservé dans les deux cas : le budget reste une petite
    fraction de la plage utile du paramètre.
    """
    pct = float(spec["max_delta_pct"]) / 100.0
    low, high = float(spec["min"]), float(spec["max"])
    amplitude = abs(high - low) * pct

    if low < 0.0 < high:
        return amplitude
    prev = abs(float(previous_value))
    if prev > ZERO_EPS:
        return prev * pct
    return amplitude


def allowed_range(
    previous_value: float, spec: Mapping[str, Any]
) -> tuple[float, float]:
    """Intervalle réellement admissible à la prochaine itération.

    Intersection de [min, max] et de la bande de variation autorisée autour de
    `previous_value`. L'agent doit choisir sa proposition dans cet intervalle.
    """
    band = max_abs_delta(previous_value, spec)
    lo = max(float(spec["min"]), float(previous_value) - band)
    hi = min(float(spec["max"]), float(previous_value) + band)
    return lo, hi


def _validate_delta(
    data: Mapping[str, Any], previous: Mapping[str, Any], report: ValidationReport
) -> None:
    """Compare la configuration proposée à la dernière itération réussie."""
    prev_params = previous.get("parameters")
    params = data.get("parameters")
    if not isinstance(params, dict) or not isinstance(prev_params, dict):
        report.errors.append(
            "comparaison impossible : 'parameters' absent ou invalide dans la "
            "configuration courante ou précédente"
        )
        return

    # Le design ne doit pas changer d'identité entre deux itérations.
    if data.get("design_id") != previous.get("design_id"):
        report.errors.append(
            f"design_id : a changé entre les itérations "
            f"({previous.get('design_id')!r} -> {data.get('design_id')!r})"
        )

    if _is_int(data.get("iteration")) and _is_int(previous.get("iteration")):
        if data["iteration"] <= previous["iteration"]:
            report.errors.append(
                f"iteration : doit être strictement croissante — "
                f"{previous['iteration']} -> {data['iteration']}"
            )
        elif data["iteration"] != previous["iteration"] + 1:
            report.warnings.append(
                f"iteration : saut de {previous['iteration']} à {data['iteration']} "
                f"(incrément de 1 attendu)"
            )

    # L'ensemble des paramètres doit rester identique : ajouter ou retirer un
    # paramètre en cours d'optimisation invaliderait la comparaison des runs.
    added = sorted(set(params) - set(prev_params))
    removed = sorted(set(prev_params) - set(params))
    if added:
        report.errors.append(f"paramètre(s) ajouté(s) depuis l'itération précédente : {added}")
    if removed:
        report.errors.append(f"paramètre(s) supprimé(s) depuis l'itération précédente : {removed}")

    for name in sorted(set(params) & set(prev_params)):
        spec = params[name]
        prev_spec = prev_params[name]
        if not isinstance(spec, dict) or not isinstance(prev_spec, dict):
            continue
        if not all(_is_number(spec.get(k)) for k in ("value", "min", "max", "max_delta_pct")):
            continue  # déjà signalé par la validation structurelle
        if not _is_number(prev_spec.get("value")):
            report.errors.append(
                f"parameters.{name} : value précédente invalide "
                f"({prev_spec.get('value')!r}) — comparaison impossible"
            )
            continue

        # Les bornes et l'enveloppe de variation appartiennent au concepteur,
        # pas à l'agent : il ne doit pas les desserrer pour s'autoriser un
        # saut plus grand.
        for key in ("min", "max", "max_delta_pct", "unit"):
            if key in spec and key in prev_spec and spec[key] != prev_spec[key]:
                report.errors.append(
                    f"parameters.{name}.{key} : modifié par rapport à l'itération "
                    f"précédente ({_fmt(prev_spec[key])} -> {_fmt(spec[key])}) — "
                    f"seul 'value' peut changer"
                )

        prev_value = float(prev_spec["value"])
        value = float(spec["value"])
        band = max_abs_delta(prev_value, spec)
        change = abs(value - prev_value)
        if change > band + TOL:
            lo, hi = allowed_range(prev_value, spec)
            pct = (
                change / abs(prev_value) * 100.0
                if abs(prev_value) > ZERO_EPS
                else float("inf")
            )
            pct_txt = f"{pct:.2f} %" if math.isfinite(pct) else "non défini (valeur précédente nulle)"
            report.errors.append(
                f"parameters.{name} : variation {_fmt(prev_value)} -> {_fmt(value)} "
                f"= {_fmt(change)} ({pct_txt}) dépasse max_delta_pct = "
                f"{_fmt(spec['max_delta_pct'])} % (variation absolue max "
                f"{_fmt(band)}) — intervalle autorisé [{_fmt(lo)}, {_fmt(hi)}]"
            )


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée de validation
# ─────────────────────────────────────────────────────────────────────────────


def validate_design_params(
    data: Mapping[str, Any],
    previous: Mapping[str, Any] | None = None,
    path: str | Path | None = None,
) -> ValidationReport:
    """Valide une configuration design_params en mémoire.

    Args:
        data: la configuration à valider.
        previous: configuration de la dernière itération RÉUSSIE. Si fournie,
            la règle max_delta_pct est appliquée en plus.
        path: chemin d'origine, uniquement pour les messages.

    Returns:
        Un ValidationReport. N'exception PAS : appeler `.raise_if_invalid()`
        ou tester `.ok` pour décider. Toutes les violations sont collectées en
        une passe, afin que l'agent puisse corriger d'un coup.
    """
    report = ValidationReport(path=str(path) if path is not None else None)

    if not isinstance(data, Mapping):
        report.errors.append(
            f"la racine doit être un mapping, obtenu {type(data).__name__}"
        )
        return report

    _validate_structure(data, report)
    if previous is not None:
        _validate_delta(data, previous, report)
    return report


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline/utils.py",
        description="Valide un fichier design_params.yaml (structure, bornes, max_delta_pct).",
    )
    parser.add_argument(
        "config",
        nargs="?",
        default="configs/design_params.yaml",
        help="chemin du design_params.yaml à valider (défaut : configs/design_params.yaml)",
    )
    parser.add_argument(
        "--previous",
        metavar="YAML",
        default=None,
        help="design_params.yaml de la dernière itération réussie ; active la règle max_delta_pct",
    )
    parser.add_argument(
        "--show-ranges",
        action="store_true",
        help="affiche, pour chaque paramètre, l'intervalle admissible à la prochaine itération",
    )
    args = parser.parse_args(argv)

    try:
        data = load_yaml(args.config)
        previous = load_yaml(args.previous) if args.previous else None
    except ConfigValidationError as exc:
        print(exc, file=sys.stderr)
        return 2

    report = validate_design_params(data, previous=previous, path=args.config)
    stream = sys.stdout if report.ok else sys.stderr
    print(report.format(), file=stream)

    if args.show_ranges and isinstance(data.get("parameters"), dict):
        print("\nIntervalles admissibles à la prochaine itération :")
        for name, spec in data["parameters"].items():
            if not isinstance(spec, dict) or not all(
                _is_number(spec.get(k)) for k in ("value", "min", "max", "max_delta_pct")
            ):
                print(f"  {name}: (non calculable, paramètre invalide)")
                continue
            lo, hi = allowed_range(float(spec["value"]), spec)
            unit = spec.get("unit", "")
            print(
                f"  {name}: [{_fmt(lo)}, {_fmt(hi)}] {unit} "
                f"(actuel {_fmt(spec['value'])}, bornes [{_fmt(spec['min'])}, "
                f"{_fmt(spec['max'])}], max_delta {_fmt(spec['max_delta_pct'])} %)"
            )

    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
