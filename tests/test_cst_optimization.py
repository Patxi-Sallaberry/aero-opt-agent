"""Phase 5 — ce qu'il faut pour qu'un optimiseur puisse toucher aux coefficients.

Faire varier un coefficient CST n'est pas anodin. Rien dans la formulation
n'empêche l'intrados de passer au dessus de l'extrados : c'est une somme de
polynômes, pas un solide. Et sur un profil réel, la forme tient souvent par
compensation entre grands termes — le Clark Y à l'ordre 11 a des coefficients
qui valent 0,13 et d'autres 3,27.

Trois garde-fous en découlent, et ce fichier les tient :

**Les bornes se déduisent de l'effet géométrique**, pas de la valeur du
coefficient. Sans cela, un coefficient valant 3,27 recevrait une marge de
±1,63, soit 65 % de corde de déplacement.

**La forme est contrôlée avant écriture.** Une itération franchement en échec
vaut mieux qu'un STL aux facettes croisées dont tout l'aval s'accommoderait.

**Un refus dit quoi faire.** L'ajustement étant linéaire, chercher l'ordre qui
passerait coûte quelques millisecondes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from profiles.cst import DEFAULT_ORDER, cosine_stations, fit_profile
from profiles.geometry import (
    MIN_RELATIVE_THICKNESS,
    check_shape,
    cst_profile,
)
from profiles.loader import load_profile
from profiles.reparameterize import (
    COEFFICIENT_SHAPE_BUDGET,
    EDGE_SHAPE_BUDGET,
    MIN_POINTS_PER_COEFFICIENT,
    build_design_params,
    coefficient_authority,
    reparameterize,
    suggest_order,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "profiles"

#: Profils réels, tirés de la base UIUC. Le Clark Y est assez dense pour être
#: ajusté ; l'E387 est un fichier de soixante et un points, trop grossier — et
#: c'est justement pour cela qu'il est ici.
DENSE_PROFILE = "clarky"
SPARSE_PROFILE = "e387"


def load(name: str):
    result = load_profile(EXAMPLES / f"{name}.dat")
    assert result.success, result.message
    return result.profile


# ─────────────────────────────────────────────────────────────────────────────
# L'autorité géométrique d'un coefficient
# ─────────────────────────────────────────────────────────────────────────────


def test_l_autorite_mesure_bien_le_deplacement_de_surface():
    """max_ψ [C(ψ)·Bᵢ(ψ)] doit être exactement le déplacement par unité de A.

    C'est l'identité sur laquelle reposent toutes les bornes : si elle est
    fausse, les marges accordées n'ont plus de sens géométrique.
    """
    order, index = 11, 5
    base = [0.2] * (order + 1)
    perturbe = list(base)
    perturbe[index] += 1.0

    avant = cst_profile(base, [-c for c in base]).upper
    apres = cst_profile(perturbe, [-c for c in base]).upper
    ecart = max(
        abs(apres.evaluate(psi) - avant.evaluate(psi))
        for psi in cosine_stations(400)
    )
    assert ecart == pytest.approx(coefficient_authority(order, index), rel=1e-3)


def test_l_autorite_n_est_pas_repartie_comme_l_intuition_le_suggere():
    """A₀ est le coefficient le PLUS influent, pas le moins.

    On s'attendrait à ce que les coefficients des bords pèsent peu, la fonction
    de classe s'y annulant. C'est vrai du dernier — B₁₁(ψ) = ψ¹¹ ne monte qu'au
    voisinage du bord de fuite, où C s'effondre — mais faux du premier :
    B₀(ψ) = (1−ψ)¹¹ vaut UN au bord d'attaque, alors que les Bernstein du
    milieu, étalés, ne culminent qu'à 0,24. A₀ finit donc avec l'autorité la
    plus forte des douze.

    D'où la précaution du code : les deux extrémités reçoivent un budget de
    déplacement réduit, et pour A₀ c'est un budget réduit divisé par une
    autorité élevée — sa marge est la plus étroite de toutes, ce qui est bien
    ce qu'on veut d'un coefficient qui tient le rayon de nez.
    """
    order = 11
    milieu = coefficient_authority(order, order // 2)
    assert coefficient_authority(order, 0) > milieu
    assert coefficient_authority(order, order) < milieu


def test_les_bornes_donnent_la_meme_autorite_a_tous_les_coefficients():
    """C'est tout l'objet du calcul : une marge égale en effet, pas en valeur."""
    profile = load(DENSE_PROFILE)
    fitted = fit_profile(profile.upper, profile.lower, DEFAULT_ORDER)
    params = build_design_params(fitted, profile)["parameters"]

    for index in range(1, fitted.order):  # hors extrémités, budget différent
        entry = params[f"cst_upper_{index}"]
        demi = (entry["max"] - entry["min"]) / 2.0
        effet = demi * coefficient_authority(fitted.order, index)
        assert effet == pytest.approx(COEFFICIENT_SHAPE_BUDGET, rel=1e-6)


def test_les_coefficients_des_bords_recoivent_un_budget_plus_serre():
    profile = load(DENSE_PROFILE)
    fitted = fit_profile(profile.upper, profile.lower, DEFAULT_ORDER)
    params = build_design_params(fitted, profile)["parameters"]

    for index in (0, fitted.order):
        entry = params[f"cst_upper_{index}"]
        demi = (entry["max"] - entry["min"]) / 2.0
        effet = demi * coefficient_authority(fitted.order, index)
        assert effet == pytest.approx(EDGE_SHAPE_BUDGET, rel=1e-6)


def test_un_gros_coefficient_ne_recoit_pas_une_marge_geante():
    """Le cas qui a motivé le calcul : un coefficient à 3,27 sur le Clark Y.

    Des bornes proportionnelles lui auraient donné ±1,63.
    """
    profile = load(DENSE_PROFILE)
    fitted = fit_profile(profile.upper, profile.lower, DEFAULT_ORDER)
    coefficients = fitted.upper.coefficients + fitted.lower.coefficients
    assert max(abs(c) for c in coefficients) > 1.0  # le cas existe bien

    params = build_design_params(fitted, profile)["parameters"]
    largeurs = [
        entry["max"] - entry["min"]
        for name, entry in params.items()
        if name.startswith("cst_")
    ]
    assert max(largeurs) < 1.0


def test_aucune_borne_ne_detruit_le_profil():
    """Chaque coefficient poussé à ses deux bornes doit laisser un profil valide.

    Une borne qui produit une forme retournée coûte une itération de CFD pour
    rien — et le budget d'itérations est ce qu'on a de plus rare.
    """
    for name in (DENSE_PROFILE, "naca2412"):
        profile = load(name)
        fitted = fit_profile(profile.upper, profile.lower, DEFAULT_ORDER)
        params = build_design_params(fitted, profile)["parameters"]
        base_upper = list(fitted.upper.coefficients)
        base_lower = list(fitted.lower.coefficients)

        for surface, base in (("upper", base_upper), ("lower", base_lower)):
            for index in range(len(base)):
                for bound in ("min", "max"):
                    upper, lower = list(base_upper), list(base_lower)
                    target = upper if surface == "upper" else lower
                    target[index] = params[f"cst_{surface}_{index}"][bound]
                    defect = check_shape(upper, lower)
                    assert defect is None, (
                        f"{name} — cst_{surface}_{index} à sa borne {bound} : "
                        f"{defect}"
                    )


# ─────────────────────────────────────────────────────────────────────────────
# Le contrôle de forme
# ─────────────────────────────────────────────────────────────────────────────


def test_un_profil_sain_passe_le_controle_de_forme():
    profile = load(DENSE_PROFILE)
    fitted = fit_profile(profile.upper, profile.lower, DEFAULT_ORDER)
    assert check_shape(fitted.upper.coefficients, fitted.lower.coefficients) is None


def test_des_surfaces_croisees_sont_detectees():
    """Intrados au dessus de l'extrados : la forme est retournée."""
    defect = check_shape([0.1] * 8, [0.3] * 8)
    assert defect is not None
    assert "se croisent" in defect


