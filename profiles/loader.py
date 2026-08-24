"""Chargement de profils 2D depuis un fichier de coordonnées (Master Doc §3, Mode 2).

    python3 profiles/loader.py mon_profil.dat
    python3 profiles/loader.py mon_profil.csv --json

Les fichiers de profils circulent depuis quarante ans dans trois conventions
incompatibles, qu'aucun en-tête ne déclare. Il faut donc les reconnaître :

**Selig** — le plus répandu (base UIUC, XFOIL). Une ligne de titre, puis un
contour continu : bord de fuite → extrados → bord d'attaque → intrados → bord
de fuite. L'abscisse décroît puis recroît.

**Lednicer** — une ligne d'en-tête portant DEUX nombres, le compte de points de
chaque surface, puis l'extrados du bord d'attaque vers le bord de fuite, et
l'intrados de même. L'abscisse croît deux fois.

**CSV libre** — colonnes `x, y`, parfois précédées d'une colonne de surface.
C'est le format que ce projet exporte lui-même (`profile_section.csv`), et le
plus simple à produire depuis un tableur.

Le piège est que Selig et Lednicer ont exactement la même allure : deux
colonnes de nombres. Seule la manière dont l'abscisse évolue les sépare — d'où
la détection par la forme de la séquence, et non par l'extension du fichier.

Aucune exception ne remonte : le chargement rend un `IngestionResult`, comme
`GeometryBackend.generate` rend un `GeometryResult`. Un fichier illisible est
un résultat en échec, pas un plantage de la boucle.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from profiles.profile import Point, Profile, ProfileTransform  # noqa: E402

FORMAT_SELIG = "selig"
FORMAT_LEDNICER = "lednicer"
FORMAT_CSV = "csv"
FORMAT_DXF = "dxf"
FORMAT_STEP = "step"
FORMAT_UNKNOWN = "inconnu"

#: En deçà, deux points sont considérés confondus (en fraction de corde).
DUPLICATE_TOL = 1e-9

#: Nombre de points minimal pour qu'un profil ait un sens.
MIN_POINTS = 10

#: Au delà, on considère que le fichier n'est pas normalisé sur une corde
#: unitaire mais exprimé dans une unité physique.
UNIT_CHORD_TOL = 0.05

STATUS_OK = "OK"
STATUS_FILE_MISSING = "FICHIER_ABSENT"
STATUS_UNREADABLE = "ILLISIBLE"
STATUS_TOO_FEW_POINTS = "POINTS_INSUFFISANTS"
STATUS_FORMAT_UNKNOWN = "FORMAT_NON_RECONNU"
STATUS_DEGENERATE = "GEOMETRIE_DEGENEREE"


@dataclass
class IngestionResult:
    """Ce que rend une ingestion — succès comme échec."""

    success: bool
    status: str = STATUS_OK
    profile: Profile | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def summary(self) -> dict[str, Any]:
        data = {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "warnings": self.warnings,
            **self.metadata,
        }
        if self.profile is not None:
            data["name"] = self.profile.name
            data["measures"] = self.profile.measures()
            data["transform"] = {
                "translation": self.profile.transform.translation,
                "rotation_deg": round(self.profile.transform.rotation_deg, 6),
                "scale": self.profile.transform.scale,
            }
        return data


# ─────────────────────────────────────────────────────────────────────────────
# Lecture brute
# ─────────────────────────────────────────────────────────────────────────────


def _split_numbers(line: str) -> list[str]:
    """Découpe une ligne en champs, quel que soit le séparateur.

    Le point-virgule signale en général une origine européenne, où la virgule
    est le séparateur décimal : « 0,5 » y vaut un demi, pas deux champs.
    """
    if ";" in line:
        return [field.replace(",", ".") for field in line.split(";")]
    if "," in line:
        return line.split(",")
    return line.split()


def _as_floats(fields: Sequence[str]) -> list[float] | None:
    """Convertit des champs en nombres finis, ou rien si l'un résiste."""
    values: list[float] = []
    for field_text in fields:
        text = field_text.strip()
        if not text:
            continue
        try:
            value = float(text)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        values.append(value)
    return values or None


