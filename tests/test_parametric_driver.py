"""Tests du driver Fusion (Phase 1), hors Fusion 360.

Le module `adsk` n'existe que dans l'interpréteur embarqué de Fusion. Deux
stratégies sont donc employées ici :

  - test direct des parties pures (lecture YAML de repli, unités, expressions,
    chemins, statut, mode simulation) ;
  - test des opérations Fusion via des doublures (`FakeDesign` & co.) pour les
    fonctions qui ne dépendent que de l'interface d'objets Fusion et non du
    module `adsk` lui-même : application des paramètres, rollback, export STEP.

Ce qui reste non couvert et exige le seed `.f3d` réel : `_get_design`,
`_recompute` et `_check_timeline_health`.
"""

import json
from pathlib import Path

import pytest
import yaml

from fusion import parametric_driver as pd

ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "configs" / "design_params.yaml"


@pytest.fixture
def cfg() -> dict:
    return {
        "iteration": 3,
        "design_id": "wing_v01",
        "parameters": {
            "chord_mm": {
                "value": 300.0,
                "min": 220.0,
                "max": 420.0,
                "max_delta_pct": 7.0,
                "unit": "mm",
            },
            "aoa_deg": {
                "value": 4.0,
                "min": -1.0,
                "max": 10.0,
                "max_delta_pct": 12.0,
                "unit": "deg",
            },
        },
        "constraints": {"topology_preserving": True, "min_wall_thickness_mm": 1.5},
        "objectives": {"primary": "maximize_Cl_Cd"},
    }


@pytest.fixture
def cfg_file(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "design_params.yaml"
    p.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return p


# ─────────────────────────────────────────────────────────────
# Doublures Fusion
# ─────────────────────────────────────────────────────────────

_UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0}
_UNIT_TO_DEG = {"deg": 1.0, "rad": 57.29577951308232}


class FakeParam:
    """User Parameter Fusion : expression assignable, unité fixe."""

    def __init__(self, name: str, expression: str, unit: str = "mm",
                 raise_on_set: bool = False) -> None:
        self.name = name
        self._expression = expression
        self.unit = unit
        self.raise_on_set = raise_on_set

    @property
    def expression(self) -> str:
        return self._expression

    @expression.setter
    def expression(self, value: str) -> None:
        if self.raise_on_set:
            raise RuntimeError(f"expression refusée : {value}")
        self._expression = value


class FakeUserParameters:
    def __init__(self, params: list[FakeParam]) -> None:
        self._params = params

    def itemByName(self, name: str):
        return next((p for p in self._params if p.name == name), None)

    def item(self, index: int) -> FakeParam:
        return self._params[index]

    @property
    def count(self) -> int:
        return len(self._params)


class FakeUnitsManager:
    """Évalue « <nombre> <unité> » et convertit vers l'unité demandée."""

    def evaluateExpression(self, expression: str, unit: str | None = None) -> float:
        parts = expression.split()
        number = float(parts[0])
        source = parts[1] if len(parts) > 1 else None
        if unit is None or source is None or source == unit:
            return number
        if source in _UNIT_TO_MM and unit in _UNIT_TO_MM:
            return number * _UNIT_TO_MM[source] / _UNIT_TO_MM[unit]
        if source in _UNIT_TO_DEG and unit in _UNIT_TO_DEG:
            return number * _UNIT_TO_DEG[source] / _UNIT_TO_DEG[unit]
        raise ValueError(f"conversion impossible {source} -> {unit}")


class FakeCollection:
    def __init__(self, count: int) -> None:
        self.count = count


class FakeRootComponent:
    def __init__(self, bodies: int = 1, occurrences: int = 0) -> None:
        self.bRepBodies = FakeCollection(bodies)
        self.occurrences = FakeCollection(occurrences)


