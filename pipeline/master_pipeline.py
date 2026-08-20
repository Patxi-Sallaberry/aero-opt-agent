"""Point d'entrée unique du pipeline (Master Doc §3.4).

    python3 pipeline/master_pipeline.py
    python3 pipeline/master_pipeline.py --config configs/design_params.yaml
    python3 pipeline/master_pipeline.py --skip-cfd     # géométrie seule

Enchaîne, pour UNE itération :

    1. valider design_params.yaml (Phase 0)
    2. produire la géométrie          (Phase 1 — Fusion ou producteur interne)
    3. valider la géométrie           (geometry_validator)
    4. lancer OpenFOAM                (Phase 2 — run_cfd.sh)
    5. relire results.json
    6. archiver l'itération
    7. retourner le résultat

`run_iteration()` ne lève jamais : chaque échec devient un `results.json`
exploitable, archivé au même titre qu'un succès (§4.5). Une itération ratée est
une information — elle dit à l'agent quelle direction ne pas reprendre — et la
perdre reviendrait à la refaire.

L'archivage conserve, dans `data/iterations/iter_XXXX/` :

    design_params.yaml    la configuration EXACTE ayant produit ce résultat
    geometry.stl/.step    la géométrie
    results.json          les coefficients, ou la cause de l'échec
    fusion_status.json    le compte rendu du driver
    logs/                 les journaux de chaque étape
    iteration.json        le compte rendu du pipeline lui-même
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fusion import parametric_driver as driver  # noqa: E402
from geometry import (  # noqa: E402
    GeometryResult,
    NoBackendAvailable,
    UnknownBackend,
    configuration_choices,
    get_backend,
)
from pipeline.geometry_validator import GeometryError, validate_geometry  # noqa: E402
from pipeline.utils import (  # noqa: E402
    ConfigValidationError,
    load_env,
    load_yaml,
    validate_design_params,
)

DEFAULT_CONFIG = REPO_ROOT / "configs" / "design_params.yaml"
DEFAULT_CFD_SETTINGS = REPO_ROOT / "configs" / "cfd_settings.yaml"
DEFAULT_ITERATIONS = REPO_ROOT / "data" / "iterations"
RUN_CFD = REPO_ROOT / "openfoam" / "run_cfd.sh"

RESULTS_FILENAME = "results.json"
ITERATION_FILENAME = "iteration.json"
ARCHIVED_CONFIG = "design_params.yaml"

STATUS_OK = "OK"
STATUS_CONFIG_ERROR = "CONFIG_ERROR"
STATUS_GEOMETRY_FAILED = "GEOMETRY_FAILED"
STATUS_CFD_FAILED = "CFD_FAILED"
STATUS_PIPELINE_ERROR = "PIPELINE_ERROR"
STATUS_SKIPPED_CFD = "SKIPPED_CFD"


# ─────────────────────────────────────────────────────────────────────────────
# Historique des itérations
# ─────────────────────────────────────────────────────────────────────────────


def iteration_dirs(iterations_root: Path) -> list[Path]:
    """Dossiers d'itération existants, dans l'ordre."""
    root = Path(iterations_root)
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name.startswith("iter_")),
        key=lambda p: p.name,
    )


def read_iteration(directory: Path) -> dict | None:
    """Relit le compte rendu d'une itération archivée."""
    path = Path(directory) / ITERATION_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def history(iterations_root: Path) -> list[dict]:
    """Toutes les itérations archivées, échecs compris."""
    return [
        record
        for record in (read_iteration(d) for d in iteration_dirs(iterations_root))
        if record is not None
    ]


def last_successful(iterations_root: Path) -> dict | None:
    """Dernière itération RÉUSSIE — la référence pour la règle max_delta_pct."""
    for record in reversed(history(iterations_root)):
        if record.get("success"):
            return record
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Étapes
# ─────────────────────────────────────────────────────────────────────────────


def _archive_config(config_path: Path, out_dir: Path) -> Path:
    """Fige la configuration ayant produit l'itération.

    Sans cette copie, un résultat archivé ne serait pas rattachable à des
    valeurs précises : le fichier de travail, lui, aura déjà été réécrit par
    l'agent à l'itération suivante.
    """
    target = Path(out_dir) / ARCHIVED_CONFIG
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(config_path, target)
    return target


def produce_geometry(
    config_path: Path,
    out_dir: Path,
    iterations_root: Path,
    geometry_backend: str | None = None,
) -> GeometryResult:
    """Fait produire la géométrie de l'itération par le backend configuré.

    Un backend indisponible ou inconnu ne remonte pas d'exception : c'est un
    échec d'itération comme un autre, que la boucle archive et dont la
    stratégie tire les conséquences.
    """
    try:
        backend = get_backend(geometry_backend)
    except (UnknownBackend, NoBackendAvailable) as exc:
        return GeometryResult(
            success=False,
            status=STATUS_GEOMETRY_FAILED,
            message=str(exc),
            backend=str(geometry_backend or "auto"),
        )
    return backend.generate(config_path, out_dir)


