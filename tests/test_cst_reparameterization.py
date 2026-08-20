"""Phase 3 — ajustement CST, porte de reconstruction, variables de conception.

Les cas se lisent en trois familles :

**Ce que la formulation garantit** — nez en racine carrée, bord de fuite
pointu, r = A₀²/2 — doit tenir exactement, parce que c'est de l'algèbre et non
de l'ajustement.

**Ce que l'ajustement rend** est vérifié contre des profils NACA dont la
géométrie est connue analytiquement : on sait ce que l'épaisseur, la cambrure
et le rayon de nez DOIVENT valoir, et l'on n'accepte pas simplement ce que le
code produit.

**Ce que la porte refuse** est le point qui protège l'optimisation : un
ajustement trop pauvre doit être rejeté, et un fichier douteux ne doit jamais
lever d'exception jusque dans la boucle.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from profiles.cst import (
    DEFAULT_ORDER,
    ERROR_FLOOR,
    CSTSurface,
    bernstein,
    class_function,
    cosine_stations,
    fit_profile,
    fit_surface,
    reconstruction_error,
    solve_least_squares,
)
from profiles.reparameterize import (
    MAX_ERROR_REJECT,
    ReconstructionError,
    build_design_params,
    check_reconstruction,
    reparameterize,
    write_design_params,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "profiles"

#: Rayon de nez exact d'un NACA à 12 % d'épaisseur : r = 1,1019 · t².
NACA12_NOSE_RADIUS = 1.1019 * 0.12 ** 2


# ─────────────────────────────────────────────────────────────────────────────
# Profils de référence
# ─────────────────────────────────────────────────────────────────────────────


def naca4(psi: float, t: float, m: float, p: float) -> tuple[tuple[float, float],
                                                             tuple[float, float]]:
    """Point NACA 4 chiffres, en corde unitaire, sur chaque surface."""
    yt = 5 * t * (0.2969 * math.sqrt(psi) - 0.1260 * psi - 0.3516 * psi ** 2
                  + 0.2843 * psi ** 3 - 0.1036 * psi ** 4)
    if m == 0:
        yc, dyc = 0.0, 0.0
    elif psi < p:
        yc = m * (psi / p ** 2) * (2 * p - psi)
        dyc = (2 * m / p ** 2) * (p - psi)
    else:
        yc = m * ((1 - psi) / (1 - p) ** 2) * (1 + psi - 2 * p)
        dyc = (2 * m / (1 - p) ** 2) * (p - psi)
    theta = math.atan(dyc)
    return ((psi - yt * math.sin(theta), yc + yt * math.cos(theta)),
            (psi + yt * math.sin(theta), yc - yt * math.cos(theta)))


def naca_surfaces(t=0.12, m=0.02, p=0.4, n=120):
    """Deux surfaces NACA, à la convention des fichiers publiés.

    Le bord d'attaque est ramené en (0, 0) et les points qui repassent derrière
    lui sont écartés — c'est ce que fait l'ingestion, et ce que contiennent les
    fichiers réels.
    """
    upper, lower = [], []
    for psi in cosine_stations(n + 1):
        u, l = naca4(psi, t, m, p)
        upper.append(u)
        lower.append(l)
    upper = [(0.0, 0.0)] + [q for q in upper[1:] if q[0] > 0.0]
    lower = [(0.0, 0.0)] + [q for q in lower[1:] if q[0] > 0.0]
    return upper, lower


# ─────────────────────────────────────────────────────────────────────────────
# Ce que la formulation garantit
# ─────────────────────────────────────────────────────────────────────────────


def test_les_bernstein_forment_une_partition_de_l_unite():
    for psi in (0.0, 0.1, 0.5, 0.83, 1.0):
        total = sum(bernstein(7, i, psi) for i in range(8))
        assert total == pytest.approx(1.0, abs=1e-12)


def test_la_fonction_de_classe_annule_les_deux_bords():
    assert class_function(0.0) == 0.0
    assert class_function(1.0) == 0.0
    assert class_function(0.25) == pytest.approx(0.5 * 0.75)


def test_la_fonction_de_classe_ne_deborde_pas_de_l_intervalle():
    """ψ hors [0, 1] n'a pas de sens : la classe doit rendre zéro, pas planter.

    Un `psi ** 0.5` sur un négatif lèverait, et la boucle d'optimisation avec.
    """
    assert class_function(-0.01) == 0.0
    assert class_function(1.5) == 0.0


def test_le_nez_suit_une_racine_carree():
    """ζ ∝ √ψ près du bord d'attaque : diviser ψ par 4 doit diviser ζ par 2."""
    surface = CSTSurface([0.2] * 8)
    proche = surface.evaluate(1e-6)
    quadruple = surface.evaluate(4e-6)
    assert quadruple / proche == pytest.approx(2.0, rel=1e-3)


