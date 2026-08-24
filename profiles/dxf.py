"""Lecture du contour d'un DXF 2D (Master Doc v1.5 §3, Mode 3).

    from profiles.dxf import read_dxf_contour

    contour = read_dxf_contour("profil.dxf")

Le Mode 3 demande d'extraire le contour extérieur d'un dessin 2D et de le
ramener à des points ordonnés — après quoi le chemin est celui du Mode 2.

**Pourquoi le DXF et pas le STEP.** Un DXF est un format texte à paires
code/valeur, dont les entités 2D — polylignes, lignes, arcs, cercles — se
lisent sans dépendance. Un STEP décrit une topologie B-Rep : le contour n'y est
pas écrit, il se déduit de faces, d'arêtes et de courbes NURBS chaînées. Le
reconstituer demande un noyau CAO, que ce projet n'embarque pas et ne peut pas
raisonnablement réimplémenter. Le STEP reste donc hors de portée, et c'est dit
plutôt que contourné par une approximation silencieuse.

**Ce qui est lu** : `LWPOLYLINE`, `POLYLINE`/`VERTEX`, `LINE`, `ARC`, `CIRCLE`,
`SPLINE` (par ses points de contrôle ou d'ajustement). Les segments sont
ensuite chaînés par proximité de leurs extrémités : un dessin de CAO ne garantit
ni l'ordre des entités, ni leur sens de parcours.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

Point = tuple[float, float]

#: Deux extrémités séparées de moins que cela, rapportées à la taille du
#: dessin, sont considérées comme jointes. Un dessin de CAO est rarement fermé
#: au bit près : les extrémités se touchent à la tolérance de l'outil qui les a
#: produites.
JOIN_TOL_RATIO = 1e-4

#: Segments d'un arc complet. Soixante-douze donne une corde de 5° — sous la
#: résolution de tout maillage qui suivra.
ARC_SEGMENTS = 72

STATUS_OK = "OK"
STATUS_UNREADABLE = "DXF_ILLISIBLE"
STATUS_NO_GEOMETRY = "DXF_SANS_GEOMETRIE"
STATUS_OPEN_CONTOUR = "DXF_CONTOUR_OUVERT"


@dataclass
class DXFResult:
    """Compte rendu de lecture. Ne lève jamais, comme le reste de la chaîne."""

    success: bool
    status: str = STATUS_OK
    message: str = ""
    contour: list[Point] = field(default_factory=list)
    entities: int = 0
    warnings: list[str] = field(default_factory=list)


def _pairs(text: str) -> list[tuple[int, str]]:
    """Paires (code, valeur) d'un DXF ASCII.

    Le format alterne strictement une ligne de code et une ligne de valeur.
    Un fichier tronqué au milieu d'une paire est donc détectable.
    """
    lignes = [ligne.strip() for ligne in text.splitlines()]
    sorties: list[tuple[int, str]] = []
    for i in range(0, len(lignes) - 1, 2):
        try:
            sorties.append((int(lignes[i]), lignes[i + 1]))
        except ValueError:
            continue
    return sorties


def _entities(pairs: list[tuple[int, str]]) -> list[dict]:
    """Découpe le flux en entités, chacune un dict de listes par code.

    On ne s'intéresse qu'à la section ENTITIES : un DXF porte aussi des tables
    de styles et de calques, dont les codes se confondraient avec des
    coordonnées.
    """
    entites: list[dict] = []
    courante: dict | None = None
    dans_entities = False

    for index, (code, value) in enumerate(pairs):
        if code == 2 and value == "ENTITIES":
            dans_entities = True
            continue
        if code == 0 and value == "ENDSEC" and dans_entities:
            break
        if not dans_entities:
            continue
        if code == 0:
            if courante is not None:
                entites.append(courante)
            courante = {"type": value}
            continue
        if courante is None:
            continue
        courante.setdefault(code, []).append(value)

    if courante is not None:
        entites.append(courante)
    return entites


def _floats(entity: dict, code: int) -> list[float]:
    valeurs = []
    for brut in entity.get(code, []):
        try:
            valeurs.append(float(brut))
        except ValueError:
            continue
    return valeurs


def _arc_points(cx: float, cy: float, r: float,
                start_deg: float, end_deg: float) -> list[Point]:
    """Discrétise un arc, dans le sens trigonométrique du DXF."""
    if r <= 0:
        return []
    sweep = (end_deg - start_deg) % 360.0
    if sweep <= 0:
        sweep = 360.0
    count = max(2, int(ARC_SEGMENTS * sweep / 360.0) + 1)
    points = []
    for i in range(count):
        angle = math.radians(start_deg + sweep * i / (count - 1))
        points.append((cx + r * math.cos(angle), cy + r * math.sin(angle)))
    return points


def _segments(entities: list[dict]) -> tuple[list[list[Point]], list[str]]:
    """Traduit chaque entité en une polyligne. Rend aussi ce qui a été ignoré."""
    segments: list[list[Point]] = []
    ignorees: dict[str, int] = {}

    for entity in entities:
        kind = entity.get("type", "")
        if kind == "LWPOLYLINE":
            xs, ys = _floats(entity, 10), _floats(entity, 20)
            points = list(zip(xs, ys))
            ferme = any(v.strip() in ("1", "129") for v in entity.get(70, []))
            if ferme and len(points) > 2:
                points.append(points[0])
            if len(points) > 1:
                segments.append(points)
        elif kind == "POLYLINE":
            # Les sommets suivent dans des entités VERTEX distinctes ; ils sont
            # récupérés par le passage sur VERTEX ci-dessous.
            continue
        elif kind == "VERTEX":
            xs, ys = _floats(entity, 10), _floats(entity, 20)
            if xs and ys:
                if segments and segments[-1] and len(segments[-1]) < 2:
                    segments[-1].append((xs[0], ys[0]))
                else:
                    segments.append([(xs[0], ys[0])])
        elif kind == "LINE":
            xs, ys = _floats(entity, 10), _floats(entity, 20)
            x2, y2 = _floats(entity, 11), _floats(entity, 21)
            if xs and ys and x2 and y2:
                segments.append([(xs[0], ys[0]), (x2[0], y2[0])])
        elif kind in ("ARC", "CIRCLE"):
            cx, cy = _floats(entity, 10), _floats(entity, 20)
            r = _floats(entity, 40)
            if not (cx and cy and r):
                continue
            if kind == "CIRCLE":
                points = _arc_points(cx[0], cy[0], r[0], 0.0, 360.0)
            else:
                start = _floats(entity, 50) or [0.0]
                end = _floats(entity, 51) or [360.0]
                points = _arc_points(cx[0], cy[0], r[0], start[0], end[0])
            if len(points) > 1:
                segments.append(points)
        elif kind == "SPLINE":
            # Les points d'AJUSTEMENT (11/21) passent par la courbe ; les points
            # de CONTRÔLE (10/20) ne font que la guider. On préfère donc les
            # premiers quand ils existent — les seconds décriraient une forme
            # systématiquement plus « tendue » que le dessin réel.
            xs, ys = _floats(entity, 11), _floats(entity, 21)
            if not xs:
                xs, ys = _floats(entity, 10), _floats(entity, 20)
            points = list(zip(xs, ys))
            if len(points) > 1:
                segments.append(points)
        elif kind not in ("SEQEND", "ENDSEC", "ENDBLK"):
            ignorees[kind] = ignorees.get(kind, 0) + 1

    warnings = []
    if ignorees:
        detail = ", ".join(f"{k} × {n}" for k, n in sorted(ignorees.items()))
        warnings.append(
            f"entités non prises en charge, ignorées : {detail} — si le contour "
            f"en dépend, le résultat sera incomplet"
        )
    # Une POLYLINE dont les VERTEX ont été agrégés séparément produit des
    # fragments d'un point : sans intérêt pour un contour.
    return [s for s in segments if len(s) > 1], warnings


def _extent(segments: list[list[Point]]) -> float:
    xs = [x for s in segments for x, _ in s]
    ys = [y for s in segments for _, y in s]
    if not xs:
        return 0.0
    return max(max(xs) - min(xs), max(ys) - min(ys))


def _chain(segments: list[list[Point]], tol: float) -> list[list[Point]]:
    """Raboute les polylignes par proximité de leurs extrémités.

    Un dessin de CAO ne garantit ni l'ordre des entités ni leur sens : le bord
    d'attaque peut être écrit en dernier et l'intrados à l'envers. On part d'un
    segment et l'on cherche, à chaque étape, celui dont une extrémité touche la
    nôtre — en le retournant au besoin.
    """
    restants = [list(s) for s in segments]
    chaines: list[list[Point]] = []

    while restants:
        courante = restants.pop(0)
        progresse = True
        while progresse and restants:
            progresse = False
            fin = courante[-1]
            for index, candidat in enumerate(restants):
                if math.dist(fin, candidat[0]) <= tol:
                    courante.extend(candidat[1:])
                elif math.dist(fin, candidat[-1]) <= tol:
                    courante.extend(list(reversed(candidat))[1:])
                elif math.dist(courante[0], candidat[-1]) <= tol:
                    courante = candidat[:-1] + courante
                elif math.dist(courante[0], candidat[0]) <= tol:
                    courante = list(reversed(candidat))[:-1] + courante
                else:
                    continue
                restants.pop(index)
                progresse = True
                break
        chaines.append(courante)
    return chaines


def contour_to_selig(contour: list[Point]) -> list[Point]:
    """Réordonne un contour fermé à la convention Selig.

    Un DXF ne dit ni où commence le contour, ni dans quel sens il tourne : cela
    dépend de l'ordre où les entités ont été dessinées. La convention Selig,
    elle, est stricte — bord de fuite, extrados, bord d'attaque, intrados,
    retour au bord de fuite — et c'est elle que tout l'aval attend.

    Deux décisions, prises sur la géométrie et non sur le fichier :

    - **où commencer** : au point d'abscisse maximale, qui est le bord de fuite
      quel que soit le dessin ;
    - **dans quel sens** : celui qui fait passer par l'extrados en premier, ce
      qu'on reconnaît à l'ordonnée moyenne du premier quart du parcours.
    """
    points = list(contour)
    if len(points) > 1 and math.dist(points[0], points[-1]) < 1e-12:
        points = points[:-1]
    if len(points) < 4:
        return points

    tail = max(range(len(points)), key=lambda i: points[i][0])
    points = points[tail:] + points[:tail]

    quarter = max(1, len(points) // 4)
    debut = sum(y for _, y in points[1:1 + quarter]) / quarter
    fin = sum(y for _, y in points[-quarter:]) / quarter
    if debut < fin:
        # On tourne à l'envers : le premier quart longe l'intrados.
        points = [points[0]] + list(reversed(points[1:]))

    return points + [points[0]]


def read_dxf_contour(path: str | Path) -> DXFResult:
    """Contour extérieur d'un DXF 2D, en points ordonnés.

    Rend un compte rendu plutôt qu'une exception : un fichier douteux ne doit
    pas interrompre une chaîne d'ingestion.
    """
    path = Path(path)
    if not path.is_file():
        return DXFResult(False, STATUS_UNREADABLE, f"fichier introuvable : {path}")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return DXFResult(False, STATUS_UNREADABLE, f"lecture impossible : {exc}")

    entities = _entities(_pairs(text))
    if not entities:
        return DXFResult(
            False, STATUS_UNREADABLE,
            "aucune section ENTITIES exploitable — fichier binaire, tronqué, "
            "ou pas un DXF",
        )

    segments, warnings = _segments(entities)
    if not segments:
        return DXFResult(
            False, STATUS_NO_GEOMETRY,
            "la section ENTITIES ne contient aucune courbe exploitable",
            entities=len(entities), warnings=warnings,
        )

    extent = _extent(segments)
    tol = max(extent * JOIN_TOL_RATIO, 1e-12)
    chaines = _chain(segments, tol)

    # Le contour extérieur est la chaîne la plus étendue. Un dessin peut porter
    # un cartouche, un axe ou une cotation ; les prendre pour le profil
    # donnerait une forme absurde.
    chaines.sort(key=lambda c: _extent([c]), reverse=True)
    contour = chaines[0]
    if len(chaines) > 1:
        warnings.append(
            f"{len(chaines)} contours distincts dans le dessin — le plus étendu "
            f"a été retenu, les autres ignorés (cartouche, axe, cotation ?)"
        )

    if len(contour) < 8:
        return DXFResult(
            False, STATUS_NO_GEOMETRY,
            f"le contour retenu n'a que {len(contour)} points : trop peu pour "
            f"décrire un profil",
            entities=len(entities), warnings=warnings,
        )

    gap = math.dist(contour[0], contour[-1])
    if gap > tol:
        if gap > extent * 0.05:
            return DXFResult(
                False, STATUS_OPEN_CONTOUR,
                f"contour ouvert de {gap:.4g} unités pour une étendue de "
                f"{extent:.4g} : les entités ne se rejoignent pas",
                contour=contour, entities=len(entities), warnings=warnings,
            )
        warnings.append(
            f"contour refermé sur un écart de {gap:.4g} unités "
            f"({gap / extent:.2%} de l'étendue)"
        )
    if gap > 0:
        contour = contour + [contour[0]]

    return DXFResult(
        True, STATUS_OK,
        f"contour de {len(contour)} points extrait de {len(entities)} entités",
        contour=contour, entities=len(entities), warnings=warnings,
    )