def test_un_profil_trop_fin_est_detecte():
    """Trop mince pour être maillé, sans pour autant être retourné.

    Les deux surfaces restent dans le bon ordre — l'extrados est bien au dessus
    — mais l'écart entre elles ne laisse plus de place à un maillage.
    """
    defect = check_shape([0.10] * 8, [0.0999] * 8)
    assert defect is not None
    assert "trop fine" in defect


def test_la_marge_du_coefficient_de_nez_est_la_plus_etroite():
    """Conséquence directe : budget réduit divisé par autorité élevée."""
    profile = load(DENSE_PROFILE)
    fitted = fit_profile(profile.upper, profile.lower, DEFAULT_ORDER)
    params = build_design_params(fitted, profile)["parameters"]
    largeurs = {
        index: params[f"cst_upper_{index}"]["max"]
        - params[f"cst_upper_{index}"]["min"]
        for index in range(fitted.order + 1)
    }
    assert min(largeurs, key=largeurs.get) == 0


def test_le_controle_ignore_le_nez_et_le_bord_de_fuite():
    """L'épaisseur y est nulle par construction : les juger refuserait tout."""
    profile = load("naca2412")
    fitted = fit_profile(profile.upper, profile.lower, DEFAULT_ORDER)
    assert fitted.thickness(0.0) == pytest.approx(0.0, abs=1e-9)
    assert check_shape(fitted.upper.coefficients, fitted.lower.coefficients) is None


