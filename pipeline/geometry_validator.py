"""Validation de la géométrie exportée, avant de dépenser une heure de CFD.

    python3 pipeline/geometry_validator.py --iteration-dir data/iterations/iter_0000

Étape 3 du master pipeline (Master Doc §3.4, §4.2). Elle répond à une seule
question : *le fichier produit décrit-il bien la forme que design_params.yaml
demande ?*

Les défauts recherchés sont ceux qui ne se voient pas autrement. Un maillage
tourne sans broncher sur une géométrie inchangée, sur une aile exportée deux
fois, ou sur un modèle en millimètres pris pour des mètres ; la CFD rend alors
des coefficients parfaitement plausibles et parfaitement faux. C'est le pire
mode de défaillance du système : il ne lève aucune erreur et contamine
l'optimisation entière.

Contrôles :

1. **Présence et taille** du fichier exporté.
2. **Lisibilité** : facettes déchiffrables, sommets en nombre suffisant.
3. **Emprise** conforme à la corde, l'épaisseur et l'envergure demandées.
4. **Nouveauté** : la géométrie diffère de celle de l'itération précédente
   quand les paramètres, eux, ont changé.
5. **Épaisseur de paroi** minimale, contrainte du §3.1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openfoam.case_builder import (  # noqa: E402
    CaseBuildError,
    expected_bounding_box,
    stl_bounding_box,
)
from pipeline.utils import load_yaml  # noqa: E402

STATUS_OK = "OK"
STATUS_GEOMETRY_MISSING = "GEOMETRY_MISSING"
STATUS_GEOMETRY_UNREADABLE = "GEOMETRY_UNREADABLE"
STATUS_GEOMETRY_MISMATCH = "GEOMETRY_MISMATCH"
STATUS_GEOMETRY_UNCHANGED = "GEOMETRY_UNCHANGED"
STATUS_CONSTRAINT_VIOLATED = "CONSTRAINT_VIOLATED"

MIN_VERTICES = 12          # un solide fermé en compte bien davantage
MIN_FILE_BYTES = 200


class GeometryError(Exception):
    """Géométrie inexploitable ou incohérente avec la configuration."""

    def __init__(self, status: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


def geometry_path(iteration_dir: Path) -> Path:
    """Fichier de géométrie de l'itération. Le STL prime : c'est ce que la CFD
    consomme, et le STEP n'est pas lisible sans noyau CAO."""
    for name in ("geometry.stl", "geometry.step"):
        candidate = Path(iteration_dir) / name
        if candidate.is_file():
            return candidate
    raise GeometryError(
        STATUS_GEOMETRY_MISSING,
        f"aucune géométrie dans {iteration_dir} — le driver n'a rien exporté "
        f"pour cette itération",
    )


