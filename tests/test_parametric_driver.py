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

# Fusion stocke ses valeurs en unités internes : cm pour les longueurs,
# radians pour les angles, et le nombre nu pour les grandeurs sans dimension.
_LENGTH_TO_CM = {"mm": 0.1, "cm": 1.0, "m": 100.0}
_ANGLE_TO_RAD = {"deg": 0.017453292519943295, "rad": 1.0}


class FakeParam:
    """User Parameter Fusion : expression assignable, unité fixe.

    `value` reproduit le comportement réel de Fusion — conversion vers les
    unités internes — pour que les tests des paramètres sans dimension soient
    représentatifs.
    """

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

    @property
    def value(self) -> float:
        parts = self._expression.split()
        number = float(parts[0])
        unit = parts[1] if len(parts) > 1 else self.unit
        if not unit:
            return number                      # grandeur sans dimension
        if unit in _LENGTH_TO_CM:
            return number * _LENGTH_TO_CM[unit]
        if unit in _ANGLE_TO_RAD:
            return number * _ANGLE_TO_RAD[unit]
        return number


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
# Paramètres du seed réel (chord, thickness, camber, span, aoa)
# ─────────────────────────────────────────────────────────────

SEED_PARAMETERS = {"chord", "thickness", "camber", "span", "aoa"}

SPEC_THICKNESS = {
    "value": 0.12, "min": 0.08, "max": 0.20, "max_delta_pct": 8.0,
    "unit": "unitless",
}
SPEC_CAMBER = {
    "value": 0.04, "min": 0.0, "max": 0.09, "max_delta_pct": 10.0,
    "unit": "unitless",
}


def test_config_livree_couvre_exactement_les_parametres_du_seed():
    assert set(pd.load_config(REAL_CONFIG)["parameters"]) == SEED_PARAMETERS


def test_expressions_attendues_pour_le_seed(tmp_path):
    status = pd.drive(config_path=REAL_CONFIG, iterations_root=tmp_path, dry_run=True)
    expressions = {
        name: info["expression"]
        for name, info in status["applied_parameters"].items()
    }
    # Valeurs du seed tel que généré : NACA 2412, tronçon de 80 mm, aoa nulle.
    assert expressions == {
        "chord": "300 mm",
        "thickness": "0.12",      # sans dimension : nombre nu
        "camber": "0.02",         # sans dimension : nombre nu
        "span": "80 mm",
        "aoa": "0 deg",
    }


def test_parametres_sans_unite_appliques():
    design = FakeDesign(
        [FakeParam("thickness", "0.1", unit=""), FakeParam("camber", "0.02", unit="")]
    )
    applied, warnings = pd._apply_parameters(
        design, {"thickness": SPEC_THICKNESS, "camber": SPEC_CAMBER}
    )
    assert design.userParameters.itemByName("thickness").expression == "0.12"
    assert design.userParameters.itemByName("camber").expression == "0.04"
    assert applied["thickness"]["requested_value"] == 0.12
    assert warnings == []


def test_relecture_sans_unite_nappelle_pas_evaluate_expression():
    # Non-régression : `evaluateExpression("0.12")` appliquerait les unités par
    # défaut du document et lirait 0.12 mm, faisant échouer un paramètre sain.
    class ExplodingUnits(FakeUnitsManager):
        def evaluateExpression(self, expression, unit=None):
            raise AssertionError("evaluateExpression appelé sur une grandeur sans unité")

    design = FakeDesign([FakeParam("camber", "0.02", unit="")])
    design.unitsManager = ExplodingUnits()
    pd._apply_parameters(design, {"camber": SPEC_CAMBER})  # ne doit pas lever
    assert design.userParameters.itemByName("camber").expression == "0.04"


def test_sans_unite_cote_yaml_mais_longueur_cote_fusion_echoue():
    # Fusion lirait « 0.12 » comme 0.12 mm, soit 0.012 en interne : la
    # relecture doit refuser, et le paramètre revenir en arrière.
    design = FakeDesign([FakeParam("thickness", "1 mm", unit="mm")])
    with pytest.raises(pd.DriverError) as exc:
        pd._apply_parameters(design, {"thickness": SPEC_THICKNESS})
    assert exc.value.status == pd.STATUS_PARAM_SET_FAILED
    assert design.userParameters.itemByName("thickness").expression == "1 mm"