def test_le_bord_de_fuite_est_pointu():
    surface = CSTSurface([0.2] * 8, trailing_edge=0.0)
    assert surface.evaluate(1.0) == pytest.approx(0.0, abs=1e-12)


def test_le_bord_de_fuite_ouvert_est_respecte_exactement():
    surface = CSTSurface([0.2] * 8, trailing_edge=0.004)
    assert surface.evaluate(1.0) == pytest.approx(0.004, abs=1e-12)


def test_le_premier_coefficient_donne_le_rayon_de_nez():
    assert CSTSurface([0.3, 0.1, 0.1]).leading_edge_radius == pytest.approx(0.045)


def test_le_rayon_de_nez_n_a_pas_de_sens_hors_nez_arrondi():
    """r = A₀²/2 ne vaut que pour N1 = 0,5 ; ailleurs il ne faut rien affirmer."""
    pointu = CSTSurface([0.3, 0.1, 0.1], n1=1.0)
    assert pointu.leading_edge_radius == 0.0


def test_les_moindres_carres_retrouvent_une_solution_exacte():
    matrix = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    target = [2.0, 3.0, 5.0]
    solution = solve_least_squares(matrix, target)
    assert solution[0] == pytest.approx(2.0, abs=1e-9)
    assert solution[1] == pytest.approx(3.0, abs=1e-9)


# ─────────────────────────────────────────────────────────────────────────────
# Ce que l'ajustement rend
# ─────────────────────────────────────────────────────────────────────────────


def test_l_ajustement_reproduit_une_surface_cst_exactement():
    """Une surface issue du modèle doit être retrouvée au bit près ou presque.

    C'est le seul cas où l'on connaît la réponse exacte : si l'ajustement ne
    la retrouve pas, l'erreur est dans le solveur et non dans le modèle.
    """
    original = CSTSurface([0.20, 0.15, 0.24, 0.13, 0.22, 0.18, 0.21, 0.19])
    stations = [psi for psi in cosine_stations(90) if 0.0 < psi < 1.0]
    points = original.points(stations)

    refit = fit_surface(points, order=7)
    for expected, obtained in zip(original.coefficients, refit.coefficients):
        assert obtained == pytest.approx(expected, abs=1e-6)


def test_l_ajustement_est_reproductible_au_bit_pres():
    """Exigence explicite du §4 : deux ajustements identiques, pas « proches »."""
    upper, lower = naca_surfaces()
    premier = fit_profile(upper, lower, DEFAULT_ORDER)
    second = fit_profile(upper, lower, DEFAULT_ORDER)
    assert premier.upper.coefficients == second.upper.coefficients
    assert premier.lower.coefficients == second.lower.coefficients


def test_le_profil_symetrique_donne_deux_surfaces_opposees():
    upper, lower = naca_surfaces(m=0.0)
    fitted = fit_profile(upper, lower, DEFAULT_ORDER)
    for haut, bas in zip(fitted.upper.coefficients, fitted.lower.coefficients):
        assert haut == pytest.approx(-bas, abs=1e-9)


def test_l_epaisseur_et_la_cambrure_ajustees_valent_celles_du_naca():
    """On vérifie contre les valeurs analytiques, pas contre la sortie du code."""
    upper, lower = naca_surfaces(t=0.12, m=0.02, p=0.4)
    fitted = fit_profile(upper, lower, DEFAULT_ORDER)

    thickness, thickness_at = fitted.max_thickness()
    camber, camber_at = fitted.max_camber()

    assert thickness == pytest.approx(0.12, abs=1e-3)
    assert thickness_at == pytest.approx(0.30, abs=0.02)
    assert camber == pytest.approx(0.02, abs=1e-3)
    assert camber_at == pytest.approx(0.40, abs=0.03)