def _previous_geometry(iterations_root: Path, iteration: int) -> tuple[str | None, dict | None]:
    """Empreinte et paramètres de l'itération précédente."""
    for directory in reversed(iteration_dirs(iterations_root)):
        if directory.name >= f"iter_{iteration:04d}":
            continue
        record = read_iteration(directory)
        if not record:
            continue
        geometry = record.get("geometry_report") or {}
        config = directory / ARCHIVED_CONFIG
        parameters = None
        if config.is_file():
            try:
                parameters = load_yaml(config).get("parameters")
            except ConfigValidationError:
                parameters = None
        return geometry.get("fingerprint"), parameters
    return None, None


def run_cfd(
    iteration_dir: Path,
    config_path: Path,
    cfd_settings_path: Path,
    timeout_s: int | None = None,
) -> tuple[bool, str]:
    """Lance `openfoam/run_cfd.sh`. Retourne (succès, message)."""
    if not RUN_CFD.is_file():
        return False, f"script CFD introuvable : {RUN_CFD}"

    cmd = [
        "bash", str(RUN_CFD),
        "--iteration-dir", str(iteration_dir),
        "--design-params", str(config_path),
        "--cfd-settings", str(cfd_settings_path),
    ]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout_s, cwd=str(REPO_ROOT)
        )
    except subprocess.TimeoutExpired:
        return False, f"la CFD a dépassé le délai imparti ({timeout_s} s)"
    except OSError as exc:
        return False, f"lancement de run_cfd.sh impossible : {exc}"

    if proc.returncode == 0:
        return True, "CFD terminée"
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    return False, " ".join(tail[-6:])[:800] or f"run_cfd.sh a échoué (code {proc.returncode})"


def prune_case(iteration_dir: Path, cfd_settings: Mapping[str, Any]) -> dict:
    """Allège le case OpenFOAM archivé selon `execution.keep_case_after_run`.

    Un case complet pèse une vingtaine de méga-octets, presque entièrement du
    maillage et des champs. Sur une optimisation de cinquante itérations qui
    tourne sans surveillance, cela fait plus d'un gigaoctet — et le maillage se
    régénère en quelques secondes à partir du STL et des dictionnaires, qui,
    eux, tiennent en quelques kilo-octets.

    Trois politiques :
      `true`    tout est conservé (défaut : ne rien jeter sans qu'on l'ait dit) ;
      `"dicts"` maillage et champs supprimés, dictionnaires, journaux et
                coefficients conservés — le case reste rejouable ;
      `false`   le case entier disparaît, seuls results.json et les journaux
                restent.
    """
    case_dir = Path(iteration_dir) / "cfd"
    policy = (cfd_settings.get("execution") or {}).get("keep_case_after_run", True)
    if isinstance(policy, str):
        policy = policy.strip().lower()

    report = {"policy": policy, "removed": [], "freed_bytes": 0}
    if not case_dir.is_dir() or policy is True or policy == "true":
        return report

    def _size(path: Path) -> int:
        try:
            return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())
        except OSError:
            return 0

    targets: list[Path] = []
    if policy in (False, "false", "0"):
        targets = [case_dir]
    elif policy == "dicts":
        targets = [case_dir / "constant" / "polyMesh"]
        targets += list(case_dir.glob("processor*"))
        for entry in case_dir.iterdir():
            # Les répertoires temporels sont numériques ; « 0 » porte les
            # conditions initiales et doit survivre pour rejouer le case.
            if entry.is_dir() and entry.name != "0":
                try:
                    float(entry.name)
                except ValueError:
                    continue
                targets.append(entry)
    else:
        return report

    for target in targets:
        if not target.exists():
            continue
        freed = _size(target)
        try:
            shutil.rmtree(target)
            report["removed"].append(str(target.relative_to(iteration_dir)))
            report["freed_bytes"] += freed
        except OSError:
            continue
    return report


def read_results(iteration_dir: Path) -> dict | None:
    path = Path(iteration_dir) / RESULTS_FILENAME
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Itération complète
# ─────────────────────────────────────────────────────────────────────────────