class FakeExportManager:
    """Écrit un vrai fichier, pour que les contrôles post-export soient réels."""

    def __init__(self, succeed: bool = True, content: bytes = b"ISO-10303-21;\n") -> None:
        self.succeed = succeed
        self.content = content
        self.calls: list[str] = []

    def createSTEPExportOptions(self, filename: str, geometry=None) -> dict:
        return {"filename": filename, "geometry": geometry}

    def execute(self, options: dict) -> bool:
        self.calls.append(options["filename"])
        if not self.succeed:
            return False
        Path(options["filename"]).write_bytes(self.content)
        return True


class FakeDesign:
    def __init__(self, params: list[FakeParam], *, bodies: int = 1,
                 export_manager: FakeExportManager | None = None) -> None:
        self.userParameters = FakeUserParameters(params)
        self.unitsManager = FakeUnitsManager()
        self.rootComponent = FakeRootComponent(bodies=bodies)
        self.exportManager = export_manager or FakeExportManager()
        self.timeline = None


@pytest.fixture
def design() -> FakeDesign:
    return FakeDesign(
        [
            FakeParam("chord_mm", "250 mm", unit="mm"),
            FakeParam("aoa_deg", "2 deg", unit="deg"),
        ]
    )


# ─────────────────────────────────────────────────────────────
# Unités et expressions
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [("mm", "mm"), ("MM", "mm"), (" deg ", "deg"), ("degrees", "deg"),
     ("meter", "m"), ("inch", "in"), ("rad", "rad")],
)
def test_unites_reconnues(raw, expected):
    assert pd.normalize_unit(raw) == expected


@pytest.mark.parametrize("raw", ["", "-", "none", "unitless", "ratio", None])
def test_unites_sans_dimension(raw):
    assert pd.normalize_unit(raw) is None


def test_unite_inconnue_rejetee():
    with pytest.raises(pd.DriverError) as exc:
        pd.normalize_unit("furlong")
    assert exc.value.status == pd.STATUS_CONFIG_ERROR


@pytest.mark.parametrize(
    "value,unit,expected",
    [
        (300.0, "mm", "300 mm"),
        (300, "mm", "300 mm"),
        (-1.5, "deg", "-1.5 deg"),
        (0.0, "deg", "0 deg"),
        (2.5, "", "2.5"),
        (1e-3, "m", "0.001 m"),
        (1234.56789, "mm", "1234.56789 mm"),
    ],
)
def test_construction_dexpression(value, unit, expected):
    assert pd.build_expression(value, unit) == expected


def test_expression_sans_notation_scientifique():
    # Fusion n'accepte pas « 1e-05 mm » : le formatage doit rester décimal.
    assert "e" not in pd.build_expression(0.00001, "mm")


@pytest.mark.parametrize("bad", [True, "300", None, float("nan"), float("inf")])
def test_valeur_non_numerique_refusee(bad):
    with pytest.raises(pd.DriverError):
        pd.build_expression(bad, "mm")


# ─────────────────────────────────────────────────────────────
# Chemins d'itération
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "iteration,expected", [(0, "iter_0000"), (7, "iter_0007"), (1234, "iter_1234"),
                           (99999, "iter_99999")]
)
def test_nom_du_dossier_diteration(tmp_path, iteration, expected):
    assert pd.iteration_dir(iteration, tmp_path).name == expected


@pytest.mark.parametrize("bad", [-1, 1.5, True, "3"])
def test_iteration_invalide_refusee(tmp_path, bad):
    with pytest.raises(pd.DriverError):
        pd.iteration_dir(bad, tmp_path)


def test_env_path_relatif_est_ancre_au_depot(monkeypatch):
    monkeypatch.setenv("DESIGN_PARAMS_PATH", "configs/design_params.yaml")
    assert pd._env_path("DESIGN_PARAMS_PATH", Path("/x")) == ROOT / "configs" / "design_params.yaml"


def test_env_path_absolu_est_conserve(monkeypatch, tmp_path):
    monkeypatch.setenv("ITERATIONS_DIR", str(tmp_path))
    assert pd._env_path("ITERATIONS_DIR", Path("/x")) == tmp_path