def test_le_rayon_de_nez_ajuste_vaut_celui_du_naca():
    """r = 1,1019 t², sur les deux familles.

    Sur un profil cambré, aucune des deux surfaces prise seule ne donne ce
    rayon — l'extrados le surestime d'environ 20 %, l'intrados le sous-estime
    d'autant. C'est leur moyenne qui vaut le rayon du profil, et c'est
    précisément ce que garde `CSTProfile.leading_edge_radius`.
    """
    for camber in (0.0, 0.02):
        upper, lower = naca_surfaces(m=camber)
        fitted = fit_profile(upper, lower, 9)
        assert fitted.leading_edge_radius == pytest.approx(
            NACA12_NOSE_RADIUS, rel=0.08
        )


def test_sur_un_profil_cambre_les_deux_surfaces_encadrent_le_rayon():
    """Le corollaire du test précédent, énoncé pour lui-même."""
    upper, lower = naca_surfaces(m=0.02)
    fitted = fit_profile(upper, lower, 9)
    haut = fitted.upper.leading_edge_radius
    bas = fitted.lower.leading_edge_radius
    assert bas < NACA12_NOSE_RADIUS < haut


def test_l_erreur_decroit_quand_l_ordre_augmente():
    upper, lower = naca_surfaces()
    erreurs = [
        reconstruction_error(fit_profile(upper, lower, order), upper, lower).max_error
        for order in (5, 7, 9, 11)
    ]
    assert erreurs == sorted(erreurs, reverse=True)


def test_l_ordre_par_defaut_franchit_la_porte():
    for camber in (0.0, 0.02):
        upper, lower = naca_surfaces(m=camber)
        fitted = fit_profile(upper, lower, DEFAULT_ORDER)
        error = reconstruction_error(fitted, upper, lower)
        assert error.max_error < MAX_ERROR_REJECT


def test_l_ecart_est_une_distance_et_non_un_ecart_vertical():
    """Le point le plus délicat de la Phase 3.

    Au bord d'attaque la surface est quasi verticale : sa pente y dépasse 6.
    Un écart mesuré verticalement y est donc plusieurs fois la distance réelle
    entre le profil reconstruit et les points d'origine — au point de faire
    refuser un ajustement parfaitement bon. Sur un NACA 2412 en répartition
    cosinus, la mesure verticale dépasse le seuil de refus alors que la
    distance géométrique en reste au cinquième.
    """
    upper, lower = naca_surfaces(m=0.02)
    fitted = fit_profile(upper, lower, DEFAULT_ORDER)
    error = reconstruction_error(fitted, upper, lower)

    assert error.max_vertical_error > MAX_ERROR_REJECT
    assert error.max_error < MAX_ERROR_REJECT
    assert error.max_error < error.max_vertical_error / 4


def test_l_erreur_est_nulle_sur_un_profil_issu_du_modele():
    """Un profil issu du modèle doit être reconstruit sans écart mesurable.

    La distance retenue ne descend pas sous `ERROR_FLOOR` : la polyligne qui
    sert à la mesurer coupe la corde des arcs qu'elle remplace. L'écart
    vertical, lui, n'a pas ce plancher — il tombe au bruit de calcul, ce qui
    montre que le résidu vient bien de la mesure et non de l'ajustement.
    """
    surface = CSTSurface([0.20, 0.16, 0.23, 0.14, 0.21, 0.17, 0.20, 0.19])
    stations = [psi for psi in cosine_stations(120) if 0.0 < psi < 1.0]
    points = surface.points(stations)
    mirror = [(psi, -zeta) for psi, zeta in points]

    fitted = fit_profile(points, mirror, 7)
    error = reconstruction_error(fitted, points, mirror)

    assert error.max_error < ERROR_FLOOR
    assert error.max_vertical_error < 1e-9


def test_la_mesure_a_un_plancher_annonce():
    """`ERROR_FLOOR` doit rester très loin sous le seuil de refus.

    Si un jour la polyligne était dégrossie au point que son plancher approche
    la porte, celle-ci ne mesurerait plus l'ajustement mais sa propre
    discrétisation — sans que rien ne le signale.
    """
    assert ERROR_FLOOR < MAX_ERROR_REJECT / 100


