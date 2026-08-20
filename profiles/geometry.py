"""Reconstruction d'un contour depuis des coefficients CST (Master Doc v1.5 §5).

    from profiles.geometry import cst_contour, cst_measures

    contour = cst_contour(upper, lower, chord=30.0, aoa_rad=0.052)

C'est le chemin retour de la Phase 3 : celle-ci transformait un fichier de
points en coefficients, celui-ci retransforme des coefficients en points. Le
module ne connaît ni unité, ni fichier, ni CAO — il rend des points dans
l'unité de la corde qu'on lui donne, ce qui le laisse utilisable aussi bien
depuis le driver (centimètres) que depuis un tracé (mètres).

Deux choix méritent d'être explicités.

**La répartition est en cosinus**, pas uniforme. La courbure se concentre au
bord d'attaque : à nombre de points égal, une répartition uniforme y laisse
des facettes qui coupent le nez en biseau, et c'est le nez qui décide du
décrochage. Le chemin NACA de la v1.0 échantillonne uniformément ; on ne le
change pas — mais rien n'oblige à répéter ici un compromis qui n'a pas lieu
d'être.

**L'incidence est appliquée analytiquement**, par rotation des points autour du
bord d'attaque, exactement comme `naca4_profile` le fait. Les deux voies
produisent ainsi des contours comparables, et l'incidence reste une variable
d'optimisation à part entière plutôt qu'une propriété figée de la forme.
"""

from __future__ import annotations

import math
from typing import Sequence

from profiles.cst import CSTProfile, CSTSurface, cosine_stations

Point = tuple[float, float]

#: Points par surface. Quatre-vingts suffisent à un profil lisse ; on en prend
#: cent parce que le contour sert aussi de section au STL, et qu'une facette
#: manquée au bord d'attaque se paie en maillage.
CST_PROFILE_POINTS = 100

#: Préfixes des paramètres de conception portant les coefficients.
UPPER_PREFIX = "cst_upper_"
LOWER_PREFIX = "cst_lower_"


class ContourError(ValueError):
    """Coefficients inexploitables — jamais une erreur d'exécution opaque."""


def collect_coefficients(
    values: dict[str, float], prefix: str
) -> list[float]:
    """Range les coefficients d'une surface par indice croissant.

    Exige une suite CONTIGUË depuis zéro. Un trou — `cst_upper_0`, `_1`, `_3` —
    signale un fichier tronqué ou édité à la main ; l'accepter en silence
    décalerait tous les polynômes de Bernstein d'un rang et produirait une
    forme différente de celle qu'on croit reconstruire, sans que rien ne le
    signale.
    """
    indexed: dict[int, float] = {}
    for name, value in values.items():
        if not name.startswith(prefix):
            continue
        suffix = name[len(prefix):]
        if not suffix.isdigit():
            raise ContourError(
                f"{name} : indice de coefficient illisible — attendu "
                f"{prefix}0, {prefix}1, …"
            )
        indexed[int(suffix)] = float(value)

    if not indexed:
        raise ContourError(f"aucun coefficient {prefix}* dans les paramètres")

    expected = set(range(len(indexed)))
    if set(indexed) != expected:
        manquants = sorted(expected - set(indexed))
        surplus = sorted(set(indexed) - expected)
        raise ContourError(
            f"suite de coefficients {prefix}* incomplète : "
            f"{len(indexed)} valeurs, indices manquants {manquants}, "
            f"indices en trop {surplus}"
        )
    return [indexed[i] for i in range(len(indexed))]


def cst_profile(
    upper_coefficients: Sequence[float],
    lower_coefficients: Sequence[float],
    trailing_edges: tuple[float, float] = (0.0, 0.0),
    name: str = "profil",
) -> CSTProfile:
    """Assemble un `CSTProfile` en corde unitaire."""
    if len(upper_coefficients) != len(lower_coefficients):
        raise ContourError(
            f"les deux surfaces n'ont pas le même nombre de coefficients : "
            f"{len(upper_coefficients)} en haut, {len(lower_coefficients)} en bas"
        )
    if len(upper_coefficients) < 2:
        raise ContourError(
            f"{len(upper_coefficients)} coefficient(s) par surface : il en faut "
            f"au moins deux pour décrire autre chose qu'un nez"
        )
    return CSTProfile(
        upper=CSTSurface(list(upper_coefficients), float(trailing_edges[0])),
        lower=CSTSurface(list(lower_coefficients), float(trailing_edges[1])),
        name=name,
    )


def cst_contour(
    upper_coefficients: Sequence[float],
    lower_coefficients: Sequence[float],
    chord: float,
    trailing_edges: tuple[float, float] = (0.0, 0.0),
    aoa_rad: float = 0.0,
    n_points: int = CST_PROFILE_POINTS,
) -> dict[str, list[Point]]:
    """Points du profil, à l'échelle de `chord`, incidence appliquée.

    Returns:
        `{"upper": [...], "lower": [...]}`, chaque surface du bord d'attaque
        vers le bord de fuite, les deux échantillonnées aux MÊMES abscisses
        relatives. C'est la convention de `naca4_profile`, et l'appariement par
        indice dont dépend le pavage des faces d'extrémité du STL.
    """
    if not chord > 0:
        raise ContourError(f"corde non positive : {chord}")
    if n_points < 10:
        raise ContourError(f"n_points trop faible : {n_points}")

    profile = cst_profile(upper_coefficients, lower_coefficients, trailing_edges)
    stations = cosine_stations(n_points + 1)

    cos_a, sin_a = math.cos(-aoa_rad), math.sin(-aoa_rad)

    def place(psi: float, zeta: float) -> Point:
        x, y = chord * psi, chord * zeta
        return x * cos_a - y * sin_a, x * sin_a + y * cos_a

    return {
        "upper": [place(psi, profile.upper.evaluate(psi)) for psi in stations],
        "lower": [place(psi, profile.lower.evaluate(psi)) for psi in stations],
    }


def cst_measures(
    upper_coefficients: Sequence[float],
    lower_coefficients: Sequence[float],
    trailing_edges: tuple[float, float] = (0.0, 0.0),
) -> dict[str, float]:
    """Épaisseur, cambrure et rayon de nez, en fractions de corde.

    Le reste du système — rapports, notes physiques, garde-fous du pipeline —
    raisonne sur ces grandeurs et non sur des coefficients. Les rendre ici
    permet à la voie CST de traverser la chaîne existante sans qu'aucun
    consommateur ait à savoir d'où vient la forme.
    """
    profile = cst_profile(upper_coefficients, lower_coefficients, trailing_edges)
    thickness, thickness_at = profile.max_thickness()
    camber, camber_at = profile.max_camber()
    return {
        "thickness": thickness,
        "thickness_position": thickness_at,
        "camber": camber,
        "camber_position": camber_at,
        "leading_edge_radius": profile.leading_edge_radius,
    }
