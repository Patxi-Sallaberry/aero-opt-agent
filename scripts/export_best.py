"""Post-traitement d'une optimisation : meilleur design, rapport et visuels.

    python3 scripts/export_best.py --iterations-dir data/iterations
    python3 scripts/export_best.py --iterations-dir data/iterations \
        --qualified-dir data/qualify/iter_0000 --no-visuals

Appelé automatiquement à la fin de `scripts/run_loop.py`, ou à la main sur une
série déjà exécutée.

Produit `results/run_AAAAMMJJ_HHMMSS/best_design/` :

    README.md                le rapport complet
    report.html              le même, autonome, à ouvrir dans un navigateur
    geometry.stl             la géométrie, en mètres
    geometry.step            si la CAO en a produit un
    profile_section.csv/.dat la section, pour reprendre la forme en CAO
    design_params.yaml       les paramètres exacts, rejouables
    results.json             les coefficients
    figures/                 courbes SVG et images CFD
    cfd/                     le case OpenFOAM (maillage et champs)
    logs/                    les journaux de chaque étape

Après une optimisation, le meilleur design est enfoui dans une arborescence
d'itérations dont la plupart ne servent plus, et les chiffres ne parlent qu'à
qui a suivi la série. Ce dossier est fait pour être lu et transmis tel quel.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from fusion.parametric_driver import profile_from_parameters  # noqa: E402
from pipeline import master_pipeline as mp  # noqa: E402
from pipeline.utils import load_yaml  # noqa: E402
from scripts import plots  # noqa: E402

RESULTS_ROOT = REPO_ROOT / "results"
PARAVIEW_SCRIPT = REPO_ROOT / "scripts" / "paraview_render.py"

# Dictionnaire d'échantillonnage de la pression de paroi, écrit dans le case le
# temps d'un appel à `postProcess`.
WING_SURFACE_FUNC = """type            surfaces;
libs            ("libsampling.so");
surfaceFormat   raw;
fields          (p);
interpolationScheme cell;
surfaces
{
    wing
    {
        type        patch;
        patches     (%s);
        interpolate false;
    }
}
"""


class ExportError(Exception):
    """Rien d'exploitable à exporter."""


# ─────────────────────────────────────────────────────────────────────────────
# Sélection
# ─────────────────────────────────────────────────────────────────────────────


def best_iteration(iterations_root: Path) -> dict:
    """Itération réussie au meilleur objectif.

    Le classement se fait sur `objective`, qui normalise tous les objectifs en
    « plus c'est grand, mieux c'est ». Trier sur Cl/Cd donnerait un contresens
    pour un objectif de minimisation de traînée ou de maximisation d'appui.
    """
    candidates = [
        record
        for record in mp.history(iterations_root)
        if record.get("success") and isinstance(record.get("objective"), (int, float))
    ]
    if not candidates:
        raise ExportError(
            f"no successful iteration in {iterations_root} — nothing to export"
        )
    return max(candidates, key=lambda record: float(record["objective"]))


def iteration_dir(iterations_root: Path, iteration: int) -> Path:
    return Path(iterations_root) / f"iter_{int(iteration):04d}"


def run_folder(root: Path | None = None, when: datetime | None = None) -> Path:
    """`results/run_AAAAMMJJ_HHMMSS/` — un dossier par série, jamais écrasé.

    La racine est lue à l'APPEL, pas figée dans la signature. Une valeur par
    défaut `root=RESULTS_ROOT` serait liée une fois pour toutes au chargement
    du module : redéfinir `RESULTS_ROOT` ensuite n'aurait plus aucun effet.

    Ce n'est pas une précaution théorique. Les tests redirigent précisément
    cette constante pour ne rien écrire dans le dépôt ; avec la valeur figée,
    la redirection ne faisait rien et chaque série de test laissait un dossier
    horodaté sur le disque — le garde-fou passait au vert sans rien garder.
    """
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return Path(RESULTS_ROOT if root is None else root) / f"run_{stamp}"


# ─────────────────────────────────────────────────────────────────────────────
# Section du profil
# ─────────────────────────────────────────────────────────────────────────────


