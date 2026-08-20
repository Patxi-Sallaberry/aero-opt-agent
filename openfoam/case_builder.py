"""Construction d'un case OpenFOAM à partir des deux fichiers de configuration.

Traduit `design_params.yaml` (la forme) et `cfd_settings.yaml` (les conditions)
en un case complet, prêt pour blockMesh / snappyHexMesh / simpleFoam.

    python3 openfoam/case_builder.py --iteration-dir data/iterations/iter_0000
    python3 openfoam/case_builder.py --iteration-dir ... --print-values

Pourquoi un module Python plutôt que du sed dans `run_cfd.sh` : les grandeurs
du case ne sont pas de simples recopies. La taille du domaine, celle des
mailles, k, omega, la surface de référence et le point `locationInMesh` se
DÉDUISENT de la corde et de l'envergure de l'itération courante. Calculer tout
cela en bash à partir de YAML serait la partie la plus fragile du système.

Deux garde-fous méritent d'être connus :

1. **Références recalculées à chaque itération.** Aref = corde x envergure et
   lRef = corde sont repris de `design_params.yaml`. Garder une surface de
   référence figée pendant que la corde varie produirait des Cd/Cl qui
   semblent évoluer alors que seule la normalisation a bougé.

2. **Contrôle de cohérence de la géométrie.** La boîte englobante du STL est
   comparée à ce que la configuration décrit. C'est ce qui détecte qu'une
   itération a exporté une géométrie inchangée — le mode de défaillance le
   plus coûteux du système, puisqu'il ne produit aucune erreur et fait tourner
   une heure de CFD pour rien.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.utils import ConfigValidationError, load_yaml  # noqa: E402

DEFAULT_DESIGN_PARAMS = REPO_ROOT / "configs" / "design_params.yaml"
DEFAULT_CFD_SETTINGS = REPO_ROOT / "configs" / "cfd_settings.yaml"

STL_NAME = "wing.stl"
CASE_DIR_NAME = "cfd"
PLACEHOLDER_PREFIX = "@@"
PLACEHOLDER_SUFFIX = "@@"

# Constante du modèle k-omega, pour omega = sqrt(k) / (Cmu^0.25 * L).
C_MU = 0.09


class CaseBuildError(Exception):
    """Échec de préparation du case, avec un statut exploitable en aval."""

    def __init__(self, status: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


STATUS_CONFIG_ERROR = "CONFIG_ERROR"
STATUS_GEOMETRY_MISSING = "GEOMETRY_MISSING"
STATUS_GEOMETRY_CONVERSION_FAILED = "GEOMETRY_CONVERSION_FAILED"
STATUS_GEOMETRY_MISMATCH = "GEOMETRY_MISMATCH"
STATUS_TEMPLATE_ERROR = "TEMPLATE_ERROR"


# ─────────────────────────────────────────────────────────────────────────────
# Lecture des STL
# ─────────────────────────────────────────────────────────────────────────────


def stl_bounding_box(path: Path) -> dict[str, float]:
    """Boîte englobante d'un STL, ASCII ou binaire.

    Écrit à la main pour ne dépendre d'aucune bibliothèque de maillage : la
    seule information nécessaire est l'emprise, et elle sert de contrôle de
    cohérence avant de lancer le calcul.
    """
    if not path.is_file():
        raise CaseBuildError(STATUS_GEOMETRY_MISSING, f"STL introuvable : {path}")

    size = path.stat().st_size
    if size < 15:
        raise CaseBuildError(STATUS_GEOMETRY_MISSING, f"STL vide ou tronqué : {path}")

    with path.open("rb") as fh:
        header = fh.read(80)
        count_bytes = fh.read(4)
        n_tri = struct.unpack("<I", count_bytes)[0] if len(count_bytes) == 4 else 0
        # Un STL binaire fait exactement 84 + 50 x nTriangles octets. C'est le
        # seul critère fiable : le mot-clé « solid » en tête n'est pas exclusif
        # aux fichiers ASCII.
        binary = size == 84 + n_tri * 50

        coords: list[float] = []          # suite plate x, y, z, x, y, z, ...
        if binary:
            fh.seek(84)
            data = fh.read(n_tri * 50)
            for i in range(n_tri):
                base = i * 50 + 12  # on saute la normale
                coords.extend(struct.unpack_from("<9f", data, base))
            xs = coords[0::3]
            ys = coords[1::3]
            zs = coords[2::3]
        else:
            fh.seek(0)
            xs, ys, zs = [], [], []
            for raw in fh.read().decode("utf-8", errors="replace").splitlines():
                parts = raw.split()
                if len(parts) == 4 and parts[0] == "vertex":
                    try:
                        xs.append(float(parts[1]))
                        ys.append(float(parts[2]))
                        zs.append(float(parts[3]))
                    except ValueError:
                        continue

    if not xs:
        raise CaseBuildError(
            STATUS_GEOMETRY_MISSING, f"aucun sommet lisible dans {path}"
        )

    return {
        "x_min": min(xs), "x_max": max(xs),
        "y_min": min(ys), "y_max": max(ys),
        "z_min": min(zs), "z_max": max(zs),
        "n_vertices": len(xs),
    }


def ensure_stl(iteration_dir: Path, target: Path) -> dict[str, Any]:
    """Fournit un STL exploitable par snappyHexMesh, en mètres.

    snappyHexMesh ne lit pas le STEP. Trois cas, dans l'ordre :
      1. `geometry.stl` présent — utilisé tel quel (le driver Fusion peut
         l'exporter en même temps que le STEP) ;
      2. `geometry.step` + gmsh disponible — conversion ;
      3. sinon, échec explicite disant quoi installer ou activer.
    """
    stl_src = iteration_dir / "geometry.stl"
    step_src = iteration_dir / "geometry.step"
    target.parent.mkdir(parents=True, exist_ok=True)

    if stl_src.is_file() and stl_src.stat().st_size > 0:
        shutil.copyfile(stl_src, target)
        return {"source": str(stl_src), "converted": False}

    if not step_src.is_file():
        raise CaseBuildError(
            STATUS_GEOMETRY_MISSING,
            f"ni {stl_src.name} ni {step_src.name} dans {iteration_dir} — "
            f"le driver Fusion n'a pas produit de géométrie pour cette itération",
        )

    gmsh = shutil.which("gmsh")
    if gmsh is None:
        raise CaseBuildError(
            STATUS_GEOMETRY_CONVERSION_FAILED,
            f"seul {step_src.name} est disponible et gmsh est absent. "
            f"snappyHexMesh ne lit pas le STEP. Deux issues : installer gmsh "
            f"(apt install gmsh), ou faire exporter un STL au driver Fusion en "
            f"plus du STEP — c'est la voie la plus robuste, elle supprime la "
            f"conversion du chemin critique.",
        )

    cmd = [gmsh, str(step_src), "-2", "-format", "stl", "-o", str(target), "-v", "1"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise CaseBuildError(
            STATUS_GEOMETRY_CONVERSION_FAILED,
            f"gmsh n'a pas terminé la conversion en 600 s : {step_src}",
        ) from exc
    if proc.returncode != 0 or not target.is_file():
        raise CaseBuildError(
            STATUS_GEOMETRY_CONVERSION_FAILED,
            f"gmsh a échoué sur {step_src} (code {proc.returncode}) : "
            f"{(proc.stderr or proc.stdout or '').strip()[:500]}",
        )
    return {"source": str(step_src), "converted": True, "converter": "gmsh"}


# ─────────────────────────────────────────────────────────────────────────────
# Grandeurs dérivées
# ─────────────────────────────────────────────────────────────────────────────


def _length_m(spec: Mapping[str, Any], name: str) -> float:
    """Longueur du YAML convertie en mètres (unité de travail d'OpenFOAM)."""
    factors = {"mm": 1e-3, "cm": 1e-2, "m": 1.0, "in": 0.0254, "ft": 0.3048}
    unit = str(spec.get("unit", "")).strip().lower()
    if unit not in factors:
        raise CaseBuildError(
            STATUS_CONFIG_ERROR,
            f"parameters.{name} : longueur attendue, unité {spec.get('unit')!r} "
            f"reçue — attendu l'une de {sorted(factors)}",
        )
    return float(spec["value"]) * factors[unit]


def _angle_deg(spec: Mapping[str, Any], name: str) -> float:
    unit = str(spec.get("unit", "")).strip().lower()
    if unit in ("deg", "degree", "degrees"):
        return float(spec["value"])
    if unit in ("rad", "radian"):
        return math.degrees(float(spec["value"]))
    raise CaseBuildError(
        STATUS_CONFIG_ERROR,
        f"parameters.{name} : angle attendu, unité {spec.get('unit')!r} reçue",
    )


def _vec(values: Any, name: str) -> tuple[float, float, float]:
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        raise CaseBuildError(
            STATUS_CONFIG_ERROR, f"{name} : vecteur de 3 composantes attendu"
        )
    return tuple(float(v) for v in values)  # type: ignore[return-value]


def compute_case_values(
    design: Mapping[str, Any], cfd: Mapping[str, Any]
) -> dict[str, Any]:
    """Calcule toutes les grandeurs du case à partir des deux configurations.

    Fonction pure et sans effet de bord : c'est elle qui porte la physique du
    montage, donc c'est elle qu'il faut pouvoir tester sans OpenFOAM.
    """
    params = design.get("parameters")
    if not isinstance(params, dict):
        raise CaseBuildError(STATUS_CONFIG_ERROR, "design_params : 'parameters' absent")
    for required in ("chord", "span", "aoa"):
        if required not in params:
            raise CaseBuildError(
                STATUS_CONFIG_ERROR,
                f"design_params : paramètre '{required}' requis pour dimensionner "
                f"le case CFD",
            )

    chord = _length_m(params["chord"], "chord")
    span = _length_m(params["span"], "span")
    aoa_deg = _angle_deg(params["aoa"], "aoa")
    if chord <= 0 or span <= 0:
        raise CaseBuildError(
            STATUS_CONFIG_ERROR, f"corde ({chord} m) et envergure ({span} m) > 0 attendues"
        )

    flow = cfd.get("flow", {})
    domain = cfd.get("domain", {})
    mesh = cfd.get("mesh", {})
    reference = cfd.get("reference", {})
    control = cfd.get("solver_control", {})
    convergence = cfd.get("convergence", {})
    execution = cfd.get("execution", {})
    case = cfd.get("case", {})

    u_inf = float(flow.get("velocity_ms", 20.0))
    if u_inf <= 0:
        raise CaseBuildError(STATUS_CONFIG_ERROR, "flow.velocity_ms doit être > 0")
    drag_dir = _vec(flow.get("direction", [1.0, 0.0, 0.0]), "flow.direction")
    lift_dir = _vec(flow.get("lift_direction", [0.0, 1.0, 0.0]), "flow.lift_direction")
    pitch_axis = _vec(flow.get("pitch_axis", [0.0, 0.0, 1.0]), "flow.pitch_axis")

    # Turbulence de l'écoulement amont.
    intensity = float(flow.get("turbulent_intensity", 0.05))
    length_scale = float(flow.get("turbulent_length_scale_m", 0.01))
    if intensity <= 0 or length_scale <= 0:
        raise CaseBuildError(
            STATUS_CONFIG_ERROR,
            "flow.turbulent_intensity et turbulent_length_scale_m doivent être > 0",
        )
    k_inf = 1.5 * (intensity * u_inf) ** 2
    omega_inf = math.sqrt(k_inf) / (C_MU**0.25 * length_scale)

    # ── Domaine ──────────────────────────────────────────────────────────
    upstream = float(domain.get("upstream_factor", 5.0)) * chord
    downstream = float(domain.get("downstream_factor", 12.0)) * chord
    vertical = float(domain.get("vertical_factor", 5.0)) * chord

    x_min, x_max = -upstream, downstream
    y_min, y_max = -vertical, vertical

    treatment = str(domain.get("spanwise_treatment", "symmetry")).lower()
    if treatment not in ("symmetry", "full_3d"):
        raise CaseBuildError(
            STATUS_CONFIG_ERROR,
            f"domain.spanwise_treatment : {treatment!r} inconnu — "
            f"attendu 'symmetry' ou 'full_3d'",
        )

    if treatment == "symmetry":
        # Tranche centrale strictement intérieure à l'aile : la géométrie
        # traverse les deux plans de symétrie, donc aucun bout d'aile dans le
        # domaine.
        fraction = float(domain.get("spanwise_fraction", 0.5))
        if not 0.0 < fraction < 1.0:
            raise CaseBuildError(
                STATUS_CONFIG_ERROR,
                f"domain.spanwise_fraction doit être dans ]0, 1[, reçu {fraction}",
            )
        half = span * fraction / 2.0
        z_center = span / 2.0
        z_min, z_max = z_center - half, z_center + half
        side_patch_type = "symmetry"
        side_field_type = "symmetry"
    else:
        margin = float(domain.get("spanwise_margin_factor", 3.0)) * chord
        z_min, z_max = -margin, span + margin
        side_patch_type = "patch"
        side_field_type = "slip"

    # ── Maillage de fond ─────────────────────────────────────────────────
    cells_per_chord = float(mesh.get("base_cell_per_chord", 8.0))
    if cells_per_chord <= 0:
        raise CaseBuildError(
            STATUS_CONFIG_ERROR, "mesh.base_cell_per_chord doit être > 0"
        )
    base_cell = chord / cells_per_chord
    n_x = max(4, round((x_max - x_min) / base_cell))
    n_y = max(4, round((y_max - y_min) / base_cell))
    n_z = max(3, round((z_max - z_min) / base_cell))

    levels = mesh.get("surface_refinement_level", [3, 4])
    if not isinstance(levels, (list, tuple)) or len(levels) != 2:
        raise CaseBuildError(
            STATUS_CONFIG_ERROR,
            "mesh.surface_refinement_level : deux entiers attendus [min, max]",
        )

    box = mesh.get("refinement_box", {})
    box_x_min = -float(box.get("upstream", 0.5)) * chord
    box_x_max = chord * (1.0 + float(box.get("downstream", 3.0)))
    box_y = float(box.get("vertical", 1.0)) * chord

    layers = mesh.get("boundary_layers", {})
    add_layers = bool(layers.get("enabled", True))

    # Point dans le fluide : en amont du bord d'attaque, décalé en Y pour
    # rester hors de toute enveloppe du profil quelle que soit l'incidence.
    location = (
        x_min + 0.5 * (0 - x_min),
        y_max * 0.5,
        (z_min + z_max) / 2.0,
    )

    # ── Références aérodynamiques ────────────────────────────────────────
    ref_mode = str(reference.get("mode", "from_design")).lower()
    domain_span = z_max - z_min
    if ref_mode == "fixed":
        a_ref = float(reference.get("area_m2", chord * span))
        l_ref = float(reference.get("length_m", chord))
    elif ref_mode == "from_design":
        # En quasi-2D, seule la tranche modélisée porte des efforts : la
        # surface de référence doit être celle du domaine, pas celle de l'aile
        # entière, sinon Cl et Cd sont divisés par la mauvaise surface.
        a_ref = chord * (domain_span if treatment == "symmetry" else span)
        l_ref = chord
    else:
        raise CaseBuildError(
            STATUS_CONFIG_ERROR,
            f"reference.mode : {ref_mode!r} inconnu — attendu 'from_design' ou 'fixed'",
        )

    cofr_fraction = float(reference.get("center_of_rotation_chord_fraction", 0.25))
    cofr = (cofr_fraction * chord, 0.0, (z_min + z_max) / 2.0)

    check = mesh.get("check_mesh", {})

    values: dict[str, Any] = {
        # solveur
        "SOLVER": str(case.get("solver", "simpleFoam")),
        "TURBULENCE_MODEL": str(case.get("turbulence_model", "kOmegaSST")),
        "WING_PATCH": str(case.get("wing_patch", "wing")),
        "START_TIME": int(control.get("start_time", 0)),
        "END_TIME": int(control.get("end_time", 2000)),
        "WRITE_INTERVAL": int(control.get("write_interval", 500)),
        "PURGE_WRITE": int(control.get("purge_write", 2)),
        # écoulement
        "NU": float(flow.get("nu_m2_s", 1.5e-5)),
        "RHO": float(flow.get("rho_kg_m3", 1.225)),
        "MAG_U": u_inf,
        "U_INF_VECTOR": " ".join(f"{u_inf * c:.8g}" for c in drag_dir),
        "K_INF": k_inf,
        "OMEGA_INF": omega_inf,
        "DRAG_DIR": " ".join(f"{c:g}" for c in drag_dir),
        "LIFT_DIR": " ".join(f"{c:g}" for c in lift_dir),
        "PITCH_AXIS": " ".join(f"{c:g}" for c in pitch_axis),
        "COFR": " ".join(f"{c:.8g}" for c in cofr),
        "A_REF": a_ref,
        "L_REF": l_ref,
        # domaine
        "X_MIN": x_min, "X_MAX": x_max,
        "Y_MIN": y_min, "Y_MAX": y_max,
        "Z_MIN": z_min, "Z_MAX": z_max,
        "N_X": n_x, "N_Y": n_y, "N_Z": n_z,
        "SIDE_PATCH_TYPE": side_patch_type,
        "SIDE_FIELD_TYPE": side_field_type,
        # snappy
        "SURFACE_LEVEL_MIN": int(levels[0]),
        "SURFACE_LEVEL_MAX": int(levels[1]),
        "FEATURE_LEVEL": int(mesh.get("feature_refinement_level", levels[1])),
        "FEATURE_ANGLE": float(mesh.get("feature_angle_deg", 150)),
        "BOX_X_MIN": box_x_min, "BOX_X_MAX": box_x_max,
        "BOX_Y_MIN": -box_y, "BOX_Y_MAX": box_y,
        "BOX_Z_MIN": z_min - abs(z_max - z_min),
        "BOX_Z_MAX": z_max + abs(z_max - z_min),
        "BOX_LEVEL": int(box.get("level", 2)),
        "MAX_LOCAL_CELLS": int(mesh.get("max_local_cells", 2000000)),
        "MAX_GLOBAL_CELLS": int(mesh.get("max_global_cells", 8000000)),
        "LOCATION_IN_MESH": " ".join(f"{c:.8g}" for c in location),
        "ADD_LAYERS": "true" if add_layers else "false",
        "N_LAYERS": int(layers.get("n_layers", 5)),
        "EXPANSION_RATIO": float(layers.get("expansion_ratio", 1.2)),
        "FINAL_LAYER_THICKNESS": float(layers.get("final_layer_thickness", 0.4)),
        "MIN_LAYER_THICKNESS": float(layers.get("min_thickness", 0.02)),
        "MAX_NON_ORTHO": float(check.get("max_non_orthogonality", 70.0)),
        "MAX_SKEWNESS": float(check.get("max_skewness", 4.0)),
        # solveur linéaire
        "RESIDUAL_P": float(convergence.get("residual_p", 1e-4)),
        "RESIDUAL_U": float(convergence.get("residual_U", 1e-5)),
        "RESIDUAL_K": float(convergence.get("residual_k", 1e-5)),
        "RESIDUAL_OMEGA": float(convergence.get("residual_omega", 1e-5)),
        "FORCE_WRITE_INTERVAL": int(cfd.get("force_coeffs", {}).get("write_interval", 1)),
        # exécution
        "N_PROCS": int(execution.get("n_processors", 4)),
        "DECOMPOSITION_METHOD": str(execution.get("decomposition_method", "scotch")),
    }

    # Conservé pour le contrôle de cohérence et le rapport, hors substitution.
    values["_design"] = {
        "chord_m": chord,
        "span_m": span,
        "aoa_deg": aoa_deg,
        "reynolds": u_inf * chord / float(flow.get("nu_m2_s", 1.5e-5)),
        "spanwise_treatment": treatment,
        "domain_span_m": domain_span,
        "a_ref_m2": a_ref,
        "l_ref_m": l_ref,
        "base_cell_m": base_cell,
        "background_cells": n_x * n_y * n_z,
    }
    return values


# ─────────────────────────────────────────────────────────────────────────────
# Contrôle de cohérence géométrique
# ─────────────────────────────────────────────────────────────────────────────


def expected_bounding_box(design: Mapping[str, Any]) -> dict[str, float] | None:
    """Emprise attendue du STL, en mètres, d'après design_params.yaml.

    Recalculée avec la MÊME fonction de profil que le driver Fusion, pour que
    le contrôle porte sur la géométrie réellement demandée et non sur une
    formule dupliquée qui pourrait diverger.
    """
    try:
        from fusion.parametric_driver import profile_from_parameters
    except Exception:
        return None

    try:
        plan = profile_from_parameters(design["parameters"])
    except Exception:
        return None

    bbox = plan["bbox_cm"]
    return {
        "x_min": bbox["x_min"] / 100.0,
        "x_max": bbox["x_max"] / 100.0,
        "y_min": bbox["y_min"] / 100.0,
        "y_max": bbox["y_max"] / 100.0,
        "z_min": 0.0,
        "z_max": plan["span_cm"] / 100.0,
    }


def detect_scale_factor(
    actual: Mapping[str, float],
    expected: Mapping[str, float],
    tolerance_pct: float = 5.0,
) -> float | None:
    """Facteur d'échelle entre le STL et la géométrie demandée.

    Fusion, gmsh et les convertisseurs CAO n'écrivent pas tous dans la même
    unité, et l'unité d'export n'est pas garantie d'une version à l'autre.
    Plutôt que de supposer, on la MESURE : on compare l'étendue du fichier à
    celle attendue et on retient le facteur usuel qui colle.

    Returns:
        1.0 si le STL est déjà en mètres, 1e-3 s'il est en millimètres, 1e-2
        en centimètres, ou None si aucun facteur usuel n'explique l'écart —
        auquel cas ce n'est pas un problème d'unité mais de géométrie.
    """
    def extent(box: Mapping[str, float]) -> float:
        return max(
            float(box["x_max"]) - float(box["x_min"]),
            float(box["y_max"]) - float(box["y_min"]),
            float(box["z_max"]) - float(box["z_min"]),
        )

    expected_extent = extent(expected)
    actual_extent = extent(actual)
    if expected_extent <= 0 or actual_extent <= 0:
        return None

    for factor in (1.0, 1e-3, 1e-2, 1e-1, 10.0, 100.0, 1000.0):
        if abs(actual_extent * factor - expected_extent) <= expected_extent * (
            tolerance_pct / 100.0
        ):
            return factor
    return None


def rescale_stl(path: Path, factor: float) -> dict[str, float]:
    """Réécrit un STL ASCII à l'échelle demandée et rend sa nouvelle emprise."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[str] = []
    for line in lines:
        parts = line.split()
        if len(parts) == 4 and parts[0] == "vertex":
            x, y, z = (float(v) * factor for v in parts[1:4])
            out.append(f"      vertex {x:.8e} {y:.8e} {z:.8e}")
        else:
            out.append(line)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return stl_bounding_box(path)


def to_ascii_stl(path: Path) -> None:
    """Convertit un STL binaire en ASCII, pour pouvoir le remettre à l'échelle."""
    bbox = stl_bounding_box(path)
    del bbox  # la lecture valide le fichier ; la conversion suit

    with path.open("rb") as fh:
        header = fh.read(80)
        del header
        n_tri = struct.unpack("<I", fh.read(4))[0]
        data = fh.read(n_tri * 50)

    lines = ["solid wing"]
    for i in range(n_tri):
        base = i * 50
        nx, ny, nz = struct.unpack_from("<3f", data, base)
        vertices = struct.unpack_from("<9f", data, base + 12)
        lines.append(f"  facet normal {nx:.6e} {ny:.6e} {nz:.6e}")
        lines.append("    outer loop")
        for j in range(3):
            x, y, z = vertices[j * 3: j * 3 + 3]
            lines.append(f"      vertex {x:.8e} {y:.8e} {z:.8e}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid wing")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def is_binary_stl(path: Path) -> bool:
    """Vrai si le fichier est un STL binaire (critère : la taille exacte)."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        fh.seek(80)
        count = fh.read(4)
    if len(count) != 4:
        return False
    return size == 84 + struct.unpack("<I", count)[0] * 50


def normalize_stl_scale(
    path: Path,
    expected: Mapping[str, float] | None,
    tolerance_pct: float,
) -> tuple[dict[str, float], list[str]]:
    """Ramène le STL en mètres si son unité diffère, et rend son emprise."""
    bbox = stl_bounding_box(path)
    if expected is None:
        return bbox, []

    factor = detect_scale_factor(bbox, expected, tolerance_pct)
    if factor is None or factor == 1.0:
        return bbox, []

    if is_binary_stl(path):
        to_ascii_stl(path)
    bbox = rescale_stl(path, factor)
    return bbox, [
        f"STL remis à l'échelle d'un facteur {factor:g} : le fichier n'était pas "
        f"en mètres (unité d'export CAO). Emprise corrigée "
        f"x [{bbox['x_min']:.4f}, {bbox['x_max']:.4f}] m."
    ]


def check_geometry(
    actual: Mapping[str, float],
    expected: Mapping[str, float] | None,
    chord_m: float,
    tolerance_pct: float,
) -> list[str]:
    """Compare l'emprise du STL à celle attendue. Lève si l'écart est net.

    C'est le contrôle qui attrape une géométrie qui n'a pas suivi les
    paramètres : sans lui, une aile inchangée traverserait toute la chaîne
    jusqu'à des coefficients parfaitement plausibles mais faux.
    """
    if expected is None:
        return [
            "cohérence géométrique non vérifiée : emprise attendue non calculable "
            "(paramètres de forme absents ou driver Fusion non importable)"
        ]

    tolerance = abs(chord_m) * tolerance_pct / 100.0
    problems: list[str] = []
    for axis in ("x", "y", "z"):
        for bound in ("min", "max"):
            key = f"{axis}_{bound}"
            delta = abs(float(actual[key]) - float(expected[key]))
            if delta > tolerance:
                problems.append(
                    f"{key} : STL {float(actual[key]):.5f} m vs attendu "
                    f"{float(expected[key]):.5f} m (écart {delta * 1000:.2f} mm)"
                )

    if problems:
        raise CaseBuildError(
            STATUS_GEOMETRY_MISMATCH,
            "la géométrie exportée ne correspond pas à design_params.yaml "
            f"(tolérance {tolerance * 1000:.2f} mm) : " + " | ".join(problems) + ". "
            "Soit l'export Fusion n'a pas suivi les paramètres, soit une "
            "conversion a changé d'échelle.",
            details={"actual": dict(actual), "expected": dict(expected)},
        )
    return []


# ─────────────────────────────────────────────────────────────────────────────
# Rendu des templates
# ─────────────────────────────────────────────────────────────────────────────


def render(text: str, values: Mapping[str, Any]) -> str:
    """Substitue les jetons @@NOM@@ ; refuse d'en laisser un seul en place."""
    out = text
    for key, value in values.items():
        if key.startswith("_"):
            continue
        token = f"{PLACEHOLDER_PREFIX}{key}{PLACEHOLDER_SUFFIX}"
        if token in out:
            out = out.replace(token, _format(value))

    leftovers = _find_placeholders(out)
    if leftovers:
        raise CaseBuildError(
            STATUS_TEMPLATE_ERROR,
            f"jeton(s) non substitué(s) : {sorted(leftovers)} — le template et "
            f"case_builder.py ont divergé",
        )
    return out


def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:.10g}"
    return str(value)


def _find_placeholders(text: str) -> set[str]:
    found: set[str] = set()
    start = 0
    while True:
        i = text.find(PLACEHOLDER_PREFIX, start)
        if i < 0:
            return found
        j = text.find(PLACEHOLDER_SUFFIX, i + len(PLACEHOLDER_PREFIX))
        if j < 0:
            return found
        name = text[i + len(PLACEHOLDER_PREFIX):j]
        # Un jeton est un identifiant en majuscules ; tout le reste est du
        # texte OpenFOAM légitime.
        if name and name.replace("_", "").isalnum() and name.upper() == name:
            found.add(name)
        start = j + len(PLACEHOLDER_SUFFIX)


def build_case(
    iteration_dir: Path,
    design_params_path: Path = DEFAULT_DESIGN_PARAMS,
    cfd_settings_path: Path = DEFAULT_CFD_SETTINGS,
    case_dir: Path | None = None,
) -> dict[str, Any]:
    """Prépare le case complet pour une itération et retourne un résumé."""
    iteration_dir = Path(iteration_dir)
    if not iteration_dir.is_dir():
        raise CaseBuildError(
            STATUS_GEOMETRY_MISSING, f"dossier d'itération absent : {iteration_dir}"
        )

    # Contrôle en tête : inutile de rendre trente fichiers pour découvrir
    # ensuite qu'aucune géométrie n'a été exportée.
    if not any(
        (iteration_dir / name).is_file() for name in ("geometry.stl", "geometry.step")
    ):
        raise CaseBuildError(
            STATUS_GEOMETRY_MISSING,
            f"ni geometry.stl ni geometry.step dans {iteration_dir} — le driver "
            f"Fusion n'a pas produit de géométrie pour cette itération",
        )

    try:
        design = load_yaml(design_params_path)
        cfd = load_yaml(cfd_settings_path)
    except ConfigValidationError as exc:
        raise CaseBuildError(STATUS_CONFIG_ERROR, str(exc)) from exc

    values = compute_case_values(design, cfd)
    case_dir = Path(case_dir) if case_dir else iteration_dir / CASE_DIR_NAME

    template_dir = REPO_ROOT / str(
        cfd.get("case", {}).get("template_dir", "openfoam/templates/external_aero")
    )
    if not template_dir.is_dir():
        raise CaseBuildError(
            STATUS_TEMPLATE_ERROR, f"template de case introuvable : {template_dir}"
        )

    # Un case reconstruit de zéro à chaque fois : un reliquat de l'itération
    # précédente (ancien maillage, anciens pas de temps) fausserait le calcul
    # en silence.
    if case_dir.exists():
        shutil.rmtree(case_dir)
    case_dir.mkdir(parents=True)

    rendered: list[str] = []
    for src in sorted(template_dir.rglob("*")):
        if src.is_dir() or src.name.startswith("."):
            continue
        dst = case_dir / src.relative_to(template_dir)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(
            render(src.read_text(encoding="utf-8"), values), encoding="utf-8"
        )
        rendered.append(str(dst.relative_to(case_dir)))

    stl_path = case_dir / "constant" / "triSurface" / STL_NAME
    geometry = ensure_stl(iteration_dir, stl_path)

    check_cfg = cfd.get("geometry_check", {})
    tolerance_pct = float(check_cfg.get("tolerance_pct", 5.0))
    expected = expected_bounding_box(design)

    # La mise à l'échelle précède le contrôle : un STL en millimètres est un
    # problème d'unité, pas de géométrie, et il se corrige tout seul.
    bbox, warnings = normalize_stl_scale(stl_path, expected, tolerance_pct)

    if bool(check_cfg.get("enabled", True)):
        warnings.extend(
            check_geometry(
                bbox, expected, values["_design"]["chord_m"], tolerance_pct
            )
        )

    summary = {
        "case_dir": str(case_dir),
        "iteration": design.get("iteration"),
        "design_id": design.get("design_id"),
        "files_rendered": len(rendered),
        "geometry": {**geometry, "bounding_box_m": bbox},
        "values": values["_design"],
        "warnings": warnings,
    }
    (case_dir / "case_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return summary


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="openfoam/case_builder.py",
        description="Prépare un case OpenFOAM pour une itération.",
    )
    parser.add_argument(
        "--iteration-dir", required=True,
        help="dossier de l'itération (contient geometry.stl ou geometry.step)",
    )
    parser.add_argument("--design-params", default=str(DEFAULT_DESIGN_PARAMS))
    parser.add_argument("--cfd-settings", default=str(DEFAULT_CFD_SETTINGS))
    parser.add_argument(
        "--case-dir", default=None,
        help="destination du case (défaut : <iteration-dir>/cfd)",
    )
    parser.add_argument(
        "--print-values", action="store_true",
        help="affiche les grandeurs calculées sans rien écrire",
    )
    args = parser.parse_args(argv)

    try:
        if args.print_values:
            design = load_yaml(Path(args.design_params))
            cfd = load_yaml(Path(args.cfd_settings))
            values = compute_case_values(design, cfd)
            print(json.dumps(
                {k: v for k, v in values.items() if not k.startswith("_")}
                | {"_design": values["_design"]},
                indent=2, ensure_ascii=False, default=str,
            ))
            return 0

        summary = build_case(
            Path(args.iteration_dir),
            Path(args.design_params),
            Path(args.cfd_settings),
            Path(args.case_dir) if args.case_dir else None,
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
        for warning in summary["warnings"]:
            print(f"[AVERTISSEMENT] {warning}", file=sys.stderr)
        return 0

    except CaseBuildError as exc:
        print(
            json.dumps(
                {"success": False, "status": exc.status, "error_message": exc.message,
                 "error_details": exc.details},
                indent=2, ensure_ascii=False, default=str,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
