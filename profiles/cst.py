"""Paramétrisation CST — Class-Shape Transformation (Master Doc v1.5 §4).

La méthode de Kulfan (Boeing, 2008) est la référence en optimisation de forme
aérodynamique. Chaque surface s'écrit

    ζ(ψ) = C(ψ) · S(ψ) + ψ · Δζ_bf

où ψ = x/c, ζ = y/c, et

    C(ψ) = ψ^N1 · (1 − ψ)^N2          la fonction de classe
    S(ψ) = Σ Aᵢ · Bᵢ(ψ)               la fonction de forme, en polynômes de Bernstein

Trois raisons de la préférer à un ajustement libre :

**Elle est lisse par construction.** Une somme de polynômes de Bernstein ne peut
pas onduler entre les points ; un optimiseur qui fait varier ses coefficients ne
produira jamais une forme en dents de scie, alors qu'une spline libre le fait
au premier pas de trop.

**Elle porte la physique dans sa forme de classe.** `N1 = 0.5` impose le nez en
racine carrée d'un profil à bord d'attaque arrondi ; `N2 = 1` impose un bord de
fuite pointu. Ces comportements sont dans la formulation, pas dans les
coefficients : ils ne peuvent pas être perdus en cours d'optimisation.

**Le premier coefficient PORTE le rayon de nez** : A₀² / 2 donne, en corde
unitaire, le rayon apparent de la surface au bord d'attaque. C'est exactement
le « contrôle explicite du rayon de bord d'attaque » que réclame le §4 — il
suffit de borner A₀, sans contrainte géométrique rapportée. Attention toutefois
à ne pas lire cette valeur pour le rayon du profil : sur une forme cambrée les
deux surfaces n'ont pas la même courbure au nez, et c'est leur moyenne qui vaut
le rayon (voir `CSTProfile.leading_edge_radius`).

Le dernier coefficient, lui, gouverne l'angle de bord de fuite.

L'ajustement est un problème **linéaire** en A : à ψ fixés, ζ − ψ·Δζ_bf vaut
C(ψ)·Σ AᵢBᵢ(ψ). Il se résout donc aux moindres carrés, sans itération, sans
point de départ, et le résultat ne dépend d'aucun aléa — ce qui satisfait
l'exigence de reproductibilité.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence

Point = tuple[float, float]

#: Exposants de la fonction de classe pour un profil courant : nez arrondi
#: (racine carrée) et bord de fuite pointu.
CLASS_ROUND_NOSE = 0.5
CLASS_SHARP_TAIL = 1.0

#: Ordre par défaut. L'ordre n donne n+1 coefficients par surface ; 11 en donne
#: donc 12, soit 24 pour le profil — le haut de la fourchette de 6 à 12 par
#: surface que recommande le document.
#:
#: Cette valeur a d'abord été fixée à 7, calibrée sur des NACA à quatre
#: chiffres. C'était une erreur de méthode : un NACA est une famille à TROIS
#: paramètres, que huit coefficients épousent trivialement. Confronté à de
#: vrais profils — Clark Y, E387, S1223 tirés de la base UIUC —, l'ordre 7
#: échoue à la porte de reconstruction sur les trois.
#:
#: L'ordre 11 a été retenu par validation croisée : on ajuste sur un point sur
#: deux et l'on mesure l'écart sur les points retenus. Jusqu'à environ cinq
#: points par coefficient, l'erreur hors échantillon suit celle d'ajustement —
#: l'ordre capte de la forme. En deçà, les deux divergent : sur l'E387, l'ordre
#: 13 affiche 8,6 × 10⁻⁴ sur ses propres points et 1,12 × 10⁻³ sur ceux qu'il
#: n'a pas vus. Il épouse alors le bruit du fichier, et l'amélioration affichée
#: est un mirage.
DEFAULT_ORDER = 11

#: Régularisation de Tikhonov, relative à la trace du système normal. Elle ne
#: déforme pas l'ajustement de façon perceptible, mais empêche le système de
#: devenir singulier quand les points sont mal répartis — par exemple quand un
#: fichier ne décrit presque pas le bord d'attaque.
DEFAULT_RIDGE = 1e-10


def binomial(n: int, k: int) -> float:
    return math.comb(n, k)


def bernstein(order: int, index: int, psi: float) -> float:
    """Polynôme de Bernstein Bᵢ,ₙ(ψ) = C(n,i) ψⁱ (1−ψ)ⁿ⁻ⁱ."""
    return binomial(order, index) * (psi ** index) * ((1.0 - psi) ** (order - index))


def class_function(psi: float, n1: float = CLASS_ROUND_NOSE,
                   n2: float = CLASS_SHARP_TAIL) -> float:
    """C(ψ) = ψ^N1 (1−ψ)^N2."""
    if psi <= 0.0:
        return 0.0
    if psi >= 1.0:
        return 0.0 if n2 > 0 else 1.0
    return (psi ** n1) * ((1.0 - psi) ** n2)


@dataclass
class CSTSurface:
    """Une surface paramétrée : ses coefficients et son bord de fuite."""

    coefficients: list[float]
    trailing_edge: float = 0.0
    n1: float = CLASS_ROUND_NOSE
    n2: float = CLASS_SHARP_TAIL

    @property
    def order(self) -> int:
        return len(self.coefficients) - 1

    def evaluate(self, psi: float) -> float:
        """Ordonnée de la surface à l'abscisse relative ψ."""
        shape = sum(
            coefficient * bernstein(self.order, index, psi)
            for index, coefficient in enumerate(self.coefficients)
        )
        return class_function(psi, self.n1, self.n2) * shape + psi * self.trailing_edge

    def points(self, stations: Sequence[float]) -> list[Point]:
        return [(psi, self.evaluate(psi)) for psi in stations]

    @property
    def leading_edge_radius(self) -> float:
        """r_ba = A₀² / 2, en corde unitaire.

        Relation exacte de la formulation CST à nez arrondi : elle ne vaut que
        pour N1 = 0.5, d'où le contrôle.
        """
        if not self.coefficients or abs(self.n1 - 0.5) > 1e-9:
            return 0.0
        return self.coefficients[0] ** 2 / 2.0

    @property
    def trailing_edge_angle_deg(self) -> float:
        """Angle de la surface au bord de fuite, en degrés.

        Gouverné par le dernier coefficient : c'est le second comportement que
        le §4 demande de garder sous contrôle.
        """
        if not self.coefficients:
            return 0.0
        return math.degrees(math.atan(self.coefficients[-1] + self.trailing_edge))