def _write_chordwise_dat(
    design: Mapping[str, Any],
    plan: Mapping[str, Any],
    output: Path,
    upper: list[tuple[float, float]],
    lower: list[tuple[float, float]],
) -> Path:
    """Écrit le profil redressé et normalisé, au format profil standard.

    On défait la rotation d'incidence et l'on ramène la corde à un. Le fichier
    obtenu est directement comparable à celui dont on est parti, et
    directement utilisable dans un outil qui pilote lui-même l'incidence.
    """
    import math as _math

    angle = _math.radians(float(plan.get("aoa_deg", 0.0)))
    cos_a, sin_a = _math.cos(angle), _math.sin(angle)
    chord_mm = float(plan["chord_cm"]) * 10.0 or 1.0

    def straighten(point: tuple[float, float]) -> tuple[float, float]:
        x, y = point
        return (
            (x * cos_a - y * sin_a) / chord_mm,
            (x * sin_a + y * cos_a) / chord_mm,
        )

    straight_upper = [straighten(p) for p in upper]
    straight_lower = [straighten(p) for p in lower]

    lines = [f"{design.get('design_id', 'wing')} (corde unitaire, incidence nulle)"]
    lines += [f"{x:12.6f}{y:12.6f}" for x, y in reversed(straight_upper)]
    lines += [f"{x:12.6f}{y:12.6f}" for x, y in straight_lower[1:]]
    target = output / "profile_chord.dat"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def write_profile_section(design: Mapping[str, Any], output: Path) -> dict:
    """Écrit les coordonnées de la section, en millimètres.

    Deux formats : un CSV lisible par tout tableur ou script d'import CAO, et
    le format profil standard que lisent XFOIL, XFLR5 et la plupart des outils
    aérodynamiques. L'incidence est déjà appliquée : c'est la section
    réellement simulée.
    """
    plan = profile_from_parameters(
        design["parameters"],
        design.get("parameterization"),
        design.get("provenance"),
    )
    upper = [(x * 10.0, y * 10.0) for x, y in plan["profile"]["upper"]]  # cm -> mm
    lower = [(x * 10.0, y * 10.0) for x, y in plan["profile"]["lower"]]

    csv_lines = ["surface,x_mm,y_mm"]
    csv_lines += [f"extrados,{x:.6f},{y:.6f}" for x, y in upper]
    csv_lines += [f"intrados,{x:.6f},{y:.6f}" for x, y in lower]
    (output / "profile_section.csv").write_text(
        "\n".join(csv_lines) + "\n", encoding="utf-8"
    )

    dat_lines = [f"{design.get('design_id', 'wing')} optimise"]
    dat_lines += [f"{x:12.6f}{y:12.6f}" for x, y in reversed(upper)]
    dat_lines += [f"{x:12.6f}{y:12.6f}" for x, y in lower[1:]]
    (output / "profile_section.dat").write_text(
        "\n".join(dat_lines) + "\n", encoding="utf-8"
    )

    # Troisième forme : le profil REDRESSÉ, en corde unitaire. C'est la
    # convention des fichiers de profil publiés, et c'est ce qu'attendent XFOIL
    # ou XFLR5 pour balayer l'incidence — leur donner une section déjà inclinée
    # de 3° ferait compter cette incidence deux fois, et tout le polaire serait
    # décalé sans que rien ne le signale.
    _write_chordwise_dat(design, plan, output, upper, lower)

    return {
        "upper": upper,
        "lower": lower,
        "chord_mm": plan["chord_cm"] * 10.0,
        "span_mm": plan["span_cm"] * 10.0,
        "aoa_deg": plan["aoa_deg"],
        "thickness": plan["thickness"],
        "camber": plan["camber"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Distribution de pression sur la paroi
# ─────────────────────────────────────────────────────────────────────────────


def sample_wing_pressure(
    case_dir: Path, patch: str = "wing", timeout_s: int = 600
) -> list[tuple[float, float, float]] | None:
    """Échantillonne p sur la paroi. Retourne [(x, y, p), ...] ou None.

    Passe par `postProcess` d'OpenFOAM plutôt que de relire le champ à la main :
    la correspondance entre valeurs de faces et centres de faces demanderait de
    décoder le maillage, alors que l'outil sait déjà le faire.
    """
    case_dir = Path(case_dir)
    if not (case_dir / "system" / "controlDict").is_file():
        return None

    func = case_dir / "system" / "wingSurfaceExport"
    try:
        func.write_text(WING_SURFACE_FUNC % patch, encoding="utf-8")
    except OSError:
        return None

    command = (
        "set +u; "
        f". {_foam_bashrc()} >/dev/null 2>&1; "
        f"cd {case_dir!s} && postProcess -func wingSurfaceExport -latestTime"
    )
    try:
        proc = subprocess.run(
            ["bash", "-lc", command], capture_output=True, text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None

    raw = sorted(
        (case_dir / "postProcessing" / "wingSurfaceExport").rglob("p_*.raw")
    )
    if not raw:
        return None

    points: list[tuple[float, float, float]] = []
    for line in raw[-1].read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        try:
            points.append((float(parts[0]), float(parts[1]), float(parts[3])))
        except ValueError:
            continue
    return points or None


def _foam_bashrc() -> str:
    import os

    candidate = os.environ.get("FOAM_BASHRC")
    if candidate and Path(candidate).is_file():
        return candidate
    for pattern in ("/usr/lib/openfoam/openfoam*/etc/bashrc",
                    "/opt/openfoam*/etc/bashrc"):
        found = sorted(Path("/").glob(pattern.lstrip("/")))
        if found:
            return str(found[-1])
    return "/dev/null"


def cp_distribution(
    samples: Sequence[tuple[float, float, float]],
    section: Mapping[str, Any],
    u_inf: float,
    bins: int = 90,
) -> dict[str, list[tuple[float, float]]]:
    """Classe les points de paroi en extrados / intrados et calcule le Cp.

    `p` est une pression cinématique (m²/s²) : Cp = p / (½ U∞²).

    Le classement se fait par proximité aux deux lignes de la section plutôt
    que par le signe de l'ordonnée : avec de la cambrure et de l'incidence,
    l'intrados repasse au dessus de la corde près du bord de fuite, et un
    critère de signe s'y trompe.

    Les valeurs sont ensuite MOYENNÉES par bande de corde. Le calcul étant
    quasi-2D, chaque abscisse revient une quarantaine de fois — une par maille
    d'envergure — et tracer ces doublons donnerait un nuage épais là où il n'y
    a qu'une courbe, pour un fichier trente fois plus lourd.
    """
    upper_m = [(x / 1000.0, y / 1000.0) for x, y in section["upper"]]
    lower_m = [(x / 1000.0, y / 1000.0) for x, y in section["lower"]]
    chord_m = section["chord_mm"] / 1000.0
    if chord_m <= 0:
        return {"upper": [], "lower": []}

    dynamic = 0.5 * u_inf * u_inf
    upper_points: list[tuple[float, float]] = []
    lower_points: list[tuple[float, float]] = []

    def nearest(polyline, x, y) -> float:
        return min((px - x) ** 2 + (py - y) ** 2 for px, py in polyline)

    for x, y, p in samples:
        cp = p / dynamic
        # Projection sur la corde : l'incidence est déjà dans la géométrie, on
        # ramène donc l'abscisse dans le repère du profil.
        aoa = math.radians(section["aoa_deg"])
        xc = (x * math.cos(aoa) - y * math.sin(aoa)) / chord_m
        if not -0.05 <= xc <= 1.05:
            continue
        if nearest(upper_m, x, y) <= nearest(lower_m, x, y):
            upper_points.append((xc, cp))
        else:
            lower_points.append((xc, cp))

    def average_by_band(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
        if not points:
            return []
        grouped: dict[int, list[tuple[float, float]]] = {}
        for xc, cp in points:
            grouped.setdefault(min(bins - 1, max(0, int(xc * bins))), []).append(
                (xc, cp)
            )
        averaged = [
            (
                sum(x for x, _ in group) / len(group),
                sum(c for _, c in group) / len(group),
            )
            for group in grouped.values()
        ]
        averaged.sort()
        return averaged

    return {
        "upper": average_by_band(upper_points),
        "lower": average_by_band(lower_points),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Figures
# ─────────────────────────────────────────────────────────────────────────────


def read_coefficient_history(case_dir: Path) -> dict[str, list[tuple[float, float]]]:
    """Historique de Cd et Cl le long du calcul, pour juger la convergence."""
    files = sorted(
        p for p in (Path(case_dir) / "postProcessing").rglob("*.dat")
        if p.name.startswith("coefficient") or p.name == "forceCoeffs.dat"
    )
    if not files:
        return {}

    header: list[str] | None = None
    rows: list[list[float]] = []
    for line in files[-1].read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            tokens = stripped.lstrip("#").split()
            if tokens and tokens[0].lower() == "time":
                header = tokens
            continue
        try:
            rows.append([float(v) for v in stripped.split()])
        except ValueError:
            continue
    if not header or not rows:
        return {}

    index = {name: i for i, name in enumerate(header)}
    out: dict[str, list[tuple[float, float]]] = {}
    for key in ("Cd", "Cl"):
        if key in index:
            column = index[key]
            out[key] = [
                (row[0], row[column]) for row in rows if len(row) > column
            ]
    if "Cd" in out and "Cl" in out:
        out["Cl_Cd"] = [
            (t, cl / cd if abs(cd) > 1e-12 else 0.0)
            for (t, cd), (_, cl) in zip(out["Cd"], out["Cl"])
        ]
    return out


def build_figures(
    output: Path,
    history: Sequence[Mapping[str, Any]],
    section: Mapping[str, Any],
    convergence: Mapping[str, list[tuple[float, float]]],
    cp: Mapping[str, list[tuple[float, float]]] | None,
    best_iteration_number: int,
) -> dict[str, str]:
    """Écrit les figures SVG et retourne {nom: chemin relatif}."""
    figures = output / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    produced: dict[str, str] = {}

    def write(name: str, svg: str) -> None:
        (figures / name).write_text(svg, encoding="utf-8")
        produced[name.rsplit(".", 1)[0]] = f"figures/{name}"

    # ── Trajectoire de l'optimisation ────────────────────────────────────
    successes = [r for r in history if r.get("success")]
    if successes:
        def series(key: str, label: str) -> dict:
            points = [
                (float(r["iteration"]), float(r[key]))
                for r in successes
                if isinstance(r.get(key), (int, float))
            ]
            return {"points": points, "label": label, "markers": True}

        cl_cd = series("Cl_Cd", "Cl/Cd")
        best = [p for p in cl_cd["points"] if p[0] == best_iteration_number]
        cl_cd["highlight"] = best
        write(
            "optimization_progress.svg",
            plots.chart(
                [cl_cd], title="Lift-to-drag over the iterations",
                x_label="itération", y_label="Cl / Cd",
            ),
        )
        write(
            "coefficients_progress.svg",
            plots.chart(
                [series("Cd", "Cd"), series("Cl", "Cl")],
                title="Cd and Cl over the iterations",
                x_label="itération", y_label="coefficient", y_zero_line=True,
            ),
        )

    # ── Section ──────────────────────────────────────────────────────────
    write(
        "profile_shape.svg",
        plots.airfoil_outline(
            section["upper"], section["lower"],
            title=(
                f"Optimised section — chord {section['chord_mm']:.0f} mm, "
                f"incidence {section['aoa_deg']:.2f}°"
            ),
        ),
    )

    # ── Convergence du solveur ───────────────────────────────────────────
    if convergence.get("Cd"):
        # Le début du calcul est un transitoire numérique sans intérêt, et son
        # amplitude écrase l'échelle : on n'affiche que la seconde moitié.
        def tail(points):
            return points[len(points) // 2:]

        write(
            "solver_convergence.svg",
            plots.chart(
                [
                    {"points": tail(convergence["Cd"]), "label": "Cd"},
                    {"points": tail(convergence.get("Cl", [])), "label": "Cl"},
                ],
                title="Solver convergence (second half)",
                x_label="solver iteration", y_label="coefficient",
                y_zero_line=True,
            ),
        )

    # ── Distribution de pression ─────────────────────────────────────────
    if cp and (cp.get("upper") or cp.get("lower")):
        write(
            "cp_distribution.svg",
            plots.chart(
                [
                    {"points": cp["upper"], "label": "extrados", "markers": True},
                    {"points": cp["lower"], "label": "intrados", "markers": True},
                ],
                title="Distribution de pression sur le profil",
                x_label="x / corde", y_label="Cp",
                invert_y=True, y_zero_line=True,
            ),
        )
    return produced


def build_comparison_figures(
    output: Path,
    before_section: Mapping[str, Any],
    after_section: Mapping[str, Any],
    before_results: Mapping[str, Any],
    after_results: Mapping[str, Any],
    before_cp: Mapping[str, list[tuple[float, float]]] | None,
    after_cp: Mapping[str, list[tuple[float, float]]] | None,
    regime: str,
) -> dict[str, str]:
    """Figures « avant / après » : sections, performances, distribution de Cp."""
    figures = output / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    produced: dict[str, str] = {}

    def write(name: str, svg: str) -> None:
        (figures / name).write_text(svg, encoding="utf-8")
        produced[name.rsplit(".", 1)[0]] = f"figures/{name}"

    before_panel = {
        **before_section,
        "label": "Seed (start)",
        "caption": (
            f"corde {before_section['chord_mm']:.0f} mm · "
            f"incidence {before_section['aoa_deg']:.2f}° · "
            f"thickness {before_section['thickness']:.3f}"
        ),
    }
    after_panel = {
        **after_section,
        "label": "Optimised design",
        "caption": (
            f"corde {after_section['chord_mm']:.0f} mm · "
            f"incidence {after_section['aoa_deg']:.2f}° · "
            f"thickness {after_section['thickness']:.3f}"
        ),
    }

    write(
        "comparison_sections.svg",
        plots.airfoil_comparison(
            before_panel, after_panel, title="Section: before / after",
        ),
    )
    write(
        "comparison_overlay.svg",
        plots.airfoil_overlay(
            {**before_panel, "label": "seed"},
            {**after_panel, "label": "optimisé"},
            title="The two sections superimposed",
        ),
    )

    groups = [
        {"label": "Lift (Cl)", "before": before_results.get("Cl"),
         "after": after_results.get("Cl"), "better": "higher", "format": ".4f"},
        {"label": "Drag (Cd)", "before": before_results.get("Cd"),
         "after": after_results.get("Cd"), "better": "lower", "format": ".5f"},
        {"label": "Lift-to-drag (Cl/Cd)", "before": before_results.get("Cl_Cd"),
         "after": after_results.get("Cl_Cd"), "better": "higher", "format": ".2f"},
    ]
    write(
        "comparison_performance.svg",
        plots.comparison_bars(
            groups, title=f"Performance — {regime}",
            before_label="seed", after_label="optimisé",
        ),
    )

    if before_cp and after_cp:
        write(
            "comparison_cp.svg",
            plots.chart(
                [
                    {"points": before_cp["upper"], "label": "seed — upper",
                     "color": plots.COLORS[1], "dashed": True},
                    {"points": before_cp["lower"], "label": "seed — lower",
                     "color": plots.COLORS[4], "dashed": True},
                    {"points": after_cp["upper"], "label": "optimised — upper",
                     "color": plots.COLORS[0]},
                    {"points": after_cp["lower"], "label": "optimised — lower",
                     "color": plots.COLORS[2]},
                ],
                title="Pressure distribution: before / after",
                x_label="x / corde", y_label="Cp",
                invert_y=True, y_zero_line=True,
            ),
        )
    return produced


def render_paraview(
    case_dir: Path,
    output: Path,
    u_inf: float,
    rho: float,
    timeout_s: int = 900,
    prefix: str = "",
) -> tuple[list[str], str | None]:
    """Lance ParaView en lot. Retourne (images, message d'échec éventuel).

    `prefix` renomme les images produites : le champ de pression du seed et
    celui du design optimisé cohabitent alors dans le même dossier, avec la
    MÊME échelle de couleurs — sans quoi les comparer visuellement n'aurait
    aucun sens.

    Le rendu préfixé passe par un dossier temporaire : ParaView écrit toujours
    sous les mêmes noms, et renommer après coup écraserait les images déjà
    produites pour l'autre design — la comparaison se retrouverait alors avec
    deux fois la même image, ou une référence vide.
    """
    figures = output / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    destination = figures / "_render_tmp" if prefix else figures
    destination.mkdir(parents=True, exist_ok=True)

    if not shutil.which("pvbatch"):
        return [], (
            "pvbatch not found — install ParaView "
            "(`apt install paraview`) to get the CFD visuals"
        )
    if not (Path(case_dir) / "constant" / "polyMesh").is_dir():
        return [], (
            "the mesh was purged from the archived case "
            "(`execution.keep_case_after_run`): the CFD visuals require "
            "re-running the computation"
        )

    command = ["pvbatch", str(PARAVIEW_SCRIPT), str(case_dir), str(destination),
               str(u_inf), str(rho)]
    # `xvfb-run` fournit l'affichage virtuel que le rendu réclame sur une
    # machine sans serveur X — le cas de tout serveur de calcul.
    if shutil.which("xvfb-run"):
        command = ["xvfb-run", "-a"] + command

    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        return [], f"ParaView did not finish within {timeout_s} s"
    except OSError as exc:
        return [], f"lancement de ParaView impossible : {exc}"

    produced: list[str] = []
    for name in ("pressure_field.png", "velocity_field.png", "streamlines.png"):
        source = destination / name
        if not source.is_file():
            continue
        if prefix:
            source.replace(figures / f"{prefix}{name}")
            produced.append(f"{prefix}{name}")
        else:
            produced.append(name)

    if prefix:
        shutil.rmtree(destination, ignore_errors=True)

    if not produced:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return [], "ParaView n'a produit aucune image : " + " ".join(tail)[:300]
    return produced, None


# ─────────────────────────────────────────────────────────────────────────────
# Lecture physique
# ─────────────────────────────────────────────────────────────────────────────


#: En deçà de cette fraction de l'amplitude autorisée, une valeur de départ est
#: trop proche de zéro pour qu'un pourcentage la commente utilement.
NEGLIGIBLE_BASE_FRACTION = 0.05


def _parameter_delta(start: float, final: float, spec: Mapping[str, Any]) -> str:
    """Écart entre deux valeurs, en pourcentage ou en absolu selon le sens.

    Un pourcentage rapporté à une valeur quasi nulle n'informe pas : sur un
    coefficient CST parti de 0,0013, un déplacement parfaitement ordinaire
    s'affichait « +2037,1 % ». Le lecteur en conclut qu'il s'est passé quelque
    chose d'extraordinaire, alors que le coefficient a simplement traversé
    zéro.

    On rapporte donc l'écart à l'AMPLITUDE autorisée quand la base est trop
    petite pour servir de référence — la même règle que celle qui gouverne le
    budget de variation, et pour la même raison.
    """
    if abs(final - start) < 1e-12:
        return "unchanged"
    try:
        span = abs(float(spec["max"]) - float(spec["min"]))
    except (KeyError, TypeError, ValueError):
        span = 0.0
    if span > 0 and abs(start) < NEGLIGIBLE_BASE_FRACTION * span:
        return f"{final - start:+.4g} ({(final - start) / span * 100:+.0f} % of the range)"
    if start:
        return f"{(final - start) / abs(start) * 100:+.1f} %"
    return f"{final - start:+g}"


def shape_values(design: Mapping[str, Any]) -> dict[str, float]:
    """Valeurs des paramètres, complétées des grandeurs de forme mesurées.

    La lecture physique raisonne en épaisseur et en cambrure, pas en
    coefficients de Bernstein — et elle a raison : « le profil s'est aminci de
    12 % à 9,4 % » se comprend, « A₃ est passé de 0,151 à 0,138 » ne dit rien
    à personne.

    Sur un profil NACA ces deux grandeurs sont des paramètres d'entrée et se
    lisent directement. Sur un profil CST elles n'existent nulle part : il faut
    les MESURER sur la forme reconstruite. Sans cela, tout le commentaire
    physique disparaîtrait du rapport dès qu'on optimise un profil issu d'un
    fichier — c'est-à-dire dans le cas même que la v1.5 rend possible.
    """
    parameters = design.get("parameters") or {}
    values = {
        name: float(spec["value"])
        for name, spec in parameters.items()
        if isinstance(spec, Mapping) and "value" in spec
    }
    if "thickness" in values and "camber" in values:
        return values

    try:
        plan = profile_from_parameters(
            parameters,
            design.get("parameterization"),
            design.get("provenance"),
        )
    except Exception:
        return values

    values.setdefault("thickness", float(plan["thickness"]))
    values.setdefault("camber", float(plan["camber"]))
    return values


def explain_physics(
    initial: Mapping[str, float],
    final: Mapping[str, float],
    first_results: Mapping[str, Any] | None,
    best_results: Mapping[str, Any],
    cp: Mapping[str, list[tuple[float, float]]] | None,
) -> list[str]:
    """Explique, à partir des chiffres, pourquoi la forme retenue est meilleure.

    Rien n'est inventé : chaque phrase découle d'un écart mesuré entre le point
    de départ et le point d'arrivée. Les paramètres qui n'ont pas bougé ne sont
    pas commentés.
    """
    notes: list[str] = []

    def moved(name: str, threshold: float = 1e-9) -> float | None:
        if name not in initial or name not in final:
            return None
        delta = float(final[name]) - float(initial[name])
        return delta if abs(delta) > threshold else None

    d_aoa = moved("aoa", 0.05)
    if d_aoa is not None:
        direction = "raised" if d_aoa > 0 else "lowered"
        notes.append(
            f"**Incidence {direction} by {abs(d_aoa):.2f}°** "
            f"({float(initial['aoa']):.2f}° → {float(final['aoa']):.2f}°). "
            "This is the most direct lever on lift: tilting the profile "
            "deflects more flow downwards, and the reaction to that deflection "
            "*is* the lift. Induced drag grows roughly as the square of lift, "
            "so lift-to-drag passes through a maximum — typically between 4° "
            "and 6° for a cambered profile — then collapses at stall. "
            + (
                f"The value retained, {float(final['aoa']):.2f}°, falls in "
                "that range: the search found the top of the curve."
                if 3.0 <= float(final["aoa"]) <= 7.0
                else "The value retained stays below that range, a sign that "
                "the iteration budget or the bounds limited the progress."
            )
        )

    d_camber = moved("camber", 1e-4)
    if d_camber is not None:
        direction = "increased" if d_camber > 0 else "reduced"
        notes.append(
            f"**Camber {direction}** ({float(initial['camber']):.4f} → "
            f"{float(final['camber']):.4f}). Camber shifts the whole lift "
            "curve: a cambered profile already lifts at zero incidence. It is "
            "paid for in form drag and in pitching moment, hence the existence "
            "of an optimum rather than endless growth."
        )

    d_thickness = moved("thickness", 1e-4)
    if d_thickness is not None:
        if d_thickness < 0:
            notes.append(
                f"**Profile thinned** ({float(initial['thickness']):.4f} → "
                f"{float(final['thickness']):.4f} relative thickness). A "
                "thinner profile disturbs the flow less and drags less. The "
                "counterpart is structural — less inertia, so less stiffness — "
                "and a more abrupt stall, since a sharper leading edge copes "
                "badly with high incidence."
            )
        else:
            notes.append(
                f"**Profile thickened** ({float(initial['thickness']):.4f} → "
                f"{float(final['thickness']):.4f}). Thickness normally costs "
                "drag; that it grows here signals it is serving something "
                "else — a rounder leading edge delays separation and lets "
                "incidence rise."
            )

    d_chord = moved("chord", 0.5)
    if d_chord is not None:
        notes.append(
            f"**Chord raised to {float(final['chord']):.1f} mm** (from "
            f"{float(initial['chord']):.1f}). It acts in two ways: the "
            "Reynolds number rises, which slightly lowers the friction "
            "coefficient, and the reference area changes — it is recomputed at "
            "every iteration, without which comparing the coefficients would "
            "mean nothing."
        )

    # Lecture des coefficients eux-mêmes.
    if first_results:
        cl0, cl1 = first_results.get("Cl"), best_results.get("Cl")
        cd0, cd1 = first_results.get("Cd"), best_results.get("Cd")
        if all(isinstance(v, (int, float)) for v in (cl0, cl1, cd0, cd1)):
            gain_cl = (cl1 - cl0) / abs(cl0) * 100 if cl0 else 0.0
            gain_cd = (cd1 - cd0) / abs(cd0) * 100 if cd0 else 0.0
            if gain_cd < 0 and gain_cl > 0:
                notes.append(
                    f"**The trade-off in numbers**: lift gains "
                    f"{gain_cl:+.0f} % *and* drag falls by "
                    f"{abs(gain_cd):.0f} %. Both terms improve at once — a "
                    "favourable case, which here comes from the reference area "
                    "following the chord."
                )
            elif gain_cl > abs(gain_cd):
                notes.append(
                    f"**The trade-off in numbers**: lift gains "
                    f"{gain_cl:+.0f} % for only {gain_cd:+.0f} % of drag. That "
                    "is exactly what a lift-to-drag optimisation seeks — not to "
                    "drag less, but to lift a great deal more for a modest drag "
                    "penalty."
                )
            else:
                notes.append(
                    f"**The trade-off in numbers**: Cl {gain_cl:+.0f} %, "
                    f"Cd {gain_cd:+.0f} %."
                )

    if cp and cp.get("upper"):
        peak = min(cp["upper"], key=lambda point: point[1])
        notes.append(
            f"**What the pressure distribution shows**: the suction peak "
            f"reaches Cp = {peak[1]:.2f} at {peak[0] * 100:.0f} % of chord. "
            "The upper surface does the work — the suction there pulls the "
            "profile upwards, and it weighs far more than the lower-surface "
            "overpressure. A very deep peak followed by an abrupt recovery "
            "would signal separation; a gradual recovery, as here, indicates "
            "flow that is still attached."
        )

    return notes


# ─────────────────────────────────────────────────────────────────────────────
# Rapport
# ─────────────────────────────────────────────────────────────────────────────


def _fmt(value: Any, spec: str = ".5f") -> str:
    return format(value, spec) if isinstance(value, (int, float)) else "—"


def _cell(text: Any) -> str:
    """Neutralise ce qui casserait une cellule de tableau Markdown.

    Les messages d'erreur contiennent des barres verticales — « checkMesh :
    1 contrôle en échec | skewness 4.03 » — qui scindent la ligne en colonnes
    surnuméraires et désalignent tout le tableau.
    """
    return str(text).replace("|", "∕").replace("\n", " ")


def _comparison_section(
    comparison: Mapping[str, Any], figures: Mapping[str, str]
) -> list[str]:
    """La section « avant / après », faite pour se lire d'un coup d'œil."""
    lines: list[str] = ["## Before / after", ""]
    regime = comparison.get("regime", "")
    before = comparison["before"]
    after = comparison["after"]

    lines.append(
        f"The starting seed against the retained design, both measured "
        f"**in the same CFD regime** ({regime}) — comparing a fine mesh "
        f"against an exploration mesh would inflate the gain without it being "
        f"real."
    )
    lines.append("")

    if "comparison_performance" in figures:
        lines.append(f"![Performance before / after]({figures['comparison_performance']})")
        lines.append("")

    lines.append("| | seed | optimised | change |")
    lines.append("|---|---|---|---|")
    for label, key, spec, better in (
        ("Lift Cl", "Cl", ".4f", "higher"),
        ("Drag Cd", "Cd", ".5f", "lower"),
        ("Lift-to-drag Cl/Cd", "Cl_Cd", ".2f", "higher"),
    ):
        b, a = before.get(key), after.get(key)
        if not all(isinstance(v, (int, float)) for v in (b, a)):
            continue
        change = (a - b) / abs(b) * 100 if b else 0.0
        improved = (a > b) if better == "higher" else (a < b)
        lines.append(
            f"| **{label}** | {format(b, spec)} | **{format(a, spec)}** "
            f"| {change:+.1f} % {'✓' if improved else '✗'} |"
        )
    lines.append("")

    if "comparison_sections" in figures:
        lines.append(f"![Sections before / after]({figures['comparison_sections']})")
        lines.append("")
        lines.append(
            "Both sections are drawn at the **same scale**: each fitted to its "
            "own frame, they would look identical, and both the chord "
            "difference and the incidence would go unnoticed."
        )
        lines.append("")
    if "comparison_overlay" in figures:
        lines.append(f"![Superimposed sections]({figures['comparison_overlay']})")
        lines.append("")

    if "comparison_cp" in figures:
        lines.append("### Pressure, before and after")
        lines.append("")
        lines.append(f"![Cp before / after]({figures['comparison_cp']})")
        lines.append("")
        lines.append(
            "The area between the upper-surface curve and the lower-surface "
            "curve *is* the lift. The optimised design deepens its "
            "upper-surface suction and spreads it along the chord: that is "
            "where the extra lift is won."
        )
        lines.append("")

    seed_images = list(comparison.get("images", []))
    if seed_images:
        lines.append("### The fields, side by side")
        lines.append("")
        for name in ("pressure_field.png", "streamlines.png"):
            seed_name = f"seed_{name}"
            if seed_name not in seed_images:
                continue
            lines.append("<!-- side-by-side -->")
            lines.append(f"![Seed — {name.split('.')[0]}](figures/{seed_name})")
            lines.append(f"![Optimised — {name.split('.')[0]}](figures/{name})")
            lines.append("<!-- /side-by-side -->")
            lines.append("")
        lines.append(
            "Same colour scale on both sides — that is the condition for the "
            "comparison to mean anything. The upper-surface suction, in blue, "
            "is markedly stronger and more extensive after optimisation."
        )
        lines.append("")
    elif comparison.get("visuals_error"):
        lines.append(f"> Seed contours not produced: {comparison['visuals_error']}")
        lines.append("")

    return lines


def build_report(
    design: Mapping[str, Any],
    initial_design: Mapping[str, Any] | None,
    record: Mapping[str, Any],
    results: Mapping[str, Any],
    fast_results: Mapping[str, Any] | None,
    history: Sequence[Mapping[str, Any]],
    section: Mapping[str, Any],
    figures: Mapping[str, str],
    images: Sequence[str],
    notes: Sequence[str],
    has_step: bool,
    has_case: bool,
    source: Path,
    visuals_error: str | None,
    comparison: Mapping[str, Any] | None = None,
) -> str:
    parameters = design["parameters"]
    initial = (initial_design or {}).get("parameters", {})
    lines: list[str] = []

    lines.append(f"# Optimised design — `{design.get('design_id', 'unnamed')}`")
    lines.append("")
    successes = sum(1 for r in history if r.get("success"))
    failures = len(history) - successes
    # Le français accordait « réussie(s) » et « échouée(s) » ; l'anglais n'a
    # pas d'accord sur le participe, et les « s » conditionnels qui restaient
    # produisaient « 8 succeededs ».
    lines.append(
        f"Best of the **{len(history)} iterations** of run "
        f"`{source.parent.name}` ({successes} succeeded, {failures} failed), "
        f"selected on objective "
        f"`{(design.get('objectives') or {}).get('primary', '?')}`."
    )
    lines.append("")

    # ── Performances ──────────────────────────────────────────────────────
    lines.append("## Performance")
    lines.append("")
    first = next(
        (r for r in history
         if r.get("success") and isinstance(r.get("Cl_Cd"), (int, float))),
        None,
    )
    lines.append("| | Cd | Cl | Cl/Cd |")
    lines.append("|---|---|---|---|")
    if first:
        lines.append(
            f"| Start (iteration {first['iteration']}) | {_fmt(first.get('Cd'))} "
            f"| {_fmt(first.get('Cl'))} | {_fmt(first.get('Cl_Cd'), '.2f')} |"
        )
    lines.append(
        f"| **Optimised (iteration {record.get('iteration')})** "
        f"| **{_fmt(results.get('Cd'))}** | **{_fmt(results.get('Cl'))}** "
        f"| **{_fmt(results.get('Cl_Cd'), '.2f')}** |"
    )
    if fast_results:
        lines.append(
            f"| *— measured in exploration* | *{_fmt(fast_results.get('Cd'))}* "
            f"| *{_fmt(fast_results.get('Cl'))}* "
            f"| *{_fmt(fast_results.get('Cl_Cd'), '.2f')}* |"
        )
    if first and isinstance(first.get("Cl_Cd"), (int, float)) and first["Cl_Cd"]:
        reference = fast_results or results
        if isinstance(reference.get("Cl_Cd"), (int, float)):
            gain = (reference["Cl_Cd"] - first["Cl_Cd"]) / abs(first["Cl_Cd"]) * 100
            lines.append("")
            # Le gain compare TOUJOURS deux mesures du même régime — sans quoi
            # il serait gonflé. Mais quand le design a été requalifié au réglage
            # fin, la ligne comparée n'est plus celle en gras : c'est celle en
            # italique. Le lecteur, lui, rapporte naturellement le pourcentage
            # aux chiffres mis en avant, et se trompe de deux fois l'écart. Il
            # faut donc nommer le régime, pas seulement l'employer correctement.
            if fast_results:
                lines.append(
                    f"**Lift-to-drag gain: {gain:+.1f} %** — between the two "
                    f"**exploration** measurements: the starting row and the "
                    f"one in italics. At the fine settings, the "
                    f"constant-regime comparison is given further down, under "
                    f"“Before / after”; it is lower, because a coarse mesh "
                    f"exaggerates differences."
                )
            else:
                lines.append(f"**Lift-to-drag gain: {gain:+.1f} %**")
    lines.append("")

    mesh = results.get("mesh") or {}
    lines.append(
        f"Mesh: {mesh.get('n_cells', '?')} cells, non-orthogonality "
        f"{_fmt(mesh.get('max_non_orthogonality'), '.1f')}, skewness "
        f"{_fmt(mesh.get('max_skewness'), '.2f')}. Coefficients averaged over "
        f"{results.get('averaging_window', '?')} iterations, relative standard deviation "
        f"{_fmt(results.get('Cd_rel_std'), '.1e')} on Cd — "
        f"{'stabilised' if results.get('coefficients_stable') else '**encore instables**'}."
    )
    lines.append("")

    # ── Paramètres ────────────────────────────────────────────────────────
    lines.append("## Parameters: start → finish")
    lines.append("")
    lines.append("| parameter | start | finish | change | bounds |")
    lines.append("|---|---|---|---|---|")
    for name, spec in parameters.items():
        final_value = float(spec["value"])
        start = initial.get(name)
        start_value = float(start["value"]) if start else None
        if start_value is None:
            delta = "—"
            start_text = "—"
        else:
            start_text = f"{start_value:g}"
            delta = _parameter_delta(start_value, final_value, spec)
        lines.append(
            f"| `{name}` | {start_text} | **{final_value:g}** {spec.get('unit', '')} "
            f"| {delta} | {float(spec['min']):g} … {float(spec['max']):g} |"
        )
    lines.append("")

    # ── Avant / après ─────────────────────────────────────────────────────
    if comparison:
        lines.extend(_comparison_section(comparison, figures))
    elif "profile_shape" in figures:
        lines.append(f"![Section du profil]({figures['profile_shape']})")
        lines.append("")

    # ── Pourquoi c'est mieux ──────────────────────────────────────────────
    if notes:
        lines.append("## Why this shape is better")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    # ── Trajectoire ───────────────────────────────────────────────────────
    lines.append("## Course of the optimisation")
    lines.append("")
    for key, caption in (
        ("optimization_progress", "Lift-to-drag over the iterations"),
        ("coefficients_progress", "Cd and Cl over the iterations"),
    ):
        if key in figures:
            lines.append(f"![{caption}]({figures[key]})")
            lines.append("")

    lines.append("| iteration | Cd | Cl | Cl/Cd | status |")
    lines.append("|---|---|---|---|---|")
    for entry in history:
        marker = " ⭐" if entry.get("iteration") == record.get("iteration") else ""
        status = "OK" if entry.get("success") else (
            f"failed — {_cell(entry.get('error_message', ''))[:90]}"
        )
        lines.append(
            f"| {entry.get('iteration')}{marker} | {_fmt(entry.get('Cd'))} "
            f"| {_fmt(entry.get('Cl'))} | {_fmt(entry.get('Cl_Cd'), '.2f')} "
            f"| {status} |"
        )
    lines.append("")

    # ── Visuels CFD ───────────────────────────────────────────────────────
    lines.append("## The flow")
    lines.append("")
    if "cp_distribution" in figures:
        lines.append(f"![Cp distribution]({figures['cp_distribution']})")
        lines.append("")
        lines.append(
            "Cp axis inverted, as convention requires: the upper curve is the "
            "upper surface, in suction. The area between the two curves is the "
            "lift."
        )
        lines.append("")

    titles = {
        "pressure_field.png": "Pressure field around the profile",
        "streamlines.png": "Streamlines",
        "velocity_field.png": "Velocity magnitude and wake",
    }
    captions = {
        "pressure_field.png": (
            "**Pressure field.** The red under the leading edge is the "
            "stagnation point, where the flow comes to rest (Cp = +1). The blue "
            "above is the suction that lifts the profile."
        ),
        "streamlines.png": (
            "**Streamlines**, coloured by velocity. The acceleration over the "
            "upper surface is the counterpart of the suction: this is "
            "Bernoulli's theorem, where accelerating fluid sees its pressure "
            "drop."
        ),
        "velocity_field.png": (
            "**Velocity magnitude.** The wake can be read behind the trailing "
            "edge; the thinner it is, the less the profile drags."
        ),
    }
    for image in images:
        lines.append(f"![{titles.get(image, image)}](figures/{image})")
        lines.append("")
        if image in captions:
            lines.append(captions[image])
            lines.append("")

    if visuals_error:
        lines.append(f"> CFD visuals not produced: {visuals_error}")
        lines.append("")

    if "solver_convergence" in figures:
        lines.append("### Solver convergence")
        lines.append("")
        lines.append(f"![Convergence]({figures['solver_convergence']})")
        lines.append("")
        lines.append(
            "Flat curves towards the end are the condition for the "
            "coefficients to mean anything."
        )
        lines.append("")

    # ── Réserve sur les chiffres ──────────────────────────────────────────
    lines.append("## What these numbers are worth")
    lines.append("")
    lines.append(
        "The `kOmegaSST` turbulence model assumes a turbulent boundary layer "
        "from the leading edge. At Re ≈ 4 × 10⁵ a good part of the upper "
        "surface is still laminar: **drag is overestimated**, by a factor that "
        "can approach 2. These values rank shapes against each other correctly "
        "— which is what an optimisation requires — but do not constitute a "
        "prediction of absolute drag. For a publishable figure you need a model "
        "with laminar-turbulent transition, or a wind tunnel."
    )
    lines.append("")

    # ── Contenu et mode d'emploi ──────────────────────────────────────────
    lines.append("## Folder contents")
    lines.append("")
    lines.append("| file | what |")
    lines.append("|---|---|")
    lines.append("| `geometry.stl` | the geometry, **in metres**, as simulated |")
    if has_step:
        lines.append("| `geometry.step` | the same, as a CAD solid |")
    lines.append("| `profile_section.csv` | 2D section in millimetres |")
    lines.append("| `profile_section.dat` | same section in airfoil format |")
    lines.append(
        "| `profile_chord.dat` | profile **straightened**, unit chord — "
        "for XFOIL / XFLR5 |"
    )
    lines.append("| `design_params.yaml` | the exact parameters, replayable |")
    lines.append("| `results.json` | the coefficients |")
    lines.append("| `report.html` | this report, self-contained, for a browser |")
    lines.append("| `FUSION_RETURN.md` | how to carry this design back into CAD |")
    lines.append("| `rebuild_in_fusion.py` | CAD script that redraws the profile |")
    lines.append("| `figures/` | curves and images |")
    if has_case:
        lines.append("| `cfd/` | OpenFOAM case: mesh and final fields |")
    lines.append("| `logs/` | logs of each step |")
    lines.append("")

    # ── Retour vers Fusion (§5 du document maître) ────────────────────────
    lines.append("## Continuing this design in CAD")
    lines.append("")
    lines.append(
        "An optimisation that only returns an STL is a design dead end: a "
        "faceted solid of several hundred faces can neither be filleted "
        "properly nor re-dimensioned. Several routes bring this shape back "
        "into editable CAD, detailed in **`FUSION_RETURN.md`**."
    )
    lines.append("")
    lines.append("| route | what you get | when to choose it |")
    lines.append("|---|---|---|")
    if has_step:
        lines.append(
            "| **0. Open `geometry.step`** | native solid, one double-click | "
            "the simplest — works in FreeCAD as well as Fusion |"
        )
    lines.append(
        "| **1. Replay the parameters** | native model, full history | as soon "
        "as a starting model exists — the only genuinely parametric route |"
    )
    lines.append(
        "| **2. `rebuild_in_fusion.py` script** | sketch + extrusion, hands "
        "off | no starting model; nothing to locate or convert |"
    )
    lines.append(
        "| **3. Import `profile_section.csv`** | sketch drawn by hand | to stay "
        "in control, or work in another CAD package |"
    )
    lines.append("")
    lines.append(
        "```bash\n"
        "# route 1: replay the parameters in Fusion\n"
        "cp design_params.yaml <project>/configs/design_params.yaml\n"
        "# then Utilities → ADD-INS → Scripts → fusion/parametric_driver.py\n"
        "\n"
        "# route 2: standalone script\n"
        "# Utilities → ADD-INS → Scripts → + → rebuild_in_fusion.py → Run\n"
        "```"
    )
    lines.append("")
    lines.append(
        "**Incidence is already in the coordinates** of the exported section: "
        "this is the geometry that was actually simulated. If a downstream "
        "setup applies an incidence of its own, it would be counted twice."
    )
    lines.append("")
    if not has_step:
        lines.append(
            "There is no STEP file in this folder: the geometry was produced by "
            "the internal computer, which writes an STL directly. It can also "
            "write a STEP, but only when the optional CAD kernel is installed "
            "(`pip install -r requirements-cad.txt`). Routes 1 and 2 produce "
            "one as well."
        )
        lines.append("")

    lines.append("## Opening the files")
    lines.append("")
    lines.append("```bash")
    lines.append("# the geometry (STL in metres)")
    lines.append("paraview geometry.stl")
    lines.append("")
    if has_case:
        lines.append("# the CFD fields")
        lines.append("paraview cfd/best_design.foam")
        lines.append("")
        lines.append("# regenerate the visuals after a change")
        lines.append("xvfb-run -a pvbatch paraview_render.py cfd figures 20 1.225")
        lines.append("")
    lines.append("# resume the optimisation from this design")
    lines.append("cp design_params.yaml configs/design_params.yaml")
    lines.append("python3 scripts/run_loop.py --max-iterations 20 \\")
    lines.append("    --cfd-settings configs/cfd_settings_fast.yaml")
    lines.append("```")
    lines.append("")
    if has_case:
        lines.append(
            "In ParaView, the final time step carries `U` (velocity), `p` "
            "(**kinematic** pressure, in m²/s² — multiply by ρ = 1.225 kg/m³ "
            "for pascals), `k`, `omega` and `nut`. The `wing` patch is the wing "
            "surface."
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"Exported on {datetime.now(timezone.utc).strftime('%d/%m/%Y at %H:%M UTC')} "
        f"by `scripts/export_best.py`."
    )
    return "\n".join(lines) + "\n"


def markdown_to_html(markdown: str, output: Path, title: str) -> str:
    """Rend le rapport en HTML autonome : SVG intégrés, images en base64.

    Un fichier unique s'envoie et s'archive sans se casser, là où un HTML qui
    référence un dossier de figures perd ses images au premier déplacement.
    Le convertisseur ne couvre que ce que `build_report` produit — titres,
    tableaux, listes, images, blocs de code, gras — et n'a pas vocation à
    traiter du Markdown quelconque.
    """
    def inline_image(match: re.Match) -> str:
        alt, src = match.group(1), match.group(2)
        path = output / src
        if not path.is_file():
            return f'<p><em>{alt} (image absente)</em></p>'
        if path.suffix == ".svg":
            svg = path.read_text(encoding="utf-8")
            svg = re.sub(r'^<\?xml[^>]*\?>\s*', "", svg)
            return f'<figure>{svg}<figcaption>{alt}</figcaption></figure>'
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return (
            f'<figure><img src="data:image/png;base64,{data}" alt="{alt}"/>'
            f'<figcaption>{alt}</figcaption></figure>'
        )

    html: list[str] = []
    in_table = False
    in_code = False
    in_row = False

    for line in markdown.splitlines():
        # Les marqueurs de côte à côte sont des commentaires Markdown : ils
        # disparaissent chez un lecteur qui ne les connaît pas, et les images
        # s'y empilent simplement au lieu de se juxtaposer.
        if line.strip() == "<!-- side-by-side -->":
            html.append('<div class="side-by-side">')
            in_row = True
            continue
        if line.strip() == "<!-- /side-by-side -->":
            html.append("</div>")
            in_row = False
            continue

        if line.startswith("```"):
            html.append("</pre>" if in_code else "<pre>")
            in_code = not in_code
            continue
        if in_code:
            html.append(
                line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )
            continue

        image = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)", line.strip())
        if image:
            if in_table:
                html.append("</table>")
                in_table = False
            html.append(inline_image(image))
            continue

        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            if not in_table:
                html.append("<table>")
                in_table = True
                tag = "th"
            else:
                tag = "td"
            row = "".join(f"<{tag}>{_inline(c)}</{tag}>" for c in cells)
            html.append(f"<tr>{row}</tr>")
            continue
        if in_table:
            html.append("</table>")
            in_table = False

        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            level = len(stripped) - len(stripped.lstrip("#"))
            html.append(f"<h{level}>{_inline(stripped[level:].strip())}</h{level}>")
        elif stripped.startswith("> "):
            html.append(f"<blockquote>{_inline(stripped[2:])}</blockquote>")
        elif stripped.startswith("- "):
            html.append(f"<ul><li>{_inline(stripped[2:])}</li></ul>")
        elif re.match(r"^\d+\.\s", stripped):
            html.append(f"<ol><li>{_inline(stripped.split('.', 1)[1].strip())}</li></ol>")
        elif stripped == "---":
            html.append("<hr/>")
        else:
            html.append(f"<p>{_inline(stripped)}</p>")

    if in_table:
        html.append("</table>")

    body = "\n".join(html)
    # Listes consécutives fusionnées, pour ne pas empiler les puces isolées.
    body = body.replace("</ul>\n<ul>", "\n").replace("</ol>\n<ol>", "\n")

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<style>
  body {{ max-width: 900px; margin: 2.5rem auto; padding: 0 1.2rem;
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
         line-height: 1.65; color: #1c1c1c; background: #fff; }}
  h1 {{ font-size: 1.85rem; border-bottom: 2px solid #1f4e8c; padding-bottom: .4rem; }}
  h2 {{ font-size: 1.35rem; margin-top: 2.4rem; color: #1f4e8c; }}
  h3 {{ font-size: 1.1rem; margin-top: 1.8rem; }}
  table {{ border-collapse: collapse; width: 100%; margin: 1.1rem 0;
           font-size: .93rem; }}
  th, td {{ border: 1px solid #d8d8d8; padding: .45rem .7rem; text-align: left; }}
  th {{ background: #f2f5f9; font-weight: 600; }}
  tr:nth-child(even) td {{ background: #fafafa; }}
  pre {{ background: #f6f8fa; padding: .9rem 1.1rem; border-radius: 6px;
         overflow-x: auto; font-size: .87rem; line-height: 1.45; }}
  code {{ background: #f0f2f5; padding: .1rem .35rem; border-radius: 3px;
          font-size: .9em; }}
  pre code {{ background: none; padding: 0; }}
  figure {{ margin: 1.6rem 0; text-align: center; }}
  figure svg, figure img {{ max-width: 100%; height: auto;
                            border: 1px solid #e4e4e4; border-radius: 4px; }}
  figcaption {{ font-size: .85rem; color: #666; margin-top: .5rem; }}
  .side-by-side {{ display: flex; gap: 1rem; align-items: flex-start;
                   flex-wrap: wrap; margin: 1.6rem 0; }}
  .side-by-side figure {{ flex: 1 1 380px; margin: 0; }}
  .side-by-side figcaption {{ font-weight: 600; color: #444; }}
  blockquote {{ border-left: 4px solid #c1440e; margin: 1.2rem 0;
                padding: .3rem 1rem; background: #fdf6f3; color: #444; }}
  li {{ margin: .45rem 0; }}
  hr {{ border: none; border-top: 1px solid #e0e0e0; margin: 2.5rem 0; }}
</style></head><body>
{body}
</body></html>
"""


def _inline(text: str) -> str:
    """Gras, code et échappement — le strict nécessaire pour ce rapport."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<em>\1</em>", text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    return text


# ─────────────────────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────────────────────


def resolve_baseline(
    iterations_root: Path,
    history: Sequence[Mapping[str, Any]],
    baseline_dir: Path | None,
    qualified: bool,
) -> tuple[Path | None, str]:
    """Choisit la référence « avant » et dit dans quel régime elle est mesurée.

    Comparer le seed mesuré en exploration au design optimisé mesuré au réglage
    fin gonflerait artificiellement le gain : deux régimes différents ne se
    comparent pas. Trois cas :

    - `baseline_dir` fourni : c'est au concepteur d'avoir aligné les régimes,
      on le suit ;
    - sinon, la première itération réussie de la série, et la comparaison se
      fait alors sur les chiffres D'EXPLORATION des deux côtés, y compris pour
      le design optimisé ;
    - rien d'exploitable : pas de comparaison.
    """
    if baseline_dir is not None:
        path = Path(baseline_dir)
        if not (path / "results.json").is_file():
            raise ExportError(f"reference without results.json: {path}")
        return path, "fine settings"

    first = next((r for r in history if r.get("success")), None)
    if first is None:
        return None, ""
    path = iteration_dir(iterations_root, first["iteration"])
    if not (path / "results.json").is_file():
        return None, ""
    return path, ("exploration" if qualified else "same regime")


def export_best(
    iterations_root: Path,
    output: Path | None = None,
    qualified_dir: Path | None = None,
    include_case: bool = True,
    visuals: bool = True,
    cfd_settings_path: Path | None = None,
    baseline_dir: Path | None = None,
    compare: bool = True,
) -> dict:
    """Assemble le dossier livrable. Retourne un résumé de ce qui a été produit."""
    iterations_root = Path(iterations_root)
    output = Path(output) if output else run_folder() / "best_design"

    record = best_iteration(iterations_root)
    source = iteration_dir(iterations_root, record["iteration"])
    if not source.is_dir():
        raise ExportError(f"iteration folder not found: {source}")

    primary = Path(qualified_dir) if qualified_dir else source
    if not primary.is_dir():
        raise ExportError(f"dossier de requalification introuvable : {primary}")

    design = load_yaml(primary / "design_params.yaml")
    if qualified_dir:
        _check_same_design(design, load_yaml(source / "design_params.yaml"))

    history = mp.history(iterations_root)
    initial_design = None
    if history:
        first_dir = iteration_dir(iterations_root, history[0]["iteration"])
        if (first_dir / "design_params.yaml").is_file():
            initial_design = load_yaml(first_dir / "design_params.yaml")

    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)

    copied: list[str] = []

    def copy(src: Path, name: str) -> bool:
        if not src.is_file():
            return False
        (output / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, output / name)
        copied.append(name)
        return True

    copy(primary / "geometry.stl", "geometry.stl")
    has_step = copy(primary / "geometry.step", "geometry.step")
    copy(primary / "design_params.yaml", "design_params.yaml")
    copy(primary / "results.json", "results.json")
    copy(primary / "iteration.json", "iteration.json")
    copy(primary / "fusion_status.json", "geometry_status.json")
    copy(iterations_root / "optimization_summary.json", "optimization_summary.json")
    if PARAVIEW_SCRIPT.is_file():
        copy(PARAVIEW_SCRIPT, "paraview_render.py")

    fast_results = None
    if qualified_dir and (source / "results.json").is_file():
        copy(source / "results.json", "results_exploration.json")
        fast_results = json.loads((source / "results.json").read_text(encoding="utf-8"))

    logs = primary / "logs"
    if logs.is_dir():
        shutil.copytree(logs, output / "logs")
        copied.append("logs/")

    has_case = False
    case_source = primary / "cfd"
    if include_case and case_source.is_dir():
        shutil.copytree(case_source, output / "cfd",
                        ignore=shutil.ignore_patterns("processor*"))
        (output / "cfd" / "best_design.foam").write_text("", encoding="utf-8")
        copied.append("cfd/")
        has_case = True

    results = json.loads((primary / "results.json").read_text(encoding="utf-8"))
    section = write_profile_section(design, output)
    copied += ["profile_section.csv", "profile_section.dat"]

    # Conditions d'écoulement, pour l'adimensionnement des figures.
    u_inf, rho = 20.0, 1.225
    settings_path = cfd_settings_path or (REPO_ROOT / "configs" / "cfd_settings.yaml")
    try:
        flow = load_yaml(settings_path).get("flow", {})
        u_inf = float(flow.get("velocity_ms", u_inf))
        rho = float(flow.get("rho_kg_m3", rho))
    except Exception:
        pass

    convergence = read_coefficient_history(case_source) if case_source.is_dir() else {}
    samples = sample_wing_pressure(output / "cfd") if has_case else None
    cp = cp_distribution(samples, section, u_inf) if samples else None

    figures = build_figures(
        output, history, section, convergence, cp, int(record["iteration"])
    )

    images: list[str] = []
    visuals_error: str | None = None
    if visuals and has_case:
        images, visuals_error = render_paraview(output / "cfd", output, u_inf, rho)
    elif visuals:
        visuals_error = "OpenFOAM case missing from the exported folder"

    # ── Comparaison avant / après ────────────────────────────────────────
    comparison: dict[str, Any] = {}
    if compare:
        baseline, regime = resolve_baseline(
            iterations_root, history, baseline_dir, bool(qualified_dir)
        )
        if baseline is not None:
            comparison = _build_comparison(
                output, baseline, regime, design, section, results, fast_results,
                u_inf, rho, visuals, cfd_settings_path,
            )
        figures.update(comparison.get("figures", {}))

    # Les coefficients cités dans la lecture physique doivent venir du MÊME
    # régime des deux côtés. Sans cela, on compare un Cd d'exploration à un Cd
    # de réglage fin, et l'on peut annoncer une traînée en baisse là où elle
    # augmente — c'est arrivé.
    if comparison:
        first_results = comparison["before"]
        compared_results = comparison["after"]
    else:
        first_results = next(
            (r for r in history
             if r.get("success") and isinstance(r.get("Cl"), (int, float))),
            None,
        )
        compared_results = results

    notes = explain_physics(
        shape_values(initial_design or design),
        shape_values(design),
        first_results, compared_results, cp,
    )

    # Le chemin de retour vers Fusion est écrit AVANT le rapport : celui-ci y
    # renvoie, et un lien vers un fichier absent serait pire que pas de lien.
    from scripts.fusion_return import write_fusion_return

    backend_used = str(
        (record.get("geometry") or {}).get("backend")
        or record.get("geometry_backend")
        or "internal"
    )
    profile_source = (design.get("provenance") or {}).get("source")
    for written in write_fusion_return(
        output, design, section, has_step, backend_used, profile_source
    ):
        copied.append(written.name)

    report = build_report(
        design, initial_design, record, results, fast_results, history, section,
        figures, images, notes, has_step, has_case, source, visuals_error,
        comparison=comparison or None,
    )
    (output / "README.md").write_text(report, encoding="utf-8")
    (output / "report.html").write_text(
        markdown_to_html(report, output,
                         f"Optimised design — {design.get('design_id', '')}"),
        encoding="utf-8",
    )
    copied += ["README.md", "report.html"]

    return {
        "output": str(output.resolve()),
        "iteration": record["iteration"],
        "source": str(source),
        "qualified_from": str(primary) if qualified_dir else None,
        "objective": record.get("objective"),
        "Cd": results.get("Cd"),
        "Cl": results.get("Cl"),
        "Cl_Cd": results.get("Cl_Cd"),
        "parameters": {
            name: float(spec["value"]) for name, spec in design["parameters"].items()
        },
        "has_step": has_step,
        "has_case": has_case,
        "figures": sorted(figures.values()),
        "images": images + list(comparison.get("images", [])),
        "visuals_error": visuals_error,
        "comparison": {
            "baseline": comparison.get("baseline"),
            "regime": comparison.get("regime"),
            "before": comparison.get("before", {}).get("Cl_Cd"),
            "after": comparison.get("after", {}).get("Cl_Cd"),
            "visuals_error": comparison.get("visuals_error"),
        } if comparison else None,
        "files": copied,
        "size_bytes": sum(p.stat().st_size for p in output.rglob("*") if p.is_file()),
    }


def _build_comparison(
    output: Path,
    baseline: Path,
    regime: str,
    design: Mapping[str, Any],
    section: Mapping[str, Any],
    results: Mapping[str, Any],
    fast_results: Mapping[str, Any] | None,
    u_inf: float,
    rho: float,
    visuals: bool,
    cfd_settings_path: Path | None,
) -> dict:
    """Assemble tout ce qui oppose le seed au design optimisé."""
    baseline_design = load_yaml(baseline / "design_params.yaml")
    baseline_results = json.loads(
        (baseline / "results.json").read_text(encoding="utf-8")
    )

    comparison_dir = output / "comparison"
    comparison_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(baseline / "results.json", comparison_dir / "seed_results.json")
    shutil.copyfile(
        baseline / "design_params.yaml", comparison_dir / "seed_design_params.yaml"
    )
    if (baseline / "geometry.stl").is_file():
        shutil.copyfile(baseline / "geometry.stl", comparison_dir / "seed_geometry.stl")

    baseline_section = write_profile_section(baseline_design, comparison_dir)

    # Les deux côtés doivent être mesurés dans le MÊME régime. Quand le design
    # optimisé a été requalifié au réglage fin alors que la référence vient de
    # l'exploration, ce sont les chiffres d'exploration des deux côtés qui
    # servent à la comparaison — sinon le gain affiché mélangerait deux
    # maillages et deux durées de calcul.
    after_results = results
    if regime == "exploration" and fast_results:
        after_results = fast_results

    baseline_cp = None
    after_cp = None
    baseline_case = baseline / "cfd"
    if (baseline_case / "constant" / "polyMesh").is_dir():
        samples = sample_wing_pressure(baseline_case)
        if samples:
            baseline_cp = cp_distribution(samples, baseline_section, u_inf)
        samples_after = sample_wing_pressure(output / "cfd")
        if samples_after:
            after_cp = cp_distribution(samples_after, section, u_inf)

    figures = build_comparison_figures(
        output, baseline_section, section, baseline_results, after_results,
        baseline_cp, after_cp, regime,
    )

    images: list[str] = []
    visuals_error: str | None = None
    if visuals and (baseline_case / "constant" / "polyMesh").is_dir():
        images, visuals_error = render_paraview(
            baseline_case, output, u_inf, rho, prefix="seed_"
        )
    elif visuals:
        visuals_error = (
            "seed fields unavailable (mesh purged): the before/after contours "
            "require re-evaluating the seed with "
            "`keep_case_after_run: true`"
        )

    return {
        "baseline": str(baseline),
        "regime": regime,
        "figures": figures,
        "images": images,
        "visuals_error": visuals_error,
        "before": {
            "Cd": baseline_results.get("Cd"),
            "Cl": baseline_results.get("Cl"),
            "Cl_Cd": baseline_results.get("Cl_Cd"),
            "parameters": {
                n: float(s["value"])
                for n, s in baseline_design["parameters"].items()
            },
        },
        "after": {
            "Cd": after_results.get("Cd"),
            "Cl": after_results.get("Cl"),
            "Cl_Cd": after_results.get("Cl_Cd"),
        },
        "sections": {"before": baseline_section, "after": section},
    }


def _check_same_design(qualified: Mapping[str, Any], selected: Mapping[str, Any]) -> None:
    """Refuse d'assembler un dossier incohérent.

    Livrer la géométrie d'un design avec les coefficients d'un autre serait la
    pire sortie possible : elle a l'air parfaitement normale.
    """
    differences = []
    for name, spec in (selected.get("parameters") or {}).items():
        other = (qualified.get("parameters") or {}).get(name)
        if other is None:
            differences.append(f"{name} absent de la requalification")
            continue
        if abs(float(other["value"]) - float(spec["value"])) > 1e-9:
            differences.append(
                f"{name} : {float(spec['value']):g} != {float(other['value']):g}"
            )
    if differences:
        raise ExportError(
            "the re-qualification does not concern the selected design: "
            + " | ".join(differences)
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts/export_best.py",
        description="Range le meilleur design d'une série dans un dossier livrable.",
    )
    parser.add_argument(
        "--iterations-dir", default=str(REPO_ROOT / "data" / "iterations")
    )
    parser.add_argument(
        "--output", default=None,
        help="dossier de sortie (défaut : results/run_AAAAMMJJ_HHMMSS/best_design)",
    )
    parser.add_argument(
        "--qualified-dir", default=None,
        help="itération réévaluant le MÊME design au réglage fin ; ses "
             "coefficients font alors référence",
    )
    parser.add_argument("--cfd-settings", default=None)
    parser.add_argument(
        "--baseline-dir", default=None,
        help="itération servant de référence « avant » ; à aligner sur le même "
             "régime CFD que le design retenu (défaut : première itération "
             "réussie de la série)",
    )
    parser.add_argument(
        "--no-comparison", action="store_true",
        help="n'assemble pas la comparaison avant / après",
    )
    parser.add_argument(
        "--no-case", action="store_true",
        help="n'inclut pas le case OpenFOAM (maillage et champs)",
    )
    parser.add_argument(
        "--no-visuals", action="store_true",
        help="n'appelle pas ParaView (les courbes SVG restent produites)",
    )
    args = parser.parse_args(argv)

    try:
        summary = export_best(
            Path(args.iterations_dir),
            Path(args.output) if args.output else None,
            Path(args.qualified_dir) if args.qualified_dir else None,
            include_case=not args.no_case,
            visuals=not args.no_visuals,
            cfd_settings_path=Path(args.cfd_settings) if args.cfd_settings else None,
            baseline_dir=Path(args.baseline_dir) if args.baseline_dir else None,
            compare=not args.no_comparison,
        )
    except ExportError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    print(
        f"\nDossier : {summary['output']}  "
        f"({summary['size_bytes'] / 1e6:.1f} Mo)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
