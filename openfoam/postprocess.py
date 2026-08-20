"""Extraction des coefficients aérodynamiques vers `results.json`.

    python3 openfoam/postprocess.py --iteration-dir data/iterations/iter_0000
    python3 openfoam/postprocess.py --iteration-dir ... \
        --failure-status MESH_CHECK_FAILED --failure-message "..."

`results.json` est le SEUL canal de retour de la CFD vers le master pipeline et
vers l'agent. Il est donc écrit dans tous les cas, succès comme échec — un run
qui s'arrête sans laisser de results.json rend l'itération illisible en aval.

Schéma (Master Doc §3.3), toujours présent :

    {
      "iteration": 5, "success": true,
      "Cd": 0.043, "Cl": 1.18, "Cl_Cd": 27.4,
      "mesh_ok": true, "error_message": null
    }

Les champs supplémentaires (statut, convergence, dispersion, fenêtre de
moyenne) servent au diagnostic et permettent à l'agent de distinguer un
résultat solide d'un résultat encore instable.

Deux principes tenus ici :

- **Aucune valeur inventée.** En cas d'échec, Cd/Cl/Cl_Cd valent `null`, jamais
  0.0 : un zéro se propagerait dans la boucle d'optimisation comme une mesure
  légitime.
- **Lecture pilotée par l'en-tête.** Les colonnes sont retrouvées par leur nom
  dans l'en-tête du fichier, pas par leur position, qui varie selon les
  versions d'OpenFOAM.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

RESULTS_FILENAME = "results.json"
CASE_DIR_NAME = "cfd"

STATUS_OK = "OK"
STATUS_NO_COEFFICIENTS = "NO_COEFFICIENTS"
STATUS_COEFFICIENTS_UNREADABLE = "COEFFICIENTS_UNREADABLE"
STATUS_NOT_FINITE = "COEFFICIENTS_NOT_FINITE"

# Noms de colonnes possibles selon les versions et les conventions.
CD_KEYS = ("Cd", "Cd_total", "cd")
CL_KEYS = ("Cl", "Cl_total", "cl")


class PostProcessError(Exception):
    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ─────────────────────────────────────────────────────────────────────────────
# Lecture des coefficients
# ─────────────────────────────────────────────────────────────────────────────


def find_coefficient_file(case_dir: Path) -> Path:
    """Localise le fichier de coefficients le plus récent.

    Les chemins et les noms ont changé au fil des versions
    (`coefficient.dat`, `forceCoeffs.dat`), et un redémarrage crée un
    sous-dossier de temps supplémentaire : on prend le plus récent.
    """
    root = case_dir / "postProcessing"
    if not root.is_dir():
        raise PostProcessError(
            STATUS_NO_COEFFICIENTS,
            f"aucun dossier postProcessing dans {case_dir} — le functionObject "
            f"forceCoeffs n'a rien écrit ; le solveur a-t-il tourné ?",
        )

    candidates = [
        p for p in root.rglob("*.dat")
        if p.name in ("coefficient.dat", "forceCoeffs.dat")
        or p.name.startswith("coefficient")
    ]
    if not candidates:
        raise PostProcessError(
            STATUS_NO_COEFFICIENTS,
            f"aucun fichier de coefficients sous {root}",
        )
    return max(candidates, key=lambda p: (p.stat().st_mtime, len(str(p))))


def parse_coefficient_file(path: Path) -> dict[str, list[float]]:
    """Lit un fichier de coefficients OpenFOAM en colonnes nommées."""
    header: list[str] | None = None
    rows: list[list[float]] = []

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            # La dernière ligne de commentaire contenant "Time" porte les noms
            # de colonnes.
            candidate = line.lstrip("#").strip()
            tokens = candidate.split()
            if tokens and tokens[0] in ("Time", "time"):
                header = tokens
            continue
        parts = line.split()
        try:
            rows.append([float(v) for v in parts])
        except ValueError:
            continue

    if not rows:
        raise PostProcessError(
            STATUS_COEFFICIENTS_UNREADABLE, f"aucune donnée numérique dans {path}"
        )
    if header is None:
        raise PostProcessError(
            STATUS_COEFFICIENTS_UNREADABLE,
            f"en-tête de colonnes introuvable dans {path} — impossible de savoir "
            f"quelle colonne est Cd et laquelle est Cl",
        )

    width = min(len(header), min(len(r) for r in rows))
    return {header[i]: [r[i] for r in rows] for i in range(width)}


def _pick(columns: Mapping[str, Sequence[float]], keys: Sequence[str], path: Path):
    for key in keys:
        if key in columns:
            return list(columns[key])
    raise PostProcessError(
        STATUS_COEFFICIENTS_UNREADABLE,
        f"aucune colonne parmi {list(keys)} dans {path} — colonnes présentes : "
        f"{list(columns)}",
    )


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def average_coefficients(
    columns: Mapping[str, Sequence[float]],
    path: Path,
    window: int,
    stability_tol: float,
) -> dict[str, Any]:
    """Moyenne Cd et Cl sur la fin du calcul et juge la stabilité.

    Une valeur instantanée à la dernière itération n'a pas de sens en RANS
    stationnaire : elle oscille. On moyenne sur une fenêtre, et on mesure la
    dispersion pour dire si le résultat est exploitable — c'est ce qui permet
    à l'agent de ne pas prendre une décision sur du bruit.
    """
    cd_all = _pick(columns, CD_KEYS, path)
    cl_all = _pick(columns, CL_KEYS, path)
    times = list(columns.get("Time") or columns.get("time") or range(len(cd_all)))

    n = min(len(cd_all), len(cl_all))
    if n == 0:
        raise PostProcessError(STATUS_COEFFICIENTS_UNREADABLE, f"{path} : 0 échantillon")
    window = max(1, min(int(window), n))

    cd_window = cd_all[-window:]
    cl_window = cl_all[-window:]
    cd, cl = _mean(cd_window), _mean(cl_window)

    if not (math.isfinite(cd) and math.isfinite(cl)):
        raise PostProcessError(
            STATUS_NOT_FINITE,
            f"coefficients non finis (Cd={cd}, Cl={cl}) — le calcul a diverge",
        )

    cd_std, cl_std = _stdev(cd_window), _stdev(cl_window)
    # Dispersion relative : sur un Cd proche de zéro, un écart-type absolu ne
    # veut rien dire.
    cd_rel = cd_std / abs(cd) if abs(cd) > 1e-12 else float("inf")
    cl_rel = cl_std / abs(cl) if abs(cl) > 1e-12 else float("inf")
    stable = cd_rel <= stability_tol and cl_rel <= stability_tol

    cl_cd = cl / cd if abs(cd) > 1e-12 else None

    return {
        "Cd": cd,
        "Cl": cl,
        "Cl_Cd": cl_cd,
        "Cd_std": cd_std,
        "Cl_std": cl_std,
        "Cd_rel_std": cd_rel if math.isfinite(cd_rel) else None,
        "Cl_rel_std": cl_rel if math.isfinite(cl_rel) else None,
        "coefficients_stable": stable,
        "averaging_window": window,
        "samples": n,
        "last_time": float(times[-1]) if times else None,
        "source": str(path),
    }


# ─────────────────────────────────────────────────────────────────────────────
# checkMesh et convergence
# ─────────────────────────────────────────────────────────────────────────────

_FAILED_CHECKS_RE = re.compile(r"Failed\s+(\d+)\s+mesh\s+checks", re.IGNORECASE)
_CELLS_RE = re.compile(r"^\s*cells:\s+(\d+)", re.MULTILINE)
_NON_ORTHO_RE = re.compile(r"non-orthogonality Max:\s*([0-9.eE+-]+)", re.IGNORECASE)
_SKEWNESS_RE = re.compile(r"Max skewness\s*=\s*([0-9.eE+-]+)", re.IGNORECASE)
_ASPECT_RE = re.compile(r"Max aspect ratio\s*=\s*([0-9.eE+-]+)", re.IGNORECASE)

# Seuils par défaut, alignés sur cfd_settings.yaml (mesh.check_mesh).
DEFAULT_MESH_LIMITS = {
    "max_non_orthogonality": 70.0,
    "max_skewness": 4.0,
    "max_aspect_ratio": 1000.0,
}

_SKEW_FACES_RE = re.compile(
    r"([0-9]+)\s+highly\s+skew\s+faces?\s+detected", re.IGNORECASE
)
_FACES_RE = re.compile(r"^\s*faces:\s+(\d+)", re.MULTILINE)
_FAILED_LINE_RE = re.compile(r"^\s*\*\*\*(.*)$", re.MULTILINE)

# Un défaut de skewness peut être TOLÉRÉ s'il est à la fois local et modéré.
#
# Le cas qui a imposé cette nuance : un Clark Y, dont le bord de fuite est
# épaissi de 0,0012 corde. Aucun des deux préréglages ne place assez de
# cellules en travers d'un intervalle aussi mince, et snappyHexMesh y écrase
# quelques cellules — TROIS faces sur 258 814, soit un millième de pour cent.
# Refuser le maillage entier pour cela arrêtait la boucle dès l'itération zéro,
# alors que le solveur converge sans peine et que l'écriture d'OpenFOAM
# elle-même est prudente : ces faces « PEUVENT dégrader la qualité ».
#
# Les deux conditions comptent. Un maillage dont un dixième des faces est
# gauchi est franchement mauvais, même modérément ; un maillage dont trois
# faces sont à 30 de skewness a une cellule retournée quelque part. On exige
# donc que le défaut soit à la fois rare ET contenu.
SKEW_TOLERATED_FRACTION = 1e-4   # un dix-millième des faces
SKEW_TOLERATED_COUNT = 20        # et jamais plus de vingt faces
SKEW_HARD_CEILING = 10.0         # au delà, la cellule est dégénérée


def _search_float(pattern: re.Pattern[str], text: str) -> float | None:
    match = pattern.search(text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def read_check_mesh(
    log_path: Path, limits: Mapping[str, float] | None = None
) -> dict[str, Any]:
    """Verdict de checkMesh : contrôles OpenFOAM ET seuils du projet.

    Deux niveaux volontairement distincts :

    - les contrôles que checkMesh lui-même déclare en échec (cellules à volume
      négatif, faces ouvertes...) : rédhibitoires ;
    - les grandeurs de qualité (non-orthogonalité, skewness, aspect ratio)
      comparées aux seuils de `cfd_settings.yaml`. Un maillage peut être
      « Mesh OK » pour OpenFOAM tout en étant trop dégradé pour qu'on fasse
      confiance aux coefficients.

    Le script d'orchestration lance `checkMesh` sans `-allGeometry`, dont le
    contrôle de cellules concaves fait échouer à peu près tout maillage
    snappyHexMesh avec couches limites, sans que le solveur en souffre.
    """
    thresholds = {**DEFAULT_MESH_LIMITS, **(limits or {})}

    if not log_path.is_file():
        return {
            "mesh_ok": False,
            "mesh_checked": False,
            "mesh_message": f"checkMesh non exécuté (journal absent : {log_path.name})",
            "n_cells": None,
            "max_non_orthogonality": None,
            "max_skewness": None,
            "max_aspect_ratio": None,
            "mesh_problems": ["checkMesh non exécuté"],
        }

    text = log_path.read_text(encoding="utf-8", errors="replace")
    cells_match = _CELLS_RE.search(text)
    faces_match = _FACES_RE.search(text)
    skew_faces = _SKEW_FACES_RE.search(text)
    info: dict[str, Any] = {
        "mesh_checked": True,
        "n_cells": int(cells_match.group(1)) if cells_match else None,
        "n_faces": int(faces_match.group(1)) if faces_match else None,
        "n_skew_faces": int(skew_faces.group(1)) if skew_faces else None,
        "max_non_orthogonality": _search_float(_NON_ORTHO_RE, text),
        "max_skewness": _search_float(_SKEWNESS_RE, text),
        "max_aspect_ratio": _search_float(_ASPECT_RE, text),
    }

    tolerated = _skewness_is_local(info, thresholds["max_skewness"])
    info["skewness_tolerated"] = tolerated

    problems: list[str] = []
    warnings: list[str] = []

    failed = _FAILED_CHECKS_RE.search(text)
    if failed:
        # Quand le SEUL contrôle en échec est celui de la skewness et que le
        # défaut est négligeable, on ne rejette pas le maillage — mais on le
        # dit. Passer sous silence un contrôle en échec serait exactement le
        # genre de silence que le reste du système s'interdit.
        if tolerated and _only_skewness_failed(text):
            warnings.append(
                f"{info['n_skew_faces']} face(s) gauchie(s) sur "
                f"{info['n_faces']} (skewness {info['max_skewness']:g}) — "
                f"défaut local, toléré"
            )
        else:
            problems.append(
                f"checkMesh signale {failed.group(1)} contrôle(s) en échec"
            )
    elif "Mesh OK" not in text:
        problems.append("verdict de checkMesh illisible dans le journal")

    for key, label in (
        ("max_non_orthogonality", "non-orthogonalité"),
        ("max_skewness", "skewness"),
        ("max_aspect_ratio", "aspect ratio"),
    ):
        value = info[key]
        limit = thresholds[key]
        if value is None or value <= limit:
            continue
        if key == "max_skewness" and tolerated:
            continue
        problems.append(f"{label} {value:g} au dessus du seuil {limit:g}")

    info["mesh_ok"] = not problems
    info["mesh_problems"] = problems
    info["mesh_warnings"] = warnings

    if problems:
        info["mesh_message"] = "checkMesh : " + " | ".join(problems)
    else:
        measures = ", ".join(
            f"{label} {info[key]:g}"
            for key, label in (
                ("max_non_orthogonality", "non-ortho"),
                ("max_skewness", "skewness"),
                ("max_aspect_ratio", "aspect ratio"),
            )
            if info[key] is not None
        )
        info["mesh_message"] = (
            f"checkMesh : maillage valide ({measures})"
            + (f" — {warnings[0]}" if warnings else "")
        )
    return info


def _skewness_is_local(info: Mapping[str, Any], limit: float) -> bool:
    """Le défaut de skewness est-il assez rare ET assez contenu pour passer ?"""
    value = info.get("max_skewness")
    count = info.get("n_skew_faces")
    total = info.get("n_faces")
    if value is None or value <= limit:
        return False  # rien à tolérer
    if count is None or total is None or total <= 0:
        return False  # sans l'étendue, on ne peut pas juger : on refuse
    if value > SKEW_HARD_CEILING:
        return False
    return count <= SKEW_TOLERATED_COUNT and count / total <= SKEW_TOLERATED_FRACTION


def _only_skewness_failed(text: str) -> bool:
    """Les lignes `***` de checkMesh ne concernent-elles QUE la skewness ?

    checkMesh préfixe de trois astérisques chaque contrôle en échec. Si une
    cellule à volume négatif ou une face ouverte s'y trouve aussi, le maillage
    est cassé pour de bon et la tolérance sur la skewness n'a pas à s'appliquer.
    """
    lignes = [m.group(1).strip() for m in _FAILED_LINE_RE.finditer(text)]
    if not lignes:
        return False
    return all("skew" in ligne.lower() for ligne in lignes)


def read_solver_convergence(log_dir: Path, solver: str) -> dict[str, Any]:
    """Cherche dans le journal du solveur s'il s'est arrêté sur convergence."""
    log_path = log_dir / f"{solver}.log"
    if not log_path.is_file():
        return {"solver_converged": None, "solver_iterations": None}

    text = log_path.read_text(encoding="utf-8", errors="replace")
    converged = "SIMPLE solution converged" in text or "solution converged" in text
    times = re.findall(r"^Time = (\d+(?:\.\d+)?)", text, re.MULTILINE)
    return {
        "solver_converged": converged,
        "solver_iterations": int(float(times[-1])) if times else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Écriture de results.json
# ─────────────────────────────────────────────────────────────────────────────


def _base_result(iteration: Any, design_id: Any) -> dict[str, Any]:
    """Squelette commun : les clés du contrat existent TOUJOURS."""
    return {
        "iteration": iteration,
        "design_id": design_id,
        "success": False,
        "status": None,
        "Cd": None,
        "Cl": None,
        "Cl_Cd": None,
        "mesh_ok": False,
        "error_message": None,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_results(iteration_dir: Path, result: Mapping[str, Any]) -> Path:
    target = Path(iteration_dir) / RESULTS_FILENAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )
    return target


def mesh_limits_from_settings(cfd_settings_path: Path | None) -> dict[str, float]:
    """Seuils de qualité de maillage lus dans cfd_settings.yaml."""
    if cfd_settings_path is None:
        return dict(DEFAULT_MESH_LIMITS)
    try:
        from pipeline.utils import load_yaml

        check = ((load_yaml(cfd_settings_path).get("mesh") or {}).get("check_mesh") or {})
        return {
            key: float(check.get(key, default))
            for key, default in DEFAULT_MESH_LIMITS.items()
        }
    except Exception:
        return dict(DEFAULT_MESH_LIMITS)


def _read_design(design_params_path: Path | None) -> tuple[Any, Any]:
    if design_params_path is None:
        return None, None
    try:
        from pipeline.utils import load_yaml

        data = load_yaml(design_params_path)
        return data.get("iteration"), data.get("design_id")
    except Exception:
        return None, None


def build_failure(
    iteration_dir: Path,
    status: str,
    message: str,
    design_params_path: Path | None = None,
) -> dict[str, Any]:
    """results.json d'échec : coefficients à null, cause explicite."""
    iteration, design_id = _read_design(design_params_path)
    result = _base_result(iteration, design_id)
    mesh = read_check_mesh(Path(iteration_dir) / "logs" / "checkMesh.log")
    result.update(
        {
            "success": False,
            "status": status,
            "error_message": message,
            "mesh_ok": mesh["mesh_ok"],
            "mesh": mesh,
        }
    )
    return result


