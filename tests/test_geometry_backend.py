"""Tests de l'interface GeometryBackend (Phase 1, Master Doc v1.5 §2).

Trois choses à établir :

  - les deux backends livrés respectent le contrat, y compris en échec ;
  - la sélection `auto | internal | fusion` se comporte comme annoncé ;
  - **un backend tiers s'ajoute sans toucher au pipeline** — c'est la raison
    d'être de l'interface, et le seul moyen de le vérifier est d'en écrire un.
"""

import json
from pathlib import Path

import pytest
import yaml

import geometry
from geometry import (
    GeometryBackend,
    GeometryResult,
    NoBackendAvailable,
    UnknownBackend,
    base,
)
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
def registre_isole(monkeypatch):
    """Registre restauré après le test, pour ne pas contaminer les suivants."""
    original = dict(base._REGISTRY)
    yield base._REGISTRY
    base._REGISTRY.clear()
    base._REGISTRY.update(original)


# ─────────────────────────────────────────────────────────────
# Le registre
# ─────────────────────────────────────────────────────────────


def test_les_deux_backends_sont_enregistres():
    assert set(geometry.backend_names()) >= {"internal", "fusion"}


def test_choix_de_configuration():
    choix = geometry.configuration_choices()
    assert choix[0] == "auto"
    assert "internal" in choix and "fusion" in choix


def test_le_backend_interne_est_toujours_disponible():
    assert geometry.InternalBackend.available() is True
    assert "internal" in geometry.available_backends()


def test_fusion_indisponible_hors_de_fusion():
    # L'API `adsk` n'existe que dans l'interpréteur de Fusion.
    assert geometry.FusionBackend.available() is False


def test_description_des_backends():
    par_nom = {b["name"]: b for b in geometry.describe_backends()}
    assert par_nom["internal"]["available"] is True
    assert par_nom["fusion"]["available"] is False
    assert par_nom["internal"]["description"]


# ─────────────────────────────────────────────────────────────
# Sélection auto | internal | fusion
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("demande", ["internal", "INTERNAL", " internal "])
def test_selection_explicite(demande):
    assert geometry.resolve(demande) == "internal"


def test_auto_retombe_sur_linterne_hors_fusion():
    # Fusion est préféré, mais indisponible ici : `auto` doit choisir seul,
    # sans faire échouer l'itération.
    assert geometry.resolve("auto") == "internal"
    assert geometry.resolve(None) == "internal"
    assert geometry.resolve("") == "internal"


def test_auto_prefere_fusion_quand_il_est_la(registre_isole, monkeypatch):
    monkeypatch.setattr(geometry.FusionBackend, "available", classmethod(lambda cls: True))
    assert geometry.resolve("auto") == "fusion"


def test_backend_inconnu_refuse():
    with pytest.raises(UnknownBackend) as exc:
        geometry.resolve("solidworks")
    assert "solidworks" in str(exc.value)
    assert "internal" in str(exc.value)      # les choix valides sont donnés


def test_aucun_backend_disponible(registre_isole, monkeypatch):
    for backend in (geometry.InternalBackend, geometry.FusionBackend):
        monkeypatch.setattr(backend, "available", classmethod(lambda cls: False))
    with pytest.raises(NoBackendAvailable):
        geometry.resolve("auto")


def test_get_backend_rend_une_instance():
    backend = geometry.get_backend("internal")
    assert isinstance(backend, geometry.InternalBackend)
    assert backend.name == "internal"


# ─────────────────────────────────────────────────────────────
# Extension par un tiers — la raison d'être de l'interface
# ─────────────────────────────────────────────────────────────


class BackendFactice(GeometryBackend):
    """Ce qu'un contributeur écrirait pour brancher son propre producteur."""

    name = "factice"
    description = "producteur d'essai"
    appels: list = []

    @classmethod
    def available(cls) -> bool:
        return True

    def generate(self, design_params, output_dir, **options):
        BackendFactice.appels.append((design_params, Path(output_dir)))
        stl = Path(output_dir) / "geometry.stl"
        stl.parent.mkdir(parents=True, exist_ok=True)
        stl.write_text("solid factice\nendsolid factice\n", encoding="utf-8")
        return GeometryResult(
            success=True, stl_path=stl, backend=self.name,
            message="géométrie factice", status="OK",
            profile_coordinates=[(0.0, 0.0), (0.3, 0.0)],
        )


