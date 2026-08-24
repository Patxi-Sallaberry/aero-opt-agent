"""Tests du producteur de géométrie interne (sans Fusion).

C'est ce mode qui rend la boucle d'optimisation autonome : sans lui, chaque
itération attendrait qu'un humain lance le script dans Fusion 360, dont l'API
n'a pas de mode headless.

La forme produite doit être RIGOUREUSEMENT celle du mode rebuild de Fusion —
c'est la même fonction de profil — et le STL doit être directement exploitable
par snappyHexMesh.
"""

import json
import math
from pathlib import Path

import pytest
import yaml

from fusion import parametric_driver as pd
from openfoam import case_builder as cb
from pipeline.utils import load_yaml

ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "configs" / "design_params.yaml"
REAL_CFD = ROOT / "configs" / "cfd_settings.yaml"


@pytest.fixture
def plan() -> dict:
    return pd.profile_from_parameters(load_yaml(REAL_CONFIG)["parameters"])


# ─────────────────────────────────────────────────────────────
# Choix du producteur
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "requested,attendu",
    [("fusion", "fusion"), ("internal", "internal"), ("INTERNAL", "internal")],
)
def test_resolution_explicite(requested, attendu, monkeypatch):
    monkeypatch.delenv("FUSION_GEOMETRY_BACKEND", raising=False)
    assert pd.resolve_geometry_backend(requested) == attendu


def test_auto_retombe_sur_interne_hors_fusion(monkeypatch):
    monkeypatch.delenv("FUSION_GEOMETRY_BACKEND", raising=False)
    assert pd.FUSION_AVAILABLE is False       # ce test tourne hors Fusion
    assert pd.resolve_geometry_backend("auto") == "internal"
    assert pd.resolve_geometry_backend(None) == "internal"


def test_backend_depuis_lenvironnement(monkeypatch):
    monkeypatch.setenv("FUSION_GEOMETRY_BACKEND", "fusion")
    assert pd.resolve_geometry_backend() == "fusion"


def test_backend_inconnu_refuse(monkeypatch):
    monkeypatch.delenv("FUSION_GEOMETRY_BACKEND", raising=False)
    with pytest.raises(pd.DriverError):
        pd.resolve_geometry_backend("solidworks")


# ─────────────────────────────────────────────────────────────
# Triangulation
# ─────────────────────────────────────────────────────────────


def test_nombre_de_facettes(plan):
    facets = pd._triangulate_prism(plan)
    n = plan["n_points"]
    # Surfaces extrudées : 2 x n segments x 2 triangles.
    # Faces d'extrémité : 2 x (2n - 2) — les deux triangles d'aire nulle du
    # bord d'attaque et du bord de fuite sont écartés.
    assert len(facets) == 4 * n + 2 * (2 * n - 2)


def test_aucune_facette_degeneree(plan):
    for _, t in pd._triangulate_prism(plan):
        assert len({tuple(round(c, 12) for c in p) for p in t}) == 3


def test_toutes_les_normales_sont_unitaires(plan):
    for normal, _ in pd._triangulate_prism(plan):
        assert math.sqrt(sum(c * c for c in normal)) == pytest.approx(1.0)


def test_les_normales_pointent_vers_lexterieur(plan):
    # snappyHexMesh se sert des normales pour savoir où est le solide : une
    # orientation incohérente produit un maillage aberrant.
    facets = pd._triangulate_prism(plan)
    upper = plan["profile"]["upper"]
    lower = plan["profile"]["lower"]
    mid = len(upper) // 3
    inside = (
        (upper[mid][0] + lower[mid][0]) / 2.0 * 0.01,
        (upper[mid][1] + lower[mid][1]) / 2.0 * 0.01,
        plan["span_cm"] * 0.01 / 2.0,
    )
    for normal, triangle in facets:
        centroid = [sum(p[i] for p in triangle) / 3.0 for i in range(3)]
        outward = [centroid[i] - inside[i] for i in range(3)]
        assert sum(normal[i] * outward[i] for i in range(3)) >= -1e-12


