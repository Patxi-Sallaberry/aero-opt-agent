"""§4 — le contrôle explicite des trois grandeurs de forme.

Le document exige que le rayon de bord d'attaque, l'épaisseur maximale et
l'épaisseur de bord de fuite « restent sous contrôle explicite ».

Les bornes des coefficients CST les contiennent déjà indirectement : chaque
coefficient ne peut déplacer sa surface que de 1,5 % de corde. Mais
indirectement ne suffit pas, pour une raison précise — rien n'empêche DEUX
coefficients de conspirer dans le même sens, et surtout rien ne le signalerait.
Un profil qui s'amincirait de 12 % à 6 % au fil de vingt itérations passerait
toutes les portes existantes.

Ces bornes-ci sont donc vérifiées sur la forme RECONSTRUITE, à chaque
itération, et une violation fait échouer l'itération franchement.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.geometry_validator import (
    GEOMETRIC_CONSTRAINTS,
    _check_geometric_constraints,
    profile_measures,
)
from profiles.reparameterize import (
    CONSTRAINT_MARGIN,
    TRAILING_EDGE_CEILING,
    reparameterize,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "profiles"


def design(name: str = "clarky") -> dict:
    result = reparameterize(EXAMPLES / f"{name}.dat", chord_mm=300.0, aoa_deg=3.0)
    assert result.success, result.message
    return result.design_params


# ─────────────────────────────────────────────────────────────────────────────
# Ce qui est mesuré
# ─────────────────────────────────────────────────────────────────────────────


def test_les_trois_grandeurs_sont_mesurables_sur_un_profil_cst():
    measures = profile_measures(design())
    assert measures is not None
    assert {"thickness", "leading_edge_radius", "trailing_edge_thickness"} <= set(
        measures
    )


def test_l_epaisseur_mesuree_vaut_celle_du_clark_y():
    """Vérifié contre la géométrie connue, pas contre la sortie du code."""
    measures = profile_measures(design())
    assert measures["thickness"] == pytest.approx(0.117, abs=5e-3)


def test_le_bord_de_fuite_epaissi_du_clark_y_est_vu():
    """Le Clark Y en a un : le mesurer à zéro trahirait une erreur."""
    measures = profile_measures(design())
    assert measures["trailing_edge_thickness"] > 1e-4


def test_les_grandeurs_sont_mesurees_sur_la_forme_pas_lues_dans_le_fichier():
    """Sur un profil CST, aucune de ces trois valeurs n'est un paramètre."""
    params = design()["parameters"]
    assert "thickness" not in params
    assert not any("radius" in name for name in params)