def test_env_path_vide_retombe_sur_le_defaut(monkeypatch):
    monkeypatch.setenv("ITERATIONS_DIR", "   ")
    assert pd._env_path("ITERATIONS_DIR", Path("/defaut")) == Path("/defaut")


# ─────────────────────────────────────────────────────────────
# Lecteur YAML minimal (repli sans PyYAML dans Fusion)
# ─────────────────────────────────────────────────────────────


def test_lecteur_minimal_lit_le_fichier_livre():
    parsed = pd.parse_simple_yaml(REAL_CONFIG.read_text(encoding="utf-8"))
    reference = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
    assert parsed == reference


def test_lecteur_minimal_types_scalaires():
    parsed = pd.parse_simple_yaml(
        "iteration: 0\n"
        "design_id: \"wing_v01\"\n"
        "flag: true\n"
        "off: false\n"
        "vide: null\n"
        "negatif: -1.5\n"
        "# commentaire\n"
        "avec_commentaire: 3.0  # inline\n"
    )
    assert parsed == {
        "iteration": 0,
        "design_id": "wing_v01",
        "flag": True,
        "off": False,
        "vide": None,
        "negatif": -1.5,
        "avec_commentaire": 3.0,
    }


def test_lecteur_minimal_imbrication():
    parsed = pd.parse_simple_yaml(
        "parameters:\n"
        "  chord_mm:\n"
        "    value: 300.0\n"
        "    unit: mm\n"
        "constraints:\n"
        "  topology_preserving: true\n"
    )
    assert parsed["parameters"]["chord_mm"] == {"value": 300.0, "unit": "mm"}
    assert parsed["constraints"] == {"topology_preserving": True}


def test_lecteur_minimal_refuse_les_listes():
    with pytest.raises(pd.DriverError) as exc:
        pd.parse_simple_yaml("direction:\n  - 1.0\n  - 0.0\n")
    assert "listes" in exc.value.message


def test_lecteur_minimal_refuse_une_ligne_sans_deux_points():
    with pytest.raises(pd.DriverError):
        pd.parse_simple_yaml("iteration 0\n")


# ─────────────────────────────────────────────────────────────
# Chargement de configuration
# ─────────────────────────────────────────────────────────────


def test_config_livree_chargee():
    assert pd.load_config(REAL_CONFIG)["design_id"] == "wing_v01"


def test_config_absente_donne_config_error(tmp_path):
    with pytest.raises(pd.DriverError) as exc:
        pd.load_config(tmp_path / "absent.yaml")
    assert exc.value.status == pd.STATUS_CONFIG_ERROR


def test_config_hors_bornes_refusee(tmp_path, cfg):
    cfg["parameters"]["chord_mm"]["value"] = 9999.0
    p = tmp_path / "design_params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(pd.DriverError) as exc:
        pd.load_config(p)
    assert exc.value.status == pd.STATUS_CONFIG_ERROR


def test_validation_minimale_attrape_les_memes_fautes(cfg, tmp_path):
    # Chemin de repli : ce que le driver vérifie lui-même sans pipeline.utils.
    cfg["parameters"]["chord_mm"]["value"] = 500.0
    with pytest.raises(pd.DriverError) as exc:
        pd._validate_config_minimal(cfg, tmp_path / "x.yaml")
    assert "hors bornes" in exc.value.message


def test_validation_minimale_accepte_une_config_valide(cfg, tmp_path):
    pd._validate_config_minimal(cfg, tmp_path / "x.yaml")  # ne lève pas


# ─────────────────────────────────────────────────────────────
# Application des paramètres (doublures)
# ─────────────────────────────────────────────────────────────


def test_parametres_appliques(design, cfg):
    applied, warnings = pd._apply_parameters(design, cfg["parameters"])
    assert design.userParameters.itemByName("chord_mm").expression == "300 mm"
    assert design.userParameters.itemByName("aoa_deg").expression == "4 deg"
    assert applied["chord_mm"]["previous_expression"] == "250 mm"
    assert applied["chord_mm"]["requested_value"] == 300.0
    assert warnings == []


