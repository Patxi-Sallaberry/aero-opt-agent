"""Phase 4 — des coefficients CST à un solide, et retour.

La Phase 3 garantissait qu'un profil se laisse décrire par seize coefficients.
Elle ne garantissait rien sur ce qu'on écrit ensuite sur le disque : entre les
coefficients et le STL se glissent une mise à l'échelle, une conversion
d'unités, une rotation d'incidence et une triangulation. Chacune peut être
fausse sans que l'ajustement le soit.

Les cas ci-dessous se lisent donc en remontant la chaîne : ce que les
coefficients produisent comme contour, ce que le driver en fait, ce que le
backend écrit, et enfin ce qu'on retrouve en relisant le fichier.

Le dernier groupe — l'aller-retour — est celui qui compte. Il ne fait confiance
à aucune des étapes précédentes : il relit le STL et le mesure contre le
fichier de points d'origine. C'est le seul contrôle qui aurait attrapé la
confusion d'unités de la v1.0, où toute la chaîne était verte pendant que la
géométrie sortait dix fois trop petite.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from fusion import parametric_driver as driver
from geometry import get_backend
from profiles.cst import DEFAULT_ORDER, fit_profile
from profiles.geometry import (
    ContourError,
    collect_coefficients,
    cst_contour,
    cst_measures,
)
from profiles.loader import load_profile
from profiles.reparameterize import reparameterize, write_design_params
from profiles.roundtrip import (
    ROUNDTRIP_TOLERANCE,
    check_roundtrip,
    extract_section,
    read_stl_vertices,
    reference_contour,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "profiles"
CHORD_MM = 300.0
AOA_DEG = 3.0


# ─────────────────────────────────────────────────────────────────────────────
# Outils
# ─────────────────────────────────────────────────────────────────────────────


def fitted_coefficients(name: str = "naca2412", order: int = 7):
    profile = load_profile(EXAMPLES / f"{name}.dat").profile
    fitted = fit_profile(profile.upper, profile.lower, order)
    return fitted.upper.coefficients, fitted.lower.coefficients, profile


def build_geometry(tmp_path: Path, name: str = "naca2412", **kwargs):
    """Chaîne complète : fichier → CST → design_params.yaml → STL."""
    source = EXAMPLES / f"{name}.dat"
    result = reparameterize(
        source, chord_mm=kwargs.pop("chord_mm", CHORD_MM),
        span_mm=80.0, aoa_deg=kwargs.pop("aoa_deg", AOA_DEG), **kwargs
    )
    assert result.success, result.message
    config = write_design_params(result.design_params, tmp_path / "design_params.yaml")
    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True, exist_ok=True)
    geometry = get_backend("internal").generate(config, out)
    return geometry, result, load_profile(source).profile


# ─────────────────────────────────────────────────────────────────────────────
# Des paramètres aux coefficients
# ─────────────────────────────────────────────────────────────────────────────


def test_les_coefficients_sont_ranges_par_indice_et_non_par_nom():
    """`cst_upper_10` suit `cst_upper_9`, pas `cst_upper_1`.

    Un tri alphabétique passerait inaperçu jusqu'à l'ordre 9 inclus, puis
    permuterait silencieusement deux polynômes de Bernstein.
    """
    values = {f"cst_upper_{i}": float(i) for i in range(12)}
    assert collect_coefficients(values, "cst_upper_") == [float(i) for i in range(12)]


def test_un_trou_dans_la_suite_de_coefficients_est_refuse():
    values = {"cst_upper_0": 0.2, "cst_upper_1": 0.1, "cst_upper_3": 0.1}
    with pytest.raises(ContourError, match="incomplète"):
        collect_coefficients(values, "cst_upper_")


def test_une_absence_totale_de_coefficients_est_refusee():
    with pytest.raises(ContourError, match="aucun coefficient"):
        collect_coefficients({"chord": 300.0}, "cst_upper_")


def test_un_indice_illisible_est_refuse():
    with pytest.raises(ContourError, match="indice"):
        collect_coefficients({"cst_upper_x": 0.2}, "cst_upper_")


def test_les_deux_surfaces_doivent_avoir_le_meme_nombre_de_coefficients():
    with pytest.raises(ContourError, match="même nombre"):
        cst_contour([0.2] * 8, [0.2] * 6, chord=1.0)


# ─────────────────────────────────────────────────────────────────────────────
# Des coefficients au contour
# ─────────────────────────────────────────────────────────────────────────────


def test_le_contour_apparie_les_deux_surfaces_station_par_station():
    """Le pavage des faces d'extrémité du STL relie upper[i] à lower[i]."""
    upper, lower, _ = fitted_coefficients()
    contour = cst_contour(upper, lower, chord=1.0)
    assert len(contour["upper"]) == len(contour["lower"])
    for haut, bas in zip(contour["upper"], contour["lower"]):
        assert haut[0] == pytest.approx(bas[0], abs=1e-12)


