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
import math
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from openfoam.case_builder import (  # noqa: E402
    CaseBuildError,
    _length_m,
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


#: Contraintes géométriques facultatives, en fraction de corde. Le §4 exige que
#: le rayon de bord d'attaque, l'épaisseur maximale et l'épaisseur de bord de
#: fuite « restent sous contrôle explicite ». Les bornes des coefficients CST
#: les contiennent indirectement — chaque coefficient ne peut déplacer la
#: surface que de 1,5 % de corde — mais indirectement n'est pas explicitement :
#: rien n'empêche DEUX coefficients de conspirer, et rien ne le SIGNALERAIT.
#:
#: Ces clés sont facultatives et absentes des fichiers de la v1.0, qui restent
#: donc valides. Déclarées, elles sont vérifiées à chaque itération sur la forme
#: RECONSTRUITE, et une violation fait échouer l'itération comme une géométrie
#: aberrante — franchement, plutôt que de dériver en silence.
GEOMETRIC_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("min_thickness_ratio", "thickness", "min"),
    ("max_thickness_ratio", "thickness", "max"),
    ("min_leading_edge_radius", "leading_edge_radius", "min"),
    ("max_leading_edge_radius", "leading_edge_radius", "max"),
    ("max_trailing_edge_thickness", "trailing_edge_thickness", "max"),
)

CONSTRAINT_LABELS = {
    "thickness": "épaisseur relative",
    "leading_edge_radius": "rayon de bord d'attaque",
    "trailing_edge_thickness": "épaisseur de bord de fuite",
}


def profile_measures(design: Mapping[str, Any]) -> dict[str, float] | None:
    """Grandeurs de forme de la géométrie décrite, en fraction de corde.

    Passe par la même fonction de profil que le driver plutôt que par une
    formule dupliquée : ce qui est mesuré ici est exactement ce qui sera écrit.
    """
    try:
        from fusion.parametric_driver import profile_from_parameters

        plan = profile_from_parameters(
            design.get("parameters") or {},
            design.get("parameterization"),
            design.get("provenance"),
        )
    except Exception:
        return None

    profile = plan.get("profile") or {}
    upper, lower = profile.get("upper") or [], profile.get("lower") or []
    chord_cm = float(plan.get("chord_cm") or 0.0)
    if not upper or not lower or chord_cm <= 0:
        return None

    measures = {
        "thickness": float(plan.get("thickness", 0.0)),
        "camber": float(plan.get("camber", 0.0)),
        "trailing_edge_thickness": (
            math.dist(upper[-1], lower[-1]) / chord_cm
        ),
    }
    if "leading_edge_radius" in plan:
        measures["leading_edge_radius"] = float(plan["leading_edge_radius"])
    return measures


def _check_geometric_constraints(
    design: Mapping[str, Any], report: dict
) -> list[str]:
    """Vérifie les bornes géométriques déclarées. Silencieux si aucune ne l'est."""
    constraints = design.get("constraints") or {}
    declared = [c for c in GEOMETRIC_CONSTRAINTS if c[0] in constraints]
    if not declared:
        return []

    measures = profile_measures(design)
    if measures is None:
        return [
            "contraintes géométriques déclarées mais la forme n'est pas "
            "mesurable : impossible de les vérifier"
        ]
    report["profile_measures"] = {k: round(v, 6) for k, v in measures.items()}

    problems: list[str] = []
    for key, quantity, sense in declared:
        value = measures.get(quantity)
        if value is None:
            problems.append(
                f"{key} déclarée mais {CONSTRAINT_LABELS[quantity]} non "
                f"mesurable sur cette paramétrisation"
            )
            continue
        try:
            limit = float(constraints[key])
        except (TypeError, ValueError):
            problems.append(f"constraints.{key} : nombre attendu")
            continue
        if sense == "min" and value < limit:
            problems.append(
                f"{CONSTRAINT_LABELS[quantity]} {value:.5f} c sous le minimum "
                f"{limit:.5f} c (constraints.{key})"
            )
        elif sense == "max" and value > limit:
            problems.append(
                f"{CONSTRAINT_LABELS[quantity]} {value:.5f} c au dessus du "
                f"maximum {limit:.5f} c (constraints.{key})"
            )
    return problems


def _thickness_ratio(
    design: Mapping[str, Any], params: Mapping[str, Any]
) -> float | None:
    """Épaisseur relative du profil, quelle que soit sa paramétrisation.

    En NACA elle est donnée : c'est un paramètre d'entrée. En CST elle est
    mesurée sur la forme reconstruite — les coefficients ne la portent pas
    explicitement. Sans cette seconde voie, la contrainte d'épaisseur minimale
    serait silencieusement abandonnée dès qu'on optimise un profil issu d'un
    fichier, c'est-à-dire précisément là où l'optimiseur peut le plus l'amincir.
    """
    if "thickness" in params:
        return float(params["thickness"]["value"])

    try:
        from profiles.geometry import collect_coefficients, cst_measures

        values = {
            name: float(spec["value"])
            for name, spec in params.items()
            if str(name).startswith(("cst_upper_", "cst_lower_"))
        }
        if not values:
            return None
        provenance = design.get("provenance") or {}
        return cst_measures(
            collect_coefficients(values, "cst_upper_"),
            collect_coefficients(values, "cst_lower_"),
            (
                float(provenance.get("trailing_edge_upper", 0.0) or 0.0),
                float(provenance.get("trailing_edge_lower", 0.0) or 0.0),
            ),
        )["thickness"]
    except Exception:
        return None


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
        # L'unité de la corde est lue, jamais supposée : un modèle décrit en
        # centimètres ou en pouces donnerait sinon un verdict absurde.
        chord_mm = _length_m(params["chord"], "chord") * 1000.0
        ratio = _thickness_ratio(design, params)
    except (KeyError, TypeError, ValueError, CaseBuildError):
        return problems
    if ratio is None:
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
        try:
            chord_m = _length_m(design["parameters"]["chord"], "chord")
        except (KeyError, TypeError, CaseBuildError) as exc:
            raise GeometryError(
                STATUS_GEOMETRY_MISMATCH,
                f"corde illisible dans design_params.yaml : {exc}",
            ) from exc
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
    problems += _check_geometric_constraints(design, report)
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