def test_le_seuil_d_epaisseur_est_reglable():
    profile = load("naca2412")
    fitted = fit_profile(profile.upper, profile.lower, DEFAULT_ORDER)
    defect = check_shape(
        fitted.upper.coefficients, fitted.lower.coefficients, minimum=0.5
    )
    assert defect is not None and "trop fine" in defect


def test_le_driver_refuse_d_ecrire_une_forme_retournee(tmp_path):
    """Le garde-fou doit agir AVANT l'écriture du STL, pas après."""
    from fusion import parametric_driver as driver

    params = {
        "chord": {"value": 300.0, "unit": "mm"},
        "span": {"value": 80.0, "unit": "mm"},
        "aoa": {"value": 3.0, "unit": "deg"},
    }
    for index in range(8):
        params[f"cst_upper_{index}"] = {"value": 0.1, "unit": "unitless"}
        params[f"cst_lower_{index}"] = {"value": 0.3, "unit": "unitless"}

    with pytest.raises(driver.DriverError, match="se croisent"):
        driver.profile_from_parameters(params)


# ─────────────────────────────────────────────────────────────────────────────
# Un refus qui dit quoi faire
# ─────────────────────────────────────────────────────────────────────────────


def test_l_ordre_suggere_franchit_effectivement_la_porte():
    profile = load(DENSE_PROFILE)
    order = suggest_order(profile.upper, profile.lower)
    assert order is not None

    from profiles.cst import reconstruction_error
    from profiles.reparameterize import check_reconstruction

    fitted = fit_profile(profile.upper, profile.lower, order)
    accepted, _, _ = check_reconstruction(
        reconstruction_error(fitted, profile.upper, profile.lower)
    )
    assert accepted


def test_un_fichier_trop_grossier_n_a_pas_d_ordre_qui_le_sauve():
    """Monter l'ordre sur un fichier pauvre épouse son bruit, pas sa forme.

    Vérifié par validation croisée : sur l'E387, l'ordre 13 affiche
    8,6 × 10⁻⁴ sur ses propres points et 1,12 × 10⁻³ sur ceux qu'il n'a pas
    vus. Le garde-fou en points par coefficient interdit d'aller là.
    """
    profile = load(SPARSE_PROFILE)
    assert suggest_order(profile.upper, profile.lower) is None