def test_l_ajustement_refuse_de_deviner_avec_trop_peu_de_points():
    points = [(0.1, 0.05), (0.5, 0.06), (0.9, 0.01)]
    with pytest.raises(ValueError, match="au moins"):
        fit_surface(points, order=7)


def test_l_ajustement_refuse_des_points_tous_aux_bords():
    """ψ = 0 et ψ = 1 n'apportent rien : le système serait sous-déterminé."""
    points = [(0.0, 0.0)] * 6 + [(1.0, 0.0)] * 6
    with pytest.raises(ValueError, match="sous-déterminé"):
        fit_surface(points, order=3)


# ─────────────────────────────────────────────────────────────────────────────
# Ce que la porte refuse
# ─────────────────────────────────────────────────────────────────────────────


def _error(max_error=0.0, mean_error=0.0) -> ReconstructionError:
    return ReconstructionError(
        max_error=max_error, mean_error=mean_error, rms_error=mean_error,
        max_error_position=0.3, max_error_surface="extrados",
        upper_max=max_error, lower_max=0.0,
    )


def test_la_porte_accepte_un_ajustement_fidele():
    accepted, errors, warnings = check_reconstruction(_error(5e-5, 1e-5))
    assert accepted and not errors and not warnings


def test_la_porte_avertit_avant_de_refuser():
    accepted, errors, warnings = check_reconstruction(_error(4e-4, 1e-5))
    assert accepted and not errors
    assert any("ordre plus élevé" in w for w in warnings)


def test_la_porte_refuse_un_ecart_maximal_trop_grand():
    accepted, errors, _ = check_reconstruction(_error(2e-3, 1e-5))
    assert not accepted
    assert any("n'est pas celle du fichier" in e for e in errors)


def test_la_porte_refuse_un_ecart_moyen_trop_grand():
    """Un ajustement peut rater la forme partout sans pic nulle part."""
    accepted, errors, _ = check_reconstruction(_error(4e-4, 5e-4))
    assert not accepted
    assert any("partout" in e for e in errors)


def test_les_seuils_de_la_porte_sont_reglables():
    accepted, _, _ = check_reconstruction(_error(2e-3, 1e-5), max_reject=1e-2)
    assert accepted


# ─────────────────────────────────────────────────────────────────────────────
# Variables de conception
# ─────────────────────────────────────────────────────────────────────────────


def test_chaque_coefficient_devient_une_variable_bornee():
    upper, lower = naca_surfaces()
    fitted = fit_profile(upper, lower, DEFAULT_ORDER)
    params = build_design_params(fitted, _profile_stub())["parameters"]

    for label in ("upper", "lower"):
        for index in range(DEFAULT_ORDER + 1):
            entry = params[f"cst_{label}_{index}"]
            assert entry["min"] < entry["value"] < entry["max"]


def test_le_coefficient_de_nez_est_borne_plus_serre():
    """A₀ tient le rayon de bord d'attaque : il ne doit pas dériver librement."""
    upper, lower = naca_surfaces()
    fitted = fit_profile(upper, lower, DEFAULT_ORDER)
    params = build_design_params(fitted, _profile_stub())["parameters"]

    nez = params["cst_upper_0"]
    autre = params["cst_upper_3"]
    largeur_nez = (nez["max"] - nez["min"]) / abs(nez["value"])
    largeur_autre = (autre["max"] - autre["min"]) / abs(autre["value"])
    assert largeur_nez < largeur_autre


def test_un_coefficient_quasi_nul_garde_une_marge_de_manoeuvre():
    """Sans plancher, l'optimiseur perdrait la variable sans jamais le savoir."""
    fitted = fit_profile(*naca_surfaces(), DEFAULT_ORDER)
    fitted.upper.coefficients[3] = 1e-9
    params = build_design_params(fitted, _profile_stub())["parameters"]
    entry = params["cst_upper_3"]
    assert entry["max"] - entry["min"] > 0.01


def test_le_fichier_produit_annonce_sa_parametrisation():
    fitted = fit_profile(*naca_surfaces(), DEFAULT_ORDER)
    params = build_design_params(fitted, _profile_stub())
    assert params["parameterization"] == "cst"
    assert params["provenance"]["cst_order"] == DEFAULT_ORDER