def test_un_backend_tiers_sajoute_au_registre(registre_isole):
    geometry.register_backend(BackendFactice)
    assert "factice" in geometry.backend_names()
    assert "factice" in geometry.configuration_choices()
    assert isinstance(geometry.get_backend("factice"), BackendFactice)


def test_le_pipeline_utilise_un_backend_tiers(registre_isole, config, tmp_path,
                                              monkeypatch):
    """Le test qui compte : le pipeline doit fonctionner avec un producteur
    qu'il ne connaît pas, sans qu'une seule de ses lignes ait changé."""
    geometry.register_backend(BackendFactice)
    BackendFactice.appels.clear()

    def _cfd(iteration_dir, config_path, cfd_settings_path, timeout_s=None):
        Path(iteration_dir, "results.json").write_text(json.dumps({
            "iteration": 0, "success": True, "status": "OK",
            "Cd": 0.02, "Cl": 0.7, "Cl_Cd": 35.0, "mesh_ok": True,
        }), encoding="utf-8")
        return True, "ok"

    monkeypatch.setattr(mp, "run_cfd", _cfd)
    # Le contrôle de géométrie porte sur une vraie forme : ici la géométrie est
    # factice, on ne vérifie donc que l'enchaînement.
    monkeypatch.setattr(mp, "validate_geometry", lambda *a, **k: {"status": "OK"})

    record = mp.run_iteration(config, REAL_CFD, tmp_path / "iters",
                              geometry_backend="factice")

    assert record["success"] is True, record["error_message"]
    assert len(BackendFactice.appels) == 1
    assert record["Cl_Cd"] == pytest.approx(35.0)


def test_un_backend_sans_nom_est_refuse(registre_isole):
    class SansNom(GeometryBackend):
        def generate(self, design_params, output_dir, **options):
            return GeometryResult(success=True)

    with pytest.raises(ValueError):
        geometry.register_backend(SansNom)


def test_generate_est_obligatoire():
    with pytest.raises(TypeError):
        GeometryBackend()          # classe abstraite


# ─────────────────────────────────────────────────────────────
# Le backend interne, à travers l'interface
# ─────────────────────────────────────────────────────────────


def test_generation_interne(config, tmp_path):
    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    result = geometry.get_backend("internal").generate(config, out)

    assert result.success is True, result.message
    assert result.backend == "internal"
    assert result.status == "OK"
    assert result.stl_path is not None and result.stl_path.is_file()


def test_le_step_suit_la_presence_du_noyau_cao(config, tmp_path):
    """Un STEP est écrit si et seulement si le noyau CAO est installé.

    `cadquery` est une dépendance FACULTATIVE de près de deux gigaoctets. Le
    système doit tourner sans elle exactement comme avant — donc ce test ne
    peut pas exiger un STEP, ni son absence : il exige la cohérence entre les
    deux.
    """
    from geometry.step_io import available

    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    result = geometry.get_backend("internal").generate(config, out)

    assert result.success, result.message
    if available():
        assert result.step_path is not None and result.step_path.is_file()
        assert result.has_cad is True
        assert result.raw["step_faces"] >= 4   # deux surfaces et deux bouts
        assert result.raw["step_volume_mm3"] > 0
    else:
        assert result.step_path is None
        assert result.has_cad is False


def test_le_step_peut_etre_refuse_explicitement(config, tmp_path):
    """Une longue série n'a pas besoin d'un STEP par itération."""
    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    result = geometry.get_backend("internal").generate(config, out, step=False)

    assert result.success, result.message
    assert result.step_path is None
    assert not (out / "geometry.step").exists()


def test_le_stl_est_en_metres(config, tmp_path):
    from openfoam.case_builder import stl_bounding_box

    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    result = geometry.get_backend("internal").generate(config, out)
    bbox = stl_bounding_box(result.stl_path)
    assert bbox["x_max"] - bbox["x_min"] == pytest.approx(0.3, rel=1e-3)


def test_coordonnees_du_profil(config, tmp_path):
    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    result = geometry.get_backend("internal").generate(config, out)

    points = result.profile_coordinates
    assert points is not None
    # Contour fermé : extrados puis intrados en sens inverse.
    assert points[0] == pytest.approx(points[-1], abs=1e-9)
    # En mètres, corde de 300 mm.
    xs = [x for x, _ in points]
    assert max(xs) - min(xs) == pytest.approx(0.3, rel=1e-3)