def test_avertissement_si_fusion_attend_une_unite():
    # unit 'cm' = unité interne de Fusion : la valeur relue coïncide, donc
    # seul un avertissement doit remonter — pas un échec.
    param = FakeParam("thickness", "0.12", unit="cm")
    warnings = pd._verify_parameter(
        FakeDesign([param]), param, "thickness", SPEC_THICKNESS, "0.12"
    )
    assert any("déclaré sans unité" in w for w in warnings)


def test_camber_nul_reste_une_expression_valide():
    # camber = 0.0 (profil symétrique) est une borne atteignable.
    design = FakeDesign([FakeParam("camber", "0.04", unit="")])
    spec = dict(SPEC_CAMBER, value=0.0)
    pd._apply_parameters(design, {"camber": spec})
    assert design.userParameters.itemByName("camber").expression == "0"


# ─────────────────────────────────────────────────────────────
# Mode rebuild : calcul du profil (pur, testable hors Fusion)
# ─────────────────────────────────────────────────────────────


def _chord_extent(profile: dict) -> float:
    xs = [x for x, _ in profile["upper"] + profile["lower"]]
    return max(xs) - min(xs)


def _max_thickness(profile: dict) -> float:
    # Épaisseur mesurée verticalement, point à point (valable à incidence nulle).
    return max(yu - yl for (_, yu), (_, yl) in zip(profile["upper"], profile["lower"]))


def test_profil_ferme_au_bord_dattaque_et_de_fuite():
    p = pd.naca4_profile(30.0, 0.12, 0.02)
    assert p["upper"][0] == pytest.approx(p["lower"][0], abs=1e-12)   # bord d'attaque
    assert p["upper"][-1] == pytest.approx(p["lower"][-1], abs=1e-9)  # bord de fuite


def test_nombre_de_points():
    p = pd.naca4_profile(30.0, 0.12, 0.02, n_points=40)
    assert len(p["upper"]) == 41 and len(p["lower"]) == 41


def test_corde_respectee():
    assert _chord_extent(pd.naca4_profile(30.0, 0.12, 0.0)) == pytest.approx(30.0, rel=1e-3)
    assert _chord_extent(pd.naca4_profile(42.0, 0.12, 0.0)) == pytest.approx(42.0, rel=1e-3)


def test_epaisseur_relative_respectee():
    # t/c = 0.12 sur une corde de 30 cm -> environ 3.6 cm d'épaisseur max.
    p = pd.naca4_profile(30.0, 0.12, 0.0)
    assert _max_thickness(p) / 30.0 == pytest.approx(0.12, rel=0.02)


def test_epaisseur_suit_le_parametre():
    fin = _max_thickness(pd.naca4_profile(30.0, 0.08, 0.0))
    epais = _max_thickness(pd.naca4_profile(30.0, 0.20, 0.0))
    assert epais / fin == pytest.approx(0.20 / 0.08, rel=0.02)


def test_cambrure_nulle_donne_un_profil_symetrique():
    p = pd.naca4_profile(30.0, 0.12, 0.0)
    for (_, yu), (_, yl) in zip(p["upper"], p["lower"]):
        assert yu == pytest.approx(-yl, abs=1e-12)


def test_la_cambrure_est_mise_a_lechelle_de_la_corde():
    # Le générateur d'origine oubliait ce facteur : la ligne de cambrure
    # restait en unités normalisées, ~1/corde fois trop petite, et le
    # paramètre `camber` n'avait presque aucun effet sur la géométrie.
    ligne_moyenne = lambda p: max(
        (yu + yl) / 2.0 for (_, yu), (_, yl) in zip(p["upper"], p["lower"])
    )
    cambre = ligne_moyenne(pd.naca4_profile(30.0, 0.12, 0.02))
    # 2 % de cambrure sur 30 cm de corde -> flèche de l'ordre de 0.6 cm.
    assert cambre == pytest.approx(0.6, rel=0.05)
    # et elle doit croître proportionnellement à la corde
    assert ligne_moyenne(pd.naca4_profile(60.0, 0.12, 0.02)) == pytest.approx(
        2 * cambre, rel=0.02
    )


def test_la_cambrure_change_reellement_la_geometrie():
    symetrique = pd.naca4_profile(30.0, 0.12, 0.0)
    cambre = pd.naca4_profile(30.0, 0.06, 0.0)
    assert symetrique != cambre  # garde-fou trivial
    plat = pd.naca4_profile(30.0, 0.12, 0.0)["upper"]
    courbe = pd.naca4_profile(30.0, 0.12, 0.04)["upper"]
    ecart = max(abs(a[1] - b[1]) for a, b in zip(plat, courbe))
    assert ecart > 0.5  # cm : franchement visible, pas un epsilon