def _record(
    iteration: Any,
    design_id: Any,
    success: bool,
    status: str,
    stage: str,
    **extra: Any,
) -> dict:
    record = {
        "iteration": iteration,
        "design_id": design_id,
        "success": success,
        "status": status,
        "stage": stage,
        "Cd": None,
        "Cl": None,
        "Cl_Cd": None,
        "mesh_ok": False,
        "error_message": None,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    record.update(extra)
    return record


def _write_record(out_dir: Path | None, record: Mapping[str, Any]) -> None:
    if out_dir is None:
        return
    try:
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (Path(out_dir) / ITERATION_FILENAME).write_text(
            json.dumps(record, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass


def run_iteration(
    config_path: str | Path = DEFAULT_CONFIG,
    cfd_settings_path: str | Path = DEFAULT_CFD_SETTINGS,
    iterations_root: str | Path = DEFAULT_ITERATIONS,
    skip_cfd: bool = False,
    geometry_backend: str | None = None,
    cfd_timeout_s: int | None = None,
) -> dict:
    """Exécute une itération complète et retourne son compte rendu.

    Ne lève jamais : tout échec devient un compte rendu archivé, avec la cause
    et l'étape où elle est survenue.
    """
    started = time.time()
    config_path = Path(config_path)
    cfd_settings_path = Path(cfd_settings_path)
    iterations_root = Path(iterations_root)

    iteration: Any = None
    design_id: Any = None
    out_dir: Path | None = None

    try:
        # ── 1. Configuration ─────────────────────────────────────────────
        design = load_yaml(config_path)
        report = validate_design_params(design, path=config_path)
        if not report.ok:
            return _record(
                design.get("iteration"), design.get("design_id"), False,
                STATUS_CONFIG_ERROR, "config",
                error_message="design_params invalide : " + " | ".join(report.errors),
                error_details=report.errors,
            )

        iteration = design["iteration"]
        design_id = design.get("design_id")
        out_dir = iterations_root / f"iter_{int(iteration):04d}"
        out_dir.mkdir(parents=True, exist_ok=True)
        _archive_config(config_path, out_dir)

        # ── 2. Géométrie ─────────────────────────────────────────────────
        # Le pipeline ne connaît que l'interface : quel producteur travaille
        # derrière — calcul interne, Fusion, ou un backend à venir — est un
        # choix de configuration.
        geometry_result = produce_geometry(
            config_path, out_dir, iterations_root, geometry_backend
        )
        geometry_status = geometry_result.raw or {
            "success": geometry_result.success,
            "status": geometry_result.status,
            "error_message": geometry_result.message,
        }
        if not geometry_result.success:
            record = _record(
                iteration, design_id, False, STATUS_GEOMETRY_FAILED, "geometry",
                error_message=geometry_result.message,
                error_details=geometry_result.status,
                geometry_status=geometry_status,
                duration_s=round(time.time() - started, 1),
            )
            _write_record(out_dir, record)
            return record

        # ── 3. Validation de la géométrie ────────────────────────────────
        previous_fp, previous_params = _previous_geometry(iterations_root, int(iteration))
        try:
            geometry_report = validate_geometry(
                out_dir, config_path,
                previous_fingerprint=previous_fp,
                previous_parameters=previous_params,
            )
        except GeometryError as exc:
            record = _record(
                iteration, design_id, False, STATUS_GEOMETRY_FAILED, "geometry_check",
                error_message=exc.message,
                error_details=exc.status,
                geometry_status=geometry_status,
                duration_s=round(time.time() - started, 1),
            )
            _write_record(out_dir, record)
            return record

        if skip_cfd:
            record = _record(
                iteration, design_id, False, STATUS_SKIPPED_CFD, "cfd",
                error_message="CFD volontairement ignorée (--skip-cfd)",
                geometry_status=geometry_status,
                geometry_report=geometry_report,
                duration_s=round(time.time() - started, 1),
            )
            _write_record(out_dir, record)
            return record

        # ── 4-5. CFD et coefficients ─────────────────────────────────────
        cfd_ok, cfd_message = run_cfd(
            out_dir, config_path, cfd_settings_path, cfd_timeout_s
        )
        results = read_results(out_dir)

        try:
            pruned = prune_case(out_dir, load_yaml(cfd_settings_path))
        except Exception:
            pruned = {"policy": "erreur", "removed": [], "freed_bytes": 0}

        if results is None:
            record = _record(
                iteration, design_id, False, STATUS_CFD_FAILED, "cfd",
                error_message=f"aucun results.json produit : {cfd_message}",
                geometry_status=geometry_status,
                geometry_report=geometry_report,
                duration_s=round(time.time() - started, 1),
            )
            _write_record(out_dir, record)
            return record

        success = bool(cfd_ok and results.get("success"))
        record = _record(
            iteration, design_id, success,
            STATUS_OK if success else STATUS_CFD_FAILED,
            "done" if success else "cfd",
            Cd=results.get("Cd"),
            Cl=results.get("Cl"),
            Cl_Cd=results.get("Cl_Cd"),
            mesh_ok=bool(results.get("mesh_ok")),
            converged=results.get("converged"),
            error_message=None if success else (
                results.get("error_message") or cfd_message
            ),
            error_details=results.get("status"),
            objective=objective_value(design, results) if success else None,
            geometry_status=geometry_status,
            geometry_report=geometry_report,
            results=results,
            pruned=pruned,
            duration_s=round(time.time() - started, 1),
        )
        _write_record(out_dir, record)
        return record

    except ConfigValidationError as exc:
        return _record(
            iteration, design_id, False, STATUS_CONFIG_ERROR, "config",
            error_message=str(exc),
        )
    except Exception as exc:  # garde-fou : le pipeline ne remonte jamais brut
        import traceback

        record = _record(
            iteration, design_id, False, STATUS_PIPELINE_ERROR, "pipeline",
            error_message=f"{type(exc).__name__}: {exc}",
            error_details=traceback.format_exc(),
            duration_s=round(time.time() - started, 1),
        )
        _write_record(out_dir, record)
        return record


# ─────────────────────────────────────────────────────────────────────────────
# Objectif
# ─────────────────────────────────────────────────────────────────────────────


def objective_value(
    design: Mapping[str, Any], results: Mapping[str, Any]
) -> float | None:
    """Valeur à MAXIMISER, quel que soit l'objectif déclaré.

    Ramener tous les objectifs à « plus c'est grand, mieux c'est » évite à
    l'agent — et à la stratégie de repli — d'avoir à se souvenir du sens de
    l'optimisation à chaque comparaison.
    """
    objective = str((design.get("objectives") or {}).get("primary", ""))
    cd, cl = results.get("Cd"), results.get("Cl")
    cl_cd = results.get("Cl_Cd")

    if objective == "maximize_Cl_Cd":
        return float(cl_cd) if isinstance(cl_cd, (int, float)) else None
    if objective == "minimize_Cd":
        return -float(cd) if isinstance(cd, (int, float)) else None
    if objective == "maximize_downforce":
        # Une aile d'appui produit une portance négative : c'est son opposé
        # qu'on cherche à maximiser.
        return -float(cl) if isinstance(cl, (int, float)) else None
    return None


def objective_label(design: Mapping[str, Any]) -> str:
    return str((design.get("objectives") or {}).get("primary", "inconnu"))


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline/master_pipeline.py",
        description="Exécute une itération complète : géométrie, CFD, archivage.",
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--cfd-settings", default=str(DEFAULT_CFD_SETTINGS))
    parser.add_argument("--iterations-dir", default=None)
    parser.add_argument(
        "--skip-cfd", action="store_true",
        help="s'arrête après la validation de la géométrie",
    )
    parser.add_argument(
        "--geometry-backend", default=None,
        choices=configuration_choices(),
        help="producteur de géométrie (défaut : auto). Les choix viennent du "
             "registre : un backend ajouté y apparaît sans modifier ce code.",
    )
    parser.add_argument("--cfd-timeout", type=int, default=None)
    parser.add_argument(
        "--quiet", action="store_true", help="n'affiche que la ligne de résumé"
    )
    args = parser.parse_args(argv)
    load_env()

    iterations_root = args.iterations_dir or os.environ.get("ITERATIONS_DIR") or str(
        DEFAULT_ITERATIONS
    )

    record = run_iteration(
        config_path=args.config,
        cfd_settings_path=args.cfd_settings,
        iterations_root=iterations_root,
        skip_cfd=args.skip_cfd,
        geometry_backend=args.geometry_backend,
        cfd_timeout_s=args.cfd_timeout,
    )

    if not args.quiet:
        print(json.dumps(record, indent=2, ensure_ascii=False, default=str))

    if record["success"]:
        print(
            f"iter {record['iteration']:>4} | Cd {record['Cd']:.5f} "
            f"| Cl {record['Cl']:.5f} | Cl/Cd {record['Cl_Cd']:.2f} "
            f"| {record['duration_s']} s",
            file=sys.stderr,
        )
        return 0

    if record["status"] == STATUS_SKIPPED_CFD:
        # Demandé explicitement : ce n'est pas un échec du système.
        print(
            f"iter {record['iteration']:>4} | géométrie validée, CFD ignorée "
            f"(--skip-cfd) | {record['duration_s']} s",
            file=sys.stderr,
        )
        return 0

    print(
        f"iter {record['iteration']} | ÉCHEC [{record['status']}/{record['stage']}] "
        f"{record['error_message']}",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
