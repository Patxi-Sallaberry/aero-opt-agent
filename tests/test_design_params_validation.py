"""Tests de la validation de design_params.yaml (Phase 0).

Couvre les trois familles de règles du contrat §3.1 :
structure, bornes min/max, et max_delta_pct.
"""

import copy
from pathlib import Path

import pytest

from pipeline.utils import (
    ConfigValidationError,
    allowed_range,
    load_design_params,
    load_yaml,
    max_abs_delta,
    save_design_params,
    validate_design_params,
)

ROOT = Path(__file__).resolve().parents[1]
REAL_CONFIG = ROOT / "configs" / "design_params.yaml"


@pytest.fixture
def cfg() -> dict:
    """Configuration valide de référence, recopiée à chaque test."""
    return {
        "iteration": 0,
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
        "constraints": {
            "topology_preserving": True,
            "min_wall_thickness_mm": 1.5,
        },
        "objectives": {"primary": "maximize_Cl_Cd"},
    }


def errors_of(data, previous=None) -> list[str]:
    return validate_design_params(data, previous=previous).errors


# ─────────────────────────────────────────────────────────────
# Le fichier livré doit être valide
# ─────────────────────────────────────────────────────────────


def test_le_fichier_configs_livre_est_valide():
    report = validate_design_params(load_yaml(REAL_CONFIG), path=REAL_CONFIG)
    assert report.ok, report.format()


def test_configuration_de_reference_valide(cfg):
    report = validate_design_params(cfg)
    assert report.ok, report.format()
    assert report.warnings == []


# ─────────────────────────────────────────────────────────────
# 1. Structure
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key", ["iteration", "design_id", "parameters", "constraints", "objectives"]
)
def test_cle_racine_manquante_rejetee(cfg, key):
    cfg.pop(key)
    assert any(key in e for e in errors_of(cfg))


def test_cle_racine_inconnue_rejetee(cfg):
    cfg["fudge_factor"] = 1.0
    assert any("inconnue" in e for e in errors_of(cfg))


@pytest.mark.parametrize("bad", ["0", 1.5, -1, True])
def test_iteration_doit_etre_entier_positif(cfg, bad):
    cfg["iteration"] = bad
    assert any("iteration" in e for e in errors_of(cfg))


@pytest.mark.parametrize("bad", ["", "   ", 42, "aile v01"])
def test_design_id_invalide_rejete(cfg, bad):
    cfg["design_id"] = bad
    assert any("design_id" in e for e in errors_of(cfg))


def test_parameters_vide_rejete(cfg):
    cfg["parameters"] = {}
    assert any("au moins un paramètre" in e for e in errors_of(cfg))


def test_nom_de_parametre_invalide_rejete(cfg):
    cfg["parameters"]["2bad name"] = cfg["parameters"].pop("chord_mm")
    assert any("nom invalide" in e for e in errors_of(cfg))


@pytest.mark.parametrize("key", ["value", "min", "max", "max_delta_pct", "unit"])
def test_cle_de_parametre_manquante_rejetee(cfg, key):
    cfg["parameters"]["chord_mm"].pop(key)
    assert any("chord_mm" in e for e in errors_of(cfg))


def test_cle_de_parametre_inconnue_rejetee(cfg):
    cfg["parameters"]["chord_mm"]["step"] = 5.0
    assert any("inconnue" in e and "chord_mm" in e for e in errors_of(cfg))


@pytest.mark.parametrize("bad", ["300", None, True, float("nan"), float("inf")])
def test_value_non_numerique_rejetee(cfg, bad):
    cfg["parameters"]["chord_mm"]["value"] = bad
    assert any("value" in e for e in errors_of(cfg))


def test_unit_vide_rejetee(cfg):
    cfg["parameters"]["chord_mm"]["unit"] = "  "
    assert any("unit" in e for e in errors_of(cfg))


def test_objectif_inconnu_rejete(cfg):
    cfg["objectives"]["primary"] = "maximize_beauty"
    assert any("objectives.primary" in e for e in errors_of(cfg))


@pytest.mark.parametrize(
    "primary", ["maximize_Cl_Cd", "maximize_downforce", "minimize_Cd"]
)
def test_objectifs_autorises_acceptes(cfg, primary):
    cfg["objectives"]["primary"] = primary
    assert validate_design_params(cfg).ok


def test_topology_preserving_non_booleen_rejete(cfg):
    cfg["constraints"]["topology_preserving"] = "true"
    assert any("topology_preserving" in e for e in errors_of(cfg))


