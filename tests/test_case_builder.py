"""Tests du constructeur de case OpenFOAM (Phase 2), sans OpenFOAM.

Tout ce qui est testé ici est du Python pur : dimensionnement du domaine,
grandeurs turbulentes, références aérodynamiques, rendu des templates, lecture
des STL et contrôle de cohérence géométrique. Ce qui exige un vrai solveur
(blockMesh, snappyHexMesh, simpleFoam) n'est pas couvert.
"""

import json
import math
import struct
from pathlib import Path

import pytest
import yaml

from openfoam import case_builder as cb
from pipeline.utils import load_yaml

ROOT = Path(__file__).resolve().parents[1]
REAL_DESIGN = ROOT / "configs" / "design_params.yaml"
REAL_CFD = ROOT / "configs" / "cfd_settings.yaml"
TEMPLATE_DIR = ROOT / "openfoam" / "templates" / "external_aero"


@pytest.fixture
def design() -> dict:
    return load_yaml(REAL_DESIGN)


@pytest.fixture
def cfd() -> dict:
    return load_yaml(REAL_CFD)


@pytest.fixture
def values(design, cfd) -> dict:
    return cb.compute_case_values(design, cfd)


# ─────────────────────────────────────────────────────────────
# Fabrication de STL de test
# ─────────────────────────────────────────────────────────────


def write_ascii_stl(path: Path, triangles) -> Path:
    lines = ["solid wing"]
    for tri in triangles:
        lines.append("  facet normal 0 0 1")
        lines.append("    outer loop")
        for x, y, z in tri:
            lines.append(f"      vertex {x} {y} {z}")
        lines.append("    endloop")
        lines.append("  endfacet")
    lines.append("endsolid wing")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_binary_stl(path: Path, triangles) -> Path:
    payload = b"binary STL produit par les tests".ljust(80, b"\0")
    payload += struct.pack("<I", len(triangles))
    for tri in triangles:
        payload += struct.pack("<3f", 0.0, 0.0, 1.0)
        for x, y, z in tri:
            payload += struct.pack("<3f", x, y, z)
        payload += struct.pack("<H", 0)
    path.write_bytes(payload)
    return path


def wing_triangles(chord=0.3, span=0.08, thickness=0.036):
    """Deux triangles couvrant l'emprise d'une aile — suffisant pour la bbox."""
    return [
        [(0.0, -thickness / 2, 0.0), (chord, -thickness / 2, 0.0), (chord, thickness / 2, span)],
        [(0.0, -thickness / 2, 0.0), (chord, thickness / 2, span), (0.0, thickness / 2, span)],
    ]


# ─────────────────────────────────────────────────────────────
# Lecture des STL
# ─────────────────────────────────────────────────────────────


def test_bbox_stl_ascii(tmp_path):
    p = write_ascii_stl(tmp_path / "w.stl", wing_triangles())
    bbox = cb.stl_bounding_box(p)
    assert bbox["x_min"] == pytest.approx(0.0)
    assert bbox["x_max"] == pytest.approx(0.3)
    assert bbox["z_max"] == pytest.approx(0.08)
    assert bbox["n_vertices"] == 6


def test_bbox_stl_binaire(tmp_path):
    p = write_binary_stl(tmp_path / "w.stl", wing_triangles())
    bbox = cb.stl_bounding_box(p)
    assert bbox["x_max"] == pytest.approx(0.3, rel=1e-5)
    assert bbox["z_max"] == pytest.approx(0.08, rel=1e-5)


def test_ascii_et_binaire_donnent_la_meme_bbox(tmp_path):
    a = cb.stl_bounding_box(write_ascii_stl(tmp_path / "a.stl", wing_triangles()))
    b = cb.stl_bounding_box(write_binary_stl(tmp_path / "b.stl", wing_triangles()))
    for key in ("x_min", "x_max", "y_min", "y_max", "z_min", "z_max"):
        assert a[key] == pytest.approx(b[key], rel=1e-5)


