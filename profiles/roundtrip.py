"""Contrôle d'aller-retour : le STL produit décrit-il bien le profil fourni ?

    from profiles.roundtrip import check_roundtrip

    rapport = check_roundtrip(stl_path, profile, chord_m=0.3, aoa_rad=0.052)

La Phase 3 mesurait déjà l'écart entre le profil d'origine et son ajustement
CST. Ce module mesure autre chose, et c'est pourquoi il existe : l'écart entre
le profil d'origine et **le solide réellement écrit sur le disque**.

Entre les deux se glissent une mise à l'échelle, une rotation d'incidence, un
changement d'unité — centimètres du plan vers mètres du STL — et une
triangulation. Chacune de ces étapes peut être fausse sans que l'ajustement
CST le soit, et aucune ne se voit dans les coefficients. Le mode de défaillance
n'est pas théorique : la v1.0 a livré pendant un temps une géométrie où la
conversion d'unités décalait tout d'un facteur dix, avec des tests verts.

Le contrôle relit donc le fichier, en extrait la section, et la compare aux
points d'origine. Il ne fait confiance à rien de ce qui a servi à l'écrire.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from profiles.cst import distance_to_curve  # noqa: E402

Point = tuple[float, float]

#: Tolérance de regroupement des sommets d'une même station, en mètres. Le STL
#: est écrit en notation scientifique à huit décimales : deux sommets censés
#: coïncider ne diffèrent que du dernier bit.
VERTEX_TOL = 1e-9

#: Écart d'aller-retour toléré, en fraction de corde. Il ne s'agit plus de la
#: qualité de l'ajustement — jugée en Phase 3 — mais de la fidélité de
#: l'écriture : à ce stade, tout écart au delà de l'échantillonnage trahit une
#: erreur d'échelle, d'unité ou d'orientation, pas une approximation.
ROUNDTRIP_TOLERANCE = 2e-3


@dataclass
class RoundTripReport:
    """Ce que le STL relu dit du profil d'origine."""

    success: bool
    message: str = ""
    max_error: float = 0.0
    mean_error: float = 0.0
    coverage_error: float = 0.0
    section_points: int = 0
    reference_points: int = 0
    chord_m: float = 0.0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "message": self.message,
            "max_error": self.max_error,
            "mean_error": self.mean_error,
            "coverage_error": self.coverage_error,
            "section_points": self.section_points,
            "reference_points": self.reference_points,
            "chord_m": self.chord_m,
            "warnings": self.warnings,
        }