def test_epaisseur_de_paroi_negative_rejetee(cfg):
    cfg["constraints"]["min_wall_thickness_mm"] = -0.5
    assert any("min_wall_thickness_mm" in e for e in errors_of(cfg))


def test_toutes_les_erreurs_sont_collectees_en_une_passe(cfg):
    cfg["parameters"]["chord_mm"]["value"] = 999.0        # hors bornes
    cfg["parameters"]["aoa_deg"]["max_delta_pct"] = -1.0  # invalide
    cfg["objectives"]["primary"] = "nope"                 # inconnu
    assert len(errors_of(cfg)) >= 3


# ─────────────────────────────────────────────────────────────
# 2. Bornes min / max
# ─────────────────────────────────────────────────────────────


def test_value_sous_le_min_rejetee(cfg):
    cfg["parameters"]["chord_mm"]["value"] = 219.9
    assert any("en dessous de min" in e for e in errors_of(cfg))


def test_value_au_dessus_du_max_rejetee(cfg):
    cfg["parameters"]["chord_mm"]["value"] = 420.1
    assert any("au dessus de" in e for e in errors_of(cfg))


@pytest.mark.parametrize("value", [220.0, 420.0])
def test_value_exactement_sur_la_borne_acceptee(cfg, value):
    cfg["parameters"]["chord_mm"]["value"] = value
    assert validate_design_params(cfg).ok


def test_min_superieur_ou_egal_au_max_rejete(cfg):
    cfg["parameters"]["chord_mm"]["min"] = 420.0
    cfg["parameters"]["chord_mm"]["max"] = 420.0
    assert any("strictement inférieur" in e for e in errors_of(cfg))


def test_valeur_negative_dans_bornes_negatives_acceptee(cfg):
    cfg["parameters"]["aoa_deg"]["value"] = -1.0
    assert validate_design_params(cfg).ok


@pytest.mark.parametrize("bad", [0.0, -5.0, 101.0])
def test_max_delta_pct_hors_plage_rejete(cfg, bad):
    cfg["parameters"]["chord_mm"]["max_delta_pct"] = bad
    assert any("max_delta_pct" in e for e in errors_of(cfg))


def test_max_delta_pct_large_avertit_sans_bloquer(cfg):
    cfg["parameters"]["chord_mm"]["max_delta_pct"] = 40.0
    report = validate_design_params(cfg)
    assert report.ok
    assert any("max_delta_pct" in w for w in report.warnings)


# ─────────────────────────────────────────────────────────────
# 3. max_delta_pct (par rapport à la dernière itération réussie)
# ─────────────────────────────────────────────────────────────


def _next(cfg: dict, **values) -> dict:
    """Construit l'itération suivante à partir de `cfg`."""
    nxt = copy.deepcopy(cfg)
    nxt["iteration"] = cfg["iteration"] + 1
    for name, value in values.items():
        nxt["parameters"][name]["value"] = value
    return nxt


def test_variation_dans_le_budget_acceptee(cfg):
    # 300 -> 315 = +5 %, budget 7 %
    assert validate_design_params(_next(cfg, chord_mm=315.0), previous=cfg).ok


def test_variation_hors_budget_rejetee(cfg):
    # 300 -> 330 = +10 %, budget 7 %
    errs = errors_of(_next(cfg, chord_mm=330.0), previous=cfg)
    assert any("max_delta_pct" in e and "chord_mm" in e for e in errs)


def test_variation_exactement_au_budget_acceptee(cfg):
    # 300 -> 321 = exactement 7 %
    assert validate_design_params(_next(cfg, chord_mm=321.0), previous=cfg).ok


def test_variation_negative_hors_budget_rejetee(cfg):
    assert not validate_design_params(_next(cfg, chord_mm=270.0), previous=cfg).ok


def test_le_message_derreur_donne_lintervalle_autorise(cfg):
    errs = errors_of(_next(cfg, chord_mm=400.0), previous=cfg)
    msg = next(e for e in errs if "chord_mm" in e)
    assert "intervalle autorisé" in msg and "279" in msg and "321" in msg


def test_bornes_et_budget_se_combinent(cfg):
    # value proche du max : la borne max écrase la bande de variation
    cfg["parameters"]["chord_mm"]["value"] = 410.0
    lo, hi = allowed_range(410.0, cfg["parameters"]["chord_mm"])
    assert hi == 420.0                       # tronqué par max
    assert lo == pytest.approx(381.3)        # 410 - 7 %