def test_parametre_absent_liste_les_disponibles(design, cfg):
    cfg["parameters"]["span_mm"] = {
        "value": 1000.0, "min": 900.0, "max": 1100.0,
        "max_delta_pct": 5.0, "unit": "mm",
    }
    with pytest.raises(pd.DriverError) as exc:
        pd._apply_parameters(design, cfg["parameters"])
    assert exc.value.status == pd.STATUS_PARAM_NOT_FOUND
    assert "span_mm" in exc.value.message
    assert "chord_mm" in exc.value.message  # les noms disponibles sont donnés


def test_aucun_parametre_modifie_si_un_nom_manque(design, cfg):
    cfg["parameters"]["span_mm"] = {
        "value": 1000.0, "min": 900.0, "max": 1100.0,
        "max_delta_pct": 5.0, "unit": "mm",
    }
    with pytest.raises(pd.DriverError):
        pd._apply_parameters(design, cfg["parameters"])
    assert design.userParameters.itemByName("chord_mm").expression == "250 mm"


def test_rollback_si_un_parametre_est_refuse(cfg):
    design = FakeDesign(
        [
            FakeParam("chord_mm", "250 mm", unit="mm"),
            FakeParam("aoa_deg", "2 deg", unit="deg", raise_on_set=True),
        ]
    )
    with pytest.raises(pd.DriverError) as exc:
        pd._apply_parameters(design, cfg["parameters"])
    assert exc.value.status == pd.STATUS_PARAM_SET_FAILED
    # Le premier paramètre avait été appliqué : il doit être revenu en arrière.
    assert design.userParameters.itemByName("chord_mm").expression == "250 mm"
    assert design.userParameters.itemByName("aoa_deg").expression == "2 deg"


def test_unite_differente_de_fusion_avertit(cfg):
    # Le YAML déclare des mm, le paramètre Fusion est en cm : conversion
    # silencieuse côté Fusion, donc avertissement explicite côté driver.
    design = FakeDesign(
        [
            FakeParam("chord_mm", "25 cm", unit="cm"),
            FakeParam("aoa_deg", "2 deg", unit="deg"),
        ]
    )
    _, warnings = pd._apply_parameters(design, cfg["parameters"])
    assert any("unité déclarée" in w for w in warnings)


def test_valeur_relue_incoherente_echoue(cfg):
    class DriftingUnits(FakeUnitsManager):
        def evaluateExpression(self, expression, unit=None):
            return super().evaluateExpression(expression, unit) * 2.0

    design = FakeDesign([FakeParam("chord_mm", "250 mm", unit="mm")])
    design.unitsManager = DriftingUnits()
    with pytest.raises(pd.DriverError) as exc:
        pd._apply_parameters(design, {"chord_mm": cfg["parameters"]["chord_mm"]})
    assert exc.value.status == pd.STATUS_PARAM_SET_FAILED
    assert design.userParameters.itemByName("chord_mm").expression == "250 mm"


# ─────────────────────────────────────────────────────────────
# Export STEP (doublures)
# ─────────────────────────────────────────────────────────────


def test_export_step_ecrit_le_fichier(design, tmp_path):
    target = tmp_path / "iter_0003" / "geometry.step"
    pd._export_step(design, target)
    assert target.is_file() and target.stat().st_size > 0


def test_export_step_refuse_un_design_vide(tmp_path):
    design = FakeDesign([], bodies=0)
    with pytest.raises(pd.DriverError) as exc:
        pd._export_step(design, tmp_path / "geometry.step")
    assert exc.value.status == pd.STATUS_GEOMETRY_EMPTY


def test_echec_dexport_signale(tmp_path):
    design = FakeDesign([], export_manager=FakeExportManager(succeed=False))
    with pytest.raises(pd.DriverError) as exc:
        pd._export_step(design, tmp_path / "geometry.step")
    assert exc.value.status == pd.STATUS_EXPORT_FAILED