def read_rows(text: str) -> tuple[list[list[float]], list[str], list[str]]:
    """Sépare le fichier en lignes numériques, lignes de texte, et sépare les
    blocs. Retourne (nombres, textes, marqueurs de blocs vides)."""
    numeric: list[list[float]] = []
    textual: list[str] = []
    breaks: list[str] = []

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].split("!", 1)[0].strip()
        if not line:
            # Une ligne vide sépare les deux surfaces dans certains fichiers
            # Lednicer : l'information est conservée.
            if numeric:
                breaks.append(str(len(numeric)))
            continue
        values = _as_floats(_split_numbers(line))
        if values is None or len(values) < 2:
            textual.append(line)
            continue
        numeric.append(values)

    return numeric, textual, breaks


# ─────────────────────────────────────────────────────────────────────────────
# Reconnaissance du format
# ─────────────────────────────────────────────────────────────────────────────


def detect_format(
    rows: Sequence[Sequence[float]], header_fields: Sequence[str] | None = None
) -> str:
    """Reconnaît la convention du fichier à la forme de la suite d'abscisses.

    Selig parcourt le contour d'un trait : l'abscisse descend du bord de fuite
    au bord d'attaque, puis remonte — un seul minimum. Lednicer donne deux
    surfaces l'une après l'autre : l'abscisse monte, retombe au bord
    d'attaque, remonte — un maximum au milieu.
    """
    if len(rows) < MIN_POINTS:
        return FORMAT_UNKNOWN

    xs = [row[0] for row in rows]

    # Un en-tête de deux nombres entiers proches du nombre de points est la
    # signature de Lednicer.
    if header_fields:
        counts = _as_floats(header_fields)
        if counts and len(counts) == 2:
            total = int(counts[0]) + int(counts[1])
            if abs(total - len(rows)) <= 2 and min(counts) > 2:
                return FORMAT_LEDNICER

    descents = sum(1 for a, b in zip(xs, xs[1:]) if b < a - 1e-12)
    ascents = sum(1 for a, b in zip(xs, xs[1:]) if b > a + 1e-12)
    if descents == 0 or ascents == 0:
        # Monotone : une seule surface, ou un contour incomplet.
        return FORMAT_UNKNOWN

    # Position du minimum et du maximum d'abscisse dans la séquence.
    i_min = min(range(len(xs)), key=lambda i: xs[i])
    i_max = max(range(len(xs)), key=lambda i: xs[i])
    interior = 0.15 * len(xs)

    if interior < i_min < len(xs) - interior:
        return FORMAT_SELIG          # le nez est au milieu du parcours
    if interior < i_max < len(xs) - interior:
        return FORMAT_LEDNICER       # le bord de fuite est au milieu
    return FORMAT_UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# Découpage en deux surfaces
# ─────────────────────────────────────────────────────────────────────────────


def split_surfaces(
    points: Sequence[Point], layout: str, counts: tuple[int, int] | None = None
) -> tuple[list[Point], list[Point]]:
    """Sépare le nuage en extrados et intrados, chacun du nez vers la queue."""
    if layout == FORMAT_LEDNICER:
        if counts:
            n_upper = counts[0]
            upper = list(points[:n_upper])
            lower = list(points[n_upper:])
        else:
            pivot = max(range(len(points)), key=lambda i: points[i][0])
            upper = list(points[: pivot + 1])
            lower = list(points[pivot + 1:])
        return _oriented(upper), _oriented(lower)

    # Selig : le bord d'attaque est le point le plus éloigné du bord de fuite.
    nose = _leading_edge_index(points)
    upper = list(reversed(points[: nose + 1]))
    lower = list(points[nose:])
    return _oriented(upper), _oriented(lower)


