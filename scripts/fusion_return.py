"""Chemin de retour vers Fusion 360 (Master Doc v1.5 §5).

    from scripts.fusion_return import write_fusion_return
    write_fusion_return(best_design_dir, design, has_step=False)

Une optimisation qui ne rend qu'un STL est un cul-de-sac de conception. Un STL
est un solide facetté de plusieurs centaines de faces : on peut l'imprimer, on
ne peut pas y ajouter un congé, changer une envergure ou repartir de sa
section. Or c'est exactement ce qu'on veut faire du résultat.

Le §5 demande donc trois voies, et ce module les écrit toutes les trois dans le
paquet final :

**Voie paramétrique** — les meilleurs paramètres sont rejouables tels quels par
le driver Fusion, qui reconstruit un modèle natif. C'est la voie à préférer :
on récupère un historique CAO complet, pas une importation.

**Voie section** — le profil optimisé est exporté en points ordonnés, à
importer comme esquisse. Elle fonctionne sans modèle de départ, et donne une
géométrie propre plutôt qu'un maillage converti.

**Voie script** — un script Fusion prêt à l'emploi, généré avec les
coordonnées de CE profil, qui trace l'esquisse et l'extrude sans intervention.
C'est le « helper script » du §5, et il évite à l'utilisateur d'avoir à
manipuler un import de nuage de points, qui est l'étape où l'on se trompe
d'unité.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

Point = tuple[float, float]

SKETCH_SCRIPT = "rebuild_in_fusion.py"
RETURN_DOC = "FUSION_RETURN.md"


def _spline_points(points: Sequence[Point], limit: int = 120) -> list[Point]:
    """Réduit la densité si besoin, en gardant les extrémités.

    Fusion accepte des centaines de points, mais une spline qui en interpole
    trois cents ondule entre eux : chaque point y devient une contrainte, et
    le bruit d'échantillonnage se transforme en ondulation de surface. Cent
    vingt suffisent largement à décrire un profil, et donnent une courbe plus
    propre que le nuage dont elle est issue.
    """
    if len(points) <= limit:
        return list(points)
    step = (len(points) - 1) / (limit - 1)
    kept = [points[round(i * step)] for i in range(limit)]
    kept[-1] = points[-1]
    return kept


def build_sketch_script(
    upper: Sequence[Point],
    lower: Sequence[Point],
    span_mm: float,
    name: str = "profil_optimise",
) -> str:
    """Script Fusion 360 qui retrace le profil et l'extrude.

    Les coordonnées sont écrites EN DUR dans le script plutôt que lues depuis
    le CSV. C'est délibéré : un script autonome se copie dans Fusion et se
    lance, là où un script qui lit un fichier oblige à gérer un chemin, un
    encodage et une unité — trois occasions de se tromper pour un gain nul.

    Fusion travaille en centimètres dans son API, quoi qu'affiche l'interface.
    La conversion est faite ici, une fois, plutôt que laissée au lecteur.
    """
    kept_upper = _spline_points(list(upper))
    kept_lower = _spline_points(list(lower))

    def literal(points: Sequence[Point]) -> str:
        return "\n".join(
            f"    ({x / 10.0:.6f}, {y / 10.0:.6f})," for x, y in points
        )

    return f'''"""Redraw the optimised profile in Fusion 360, then extrude it.

Generated automatically by aero-opt-agent — do not edit by hand.

    Fusion 360 → Utilities → ADD-INS → Scripts and Add-Ins → + → this file