def test_un_profil_naca_reste_mesurable():
    """La v1.0 ne doit rien perdre : ses formes se mesurent aussi."""
    measures = profile_measures({
        "parameters": {
            "chord": {"value": 300.0, "unit": "mm"},
            "span": {"value": 80.0, "unit": "mm"},
            "thickness": {"value": 0.12, "unit": "unitless"},
            "camber": {"value": 0.02, "unit": "unitless"},
            "aoa": {"value": 3.0, "unit": "deg"},
        },
    })
    assert measures is not None
    assert measures["thickness"] == pytest.approx(0.12, abs=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# Ce que la vérification refuse
# ─────────────────────────────────────────────────────────────────────────────


def test_un_profil_conforme_ne_declenche_rien():
    """La forme de départ doit évidemment satisfaire ses propres bornes."""
    assert _check_geometric_constraints(design(), {}) == []


def test_un_profil_trop_fin_est_refuse():
    data = design()
    data["constraints"]["min_thickness_ratio"] = 0.5
    problems = _check_geometric_constraints(data, {})
    assert problems and "épaisseur relative" in problems[0]
    assert "sous le minimum" in problems[0]


def test_un_profil_trop_epais_est_refuse():
    data = design()
    data["constraints"]["max_thickness_ratio"] = 0.01
    problems = _check_geometric_constraints(data, {})
    assert problems and "au dessus du maximum" in problems[0]


def test_un_nez_trop_aigu_est_refuse():
    """Un nez qui s'aiguise fait décrocher brutalement."""
    data = design()
    data["constraints"]["min_leading_edge_radius"] = 0.5
    problems = _check_geometric_constraints(data, {})
    assert problems and "rayon de bord d'attaque" in problems[0]


def test_un_bord_de_fuite_trop_epais_est_refuse():
    data = design()
    data["constraints"]["max_trailing_edge_thickness"] = 1e-6
    problems = _check_geometric_constraints(data, {})
    assert problems and "bord de fuite" in problems[0]


def test_plusieurs_violations_sont_toutes_rapportees():
    """Corriger une contrainte pour découvrir la suivante ferait perdre du temps."""
    data = design()
    data["constraints"]["min_thickness_ratio"] = 0.5
    data["constraints"]["min_leading_edge_radius"] = 0.5
    assert len(_check_geometric_constraints(data, {})) == 2


def test_le_rapport_conserve_les_grandeurs_mesurees():
    """Pour qu'on puisse voir dériver une forme AVANT qu'elle ne viole."""
    report: dict = {}
    _check_geometric_constraints(design(), report)
    assert "profile_measures" in report
    assert report["profile_measures"]["thickness"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# Ce qui reste silencieux
# ─────────────────────────────────────────────────────────────────────────────


def test_un_fichier_sans_contrainte_geometrique_ne_declenche_rien():
    """Les fichiers de la v1.0 n'en déclarent aucune et restent valides."""
    data = design()
    for key, _, _ in GEOMETRIC_CONSTRAINTS:
        data["constraints"].pop(key, None)
    assert _check_geometric_constraints(data, {}) == []


def test_une_contrainte_illisible_est_signalee_pas_ignoree():
    data = design()
    data["constraints"]["max_thickness_ratio"] = "épais"
    problems = _check_geometric_constraints(data, {})
    assert problems and "nombre attendu" in problems[0]


def test_une_forme_non_mesurable_est_signalee_pas_ignoree():
    """Une contrainte déclarée qu'on ne peut pas vérifier doit se voir."""
    problems = _check_geometric_constraints(
        {"parameters": {}, "constraints": {"max_thickness_ratio": 0.2}}, {}
    )
    assert problems and "impossible de les vérifier" in problems[0]


# ─────────────────────────────────────────────────────────────────────────────
# Les bornes écrites par la re-paramétrisation
# ─────────────────────────────────────────────────────────────────────────────


def test_la_reparametrisation_ecrit_les_trois_bornes():
    constraints = design()["constraints"]
    for key, _, _ in GEOMETRIC_CONSTRAINTS:
        assert key in constraints


def test_les_bornes_encadrent_la_forme_de_depart():
    """Une borne que la forme initiale viole rendrait la boucle impossible."""
    data = design()
    measures = profile_measures(data)
    constraints = data["constraints"]
    assert constraints["min_thickness_ratio"] < measures["thickness"]
    assert measures["thickness"] < constraints["max_thickness_ratio"]
    assert constraints["min_leading_edge_radius"] < measures["leading_edge_radius"]
    assert measures["leading_edge_radius"] < constraints["max_leading_edge_radius"]


def test_la_marge_laisse_travailler_sans_laisser_deriver():
    """Ni si serrée qu'elle bloque, ni si large qu'elle ne veut plus rien dire."""
    assert 0.2 <= CONSTRAINT_MARGIN <= 0.6


def test_le_bord_de_fuite_ferme_recoit_une_borne_absolue():
    """Une marge proportionnelle sur zéro donnerait zéro : la forme serait figée."""
    constraints = design("naca2412")["constraints"]
    assert constraints["max_trailing_edge_thickness"] == pytest.approx(
        TRAILING_EDGE_CEILING
    )
