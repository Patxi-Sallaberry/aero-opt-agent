"""Graphiques SVG sans dépendance, pour les rapports d'optimisation.

Matplotlib ferait cela en trois lignes, mais l'ajouter au projet pour tracer
quatre courbes coûterait une dépendance lourde là où quelques centaines de
lignes suffisent. Le SVG est du texte : il s'intègre directement dans un HTML
autonome, reste lisible à toute échelle, et se relit à la main en cas de doute.

Les fonctions rendent une chaîne SVG, que l'appelant écrit ou intègre.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

# Palette : lisible sur fond clair, distinguable en noir et blanc.
COLORS = ("#1f4e8c", "#c1440e", "#2e7d32", "#7b1fa2", "#ef6c00", "#00838f")
GRID = "#dcdcdc"
AXIS = "#5a5a5a"
TEXT = "#222222"
BACKGROUND = "#ffffff"


def _nice_step(span: float, target_ticks: int = 6) -> float:
    """Pas de graduation « rond » couvrant `span` en ~target_ticks intervalles."""
    if span <= 0:
        return 1.0
    raw = span / max(1, target_ticks)
    magnitude = 10 ** math.floor(math.log10(raw))
    for factor in (1, 2, 2.5, 5, 10):
        if raw <= factor * magnitude:
            return factor * magnitude
    return 10 * magnitude


def _ticks(low: float, high: float, target: int = 6) -> list[float]:
    if high <= low:
        return [low]
    step = _nice_step(high - low, target)
    start = math.floor(low / step) * step
    values, value = [], start
    while value <= high + step * 1e-9:
        if value >= low - step * 1e-9:
            values.append(round(value, 12))
        value += step
    return values or [low, high]


def _format_tick(value: float) -> str:
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= 1000 or magnitude < 0.001:
        return f"{value:.1e}"
    if magnitude >= 100:
        return f"{value:.0f}"
    if magnitude >= 10:
        return f"{value:.1f}"
    if magnitude >= 1:
        return f"{value:.2f}"
    return f"{value:.3f}"


def _escape(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def chart(
    series: Sequence[Mapping[str, Any]],
    title: str = "",
    x_label: str = "",
    y_label: str = "",
    width: int = 760,
    height: int = 420,
    invert_y: bool = False,
    y_zero_line: bool = False,
    x_range: tuple[float, float] | None = None,
    y_range: tuple[float, float] | None = None,
) -> str:
    """Trace une ou plusieurs séries.

    Args:
        series: chaque entrée porte `points` [(x, y), ...], `label`, et
            éventuellement `color`, `mode` ("line" ou "points"), `dashed`.
        invert_y: axe des ordonnées vers le bas. Convention obligatoire pour un
            Cp, où la dépression — donc la portance — se lit vers le haut.
        y_zero_line: souligne l'ordonnée nulle, utile quand le signe compte.
    """
    usable = [s for s in series if s.get("points")]
    if not usable:
        return _empty_chart(title, width, height)

    left, right, top, bottom = 78, 26, 46 if title else 20, 58
    plot_w = width - left - right
    plot_h = height - top - bottom

    xs = [p[0] for s in usable for p in s["points"]]
    ys = [p[1] for s in usable for p in s["points"]]
    x_min, x_max = x_range or (min(xs), max(xs))
    y_min, y_max = y_range or (min(ys), max(ys))
    if x_max - x_min < 1e-12:
        x_min, x_max = x_min - 0.5, x_max + 0.5
    if y_max - y_min < 1e-12:
        y_min, y_max = y_min - 0.5, y_max + 0.5
    if y_range is None:
        margin = (y_max - y_min) * 0.08
        y_min, y_max = y_min - margin, y_max + margin

    def sx(x: float) -> float:
        return left + (x - x_min) / (x_max - x_min) * plot_w

    def sy(y: float) -> float:
        fraction = (y - y_min) / (y_max - y_min)
        if invert_y:
            return top + fraction * plot_h
        return top + plot_h - fraction * plot_h

    out: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="system-ui, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="{BACKGROUND}"/>',
    ]
    if title:
        out.append(
            f'<text x="{width / 2:.1f}" y="26" text-anchor="middle" '
            f'font-size="15" font-weight="600" fill="{TEXT}">{_escape(title)}</text>'
        )

    # Grille et graduations
    for value in _ticks(x_min, x_max):
        if not (x_min - 1e-12 <= value <= x_max + 1e-12):
            continue
        x = sx(value)
        out.append(
            f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{top + plot_h}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{x:.1f}" y="{top + plot_h + 18}" text-anchor="middle" '
            f'font-size="11" fill="{AXIS}">{_format_tick(value)}</text>'
        )
    for value in _ticks(y_min, y_max):
        if not (y_min - 1e-12 <= value <= y_max + 1e-12):
            continue
        y = sy(value)
        out.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
            f'stroke="{GRID}" stroke-width="1"/>'
        )
        out.append(
            f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="{AXIS}">{_format_tick(value)}</text>'
        )

    if y_zero_line and y_min < 0 < y_max:
        y = sy(0.0)
        out.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" '
            f'stroke="{AXIS}" stroke-width="1.2" stroke-dasharray="4 3"/>'
        )

    out.append(
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" '
        f'fill="none" stroke="{AXIS}" stroke-width="1.2"/>'
    )

    # Séries
    for index, serie in enumerate(usable):
        color = serie.get("color") or COLORS[index % len(COLORS)]
        points = serie["points"]
        if serie.get("mode") == "points":
            for x, y in points:
                out.append(
                    f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="2" '
                    f'fill="{color}" fill-opacity="0.75"/>'
                )
        else:
            path = " ".join(
                f'{"M" if i == 0 else "L"}{sx(x):.1f},{sy(y):.1f}'
                for i, (x, y) in enumerate(points)
            )
            dash = ' stroke-dasharray="6 4"' if serie.get("dashed") else ""
            out.append(
                f'<path d="{path}" fill="none" stroke="{color}" '
                f'stroke-width="2" stroke-linejoin="round"{dash}/>'
            )
            if serie.get("markers"):
                for x, y in points:
                    out.append(
                        f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3" '
                        f'fill="{color}"/>'
                    )
        for marker in serie.get("highlight", []):
            x, y = marker
            out.append(
                f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="5.5" '
                f'fill="none" stroke="{color}" stroke-width="2.5"/>'
            )

    # Légende
    labelled = [s for s in usable if s.get("label")]
    if labelled:
        legend_x, legend_y = left + 10, top + 14
        # Fond opaque : sans lui, les courbes traversent le texte de la
        # légende, qui devient illisible là où le tracé est dense.
        legend_w = 42 + max(len(str(s["label"])) for s in labelled) * 6.6
        out.append(
            f'<rect x="{legend_x - 6}" y="{legend_y - 15}" '
            f'width="{legend_w:.0f}" height="{len(labelled) * 17 + 8}" '
            f'fill="{BACKGROUND}" fill-opacity="0.86" stroke="{GRID}" '
            f'stroke-width="1" rx="3"/>'
        )
        for index, serie in enumerate(labelled):
            color = serie.get("color") or COLORS[
                usable.index(serie) % len(COLORS)
            ]
            y = legend_y + index * 17
            out.append(
                f'<line x1="{legend_x}" y1="{y - 4}" x2="{legend_x + 22}" '
                f'y2="{y - 4}" stroke="{color}" stroke-width="2.5"/>'
            )
            out.append(
                f'<text x="{legend_x + 28}" y="{y}" font-size="12" '
                f'fill="{TEXT}">{_escape(serie["label"])}</text>'
            )

    if x_label:
        out.append(
            f'<text x="{left + plot_w / 2:.1f}" y="{height - 14}" '
            f'text-anchor="middle" font-size="12" fill="{TEXT}">'
            f'{_escape(x_label)}</text>'
        )
    if y_label:
        cy = top + plot_h / 2
        out.append(
            f'<text x="18" y="{cy:.1f}" text-anchor="middle" font-size="12" '
            f'fill="{TEXT}" transform="rotate(-90 18 {cy:.1f})">'
            f'{_escape(y_label)}</text>'
        )

    out.append("</svg>")
    return "\n".join(out)


def _empty_chart(title: str, width: int, height: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="system-ui, sans-serif">'
        f'<rect width="{width}" height="{height}" fill="{BACKGROUND}"/>'
        f'<text x="{width / 2}" y="{height / 2}" text-anchor="middle" '
        f'font-size="13" fill="{AXIS}">{_escape(title)} — aucune donnée</text></svg>'
    )


def airfoil_comparison(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    width: int = 900,
    height: int = 330,
    title: str = "",
) -> str:
    """Deux sections côte à côte, à la MÊME échelle.

    L'échelle commune n'est pas un détail : mise à la taille de son cadre,
    chaque section paraîtrait identique, et une corde 15 % plus longue ou une
    incidence de 5° passeraient inaperçues. C'est précisément ce que la figure
    doit montrer.
    """
    panels = [before, after]
    all_points = [p for panel in panels for p in panel["upper"] + panel["lower"]]
    if not all_points:
        return _empty_chart(title, width, height)

    x_min = min(p[0] for p in all_points)
    x_max = max(p[0] for p in all_points)
    y_min = min(p[1] for p in all_points)
    y_max = max(p[1] for p in all_points)
    span_x = max(x_max - x_min, 1e-9)
    span_y = max(y_max - y_min, 1e-9)

    top = 46 if title else 16
    label_h = 46
    margin = 22
    panel_w = (width - 3 * margin) / 2
    panel_h = height - top - label_h - margin
    scale = min(panel_w / span_x, panel_h / span_y) * 0.92

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="system-ui, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="{BACKGROUND}"/>',
    ]
    if title:
        out.append(
            f'<text x="{width / 2}" y="26" text-anchor="middle" font-size="15" '
            f'font-weight="600" fill="{TEXT}">{_escape(title)}</text>'
        )

    for index, panel in enumerate(panels):
        color = COLORS[1] if index == 0 else COLORS[0]
        left = margin + index * (panel_w + margin)
        offset_x = left + (panel_w - span_x * scale) / 2
        offset_y = top + (panel_h - span_y * scale) / 2

        def sx(x: float, _o=offset_x) -> float:
            return _o + (x - x_min) * scale

        def sy(y: float, _o=offset_y) -> float:
            return _o + (y_max - y) * scale

        out.append(
            f'<rect x="{left:.1f}" y="{top}" width="{panel_w:.1f}" '
            f'height="{panel_h:.1f}" fill="#fbfbfc" stroke="{GRID}" '
            f'stroke-width="1" rx="4"/>'
        )
        upper, lower = panel["upper"], panel["lower"]
        out.append(
            f'<line x1="{sx(upper[0][0]):.2f}" y1="{sy(upper[0][1]):.2f}" '
            f'x2="{sx(upper[-1][0]):.2f}" y2="{sy(upper[-1][1]):.2f}" '
            f'stroke="{AXIS}" stroke-width="1" stroke-dasharray="5 4"/>'
        )
        contour = list(upper) + list(reversed(lower))
        path = " ".join(
            f'{"M" if i == 0 else "L"}{sx(x):.2f},{sy(y):.2f}'
            for i, (x, y) in enumerate(contour)
        ) + " Z"
        out.append(
            f'<path d="{path}" fill="{color}" fill-opacity="0.16" '
            f'stroke="{color}" stroke-width="2" stroke-linejoin="round"/>'
        )

        cx = left + panel_w / 2
        out.append(
            f'<text x="{cx:.1f}" y="{top + panel_h + 22:.1f}" '
            f'text-anchor="middle" font-size="13" font-weight="600" '
            f'fill="{color}">{_escape(panel.get("label", ""))}</text>'
        )
        out.append(
            f'<text x="{cx:.1f}" y="{top + panel_h + 40:.1f}" '
            f'text-anchor="middle" font-size="11" fill="{AXIS}">'
            f'{_escape(panel.get("caption", ""))}</text>'
        )

    out.append("</svg>")
    return "\n".join(out)


def airfoil_overlay(
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    width: int = 900,
    height: int = 300,
    title: str = "",
) -> str:
    """Les deux sections superposées, bords d'attaque confondus.

    Le côte à côte montre chaque forme ; la superposition montre l'ÉCART, qui
    est ce qu'on cherche à lire.
    """
    all_points = [
        p for panel in (before, after) for p in panel["upper"] + panel["lower"]
    ]
    if not all_points:
        return _empty_chart(title, width, height)

    x_min = min(p[0] for p in all_points)
    x_max = max(p[0] for p in all_points)
    y_min = min(p[1] for p in all_points)
    y_max = max(p[1] for p in all_points)
    span_x = max(x_max - x_min, 1e-9)
    span_y = max(y_max - y_min, 1e-9)

    top = 46 if title else 16
    margin = 28
    legend_h = 30
    scale = min(
        (width - 2 * margin) / span_x,
        (height - top - margin - legend_h) / span_y,
    )
    offset_x = (width - span_x * scale) / 2
    offset_y = top + (height - top - margin - legend_h - span_y * scale) / 2

    def sx(x: float) -> float:
        return offset_x + (x - x_min) * scale

    def sy(y: float) -> float:
        return offset_y + (y_max - y) * scale

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="system-ui, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="{BACKGROUND}"/>',
    ]
    if title:
        out.append(
            f'<text x="{width / 2}" y="26" text-anchor="middle" font-size="15" '
            f'font-weight="600" fill="{TEXT}">{_escape(title)}</text>'
        )

    for index, panel in enumerate((before, after)):
        color = COLORS[1] if index == 0 else COLORS[0]
        contour = list(panel["upper"]) + list(reversed(panel["lower"]))
        path = " ".join(
            f'{"M" if i == 0 else "L"}{sx(x):.2f},{sy(y):.2f}'
            for i, (x, y) in enumerate(contour)
        ) + " Z"
        dash = ' stroke-dasharray="7 4"' if index == 0 else ""
        out.append(
            f'<path d="{path}" fill="{color}" fill-opacity="0.10" '
            f'stroke="{color}" stroke-width="2.2" stroke-linejoin="round"{dash}/>'
        )
        legend_x = width / 2 - 150 + index * 170
        legend_y = height - 14
        out.append(
            f'<line x1="{legend_x}" y1="{legend_y - 4}" x2="{legend_x + 26}" '
            f'y2="{legend_y - 4}" stroke="{color}" stroke-width="2.5"{dash}/>'
        )
        out.append(
            f'<text x="{legend_x + 32}" y="{legend_y}" font-size="12" '
            f'fill="{TEXT}">{_escape(panel.get("label", ""))}</text>'
        )

    out.append("</svg>")
    return "\n".join(out)


def comparison_bars(
    groups: Sequence[Mapping[str, Any]],
    width: int = 900,
    height: int = 300,
    title: str = "",
    before_label: str = "avant",
    after_label: str = "après",
) -> str:
    """Barres avant / après, un panneau par grandeur.

    Chaque grandeur a son propre panneau et sa propre échelle : mettre un Cd de
    0,026 et une finesse de 30 sur un même axe écraserait le premier à zéro.

    La couleur suit l'AMÉLIORATION, pas le sens de variation : pour une
    traînée, baisser est un gain. Un vert sur une barre plus courte se lit
    correctement ; un rouge y ferait croire à une régression.

    Chaque groupe porte `label`, `before`, `after`, `better` ("higher" ou
    "lower"), et un `format` optionnel.
    """
    usable = [
        g for g in groups
        if isinstance(g.get("before"), (int, float))
        and isinstance(g.get("after"), (int, float))
    ]
    if not usable:
        return _empty_chart(title, width, height)

    good, bad = "#2e7d32", "#c1440e"
    top = 46 if title else 16
    margin = 20
    panel_w = (width - margin * (len(usable) + 1)) / len(usable)
    footer = 64
    panel_h = height - top - footer

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="system-ui, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="{BACKGROUND}"/>',
    ]
    if title:
        out.append(
            f'<text x="{width / 2}" y="26" text-anchor="middle" font-size="15" '
            f'font-weight="600" fill="{TEXT}">{_escape(title)}</text>'
        )

    for index, group in enumerate(usable):
        before = float(group["before"])
        after = float(group["after"])
        spec = group.get("format", ".4g")
        higher_is_better = group.get("better", "higher") == "higher"
        improved = (after > before) if higher_is_better else (after < before)
        color = good if improved else bad

        left = margin + index * (panel_w + margin)
        # Assez de dégagement au dessus des barres pour que l'étiquette de
        # valeur de la plus haute ne vienne pas percuter le titre du panneau.
        top_of_bars = top + 48
        bars_h = panel_h - 48
        reference = max(abs(before), abs(after), 1e-12)

        out.append(
            f'<rect x="{left:.1f}" y="{top}" width="{panel_w:.1f}" '
            f'height="{panel_h:.1f}" fill="#fbfbfc" stroke="{GRID}" '
            f'stroke-width="1" rx="4"/>'
        )

        bar_w = panel_w * 0.26
        gap = panel_w * 0.14
        x_before = left + panel_w / 2 - bar_w - gap / 2
        x_after = left + panel_w / 2 + gap / 2

        for x, value, label, fill in (
            (x_before, before, before_label, "#9aa5b1"),
            (x_after, after, after_label, color),
        ):
            bar_h = max(2.0, abs(value) / reference * (bars_h - 34))
            y = top_of_bars + (bars_h - 34) - bar_h
            out.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w:.1f}" '
                f'height="{bar_h:.1f}" fill="{fill}" rx="2"/>'
            )
            out.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" '
                f'text-anchor="middle" font-size="12" font-weight="600" '
                f'fill="{TEXT}">{format(value, spec)}</text>'
            )
            out.append(
                f'<text x="{x + bar_w / 2:.1f}" y="{top_of_bars + bars_h - 14:.1f}" '
                f'text-anchor="middle" font-size="11" fill="{AXIS}">'
                f'{_escape(label)}</text>'
            )

        change = (after - before) / abs(before) * 100 if before else 0.0
        verdict = "mieux" if improved else "moins bien"
        out.append(
            f'<text x="{left + panel_w / 2:.1f}" y="{top + 20:.1f}" '
            f'text-anchor="middle" font-size="13" font-weight="600" '
            f'fill="{TEXT}">{_escape(group.get("label", ""))}</text>'
        )
        out.append(
            f'<text x="{left + panel_w / 2:.1f}" y="{height - 34:.1f}" '
            f'text-anchor="middle" font-size="15" font-weight="700" '
            f'fill="{color}">{change:+.1f} %</text>'
        )
        out.append(
            f'<text x="{left + panel_w / 2:.1f}" y="{height - 16:.1f}" '
            f'text-anchor="middle" font-size="11" fill="{AXIS}">'
            f'{_escape(verdict)}</text>'
        )

    out.append("</svg>")
    return "\n".join(out)


def airfoil_outline(
    upper: Sequence[tuple[float, float]],
    lower: Sequence[tuple[float, float]],
    width: int = 760,
    height: int = 260,
    title: str = "",
) -> str:
    """Dessine la section, à l'échelle 1:1 pour ne pas déformer l'incidence."""
    points = list(upper) + list(lower)
    if not points:
        return _empty_chart(title, width, height)

    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    span_x = max(x_max - x_min, 1e-9)
    span_y = max(y_max - y_min, 1e-9)

    margin = 34
    top_margin = margin + (20 if title else 0)
    # Une seule échelle sur les deux axes : un profil étiré donnerait une
    # fausse idée de son épaisseur comme de son incidence.
    scale = min(
        (width - 2 * margin) / span_x, (height - top_margin - margin) / span_y
    )
    offset_x = (width - span_x * scale) / 2
    offset_y = top_margin + (height - top_margin - margin - span_y * scale) / 2

    def sx(x: float) -> float:
        return offset_x + (x - x_min) * scale

    def sy(y: float) -> float:
        return offset_y + (y_max - y) * scale

    contour = list(upper) + list(reversed(lower))
    path = " ".join(
        f'{"M" if i == 0 else "L"}{sx(x):.2f},{sy(y):.2f}'
        for i, (x, y) in enumerate(contour)
    ) + " Z"

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" font-family="system-ui, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="{BACKGROUND}"/>',
    ]
    if title:
        out.append(
            f'<text x="{width / 2}" y="24" text-anchor="middle" font-size="15" '
            f'font-weight="600" fill="{TEXT}">{_escape(title)}</text>'
        )
    # Corde, du bord d'attaque au bord de fuite.
    out.append(
        f'<line x1="{sx(upper[0][0]):.2f}" y1="{sy(upper[0][1]):.2f}" '
        f'x2="{sx(upper[-1][0]):.2f}" y2="{sy(upper[-1][1]):.2f}" '
        f'stroke="{AXIS}" stroke-width="1" stroke-dasharray="5 4"/>'
    )
    out.append(
        f'<path d="{path}" fill="{COLORS[0]}" fill-opacity="0.13" '
        f'stroke="{COLORS[0]}" stroke-width="2" stroke-linejoin="round"/>'
    )
    out.append("</svg>")
    return "\n".join(out)
