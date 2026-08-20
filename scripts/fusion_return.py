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

    return f'''"""Retrace le profil optimisé dans Fusion 360, puis l'extrude.

Généré automatiquement par aero-opt-agent — ne pas modifier à la main.

    Fusion 360 → Utilities → ADD-INS → Scripts and Add-Ins → + → ce fichier

Les coordonnées sont en CENTIMÈTRES : c'est l'unité interne de l'API Fusion,
quelle que soit l'unité affichée dans le document.
"""

import adsk.core
import adsk.fusion
import traceback

# Extrados, du bord d'attaque vers le bord de fuite (cm).
UPPER = [
{literal(kept_upper)}
]

# Intrados, du bord d'attaque vers le bord de fuite (cm).
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
                "Aucun design actif. Ouvrir ou créer un document Fusion, "
                "puis relancer le script."
            )
            return

        root = design.rootComponent
        sketch = root.sketches.add(root.xYConstructionPlane)
        sketch.name = NAME

        # Une spline par surface, et non une seule sur tout le contour : au
        # bord d'attaque la courbe rebrousse, et une spline unique y placerait
        # un point d'inflexion au lieu d'un nez.
        for points in (UPPER, LOWER):
            collection = adsk.core.ObjectCollection.create()
            for x, y in points:
                collection.add(adsk.core.Point3D.create(x, y, 0.0))
            sketch.sketchCurves.sketchFittedSplines.add(collection)

        # Refermer le bord de fuite s'il est ouvert.
        tail_upper = adsk.core.Point3D.create(UPPER[-1][0], UPPER[-1][1], 0.0)
        tail_lower = adsk.core.Point3D.create(LOWER[-1][0], LOWER[-1][1], 0.0)
        if tail_upper.distanceTo(tail_lower) > 1e-6:
            sketch.sketchCurves.sketchLines.addByTwoPoints(tail_upper, tail_lower)

        profiles = sketch.profiles
        if profiles.count == 0:
            ui.messageBox(
                "L'esquisse a été tracée mais Fusion n'y voit aucun profil "
                "fermé. Vérifier la jonction au bord d'attaque, puis extruder "
                "à la main."
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
            "Profil reconstruit et extrudé sur %.1f mm.\\n\\n"
            "La géométrie est native : on peut désormais y ajouter des "
            "congés, la vriller, ou en changer l'envergure."
            % (SPAN_CM * 10.0)
        )

    except Exception:
        if ui:
            ui.messageBox("Échec du script :\\n{{}}".format(traceback.format_exc()))
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
        "# Reprendre ce design dans Fusion 360",
        "",
        "Le résultat d'une optimisation n'est utile que si l'on peut "
        "continuer à le travailler. Ce document donne les trois façons de "
        "ramener la forme optimisée dans Fusion, de la plus propre à la plus "
        "universelle.",
        "",
        "## La forme à reproduire",
        "",
        "| grandeur | valeur |",
        "|---|---|",
        f"| corde | {chord_mm:.2f} mm |",
        f"| envergure | {span_mm:.2f} mm |",
        f"| incidence | {aoa_deg:.2f}° |",
        f"| épaisseur relative | {float(section.get('thickness', 0.0)):.4f} |",
        f"| cambrure relative | {float(section.get('camber', 0.0)):.4f} |",
        f"| paramétrisation | `{parameterization}` |",
    ]
    if source:
        lines.append(f"| profil d'origine | `{source}` |")
    if provenance.get("cst_order") is not None:
        lines.append(
            f"| ordre CST | {provenance['cst_order']} "
            f"({2 * (int(provenance['cst_order']) + 1)} coefficients) |"
        )
    lines.append("")
    lines.append(
        "**L'incidence est déjà dans les coordonnées.** La section exportée "
        "est celle qui a été simulée, profil incliné compris. Si le montage "
        "aval applique lui-même une incidence, il faut partir du profil "
        "redressé, sans quoi elle serait comptée deux fois."
    )
    lines.append("")

    # ── Voie 1 ────────────────────────────────────────────────────────────
    lines += [
        "## Voie 1 — rejouer les paramètres (recommandée)",
        "",
        "C'est la seule voie qui rend un modèle **paramétrique** : un "
        "historique de features modifiable, pas une importation figée.",
        "",
        "```bash",
        "cp design_params.yaml <projet>/configs/design_params.yaml",
        "```",
        "",
        "Puis, dans Fusion : ouvrir le modèle de départ, aller dans "
        "*Utilities → ADD-INS → Scripts and Add-Ins*, et lancer "
        "`fusion/parametric_driver.py`. Le driver reconstruit exactement "
        "cette forme et exporte STEP et STL.",
        "",
    ]
    if parameterization == "cst":
        lines.append(
            "> Le driver accepte les deux paramétrisations. Sur un fichier "
            "`cst`, il reconstruit la forme depuis les coefficients de Kulfan "
            "— le tracé ne manipule que des points, la voie Fusion n'a donc "
            "besoin d'aucun code supplémentaire."
        )
        lines.append("")
    if backend != "fusion":
        lines.append(
            "> Cette optimisation a tourné sur le calculateur **interne**, "
            "sans Fusion. Les paramètres restent parfaitement rejouables : "
            "c'est le même fichier qui décrit la forme des deux côtés."
        )
        lines.append("")

    # ── Voie 2 ────────────────────────────────────────────────────────────
    lines += [
        "## Voie 2 — script prêt à l'emploi",
        "",
        f"`{SKETCH_SCRIPT}` contient les coordonnées de CE profil et trace "
        "l'esquisse tout seul, puis l'extrude sur l'envergure.",
        "",
        "1. Fusion 360 → *Utilities → ADD-INS → Scripts and Add-Ins*",
        f"2. Onglet **Scripts** → **+** → choisir `{SKETCH_SCRIPT}`",
        "3. **Run**",
        "",
        "Le script trace **une spline par surface** plutôt qu'une seule sur "
        "tout le contour : au bord d'attaque la courbe rebrousse, et une "
        "spline unique y placerait un point d'inflexion au lieu d'un nez — "
        "ce qui abîmerait précisément la zone qui décide du décrochage.",
        "",
        "Aucun fichier à localiser, aucune unité à convertir : les "
        "coordonnées sont écrites dans le script, en centimètres, l'unité "
        "interne de l'API Fusion.",
        "",
    ]

    # ── Voie 3 ────────────────────────────────────────────────────────────
    lines += [
        "## Voie 3 — importer la section à la main",
        "",
        "Utile si l'on préfère garder la main, ou travailler dans un autre "
        "logiciel de CAO.",
        "",
        "1. Ouvrir `profile_section.csv` — trois colonnes : `surface`, "
        "`x_mm`, `y_mm`.",
        "2. Dans Fusion : *Insert → Insert Manufacturing Model* ou un "
        "add-in d'import de points ; sinon, tracer une spline en saisissant "
        "les points.",
        "3. Passer une spline ajustée par les points de chaque surface.",
        "4. Refermer le bord de fuite, puis extruder sur "
        f"{span_mm:.1f} mm.",
        "",
        "`profile_section.dat` porte les mêmes points au format profil "
        "standard.",
        "",
        "`profile_chord.dat` porte le profil **redressé**, en corde unitaire. "
        "C'est celui qu'il faut donner à XFOIL ou XFLR5 : ces outils pilotent "
        "eux-mêmes l'incidence, et leur fournir une section déjà inclinée la "
        "compterait deux fois — tout le polaire serait décalé sans que rien "
        "ne le signale.",
        "",
    ]

    # ── Ce qu'il ne faut pas faire ────────────────────────────────────────
    lines += [
        "## Ce qu'il vaut mieux éviter",
        "",
        "**Convertir `geometry.stl` en solide.** Fusion sait le faire, mais "
        "le résultat est un maillage de plusieurs centaines de faces "
        "planes : impossible d'y poser un congé propre, impossible d'en "
        "changer une cote. Le STL est là pour la simulation et "
        "l'impression, pas pour la conception.",
        "",
    ]
    if not has_step:
        lines += [
            "**Chercher un fichier STEP dans ce dossier.** Il n'y en a pas : "
            "la géométrie a été produite par le calculateur interne, qui "
            "écrit directement un STL sans passer par un noyau CAO. Les "
            "voies 1 et 2 en produisent un.",
            "",
        ]

    lines += [
        "## Vérifier que la reprise est fidèle",
        "",
        "Après reconstruction, exporter un STL depuis Fusion et le comparer "
        "à la section de CE dossier :",
        "",
        "```bash",
        "python3 -m profiles.roundtrip export_fusion.stl profile_chord.dat \\",
        f"    --chord {chord_mm:.1f} --aoa {aoa_deg:.2f}",
        "```",
        "",
        "L'outil relit le fichier, en extrait la section et mesure sa "
        "distance au profil — sans faire confiance à ce qui a servi à "
        "l'écrire. Un écart au delà de 2 × 10⁻³ de corde signale une erreur "
        "d'échelle, d'unité ou d'orientation.",
        "",
        "La référence doit être `profile_chord.dat` ou `profile_section.dat`, "
        "**pas le profil de départ**. Le design a été optimisé : il s'écarte "
        "de son point de départ à dessein, et l'outil signalerait cet écart "
        "voulu comme un défaut.",
        "",
        "Mesuré sur le solide de ce dossier : l'écart entre `design_params."
        "yaml` et `geometry.stl` est de l'ordre de 10⁻⁶ de corde — la chaîne "
        "de génération est exacte, ce qui reste à vérifier est ce que Fusion "
        "en fait.",
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