def test_le_refus_nomme_l_ordre_qui_marcherait():
    """Un fichier dense refusé à l'ordre 5 doit se voir proposer mieux."""
    result = reparameterize(EXAMPLES / f"{DENSE_PROFILE}.dat", order=5)
    assert not result.success
    assert "--order" in result.message


def test_le_refus_d_un_fichier_pauvre_conseille_de_le_densifier():
    result = reparameterize(EXAMPLES / f"{SPARSE_PROFILE}.dat")
    assert not result.success
    assert "plus dense" in result.message


def test_le_refus_compte_les_points_fautifs():
    """Un point sur soixante-deux et soixante sur soixante-deux n'ont pas le
    même sens : le premier signale un fichier bruité, le second un modèle
    inadapté."""
    result = reparameterize(EXAMPLES / f"{SPARSE_PROFILE}.dat")
    assert "points sur" in result.message or "point sur" in result.message


def test_l_exploration_respecte_le_nombre_de_points_disponibles():
    """Le garde-fou de surajustement doit se déclencher, pas décorer."""
    court = [(i / 20.0, 0.05) for i in range(1, 20)]
    assert len(court) / (5 + 1) < MIN_POINTS_PER_COEFFICIENT * 2
    assert suggest_order(court, [(x, -y) for x, y in court],
                         candidates=(15, 17)) is None


# ─────────────────────────────────────────────────────────────────────────────
# Un profil réel de bout en bout
# ─────────────────────────────────────────────────────────────────────────────


def test_un_profil_reel_de_la_base_uiuc_traverse_toute_la_chaine(tmp_path):
    """Clark Y : ingestion → ajustement → porte → paramètres optimisables."""
    result = reparameterize(
        EXAMPLES / f"{DENSE_PROFILE}.dat", chord_mm=300.0, aoa_deg=3.0
    )
    assert result.success, result.message

    params = result.design_params["parameters"]
    assert result.design_params["parameterization"] == "cst"
    assert len([n for n in params if n.startswith("cst_")]) == 2 * (DEFAULT_ORDER + 1)
    assert {"chord", "span", "aoa"} <= set(params)


def test_le_bord_de_fuite_ouvert_du_clark_y_est_conserve():
    """Le Clark Y a un bord de fuite épaissi : c'est une caractéristique."""
    result = reparameterize(EXAMPLES / f"{DENSE_PROFILE}.dat")
    assert result.success, result.message
    provenance = result.design_params["provenance"]
    assert abs(provenance["trailing_edge_gap"]) > 1e-4


def test_l_optimiseur_voit_bien_tous_les_coefficients_comme_manoeuvrables():
    """Un coefficient figé serait une variable perdue sans que rien ne le dise."""
    from agent.orchestrator import free_parameters

    result = reparameterize(EXAMPLES / f"{DENSE_PROFILE}.dat")
    assert result.success, result.message
    free = free_parameters(result.design_params["parameters"])
    coefficients = [n for n in free if n.startswith("cst_")]
    assert len(coefficients) == 2 * (DEFAULT_ORDER + 1)


def test_la_contrainte_d_epaisseur_s_applique_aussi_en_cst(tmp_path):
    """Mesurée sur la forme, faute d'être donnée — sinon elle disparaîtrait."""
    from pipeline.geometry_validator import _thickness_ratio

    result = reparameterize(EXAMPLES / f"{DENSE_PROFILE}.dat")
    assert result.success, result.message
    ratio = _thickness_ratio(result.design_params, result.design_params["parameters"])
    assert ratio is not None
    assert ratio == pytest.approx(0.117, abs=5e-3)


def test_la_forme_minimale_exigee_reste_tres_en_deca_d_un_profil_reel():
    """Le seuil doit attraper une forme dégénérée, pas un profil mince."""
    assert MIN_RELATIVE_THICKNESS < 0.01
