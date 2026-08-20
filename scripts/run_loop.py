"""Boucle d'optimisation : évaluer, proposer, recommencer (Master Doc §3.5).

    python3 scripts/run_loop.py --max-iterations 15
    python3 scripts/run_loop.py --max-iterations 15 --strategy local
    python3 scripts/run_loop.py --resume            # reprend une série en cours

À chaque tour :

    1. `master_pipeline.run_iteration()` — géométrie, CFD, archivage
    2. `orchestrator.propose()` — nouvelles valeurs dans design_params.yaml
    3. on recommence, jusqu'au budget d'itérations ou à l'arrêt sur stagnation

La boucle est conçue pour tourner sans surveillance :

- **elle ne s'arrête pas sur un échec** : une itération ratée est archivée, la
  stratégie resserre le pas et la suivante repart de la meilleure forme connue.
  Seuls des échecs consécutifs, signe d'un problème de fond, l'interrompent ;
- **elle est reprenable** : tout l'état vit dans `data/iterations/`, donc une
  interruption ne perd que l'itération en cours ;
- **elle s'arrête d'elle-même** quand plus rien ne progresse, plutôt que de
  brûler le budget sur du bruit numérique.
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent import orchestrator  # noqa: E402
from agent.orchestrator import ProposalError  # noqa: E402
from pipeline import master_pipeline as mp  # noqa: E402
from pipeline.utils import load_env, load_yaml, save_design_params  # noqa: E402

DEFAULT_CONFIG = REPO_ROOT / "configs" / "design_params.yaml"
DEFAULT_CFD = REPO_ROOT / "configs" / "cfd_settings.yaml"
DEFAULT_ITERATIONS = REPO_ROOT / "data" / "iterations"
SUMMARY_FILENAME = "optimization_summary.json"

STOP_BUDGET = "budget_epuise"
STOP_STAGNATION = "stagnation"
STOP_CONSECUTIVE_FAILURES = "echecs_consecutifs"
STOP_NO_PROPOSAL = "plus_de_proposition"
STOP_INTERRUPTED = "interrompu"

_interrupted = False


def _handle_signal(signum, frame):  # pragma: no cover - dépend du système
    """Ctrl-C : on termine l'itération en cours puis on s'arrête proprement.

    Couper au milieu d'un run OpenFOAM laisserait une itération à moitié
    archivée, que la reprise interpréterait mal.
    """
    global _interrupted
    _interrupted = True
    print(
        "\n[loop] interruption demandée — arrêt après l'itération en cours",
        file=sys.stderr,
    )


def _fmt(value: Any, spec: str = ".5f") -> str:
    return format(value, spec) if isinstance(value, (int, float)) else "—"


def run_loop(
    config_path: Path = DEFAULT_CONFIG,
    cfd_settings_path: Path = DEFAULT_CFD,
    iterations_root: Path = DEFAULT_ITERATIONS,
    max_iterations: int = 20,
    strategy: str = orchestrator.STRATEGY_AUTO,
    geometry_backend: str | None = None,
    skip_cfd: bool = False,
    max_consecutive_failures: int = 4,
    stagnation_patience: int = 6,
    min_relative_gain: float = 1e-3,
    cfd_timeout_s: int | None = None,
    on_iteration=None,
) -> dict:
    """Enchaîne les itérations et retourne le bilan de la série."""
    config_path = Path(config_path)
    iterations_root = Path(iterations_root)
    started = time.time()

    records: list[dict] = []
    consecutive_failures = 0
    best_objective: float | None = None
    best_iteration: int | None = None
    since_improvement = 0
    stop_reason = STOP_BUDGET

    design = load_yaml(config_path)
    objective_label = mp.objective_label(design)
    print(
        f"[loop] objectif « {objective_label} » — budget {max_iterations} itérations",
        file=sys.stderr,
    )

    for step in range(max_iterations):
        if _interrupted:
            stop_reason = STOP_INTERRUPTED
            break

        record = mp.run_iteration(
            config_path=config_path,
            cfd_settings_path=cfd_settings_path,
            iterations_root=iterations_root,
            skip_cfd=skip_cfd,
            geometry_backend=geometry_backend,
            cfd_timeout_s=cfd_timeout_s,
        )
        records.append(record)

        if record["success"]:
            consecutive_failures = 0
            objective = record.get("objective")
            improved = (
                isinstance(objective, (int, float))
                and (
                    best_objective is None
                    or objective > best_objective * (1 + min_relative_gain)
                    or (best_objective <= 0 and objective > best_objective)
                )
            )
            if improved:
                best_objective = float(objective)
                best_iteration = int(record["iteration"])
                since_improvement = 0
            else:
                since_improvement += 1
            marker = "  <- meilleur" if improved else ""
            print(
                f"[loop] iter {record['iteration']:>3} | Cd {_fmt(record['Cd'])} "
                f"| Cl {_fmt(record['Cl'])} | Cl/Cd {_fmt(record['Cl_Cd'], '.2f')} "
                f"| {record['duration_s']}s{marker}",
                file=sys.stderr,
            )
        else:
            consecutive_failures += 1
            since_improvement += 1
            print(
                f"[loop] iter {record['iteration']:>3} | ÉCHEC "
                f"[{record['status']}/{record['stage']}] {record['error_message']}",
                file=sys.stderr,
            )

        if on_iteration is not None:
            on_iteration(record)

        if consecutive_failures >= max_consecutive_failures:
            stop_reason = STOP_CONSECUTIVE_FAILURES
            print(
                f"[loop] {consecutive_failures} échecs consécutifs — arrêt. "
                f"La cause est probablement structurelle, pas une question de pas.",
                file=sys.stderr,
            )
            break

        if since_improvement >= stagnation_patience and best_objective is not None:
            stop_reason = STOP_STAGNATION
            print(
                f"[loop] aucun gain depuis {since_improvement} itérations — arrêt.",
                file=sys.stderr,
            )
            break

        if step == max_iterations - 1:
            break  # budget épuisé : inutile de proposer une itération de plus

        try:
            proposal = orchestrator.propose(
                config_path=config_path,
                iterations_root=iterations_root,
                strategy=strategy,
            )
        except ProposalError as exc:
            stop_reason = STOP_NO_PROPOSAL
            print(f"[loop] plus de proposition possible : {exc}", file=sys.stderr)
            break

        changes = ", ".join(
            f"{name} {change['from']:g}->{change['to']:g}"
            for name, change in proposal["changed"].items()
        )
        print(
            f"[loop]      proposition [{proposal['strategy']}] "
            f"{changes or 'aucun changement'}",
            file=sys.stderr,
        )
        for note in proposal["notes"]:
            print(f"[loop]      note : {note}", file=sys.stderr)

    summary = {
        "objective": objective_label,
        "iterations_run": len(records),
        "successes": sum(1 for r in records if r["success"]),
        "failures": sum(1 for r in records if not r["success"]),
        "best_iteration": best_iteration,
        "best_objective": best_objective,
        "stop_reason": stop_reason,
        "duration_s": round(time.time() - started, 1),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "history": [
            {
                "iteration": r["iteration"],
                "success": r["success"],
                "status": r["status"],
                "Cd": r.get("Cd"),
                "Cl": r.get("Cl"),
                "Cl_Cd": r.get("Cl_Cd"),
                "objective": r.get("objective"),
                "error_message": r.get("error_message"),
            }
            for r in records
        ],
    }

    best = mp.last_successful(iterations_root)
    if best_iteration is not None:
        summary["best_parameters"] = orchestrator.parameters_of(
            {"iteration": best_iteration}, iterations_root
        )
        first = next((r for r in records if r["success"]), None)
        if first and isinstance(first.get("objective"), (int, float)) and best_objective:
            initial = float(first["objective"])
            if initial:
                summary["improvement_pct"] = round(
                    (best_objective - initial) / abs(initial) * 100.0, 2
                )
    del best

    try:
        Path(iterations_root).mkdir(parents=True, exist_ok=True)
        (Path(iterations_root) / SUMMARY_FILENAME).write_text(
            json.dumps(summary, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass

    return summary


def resume_point(config_path: Path, iterations_root: Path) -> int | None:
    """Cale le compteur d'itérations après la dernière archive.

    Relancer une série interrompue avec une configuration dont le compteur est
    resté en arrière écraserait des itérations déjà calculées — et l'historique
    sur lequel la stratégie s'appuie deviendrait incohérent.

    Returns:
        Le nouveau numéro d'itération s'il a fallu l'avancer, None sinon.
    """
    archived = mp.iteration_dirs(iterations_root)
    if not archived:
        return None

    numbers = []
    for directory in archived:
        try:
            numbers.append(int(directory.name.split("_")[1]))
        except (IndexError, ValueError):
            continue
    if not numbers:
        return None

    design = load_yaml(config_path)
    following = max(numbers) + 1
    if int(design.get("iteration", 0)) >= following:
        return None

    design["iteration"] = following
    save_design_params(design, config_path)
    return following


def build_report(iterations_root: Path) -> str:
    """Rapport lisible d'une série déjà exécutée.

    Après plusieurs heures sans surveillance, `optimization_summary.json` dit
    l'essentiel mais se lit mal. Ce tableau montre la trajectoire : ce qui a
    bougé, ce que ça a donné, et où ça a échoué.
    """
    records = mp.history(iterations_root)
    if not records:
        return f"Aucune itération archivée dans {iterations_root}."

    lines: list[str] = []
    header = f"{'iter':>5} {'statut':<18} {'Cd':>9} {'Cl':>9} {'Cl/Cd':>8} " \
             f"{'objectif':>9} {'durée':>7}  paramètres"
    lines.append(header)
    lines.append("-" * len(header))

    best_objective: float | None = None
    best_iteration: int | None = None

    for record in records:
        iteration = record.get("iteration")
        values = orchestrator.parameters_of(record, iterations_root) or {}
        shown = ", ".join(
            f"{name}={float(spec['value']):g}"
            for name, spec in values.items()
            if name != "span"
        )
        objective = record.get("objective")
        if isinstance(objective, (int, float)) and (
            best_objective is None or objective > best_objective
        ):
            best_objective, best_iteration = float(objective), iteration

        statut = record.get("status", "?")
        if not record.get("success"):
            statut = f"{statut}/{record.get('stage', '?')}"

        lines.append(
            f"{iteration:>5} {statut:<18} {_fmt(record.get('Cd')):>9} "
            f"{_fmt(record.get('Cl')):>9} {_fmt(record.get('Cl_Cd'), '.2f'):>8} "
            f"{_fmt(objective, '.4f'):>9} "
            f"{_fmt(record.get('duration_s'), '.0f'):>6}s  {shown}"
        )
        if not record.get("success") and record.get("error_message"):
            lines.append(f"{'':>5} └─ {record['error_message'][:120]}")

    lines.append("")
    successes = sum(1 for r in records if r.get("success"))
    lines.append(
        f"{len(records)} itérations — {successes} réussies, "
        f"{len(records) - successes} échouées"
    )
    if best_iteration is not None:
        lines.append(
            f"meilleure : itération {best_iteration}, objectif {best_objective:.4f}"
        )
        first = next(
            (r for r in records
             if r.get("success") and isinstance(r.get("objective"), (int, float))),
            None,
        )
        if first and first["objective"]:
            gain = (best_objective - first["objective"]) / abs(first["objective"]) * 100
            lines.append(f"gain      : {gain:+.2f} % depuis la première évaluation")
        best_values = orchestrator.parameters_of(
            {"iteration": best_iteration}, iterations_root
        ) or {}
        for name, spec in best_values.items():
            lines.append(f"    {name:<12} {float(spec['value']):g} {spec.get('unit', '')}")
    return "\n".join(lines)


def print_summary(summary: dict) -> None:
    print("\n" + "=" * 62, file=sys.stderr)
    print(f"  Objectif        : {summary['objective']}", file=sys.stderr)
    print(
        f"  Itérations      : {summary['iterations_run']} "
        f"({summary['successes']} réussies, {summary['failures']} échouées)",
        file=sys.stderr,
    )
    if summary["best_iteration"] is not None:
        print(
            f"  Meilleure       : itération {summary['best_iteration']} — "
            f"objectif {summary['best_objective']:.4f}",
            file=sys.stderr,
        )
    if "improvement_pct" in summary:
        print(f"  Gain            : {summary['improvement_pct']:+.2f} %", file=sys.stderr)
    print(f"  Arrêt           : {summary['stop_reason']}", file=sys.stderr)
    print(f"  Durée           : {summary['duration_s']} s", file=sys.stderr)
    print("=" * 62, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/run_loop.py",
        description="Boucle d'optimisation aérodynamique autonome.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cfd-settings", default=str(DEFAULT_CFD))
    parser.add_argument("--iterations-dir", default=str(DEFAULT_ITERATIONS))
    parser.add_argument("--max-iterations", type=int, default=20)
    parser.add_argument(
        "--strategy", choices=orchestrator.STRATEGIES,
        default=orchestrator.STRATEGY_AUTO,
        help="'auto' interroge l'agent et retombe sur la recherche locale",
    )
    parser.add_argument("--geometry-backend", default=None)
    parser.add_argument("--skip-cfd", action="store_true")
    parser.add_argument("--cfd-timeout", type=int, default=None)
    parser.add_argument("--max-consecutive-failures", type=int, default=4)
    parser.add_argument("--stagnation-patience", type=int, default=6)
    parser.add_argument("--json", action="store_true", help="bilan sur stdout")
    parser.add_argument(
        "--report", action="store_true",
        help="affiche le rapport d'une série déjà exécutée, sans rien relancer",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="reprend après la dernière itération archivée, sans l'écraser",
    )
    args = parser.parse_args(argv)

    if args.report:
        print(build_report(Path(args.iterations_dir)))
        return 0

    loaded = load_env()

    if args.resume:
        following = resume_point(Path(args.config), Path(args.iterations_dir))
        if following is None:
            print("[loop] rien à reprendre : le compteur est déjà à jour",
                  file=sys.stderr)
        else:
            print(f"[loop] reprise à l'itération {following}", file=sys.stderr)
    if loaded:
        print(f"[loop] .env chargé ({len(loaded)} variables)", file=sys.stderr)

    signal.signal(signal.SIGINT, _handle_signal)

    summary = run_loop(
        config_path=Path(args.config),
        cfd_settings_path=Path(args.cfd_settings),
        iterations_root=Path(args.iterations_dir),
        max_iterations=args.max_iterations,
        strategy=args.strategy,
        geometry_backend=args.geometry_backend,
        skip_cfd=args.skip_cfd,
        max_consecutive_failures=args.max_consecutive_failures,
        stagnation_patience=args.stagnation_patience,
        cfd_timeout_s=args.cfd_timeout,
    )

    print_summary(summary)
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))

    return 0 if summary["successes"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