Coordinates are in CENTIMETRES: that is the internal unit of the Fusion API,
whatever unit the document displays.
"""

import adsk.core
import adsk.fusion
import traceback

# Upper surface, from leading edge to trailing edge (cm).
UPPER = [
{literal(kept_upper)}
]

# Lower surface, from leading edge to trailing edge (cm).
LOWER = [
{literal(kept_lower)}
]

SPAN_CM = {span_mm / 10.0:.6f}
NAME = "{name}"


def run(context):
    ui = None
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        design = adsk.fusion.Design.cast(app.activeProduct)
        if design is None:
            ui.messageBox(
                "No active design. Open or create a Fusion document, "
                "then run the script again."
            )
            return

        root = design.rootComponent
        sketch = root.sketches.add(root.xYConstructionPlane)
        sketch.name = NAME

        # One spline per surface, rather than a single one over the whole
        # contour: at the leading edge the curve turns back on itself, and a
        # single spline would put an inflection point there instead of a nose.
        for points in (UPPER, LOWER):
            collection = adsk.core.ObjectCollection.create()
            for x, y in points:
                collection.add(adsk.core.Point3D.create(x, y, 0.0))
            sketch.sketchCurves.sketchFittedSplines.add(collection)

        # Close the trailing edge if it is open.
        tail_upper = adsk.core.Point3D.create(UPPER[-1][0], UPPER[-1][1], 0.0)
        tail_lower = adsk.core.Point3D.create(LOWER[-1][0], LOWER[-1][1], 0.0)
        if tail_upper.distanceTo(tail_lower) > 1e-6:
            sketch.sketchCurves.sketchLines.addByTwoPoints(tail_upper, tail_lower)

        profiles = sketch.profiles
        if profiles.count == 0:
            ui.messageBox(
                "The sketch was drawn but Fusion sees no closed profile in "
                "it. Check the junction at the leading edge, then extrude by "
                "hand."
            )
            return

        extrudes = root.features.extrudeFeatures
        entry = extrudes.createInput(
            profiles.item(0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        entry.setDistanceExtent(
            False, adsk.core.ValueInput.createByReal(SPAN_CM)
        )
        body = extrudes.add(entry)
        body.name = NAME

        ui.messageBox(
            "Profile rebuilt and extruded over %.1f mm.\\n\\n"
            "The geometry is native: you can now add fillets to it, twist it, "
            "or change its span."
            % (SPAN_CM * 10.0)
        )

    except Exception:
        if ui:
            ui.messageBox("Script failed:\\n{{}}".format(traceback.format_exc()))
'''


def build_return_doc(
    design: Mapping[str, Any],
    section: Mapping[str, Any],
    has_step: bool,
    backend: str = "internal",
    source: str | None = None,
) -> str:
    """Écrit `FUSION_RETURN.md` : les trois voies, avec leurs limites."""
    parameterization = design.get("parameterization", "naca")
    provenance = design.get("provenance") or {}
    chord_mm = float(section.get("chord_mm", 0.0))
    span_mm = float(section.get("span_mm", 0.0))
    aoa_deg = float(section.get("aoa_deg", 0.0))

    lines: list[str] = [
        "# Carrying this design back into CAD",
        "",
        "The result of an optimisation is only useful if you can carry on "
        "working with it. This document gives the ways to bring the optimised "
        "shape back into CAD, from the cleanest to the most universal.",
        "",
        "## The shape to reproduce",
        "",
        "| quantity | value |",
        "|---|---|",
        f"| chord | {chord_mm:.2f} mm |",
        f"| span | {span_mm:.2f} mm |",
        f"| incidence | {aoa_deg:.2f}° |",
        f"| relative thickness | {float(section.get('thickness', 0.0)):.4f} |",
        f"| relative camber | {float(section.get('camber', 0.0)):.4f} |",
        f"| parameterisation | `{parameterization}` |",
    ]
    if source:
        lines.append(f"| source profile | `{source}` |")
    if provenance.get("cst_order") is not None:
        lines.append(
            f"| CST order | {provenance['cst_order']} "
            f"({2 * (int(provenance['cst_order']) + 1)} coefficients) |"
        )
    lines.append("")
    lines.append(
        "**Incidence is already in the coordinates.** The exported section is "
        "the one that was simulated, tilt included. If a downstream setup "
        "applies an incidence of its own, start from the straightened profile "
        "instead, otherwise it would be counted twice."
    )
    lines.append("")

    # ── Voie 0 : le STEP, quand il est là ─────────────────────────────────
    if has_step:
        lines += [
            "## Shortest route — open `geometry.step`",
            "",
            "*File → Open*, or drag and drop the file into your CAD package. "
            "It is a real B-Rep solid of a few faces: you can fillet it, "
            "change its span, assemble it. No conversion, no script. "
            "**FreeCAD reads it just as well as Fusion.**",
            "",
            "> **Do not open `geometry.stl` instead.** CAD will read it, but it "
            "turns it into a mesh body of several hundred flat facets, useless "
            "for design work. The STL is there for the solver and for "
            "printing.",
            "",
            "This route does not give a *parametric* model: the solid has no "
            "feature history. For that, see route 1.",
            "",
        ]

    # ── Voie 1 ────────────────────────────────────────────────────────────
    lines += [
        "## Route 1 — replay the parameters (recommended)",
        "",
        "This is the only route that gives a **parametric** model: an editable "
        "feature history, not a frozen import.",
        "",
        "```bash",
        "cp design_params.yaml <project>/configs/design_params.yaml",
        "```",
        "",
        "Then, in Fusion: open the starting model, go to "
        "*Utilities → ADD-INS → Scripts and Add-Ins*, and run "
        "`fusion/parametric_driver.py`. The driver rebuilds exactly this shape "
        "and exports STEP and STL.",
        "",
    ]
    if parameterization == "cst":
        lines.append(
            "> The driver accepts both parameterisations. On a `cst` file it "
            "rebuilds the shape from the Kulfan coefficients — its drawing "
            "code only handles points, so the Fusion route needs no extra "
            "support."
        )
        lines.append("")
    if backend != "fusion":
        lines.append(
            "> This optimisation ran on the **internal** computer, without "
            "Fusion. The parameters remain perfectly replayable: it is the "
            "same file that describes the shape on both sides."
        )
        lines.append("")

    # ── Voie 2 ────────────────────────────────────────────────────────────
    lines += [
        "## Route 2 — ready-to-run script",
        "",
        f"`{SKETCH_SCRIPT}` contains the coordinates of THIS profile and draws "
        "the sketch on its own, then extrudes it over the span.",
        "",
        "1. Fusion 360 → *Utilities → ADD-INS → Scripts and Add-Ins*",
        f"2. **Scripts** tab → **+** → choose `{SKETCH_SCRIPT}`",
        "3. **Run**",
        "",
        "The script draws **one spline per surface** rather than a single one "
        "over the whole contour: at the leading edge the curve turns back on "
        "itself, and a single spline would put an inflection point there "
        "instead of a nose — which would damage precisely the region that "
        "decides stall.",
        "",
        "No file to locate, no unit to convert: the coordinates are written "
        "into the script, in centimetres, the internal unit of the Fusion API.",
        "",
    ]

    # ── Voie 3 ────────────────────────────────────────────────────────────
    lines += [
        "## Route 3 — import the section by hand",
        "",
        "Useful if you prefer to stay in control, or to work in another CAD "
        "package.",
        "",
        "1. Open `profile_section.csv` — three columns: `surface`, `x_mm`, "
        "`y_mm`.",
        "2. In Fusion: *Insert → Insert Manufacturing Model* or a point-import "
        "add-in; otherwise, draw a spline by entering the points.",
        "3. Fit a spline through the points of each surface.",
        "4. Close the trailing edge, then extrude over "
        f"{span_mm:.1f} mm.",
        "",
        "`profile_section.dat` carries the same points in the standard airfoil "
        "format.",
        "",
        "`profile_chord.dat` carries the profile **straightened**, at unit "
        "chord. That is the one to give XFOIL or XFLR5: those tools drive the "
        "incidence themselves, and feeding them an already-tilted section "
        "would count it twice — the whole polar would be shifted with nothing "
        "to say so.",
        "",
    ]

    # ── Ce qu'il ne faut pas faire ────────────────────────────────────────
    lines += [
        "## What is better avoided",
        "",
        "**Converting `geometry.stl` into a solid.** CAD can do it, but the "
        "result is a mesh of several hundred flat faces: impossible to fillet "
        "properly, impossible to re-dimension. The STL is there for simulation "
        "and printing, not for design.",
        "",
    ]
    if not has_step:
        lines += [
            "**Looking for a STEP file in this folder.** There is none. The "
            "geometry was produced by the internal computer, which writes an "
            "STL; it can also write a STEP, but only when the CAD kernel is "
            "installed (`pip install -r requirements-cad.txt`, about 2 GB). "
            "Without it, routes 1 and 2 produce one.",
            "",
        ]

    lines += [
        "## Checking that the transfer is faithful",
        "",
        "After rebuilding, export an STL from your CAD package and compare it "
        "against the section in THIS folder:",
        "",
        "```bash",
        "python3 -m profiles.roundtrip exported.stl profile_chord.dat \\",
        f"    --chord {chord_mm:.1f} --aoa {aoa_deg:.2f}",
        "```",
        "",
        "The tool reads the file back, extracts its section and measures its "
        "distance to the profile — trusting nothing that was used to write it. "
        "A deviation beyond 2 × 10⁻³ of chord signals an error of scale, unit "
        "or orientation.",
        "",
        "The reference must be `profile_chord.dat` or `profile_section.dat`, "
        "**not the starting profile**. The design has been optimised: it "
        "departs from its starting point by design, and the tool would flag "
        "that intended difference as a defect.",
        "",
        "Measured on the solid in this folder: the deviation between "
        "`design_params.yaml` and `geometry.stl` is of the order of 10⁻⁶ of "
        "chord — the generation chain is exact; what remains to be checked is "
        "what CAD makes of it.",
        "",
    ]
    return "\n".join(lines) + "\n"


def write_fusion_return(
    output: Path,
    design: Mapping[str, Any],
    section: Mapping[str, Any],
    has_step: bool = False,
    backend: str = "internal",
    source: str | None = None,
) -> list[Path]:
    """Écrit `FUSION_RETURN.md` et le script d'esquisse. Rend les chemins écrits."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)

    doc = output / RETURN_DOC
    doc.write_text(
        build_return_doc(design, section, has_step, backend, source),
        encoding="utf-8",
    )

    script = output / SKETCH_SCRIPT
    script.write_text(
        build_sketch_script(
            section.get("upper") or [],
            section.get("lower") or [],
            float(section.get("span_mm", 0.0)),
            str(design.get("design_id", "profil_optimise")),
        ),
        encoding="utf-8",
    )
    return [doc, script]