def test_le_contour_part_du_bord_d_attaque_et_finit_au_bord_de_fuite():
    upper, lower, _ = fitted_coefficients()
    contour = cst_contour(upper, lower, chord=1.0)
    assert contour["upper"][0] == pytest.approx((0.0, 0.0), abs=1e-12)
    assert contour["upper"][-1][0] == pytest.approx(1.0, abs=1e-12)
    assert contour["lower"][-1][0] == pytest.approx(1.0, abs=1e-12)


def test_la_corde_met_le_contour_a_l_echelle_lineairement():
    upper, lower, _ = fitted_coefficients()
    petit = cst_contour(upper, lower, chord=1.0)
    grand = cst_contour(upper, lower, chord=30.0)
    for a, b in zip(petit["upper"], grand["upper"]):
        assert b[0] == pytest.approx(30.0 * a[0], abs=1e-9)
        assert b[1] == pytest.approx(30.0 * a[1], abs=1e-9)


def test_l_incidence_tourne_le_profil_dans_le_bon_sens():
    """Incidence positive : le bord de fuite descend, l'écoulement reste en +X.

    Le signe est une source d'erreur classique, et une erreur de signe se voit
    dans les résultats CFD comme une portance négative — bien trop tard.
    """
    upper, lower, _ = fitted_coefficients()
    droit = cst_contour(upper, lower, chord=1.0, aoa_rad=0.0)
    incline = cst_contour(upper, lower, chord=1.0, aoa_rad=math.radians(10.0))
    assert incline["upper"][-1][1] < droit["upper"][-1][1]


def test_l_incidence_conserve_la_longueur_de_corde():
    upper, lower, _ = fitted_coefficients()
    incline = cst_contour(upper, lower, chord=1.0, aoa_rad=math.radians(10.0))
    bord_de_fuite = incline["upper"][-1]
    assert math.hypot(*bord_de_fuite) == pytest.approx(1.0, abs=1e-9)


def test_le_bord_de_fuite_ouvert_est_restitue():
    """Sans les ordonnées de bord de fuite, un profil épaissi sortirait fermé."""
    upper, lower, _ = fitted_coefficients()
    ferme = cst_contour(upper, lower, chord=1.0)
    ouvert = cst_contour(upper, lower, chord=1.0, trailing_edges=(0.004, -0.004))

    assert ferme["upper"][-1][1] == pytest.approx(ferme["lower"][-1][1], abs=1e-12)
    assert ouvert["upper"][-1][1] - ouvert["lower"][-1][1] == pytest.approx(
        0.008, abs=1e-9
    )


def test_une_corde_non_positive_est_refusee():
    upper, lower, _ = fitted_coefficients()
    with pytest.raises(ContourError, match="corde non positive"):
        cst_contour(upper, lower, chord=0.0)


def test_les_grandeurs_mesurees_valent_celles_du_naca():
    """Épaisseur et cambrure sont MESURÉES ici, alors que la voie NACA les reçoit."""
    upper, lower, _ = fitted_coefficients()
    measures = cst_measures(upper, lower)
    assert measures["thickness"] == pytest.approx(0.12, abs=1e-3)
    assert measures["camber"] == pytest.approx(0.02, abs=1e-3)
    assert measures["camber_position"] == pytest.approx(0.40, abs=0.03)


# ─────────────────────────────────────────────────────────────────────────────
# Le driver
# ─────────────────────────────────────────────────────────────────────────────


def _cst_parameters(chord_mm=CHORD_MM, aoa_deg=AOA_DEG):
    upper, lower, _ = fitted_coefficients()
    params = {
        "chord": {"value": chord_mm, "unit": "mm"},
        "span": {"value": 80.0, "unit": "mm"},
        "aoa": {"value": aoa_deg, "unit": "deg"},
    }
    for index, value in enumerate(upper):
        params[f"cst_upper_{index}"] = {"value": value, "unit": "unitless"}
    for index, value in enumerate(lower):
        params[f"cst_lower_{index}"] = {"value": value, "unit": "unitless"}
    return params