def _leading_edge_index(points: Sequence[Point]) -> int:
    """Indice du bord d'attaque : le point le plus loin du bord de fuite.

    Prendre simplement l'abscisse minimale échoue sur un profil incliné, ou
    dont le nez déborde légèrement — la distance au bord de fuite, elle, reste
    juste dans tous les cas.
    """
    tail = (
        (points[0][0] + points[-1][0]) / 2.0,
        (points[0][1] + points[-1][1]) / 2.0,
    )
    return max(
        range(len(points)),
        key=lambda i: (points[i][0] - tail[0]) ** 2 + (points[i][1] - tail[1]) ** 2,
    )


def _oriented(surface: list[Point]) -> list[Point]:
    """Ordonne une surface du bord d'attaque vers le bord de fuite."""
    if len(surface) >= 2 and surface[0][0] > surface[-1][0]:
        return list(reversed(surface))
    return surface


# ─────────────────────────────────────────────────────────────────────────────
# Nettoyage
# ─────────────────────────────────────────────────────────────────────────────


def drop_duplicates(points: Sequence[Point], tol: float = DUPLICATE_TOL) -> list[Point]:
    """Supprime les points consécutifs confondus.

    Fréquents au bord d'attaque, où les deux surfaces se rejoignent et où
    beaucoup de fichiers répètent le point. Un doublon casse le calcul de
    courbure et introduit une facette d'aire nulle en aval.
    """
    cleaned: list[Point] = []
    for point in points:
        if cleaned and math.dist(point, cleaned[-1]) <= tol:
            continue
        cleaned.append(point)
    return cleaned


def normalize(
    upper: Sequence[Point], lower: Sequence[Point]
) -> tuple[list[Point], list[Point], ProfileTransform]:
    """Ramène le profil au repère normalisé : nez à l'origine, corde unitaire.

    Retire aussi l'inclinaison éventuelle. Un fichier peut porter une incidence
    figée dans ses coordonnées ; comme l'incidence est ici un paramètre de
    conception, la garder reviendrait à la compter deux fois.
    """
    nose = upper[0] if upper else (0.0, 0.0)
    tail = (
        (upper[-1][0] + lower[-1][0]) / 2.0,
        (upper[-1][1] + lower[-1][1]) / 2.0,
    ) if upper and lower else (1.0, 0.0)

    dx, dy = tail[0] - nose[0], tail[1] - nose[1]
    chord = math.hypot(dx, dy)
    if chord < 1e-12:
        raise ValueError("corde nulle : bord d'attaque et bord de fuite confondus")

    angle = math.atan2(dy, dx)
    cos_a, sin_a = math.cos(-angle), math.sin(-angle)

    def apply(point: Point) -> Point:
        px, py = point[0] - nose[0], point[1] - nose[1]
        rx = px * cos_a - py * sin_a
        ry = px * sin_a + py * cos_a
        return rx / chord, ry / chord

    normalized_upper = [apply(p) for p in upper]
    normalized_lower = [apply(p) for p in lower]

    transform = ProfileTransform(
        translation=(nose[0], nose[1]),
        rotation_deg=math.degrees(angle),
        scale=chord,
    )
    return normalized_upper, normalized_lower, transform


def drop_nose_fold(
    upper: list[Point], lower: list[Point]
) -> tuple[list[Point], list[Point], str | None]:
    """Écarte les points passés DERRIÈRE le bord d'attaque.

    Sur un profil cambré, la surface contourne le nez et quelques points s'y
    retrouvent à une abscisse légèrement négative — de l'ordre de 10⁻⁵ de
    corde, invisible à l'œil. C'est un micro-repli : la surface cesse d'y être
    une fonction de l'abscisse, ce que la validation refuse par ailleurs.

    Il faut les écarter, et pas seulement pour la forme. Une paramétrisation
    CST impose ζ(0) = 0 et n'est pas définie pour ψ < 0 : ces points seraient
    structurellement inatteignables, avec une erreur de reconstruction de
    l'ordre de 3 × 10⁻³ de corde qu'AUCUN ordre ne réduirait — le genre de
    plafond qui fait conclure à tort que la méthode ne convient pas.
    """
    def keep(surface: list[Point]) -> list[Point]:
        return [surface[0]] + [p for p in surface[1:] if p[0] > 0.0]

    kept_upper, kept_lower = keep(upper), keep(lower)
    removed = (len(upper) - len(kept_upper)) + (len(lower) - len(kept_lower))
    if not removed:
        return upper, lower, None
    return kept_upper, kept_lower, (
        f"{removed} point(s) écarté(s) en amont du bord d'attaque : la surface "
        f"y contourne le nez et cesse d'être une fonction de l'abscisse"
    )


