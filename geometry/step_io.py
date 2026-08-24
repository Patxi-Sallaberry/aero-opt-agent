"""Écriture et lecture de STEP, via un noyau CAO libre (OpenCASCADE).

    from geometry.step_io import available, write_step, read_step_contour

Deux besoins que le reste du système ne pouvait pas satisfaire seul.

**Écrire un vrai solide.** Le producteur interne écrit un STL : un maillage de
plusieurs centaines de facettes planes. On peut le simuler et l'imprimer, on ne
peut pas y poser un congé ni en changer une cote — importé dans une CAO, il
reste un maillage. Un STEP, lui, décrit des SURFACES : le profil devient une
face réglée unique, et Fusion, SolidWorks ou FreeCAD l'ouvrent comme un solide
natif, éditable.

**Lire un dessin.** Le §3 demande le mode STEP en entrée. Un DXF se déchiffre à
la main — c'est ce que fait `profiles/dxf.py` — mais un STEP décrit une
topologie B-Rep : le contour n'y est pas écrit, il se déduit de faces, d'arêtes
et de courbes NURBS chaînées. Cela demande un noyau, pas un parseur.

**La dépendance est facultative.** `cadquery` pèse près de deux gigaoctets — il
embarque OpenCASCADE en entier. Tout le système fonctionne sans lui, comme
avant ; installé, il ajoute le STEP dans les deux sens. C'est pourquoi rien ici
n'est importé au chargement du module.

    pip install cadquery
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

Point = tuple[float, float]

#: Points maximaux confiés à une spline. Au delà, la courbe se met à onduler
#: entre eux : chaque point devient une contrainte, et le bruit
#: d'échantillonnage se transforme en ondulation de surface. La même limite que
#: pour le script Fusion, et pour la même raison.
MAX_SPLINE_POINTS = 120

#: Nombre minimal de points pour qu'un contour lu ait un sens.
MIN_CONTOUR_POINTS = 8


@dataclass
class StepResult:
    """Compte rendu. Ne lève jamais : l'absence du noyau est un cas normal."""

    success: bool
    message: str = ""
    path: Path | None = None
    contour: list[Point] = field(default_factory=list)
    faces: int = 0
    volume_mm3: float = 0.0
    warnings: list[str] = field(default_factory=list)


def available() -> bool:
    """Le noyau CAO est-il installé ?"""
    try:
        import cadquery  # noqa: F401
    except Exception:
        return False
    return True


def _unavailable(action: str) -> StepResult:
    return StepResult(
        False,
        f"{action} impossible : le noyau CAO n'est pas installé. "
        f"`pip install cadquery` l'ajoute (environ 2 Go, OpenCASCADE inclus). "
        f"Sans lui, le système fonctionne normalement mais n'écrit ni ne lit "
        f"de STEP.",
    )


def _thin(points: Sequence[Point], limit: int = MAX_SPLINE_POINTS) -> list[Point]:
    """Réduit la densité en gardant les extrémités."""
    points = list(points)
    if len(points) <= limit:
        return points
    step = (len(points) - 1) / (limit - 1)
    kept = [points[round(i * step)] for i in range(limit)]
    kept[-1] = points[-1]
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Écriture
# ─────────────────────────────────────────────────────────────────────────────