def test_la_parametrisation_se_reconnait_a_ses_parametres():
    assert driver.detect_parameterization(_cst_parameters()) == "cst"
    assert driver.detect_parameterization(
        {"chord": {}, "thickness": {}, "camber": {}}
    ) == "naca"


def test_un_fichier_qui_ment_sur_sa_parametrisation_est_refuse():
    """Un en-tête `naca` sur des coefficients CST donnerait une autre forme."""
    with pytest.raises(driver.DriverError, match="silencieusement fausse"):
        driver.profile_from_parameters(_cst_parameters(), parameterization="naca")


def test_une_parametrisation_inconnue_est_refusee():
    with pytest.raises(driver.DriverError, match="paramétrisation inconnue"):
        driver.profile_from_parameters(_cst_parameters(), parameterization="bezier")


def test_le_plan_cst_a_la_meme_structure_que_le_plan_naca():
    """Tout l'aval — STL, statut, rapports — dépend de cette identité."""
    cst = driver.profile_from_parameters(_cst_parameters())
    naca = driver.profile_from_parameters({
        "chord": {"value": CHORD_MM, "unit": "mm"},
        "span": {"value": 80.0, "unit": "mm"},
        "thickness": {"value": 0.12, "unit": "unitless"},
        "camber": {"value": 0.02, "unit": "unitless"},
        "aoa": {"value": AOA_DEG, "unit": "deg"},
    })
    communes = {
        "profile", "chord_cm", "span_cm", "thickness", "camber", "aoa_rad",
        "aoa_deg", "n_points", "camber_position", "bbox_cm",
    }
    assert communes <= set(cst)
    assert communes <= set(naca)


def test_le_driver_convertit_les_millimetres_en_centimetres():
    """300 mm de corde font 30 cm de plan — la confusion qui a coûté la v1.0."""
    plan = driver.profile_from_parameters(_cst_parameters(chord_mm=300.0))
    assert plan["chord_cm"] == pytest.approx(30.0)
    assert plan["span_cm"] == pytest.approx(8.0)


def test_le_driver_manque_de_parametres_le_dit_clairement():
    params = _cst_parameters()
    del params["span"]
    with pytest.raises(driver.DriverError, match="span"):
        driver.profile_from_parameters(params)


def test_les_ordonnees_de_bord_de_fuite_viennent_de_la_provenance():
    plan = driver.profile_from_parameters(
        _cst_parameters(),
        provenance={"trailing_edge_upper": 0.004, "trailing_edge_lower": -0.004},
    )
    haut = plan["profile"]["upper"][-1]
    bas = plan["profile"]["lower"][-1]
    assert math.dist(haut, bas) == pytest.approx(0.008 * plan["chord_cm"], rel=1e-6)


def test_les_deux_voies_placent_le_profil_au_meme_endroit():
    """Un NACA 2412 ajusté en CST doit retomber sur le NACA 2412 analytique.

    C'est le contrôle croisé le plus fort dont on dispose : les deux voies
    n'ont en commun ni leur code, ni leur échantillonnage, ni leur façon
    d'obtenir la forme — l'une la calcule, l'autre l'a apprise d'un fichier.
    Si elles s'accordent, l'échelle, l'orientation et l'incidence le sont des
    deux côtés.
    """
    from profiles.cst import distance_to_curve

    cst = driver.profile_from_parameters(_cst_parameters())["profile"]
    # La référence est volontairement suréchantillonnée. `naca4_profile`
    # répartit ses points UNIFORMÉMENT : à quatre-vingts points, sa polyligne
    # coupe le nez en biseau et l'écart mesuré — près de 3 × 10⁻³ de corde — est
    # celui de sa propre discrétisation, pas celui entre les deux voies.
    naca = driver.naca4_profile(30.0, 0.12, 0.02, math.radians(AOA_DEG),
                                n_points=4000)

    reference = naca["upper"] + list(reversed(naca["lower"]))
    reference = reference + [reference[0]]
    ecarts = [
        distance_to_curve(p, reference) / 30.0
        for p in cst["upper"] + cst["lower"]
    ]
    assert max(ecarts) < 6e-4


# ─────────────────────────────────────────────────────────────────────────────
# Le backend
# ─────────────────────────────────────────────────────────────────────────────


def test_le_backend_interne_ecrit_un_stl_depuis_des_coefficients(tmp_path):
    geometry, _, _ = build_geometry(tmp_path)
    assert geometry.success, geometry.message
    assert geometry.stl_path is not None and geometry.stl_path.is_file()
    assert geometry.stl_path.stat().st_size > 1000