def close_leading_edge(
    upper: list[Point], lower: list[Point]
) -> tuple[list[Point], list[Point], str | None]:
    """Fait partir les deux surfaces d'un même point de nez.

    Certains fichiers donnent des nez très légèrement disjoints. Laisser cet
    écart produirait un contour ouvert, qu'aucun mailleur n'accepte.
    """
    if not upper or not lower:
        return upper, lower, None
    gap = math.dist(upper[0], lower[0])
    if gap <= DUPLICATE_TOL:
        return upper, lower, None

    nose = ((upper[0][0] + lower[0][0]) / 2.0, (upper[0][1] + lower[0][1]) / 2.0)
    upper[0] = nose
    lower[0] = nose
    return upper, lower, (
        f"bord d'attaque refermé : les deux surfaces en étaient distantes de "
        f"{gap:.2e} corde"
    )


def close_trailing_edge(
    upper: list[Point], lower: list[Point], tol: float
) -> tuple[list[Point], list[Point], str | None]:
    """Referme le bord de fuite quand l'écart est négligeable.

    Un bord de fuite ouvert est parfaitement légitime — beaucoup de profils
    réels sont épaissis en sortie, pour la fabrication. On ne referme donc que
    ce qui relève de l'arrondi numérique, et l'on signale le reste sans y
    toucher.
    """
    if not upper or not lower:
        return upper, lower, None
    gap = math.dist(upper[-1], lower[-1])
    if gap <= DUPLICATE_TOL:
        return upper, lower, None
    if gap > tol:
        return upper, lower, (
            f"bord de fuite ouvert de {gap:.4f} corde — conservé tel quel, "
            f"c'est une caractéristique du profil et non un défaut"
        )

    tail = ((upper[-1][0] + lower[-1][0]) / 2.0, (upper[-1][1] + lower[-1][1]) / 2.0)
    upper[-1] = tail
    lower[-1] = tail
    return upper, lower, (
        f"bord de fuite refermé : écart de {gap:.2e} corde, sous le seuil"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ingestion
# ─────────────────────────────────────────────────────────────────────────────


def load_profile(
    path: str | Path,
    name: str | None = None,
    close_te_below: float = 1e-4,
) -> IngestionResult:
    """Charge, nettoie et normalise un profil. Ne lève jamais.

    Args:
        path: fichier de coordonnées, en Selig, Lednicer ou CSV.
        name: nom du profil ; à défaut, la ligne de titre ou le nom du fichier.
        close_te_below: en deçà de cet écart (en corde), le bord de fuite est
            refermé. Au delà, il est conservé et signalé : un bord de fuite
            épais est une caractéristique voulue.
    """
    path = Path(path)
    warnings: list[str] = []

    if not path.is_file():
        return IngestionResult(
            False, STATUS_FILE_MISSING, message=f"fichier introuvable : {path}"
        )

    # ── Mode 3 : CAO 2D, voie STEP ────────────────────────────────────────
    # Le contour n'est pas écrit dans un STEP : il se déduit d'une topologie de
    # faces, d'arêtes et de courbes NURBS. Il faut donc un noyau CAO — et si
    # l'utilisateur n'en a pas, il vaut mieux le lui dire que d'échouer sur un
    # « format non reconnu » qui l'enverrait chercher au mauvais endroit.
    if path.suffix.lower() in (".step", ".stp"):
        from geometry.step_io import available as step_available
        from geometry.step_io import read_step_contour
        from profiles.dxf import contour_to_selig

        if not step_available():
            return IngestionResult(
                False, STATUS_FORMAT_UNKNOWN,
                message=(
                    f"{path.name} est un STEP, dont la lecture demande un "
                    f"noyau CAO absent de cette installation. "
                    f"`pip install cadquery` l'ajoute — ou exporter le contour "
                    f"en DXF depuis la CAO, qui se lit sans dépendance."
                ),
            )

        extraction = read_step_contour(path)
        if not extraction.success:
            return IngestionResult(
                False, STATUS_UNREADABLE, message=extraction.message,
                warnings=list(extraction.warnings),
            )
        warnings.extend(extraction.warnings)
        rows = contour_to_selig(extraction.contour)
        if len(rows) < MIN_POINTS:
            return IngestionResult(
                False, STATUS_TOO_FEW_POINTS,
                message=(
                    f"{len(rows)} point(s) dans le contour extrait de "
                    f"{path.name} — il en faut au moins {MIN_POINTS}"
                ),
                warnings=warnings,
            )
        upper_raw, lower_raw = split_surfaces(rows, FORMAT_SELIG, None)
        return _finish_profile(
            upper_raw, lower_raw, rows, [], path, name,
            close_te_below, warnings, FORMAT_STEP,
        )

    # ── Mode 3 : dessin 2D ────────────────────────────────────────────────
    # Un DXF n'est pas une liste de coordonnées mais un dessin : entités
    # dispersées, dans un ordre et un sens quelconques. On en extrait le contour
    # et on le ramène à la convention Selig — après quoi le chemin est
    # exactement celui du Mode 2, nettoyage et validation compris. C'est ce que
    # demande le §3 : « then same path as Mode 2 ».
    if path.suffix.lower() == ".dxf":
        from profiles.dxf import contour_to_selig, read_dxf_contour

        extraction = read_dxf_contour(path)
        if not extraction.success:
            return IngestionResult(
                False, extraction.status, message=extraction.message,
                warnings=list(extraction.warnings),
            )
        warnings.extend(extraction.warnings)
        rows = contour_to_selig(extraction.contour)
        if len(rows) < MIN_POINTS:
            return IngestionResult(
                False, STATUS_TOO_FEW_POINTS,
                message=(
                    f"{len(rows)} point(s) dans le contour extrait de "
                    f"{path.name} — il en faut au moins {MIN_POINTS}"
                ),
                warnings=warnings,
            )
        text, textual, counts = "", [], None
        layout = FORMAT_DXF
        upper_raw, lower_raw = split_surfaces(rows, FORMAT_SELIG, None)
        return _finish_profile(
            upper_raw, lower_raw, rows, textual, path, name,
            close_te_below, warnings, layout,
        )

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return IngestionResult(
            False, STATUS_UNREADABLE, message=f"lecture impossible : {exc}"
        )

    rows, textual, _ = read_rows(text)
    counts: tuple[int, int] | None = None

    # Une colonne de surface explicite dispense de deviner quoi que ce soit.
    # Ce contrôle vient EN PREMIER : dans un tel fichier, chaque ligne porte du
    # texte, donc aucune n'est vue comme un point, et un examen des seules
    # lignes numériques conclurait à un fichier vide.
    labelled = _labelled_surfaces(text)

    if labelled is not None:
        layout = FORMAT_CSV
        upper_raw, lower_raw = labelled
    else:
        if len(rows) < MIN_POINTS:
            return IngestionResult(
                False, STATUS_TOO_FEW_POINTS,
                message=(
                    f"{len(rows)} point(s) lisible(s) dans {path.name} — il en "
                    f"faut au moins {MIN_POINTS} pour décrire un profil"
                ),
            )

        # L'en-tête de comptes d'un fichier Lednicer est lui-même une ligne de
        # deux nombres : rien ne le distingue d'un point, sinon ce qu'il
        # annonce. Le confondre avec une coordonnée fausse à la fois le
        # découpage des surfaces et la reconnaissance du format.
        header_row = _count_header(rows)
        if header_row is not None:
            counts = header_row
            rows = rows[1:]

        header = _split_numbers(textual[0]) if textual else None
        layout = detect_format(rows, header or (
            [str(counts[0]), str(counts[1])] if counts else None
        ))
        if layout == FORMAT_LEDNICER and counts is None and header:
            values = _as_floats(header)
            if values and len(values) == 2:
                counts = (int(values[0]), int(values[1]))
        if layout == FORMAT_UNKNOWN:
            return IngestionResult(
                False, STATUS_FORMAT_UNKNOWN,
                message=(
                    f"format non reconnu dans {path.name} : la suite des "
                    f"abscisses ne correspond ni à un contour Selig, ni à deux "
                    f"surfaces Lednicer. Un fichier ne décrivant qu'une seule "
                    f"surface ne peut pas être un profil fermé."
                ),
            )
        upper_raw, lower_raw = split_surfaces(
            [(row[0], row[1]) for row in rows], layout, counts
        )

    return _finish_profile(
        upper_raw, lower_raw, rows, textual, path, name,
        close_te_below, warnings, layout,
    )


def _finish_profile(
    upper_raw: list[Point],
    lower_raw: list[Point],
    rows: Sequence[Sequence[float]],
    textual: Sequence[str],
    path: Path,
    name: str | None,
    close_te_below: float,
    warnings: list[str],
    layout: str,
) -> IngestionResult:
    """Nettoyage, normalisation et emballage, communs à tous les formats.

    Partagée par les quatre entrées — Selig, Lednicer, CSV et DXF — plutôt que
    recopiée : un contour venu d'un dessin doit subir exactement les mêmes
    contrôles qu'un fichier de coordonnées, sans quoi la confiance qu'on
    accorde au second ne s'étendrait pas au premier.
    """
    upper = drop_duplicates(upper_raw)
    lower = drop_duplicates(lower_raw)
    removed = (len(upper_raw) - len(upper)) + (len(lower_raw) - len(lower))
    if removed:
        warnings.append(f"{removed} point(s) en doublon supprimé(s)")

    if len(upper) < 3 or len(lower) < 3:
        return IngestionResult(
            False, STATUS_DEGENERATE,
            message=(
                f"surfaces trop pauvres après nettoyage : {len(upper)} points "
                f"à l'extrados, {len(lower)} à l'intrados"
            ),
        )

    upper, lower, note = close_leading_edge(upper, lower)
    if note:
        warnings.append(note)

    try:
        upper, lower, transform = normalize(upper, lower)
    except ValueError as exc:
        return IngestionResult(False, STATUS_DEGENERATE, message=str(exc))

    upper, lower, note = drop_nose_fold(upper, lower)
    if note:
        warnings.append(note)

    upper, lower, note = close_trailing_edge(upper, lower, close_te_below)
    if note:
        warnings.append(note)

    if abs(transform.rotation_deg) > 0.05:
        warnings.append(
            f"incidence de {transform.rotation_deg:+.2f}° retirée des "
            f"coordonnées : elle est un paramètre de conception à part entière, "
            f"la laisser dans la géométrie la compterait deux fois"
        )

    chord_mm = None
    if abs(transform.scale - 1.0) > UNIT_CHORD_TOL:
        chord_mm = transform.scale
        warnings.append(
            f"fichier non normalisé : corde de {transform.scale:g} unités, "
            f"ramenée à 1 (les mesures sont donc relatives)"
        )

    profile = Profile(
        upper=upper,
        lower=lower,
        name=name or _guess_name(textual, path),
        source=path,
        transform=transform,
        metadata={
            "format": layout,
            "points_lus": len(rows),
            "chord_mm": chord_mm,
        },
    )

    return IngestionResult(
        True, STATUS_OK, profile=profile,
        message=f"profil « {profile.name} » chargé ({layout}, {profile.n_points} points)",
        warnings=warnings,
        metadata={"format": layout, "source": str(path)},
    )


def _count_header(rows: Sequence[Sequence[float]]) -> tuple[int, int] | None:
    """Reconnaît l'en-tête de comptes d'un fichier Lednicer.

    Deux entiers dont la somme vaut le nombre de points restants : aucune
    coordonnée de profil ne ressemble à cela, les abscisses étant comprises
    entre 0 et 1 sur un fichier normalisé, et les comptes valant plusieurs
    dizaines.
    """
    if len(rows) < MIN_POINTS + 1:
        return None
    first = rows[0]
    if len(first) != 2:
        return None
    if any(value < 3 or value != int(value) for value in first):
        return None
    if abs(int(first[0]) + int(first[1]) - (len(rows) - 1)) > 2:
        return None
    return int(first[0]), int(first[1])


def _labelled_surfaces(text: str) -> tuple[list[Point], list[Point]] | None:
    """Lit un CSV portant une colonne de surface, s'il y en a une.

    C'est le format que ce projet exporte : la surface y est déclarée, ce qui
    dispense de la deviner.
    """
    upper: list[Point] = []
    lower: list[Point] = []
    seen_header = False

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        fields = [f.strip() for f in _split_numbers(line)]
        if len(fields) < 3:
            continue
        label = fields[0].lower()
        if label in ("surface", "side"):
            seen_header = True
            continue
        values = _as_floats(fields[1:3])
        if values is None or len(values) < 2:
            continue
        side = SURFACE_LABELS.get(label)
        if side == "upper":
            upper.append((values[0], values[1]))
        elif side == "lower":
            lower.append((values[0], values[1]))

    if not seen_header or len(upper) < 3 or len(lower) < 3:
        return None
    return _oriented(upper), _oriented(lower)


#: Étiquettes de surface reconnues dans un CSV.
SURFACE_LABELS = {
    "extrados": "upper", "upper": "upper", "up": "upper", "haut": "upper",
    "intrados": "lower", "lower": "lower", "low": "lower", "bas": "lower",
}


def _guess_name(textual: Sequence[str], path: Path) -> str:
    """Nom du profil : la ligne de titre du fichier, ou le nom du fichier.

    Dans un CSV étiqueté, chaque ligne de données commence par un mot — elle
    n'est donc pas numérique et se retrouve parmi les candidats. Sans ce
    filtre, le profil s'appellerait « extrados,0.000000,0.000000 ».
    """
    for line in textual:
        cleaned = line.strip()
        if not cleaned:
            continue
        first = _split_numbers(cleaned)[0].strip().lower()
        if first in SURFACE_LABELS or first in ("x", "surface", "side"):
            continue
        return cleaned
    return path.stem


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="profiles/loader.py",
        description="Charge et inspecte un fichier de profil 2D (Selig, Lednicer, CSV).",
    )
    parser.add_argument("fichier")
    parser.add_argument("--name", default=None, help="nom du profil")
    parser.add_argument("--json", action="store_true", help="sortie brute")
    parser.add_argument(
        "--close-te-below", type=float, default=1e-4,
        help="écart de bord de fuite en deçà duquel il est refermé",
    )
    args = parser.parse_args(argv)

    result = load_profile(args.fichier, args.name, args.close_te_below)

    if args.json:
        print(json.dumps(result.summary(), indent=2, ensure_ascii=False, default=str))
        return 0 if result.success else 1

    if not result.success:
        print(f"ÉCHEC [{result.status}] {result.message}", file=sys.stderr)
        return 1

    profile = result.profile
    measures = profile.measures()
    print(f"Profil          : {profile.name}")
    print(f"Format          : {result.metadata['format']}")
    print(f"Points          : {measures['n_points']} "
          f"({measures['n_upper']} extrados, {measures['n_lower']} intrados)")
    print(f"Épaisseur max   : {measures['max_thickness']:.4f} c "
          f"à {measures['max_thickness_position']:.1%} de corde")
    print(f"Cambrure max    : {measures['max_camber']:+.4f} c "
          f"à {measures['max_camber_position']:.1%} de corde")
    print(f"Bord de fuite   : {measures['trailing_edge_gap']:.5f} c")
    print(f"Rayon de nez    : {measures['leading_edge_radius']:.5f} c")
    if abs(profile.transform.rotation_deg) > 1e-6:
        print(f"Incidence retirée : {profile.transform.rotation_deg:+.3f}°")
    for warning in result.warnings:
        print(f"  [avertissement] {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