def test_valeur_precedente_nulle_utilise_lamplitude(cfg):
    # aoa_deg = 0 : le delta relatif est indéfini, on retombe sur 12 % de
    # l'amplitude (10 - (-1)) = 1.32 deg.
    cfg["parameters"]["aoa_deg"]["value"] = 0.0
    assert max_abs_delta(0.0, cfg["parameters"]["aoa_deg"]) == pytest.approx(1.32)
    assert validate_design_params(_next(cfg, aoa_deg=1.3), previous=cfg).ok
    assert not validate_design_params(_next(cfg, aoa_deg=2.0), previous=cfg).ok


def test_delta_non_verifie_sans_configuration_precedente(cfg):
    # Sans `previous`, seules structure et bornes s'appliquent.
    assert validate_design_params(_next(cfg, chord_mm=420.0)).ok


def test_iteration_doit_progresser(cfg):
    stale = copy.deepcopy(cfg)  # même numéro d'itération
    assert any("croissante" in e for e in errors_of(stale, previous=cfg))


def test_saut_diteration_avertit_sans_bloquer(cfg):
    jumped = _next(cfg, chord_mm=305.0)
    jumped["iteration"] = 5
    report = validate_design_params(jumped, previous=cfg)
    assert report.ok
    assert any("saut" in w for w in report.warnings)


def test_changement_de_design_id_rejete(cfg):
    nxt = _next(cfg, chord_mm=305.0)
    nxt["design_id"] = "wing_v02"
    assert any("design_id" in e for e in errors_of(nxt, previous=cfg))


@pytest.mark.parametrize(
    "key,value", [("min", 100.0), ("max", 900.0), ("max_delta_pct", 50.0), ("unit", "cm")]
)
def test_lagent_ne_peut_pas_desserrer_les_bornes(cfg, key, value):
    nxt = _next(cfg, chord_mm=305.0)
    nxt["parameters"]["chord_mm"][key] = value
    assert any("seul 'value' peut changer" in e for e in errors_of(nxt, previous=cfg))


def test_ajout_ou_suppression_de_parametre_rejete(cfg):
    added = _next(cfg, chord_mm=305.0)
    added["parameters"]["span_mm"] = {
        "value": 1000.0, "min": 800.0, "max": 1200.0,
        "max_delta_pct": 5.0, "unit": "mm",
    }
    assert any("ajouté" in e for e in errors_of(added, previous=cfg))

    removed = _next(cfg, chord_mm=305.0)
    removed["parameters"].pop("aoa_deg")
    assert any("supprimé" in e for e in errors_of(removed, previous=cfg))


# ─────────────────────────────────────────────────────────────
# Chargement / écriture
# ─────────────────────────────────────────────────────────────


def test_load_design_params_leve_sur_config_invalide(tmp_path, cfg):
    import yaml

    cfg["parameters"]["chord_mm"]["value"] = 9999.0
    p = tmp_path / "design_params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ConfigValidationError) as exc:
        load_design_params(p)
    assert exc.value.errors


def test_load_design_params_accepte_le_fichier_livre():
    assert load_design_params(REAL_CONFIG)["design_id"] == "wing_v01"


def test_fichier_introuvable_leve(tmp_path):
    with pytest.raises(ConfigValidationError):
        load_yaml(tmp_path / "absent.yaml")


def test_fichier_vide_leve(tmp_path):
    p = tmp_path / "vide.yaml"
    p.write_text("", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_yaml(p)


def test_yaml_malforme_leve(tmp_path):
    p = tmp_path / "casse.yaml"
    p.write_text("parameters: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigValidationError):
        load_yaml(p)


def test_save_refuse_decrire_une_config_invalide(tmp_path, cfg):
    cfg["parameters"]["chord_mm"]["value"] = 9999.0
    target = tmp_path / "out.yaml"
    with pytest.raises(ConfigValidationError):
        save_design_params(cfg, target)
    assert not target.exists()


def test_aller_retour_ecriture_lecture(tmp_path, cfg):
    target = tmp_path / "out.yaml"
    save_design_params(cfg, target)
    assert load_design_params(target) == cfg


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def test_cli_retourne_0_sur_config_valide(capsys):
    from pipeline.utils import main

    assert main([str(REAL_CONFIG)]) == 0
    assert "OK" in capsys.readouterr().out


def test_cli_retourne_1_sur_config_invalide(tmp_path, cfg, capsys):
    import yaml

    from pipeline.utils import main

    cfg["parameters"]["chord_mm"]["value"] = 9999.0
    p = tmp_path / "design_params.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    assert main([str(p)]) == 1
    assert "ECHEC" in capsys.readouterr().err


def test_cli_retourne_2_sur_fichier_absent(tmp_path, capsys):
    from pipeline.utils import main

    assert main([str(tmp_path / "absent.yaml")]) == 2