def test_stl_binaire_commencant_par_solid(tmp_path):
    # Piège classique : un STL binaire dont l'en-tête commence par "solid".
    # Seule la taille du fichier permet de trancher.
    p = tmp_path / "piege.stl"
    payload = b"solid ceci est un binaire".ljust(80, b"\0")
    tris = wing_triangles()
    payload += struct.pack("<I", len(tris))
    for tri in tris:
        payload += struct.pack("<3f", 0.0, 0.0, 1.0)
        for x, y, z in tri:
            payload += struct.pack("<3f", x, y, z)
        payload += struct.pack("<H", 0)
    p.write_bytes(payload)
    assert cb.stl_bounding_box(p)["x_max"] == pytest.approx(0.3, rel=1e-5)


def test_stl_absent(tmp_path):
    with pytest.raises(cb.CaseBuildError) as exc:
        cb.stl_bounding_box(tmp_path / "absent.stl")
    assert exc.value.status == cb.STATUS_GEOMETRY_MISSING


def test_stl_vide(tmp_path):
    p = tmp_path / "vide.stl"
    p.write_text("", encoding="utf-8")
    with pytest.raises(cb.CaseBuildError):
        cb.stl_bounding_box(p)


# ─────────────────────────────────────────────────────────────
# Grandeurs calculées
# ─────────────────────────────────────────────────────────────


def test_corde_et_envergure_en_metres(values):
    assert values["_design"]["chord_m"] == pytest.approx(0.30)
    assert values["_design"]["span_m"] == pytest.approx(0.08)


def test_reynolds(values):
    # 20 m/s x 0.3 m / 1.5e-5 = 4e5
    assert values["_design"]["reynolds"] == pytest.approx(4.0e5, rel=1e-3)


def test_domaine_proportionnel_a_la_corde(design, cfd):
    v1 = cb.compute_case_values(design, cfd)
    design["parameters"]["chord"]["value"] = 400.0
    v2 = cb.compute_case_values(design, cfd)
    # Le domaine suit la corde : 5 corde en amont, 12 en aval.
    assert v2["X_MIN"] == pytest.approx(v1["X_MIN"] * 400.0 / 300.0)
    assert v2["X_MAX"] == pytest.approx(v1["X_MAX"] * 400.0 / 300.0)


def test_maillage_de_fond_reste_a_lechelle(design, cfd):
    # base_cell_per_chord = 8 : la maille suit la corde, donc le nombre de
    # cellules du maillage de fond ne doit PAS exploser quand la corde grandit.
    v1 = cb.compute_case_values(design, cfd)
    design["parameters"]["chord"]["value"] = 420.0
    v2 = cb.compute_case_values(design, cfd)
    assert v2["_design"]["background_cells"] == pytest.approx(
        v1["_design"]["background_cells"], rel=0.05
    )


def test_grandeurs_turbulentes(values, cfd):
    u = cfd["flow"]["velocity_ms"]
    i = cfd["flow"]["turbulent_intensity"]
    length = cfd["flow"]["turbulent_length_scale_m"]
    k = 1.5 * (i * u) ** 2
    assert values["K_INF"] == pytest.approx(k)
    assert values["OMEGA_INF"] == pytest.approx(math.sqrt(k) / (0.09**0.25 * length))


def test_vitesse_dentree_alignee_sur_x(values):
    assert values["U_INF_VECTOR"] == "20 0 0"


def test_portance_selon_y(values):
    # Le profil est dans le plan XY, extrudé selon Z : la portance est en +Y.
    assert values["LIFT_DIR"] == "0 1 0"
    assert values["DRAG_DIR"] == "1 0 0"
    assert values["PITCH_AXIS"] == "0 0 1"


def test_surface_de_reference_suit_la_corde(design, cfd):
    a1 = cb.compute_case_values(design, cfd)["A_REF"]
    design["parameters"]["chord"]["value"] = 400.0
    a2 = cb.compute_case_values(design, cfd)["A_REF"]
    # Aref = corde x envergure du domaine : figer Aref pendant que la corde
    # varie rendrait les Cd/Cl incomparables entre itérations.
    assert a2 / a1 == pytest.approx(400.0 / 300.0)


