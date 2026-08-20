"""Phase 5 — l'ordre de sondage sur un grand espace de conception.

Un profil NACA offre cinq paramètres : un cycle complet de sondage en coûte
dix, et tout finit par être essayé quel que soit l'ordre. Un profil CST en
offre vingt-sept : le cycle en coûte cinquante-quatre, et ce qui est sondé en
dernier n'est jamais sondé.

Le cas qui a imposé ces règles : sur une série Clark Y de onze itérations,
`aoa` — le levier le plus direct sur la portance — n'a JAMAIS eu son tour. Il
figurait après les vingt-quatre coefficients dans l'ordre de déclaration du
fichier, un ordre qui n'a aucun sens physique.

La correction ne doit toucher que ce cas. Sur un petit espace, l'ordre établi
de la v1.0 reste inchangé.
"""

from __future__ import annotations

from agent.orchestrator import (
    LARGE_SPACE_THRESHOLD,
    PROBE_PRIORITY,
    _probe_order,
)


def spec(value: float, low: float, high: float) -> dict:
    return {"value": value, "min": low, "max": high,
            "max_delta_pct": 10.0, "unit": "unitless"}


def cst_parameters(order: int = 11) -> dict:
    """Un espace CST réaliste : les coefficients d'abord, l'incidence en fin."""
    params: dict[str, dict] = {}
    for surface in ("upper", "lower"):
        for index in range(order + 1):
            params[f"cst_{surface}_{index}"] = spec(0.2, 0.1, 0.3)
    params["chord"] = spec(300.0, 280.0, 320.0)
    params["span"] = spec(80.0, 79.0, 81.0)
    params["aoa"] = spec(3.0, -2.0, 12.0)
    return params


def naca_parameters() -> dict:
    return {
        "chord": spec(300.0, 280.0, 320.0),
        "span": spec(80.0, 79.0, 81.0),
        "thickness": spec(0.12, 0.10, 0.14),
        "camber": spec(0.02, 0.0, 0.04),
        "aoa": spec(0.0, -2.0, 12.0),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Grand espace
# ─────────────────────────────────────────────────────────────────────────────


def test_l_incidence_est_sondee_en_premier_sur_un_espace_cst():
    """Le défaut observé : elle passait après vingt-quatre coefficients."""
    params = cst_parameters()
    ordre = _probe_order(list(params), [], params)
    assert ordre[0] == "aoa"


def test_la_corde_suit_immediatement():
    params = cst_parameters()
    ordre = _probe_order(list(params), [], params)
    assert ordre[1] == "chord"


def test_les_coefficients_gardent_leur_ordre_entre_eux():
    """Un coefficient n'a pas de raison de passer devant son voisin."""
    params = cst_parameters()
    ordre = _probe_order(list(params), [], params)
    coefficients = [n for n in ordre if n.startswith("cst_")]
    attendu = [n for n in params if n.startswith("cst_")]
    assert coefficients == attendu


def test_aucun_parametre_n_est_perdu_dans_le_reordonnancement():
    params = cst_parameters()
    ordre = _probe_order(list(params), [], params)
    assert sorted(ordre) == sorted(params)


# ─────────────────────────────────────────────────────────────────────────────
# Petit espace : le comportement de la v1.0 est intact
# ─────────────────────────────────────────────────────────────────────────────


def test_un_petit_espace_garde_l_ordre_declare():
    """Avec cinq paramètres, tout est sondé de toute façon : rien à changer.

    La v1.5 ne doit pas modifier le comportement de la v1.0 là où il n'y a rien
    à corriger.
    """
    params = naca_parameters()
    ordre = _probe_order(list(params), [], params)
    assert ordre == list(params)


def test_le_seuil_separe_bien_les_deux_regimes():
    assert len(naca_parameters()) <= LARGE_SPACE_THRESHOLD
    assert len(cst_parameters()) > LARGE_SPACE_THRESHOLD


def test_le_seuil_est_franchi_exactement_ou_il_faut():
    """Juste sous le seuil : ordre déclaré. Juste au dessus : priorité."""
    params = {f"p{i}": spec(1.0, 0.0, 2.0) for i in range(LARGE_SPACE_THRESHOLD - 1)}
    params["aoa"] = spec(3.0, -2.0, 12.0)
    assert len(params) == LARGE_SPACE_THRESHOLD
    assert _probe_order(list(params), [], params)[0] != "aoa"

    params["chord"] = spec(300.0, 280.0, 320.0)
    assert len(params) == LARGE_SPACE_THRESHOLD + 1
    assert _probe_order(list(params), [], params)[0] == "aoa"


# ─────────────────────────────────────────────────────────────────────────────
# La priorité ne survit pas à la mesure
# ─────────────────────────────────────────────────────────────────────────────


def test_un_parametre_deja_mesure_passe_derriere_l_inexplore():
    """La priorité ne vaut que faute de mieux : une mesure la remplace.

    Une fois `aoa` sondé, il rejoint les paramètres classés par influence
    mesurée. Le laisser en tête indéfiniment reviendrait à ne jamais explorer
    le reste.
    """
    params = cst_parameters()
    history = [
        {"iteration": 0, "objective": 30.0,
         "values": {n: float(s["value"]) for n, s in params.items()}},
        {"iteration": 1, "objective": 31.0,
         "values": {**{n: float(s["value"]) for n, s in params.items()},
                    "aoa": 4.0}},
    ]
    ordre = _probe_order(list(params), history, params)
    assert ordre[0] != "aoa"
    assert "aoa" in ordre


def test_la_priorite_ne_nomme_que_des_grandeurs_physiques():
    """Y glisser un coefficient CST n'aurait aucun fondement."""
    assert all(not name.startswith("cst_") for name in PROBE_PRIORITY)
