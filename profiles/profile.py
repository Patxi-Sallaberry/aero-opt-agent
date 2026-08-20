"""Le profil 2D normalisé, et ce qu'on peut en mesurer.

C'est l'objet qui circule entre l'ingestion (Phase 2) et la
re-paramétrisation (Phase 3). Il porte deux surfaces échantillonnées du bord
d'attaque vers le bord de fuite, dans un repère normalisé :

    bord d'attaque à (0, 0), bord de fuite à (1, 0), corde unitaire

Cette normalisation n'est pas cosmétique. Un fichier de profil peut arriver en
millimètres, décalé, et incliné de quelques degrés — l'incidence ayant été
figée dans les coordonnées. Or l'incidence est ici un paramètre de conception à
part entière : la laisser dans la géométrie la compterait deux fois. La
transformation appliquée est conservée dans `transform`, ce qui permet de
revenir aux coordonnées d'origine et de rendre compte de ce qui a été retiré.

Les mesures — épaisseur, cambrure, rayon de bord d'attaque — servent à la fois
aux contrôles de validité et à l'initialisation de l'ajustement CST.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

Point = tuple[float, float]


@dataclass
class ProfileTransform:
    """Ce qui a été retiré des coordonnées pour les normaliser.

    Permet de reconstituer la géométrie d'origine, et de dire à l'utilisateur
    ce que son fichier contenait — notamment une incidence qu'il n'avait
    peut-être pas conscience d'y avoir figée.
    """

    translation: Point = (0.0, 0.0)
    rotation_deg: float = 0.0
    scale: float = 1.0

    def restore(self, point: Point) -> Point:
        """Ramène un point normalisé dans le repère du fichier d'origine.

        Inverse exact de la normalisation, qui applique translation, puis
        rotation de −θ, puis mise à l'échelle : on remonte donc en sens
        inverse, avec une rotation de **+θ**.
        """
        angle = math.radians(self.rotation_deg)
        x, y = point[0] * self.scale, point[1] * self.scale
        rx = x * math.cos(angle) - y * math.sin(angle)
        ry = x * math.sin(angle) + y * math.cos(angle)
        return rx + self.translation[0], ry + self.translation[1]


@dataclass
class Profile:
    """Un profil 2D propre, normalisé, prêt pour la re-paramétrisation."""

    upper: list[Point]
    lower: list[Point]
    name: str = "profil"
    source: Path | None = None
    transform: ProfileTransform = field(default_factory=ProfileTransform)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ── Formes dérivées ──────────────────────────────────────────────────

    def contour(self) -> list[Point]:
        """Contour fermé : extrados du bord d'attaque au bord de fuite, puis
        intrados en sens inverse. Le premier point est répété à la fin."""
        loop = list(self.upper) + list(reversed(self.lower))[1:]
        if loop and loop[0] != loop[-1]:
            loop.append(loop[0])
        return loop

    def selig(self) -> list[Point]:
        """Contour au format Selig : bord de fuite → extrados → bord d'attaque
        → intrados → bord de fuite. C'est l'ordre qu'attendent XFOIL et la
        plupart des outils de profils."""
        return list(reversed(self.upper)) + list(self.lower)[1:]

    @property
    def n_points(self) -> int:
        return len(self.upper) + len(self.lower)

    # ── Mesures ──────────────────────────────────────────────────────────

    @property
    def chord_mm(self) -> float | None:
        """Corde du fichier d'origine, en millimètres, si l'unité est connue."""
        return self.metadata.get("chord_mm")

    def thickness_distribution(self, stations: int = 100) -> list[Point]:
        """Épaisseur (extrados − intrados) le long de la corde.

        Échantillonnée sur des stations communes : les deux surfaces d'un
        fichier n'ont aucune raison de partager leurs abscisses, et soustraire
        des points d'abscisses différentes n'aurait pas de sens.
        """
        return [
            (x, self._y(self.upper, x) - self._y(self.lower, x))
            for x in _stations(stations)
        ]

    def camber_line(self, stations: int = 100) -> list[Point]:
        """Ligne moyenne : demi-somme des deux surfaces."""
        return [
            (x, (self._y(self.upper, x) + self._y(self.lower, x)) / 2.0)
            for x in _stations(stations)
        ]

    @property
    def max_thickness(self) -> float:
        """Épaisseur relative maximale (t/c)."""
        return max((t for _, t in self.thickness_distribution()), default=0.0)

    @property
    def max_thickness_position(self) -> float:
        """Position de l'épaisseur maximale, en fraction de corde."""
        distribution = self.thickness_distribution()
        if not distribution:
            return 0.0
        return max(distribution, key=lambda point: point[1])[0]

    @property
    def max_camber(self) -> float:
        """Cambrure relative maximale, signée : négative pour un profil
        d'appui, dont la ligne moyenne est sous la corde."""
        line = self.camber_line()
        if not line:
            return 0.0
        return max(line, key=lambda point: abs(point[1]))[1]

    @property
    def max_camber_position(self) -> float:
        line = self.camber_line()
        if not line:
            return 0.0
        return max(line, key=lambda point: abs(point[1]))[0]

    @property
    def trailing_edge_gap(self) -> float:
        """Ouverture du bord de fuite, en fraction de corde.

        Nulle pour un bord de fuite pointu, de l'ordre du pour-cent pour un
        profil épaissi en sortie — ce qui est courant et parfaitement valide.
        """
        if not self.upper or not self.lower:
            return 0.0
        return abs(self.upper[-1][1] - self.lower[-1][1])

    def leading_edge_radius(self, extent: float = 0.05) -> float:
        """Rayon de courbure au bord d'attaque, en fraction de corde.

        Le nez d'un profil est parabolique : la demi-épaisseur y suit
        e(x)² ≈ 2·r·x. La pente de e² en fonction de x, ajustée aux moindres
        carrés en passant par l'origine, donne donc directement 2r. Cette
        relation est exacte pour la famille NACA, et une bonne approximation
        pour tout profil dont le nez est arrondi.

        Deux méthodes plus directes ont été écartées à l'usage :

        - le cercle circonscrit à trois points voisins est dominé par
          l'arrondi, les points de nez étant distants de quelques millièmes de
          corde ; il donnait vingt fois le rayon réel, et le faisait
          *décroître* quand l'épaisseur augmentait ;
        - un cercle ajusté aux moindres carrés sur les points du nez le
          surestime de 30 à 80 %, parce que l'arc échantillonné déborde
          largement la zone où le nez est circulaire.

        Grandeur déterminante pour le décrochage — un nez aigu décroche
        brutalement — et que la Phase 3 devra garder sous contrôle explicite,
        comme le demande le §4 du document.

        **C'est une estimation.** Sur un fichier à pas régulier dont le premier
        point est à 1 % de corde, elle sous-estime le rayon d'environ 7 %, de
        manière constante quelle que soit l'épaisseur : le nez n'y est tout
        simplement pas décrit. Un fichier en répartition cosinus, où les points
        se resserrent au bord d'attaque, donne bien mieux.
        """
        # On se sert des points RÉELS du fichier, pas d'abscisses inventées :
        # interpoler linéairement entre le nez et le premier point disponible
        # remplace une racine carrée par une droite, ce qui écrase l'épaisseur
        # là où elle croît le plus vite et sous-estimait le rayon d'un cinquième.
        # Seule la surface opposée est interpolée, à une abscisse où l'autre a
        # une valeur mesurée.
        samples = [
            (x, (y - self._y(self.lower, x)) / 2.0)
            for x, y in self.upper
            if 0.0 < x <= extent
        ]
        samples += [
            (x, (self._y(self.upper, x) - y) / 2.0)
            for x, y in self.lower
            if 0.0 < x <= extent
        ]
        if len(samples) < 2:
            return 0.0

        # Ajustement de e² = 2·r·x + b·x². Le second terme absorbe la
        # correction d'ordre suivant de la loi d'épaisseur : sans lui, la
        # décroissance de celle-ci au delà du nez tire l'estimation 10 % trop
        # bas, de façon systématique.
        sxx = sxxx = sxxxx = sxe = sxxe = 0.0
        for x, e in samples:
            e2 = e * e
            sxx += x * x
            sxxx += x ** 3
            sxxxx += x ** 4
            sxe += x * e2
            sxxe += x * x * e2

        determinant = sxx * sxxxx - sxxx * sxxx
        if abs(determinant) > 1e-24 and len(samples) >= 4:
            slope = (sxe * sxxxx - sxxe * sxxx) / determinant
        elif sxx > 0:
            slope = sxe / sxx            # repli : ajustement au premier ordre
        else:
            return 0.0
        return max(0.0, slope / 2.0)

    def measures(self) -> dict[str, Any]:
        """Toutes les mesures, pour les rapports et les contrôles."""
        return {
            "n_points": self.n_points,
            "n_upper": len(self.upper),
            "n_lower": len(self.lower),
            "max_thickness": round(self.max_thickness, 6),
            "max_thickness_position": round(self.max_thickness_position, 4),
            "max_camber": round(self.max_camber, 6),
            "max_camber_position": round(self.max_camber_position, 4),
            "trailing_edge_gap": round(self.trailing_edge_gap, 6),
            "leading_edge_radius": round(self.leading_edge_radius(), 6),
        }

    # ── Interne ──────────────────────────────────────────────────────────

    @staticmethod
    def _y(surface: Sequence[Point], x: float) -> float:
        """Ordonnée de la surface à l'abscisse x, par interpolation linéaire."""
        if not surface:
            return 0.0
        if x <= surface[0][0]:
            return surface[0][1]
        if x >= surface[-1][0]:
            return surface[-1][1]
        for (x0, y0), (x1, y1) in zip(surface, surface[1:]):
            if x0 <= x <= x1:
                if x1 - x0 < 1e-12:
                    return y0
                ratio = (x - x0) / (x1 - x0)
                return y0 + ratio * (y1 - y0)
        return surface[-1][1]