def test_le_volume_est_positif_et_plausible(plan):
    # Volume par le théorème de la divergence : somme de (centroid . n) * aire / 3.
    total = 0.0
    for normal, t in pd._triangulate_prism(plan):
        ux = [t[1][i] - t[0][i] for i in range(3)]
        vx = [t[2][i] - t[0][i] for i in range(3)]
        cross = (ux[1] * vx[2] - ux[2] * vx[1],
                 ux[2] * vx[0] - ux[0] * vx[2],
                 ux[0] * vx[1] - ux[1] * vx[0])
        area = 0.5 * math.sqrt(sum(c * c for c in cross))
        centroid = [sum(p[i] for p in t) / 3.0 for i in range(3)]
        total += sum(centroid[i] * normal[i] for i in range(3)) * area / 3.0

    # Section d'un NACA 4 chiffres : aire ~ 0.685 * t * c^2, extrudée sur span.
    attendu = 0.685 * plan["thickness"] * (plan["chord_cm"] * 0.01) ** 2 * (
        plan["span_cm"] * 0.01
    )
    assert total > 0
    assert total == pytest.approx(attendu, rel=0.10)


# ─────────────────────────────────────────────────────────────
# Écriture du STL
# ─────────────────────────────────────────────────────────────


def test_stl_ecrit_en_metres(plan, tmp_path):
    path = pd.write_stl(plan, tmp_path / "wing.stl")
    bbox = cb.stl_bounding_box(path)
    # 300 mm de corde = 0.3 m, 80 mm d'envergure = 0.08 m.
    assert bbox["x_max"] - bbox["x_min"] == pytest.approx(0.3, rel=1e-3)
    assert bbox["z_max"] - bbox["z_min"] == pytest.approx(0.08, rel=1e-3)


def test_aucune_remise_a_lechelle_necessaire(plan, tmp_path):
    # Écrire directement en mètres supprime toute ambiguïté d'unité.
    path = pd.write_stl(plan, tmp_path / "wing.stl")
    expected = cb.expected_bounding_box(load_yaml(REAL_CONFIG))
    assert cb.detect_scale_factor(cb.stl_bounding_box(path), expected) == 1.0
    _, warnings = cb.normalize_stl_scale(path, expected, 5.0)
    assert warnings == []


def test_le_stl_est_relu_correctement(plan, tmp_path):
    path = pd.write_stl(plan, tmp_path / "wing.stl")
    bbox = cb.stl_bounding_box(path)
    assert bbox["n_vertices"] == len(pd._triangulate_prism(plan)) * 3


# ─────────────────────────────────────────────────────────────
# drive() en mode interne
# ─────────────────────────────────────────────────────────────


def test_iteration_complete_sans_fusion(tmp_path):
    status = pd.drive(config_path=REAL_CONFIG, iterations_root=tmp_path,
                      geometry_backend="internal")
    assert status["success"] is True, status["error_message"]
    assert status["status"] == pd.STATUS_OK
    assert status["geometry_backend"] == "internal"
    out = tmp_path / "iter_0000"
    assert (out / "geometry.stl").is_file()
    assert (out / "fusion_status.json").is_file()
    assert (out / "fusion_driver.log").is_file()


def test_le_driver_seul_n_ecrit_pas_de_step(tmp_path):
    """Le driver écrit un STL, rien d'autre.

    Le STEP, lorsqu'un noyau CAO est installé, est ajouté par le BACKEND et non
    par le driver. La séparation est voulue : le driver doit rester importable
    dans l'interpréteur embarqué de Fusion, où aucune dépendance lourde n'est
    disponible.
    """
    status = pd.drive(config_path=REAL_CONFIG, iterations_root=tmp_path,
                      geometry_backend="internal")
    assert status["step_path"] is None
    assert not (tmp_path / "iter_0000" / "geometry.step").exists()
    assert any("historique CAO" in w for w in status["warnings"])