@pytest.mark.parametrize("aoa_deg", [5.0, -5.0, 12.0])
def test_incidence_tourne_le_profil(aoa_deg):
    import math

    aoa = math.radians(aoa_deg)
    droit = pd.naca4_profile(30.0, 0.12, 0.0, 0.0)
    tourne = pd.naca4_profile(30.0, 0.12, 0.0, aoa)
    # Le bord d'attaque est à l'origine : il ne bouge pas.
    assert tourne["upper"][0] == pytest.approx(droit["upper"][0], abs=1e-9)
    # Le bord de fuite descend pour une incidence positive (nez cabré).
    x_te, y_te = tourne["upper"][-1]
    assert y_te == pytest.approx(-30.0 * math.sin(aoa), abs=0.05)
    assert x_te == pytest.approx(30.0 * math.cos(aoa), abs=0.05)


def test_la_rotation_conserve_la_corde():
    import math

    for aoa_deg in (0.0, 8.0, -3.0):
        p = pd.naca4_profile(30.0, 0.12, 0.02, math.radians(aoa_deg))
        le, te = p["upper"][0], p["upper"][-1]
        longueur = math.hypot(te[0] - le[0], te[1] - le[1])
        assert longueur == pytest.approx(30.0, rel=1e-3)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"chord_cm": 0.0}, {"chord_cm": -1.0},
        {"thickness": 0.0}, {"thickness": -0.1},
        {"camber": -0.01},
        {"n_points": 3},
        {"camber_position": 0.0}, {"camber_position": 1.0},
    ],
)
def test_valeurs_de_profil_aberrantes_refusees(kwargs):
    base = {"chord_cm": 30.0, "thickness": 0.12, "camber": 0.02}
    base.update(kwargs)
    with pytest.raises(pd.DriverError):
        pd.naca4_profile(**base)


# ─────────────────────────────────────────────────────────────
# Mode rebuild : traduction design_params -> géométrie
# ─────────────────────────────────────────────────────────────


def test_plan_de_reconstruction_depuis_la_config_livree():
    plan = pd.profile_from_parameters(pd.load_config(REAL_CONFIG)["parameters"])
    assert plan["chord_cm"] == pytest.approx(30.0)    # 300 mm
    assert plan["span_cm"] == pytest.approx(8.0)      # 80 mm
    assert plan["thickness"] == pytest.approx(0.12)
    assert plan["camber"] == pytest.approx(0.02)
    assert plan["aoa_deg"] == pytest.approx(0.0)


def test_conversion_des_unites_de_longueur():
    for unit, attendu in (("mm", 30.0), ("cm", 300.0), ("m", 30000.0), ("in", 762.0)):
        spec = {"value": 300.0, "min": 1.0, "max": 1e6, "max_delta_pct": 5.0,
                "unit": unit}
        assert pd.to_cm(spec, "chord") == pytest.approx(attendu)


def test_conversion_des_angles():
    import math

    deg = {"value": 90.0, "min": -180.0, "max": 180.0, "max_delta_pct": 5.0,
           "unit": "deg"}
    rad = dict(deg, value=math.pi / 2, unit="rad")
    assert pd.to_rad(deg, "aoa") == pytest.approx(math.pi / 2)
    assert pd.to_rad(rad, "aoa") == pytest.approx(math.pi / 2)


def test_une_longueur_sans_unite_est_refusee():
    spec = {"value": 300.0, "min": 1.0, "max": 1e6, "max_delta_pct": 5.0,
            "unit": "unitless"}
    with pytest.raises(pd.DriverError):
        pd.to_cm(spec, "chord")


def test_un_ratio_avec_unite_est_refuse():
    spec = {"value": 0.12, "min": 0.05, "max": 0.3, "max_delta_pct": 5.0,
            "unit": "mm"}
    with pytest.raises(pd.DriverError):
        pd.to_dimensionless(spec, "thickness")


def test_parametre_manquant_pour_le_rebuild(cfg):
    with pytest.raises(pd.DriverError) as exc:
        pd.profile_from_parameters(cfg["parameters"])  # chord_mm / aoa_deg
    assert exc.value.status == pd.STATUS_CONFIG_ERROR
    assert "chord" in exc.value.message


