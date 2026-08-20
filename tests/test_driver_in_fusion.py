"""Exécution du driver Fusion complet contre une émulation de l'API `adsk`.

Ces tests couvrent ce qu'aucun autre ne peut atteindre sans Fusion : `run()`,
`_get_design`, `_apply_parameters`, `_rebuild_geometry`, `_recompute`,
`_export_step` et `_export_stl`, enchaînés par `drive()` sur un vrai document.

Le document de départ reproduit celui du premier run réel — esquisse
`NACA_2412_Profile` plus un corps `Body1` — pour que le correctif de purge soit
vérifié sur le cas qui l'a motivé.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest
import yaml

import fake_adsk

ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "configs" / "design_params.yaml"


@pytest.fixture
def fusion(monkeypatch):
    """Installe le faux `adsk` et recharge le driver pour qu'il le voie."""
    design = fake_adsk.seed_design()
    app = fake_adsk.install(design)
    app.seed_parameters = fake_adsk.seed_parameters()

    monkeypatch.delenv("FUSION_GEOMETRY_MODE", raising=False)
    monkeypatch.delenv("FUSION_PURGE_MODE", raising=False)
    monkeypatch.delenv("FUSION_FORCE_SEED_IMPORT", raising=False)

    sys.modules.pop("fusion.parametric_driver", None)
    driver = importlib.import_module("fusion.parametric_driver")
    importlib.reload(driver)
    assert driver.FUSION_AVAILABLE, "le faux adsk doit être vu comme disponible"

    yield driver, app, design

    fake_adsk.uninstall()
    sys.modules.pop("fusion.parametric_driver", None)
    importlib.import_module("fusion.parametric_driver")