@dataclass
class CSTProfile:
    """Un profil complet, décrit par deux surfaces CST."""

    upper: CSTSurface
    lower: CSTSurface
    name: str = "profil"
    metadata: dict = field(default_factory=dict)

    @property
    def order(self) -> int:
        return self.upper.order

    @property
    def n_coefficients(self) -> int:
        return len(self.upper.coefficients) + len(self.lower.coefficients)

    def surfaces(self, stations: Sequence[float]) -> tuple[list[Point], list[Point]]:
        return self.upper.points(stations), self.lower.points(stations)

    def thickness(self, psi: float) -> float:
        return self.upper.evaluate(psi) - self.lower.evaluate(psi)

    def max_thickness(self, stations: int = 200) -> tuple[float, float]:
        """Épaisseur maximale et son abscisse."""
        best = max(
            ((psi, self.thickness(psi)) for psi in cosine_stations(stations)),
            key=lambda item: item[1],
            default=(0.0, 0.0),
        )
        return best[1], best[0]

    def max_camber(self, stations: int = 200) -> tuple[float, float]:
        """Cambrure maximale (signée) et son abscisse."""
        best = max(
            (
                (psi, (self.upper.evaluate(psi) + self.lower.evaluate(psi)) / 2.0)
                for psi in cosine_stations(stations)
            ),
            key=lambda item: abs(item[1]),
            default=(0.0, 0.0),
        )
        return best[1], best[0]

    @property
    def leading_edge_radius(self) -> float:
        """Rayon de nez du PROFIL, en corde unitaire.

        `A₀²/2` donne le rayon apparent d'une surface prise seule. Sur un
        profil cambré les deux surfaces n'ont pas la même courbure au nez et
        aucune des deux ne vaut le rayon du profil : sur un NACA 2412, l'une
        annonce +22 % et l'autre −28 % du rayon véritable. Leur moyenne, elle,
        tombe à moins de 1 % — sur un profil symétrique elles sont d'ailleurs
        égales et la moyenne se réduit à la valeur commune.
        """
        return (
            self.upper.leading_edge_radius + self.lower.leading_edge_radius
        ) / 2.0

    def measures(self) -> dict:
        thickness, thickness_at = self.max_thickness()
        camber, camber_at = self.max_camber()
        return {
            "order": self.order,
            "n_coefficients": self.n_coefficients,
            "max_thickness": round(thickness, 6),
            "max_thickness_position": round(thickness_at, 4),
            "max_camber": round(camber, 6),
            "max_camber_position": round(camber_at, 4),
            "leading_edge_radius": round(self.leading_edge_radius, 6),
            "trailing_edge_gap": round(
                self.upper.trailing_edge - self.lower.trailing_edge, 6
            ),
            "trailing_edge_angle_deg": round(
                self.upper.trailing_edge_angle_deg
                - self.lower.trailing_edge_angle_deg,
                3,
            ),
        }


