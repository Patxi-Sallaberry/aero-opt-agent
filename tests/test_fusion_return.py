"""Phase 6 — le chemin de retour vers Fusion.

Le §5 du document maître en fait une exigence de premier plan, et pour une
raison concrète : une optimisation qui ne rend qu'un STL est un cul-de-sac.
Un STL est un solide facetté de plusieurs centaines de faces — on peut
l'imprimer, on ne peut pas y poser un congé ni en changer une cote.

Ce que ces cas vérifient tient en trois points. Les fichiers de reprise sont
produits à CHAQUE export, pas sur demande. Le script Fusion généré est
syntaxiquement valide et porte les bonnes coordonnées dans la bonne unité — les
centimètres de l'API, pas les millimètres affichés. Et le document de reprise
dit la vérité sur ce que contient réellement le dossier.
"""

from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

from profiles.reparameterize import reparameterize
from scripts.fusion_return import (
    RETURN_DOC,
    SKETCH_SCRIPT,
    build_return_doc,
    build_sketch_script,
    write_fusion_return,
)

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "profiles"


def section_of(name: str = "clarky", chord_mm: float = 300.0) -> dict:
    """Section telle que `export_best.write_profile_section` la produit."""
    from fusion.parametric_driver import profile_from_parameters

    result = reparameterize(EXAMPLES / f"{name}.dat", chord_mm=chord_mm,
                            span_mm=80.0, aoa_deg=3.0)
    assert result.success, result.message
    design = result.design_params
    plan = profile_from_parameters(
        design["parameters"], design.get("parameterization"),
        design.get("provenance"),
    )
    return design, {
        "upper": [(x * 10.0, y * 10.0) for x, y in plan["profile"]["upper"]],
        "lower": [(x * 10.0, y * 10.0) for x, y in plan["profile"]["lower"]],
        "chord_mm": plan["chord_cm"] * 10.0,
        "span_mm": plan["span_cm"] * 10.0,
        "aoa_deg": plan["aoa_deg"],
        "thickness": plan["thickness"],
        "camber": plan["camber"],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Le script Fusion
# ─────────────────────────────────────────────────────────────────────────────


def test_le_script_genere_est_du_python_valide():
    """Un script qui ne se compile pas ne se découvre qu'une fois dans Fusion."""
    design, section = section_of()
    source = build_sketch_script(
        section["upper"], section["lower"], section["span_mm"]
    )
    ast.parse(source)  # lève SyntaxError si le script est cassé


def test_le_script_porte_les_coordonnees_en_centimetres():
    """L'API Fusion travaille en cm, quelle que soit l'unité du document.

    C'est le genre de conversion qu'on croit évidente et qu'on rate : la v1.0
    a livré une géométrie dix fois trop petite pour exactement cette raison.
    """
    design, section = section_of(chord_mm=300.0)
    source = build_sketch_script(
        section["upper"], section["lower"], section["span_mm"]
    )
    tree = ast.parse(source)
    upper = next(
        node.value for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", "") == "UPPER"
    )
    points = ast.literal_eval(upper)

    étendue = max(x for x, _ in points) - min(x for x, _ in points)
    attendue = 30.0 * math.cos(math.radians(3.0))  # 300 mm -> 30 cm
    assert étendue == pytest.approx(attendue, rel=1e-2)


def test_le_script_extrude_sur_la_bonne_envergure():
    design, section = section_of()
    source = build_sketch_script(
        section["upper"], section["lower"], span_mm=80.0
    )
    tree = ast.parse(source)
    span = next(
        node.value for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", "") == "SPAN_CM"
    )
    assert ast.literal_eval(span) == pytest.approx(8.0)


def test_le_script_trace_une_spline_par_surface():
    """Une spline unique sur tout le contour placerait une inflexion au nez.

    C'est précisément la zone qui décide du décrochage : l'y abîmer viderait
    l'optimisation de son sens.
    """
    design, section = section_of()
    source = build_sketch_script(
        section["upper"], section["lower"], section["span_mm"]
    )
    assert "for points in (UPPER, LOWER):" in source


def test_le_script_allege_un_contour_trop_dense():
    """Une spline qui interpole trois cents points ondule entre eux."""
    dense = [(i / 400.0 * 100.0, 5.0) for i in range(401)]
    source = build_sketch_script(dense, dense, 80.0)
    tree = ast.parse(source)
    upper = next(
        node.value for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", "") == "UPPER"
    )
    points = ast.literal_eval(upper)
    assert len(points) <= 120


def test_l_allegement_conserve_les_extremites():
    """Perdre le bord de fuite raccourcirait la corde sans prévenir."""
    dense = [(i / 400.0 * 100.0, 5.0) for i in range(401)]
    source = build_sketch_script(dense, dense, 80.0)
    tree = ast.parse(source)
    upper = next(
        node.value for node in tree.body
        if isinstance(node, ast.Assign)
        and getattr(node.targets[0], "id", "") == "UPPER"
    )
    points = ast.literal_eval(upper)
    assert points[0][0] == pytest.approx(0.0, abs=1e-9)
    assert points[-1][0] == pytest.approx(10.0, abs=1e-6)  # 100 mm -> 10 cm


def test_le_script_referme_un_bord_de_fuite_ouvert():
    """Le Clark Y en a un : sans la ligne de fermeture, aucun profil fermé."""
    design, section = section_of("clarky")
    source = build_sketch_script(
        section["upper"], section["lower"], section["span_mm"]
    )
    assert "addByTwoPoints" in source


# ─────────────────────────────────────────────────────────────────────────────
# Le document de reprise
# ─────────────────────────────────────────────────────────────────────────────


def test_le_document_decrit_les_trois_voies():
    design, section = section_of()
    doc = build_return_doc(design, section, has_step=False)
    assert "Route 1" in doc and "Route 2" in doc and "Route 3" in doc


def test_le_document_donne_les_cotes_de_la_forme():
    design, section = section_of(chord_mm=250.0)
    doc = build_return_doc(design, section, has_step=False)
    assert "250.00 mm" in doc
    assert "3.00°" in doc


def test_le_document_signale_l_absence_de_step():
    """Chercher un STEP qui n'existe pas fait perdre un quart d'heure."""
    design, section = section_of()
    sans = build_return_doc(design, section, has_step=False)
    avec = build_return_doc(design, section, has_step=True)
    assert "there is none" in sans.lower()
    assert "there is none" not in avec.lower()


def test_le_document_met_le_step_en_avant_quand_il_existe():
    """C'est la voie la plus courte : elle doit être la première proposée."""
    design, section = section_of()
    avec = build_return_doc(design, section, has_step=True)
    assert "geometry.step" in avec
    assert avec.index("geometry.step") < avec.index("Route 1")


def test_le_document_previent_de_ne_pas_ouvrir_le_stl_a_la_place():
    """Fusion ouvre le STL sans broncher — et en fait un maillage inutilisable."""
    design, section = section_of()
    avec = build_return_doc(design, section, has_step=True)
    assert "geometry.stl" in avec
    assert "mesh body" in avec


def test_l_absence_de_step_nomme_la_dependance():
    """Dire qu'il manque quelque chose sans dire comment l'obtenir n'aide pas."""
    design, section = section_of()
    sans = build_return_doc(design, section, has_step=False)
    assert "requirements-cad.txt" in sans


def test_le_document_previent_du_double_comptage_de_l_incidence():
    """L'erreur la plus facile à commettre en reprenant la section."""
    design, section = section_of()
    doc = build_return_doc(design, section, has_step=False)
    assert "counted twice" in doc


def test_le_document_deconseille_la_conversion_du_stl():
    design, section = section_of()
    doc = build_return_doc(design, section, has_step=False)
    assert "geometry.stl" in doc
    assert "faces" in doc


def test_le_document_annonce_la_parametrisation_cst():
    design, section = section_of()
    doc = build_return_doc(design, section, has_step=False)
    assert "`cst`" in doc
    assert "Kulfan" in doc


def test_le_document_propose_de_verifier_la_reprise():
    """La reprise doit être vérifiable, pas seulement décrite."""
    design, section = section_of()
    doc = build_return_doc(design, section, has_step=False)
    assert "profiles.roundtrip" in doc


# ─────────────────────────────────────────────────────────────────────────────
# L'écriture dans le paquet
# ─────────────────────────────────────────────────────────────────────────────


def test_les_deux_fichiers_sont_ecrits(tmp_path):
    design, section = section_of()
    written = write_fusion_return(tmp_path, design, section)
    assert {p.name for p in written} == {RETURN_DOC, SKETCH_SCRIPT}
    assert all(p.is_file() and p.stat().st_size > 500 for p in written)


def test_l_ecriture_cree_le_dossier_au_besoin(tmp_path):
    design, section = section_of()
    cible = tmp_path / "best_design"
    write_fusion_return(cible, design, section)
    assert (cible / RETURN_DOC).is_file()


def test_une_section_vide_ne_fait_pas_tout_echouer(tmp_path):
    """Un export dégradé doit rester un export, pas une exception."""
    written = write_fusion_return(
        tmp_path, {"design_id": "x"},
        {"chord_mm": 0.0, "span_mm": 0.0, "aoa_deg": 0.0},
    )
    assert len(written) == 2