def read_stl_vertices(path: str | Path) -> list[tuple[float, float, float]]:
    """Sommets d'un STL ASCII, dans l'ordre du fichier."""
    vertices: list[tuple[float, float, float]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if not stripped.startswith("vertex"):
                continue
            parts = stripped.split()
            if len(parts) != 4:
                raise ValueError(f"ligne 'vertex' malformée : {stripped!r}")
            vertices.append(tuple(float(p) for p in parts[1:]))  # type: ignore[misc]
    return vertices


def extract_section(
    path: str | Path, tolerance: float = VERTEX_TOL
) -> list[Point]:
    """Section du solide dans le plan d'extrusion le plus bas, en mètres.

    La géométrie est un prisme droit : tous ses sommets sont sur l'une ou
    l'autre des deux faces d'extrémité, et l'une d'elles porte donc le profil
    entier. On prend la plus basse en z, et l'on déduplique — chaque sommet
    appartient à plusieurs facettes.

    Les points sont rendus SANS ordre : les rétablir dans l'ordre du contour
    exigerait de faire confiance à la façon dont le STL a été écrit, ce que
    tout ce module s'interdit. Les mesures qui suivent n'en ont pas besoin.
    """
    vertices = read_stl_vertices(path)
    if not vertices:
        return []

    z_min = min(z for _, _, z in vertices)
    seen: set[tuple[int, int]] = set()
    section: list[Point] = []
    scale = 1.0 / tolerance
    for x, y, z in vertices:
        if abs(z - z_min) > tolerance:
            continue
        key = (round(x * scale), round(y * scale))
        if key in seen:
            continue
        seen.add(key)
        section.append((x, y))
    return section


def reference_contour(
    upper: Sequence[Point],
    lower: Sequence[Point],
    chord_m: float,
    aoa_rad: float = 0.0,
) -> list[Point]:
    """Contour attendu en mètres : le profil d'origine, mis à l'échelle et incliné.

    Reproduit ce que la génération est censée avoir fait — et seulement cela.
    La rotation est de -aoa autour du bord d'attaque, convention du driver :
    l'écoulement reste dirigé selon +X et c'est la forme qui s'incline.
    """
    cos_a, sin_a = math.cos(-aoa_rad), math.sin(-aoa_rad)

    def place(point: Point) -> Point:
        x, y = chord_m * point[0], chord_m * point[1]
        return x * cos_a - y * sin_a, x * sin_a + y * cos_a

    return [place(p) for p in upper] + [place(p) for p in reversed(lower)]


def _projection(point: Point, curve: Sequence[Point]) -> tuple[float, float]:
    """(distance, abscisse curviligne) du point de `curve` le plus proche."""
    best_distance, best_station = float("inf"), 0.0
    travelled = 0.0
    px, py = point
    for index in range(len(curve) - 1):
        (ax, ay), (bx, by) = curve[index], curve[index + 1]
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        length = math.sqrt(length2)
        if length2 <= 0.0:
            t = 0.0
        else:
            t = ((px - ax) * dx + (py - ay) * dy) / length2
            t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
        distance = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
        if distance < best_distance:
            best_distance, best_station = distance, travelled + t * length
        travelled += length
    return best_distance, best_station


def order_section(section: Sequence[Point], reference: Sequence[Point]) -> list[Point]:
    """Remet les sommets de la section dans l'ordre du contour, et le referme.

    L'ordre est reconstruit en projetant chaque sommet sur le contour de
    RÉFÉRENCE et en triant par abscisse curviligne. Le classement ne doit donc
    rien à la façon dont le STL a été écrit — c'est tout l'intérêt : un fichier
    dont on cherche à savoir s'il est juste ne peut pas servir à établir son
    propre ordre de parcours.

    Un solide amputé ne passe pas au travers pour autant. Ses sommets restants
    se rangent correctement, mais la polyligne obtenue enjambe le trou par une
    corde, dont les points de référence manquants s'écartent d'autant plus que
    le morceau absent est grand.
    """
    ranked = sorted(section, key=lambda p: _projection(p, reference)[1])
    return ranked + [ranked[0]] if ranked else []


def check_roundtrip(
    stl_path: str | Path,
    upper: Sequence[Point],
    lower: Sequence[Point],
    chord_m: float,
    aoa_rad: float = 0.0,
    tolerance: float = ROUNDTRIP_TOLERANCE,
) -> RoundTripReport:
    """Compare la section du STL au profil d'origine.

    Deux mesures, parce qu'une seule ne suffit pas :

    **L'écart de forme** — chaque sommet de la section à sa distance au contour
    de référence — dit si ce qui a été écrit est au bon endroit. C'est la
    mesure qui attrape une erreur d'échelle, d'unité ou de rotation.

    **L'écart de couverture** — chaque point de référence à la section remise
    en ordre — dit si tout le profil a été écrit. Sans elle, un STL amputé de
    son bord d'attaque passerait : les sommets restants seraient tous
    parfaitement placés.

    Les deux mesures sont des distances à une COURBE, jamais à des sommets
    isolés. Une distance de point à point mesurerait le pas d'échantillonnage —
    sur ce profil, la moitié d'un pas de cosinus vaut 7,8 × 10⁻³ de corde, soit
    quatre fois la tolérance — et refuserait un solide parfait au motif qu'il
    n'échantillonne pas aux mêmes abscisses que le fichier d'origine.

    Les deux sont rendus en fraction de corde, pour ne pas dépendre de la
    taille de la pièce. Ne lève pas : un fichier absent ou illisible devient un
    compte rendu, comme partout ailleurs dans la chaîne.
    """
    if not chord_m > 0:
        return RoundTripReport(False, f"corde non positive : {chord_m}")

    path = Path(stl_path)
    if not path.is_file():
        return RoundTripReport(False, f"STL introuvable : {path}")

    try:
        section = extract_section(path)
    except (OSError, ValueError) as exc:
        return RoundTripReport(False, f"STL illisible : {exc}")

    if len(section) < 3:
        return RoundTripReport(
            False,
            f"section vide ou dégénérée : {len(section)} point(s) dans le plan "
            f"d'extrémité — le solide n'est pas un prisme de profil",
        )

    contour = reference_contour(upper, lower, chord_m, aoa_rad)
    if len(contour) < 3:
        return RoundTripReport(False, "profil de référence vide")

    # Le contour est fermé : le dernier segment ramène au premier point, sans
    # quoi le bord de fuite paraîtrait manquant.
    closed = list(contour) + [contour[0]]

    shape = [distance_to_curve(p, closed) / chord_m for p in section]
    ordered = order_section(section, closed)
    coverage = max(distance_to_curve(p, ordered) / chord_m for p in contour)

    max_error = max(shape)
    mean_error = sum(shape) / len(shape)
    success = max_error <= tolerance and coverage <= tolerance

    warnings: list[str] = []
    if success and max_error > tolerance / 2:
        warnings.append(
            f"écart de forme de {max_error:.2e} corde — sous la tolérance, "
            f"mais plus qu'un simple effet d'échantillonnage"
        )

    if success:
        message = (
            f"section relue conforme : {len(section)} sommets à "
            f"{max_error:.2e} corde au plus du profil d'origine"
        )
    elif max_error > tolerance:
        # Le message ne DÉSIGNE pas de coupable. L'outil mesure un écart entre
        # deux formes ; il ne peut pas savoir si le solide est mal écrit ou si
        # la référence qu'on lui a donnée n'est simplement pas celle dont il
        # est issu. Comparer une géométrie OPTIMISÉE à son profil de départ
        # produit un écart important et parfaitement normal — c'est le but même
        # de l'optimisation.
        message = (
            f"la section écrite s'écarte de {max_error:.2e} corde du profil "
            f"fourni (tolérance {tolerance:.0e}). Deux lectures possibles : "
            f"soit le solide a été écrit à la mauvaise échelle, dans la "
            f"mauvaise unité ou avec la mauvaise orientation ; soit la "
            f"référence n'est pas la forme dont il est issu — comparer un "
            f"design optimisé à son profil de départ donne exactement cela"
        )
    else:
        message = (
            f"le profil d'origine n'est pas entièrement couvert : "
            f"{coverage:.2e} corde sans sommet en regard (tolérance "
            f"{tolerance:.0e}) — une partie du contour manque au solide"
        )

    return RoundTripReport(
        success=success, message=message,
        max_error=max_error, mean_error=mean_error, coverage_error=coverage,
        section_points=len(section), reference_points=len(contour),
        chord_m=chord_m, warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="profiles/roundtrip.py",
        description="Compare la section d'un STL au profil dont il est issu.",
    )
    parser.add_argument("stl", help="géométrie produite (STL ASCII, en mètres)")
    parser.add_argument("profil", help="fichier de points d'origine (.dat/.csv)")
    parser.add_argument("--chord", type=float, required=True,
                        help="corde physique en millimètres")
    parser.add_argument("--aoa", type=float, default=0.0,
                        help="incidence appliquée, en degrés")
    parser.add_argument("--tolerance", type=float, default=ROUNDTRIP_TOLERANCE,
                        help="écart toléré, en fraction de corde")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from profiles.loader import load_profile

    ingestion = load_profile(args.profil)
    if not ingestion.success:
        print(f"ÉCHEC [{ingestion.status}] {ingestion.message}", file=sys.stderr)
        return 1

    report = check_roundtrip(
        args.stl, ingestion.profile.upper, ingestion.profile.lower,
        chord_m=args.chord / 1000.0, aoa_rad=math.radians(args.aoa),
        tolerance=args.tolerance,
    )

    if args.json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
        return 0 if report.success else 1

    print(f"Solide relu       : {args.stl}")
    print(f"Profil d'origine  : {ingestion.profile.name}")
    print(f"Section           : {report.section_points} sommets "
          f"(référence {report.reference_points} points)")
    print()
    print("Écarts au profil d'origine (distance géométrique, en corde)")
    print(f"  forme, maximal  : {report.max_error:.3e}")
    print(f"  forme, moyen    : {report.mean_error:.3e}")
    print(f"  couverture      : {report.coverage_error:.3e}")
    print()
    print(f"Aller-retour : {'CONFORME' if report.success else 'REFUSÉ'} "
          f"(tolérance {args.tolerance:.0e} corde)")
    print(f"  {report.message}")
    for warning in report.warnings:
        print(f"  [avertissement] {warning}")
    return 0 if report.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