def postprocess(
    iteration_dir: Path,
    design_params_path: Path | None = None,
    cfd_settings_path: Path | None = None,
) -> dict[str, Any]:
    """Assemble results.json à partir d'un case calculé."""
    iteration_dir = Path(iteration_dir)
    case_dir = iteration_dir / CASE_DIR_NAME
    log_dir = iteration_dir / "logs"

    window, tol, solver = 200, 0.02, "simpleFoam"
    if cfd_settings_path is not None:
        try:
            from pipeline.utils import load_yaml

            cfd = load_yaml(cfd_settings_path)
            convergence = cfd.get("convergence", {}) or {}
            window = int(convergence.get("averaging_window", window))
            tol = float(convergence.get("coeff_stability_tol", tol))
            solver = str((cfd.get("case", {}) or {}).get("solver", solver))
        except Exception:
            pass  # valeurs par défaut : le post-traitement ne doit pas échouer ici

    iteration, design_id = _read_design(design_params_path)
    result = _base_result(iteration, design_id)

    mesh = read_check_mesh(
        log_dir / "checkMesh.log", mesh_limits_from_settings(cfd_settings_path)
    )
    result["mesh_ok"] = mesh["mesh_ok"]
    result["mesh"] = mesh

    path = find_coefficient_file(case_dir)
    columns = parse_coefficient_file(path)
    coefficients = average_coefficients(columns, path, window, tol)

    result.update(coefficients)
    result.update(read_solver_convergence(log_dir, solver))
    result["success"] = True
    result["status"] = STATUS_OK
    # « converged » résume les deux conditions qui rendent un point exploitable :
    # un maillage valide et des coefficients stabilisés.
    result["converged"] = bool(
        coefficients["coefficients_stable"] and result["mesh_ok"]
    )
    if not result["converged"]:
        result["warning"] = (
            "résultat exploitable mais peu sûr : "
            + ("coefficients encore instables" if not coefficients["coefficients_stable"]
               else "")
            + ("" if result["mesh_ok"] else " maillage non validé")
        ).strip()
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openfoam/postprocess.py",
        description="Extrait Cd / Cl / Cl_Cd d'un case OpenFOAM vers results.json.",
    )
    parser.add_argument("--iteration-dir", required=True)
    parser.add_argument("--design-params", default=None)
    parser.add_argument("--cfd-settings", default=None)
    parser.add_argument(
        "--failure-status", default=None,
        help="écrit un results.json d'échec avec ce statut au lieu de dépouiller",
    )
    parser.add_argument("--failure-message", default="")
    parser.add_argument(
        "--evaluate-mesh", action="store_true",
        help="juge le journal checkMesh contre les seuils de cfd_settings.yaml "
             "et sort en 1 si le maillage est inexploitable",
    )
    args = parser.parse_args(argv)

    iteration_dir = Path(args.iteration_dir)
    design_params = Path(args.design_params) if args.design_params else None
    cfd_settings = Path(args.cfd_settings) if args.cfd_settings else None

    if args.evaluate_mesh:
        mesh = read_check_mesh(
            iteration_dir / "logs" / "checkMesh.log",
            mesh_limits_from_settings(cfd_settings),
        )
        print(json.dumps(mesh, indent=2, ensure_ascii=False, default=str))
        return 0 if mesh["mesh_ok"] else 1

    if args.failure_status:
        result = build_failure(
            iteration_dir, args.failure_status, args.failure_message, design_params
        )
        write_results(iteration_dir, result)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        return 0  # l'échec est rapporté DANS le fichier, l'écriture a réussi

    try:
        result = postprocess(iteration_dir, design_params, cfd_settings)
    except PostProcessError as exc:
        result = build_failure(iteration_dir, exc.status, exc.message, design_params)
        write_results(iteration_dir, result)
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str),
              file=sys.stderr)
        return 1

    write_results(iteration_dir, result)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