def test_surface_de_reference_quasi_2d(values):
    # En quasi-2D seule la tranche modélisée porte des efforts : Aref doit être
    # corde x envergure DU DOMAINE (50 % de 80 mm), pas de l'aile entière.
    assert values["_design"]["domain_span_m"] == pytest.approx(0.04)
    assert values["A_REF"] == pytest.approx(0.3 * 0.04)


def test_surface_de_reference_fixe(design, cfd):
    cfd["reference"]["mode"] = "fixed"
    cfd["reference"]["area_m2"] = 0.5
    cfd["reference"]["length_m"] = 0.7
    v = cb.compute_case_values(design, cfd)
    assert v["A_REF"] == 0.5 and v["L_REF"] == 0.7


def test_mode_de_reference_inconnu(design, cfd):
    cfd["reference"]["mode"] = "magique"
    with pytest.raises(cb.CaseBuildError):
        cb.compute_case_values(design, cfd)


def test_tranche_de_symetrie_interieure_a_laile(values):
    # Les plans de symétrie doivent être STRICTEMENT à l'intérieur de
    # l'envergure, sinon un bout d'aile se retrouve dans le domaine.
    assert values["Z_MIN"] > 0.0
    assert values["Z_MAX"] < 0.08
    assert values["SIDE_PATCH_TYPE"] == "symmetry"
    assert values["SIDE_FIELD_TYPE"] == "symmetry"


def test_full_3d_englobe_toute_laile(design, cfd):
    cfd["domain"]["spanwise_treatment"] = "full_3d"
    v = cb.compute_case_values(design, cfd)
    assert v["Z_MIN"] < 0.0 and v["Z_MAX"] > 0.08
    assert v["SIDE_PATCH_TYPE"] == "patch"
    assert v["A_REF"] == pytest.approx(0.3 * 0.08)


def test_traitement_spanwise_inconnu(design, cfd):
    cfd["domain"]["spanwise_treatment"] = "periodique"
    with pytest.raises(cb.CaseBuildError):
        cb.compute_case_values(design, cfd)


def test_location_in_mesh_hors_de_laile(values):
    x, y, z = (float(v) for v in values["LOCATION_IN_MESH"].split())
    assert x < 0.0          # en amont du bord d'attaque
    assert y > 0.05         # bien au dessus du profil
    assert values["Z_MIN"] < z < values["Z_MAX"]


def test_centre_de_reduction_au_quart_de_corde(values):
    x = float(values["COFR"].split()[0])
    assert x == pytest.approx(0.25 * 0.30)


@pytest.mark.parametrize("param", ["chord", "span", "aoa"])
def test_parametre_manquant(design, cfd, param):
    design["parameters"].pop(param)
    with pytest.raises(cb.CaseBuildError) as exc:
        cb.compute_case_values(design, cfd)
    assert param in exc.value.message


def test_corde_sans_unite_de_longueur(design, cfd):
    design["parameters"]["chord"]["unit"] = "unitless"
    with pytest.raises(cb.CaseBuildError):
        cb.compute_case_values(design, cfd)


def test_vitesse_nulle_refusee(design, cfd):
    cfd["flow"]["velocity_ms"] = 0.0
    with pytest.raises(cb.CaseBuildError):
        cb.compute_case_values(design, cfd)


# ─────────────────────────────────────────────────────────────
# Rendu des templates
# ─────────────────────────────────────────────────────────────


def test_rendu_substitue_les_jetons():
    assert cb.render("a @@X@@ b", {"X": 3}) == "a 3 b"


def test_jeton_non_substitue_detecte():
    with pytest.raises(cb.CaseBuildError) as exc:
        cb.render("valeur @@INCONNU@@;", {"X": 1})
    assert exc.value.status == cb.STATUS_TEMPLATE_ERROR
    assert "INCONNU" in exc.value.message


def test_les_booleens_sont_rendus_en_minuscules():
    assert cb.render("@@F@@", {"F": True}) == "true"