def fingerprint(path: Path) -> str:
    """Empreinte du contenu, pour détecter deux itérations identiques."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for block in iter(lambda: fh.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _check_thickness(
    bbox: Mapping[str, float], design: Mapping[str, Any], report: dict
) -> list[str]:
    """Contrôle grossier de l'épaisseur minimale (contrainte §3.1).

    On compare l'épaisseur maximale du profil — t/c × corde — au minimum exigé.
    Ce n'est pas une mesure d'épaisseur de paroi au sens CAO : le profil est
    plein, il n'a pas de paroi. C'est un garde-fou contre une forme devenue si
    fine qu'elle n'est ni maillable ni fabricable.
    """
    problems: list[str] = []
    constraints = design.get("constraints") or {}
    minimum_mm = constraints.get("min_wall_thickness_mm")
    if minimum_mm is None:
        return problems

    params = design.get("parameters") or {}
    try:
        chord_mm = float(params["chord"]["value"])
        ratio = float(params["thickness"]["value"])
    except (KeyError, TypeError, ValueError):
        return problems

    thickness_mm = ratio * chord_mm
    report["max_thickness_mm"] = thickness_mm
    if thickness_mm < float(minimum_mm):
        problems.append(
            f"épaisseur maximale du profil {thickness_mm:.2f} mm sous le minimum "
            f"exigé {float(minimum_mm):.2f} mm (constraints.min_wall_thickness_mm)"
        )
    return problems


def validate_geometry(
    iteration_dir: Path,
    design_params_path: Path,
    previous_fingerprint: str | None = None,
    previous_parameters: Mapping[str, Any] | None = None,
    tolerance_pct: float = 5.0,
) -> dict:
    """Valide la géométrie d'une itération. Lève GeometryError si inexploitable.

    Args:
        iteration_dir: dossier de l'itération.
        design_params_path: configuration ayant servi à la produire.
        previous_fingerprint: empreinte de la géométrie précédente, pour
            détecter une itération qui n'a rien changé.
        previous_parameters: paramètres précédents — sans eux, impossible de
            savoir si une géométrie identique est normale ou suspecte.
        tolerance_pct: écart toléré sur l'emprise, en pourcentage de corde.

    Returns:
        Un rapport : chemin, empreinte, emprise, avertissements.
    """
    iteration_dir = Path(iteration_dir)
    design = load_yaml(design_params_path)
    path = geometry_path(iteration_dir)

    size = path.stat().st_size
    if size < MIN_FILE_BYTES:
        raise GeometryError(
            STATUS_GEOMETRY_UNREADABLE,
            f"{path.name} ne fait que {size} octets — export tronqué",
        )

    report: dict[str, Any] = {
        "status": STATUS_OK,
        "path": str(path),
        "format": path.suffix.lstrip("."),
        "size_bytes": size,
        "fingerprint": fingerprint(path),
        "warnings": [],
    }

    if path.suffix.lower() != ".stl":
        # Sans noyau CAO, un STEP n'est pas inspectable : on ne prétend pas le
        # valider, on le dit.
        report["warnings"].append(
            f"{path.name} : contrôle d'emprise impossible sur un STEP — "
            f"exporter aussi un STL pour que la géométrie soit vérifiée"
        )
        return report

    try:
        bbox = stl_bounding_box(path)
    except CaseBuildError as exc:
        raise GeometryError(STATUS_GEOMETRY_UNREADABLE, exc.message) from exc

    report["bounding_box_m"] = bbox
    if bbox["n_vertices"] < MIN_VERTICES:
        raise GeometryError(
            STATUS_GEOMETRY_UNREADABLE,
            f"{path.name} ne contient que {bbox['n_vertices']} sommets — "
            f"la géométrie est vide ou corrompue",
        )

    expected = expected_bounding_box(design)
    if expected is None:
        report["warnings"].append(
            "emprise attendue non calculable : la conformité de la géométrie "
            "n'a pas pu être vérifiée"
        )
    else:
        chord_m = float(design["parameters"]["chord"]["value"]) / 1000.0
        tolerance = abs(chord_m) * tolerance_pct / 100.0
        problems = [
            f"{axis}_{bound} : {bbox[f'{axis}_{bound}']:.5f} m au lieu de "
            f"{expected[f'{axis}_{bound}']:.5f} m"
            for axis in ("x", "y", "z")
            for bound in ("min", "max")
            if abs(bbox[f"{axis}_{bound}"] - expected[f"{axis}_{bound}"]) > tolerance
        ]
        if problems:
            raise GeometryError(
                STATUS_GEOMETRY_MISMATCH,
                f"la géométrie exportée ne correspond pas à design_params.yaml "
                f"(tolérance {tolerance * 1000:.1f} mm) : " + " | ".join(problems),
                details={"actual": bbox, "expected": expected},
            )

    problems = _check_thickness(bbox, design, report)
    if problems:
        raise GeometryError(
            STATUS_CONSTRAINT_VIOLATED, " | ".join(problems), details=problems
        )

    # Une géométrie inchangée alors que les paramètres ont bougé signale que
    # l'export n'a pas suivi. C'est la panne la plus coûteuse : tout le reste
    # de la chaîne fonctionne, et l'agent optimise une constante.
    if previous_fingerprint and report["fingerprint"] == previous_fingerprint:
        current = _values(design.get("parameters"))
        earlier = _values(previous_parameters)
        if earlier is not None and current != earlier:
            raise GeometryError(
                STATUS_GEOMETRY_UNCHANGED,
                "géométrie rigoureusement identique à l'itération précédente "
                "alors que les paramètres ont changé — l'export n'a pas pris en "
                "compte les nouvelles valeurs",
                details={"previous": earlier, "current": current},
            )
        report["warnings"].append(
            "géométrie identique à l'itération précédente (paramètres inchangés)"
        )

    return report


def _values(parameters: Mapping[str, Any] | None) -> dict[str, float] | None:
    if not isinstance(parameters, Mapping):
        return None
    out: dict[str, float] = {}
    for name, spec in parameters.items():
        if isinstance(spec, Mapping) and isinstance(spec.get("value"), (int, float)):
            out[str(name)] = float(spec["value"])
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline/geometry_validator.py",
        description="Valide la géométrie exportée pour une itération.",
    )
    parser.add_argument("--iteration-dir", required=True)
    parser.add_argument(
        "--design-params", default=str(REPO_ROOT / "configs" / "design_params.yaml")
    )
    parser.add_argument("--previous-fingerprint", default=None)
    parser.add_argument("--tolerance-pct", type=float, default=5.0)
    args = parser.parse_args(argv)

    try:
        report = validate_geometry(
            Path(args.iteration_dir),
            Path(args.design_params),
            previous_fingerprint=args.previous_fingerprint,
            tolerance_pct=args.tolerance_pct,
        )
    except GeometryError as exc:
        print(
            json.dumps(
                {"status": exc.status, "error_message": exc.message,
                 "error_details": exc.details},
                indent=2, ensure_ascii=False, default=str,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
