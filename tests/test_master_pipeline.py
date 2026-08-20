"""Tests du master pipeline et du validateur de géométrie (Phase 3).

La CFD est court-circuitée (`--skip-cfd`, ou une doublure de `run_cfd`) : ce
qui est vérifié ici, c'est l'ENCHAÎNEMENT — validation, géométrie, contrôle,
archivage, compte rendu — pas la physique, couverte ailleurs.
"""

import json
from pathlib import Path

import pytest
import yaml

from pipeline import geometry_validator as gv
from pipeline import master_pipeline as mp
from pipeline.utils import load_yaml

ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "configs" / "design_params.yaml"
REAL_CFD = ROOT / "configs" / "cfd_settings.yaml"


@pytest.fixture
def config(tmp_path) -> Path:
    target = tmp_path / "design_params.yaml"
    target.write_text(REAL_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return target


@pytest.fixture
def iterations(tmp_path) -> Path:
    d = tmp_path / "iterations"
    d.mkdir()
    return d


def set_params(config: Path, iteration=None, **values) -> Path:
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    if iteration is not None:
        data["iteration"] = iteration
    for name, value in values.items():
        data["parameters"][name]["value"] = value
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return config


def fake_cfd(monkeypatch, cd=0.017, cl=0.2275, success=True, mesh_ok=True,
             message="CFD terminée"):
    """Remplace run_cfd par une doublure qui écrit un results.json plausible."""
    def _run(iteration_dir, config_path, cfd_settings_path, timeout_s=None):
        results = {
            "iteration": load_yaml(config_path)["iteration"],
            "success": success,
            "status": "OK" if success else "SOLVER_FAILED",
            "Cd": cd if success else None,
            "Cl": cl if success else None,
            "Cl_Cd": (cl / cd) if (success and cd) else None,
            "mesh_ok": mesh_ok,
            "converged": success,
            "error_message": None if success else "le solveur a diverge",
        }
        (Path(iteration_dir) / "results.json").write_text(
            json.dumps(results, indent=2), encoding="utf-8"
        )
        return success, message

    monkeypatch.setattr(mp, "run_cfd", _run)


# ─────────────────────────────────────────────────────────────
# Validateur de géométrie
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def geometry(config, iterations):
    """Produit une géométrie réelle via le producteur interne."""
    from fusion import parametric_driver as pd

    pd.drive(config_path=config, iterations_root=iterations,
             geometry_backend="internal")
    return iterations / "iter_0000"


def test_geometrie_valide(geometry, config):
    report = gv.validate_geometry(geometry, config)
    assert report["status"] == gv.STATUS_OK
    assert report["format"] == "stl"
    assert report["bounding_box_m"]["x_max"] == pytest.approx(0.3, rel=1e-3)
    assert report["warnings"] == []


def test_empreinte_stable(geometry, config):
    a = gv.validate_geometry(geometry, config)["fingerprint"]
    b = gv.validate_geometry(geometry, config)["fingerprint"]
    assert a == b and len(a) == 64


def test_geometrie_absente(tmp_path, config):
    vide = tmp_path / "iter_0009"
    vide.mkdir()
    with pytest.raises(gv.GeometryError) as exc:
        gv.validate_geometry(vide, config)
    assert exc.value.status == gv.STATUS_GEOMETRY_MISSING


def test_geometrie_tronquee(geometry, config):
    (geometry / "geometry.stl").write_text("solid wing\nendsolid wing\n",
                                           encoding="utf-8")
    with pytest.raises(gv.GeometryError) as exc:
        gv.validate_geometry(geometry, config)
    assert exc.value.status == gv.STATUS_GEOMETRY_UNREADABLE


def test_geometrie_qui_ne_correspond_pas(geometry, config):
    # La configuration réclame 400 mm, la géométrie sur disque en fait 300.
    set_params(config, chord=400.0)
    with pytest.raises(gv.GeometryError) as exc:
        gv.validate_geometry(geometry, config)
    assert exc.value.status == gv.STATUS_GEOMETRY_MISMATCH


def test_geometrie_inchangee_alors_que_les_parametres_ont_bouge(geometry, config):
    # LE défaut coûteux : tout fonctionne, mais l'agent optimise une constante.
    empreinte = gv.validate_geometry(geometry, config)["fingerprint"]
    anciens = load_yaml(config)["parameters"]
    anciens["chord"]["value"] = 280.0
    with pytest.raises(gv.GeometryError) as exc:
        gv.validate_geometry(geometry, config, previous_fingerprint=empreinte,
                             previous_parameters=anciens)
    assert exc.value.status == gv.STATUS_GEOMETRY_UNCHANGED


def test_geometrie_inchangee_avec_parametres_inchanges_est_toleree(geometry, config):
    empreinte = gv.validate_geometry(geometry, config)["fingerprint"]
    report = gv.validate_geometry(
        geometry, config, previous_fingerprint=empreinte,
        previous_parameters=load_yaml(config)["parameters"],
    )
    assert report["status"] == gv.STATUS_OK
    assert any("identique" in w for w in report["warnings"])


def test_epaisseur_sous_le_minimum(geometry, config):
    # thickness 0.08 x corde 220 mm = 17.6 mm : au dessus de 1.5 mm.
    # On force une contrainte absurde pour vérifier que le contrôle mord.
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    data["constraints"]["min_wall_thickness_mm"] = 100.0
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(gv.GeometryError) as exc:
        gv.validate_geometry(geometry, config)
    assert exc.value.status == gv.STATUS_CONSTRAINT_VIOLATED


def test_step_seul_avertit(tmp_path, config):
    d = tmp_path / "iter_0000"
    d.mkdir()
    (d / "geometry.step").write_text("ISO-10303-21;\n" + "x" * 500, encoding="utf-8")
    report = gv.validate_geometry(d, config)
    assert report["format"] == "step"
    assert any("STEP" in w for w in report["warnings"])


def test_cli_validateur(geometry, config, capsys):
    assert gv.main(["--iteration-dir", str(geometry),
                    "--design-params", str(config)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == gv.STATUS_OK


def test_cli_validateur_en_echec(tmp_path, config, capsys):
    vide = tmp_path / "iter_0009"
    vide.mkdir()
    assert gv.main(["--iteration-dir", str(vide),
                    "--design-params", str(config)]) == 1
    assert json.loads(capsys.readouterr().err)["status"] == gv.STATUS_GEOMETRY_MISSING


# ─────────────────────────────────────────────────────────────
# Itération complète
# ─────────────────────────────────────────────────────────────


def test_iteration_reussie(config, iterations, monkeypatch):
    fake_cfd(monkeypatch)
    record = mp.run_iteration(config, REAL_CFD, iterations,
                              geometry_backend="internal")
    assert record["success"] is True, record["error_message"]
    assert record["status"] == mp.STATUS_OK
    assert record["stage"] == "done"
    assert record["Cd"] == pytest.approx(0.017)
    assert record["Cl_Cd"] == pytest.approx(0.2275 / 0.017)
    assert record["mesh_ok"] is True


def test_archivage_complet(config, iterations, monkeypatch):
    fake_cfd(monkeypatch)
    mp.run_iteration(config, REAL_CFD, iterations, geometry_backend="internal")
    out = iterations / "iter_0000"
    for name in ("design_params.yaml", "geometry.stl", "results.json",
                 "iteration.json", "fusion_status.json"):
        assert (out / name).is_file(), name


def test_la_config_archivee_est_celle_qui_a_servi(config, iterations, monkeypatch):
    fake_cfd(monkeypatch)
    set_params(config, chord=340.0)
    mp.run_iteration(config, REAL_CFD, iterations, geometry_backend="internal")
    # L'agent réécrit ensuite le fichier de travail : la copie doit rester.
    set_params(config, iteration=1, chord=350.0)
    archived = load_yaml(iterations / "iter_0000" / "design_params.yaml")
    assert archived["parameters"]["chord"]["value"] == 340.0


def test_objectif_calcule(config, iterations, monkeypatch):
    fake_cfd(monkeypatch, cd=0.02, cl=1.0)
    record = mp.run_iteration(config, REAL_CFD, iterations,
                              geometry_backend="internal")
    assert record["objective"] == pytest.approx(50.0)   # maximize_Cl_Cd


@pytest.mark.parametrize(
    "objectif,cd,cl,attendu",
    [("maximize_Cl_Cd", 0.02, 1.0, 50.0),
     ("minimize_Cd", 0.02, 1.0, -0.02),
     ("maximize_downforce", 0.02, -1.5, 1.5)],
)
def test_tous_les_objectifs_se_maximisent(objectif, cd, cl, attendu):
    design = {"objectives": {"primary": objectif}}
    results = {"Cd": cd, "Cl": cl, "Cl_Cd": cl / cd}
    assert mp.objective_value(design, results) == pytest.approx(attendu)


def test_objectif_inconnu_donne_none():
    assert mp.objective_value({"objectives": {"primary": "?"}},
                              {"Cd": 1, "Cl": 1, "Cl_Cd": 1}) is None


# ─────────────────────────────────────────────────────────────
# Échecs — chacun doit être archivé, pas perdu
# ─────────────────────────────────────────────────────────────


def test_config_invalide(config, iterations):
    set_params(config, chord=9999.0)
    record = mp.run_iteration(config, REAL_CFD, iterations,
                              geometry_backend="internal")
    assert record["success"] is False
    assert record["status"] == mp.STATUS_CONFIG_ERROR
    assert record["stage"] == "config"
    assert record["Cd"] is None


def test_echec_de_geometrie(config, iterations, monkeypatch):
    from fusion import parametric_driver as pd

    def _fail(**kwargs):
        return {"success": False, "status": "REBUILD_FAILED",
                "error_message": "extrusion impossible"}

    monkeypatch.setattr(mp.driver, "drive", _fail)
    record = mp.run_iteration(config, REAL_CFD, iterations)
    assert record["status"] == mp.STATUS_GEOMETRY_FAILED
    assert record["stage"] == "geometry"
    assert "extrusion" in record["error_message"]


def test_echec_cfd_archive(config, iterations, monkeypatch):
    fake_cfd(monkeypatch, success=False)
    record = mp.run_iteration(config, REAL_CFD, iterations,
                              geometry_backend="internal")
    assert record["success"] is False
    assert record["status"] == mp.STATUS_CFD_FAILED
    assert record["Cd"] is None
    # Une itération ratée reste archivée : elle dit à l'agent quoi ne pas refaire.
    assert (iterations / "iter_0000" / "iteration.json").is_file()
    assert mp.read_iteration(iterations / "iter_0000")["status"] == mp.STATUS_CFD_FAILED


def test_cfd_sans_results_json(config, iterations, monkeypatch):
    monkeypatch.setattr(
        mp, "run_cfd", lambda *a, **k: (False, "OpenFOAM introuvable")
    )
    record = mp.run_iteration(config, REAL_CFD, iterations,
                              geometry_backend="internal")
    assert record["status"] == mp.STATUS_CFD_FAILED
    assert "aucun results.json" in record["error_message"]


def test_le_pipeline_ne_leve_jamais(config, iterations, monkeypatch):
    monkeypatch.setattr(
        mp.driver, "drive", lambda **k: (_ for _ in ()).throw(RuntimeError("boum"))
    )
    record = mp.run_iteration(config, REAL_CFD, iterations)
    assert record["status"] == mp.STATUS_PIPELINE_ERROR
    assert "boum" in record["error_message"]


def test_skip_cfd(config, iterations):
    record = mp.run_iteration(config, REAL_CFD, iterations, skip_cfd=True,
                              geometry_backend="internal")
    assert record["status"] == mp.STATUS_SKIPPED_CFD
    assert record["geometry_report"]["status"] == "OK"
    assert not (iterations / "iter_0000" / "results.json").exists()


# ─────────────────────────────────────────────────────────────
# Historique
# ─────────────────────────────────────────────────────────────


def test_historique_dans_lordre(config, iterations, monkeypatch):
    fake_cfd(monkeypatch)
    for i in range(3):
        set_params(config, iteration=i, chord=300.0 + 5 * i)
        mp.run_iteration(config, REAL_CFD, iterations, geometry_backend="internal")
    records = mp.history(iterations)
    assert [r["iteration"] for r in records] == [0, 1, 2]


def test_derniere_iteration_reussie(config, iterations, monkeypatch):
    fake_cfd(monkeypatch, cd=0.02)
    set_params(config, iteration=0)
    mp.run_iteration(config, REAL_CFD, iterations, geometry_backend="internal")

    fake_cfd(monkeypatch, success=False)
    set_params(config, iteration=1, chord=310.0)
    mp.run_iteration(config, REAL_CFD, iterations, geometry_backend="internal")

    last = mp.last_successful(iterations)
    assert last["iteration"] == 0
    assert last["Cd"] == pytest.approx(0.02)


def test_pas_diteration_reussie(iterations):
    assert mp.last_successful(iterations) is None
    assert mp.history(iterations) == []


def test_la_geometrie_inchangee_est_detectee_par_le_pipeline(
    config, iterations, monkeypatch
):
    fake_cfd(monkeypatch)
    set_params(config, iteration=0)
    mp.run_iteration(config, REAL_CFD, iterations, geometry_backend="internal")

    # Itération 1 : paramètres changés, mais on force la géométrie précédente.
    set_params(config, iteration=1, chord=320.0)

    def _stale(**kwargs):
        import shutil

        src = iterations / "iter_0000" / "geometry.stl"
        dst = iterations / "iter_0001"
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst / "geometry.stl")
        return {"success": True, "status": "OK", "geometry_backend": "internal"}

    monkeypatch.setattr(mp.driver, "drive", _stale)
    record = mp.run_iteration(config, REAL_CFD, iterations)
    assert record["success"] is False
    assert record["stage"] == "geometry_check"
    assert record["error_details"] in (
        gv.STATUS_GEOMETRY_UNCHANGED, gv.STATUS_GEOMETRY_MISMATCH
    )


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def test_cli_succes(config, iterations, monkeypatch, capsys):
    fake_cfd(monkeypatch)
    code = mp.main(["--config", str(config), "--cfd-settings", str(REAL_CFD),
                    "--iterations-dir", str(iterations),
                    "--geometry-backend", "internal", "--quiet"])
    assert code == 0
    assert "Cl/Cd" in capsys.readouterr().err


def test_cli_echec(config, iterations, capsys):
    set_params(config, chord=9999.0)
    code = mp.main(["--config", str(config), "--cfd-settings", str(REAL_CFD),
                    "--iterations-dir", str(iterations), "--quiet"])
    assert code == 1
    assert "ÉCHEC" in capsys.readouterr().err