def test_le_compte_rendu_annonce_la_parametrisation(tmp_path):
    geometry, _, _ = build_geometry(tmp_path)
    assert geometry.geometry["parameterization"] == "cst"
    assert geometry.geometry["cst_order"] == DEFAULT_ORDER


def test_le_backend_rend_le_contour_ordonne_en_metres(tmp_path):
    """Exigence 2 de la phase : des coordonnées ordonnées, prêtes à l'emploi."""
    geometry, _, _ = build_geometry(tmp_path)
    contour = geometry.profile_coordinates
    assert contour is not None and len(contour) > 100
    xs = [x for x, _ in contour]
    assert max(xs) == pytest.approx(CHORD_MM / 1000.0 * math.cos(math.radians(AOA_DEG)),
                                    rel=1e-3)


def test_un_profil_naca_passe_toujours_par_le_meme_backend(tmp_path):
    """La v1.0 ne doit rien perdre : mêmes paramètres, même chemin, même STL."""
    from pipeline.utils import save_design_params

    config = save_design_params({
        "iteration": 0,
        "design_id": "naca_temoin",
        "parameters": {
            "chord": {"value": 300.0, "min": 200.0, "max": 400.0,
                      "max_delta_pct": 10.0, "unit": "mm"},
            "span": {"value": 80.0, "min": 79.0, "max": 81.0,
                     "max_delta_pct": 1.0, "unit": "mm"},
            "thickness": {"value": 0.12, "min": 0.08, "max": 0.18,
                          "max_delta_pct": 10.0, "unit": "unitless"},
            "camber": {"value": 0.02, "min": 0.0, "max": 0.06,
                       "max_delta_pct": 10.0, "unit": "unitless"},
            "aoa": {"value": 3.0, "min": -2.0, "max": 12.0,
                    "max_delta_pct": 12.0, "unit": "deg"},
        },
        "constraints": {"topology_preserving": True, "min_wall_thickness_mm": 1.5},
        "objectives": {"primary": "maximize_Cl_Cd"},
    }, tmp_path / "design_params.yaml")

    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    geometry = get_backend("internal").generate(config, out)

    assert geometry.success, geometry.message
    assert geometry.stl_path.is_file()
    assert "parameterization" not in geometry.geometry


# ─────────────────────────────────────────────────────────────────────────────
# L'aller-retour
# ─────────────────────────────────────────────────────────────────────────────


def test_la_section_relue_est_le_profil_entier(tmp_path):
    geometry, _, _ = build_geometry(tmp_path)
    section = extract_section(geometry.stl_path)
    assert len(section) > 150

    xs = [x for x, _ in section]
    assert min(xs) == pytest.approx(0.0, abs=1e-6)
    assert max(xs) == pytest.approx(0.3 * math.cos(math.radians(AOA_DEG)), rel=1e-3)


def test_le_stl_est_ecrit_en_metres(tmp_path):
    """Une corde de 300 mm doit mesurer 0,3 dans le fichier — ni 30, ni 0,003."""
    geometry, _, _ = build_geometry(tmp_path)
    vertices = read_stl_vertices(geometry.stl_path)
    etendue = max(x for x, _, _ in vertices) - min(x for x, _, _ in vertices)
    assert etendue == pytest.approx(0.3 * math.cos(math.radians(AOA_DEG)), rel=1e-3)


def test_l_aller_retour_est_conforme_sur_un_profil_cambre(tmp_path):
    geometry, result, original = build_geometry(tmp_path, "naca2412")
    report = check_roundtrip(
        geometry.stl_path, original.upper, original.lower,
        chord_m=CHORD_MM / 1000.0, aoa_rad=math.radians(AOA_DEG),
    )
    assert report.success, report.message
    assert report.max_error < ROUNDTRIP_TOLERANCE
    assert report.coverage_error < ROUNDTRIP_TOLERANCE


def test_l_aller_retour_est_conforme_sur_un_profil_symetrique(tmp_path):
    geometry, _, original = build_geometry(tmp_path, "naca0012")
    report = check_roundtrip(
        geometry.stl_path, original.upper, original.lower,
        chord_m=CHORD_MM / 1000.0, aoa_rad=math.radians(AOA_DEG),
    )
    assert report.success, report.message