def cosine_stations(count: int) -> list[float]:
    """Abscisses en répartition cosinus, resserrées aux deux bords.

    C'est là que la géométrie varie le plus vite : un pas uniforme raterait le
    nez, qui décide du décrochage.
    """
    if count < 2:
        return [0.0, 1.0]
    return [0.5 * (1.0 - math.cos(math.pi * i / (count - 1))) for i in range(count)]


# ─────────────────────────────────────────────────────────────────────────────
# Ajustement
# ─────────────────────────────────────────────────────────────────────────────


def fit_surface(
    points: Sequence[Point],
    order: int = DEFAULT_ORDER,
    trailing_edge: float | None = None,
    n1: float = CLASS_ROUND_NOSE,
    n2: float = CLASS_SHARP_TAIL,
    ridge: float = DEFAULT_RIDGE,
) -> CSTSurface:
    """Ajuste une surface CST sur des points, aux moindres carrés.

    L'ajustement est linéaire en les coefficients : il n'a ni point de départ,
    ni tolérance d'arrêt, ni tirage aléatoire. Deux appels sur les mêmes points
    donnent le même résultat au bit près — ce que l'exigence de reproductibilité
    réclame.

    Args:
        points: (ψ, ζ) sur une surface, en corde unitaire.
        order: ordre des polynômes ; donne `order + 1` coefficients.
        trailing_edge: ordonnée au bord de fuite. Déduite du dernier point si
            elle n'est pas fournie.
        ridge: régularisation, relative à la trace du système normal.
    """
    if len(points) < order + 2:
        raise ValueError(
            f"{len(points)} points pour un ordre {order} : il en faut au moins "
            f"{order + 2} pour que l'ajustement soit déterminé"
        )

    if trailing_edge is None:
        # Δζ_bf n'a de sens qu'EN ψ = 1, seul endroit où la fonction de classe
        # s'annule et laisse le terme linéaire seul. Prendre l'ordonnée du
        # dernier point sans vérifier qu'il s'agit bien du bord de fuite
        # reviendrait, sur des points qui s'arrêtent avant, à lire un ζ de
        # surface comme un écartement : le terme ψ·Δζ_bf qui en découle biaise
        # alors l'ajustement sur TOUTE la corde, pas seulement près du bord.
        # L'ingestion place toujours le dernier point en ψ = 1 ; à défaut, on
        # suppose le bord de fuite fermé plutôt que d'inventer un écartement.
        last = points[-1] if points else (0.0, 0.0)
        trailing_edge = last[1] if last[0] >= 1.0 - 1e-6 else 0.0

    # ψ = 0 et ψ = 1 annulent la fonction de classe : ces points n'apportent
    # aucune information sur les coefficients, ils imposeraient seulement des
    # lignes nulles au système.
    usable = [(psi, zeta) for psi, zeta in points if 1e-12 < psi < 1.0 - 1e-12]
    if len(usable) < order + 1:
        raise ValueError(
            f"{len(usable)} points exploitables (hors bords) pour {order + 1} "
            f"coefficients : l'ajustement serait sous-déterminé"
        )

    matrix = [
        [
            class_function(psi, n1, n2) * bernstein(order, index, psi)
            for index in range(order + 1)
        ]
        for psi, _ in usable
    ]
    target = [zeta - psi * trailing_edge for psi, zeta in usable]

    coefficients = solve_least_squares(matrix, target, ridge)
    return CSTSurface(coefficients, trailing_edge, n1, n2)