def test_step_vide_signale(tmp_path):
    design = FakeDesign([], export_manager=FakeExportManager(content=b""))
    with pytest.raises(pd.DriverError) as exc:
        pd._export_step(design, tmp_path / "geometry.step")
    assert exc.value.status == pd.STATUS_EXPORT_FAILED


def test_ancien_step_supprime_avant_export(tmp_path):
    target = tmp_path / "geometry.step"
    target.write_bytes(b"ANCIENNE ITERATION")
    design = FakeDesign([], export_manager=FakeExportManager(succeed=False))
    with pytest.raises(pd.DriverError):
        pd._export_step(design, target)
    # Un export raté ne doit jamais laisser le STEP précédent passer pour neuf.
    assert not target.exists()


# ─────────────────────────────────────────────────────────────
# Mode simulation (drive) et CLI
# ─────────────────────────────────────────────────────────────


def test_mode_simulation_ne_produit_pas_de_step(tmp_path, cfg_file):
    status = pd.drive(config_path=cfg_file, iterations_root=tmp_path, dry_run=True)
    assert status["success"] is False
    assert status["status"] == pd.STATUS_DRY_RUN
    assert status["iteration"] == 3
    assert status["design_id"] == "wing_v01"
    assert not (tmp_path / "iter_0003" / "geometry.step").exists()


def test_mode_simulation_prevoit_les_expressions(tmp_path, cfg_file):
    status = pd.drive(config_path=cfg_file, iterations_root=tmp_path, dry_run=True)
    assert status["applied_parameters"]["chord_mm"]["expression"] == "300 mm"
    assert status["applied_parameters"]["aoa_deg"]["expression"] == "4 deg"


def test_statut_et_journal_ecrits_dans_le_dossier_diteration(tmp_path, cfg_file):
    pd.drive(config_path=cfg_file, iterations_root=tmp_path, dry_run=True)
    out = tmp_path / "iter_0003"
    status = json.loads((out / pd.STATUS_FILENAME).read_text(encoding="utf-8"))
    assert status["status"] == pd.STATUS_DRY_RUN
    assert (out / pd.LOG_FILENAME).read_text(encoding="utf-8").strip()


def test_drive_ne_leve_jamais_sur_config_invalide(tmp_path, cfg):
    cfg["parameters"]["chord_mm"]["value"] = 9999.0
    p = tmp_path / "design_params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    status = pd.drive(config_path=p, iterations_root=tmp_path)
    assert status["success"] is False
    assert status["status"] == pd.STATUS_CONFIG_ERROR
    assert "hors bornes" in status["error_message"]


def test_drive_config_absente(tmp_path):
    status = pd.drive(config_path=tmp_path / "absent.yaml", iterations_root=tmp_path)
    assert status["status"] == pd.STATUS_CONFIG_ERROR
    assert status["step_path"] is None


def test_statut_serialisable_en_json(tmp_path, cfg_file):
    status = pd.drive(config_path=cfg_file, iterations_root=tmp_path, dry_run=True)
    json.dumps(status)  # ne doit pas lever
    for key in ("success", "status", "iteration", "design_id", "step_path",
                "applied_parameters", "error_message", "warnings", "timestamp"):
        assert key in status


def test_cli_retourne_3_en_mode_simulation(tmp_path, cfg_file, capsys):
    code = pd.main(["--config", str(cfg_file), "--iterations-dir", str(tmp_path),
                    "--dry-run"])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == pd.STATUS_DRY_RUN


def test_cli_retourne_1_sur_config_invalide(tmp_path, cfg, capsys):
    cfg["parameters"]["aoa_deg"]["value"] = 99.0
    p = tmp_path / "design_params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    code = pd.main(["--config", str(p), "--iterations-dir", str(tmp_path)])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == pd.STATUS_CONFIG_ERROR