def test_tous_les_jetons_des_templates_sont_couverts(values):
    # Verrou d'intégrité : chaque @@JETON@@ des templates doit avoir une valeur.
    # C'est ce qui évite qu'un template et le builder divergent en silence.
    manquants = {}
    for path in sorted(TEMPLATE_DIR.rglob("*")):
        if path.is_file():
            tokens = cb._find_placeholders(path.read_text(encoding="utf-8"))
            absents = tokens - set(values)
            if absents:
                manquants[str(path.relative_to(TEMPLATE_DIR))] = sorted(absents)
    assert manquants == {}


def test_chaque_template_se_rend_sans_reste(values):
    for path in sorted(TEMPLATE_DIR.rglob("*")):
        if path.is_file():
            cb.render(path.read_text(encoding="utf-8"), values)  # ne doit pas lever


# ─────────────────────────────────────────────────────────────
# Contrôle de cohérence géométrique
# ─────────────────────────────────────────────────────────────


def test_emprise_attendue_depuis_la_config(design):
    expected = cb.expected_bounding_box(design)
    assert expected is not None
    assert expected["x_max"] == pytest.approx(0.30, rel=1e-3)
    assert expected["z_max"] == pytest.approx(0.08, rel=1e-3)


def test_geometrie_conforme_acceptee(design):
    expected = cb.expected_bounding_box(design)
    assert cb.check_geometry(dict(expected), expected, 0.3, 5.0) == []


def test_geometrie_a_la_mauvaise_echelle_refusee(design):
    expected = cb.expected_bounding_box(design)
    actual = {k: v * 10 for k, v in expected.items()}   # export en cm pris pour des m
    with pytest.raises(cb.CaseBuildError) as exc:
        cb.check_geometry(actual, expected, 0.3, 5.0)
    assert exc.value.status == cb.STATUS_GEOMETRY_MISMATCH


def test_geometrie_inchangee_detectee(design):
    # Le cas qui compte : l'itération demande une corde de 400 mm, mais le STEP
    # exporté est encore celui de 300 mm.
    expected_ancien = cb.expected_bounding_box(design)
    design["parameters"]["chord"]["value"] = 400.0
    expected_nouveau = cb.expected_bounding_box(design)
    with pytest.raises(cb.CaseBuildError) as exc:
        cb.check_geometry(expected_ancien, expected_nouveau, 0.4, 5.0)
    assert "ne correspond pas" in exc.value.message


def test_petit_ecart_tolere(design):
    expected = cb.expected_bounding_box(design)
    actual = dict(expected)
    actual["x_max"] += 0.3 * 0.02          # 2 % de corde, sous la tolérance de 5 %
    assert cb.check_geometry(actual, expected, 0.3, 5.0) == []


def test_emprise_non_calculable_avertit_sans_bloquer():
    warnings = cb.check_geometry({"x_min": 0}, None, 0.3, 5.0)
    assert warnings and "non vérifiée" in warnings[0]


# ─────────────────────────────────────────────────────────────
# Construction complète
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def iteration_dir(tmp_path, design) -> Path:
    """Dossier d'itération contenant un STL cohérent avec la configuration."""
    d = tmp_path / "iter_0000"
    d.mkdir()
    expected = cb.expected_bounding_box(design)
    write_ascii_stl(
        d / "geometry.stl",
        [
            [(expected["x_min"], expected["y_min"], expected["z_min"]),
             (expected["x_max"], expected["y_min"], expected["z_min"]),
             (expected["x_max"], expected["y_max"], expected["z_max"])],
            [(expected["x_min"], expected["y_min"], expected["z_min"]),
             (expected["x_max"], expected["y_max"], expected["z_max"]),
             (expected["x_min"], expected["y_max"], expected["z_max"])],
        ],
    )
    return d


def test_construction_complete(iteration_dir):
    summary = cb.build_case(iteration_dir, REAL_DESIGN, REAL_CFD)
    case = Path(summary["case_dir"])
    for expected_file in (
        "system/controlDict", "system/blockMeshDict", "system/snappyHexMeshDict",
        "system/fvSchemes", "system/fvSolution", "system/decomposeParDict",
        "system/meshQualityDict", "system/surfaceFeatureExtractDict",
        "constant/transportProperties", "constant/turbulenceProperties",
        "0/U", "0/p", "0/k", "0/omega", "0/nut",
        "constant/triSurface/wing.stl",
    ):
        assert (case / expected_file).is_file(), expected_file
    assert summary["warnings"] == []