def test_le_backend_accepte_un_dictionnaire(tmp_path):
    """L'ingestion de la Phase 2 travaillera sur des dictionnaires, pas sur des
    fichiers : l'interface doit accepter les deux."""
    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    design = load_yaml(REAL_CONFIG)

    result = geometry.get_backend("internal").generate(design, out)

    assert result.success is True, result.message
    # La configuration exacte reste à côté de la géométrie.
    assert (out / "design_params.yaml").is_file()
    assert load_yaml(out / "design_params.yaml")["parameters"]["chord"]["value"] \
        == pytest.approx(300.0)


def test_echec_de_configuration_rendu_comme_resultat(tmp_path):
    """Un échec attendu est un résultat, pas une exception : c'est ce qui
    permet à la boucle d'archiver l'itération et de continuer."""
    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    mauvaise = tmp_path / "mauvaise.yaml"
    design = load_yaml(REAL_CONFIG)
    design["parameters"]["chord"]["value"] = 9999.0
    mauvaise.write_text(yaml.safe_dump(design), encoding="utf-8")

    result = geometry.get_backend("internal").generate(mauvaise, out)

    assert result.success is False
    assert result.status == "CONFIG_ERROR"
    assert "hors bornes" in result.message
    assert result.stl_path is None


def test_le_compte_rendu_brut_est_conserve(config, tmp_path):
    """`raw` porte le compte rendu complet du driver : le pipeline l'archive
    tel quel, sans que l'interface ait à modéliser chacun de ses champs."""
    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    result = geometry.get_backend("internal").generate(config, out)

    assert result.raw["geometry_backend"] == "internal"
    assert result.raw["applied_parameters"]["chord"]["expression"] == "300 mm"
    assert "timestamp" in result.raw


def test_mode_simulation(config, tmp_path):
    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    result = geometry.get_backend("internal").generate(config, out, dry_run=True)

    assert result.success is False
    assert result.status == "DRY_RUN"
    # Rien n'a été écrit : un chemin annoncé mais inexistant ne doit pas être
    # présenté comme un fichier.
    assert result.stl_path is None


# ─────────────────────────────────────────────────────────────
# Le backend Fusion, hors de Fusion
# ─────────────────────────────────────────────────────────────


def test_fusion_hors_de_fusion_echoue_proprement(config, tmp_path):
    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    result = geometry.get_backend("fusion").generate(config, out)

    assert result.success is False
    assert result.status == "DRY_RUN"
    assert result.backend == "fusion"
    assert "adsk" in result.message or "simulation" in result.message


# ─────────────────────────────────────────────────────────────
# Intégration au pipeline
# ─────────────────────────────────────────────────────────────


def test_le_pipeline_passe_par_linterface(config, tmp_path, monkeypatch):
    appels = []
    original = geometry.get_backend

    def _espion(name=None, **kwargs):
        appels.append(name)
        return original(name, **kwargs)

    monkeypatch.setattr(mp, "get_backend", _espion)
    mp.run_iteration(config, REAL_CFD, tmp_path / "iters", skip_cfd=True,
                     geometry_backend="internal")
    assert appels == ["internal"]


def test_backend_inconnu_devient_un_echec_diteration(config, tmp_path):
    """Un nom erroné ne doit pas faire exploser le pipeline : la boucle doit
    pouvoir l'archiver comme n'importe quel autre échec."""
    record = mp.run_iteration(config, REAL_CFD, tmp_path / "iters",
                              geometry_backend="inexistant")
    assert record["success"] is False
    assert record["status"] == mp.STATUS_GEOMETRY_FAILED
    assert "inexistant" in record["error_message"]


def test_produce_geometry_rend_un_resultat(config, tmp_path):
    out = tmp_path / "iters" / "iter_0000"
    out.mkdir(parents=True)
    result = mp.produce_geometry(config, out, tmp_path / "iters", "internal")
    assert isinstance(result, GeometryResult)
    assert result.success is True, result.message


def test_les_choix_de_la_cli_viennent_du_registre(registre_isole, capsys):
    """Un backend ajouté doit apparaître dans l'aide sans modifier la CLI."""
    geometry.register_backend(BackendFactice)
    with pytest.raises(SystemExit):
        mp.main(["--help"])
    assert "factice" in capsys.readouterr().out
