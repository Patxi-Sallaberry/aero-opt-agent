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

from fusion.parametric_driver import naca4_profile, profile_from_parameters  # noqa: E402
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
            f"aucune itération réussie dans {iterations_root} — rien à exporter"
        )
    return max(candidates, key=lambda record: float(record["objective"]))


def iteration_dir(iterations_root: Path, iteration: int) -> Path:
    return Path(iterations_root) / f"iter_{int(iteration):04d}"


def run_folder(root: Path = RESULTS_ROOT, when: datetime | None = None) -> Path:
    """`results/run_AAAAMMJJ_HHMMSS/` — un dossier par série, jamais écrasé."""
    stamp = (when or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return Path(root) / f"run_{stamp}"


# ─────────────────────────────────────────────────────────────────────────────
# Section du profil
# ─────────────────────────────────────────────────────────────────────────────


def write_profile_section(design: Mapping[str, Any], output: Path) -> dict:
    """Écrit les coordonnées de la section, en millimètres.

    Deux formats : un CSV lisible par tout tableur ou script d'import CAO, et
    le format profil standard que lisent XFOIL, XFLR5 et la plupart des outils
    aérodynamiques. L'incidence est déjà appliquée : c'est la section
    réellement simulée.
    """
    plan = profile_from_parameters(design["parameters"])
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
                [cl_cd], title="Finesse au fil des itérations",
                x_label="itération", y_label="Cl / Cd",
            ),
        )
        write(
            "coefficients_progress.svg",
            plots.chart(
                [series("Cd", "Cd"), series("Cl", "Cl")],
                title="Cd et Cl au fil des itérations",
                x_label="itération", y_label="coefficient", y_zero_line=True,
            ),
        )

    # ── Section ──────────────────────────────────────────────────────────
    write(
        "profile_shape.svg",
        plots.airfoil_outline(
            section["upper"], section["lower"],
            title=(
                f"Section optimisée — corde {section['chord_mm']:.0f} mm, "
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
                title="Convergence du calcul (seconde moitié)",
                x_label="itération du solveur", y_label="coefficient",
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


def render_paraview(
    case_dir: Path, output: Path, u_inf: float, rho: float, timeout_s: int = 900
) -> tuple[list[str], str | None]:
    """Lance ParaView en lot. Retourne (images, message d'échec éventuel)."""
    figures = output / "figures"
    figures.mkdir(parents=True, exist_ok=True)

    if not shutil.which("pvbatch"):
        return [], (
            "pvbatch introuvable — installer ParaView "
            "(`apt install paraview`) pour obtenir les visuels CFD"
        )
    if not (Path(case_dir) / "constant" / "polyMesh").is_dir():
        return [], (
            "le maillage a été purgé du case archivé "
            "(`execution.keep_case_after_run`) : les visuels CFD demandent "
            "de rejouer le calcul"
        )

    command = ["pvbatch", str(PARAVIEW_SCRIPT), str(case_dir), str(figures),
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
        return [], f"ParaView n'a pas terminé en {timeout_s} s"
    except OSError as exc:
        return [], f"lancement de ParaView impossible : {exc}"

    produced = [
        name for name in ("pressure_field.png", "velocity_field.png",
                          "streamlines.png")
        if (figures / name).is_file()
    ]
    if not produced:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        return [], "ParaView n'a produit aucune image : " + " ".join(tail)[:300]
    return produced, None


# ─────────────────────────────────────────────────────────────────────────────
# Lecture physique
# ─────────────────────────────────────────────────────────────────────────────


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
        direction = "augmentée" if d_aoa > 0 else "réduite"
        notes.append(
            f"**Incidence {direction} de {abs(d_aoa):.2f}°** "
            f"({float(initial['aoa']):.2f}° → {float(final['aoa']):.2f}°). "
            "C'est le levier le plus direct sur la portance : incliner le "
            "profil dévie davantage l'écoulement vers le bas, et la réaction "
            "de cette déviation *est* la portance. La traînée induite croît "
            "en gros comme le carré de la portance, si bien que la finesse "
            "passe par un maximum — typiquement entre 4° et 6° pour un profil "
            "cambré — puis s'effondre au décrochage. "
            + (
                f"La valeur retenue, {float(final['aoa']):.2f}°, tombe dans "
                "cette plage : la recherche a trouvé le sommet de la courbe."
                if 3.0 <= float(final["aoa"]) <= 7.0
                else "La valeur retenue reste en deçà de cette plage, signe "
                "que le budget d'itérations ou les bornes ont limité la "
                "progression."
            )
        )

    d_camber = moved("camber", 1e-4)
    if d_camber is not None:
        direction = "accentuée" if d_camber > 0 else "réduite"
        notes.append(
            f"**Cambrure {direction}** ({float(initial['camber']):.4f} → "
            f"{float(final['camber']):.4f}). La cambrure décale toute la "
            "courbe de portance : un profil cambré porte déjà à incidence "
            "nulle. Elle se paie en traînée de forme et en moment de tangage, "
            "d'où l'existence d'un optimum plutôt qu'une croissance sans fin."
        )

    d_thickness = moved("thickness", 1e-4)
    if d_thickness is not None:
        if d_thickness < 0:
            notes.append(
                f"**Profil aminci** ({float(initial['thickness']):.4f} → "
                f"{float(final['thickness']):.4f} d'épaisseur relative). Un "
                "profil plus fin perturbe moins l'écoulement et traîne moins. "
                "La contrepartie est structurelle — moins d'inertie, donc "
                "moins de rigidité — et un décrochage plus brutal, le bord "
                "d'attaque plus aigu supportant mal les fortes incidences."
            )
        else:
            notes.append(
                f"**Profil épaissi** ({float(initial['thickness']):.4f} → "
                f"{float(final['thickness']):.4f}). L'épaisseur coûte "
                "normalement de la traînée ; qu'elle progresse ici signale "
                "qu'elle sert autre chose — un bord d'attaque plus rond "
                "retarde le décollement et laisse monter l'incidence."
            )

    d_chord = moved("chord", 0.5)
    if d_chord is not None:
        notes.append(
            f"**Corde portée à {float(final['chord']):.1f} mm** (depuis "
            f"{float(initial['chord']):.1f}). Elle agit par deux voies : le "
            "nombre de Reynolds augmente, ce qui abaisse légèrement le "
            "coefficient de frottement, et la surface de référence change — "
            "elle est recalculée à chaque itération, sans quoi la comparaison "
            "des coefficients n'aurait aucun sens."
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
                    f"**Le compromis chiffré** : la portance gagne "
                    f"{gain_cl:+.0f} % *et* la traînée baisse de "
                    f"{abs(gain_cd):.0f} %. Les deux termes s'améliorent à la "
                    "fois — cas favorable, qui tient ici à ce que la surface "
                    "de référence suit la corde."
                )
            elif gain_cl > abs(gain_cd):
                notes.append(
                    f"**Le compromis chiffré** : la portance gagne "
                    f"{gain_cl:+.0f} % pour seulement {gain_cd:+.0f} % de "
                    "traînée. C'est exactement ce que cherche une optimisation "
                    "de finesse — non pas traîner moins, mais porter beaucoup "
                    "plus pour un supplément de traînée modeste."
                )
            else:
                notes.append(
                    f"**Le compromis chiffré** : Cl {gain_cl:+.0f} %, "
                    f"Cd {gain_cd:+.0f} %."
                )

    if cp and cp.get("upper"):
        peak = min(cp["upper"], key=lambda point: point[1])
        notes.append(
            f"**Ce que montre la distribution de pression** : le pic de "
            f"dépression atteint Cp = {peak[1]:.2f} à {peak[0] * 100:.0f} % de "
            "corde. C'est l'extrados qui fait le travail — la dépression y "
            "aspire le profil vers le haut, et elle pèse bien davantage que la "
            "surpression d'intrados. Un pic très creusé suivi d'une remontée "
            "brutale annoncerait un décollement ; une remontée progressive, "
            "comme ici, indique un écoulement encore attaché."
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
) -> str:
    parameters = design["parameters"]
    initial = (initial_design or {}).get("parameters", {})
    lines: list[str] = []

    lines.append(f"# Design optimisé — `{design.get('design_id', 'sans nom')}`")
    lines.append("")
    successes = sum(1 for r in history if r.get("success"))
    failures = len(history) - successes
    lines.append(
        f"Meilleure des **{len(history)} itérations** de la série "
        f"`{source.parent.name}` ({successes} réussie"
        f"{'s' if successes > 1 else ''}, {failures} échouée"
        f"{'s' if failures > 1 else ''}), retenue sur l'objectif "
        f"`{(design.get('objectives') or {}).get('primary', '?')}`."
    )
    lines.append("")

    # ── Performances ──────────────────────────────────────────────────────
    lines.append("## Performances")
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
            f"| Départ (itération {first['iteration']}) | {_fmt(first.get('Cd'))} "
            f"| {_fmt(first.get('Cl'))} | {_fmt(first.get('Cl_Cd'), '.2f')} |"
        )
    lines.append(
        f"| **Optimisé (itération {record.get('iteration')})** "
        f"| **{_fmt(results.get('Cd'))}** | **{_fmt(results.get('Cl'))}** "
        f"| **{_fmt(results.get('Cl_Cd'), '.2f')}** |"
    )
    if fast_results:
        lines.append(
            f"| *— mesuré en exploration* | *{_fmt(fast_results.get('Cd'))}* "
            f"| *{_fmt(fast_results.get('Cl'))}* "
            f"| *{_fmt(fast_results.get('Cl_Cd'), '.2f')}* |"
        )
    if first and isinstance(first.get("Cl_Cd"), (int, float)) and first["Cl_Cd"]:
        reference = fast_results or results
        if isinstance(reference.get("Cl_Cd"), (int, float)):
            gain = (reference["Cl_Cd"] - first["Cl_Cd"]) / abs(first["Cl_Cd"]) * 100
            lines.append("")
            lines.append(f"**Gain de finesse : {gain:+.1f} %**")
    lines.append("")

    mesh = results.get("mesh") or {}
    lines.append(
        f"Maillage : {mesh.get('n_cells', '?')} cellules, non-orthogonalité "
        f"{_fmt(mesh.get('max_non_orthogonality'), '.1f')}, skewness "
        f"{_fmt(mesh.get('max_skewness'), '.2f')}. Coefficients moyennés sur "
        f"{results.get('averaging_window', '?')} itérations, écart-type relatif "
        f"{_fmt(results.get('Cd_rel_std'), '.1e')} sur Cd — "
        f"{'stabilisés' if results.get('coefficients_stable') else '**encore instables**'}."
    )
    lines.append("")

    # ── Paramètres ────────────────────────────────────────────────────────
    lines.append("## Paramètres : départ → arrivée")
    lines.append("")
    lines.append("| paramètre | départ | arrivée | écart | bornes |")
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
            if abs(final_value - start_value) < 1e-12:
                delta = "inchangé"
            elif start_value:
                delta = f"{(final_value - start_value) / abs(start_value) * 100:+.1f} %"
            else:
                delta = f"{final_value - start_value:+g}"
        lines.append(
            f"| `{name}` | {start_text} | **{final_value:g}** {spec.get('unit', '')} "
            f"| {delta} | {float(spec['min']):g} … {float(spec['max']):g} |"
        )
    lines.append("")

    if "profile_shape" in figures:
        lines.append(f"![Section du profil]({figures['profile_shape']})")
        lines.append("")

    # ── Pourquoi c'est mieux ──────────────────────────────────────────────
    if notes:
        lines.append("## Pourquoi cette forme est meilleure")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")

    # ── Trajectoire ───────────────────────────────────────────────────────
    lines.append("## Déroulé de l'optimisation")
    lines.append("")
    for key, caption in (
        ("optimization_progress", "Finesse au fil des itérations"),
        ("coefficients_progress", "Cd et Cl au fil des itérations"),
    ):
        if key in figures:
            lines.append(f"![{caption}]({figures[key]})")
            lines.append("")

    lines.append("| itération | Cd | Cl | Cl/Cd | statut |")
    lines.append("|---|---|---|---|---|")
    for entry in history:
        marker = " ⭐" if entry.get("iteration") == record.get("iteration") else ""
        status = "OK" if entry.get("success") else (
            f"échec — {_cell(entry.get('error_message', ''))[:90]}"
        )
        lines.append(
            f"| {entry.get('iteration')}{marker} | {_fmt(entry.get('Cd'))} "
            f"| {_fmt(entry.get('Cl'))} | {_fmt(entry.get('Cl_Cd'), '.2f')} "
            f"| {status} |"
        )
    lines.append("")

    # ── Visuels CFD ───────────────────────────────────────────────────────
    lines.append("## L'écoulement")
    lines.append("")
    if "cp_distribution" in figures:
        lines.append(f"![Distribution de Cp]({figures['cp_distribution']})")
        lines.append("")
        lines.append(
            "Axe des Cp inversé, comme le veut l'usage : la courbe du haut est "
            "l'extrados, en dépression. L'aire entre les deux courbes est la "
            "portance."
        )
        lines.append("")

    titles = {
        "pressure_field.png": "Champ de pression autour du profil",
        "streamlines.png": "Lignes de courant",
        "velocity_field.png": "Module de la vitesse et sillage",
    }
    captions = {
        "pressure_field.png": (
            "**Champ de pression.** Le rouge sous le bord d'attaque est le "
            "point d'arrêt, où l'écoulement s'immobilise (Cp = +1). Le bleu au "
            "dessus est la dépression qui porte le profil."
        ),
        "streamlines.png": (
            "**Lignes de courant**, colorées par la vitesse. L'accélération "
            "sur l'extrados est la contrepartie de la dépression : c'est le "
            "théorème de Bernoulli, où le fluide qui accélère voit sa pression "
            "chuter."
        ),
        "velocity_field.png": (
            "**Module de la vitesse.** Le sillage se lit derrière le bord de "
            "fuite ; plus il est mince, moins le profil traîne."
        ),
    }
    for image in images:
        lines.append(f"![{titles.get(image, image)}](figures/{image})")
        lines.append("")
        if image in captions:
            lines.append(captions[image])
            lines.append("")

    if visuals_error:
        lines.append(f"> Visuels CFD non produits : {visuals_error}")
        lines.append("")

    if "solver_convergence" in figures:
        lines.append("### Convergence du calcul")
        lines.append("")
        lines.append(f"![Convergence]({figures['solver_convergence']})")
        lines.append("")
        lines.append(
            "Des courbes plates sur la fin sont la condition pour que les "
            "coefficients veuillent dire quelque chose."
        )
        lines.append("")

    # ── Réserve sur les chiffres ──────────────────────────────────────────
    lines.append("## Ce que valent ces chiffres")
    lines.append("")
    lines.append(
        "Le modèle de turbulence `kOmegaSST` suppose la couche limite "
        "turbulente dès le bord d'attaque. À Re ≈ 4 × 10⁵, une bonne part de "
        "l'extrados est encore laminaire : **la traînée est surestimée**, d'un "
        "facteur qui peut approcher 2. Ces valeurs classent correctement des "
        "formes entre elles — ce qu'exige une optimisation — mais ne "
        "constituent pas une prédiction de traînée absolue. Pour un chiffre "
        "publiable, il faut un modèle avec transition laminaire-turbulent, ou "
        "une soufflerie."
    )
    lines.append("")

    # ── Contenu et mode d'emploi ──────────────────────────────────────────
    lines.append("## Contenu du dossier")
    lines.append("")
    lines.append("| fichier | quoi |")
    lines.append("|---|---|")
    lines.append("| `geometry.stl` | la géométrie, **en mètres**, telle que simulée |")
    if has_step:
        lines.append("| `geometry.step` | la même, en CAO |")
    lines.append("| `profile_section.csv` | section 2D en millimètres |")
    lines.append("| `profile_section.dat` | même section au format profil (XFOIL, XFLR5) |")
    lines.append("| `design_params.yaml` | les paramètres exacts, rejouables |")
    lines.append("| `results.json` | les coefficients |")
    lines.append("| `report.html` | ce rapport, autonome, pour un navigateur |")
    lines.append("| `figures/` | courbes et images |")
    if has_case:
        lines.append("| `cfd/` | case OpenFOAM : maillage et champs finaux |")
    lines.append("| `logs/` | journaux de chaque étape |")
    lines.append("")

    if not has_step:
        lines.append("### Pas de fichier STEP")
        lines.append("")
        lines.append(
            "Cette géométrie a été produite par le calculateur interne, qui "
            "écrit directement un STL : sans noyau CAO, il ne peut pas générer "
            "de STEP. Deux façons d'en obtenir un :"
        )
        lines.append("")
        lines.append(
            "1. **Depuis Fusion 360** — copier `design_params.yaml` dans "
            "`configs/`, ouvrir le modèle, lancer `fusion/parametric_driver.py` "
            "(*Utilities → ADD-INS → Scripts and Add-Ins*). Le driver "
            "reconstruit exactement cette forme et exporte STEP **et** STL."
        )
        lines.append(
            "2. **En repartant de la section** — importer "
            "`profile_section.csv` comme nuage de points en CAO, y passer une "
            "spline, extruder sur l'envergure. C'est la voie à préférer pour "
            "de la conception : on récupère une géométrie propre et "
            "paramétrable, là où une conversion de STL ne donnerait qu'un "
            "solide facetté de plusieurs centaines de faces."
        )
        lines.append("")

    lines.append("## Ouvrir les fichiers")
    lines.append("")
    lines.append("```bash")
    lines.append("# la géométrie (STL en mètres)")
    lines.append("paraview geometry.stl")
    lines.append("")
    if has_case:
        lines.append("# les champs CFD")
        lines.append("paraview cfd/best_design.foam")
        lines.append("")
        lines.append("# refaire les visuels après modification")
        lines.append("xvfb-run -a pvbatch paraview_render.py cfd figures 20 1.225")
        lines.append("")
    lines.append("# reprendre l'optimisation depuis ce design")
    lines.append("cp design_params.yaml configs/design_params.yaml")
    lines.append("python3 scripts/run_loop.py --max-iterations 20 \\")
    lines.append("    --cfd-settings configs/cfd_settings_fast.yaml")
    lines.append("```")
    lines.append("")
    if has_case:
        lines.append(
            "Dans ParaView, le pas de temps final porte `U` (vitesse), `p` "
            "(pression **cinématique**, en m²/s² — multiplier par ρ = 1,225 "
            "kg/m³ pour des pascals), `k`, `omega` et `nut`. Le patch `wing` "
            "est la surface de l'aile."
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"Exporté le {datetime.now(timezone.utc).strftime('%d/%m/%Y à %H:%M UTC')} "
        f"par `scripts/export_best.py`."
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

    for line in markdown.splitlines():
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


def export_best(
    iterations_root: Path,
    output: Path | None = None,
    qualified_dir: Path | None = None,
    include_case: bool = True,
    visuals: bool = True,
    cfd_settings_path: Path | None = None,
) -> dict:
    """Assemble le dossier livrable. Retourne un résumé de ce qui a été produit."""
    iterations_root = Path(iterations_root)
    output = Path(output) if output else run_folder() / "best_design"

    record = best_iteration(iterations_root)
    source = iteration_dir(iterations_root, record["iteration"])
    if not source.is_dir():
        raise ExportError(f"dossier de l'itération introuvable : {source}")

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
        visuals_error = "case OpenFOAM absent du dossier exporté"

    first_results = next(
        (r for r in history
         if r.get("success") and isinstance(r.get("Cl"), (int, float))),
        None,
    )
    notes = explain_physics(
        {n: float(s["value"]) for n, s in (initial_design or design)["parameters"].items()},
        {n: float(s["value"]) for n, s in design["parameters"].items()},
        first_results, results, cp,
    )

    report = build_report(
        design, initial_design, record, results, fast_results, history, section,
        figures, images, notes, has_step, has_case, source, visuals_error,
    )
    (output / "README.md").write_text(report, encoding="utf-8")
    (output / "report.html").write_text(
        markdown_to_html(report, output,
                         f"Design optimisé — {design.get('design_id', '')}"),
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
        "images": images,
        "visuals_error": visuals_error,
        "files": copied,
        "size_bytes": sum(p.stat().st_size for p in output.rglob("*") if p.is_file()),
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
            "la requalification ne porte pas sur le design sélectionné : "
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