def test_le_case_ne_contient_plus_de_jeton(iteration_dir):
    summary = cb.build_case(iteration_dir, REAL_DESIGN, REAL_CFD)
    for path in Path(summary["case_dir"]).rglob("*"):
        if path.is_file() and path.suffix != ".stl" and path.name != "case_summary.json":
            assert "@@" not in path.read_text(encoding="utf-8"), path


def test_les_valeurs_arrivent_dans_le_case(iteration_dir):
    summary = cb.build_case(iteration_dir, REAL_DESIGN, REAL_CFD)
    control = (Path(summary["case_dir"]) / "system" / "controlDict").read_text()
    assert "application     simpleFoam;" in control
    assert "magUInf         20;" in control
    assert "liftDir         (0 1 0);" in control
    transport = (Path(summary["case_dir"]) / "constant" / "transportProperties").read_text()
    assert "nu              1.5e-05;" in transport


def test_reconstruction_ecrase_le_case_precedent(iteration_dir):
    first = cb.build_case(iteration_dir, REAL_DESIGN, REAL_CFD)
    intrus = Path(first["case_dir"]) / "1000" / "U"
    intrus.parent.mkdir(parents=True)
    intrus.write_text("ancien pas de temps", encoding="utf-8")
    cb.build_case(iteration_dir, REAL_DESIGN, REAL_CFD)
    # Un reliquat de l'itération précédente fausserait le calcul en silence.
    assert not intrus.exists()


def test_resume_ecrit_dans_le_case(iteration_dir):
    summary = cb.build_case(iteration_dir, REAL_DESIGN, REAL_CFD)
    on_disk = json.loads(
        (Path(summary["case_dir"]) / "case_summary.json").read_text(encoding="utf-8")
    )
    assert on_disk["iteration"] == 0
    assert on_disk["geometry"]["bounding_box_m"]["x_max"] == pytest.approx(0.3, rel=1e-3)


def test_geometrie_absente(tmp_path):
    vide = tmp_path / "iter_0001"
    vide.mkdir()
    with pytest.raises(cb.CaseBuildError) as exc:
        cb.build_case(vide, REAL_DESIGN, REAL_CFD)
    assert exc.value.status == cb.STATUS_GEOMETRY_MISSING


def test_step_sans_convertisseur(tmp_path, monkeypatch):
    d = tmp_path / "iter_0002"
    d.mkdir()
    (d / "geometry.step").write_text("ISO-10303-21;", encoding="utf-8")
    monkeypatch.setattr(cb.shutil, "which", lambda name: None)
    with pytest.raises(cb.CaseBuildError) as exc:
        cb.build_case(d, REAL_DESIGN, REAL_CFD)
    assert exc.value.status == cb.STATUS_GEOMETRY_CONVERSION_FAILED
    assert "gmsh" in exc.value.message


def test_cli_construit_le_case(iteration_dir, capsys):
    code = cb.main(["--iteration-dir", str(iteration_dir),
                    "--design-params", str(REAL_DESIGN),
                    "--cfd-settings", str(REAL_CFD)])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["files_rendered"] >= 15


def test_cli_signale_lechec_en_json(tmp_path, capsys):
    vide = tmp_path / "iter_0003"
    vide.mkdir()
    code = cb.main(["--iteration-dir", str(vide),
                    "--design-params", str(REAL_DESIGN),
                    "--cfd-settings", str(REAL_CFD)])
    assert code == 1
    assert json.loads(capsys.readouterr().err)["status"] == cb.STATUS_GEOMETRY_MISSING


def test_cli_print_values(capsys):
    assert cb.main(["--iteration-dir", ".", "--print-values",
                    "--design-params", str(REAL_DESIGN),
                    "--cfd-settings", str(REAL_CFD)]) == 0
    assert "A_REF" in json.loads(capsys.readouterr().out)