@pytest.mark.parametrize(
    "requested,attendu",
    [(None, pd.GEOMETRY_MODE_REBUILD), ("rebuild", "rebuild"),
     ("parameters", "parameters"), ("REBUILD", "rebuild")],
)
def test_resolution_du_mode(requested, attendu, monkeypatch):
    monkeypatch.delenv("FUSION_GEOMETRY_MODE", raising=False)
    assert pd.resolve_geometry_mode(requested) == attendu


def test_mode_depuis_lenvironnement(monkeypatch):
    monkeypatch.setenv("FUSION_GEOMETRY_MODE", "parameters")
    assert pd.resolve_geometry_mode() == "parameters"


def test_mode_inconnu_refuse(monkeypatch):
    monkeypatch.delenv("FUSION_GEOMETRY_MODE", raising=False)
    with pytest.raises(pd.DriverError):
        pd.resolve_geometry_mode("magique")


def test_le_mode_simulation_calcule_la_geometrie(tmp_path):
    status = pd.drive(config_path=REAL_CONFIG, iterations_root=tmp_path,
                      dry_run=True)
    assert status["geometry_mode"] == pd.GEOMETRY_MODE_REBUILD
    geo = status["geometry"]
    assert geo["chord_cm"] == pytest.approx(30.0)
    assert geo["span_cm"] == pytest.approx(8.0)
    assert geo["bbox_cm"]["x_max"] == pytest.approx(30.0, rel=1e-3)
    json.dumps(status)  # le plan doit rester sérialisable


def test_mode_parameters_ne_calcule_pas_de_geometrie(tmp_path):
    status = pd.drive(config_path=REAL_CONFIG, iterations_root=tmp_path,
                      dry_run=True, geometry_mode="parameters")
    assert status["geometry_mode"] == "parameters"
    assert status["geometry"] is None


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


# La fixture `cfg` décrit un design générique à deux paramètres : le mode
# rebuild, lui, exige les cinq paramètres du seed. Ces tests portent sur la
# mécanique de statut, d'où le mode 'parameters'.


def test_mode_simulation_ne_produit_pas_de_step(tmp_path, cfg_file):
    status = pd.drive(config_path=cfg_file, iterations_root=tmp_path, dry_run=True,
                      geometry_mode="parameters")
    assert status["success"] is False
    assert status["status"] == pd.STATUS_DRY_RUN
    assert status["iteration"] == 3
    assert status["design_id"] == "wing_v01"
    assert not (tmp_path / "iter_0003" / "geometry.step").exists()


def test_mode_simulation_prevoit_les_expressions(tmp_path, cfg_file):
    status = pd.drive(config_path=cfg_file, iterations_root=tmp_path, dry_run=True,
                      geometry_mode="parameters")
    assert status["applied_parameters"]["chord_mm"]["expression"] == "300 mm"
    assert status["applied_parameters"]["aoa_deg"]["expression"] == "4 deg"


def test_rebuild_refuse_une_config_sans_les_parametres_du_seed(tmp_path, cfg_file):
    # Garde-fou : plutôt que de reconstruire n'importe quoi, le driver s'arrête.
    status = pd.drive(config_path=cfg_file, iterations_root=tmp_path, dry_run=True)
    assert status["status"] == pd.STATUS_CONFIG_ERROR
    assert "chord" in status["error_message"]


def test_statut_et_journal_ecrits_dans_le_dossier_diteration(tmp_path, cfg_file):
    pd.drive(config_path=cfg_file, iterations_root=tmp_path, dry_run=True,
             geometry_mode="parameters")
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
    status = pd.drive(config_path=cfg_file, iterations_root=tmp_path, dry_run=True,
                      geometry_mode="parameters")
    json.dumps(status)  # ne doit pas lever
    for key in ("success", "status", "iteration", "design_id", "step_path",
                "geometry_mode", "geometry", "applied_parameters",
                "error_message", "warnings", "timestamp"):
        assert key in status


def test_cli_retourne_3_en_mode_simulation(tmp_path, cfg_file, capsys):
    code = pd.main(["--config", str(cfg_file), "--iterations-dir", str(tmp_path),
                    "--dry-run", "--geometry-mode", "parameters"])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["status"] == pd.STATUS_DRY_RUN


def test_cli_retourne_1_sur_config_invalide(tmp_path, cfg, capsys):
    cfg["parameters"]["aoa_deg"]["value"] = 99.0
    p = tmp_path / "design_params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    code = pd.main(["--config", str(p), "--iterations-dir", str(tmp_path)])
    assert code == 1
    assert json.loads(capsys.readouterr().out)["status"] == pd.STATUS_CONFIG_ERROR