def test_l_ecart_d_aller_retour_ne_depasse_pas_celui_de_l_ajustement(tmp_path):
    """L'écriture ne doit rien ajouter à l'approximation CST.

    Si le solide s'écarte plus du fichier d'origine que ne le fait
    l'ajustement, c'est que quelque chose s'est perdu APRÈS les coefficients —
    et c'est précisément ce que la Phase 3 ne pouvait pas voir.
    """
    geometry, result, original = build_geometry(tmp_path, "naca2412")
    report = check_roundtrip(
        geometry.stl_path, original.upper, original.lower,
        chord_m=CHORD_MM / 1000.0, aoa_rad=math.radians(AOA_DEG),
    )
    assert report.max_error < 1.5 * result.error.max_error


def test_une_erreur_d_echelle_est_attrapee(tmp_path):
    """Le contrôle doit refuser un solide dix fois trop grand."""
    geometry, _, original = build_geometry(tmp_path)
    gonfle = tmp_path / "gonfle.stl"
    gonfle.write_text(
        "\n".join(
            _scale_vertex(line, 10.0)
            for line in geometry.stl_path.read_text(encoding="utf-8").splitlines()
        ),
        encoding="utf-8",
    )

    report = check_roundtrip(
        gonfle, original.upper, original.lower,
        chord_m=CHORD_MM / 1000.0, aoa_rad=math.radians(AOA_DEG),
    )
    assert not report.success
    assert "échelle" in report.message


def test_un_solide_ampute_est_attrape(tmp_path):
    """Un STL privé de son bord d'attaque : chaque sommet restant est juste.

    C'est le cas que la seule mesure de forme laisserait passer — d'où la
    mesure de couverture.
    """
    geometry, _, original = build_geometry(tmp_path)
    ampute = tmp_path / "ampute.stl"
    ampute.write_text(
        _drop_nose(geometry.stl_path.read_text(encoding="utf-8"), 0.03),
        encoding="utf-8",
    )

    report = check_roundtrip(
        ampute, original.upper, original.lower,
        chord_m=CHORD_MM / 1000.0, aoa_rad=math.radians(AOA_DEG),
    )
    assert not report.success
    assert "couvert" in report.message
    assert report.max_error < ROUNDTRIP_TOLERANCE  # la forme, elle, reste juste


def test_un_stl_absent_est_un_compte_rendu_pas_une_exception(tmp_path):
    report = check_roundtrip(tmp_path / "rien.stl", [(0.0, 0.0)], [(0.0, 0.0)], 0.3)
    assert not report.success
    assert "introuvable" in report.message


def test_un_stl_illisible_est_un_compte_rendu_pas_une_exception(tmp_path):
    casse = tmp_path / "casse.stl"
    casse.write_text("solid x\n  vertex 1.0 2.0\nendsolid x\n", encoding="utf-8")
    report = check_roundtrip(casse, [(0.0, 0.0)], [(0.0, 0.0)], 0.3)
    assert not report.success
    assert "illisible" in report.message


def test_une_corde_non_positive_est_un_compte_rendu(tmp_path):
    report = check_roundtrip(tmp_path / "rien.stl", [], [], 0.0)
    assert not report.success
    assert "corde" in report.message


def test_le_contour_de_reference_ferme_le_profil():
    contour = reference_contour([(0.0, 0.0), (1.0, 0.0)], [(0.0, 0.0), (1.0, 0.0)], 2.0)
    assert contour[0] == pytest.approx((0.0, 0.0))
    assert contour[1] == pytest.approx((2.0, 0.0))


def _scale_vertex(line: str, factor: float) -> str:
    stripped = line.strip()
    if not stripped.startswith("vertex"):
        return line
    parts = stripped.split()
    return "      vertex " + " ".join(f"{float(p) * factor:.8e}" for p in parts[1:])


def _drop_nose(text: str, threshold: float) -> str:
    """Retire les facettes dont un sommet est en deçà de `threshold` en x."""
    lines = text.splitlines()
    kept: list[str] = [lines[0]]
    block: list[str] = []
    for line in lines[1:]:
        stripped = line.strip()
        if stripped.startswith("facet normal"):
            block = [line]
            continue
        block.append(line)
        if stripped.startswith("endfacet"):
            xs = [
                float(entry.split()[1])
                for entry in block
                if entry.strip().startswith("vertex")
            ]
            if min(xs) >= threshold:
                kept.extend(block)
            block = []
        elif stripped.startswith("endsolid"):
            kept.append(line)
    if not kept[-1].strip().startswith("endsolid"):
        kept.append("endsolid wing")
    return "\n".join(kept) + "\n"