def write_step(
    upper: Sequence[Point],
    lower: Sequence[Point],
    span_mm: float,
    target: str | Path,
    name: str = "wing",
) -> StepResult:
    """Écrit le profil extrudé en STEP, **en millimètres**.

    Le millimètre est l'unité de la CAO, là où le STL du solveur est en mètres.
    Les deux conventions coexistent volontairement : chacune est celle de son
    consommateur, et la conversion est faite ici une fois plutôt que laissée au
    lecteur.

    Args:
        upper, lower: surfaces en millimètres, du bord d'attaque au bord de
            fuite, incidence déjà appliquée.
        span_mm: longueur d'extrusion.
    """
    if not available():
        return _unavailable("écriture STEP")

    import cadquery as cq

    target = Path(target)
    if span_mm <= 0:
        return StepResult(False, f"envergure non positive : {span_mm}")
    if len(upper) < 4 or len(lower) < 4:
        return StepResult(
            False,
            f"surfaces trop pauvres : {len(upper)} points à l'extrados, "
            f"{len(lower)} à l'intrados",
        )

    warnings: list[str] = []
    haut, bas = _thin(upper), _thin(lower)
    if len(haut) < len(upper):
        warnings.append(
            f"contour allégé de {len(upper) + len(lower)} à "
            f"{len(haut) + len(bas)} points : une spline qui en interpole "
            f"davantage ondule entre eux"
        )

    # Une spline PAR SURFACE, et non une seule sur tout le contour : au bord
    # d'attaque la courbe rebrousse, et une spline unique y placerait un point
    # d'inflexion au lieu d'un nez — la zone même qui décide du décrochage.
    #
    # Le bord de fuite demande deux traitements distincts. Fermé, les deux
    # surfaces s'y rejoignent en UN point : le répéter dans la seconde spline
    # crée une arête de longueur nulle, que le noyau refuse. Ouvert, il faut un
    # SEGMENT DROIT entre les deux lèvres — laisser la spline passer de l'une à
    # l'autre arrondirait un bord de fuite qui est plat par conception.
    ouvert = math.dist(haut[-1], bas[-1]) > 1e-9
    retour = [tuple(p) for p in reversed(bas)]
    try:
        esquisse = cq.Workplane("XY").spline([tuple(p) for p in haut])
        if ouvert:
            esquisse = esquisse.lineTo(*retour[0])
        esquisse = esquisse.spline(retour[1:], includeCurrent=True)
        solide = esquisse.close().extrude(float(span_mm))
    except Exception as exc:  # pragma: no cover - dépend du noyau
        return StepResult(
            False,
            f"le noyau CAO n'a pas pu construire le solide : "
            f"{type(exc).__name__}: {exc}",
            warnings=warnings,
        )

    forme = solide.val()
    volume = float(forme.Volume())
    if volume <= 0:
        return StepResult(
            False,
            "le solide construit a un volume nul ou négatif : le contour se "
            "recoupe probablement",
            warnings=warnings,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        cq.exporters.export(solide, str(target))
    except Exception as exc:  # pragma: no cover - dépend du noyau
        return StepResult(False, f"export STEP impossible : {exc}",
                          warnings=warnings)

    return StepResult(
        True,
        f"STEP écrit : {len(forme.Faces())} faces, {volume:.0f} mm³",
        path=target, faces=len(forme.Faces()), volume_mm3=volume,
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Lecture
# ─────────────────────────────────────────────────────────────────────────────


def read_step_contour(
    path: str | Path, deflection: float = 0.005
) -> StepResult:
    """Contour extérieur d'un STEP 2D ou d'un prisme, en points ordonnés.

    Le contour n'est pas écrit dans le fichier : il faut le déduire. On prend
    la face dont l'aire est la plus grande parmi celles qui sont planes et
    perpendiculaires à l'axe d'extrusion — c'est la section — puis on discrétise
    sa boucle extérieure.

    `deflection` est l'écart maximal toléré entre la courbe vraie et la
    polyligne qui la remplace, dans l'unité du fichier. Trop grossier, le nez
    serait coupé en biseau ; trop fin, on produirait des milliers de points sans
    rien gagner.
    """
    if not available():
        return _unavailable("lecture STEP")

    import cadquery as cq

    path = Path(path)
    if not path.is_file():
        return StepResult(False, f"fichier introuvable : {path}")

    try:
        forme = cq.importers.importStep(str(path))
    except Exception as exc:
        return StepResult(False, f"STEP illisible : {exc}")

    faces = forme.faces().vals()
    if not faces:
        return StepResult(False, "le STEP ne contient aucune face")

    # La section est la plus grande face PLANE. Sur un prisme, les deux faces
    # d'extrémité le sont ; sur un dessin 2D, il n'y en a qu'une. La surface
    # réglée du profil, elle, est courbe et sort d'elle-même.
    planes = []
    for face in faces:
        try:
            if face.geomType() == "PLANE":
                planes.append(face)
        except Exception:  # pragma: no cover - géométrie exotique
            continue
    if not planes:
        return StepResult(
            False,
            "aucune face plane dans le STEP : ce n'est ni un dessin 2D ni un "
            "profil extrudé",
        )

    section = max(planes, key=lambda f: f.Area())
    warnings: list[str] = []
    if len(planes) > 2:
        warnings.append(
            f"{len(planes)} faces planes dans le fichier — la plus grande a été "
            f"retenue comme section"
        )

    points = _wire_points(section, deflection)
    if len(points) < MIN_CONTOUR_POINTS:
        return StepResult(
            False,
            f"contour de {len(points)} points seulement : trop peu pour "
            f"décrire un profil",
            warnings=warnings,
        )

    return StepResult(
        True,
        f"contour de {len(points)} points extrait de {len(faces)} faces",
        contour=points, faces=len(faces), warnings=warnings,
    )


def _wire_points(face, deflection: float) -> list[Point]:
    """Discrétise la boucle extérieure d'une face, arête par arête."""
    points: list[Point] = []
    try:
        aretes = face.outerWire().Edges()
    except Exception:  # pragma: no cover - géométrie exotique
        return points

    for arete in aretes:
        try:
            longueur = float(arete.Length())
        except Exception:  # pragma: no cover
            continue
        # Une droite n'a besoin que de ses deux extrémités ; une courbe est
        # découpée assez finement pour que sa corde reste sous `deflection`.
        courbe = arete.geomType() not in ("LINE",)
        count = 2
        if courbe and longueur > 0:
            count = max(2, min(400, int(longueur / max(deflection, 1e-9)) + 1))
        for i in range(count):
            position = arete.positionAt(i / (count - 1))
            candidat = (float(position.x), float(position.y))
            if not points or math.dist(points[-1], candidat) > 1e-12:
                points.append(candidat)

    if points and math.dist(points[0], points[-1]) > 1e-12:
        points.append(points[0])
    return points