@pytest.fixture
def config(tmp_path) -> Path:
    """Copie de la configuration livrée, pour ne pas toucher au dépôt."""
    target = tmp_path / "design_params.yaml"
    target.write_text(REAL_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return target


def run(driver, config: Path, iterations: Path, **kwargs) -> dict:
    return driver.drive(config_path=config, iterations_root=iterations, **kwargs)


# ─────────────────────────────────────────────────────────────
# Itération nominale
# ─────────────────────────────────────────────────────────────


def test_iteration_complete(fusion, config, tmp_path):
    driver, _, _ = fusion
    status = run(driver, config, tmp_path)
    assert status["success"] is True, status["error_message"]
    assert status["status"] == driver.STATUS_OK
    assert status["geometry_mode"] == driver.GEOMETRY_MODE_REBUILD


def test_les_parametres_sont_appliques(fusion, config, tmp_path):
    driver, _, design = fusion
    run(driver, config, tmp_path)
    params = design.userParameters
    assert params.itemByName("chord").expression == "300 mm"
    assert params.itemByName("chord").value == pytest.approx(30.0)   # cm internes
    assert params.itemByName("thickness").expression == "0.12"
    assert params.itemByName("aoa").expression == "0 deg"


def test_les_fichiers_sont_ecrits(fusion, config, tmp_path):
    driver, _, _ = fusion
    run(driver, config, tmp_path)
    out = tmp_path / "iter_0000"
    for name in ("geometry.step", "geometry.stl", "fusion_status.json",
                 "fusion_driver.log"):
        assert (out / name).is_file(), name
        assert (out / name).stat().st_size > 0, name


def test_le_statut_sur_disque_reflete_le_retour(fusion, config, tmp_path):
    driver, _, _ = fusion
    status = run(driver, config, tmp_path)
    on_disk = json.loads(
        (tmp_path / "iter_0000" / "fusion_status.json").read_text(encoding="utf-8")
    )
    assert on_disk["status"] == status["status"]
    assert on_disk["stl_path"] is not None


# ─────────────────────────────────────────────────────────────
# Purge — non-régression du premier run réel
# ─────────────────────────────────────────────────────────────


def test_une_seule_aile_apres_reconstruction(fusion, config, tmp_path):
    driver, _, design = fusion
    root = design.rootComponent
    assert root.bRepBodies.count == 1 and root.bRepBodies.names == ["Body1"]

    status = run(driver, config, tmp_path)

    assert status["success"] is True, status["error_message"]
    # Le corps du seed a bien disparu : c'est LE defaut du premier run.
    assert root.bRepBodies.count == 1
    assert root.bRepBodies.names == [driver.REBUILD_BODY_NAME]
    assert status["geometry"]["bodies"] == 1
    assert status["geometry"]["purged_entities"] == 2   # l'esquisse ET le corps


def test_le_journal_confirme_la_purge(fusion, config, tmp_path):
    driver, _, _ = fusion
    run(driver, config, tmp_path)
    log = (tmp_path / "iter_0000" / "fusion_driver.log").read_text(encoding="utf-8")
    assert "Purge de l'esquisse 'NACA_2412_Profile'" in log
    assert "Purge du corps 'Body1'" in log
    assert "1 corps créé(s), 2 entité(s) purgée(s)" in log


def test_le_mode_tagged_laisse_le_corps_et_le_garde_fou_refuse(
    fusion, config, tmp_path, monkeypatch
):
    # En purge ciblée, « Body1 » n'est pas reconnu : le driver doit REFUSER
    # d'exporter plutôt que de produire un STEP à deux ailes.
    driver, _, design = fusion
    monkeypatch.setenv("FUSION_PURGE_MODE", "tagged")
    status = run(driver, config, tmp_path)
    assert status["success"] is False
    assert status["status"] == driver.STATUS_REBUILD_FAILED
    assert "Body1" in status["error_message"]
    assert not (tmp_path / "iter_0000" / "geometry.step").exists()


def test_iterations_successives_nempilent_pas_les_corps(fusion, config, tmp_path):
    driver, _, design = fusion
    for iteration in range(3):
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        data["iteration"] = iteration
        data["parameters"]["chord"]["value"] = 300.0 + 10 * iteration
        config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

        status = run(driver, config, tmp_path)
        assert status["success"] is True, status["error_message"]
        assert design.rootComponent.bRepBodies.count == 1
        assert design.rootComponent.sketches.count == 1


# ─────────────────────────────────────────────────────────────
# Géométrie reconstruite
# ─────────────────────────────────────────────────────────────


def test_la_geometrie_suit_les_parametres(fusion, config, tmp_path):
    driver, _, design = fusion
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["parameters"]["chord"]["value"] = 400.0
    data["parameters"]["aoa"]["value"] = 6.0
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    status = run(driver, config, tmp_path)

    assert status["success"] is True, status["error_message"]
    assert status["geometry"]["chord_cm"] == pytest.approx(40.0)
    assert status["geometry"]["aoa_deg"] == pytest.approx(6.0)
    body = design.rootComponent.bRepBodies.item(0)
    xs = [x for x, _ in body.sections["upper"]]
    # Corde de 40 cm tournee de 6 deg : l'emprise en x vaut 40 cos(6 deg).
    assert max(xs) == pytest.approx(40.0 * 0.99452, rel=1e-2)


def test_le_contour_est_ferme(fusion, config, tmp_path):
    driver, _, design = fusion
    run(driver, config, tmp_path)
    sketch = design.rootComponent.sketches.item(0)
    assert sketch.is_closed(), "extrados et intrados doivent se rejoindre"
    assert sketch.profiles.count == 1


def test_lextrusion_utilise_lenvergure(fusion, config, tmp_path):
    driver, _, design = fusion
    run(driver, config, tmp_path)
    assert design.rootComponent.bRepBodies.item(0).z_range == (0.0, 8.0)  # 80 mm


# ─────────────────────────────────────────────────────────────
# Le STL produit est réellement exploitable en aval
# ─────────────────────────────────────────────────────────────


def test_le_stl_est_lisible_et_a_la_bonne_taille(fusion, config, tmp_path):
    from openfoam.case_builder import expected_bounding_box, stl_bounding_box
    from pipeline.utils import load_yaml

    driver, _, _ = fusion
    run(driver, config, tmp_path)

    bbox = stl_bounding_box(tmp_path / "iter_0000" / "geometry.stl")
    # Fusion ecrit en millimetres : la corde de 300 mm doit s'y retrouver.
    assert bbox["x_max"] - bbox["x_min"] == pytest.approx(300.0, rel=1e-2)
    assert bbox["z_max"] - bbox["z_min"] == pytest.approx(80.0, rel=1e-2)

    expected = expected_bounding_box(load_yaml(config))
    from openfoam.case_builder import detect_scale_factor

    # Et la chaine CFD doit reconnaitre l'unite toute seule.
    assert detect_scale_factor(bbox, expected) == pytest.approx(1e-3)


def test_le_case_openfoam_se_construit_sur_le_stl_du_driver(fusion, config, tmp_path):
    from openfoam.case_builder import build_case

    driver, _, _ = fusion
    run(driver, config, tmp_path)

    summary = build_case(tmp_path / "iter_0000", config,
                         ROOT / "configs" / "cfd_settings.yaml")
    case = Path(summary["case_dir"])
    assert (case / "constant" / "triSurface" / "wing.stl").is_file()
    assert (case / "system" / "snappyHexMeshDict").is_file()
    # Le STL a ete remis a l'echelle en metres sans intervention.
    assert any("échelle" in w for w in summary["warnings"])
    assert summary["geometry"]["bounding_box_m"]["x_max"] == pytest.approx(0.3, rel=1e-2)


# ─────────────────────────────────────────────────────────────
# Chemins d'échec
# ─────────────────────────────────────────────────────────────


def test_parametre_absent_du_modele(fusion, config, tmp_path):
    driver, _, design = fusion
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["parameters"]["twist"] = {"value": 1.0, "min": 0.0, "max": 5.0,
                                   "max_delta_pct": 10.0, "unit": "deg"}
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    status = run(driver, config, tmp_path)
    assert status["status"] == driver.STATUS_PARAM_NOT_FOUND
    assert "twist" in status["error_message"]
    # Rien ne doit avoir bouge dans le modele.
    assert design.rootComponent.bRepBodies.names == ["Body1"]


def test_echec_dexport_step(fusion, config, tmp_path):
    driver, _, design = fusion
    design.exportManager.fail_step = True
    status = run(driver, config, tmp_path)
    assert status["status"] == driver.STATUS_EXPORT_FAILED
    assert not (tmp_path / "iter_0000" / "geometry.step").exists()


def test_echec_dexport_stl_non_bloquant(fusion, config, tmp_path):
    # Le STEP est le livrable contractuel : un STL manquant ne doit pas
    # invalider une iteration dont la geometrie est bonne.
    driver, _, design = fusion
    design.exportManager.fail_stl = True
    status = run(driver, config, tmp_path)
    assert status["success"] is True
    assert status["stl_path"] is None
    assert any("STL" in w for w in status["warnings"])


def test_recompute_en_echec_en_mode_parameters(fusion, config, tmp_path):
    driver, _, design = fusion
    design.compute_raises = True
    status = run(driver, config, tmp_path, geometry_mode="parameters")
    assert status["status"] == driver.STATUS_RECOMPUTE_FAILED
    assert "conservative" in status["error_message"]


def test_mode_parameters_ne_touche_pas_la_geometrie(fusion, config, tmp_path):
    driver, _, design = fusion
    status = run(driver, config, tmp_path, geometry_mode="parameters")
    assert status["success"] is True
    assert design.compute_calls == 1
    # Le corps du seed est intact : en mode parameters, on ne reconstruit rien.
    assert design.rootComponent.bRepBodies.names == ["Body1"]


def test_config_hors_bornes_ne_touche_pas_au_modele(fusion, config, tmp_path):
    driver, _, design = fusion
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["parameters"]["chord"]["value"] = 9999.0
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    status = run(driver, config, tmp_path)
    assert status["status"] == driver.STATUS_CONFIG_ERROR
    assert design.userParameters.itemByName("chord").expression == "300 mm"


# ─────────────────────────────────────────────────────────────
# Choix du document
# ─────────────────────────────────────────────────────────────


def test_le_document_actif_est_utilise(fusion, config, tmp_path):
    driver, app, _ = fusion
    run(driver, config, tmp_path)
    assert app.importManager.imported == []


def test_import_du_seed_si_force(fusion, config, tmp_path, monkeypatch):
    driver, app, _ = fusion
    # Le seed est un binaire propre au projet, absent du dépôt : le test
    # fournit le sien. S'appuyer sur `fusion/seed_design.f3d` ferait échouer la
    # suite sur tout clone frais — le driver ne vérifie que l'existence du
    # fichier, l'import étant assuré par Fusion.
    seed = tmp_path / "seed_de_test.f3d"
    seed.write_bytes(b"PK\x03\x04 archive Fusion factice")
    monkeypatch.setenv("FUSION_FORCE_SEED_IMPORT", "1")
    monkeypatch.setenv("FUSION_SEED_PATH", str(seed))
    status = run(driver, config, tmp_path)
    assert status["success"] is True, status["error_message"]
    assert app.importManager.imported == [str(seed)]


def test_seed_absent_signale(fusion, config, tmp_path, monkeypatch):
    driver, app, _ = fusion
    monkeypatch.setenv("FUSION_FORCE_SEED_IMPORT", "1")
    monkeypatch.setenv("FUSION_SEED_PATH", str(tmp_path / "absent.f3d"))
    status = run(driver, config, tmp_path)
    assert status["status"] == driver.STATUS_SEED_MISSING


def test_point_dentree_run(fusion, config, tmp_path, monkeypatch):
    # `run(context)` est ce que Fusion appelle reellement.
    driver, _, _ = fusion
    monkeypatch.setenv("DESIGN_PARAMS_PATH", str(config))
    monkeypatch.setenv("ITERATIONS_DIR", str(tmp_path))
    status = driver.run(None)
    assert status["success"] is True, status["error_message"]
    driver.stop(None)