def test_les_grandeurs_physiques_restent_des_parametres_a_part():
    fitted = fit_profile(*naca_surfaces(), DEFAULT_ORDER)
    params = build_design_params(
        fitted, _profile_stub(), chord_mm=250.0, span_mm=90.0, aoa_deg=3.0
    )["parameters"]
    assert params["chord"]["value"] == 250.0
    assert params["chord"]["unit"] == "mm"
    assert params["span"]["value"] == 90.0
    assert params["aoa"]["value"] == 3.0
    assert params["aoa"]["unit"] == "deg"


def _profile_stub():
    from profiles.profile import Profile, ProfileTransform

    upper, lower = naca_surfaces()
    return Profile(
        name="NACA 2412", upper=upper, lower=lower,
        transform=ProfileTransform((0.0, 0.0), 0.0, 1.0),
        metadata={"chord_mm": 300.0},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Enchaînement complet
# ─────────────────────────────────────────────────────────────────────────────


def test_un_fichier_reel_traverse_toute_la_chaine(tmp_path):
    """Fichier Selig → ingestion → ajustement → porte → design_params.yaml."""
    result = reparameterize(EXAMPLES / "naca2412.dat", order=DEFAULT_ORDER,
                            chord_mm=300.0, aoa_deg=3.0)

    assert result.success, result.message
    assert result.error.max_error < MAX_ERROR_REJECT
    assert result.design_params["parameterization"] == "cst"

    target = write_design_params(result.design_params, tmp_path / "design_params.yaml")
    assert target.exists()


def test_le_fichier_ecrit_satisfait_le_contrat_du_projet(tmp_path):
    """Il doit être relisible par la v1.0, sinon la Phase 4 casserait dessus."""
    from pipeline.utils import load_design_params

    result = reparameterize(EXAMPLES / "naca0012.dat")
    assert result.success, result.message
    target = write_design_params(result.design_params, tmp_path / "dp.yaml")

    relu = load_design_params(target)
    assert relu["parameterization"] == "cst"
    assert "cst_upper_0" in relu["parameters"]


def test_un_fichier_introuvable_est_un_compte_rendu_pas_une_exception():
    result = reparameterize("/inexistant/profil.dat")
    assert not result.success
    assert result.status == "INGESTION_ECHOUEE"


def test_un_fichier_illisible_est_un_compte_rendu_pas_une_exception(tmp_path):
    bruit = tmp_path / "bruit.dat"
    bruit.write_text("ceci n'est pas un profil\nni ceci\n", encoding="utf-8")
    result = reparameterize(bruit)
    assert not result.success
    assert result.fitted is None


def test_la_porte_bloque_effectivement_l_ecriture(tmp_path):
    """Le seuil abaissé à l'absurde doit faire échouer la re-paramétrisation."""
    result = reparameterize(EXAMPLES / "naca2412.dat", max_reject=1e-9)
    assert not result.success
    assert result.status == "RECONSTRUCTION_REFUSEE"
    assert result.design_params is None


def test_le_mode_non_strict_avertit_au_lieu_de_refuser():
    result = reparameterize(EXAMPLES / "naca2412.dat", max_reject=1e-9, strict=False)
    assert result.success
    assert any("non bloquant" in w for w in result.warnings)
    assert result.design_params is not None


def test_l_incidence_retiree_est_conservee_pour_la_suite(tmp_path):
    """Le retour vers Fusion (Phase 6) devra la restituer : elle doit survivre."""
    incline = tmp_path / "incline.dat"
    lignes = ["Profil incline"]
    upper, lower = naca_surfaces()
    ordonne = list(reversed(upper)) + lower[1:]
    angle = math.radians(-5.0)
    for x, y in ordonne:
        xr = x * math.cos(angle) - y * math.sin(angle)
        yr = x * math.sin(angle) + y * math.cos(angle)
        lignes.append(f"{xr:.6f}  {yr:.6f}")
    incline.write_text("\n".join(lignes) + "\n", encoding="utf-8")

    result = reparameterize(incline)
    assert result.success, result.message
    assert result.design_params["provenance"]["removed_incidence_deg"] == pytest.approx(
        -5.0, abs=0.2
    )