def _stations(count: int) -> list[float]:
    """Abscisses d'échantillonnage, resserrées aux bords.

    Une répartition en cosinus place les points là où la géométrie varie le
    plus — au bord d'attaque et au bord de fuite. Un pas uniforme raterait le
    nez, qui est précisément l'endroit qui décide du décrochage.
    """
    return [
        0.5 * (1.0 - math.cos(math.pi * i / (count - 1))) for i in range(count)
    ] if count > 1 else [0.0]


def _fit_circle_radius(points: Sequence[Point]) -> float | None:
    """Rayon du cercle ajusté aux moindres carrés sur un nuage de points.

    Ajustement algébrique classique : le cercle d'équation
    x² + y² + Dx + Ey + F = 0 est linéaire en (D, E, F), ce qui ramène le
    problème à un système 3×3 — résolu ici sans dépendance externe.
    """
    if len(points) < 3:
        return None

    n = float(len(points))
    sx = sy = sxx = syy = sxy = sxz = syz = sz = 0.0
    for x, y in points:
        z = x * x + y * y
        sx += x
        sy += y
        sxx += x * x
        syy += y * y
        sxy += x * y
        sxz += x * z
        syz += y * z
        sz += z

    matrix = [
        [sxx, sxy, sx],
        [sxy, syy, sy],
        [sx, sy, n],
    ]
    vector = [-sxz, -syz, -sz]
    solution = _solve3(matrix, vector)
    if solution is None:
        return None

    d, e, f = solution
    radius_squared = (d * d + e * e) / 4.0 - f
    if radius_squared <= 0:
        return None
    return math.sqrt(radius_squared)


def _solve3(matrix: list[list[float]], vector: list[float]) -> list[float] | None:
    """Résout un système 3×3 par élimination de Gauss avec pivot partiel."""
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]

    for column in range(3):
        pivot = max(range(column, 3), key=lambda r: abs(a[r][column]))
        if abs(a[pivot][column]) < 1e-18:
            return None                  # système singulier : points alignés
        a[column], a[pivot] = a[pivot], a[column]
        for row in range(column + 1, 3):
            factor = a[row][column] / a[column][column]
            for k in range(column, 4):
                a[row][k] -= factor * a[column][k]

    solution = [0.0, 0.0, 0.0]
    for row in range(2, -1, -1):
        total = a[row][3] - sum(a[row][k] * solution[k] for k in range(row + 1, 3))
        solution[row] = total / a[row][row]
    return solution