def solve_least_squares(
    matrix: Sequence[Sequence[float]], target: Sequence[float], ridge: float = 0.0
) -> list[float]:
    """Moindres carrés par équations normales, avec pivot partiel.

    Le système est de taille (ordre + 1) — huit ou dix au plus. Les équations
    normales, souvent déconseillées parce qu'elles carrent le conditionnement,
    restent ici parfaitement sûres à cette dimension, et la régularisation
    couvre le cas où les points sont si mal répartis que le système dégénère.
    """
    n = len(matrix[0])
    normal = [[0.0] * n for _ in range(n)]
    rhs = [0.0] * n

    for row, value in zip(matrix, target):
        for i in range(n):
            rhs[i] += row[i] * value
            for j in range(n):
                normal[i][j] += row[i] * row[j]

    if ridge > 0.0:
        trace = sum(normal[i][i] for i in range(n)) or 1.0
        for i in range(n):
            normal[i][i] += ridge * trace

    return _gauss(normal, rhs)


def _gauss(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Élimination de Gauss avec pivot partiel."""
    n = len(vector)
    augmented = [row[:] + [vector[i]] for i, row in enumerate(matrix)]

    for column in range(n):
        pivot = max(range(column, n), key=lambda r: abs(augmented[r][column]))
        if abs(augmented[pivot][column]) < 1e-18:
            raise ValueError(
                "système singulier : les points ne déterminent pas les "
                "coefficients — répartition trop pauvre, ou ordre trop élevé"
            )
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        for row in range(column + 1, n):
            factor = augmented[row][column] / augmented[column][column]
            for k in range(column, n + 1):
                augmented[row][k] -= factor * augmented[column][k]

    solution = [0.0] * n
    for row in range(n - 1, -1, -1):
        total = augmented[row][n] - sum(
            augmented[row][k] * solution[k] for k in range(row + 1, n)
        )
        solution[row] = total / augmented[row][row]
    return solution


def fit_profile(
    upper: Sequence[Point],
    lower: Sequence[Point],
    order: int = DEFAULT_ORDER,
    name: str = "profil",
    n1: float = CLASS_ROUND_NOSE,
    n2: float = CLASS_SHARP_TAIL,
    ridge: float = DEFAULT_RIDGE,
) -> CSTProfile:
    """Ajuste les deux surfaces d'un profil."""
    return CSTProfile(
        upper=fit_surface(upper, order, None, n1, n2, ridge),
        lower=fit_surface(lower, order, None, n1, n2, ridge),
        name=name,
        metadata={"order": order, "n1": n1, "n2": n2, "ridge": ridge},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Erreur de reconstruction
# ─────────────────────────────────────────────────────────────────────────────


#: Finesse de la polyligne servant à mesurer la distance au profil reconstruit.
#: En répartition cosinus, 600 segments placent le premier à ψ ≈ 7 × 10⁻⁶, ce
#: qui suffit à épouser le nez.
ERROR_SAMPLING = 600

#: Plancher de la mesure, en corde. Les segments de la polyligne coupent la
#: corde de l'arc qu'ils remplacent : même un ajustement EXACT affiche environ
#: 10⁻⁶ de distance. C'est cinq cents fois moins que le seuil de refus, donc
#: sans effet sur le verdict — mais il ne faut pas lire une valeur de cet ordre
#: comme un écart réel, ni écrire de test qui exigerait moins.
ERROR_FLOOR = 2e-6


@dataclass
class ReconstructionError:
    """Écart entre le profil ajusté et le profil d'origine, en corde."""

    max_error: float
    mean_error: float
    rms_error: float
    max_error_position: float
    max_error_surface: str
    upper_max: float
    lower_max: float
    max_vertical_error: float = 0.0
    n_points: int = 0
    errors: list[float] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "max_error": self.max_error,
            "mean_error": self.mean_error,
            "rms_error": self.rms_error,
            "max_error_position": self.max_error_position,
            "max_error_surface": self.max_error_surface,
            "upper_max": self.upper_max,
            "lower_max": self.lower_max,
            "max_vertical_error": self.max_vertical_error,
            "n_points": self.n_points,
        }

    def outliers(self, threshold: float) -> int:
        """Nombre de points au delà d'un seuil.

        Distingue un ajustement qui rate la forme partout d'un ajustement bon
        partout sauf en un point — deux situations que le seul écart maximal
        confond, et qui n'appellent pas la même réaction : la première est un
        modèle inadapté, la seconde le plus souvent un fichier bruité.
        """
        return sum(1 for value in self.errors if value > threshold)


def _distance_to_segment(point: Point, start: Point, end: Point) -> float:
    """Distance d'un point au SEGMENT [start, end] — pas à sa droite support."""
    px, py = point
    ax, ay = start
    bx, by = end
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 <= 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / length2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def distance_to_curve(point: Point, curve: Sequence[Point]) -> float:
    return min(
        (
            _distance_to_segment(point, curve[i], curve[i + 1])
            for i in range(len(curve) - 1)
        ),
        default=0.0,
    )


def reconstruction_error(
    fitted: CSTProfile, upper: Sequence[Point], lower: Sequence[Point]
) -> ReconstructionError:
    """Mesure la distance géométrique du profil reconstruit à l'original.

    L'écart retenu est la distance EUCLIDIENNE de chaque point d'origine à la
    courbe reconstruite, et non l'écart vertical à abscisse égale.

    La distinction n'est pas cosmétique, elle change le verdict. Au bord
    d'attaque la surface est quasi verticale — sa pente y dépasse 6 — et un
    écart vertical y surestime la distance réelle d'autant : sur un NACA 2412
    échantillonné en cosinus, la mesure verticale annonce 1,5 × 10⁻³ de corde
    quand le profil reconstruit passe en réalité à 2 × 10⁻⁴ des points. Le
    premier chiffre ferait rejeter un ajustement parfaitement bon.

    On garde tout de même l'écart vertical maximal sous `max_vertical_error`,
    parce que c'est lui que publie la littérature et qu'il rend les
    comparaisons possibles.
    """
    stations = cosine_stations(ERROR_SAMPLING)
    errors: list[tuple[float, float, str]] = []
    vertical_max = 0.0

    for surface, points, label in (
        (fitted.upper, upper, "extrados"),
        (fitted.lower, lower, "intrados"),
    ):
        curve = surface.points(stations)
        for psi, zeta in points:
            errors.append((distance_to_curve((psi, zeta), curve), psi, label))
            vertical_max = max(vertical_max, abs(surface.evaluate(psi) - zeta))

    if not errors:
        return ReconstructionError(0.0, 0.0, 0.0, 0.0, "", 0.0, 0.0)

    worst = max(errors, key=lambda item: item[0])
    values = [error for error, _, _ in errors]
    upper_max = max(
        (e for e, _, label in errors if label == "extrados"), default=0.0
    )
    lower_max = max(
        (e for e, _, label in errors if label == "intrados"), default=0.0
    )

    return ReconstructionError(
        max_error=worst[0],
        mean_error=sum(values) / len(values),
        rms_error=math.sqrt(sum(v * v for v in values) / len(values)),
        max_error_position=worst[1],
        max_error_surface=worst[2],
        upper_max=upper_max,
        lower_max=lower_max,
        max_vertical_error=vertical_max,
        n_points=len(values),
        errors=values,
    )