def test_les_parametres_sont_rapportes(tmp_path):
    status = pd.drive(config_path=REAL_CONFIG, iterations_root=tmp_path,
                      geometry_backend="internal")
    assert status["applied_parameters"]["chord"]["expression"] == "300 mm"
    assert status["applied_parameters"]["thickness"]["expression"] == "0.12"


def test_config_invalide_refusee_avant_ecriture(tmp_path):
    data = load_yaml(REAL_CONFIG)
    data["parameters"]["chord"]["value"] = 9999.0
    config = tmp_path / "design_params.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    status = pd.drive(config_path=config, iterations_root=tmp_path,
                      geometry_backend="internal")
    assert status["status"] == pd.STATUS_CONFIG_ERROR
    assert not (tmp_path / "iter_0000" / "geometry.stl").exists()


def test_la_geometrie_suit_les_parametres(tmp_path):
    data = load_yaml(REAL_CONFIG)
    data["parameters"]["chord"]["value"] = 400.0
    data["parameters"]["aoa"]["value"] = 8.0
    config = tmp_path / "design_params.yaml"
    config.write_text(yaml.safe_dump(data), encoding="utf-8")

    status = pd.drive(config_path=config, iterations_root=tmp_path,
                      geometry_backend="internal")

    assert status["geometry"]["chord_cm"] == pytest.approx(40.0)
    bbox = cb.stl_bounding_box(tmp_path / "iter_0000" / "geometry.stl")
    # 400 mm tournés de 8 deg.
    assert bbox["x_max"] == pytest.approx(0.4 * math.cos(math.radians(8)), rel=1e-2)


def test_deux_iterations_donnent_des_geometries_differentes(tmp_path):
    # Le contraire — deux STL identiques — est le mode de défaillance
    # silencieux que tout le système cherche à empêcher.
    empreintes = []
    for iteration, chord in ((0, 300.0), (1, 320.0)):
        data = load_yaml(REAL_CONFIG)
        data["iteration"] = iteration
        data["parameters"]["chord"]["value"] = chord
        config = tmp_path / "design_params.yaml"
        config.write_text(yaml.safe_dump(data), encoding="utf-8")
        pd.drive(config_path=config, iterations_root=tmp_path,
                 geometry_backend="internal")
        empreintes.append(
            (tmp_path / f"iter_{iteration:04d}" / "geometry.stl").read_text()
        )
    assert empreintes[0] != empreintes[1]


# ─────────────────────────────────────────────────────────────
# Enchaînement avec la chaîne CFD
# ─────────────────────────────────────────────────────────────


def test_le_case_openfoam_se_construit_sur_la_geometrie_interne(tmp_path):
    pd.drive(config_path=REAL_CONFIG, iterations_root=tmp_path,
             geometry_backend="internal")
    summary = cb.build_case(tmp_path / "iter_0000", REAL_CONFIG, REAL_CFD)
    case = Path(summary["case_dir"])
    assert (case / "constant" / "triSurface" / "wing.stl").is_file()
    assert summary["warnings"] == []      # ni échelle, ni incohérence
    assert summary["geometry"]["bounding_box_m"]["x_max"] == pytest.approx(0.3, rel=1e-3)


def test_le_controle_de_coherence_passe(tmp_path):
    pd.drive(config_path=REAL_CONFIG, iterations_root=tmp_path,
             geometry_backend="internal")
    bbox = cb.stl_bounding_box(tmp_path / "iter_0000" / "geometry.stl")
    expected = cb.expected_bounding_box(load_yaml(REAL_CONFIG))
    assert cb.check_geometry(bbox, expected, 0.3, 5.0) == []


def test_cli_mode_interne(tmp_path, capsys):
    code = pd.main(["--config", str(REAL_CONFIG), "--iterations-dir", str(tmp_path),
                    "--geometry-backend", "internal"])
    assert code == 0
    status = json.loads(capsys.readouterr().out)
    assert status["success"] is True
    assert status["geometry_backend"] == "internal"
