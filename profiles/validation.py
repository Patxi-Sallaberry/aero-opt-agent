"""Contrôles de validité d'un profil ingéré (Master Doc §7, porte n°1).

    python3 profiles/validation.py mon_profil.dat

Un profil accepté ici sera maillé, calculé, et servira de point de départ à
toute une optimisation. Les défauts qu'on laisse passer coûtent donc bien plus
qu'une erreur de lecture : un profil qui se recroise produit un maillage
aberrant, un profil trop fin fait échouer snappyHexMesh, et un contour ouvert
n'est pas un solide.

Les contrôles se rangent en deux familles :

- **rédhibitoires** — la géométrie n'est pas un profil exploitable ;
- **avertissements** — c'est inhabituel, mais légitime : un bord de fuite épais
  ou une forte cambrure sont des choix de conception, pas des erreurs.

Confondre les deux est le piège : refuser tout ce qui sort de l'ordinaire
interdirait la moitié des profils réels.
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

from profiles.profile import Point, Profile  # noqa: E402

#: Bornes d'épaisseur relative. En deçà de 1 %, le profil n'est ni maillable
#: avec des couches limites, ni fabricable ; au delà de 40 %, ce n'est plus un
#: profil mais un corps épais, pour lequel ce montage CFD n'est pas calibré.
MIN_THICKNESS = 0.01
MAX_THICKNESS = 0.40

#: Un bord de fuite plus épais que 5 % de corde n'est plus un bord de fuite.
MAX_TE_GAP = 0.05

#: Au delà, le bord de fuite est signalé comme épais — sans être refusé.
NOTABLE_TE_GAP = 0.005

#: Tolérance de fermeture du contour, en fraction de corde.
CLOSURE_TOL = 1e-6

#: Recul d'abscisse toléré sur une surface, en fraction de corde. Un vrai
#: repli de la géométrie dépasse largement ce seuil.
MONOTONIC_TOL = 1e-9


@dataclass
class ValidationReport:
    """Verdict, avec le détail de ce qui a été mesuré."""

    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    measures: dict = field(default_factory=dict)

    def format(self) -> str:
        lines = ["Profil VALIDE" if self.valid else "Profil REFUSÉ"]
        lines += [f"  [erreur] {e}" for e in self.errors]
        lines += [f"  [avertissement] {w}" for w in self.warnings]
        return "\n".join(lines)


def validate_profile(
    profile: Profile,
    min_thickness: float = MIN_THICKNESS,
    max_thickness: float = MAX_THICKNESS,
) -> ValidationReport:
    """Passe le profil au crible. Ne lève jamais."""
    errors: list[str] = []
    warnings: list[str] = []

    upper, lower = profile.upper, profile.lower

    # ── Densité de points ────────────────────────────────────────────────
    if len(upper) < 5 or len(lower) < 5:
        errors.append(
            f"trop peu de points : {len(upper)} à l'extrados, {len(lower)} à "
            f"l'intrados — il en faut au moins 5 par surface"
        )
        return ValidationReport(False, errors, warnings, profile.measures())
    if profile.n_points < 40:
        warnings.append(
            f"{profile.n_points} points seulement : la forme sera grossièrement "
            f"décrite, et le rayon de bord d'attaque mal estimé"
        )

    # ── Fermeture ────────────────────────────────────────────────────────
    nose_gap = math.dist(upper[0], lower[0])
    if nose_gap > CLOSURE_TOL:
        errors.append(
            f"bord d'attaque ouvert de {nose_gap:.2e} corde : le contour n'est "
            f"pas fermé, il ne décrit donc pas un solide"
        )

    te_gap = profile.trailing_edge_gap
    if te_gap > MAX_TE_GAP:
        errors.append(
            f"bord de fuite ouvert de {te_gap:.3f} corde, au delà du maximum "
            f"admis ({MAX_TE_GAP:g}) : la forme n'est plus un profil"
        )
    elif te_gap > NOTABLE_TE_GAP:
        warnings.append(
            f"bord de fuite épais ({te_gap:.4f} corde) — courant sur un profil "
            f"réel, mais le sillage en sera élargi"
        )

    # ── Extrados au dessus de l'intrados ─────────────────────────────────
    crossings = _crossings(profile)
    if crossings:
        errors.append(
            f"les deux surfaces se croisent à {len(crossings)} station(s), "
            f"la première à {crossings[0]:.1%} de corde : extrados et intrados "
            f"ont probablement été intervertis, ou le fichier mélange deux "
            f"profils"
        )

    # ── Repli des surfaces ───────────────────────────────────────────────
    for label, surface in (("extrados", upper), ("intrados", lower)):
        fold = _fold_position(surface)
        if fold is not None:
            errors.append(
                f"{label} : l'abscisse recule à {fold:.1%} de corde — la "
                f"surface se replie sur elle-même"
            )

    # ── Auto-intersection ────────────────────────────────────────────────
    crossing = _self_intersection(profile.contour())
    if crossing is not None:
        errors.append(
            f"contour auto-intersectant vers ({crossing[0]:.4f}, "
            f"{crossing[1]:.4f}) : aucun mailleur n'en tirera un volume"
        )

    # ── Épaisseur ────────────────────────────────────────────────────────
    thickness = profile.max_thickness
    if thickness < min_thickness:
        errors.append(
            f"épaisseur maximale de {thickness:.4f} corde, sous le minimum "
            f"({min_thickness:g}) : trop fin pour être maillé avec des couches "
            f"limites, et pour être fabriqué"
        )
    elif thickness > max_thickness:
        errors.append(
            f"épaisseur maximale de {thickness:.3f} corde, au dessus du maximum "
            f"({max_thickness:g}) : c'est un corps épais, pour lequel ce "
            f"montage CFD n'est pas calibré"
        )

    negative = [(x, t) for x, t in profile.thickness_distribution() if t < -1e-9]
    if negative and not crossings:
        errors.append(
            f"épaisseur négative à {len(negative)} station(s) : les surfaces "
            f"sont inversées"
        )

    # ── Cambrure ─────────────────────────────────────────────────────────
    camber = abs(profile.max_camber)
    if camber > 0.15:
        warnings.append(
            f"cambrure de {camber:.3f} corde, très forte : le décollement de "
            f"bord de fuite est probable, et le RANS stationnaire y perd sa "
            f"validité"
        )

    # ── Bord d'attaque ───────────────────────────────────────────────────
    radius = profile.leading_edge_radius()
    if radius < 1e-4:
        warnings.append(
            f"rayon de bord d'attaque quasi nul ({radius:.2e} corde) : nez "
            f"très aigu, décrochage brutal à attendre"
        )
    elif radius > 0.2:
        warnings.append(
            f"rayon de bord d'attaque de {radius:.3f} corde, inhabituellement "
            f"grand — vérifier que le nez a bien été identifié"
        )

    return ValidationReport(not errors, errors, warnings, profile.measures())


# ─────────────────────────────────────────────────────────────────────────────
# Contrôles géométriques
# ─────────────────────────────────────────────────────────────────────────────


def _crossings(profile: Profile, stations: int = 200) -> list[float]:
    """Stations où l'intrados passe au dessus de l'extrados.

    Les bords sont exclus : les deux surfaces s'y rejoignent par construction,
    et le bruit d'arrondi y produirait des croisements fantômes.
    """
    return [
        x
        for x, thickness in profile.thickness_distribution(stations)
        if thickness < -1e-9 and 0.001 < x < 0.999
    ]


def _fold_position(surface: Sequence[Point]) -> float | None:
    """Abscisse où la surface se replie, si elle le fait.

    Une surface de profil est une fonction de l'abscisse : elle avance
    toujours du nez vers la queue. Un recul signale des points désordonnés ou
    deux profils concaténés.
    """
    for (x0, _), (x1, _) in zip(surface, surface[1:]):
        if x1 < x0 - MONOTONIC_TOL:
            return x0
    return None


def _self_intersection(contour: Sequence[Point]) -> Point | None:
    """Premier point d'auto-intersection du contour, s'il y en a un.

    Comparaison de toutes les paires de segments non adjacents. Le coût est
    quadratique, mais sur quelques centaines de points il reste négligeable
    devant la moindre seconde de CFD — et une auto-intersection non détectée
    coûte, elle, un maillage entier.
    """
    n = len(contour)
    for i in range(n - 1):
        a1, a2 = contour[i], contour[i + 1]
        for j in range(i + 2, n - 1):
            # Les segments qui se touchent par une extrémité, y compris le
            # premier et le dernier d'un contour fermé, ne comptent pas.
            if i == 0 and j == n - 2:
                continue
            point = _segment_intersection(a1, a2, contour[j], contour[j + 1])
            if point is not None:
                return point
    return None


def _segment_intersection(
    p1: Point, p2: Point, p3: Point, p4: Point
) -> Point | None:
    """Intersection stricte de deux segments, ou None."""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    x4, y4 = p4

    denominator = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(denominator) < 1e-15:
        return None                      # parallèles ou confondus

    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / denominator
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / denominator
    epsilon = 1e-9
    if epsilon < t < 1 - epsilon and epsilon < u < 1 - epsilon:
        return x1 + t * (x2 - x1), y1 + t * (y2 - y1)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    from profiles.loader import load_profile

    parser = argparse.ArgumentParser(
        prog="profiles/validation.py",
        description="Charge un profil et le soumet aux contrôles de validité.",
    )
    parser.add_argument("fichier")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--min-thickness", type=float, default=MIN_THICKNESS)
    parser.add_argument("--max-thickness", type=float, default=MAX_THICKNESS)
    args = parser.parse_args(argv)

    ingestion = load_profile(args.fichier)
    if not ingestion.success:
        print(f"ÉCHEC [{ingestion.status}] {ingestion.message}", file=sys.stderr)
        return 2

    report = validate_profile(
        ingestion.profile, args.min_thickness, args.max_thickness
    )

    if args.json:
        print(json.dumps({
            "valid": report.valid,
            "errors": report.errors,
            "warnings": report.warnings,
            "measures": report.measures,
            "ingestion_warnings": ingestion.warnings,
        }, indent=2, ensure_ascii=False))
    else:
        print(f"Profil : {ingestion.profile.name}")
        for warning in ingestion.warnings:
            print(f"  [ingestion] {warning}")
        print(report.format())

    return 0 if report.valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
