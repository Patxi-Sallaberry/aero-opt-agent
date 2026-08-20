"""Tests du post-traitement : sélection, figures, rapport, export.

ParaView et OpenFOAM ne sont pas requis : ce qui les concerne est court-circuité
et vérifié par son message d'indisponibilité. Le reste — sélection du meilleur
design, tracés SVG, lecture physique, rapport, HTML autonome — est du Python
pur et se teste intégralement.
"""

import json
import math
from pathlib import Path

import pytest
import yaml

from pipeline import master_pipeline as mp
from pipeline.utils import load_yaml
from scripts import export_best as eb
from scripts import plots
from scripts import run_loop as loop

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


def analytic_cfd(monkeypatch):
    """Modèle CFD analytique : Cl/Cd croît avec l'incidence puis décroche."""
    def _run(iteration_dir, config_path, cfd_settings_path, timeout_s=None):
        design = load_yaml(config_path)
        p = {n: float(s["value"]) for n, s in design["parameters"].items()}
        aoa = p["aoa"]
        cl = 0.11 * (aoa - (aoa - 5.0) ** 2 * 0.06) + 12.0 * p["camber"] + 0.1
        cd = 0.008 + 0.35 * p["thickness"] + 0.0006 * aoa ** 2
        results = {
            "iteration": design["iteration"], "success": True, "status": "OK",
            "Cd": cd, "Cl": cl, "Cl_Cd": cl / cd, "mesh_ok": True,
            "converged": True, "error_message": None,
            "averaging_window": 200, "Cd_rel_std": 1e-5,
            "coefficients_stable": True,
            "mesh": {"n_cells": 168394, "max_non_orthogonality": 47.4,
                     "max_skewness": 2.1},
        }
        Path(iteration_dir, "results.json").write_text(json.dumps(results))
        return True, "ok"

    monkeypatch.setattr(mp, "run_cfd", _run)


@pytest.fixture
def serie(config, iterations, monkeypatch) -> Path:
    """Une série d'optimisation complète, sans export automatique."""
    analytic_cfd(monkeypatch)
    loop.run_loop(config, REAL_CFD, iterations, max_iterations=8, strategy="local",
                  geometry_backend="internal", stagnation_patience=8,
                  export_best=False)
    return iterations


# ─────────────────────────────────────────────────────────────
# Sélection
# ─────────────────────────────────────────────────────────────


def test_meilleure_iteration(serie):
    best = eb.best_iteration(serie)
    objectives = [
        r["objective"] for r in mp.history(serie)
        if isinstance(r.get("objective"), (int, float))
    ]
    assert best["objective"] == max(objectives)


def test_selection_sur_lobjectif_pas_sur_cl_cd(tmp_path):
    """Un objectif de minimisation de traînée ne se classe pas sur Cl/Cd."""
    root = tmp_path / "iters"
    for i, (cl_cd, objective) in enumerate([(50.0, -0.02), (10.0, -0.005)]):
        d = root / f"iter_{i:04d}"
        d.mkdir(parents=True)
        (d / "iteration.json").write_text(json.dumps({
            "iteration": i, "success": True, "status": "OK",
            "Cl_Cd": cl_cd, "objective": objective,
        }), encoding="utf-8")
    # Le meilleur objectif (-0.005, soit Cd = 0.005) a le PIRE Cl/Cd.
    assert eb.best_iteration(root)["iteration"] == 1


def test_serie_sans_succes(tmp_path):
    root = tmp_path / "vide"
    root.mkdir()
    with pytest.raises(eb.ExportError):
        eb.best_iteration(root)


def test_dossier_horodate():
    from datetime import datetime

    folder = eb.run_folder(Path("/tmp/x"), datetime(2026, 8, 20, 12, 50, 6))
    assert folder.name == "run_20260820_125006"


# ─────────────────────────────────────────────────────────────
# Section du profil
# ─────────────────────────────────────────────────────────────


def test_section_exportee(config, tmp_path):
    section = eb.write_profile_section(load_yaml(config), tmp_path)
    assert (tmp_path / "profile_section.csv").is_file()
    assert (tmp_path / "profile_section.dat").is_file()
    assert section["chord_mm"] == pytest.approx(300.0)

    csv = (tmp_path / "profile_section.csv").read_text(encoding="utf-8").splitlines()
    assert csv[0] == "surface,x_mm,y_mm"
    assert sum(1 for line in csv if line.startswith("extrados")) == 81


def test_le_dat_suit_la_convention_profil(config, tmp_path):
    """Format profil : bord de fuite → bord d'attaque, puis retour."""
    eb.write_profile_section(load_yaml(config), tmp_path)
    lignes = (tmp_path / "profile_section.dat").read_text(encoding="utf-8").splitlines()
    coords = [tuple(map(float, line.split())) for line in lignes[1:]]
    assert coords[0][0] > coords[len(coords) // 2][0]      # part du bord de fuite
    assert coords[-1][0] > coords[len(coords) // 2][0]     # revient au bord de fuite


# ─────────────────────────────────────────────────────────────
# Distribution de pression
# ─────────────────────────────────────────────────────────────


def _fake_samples(section, u_inf=20.0):
    """Points de paroi synthétiques : dépression à l'extrados, arrêt dessous."""
    samples = []
    dynamic = 0.5 * u_inf * u_inf
    for x_mm, y_mm in section["upper"]:
        xc = x_mm / section["chord_mm"]
        cp = -1.8 * math.exp(-xc * 4) - 0.1
        # Plusieurs points par abscisse, comme le fait un maillage 3D.
        for _ in range(5):
            samples.append((x_mm / 1000.0, y_mm / 1000.0, cp * dynamic))
    for x_mm, y_mm in section["lower"]:
        xc = x_mm / section["chord_mm"]
        cp = 0.9 * math.exp(-xc * 8) + 0.02
        for _ in range(5):
            samples.append((x_mm / 1000.0, y_mm / 1000.0, cp * dynamic))
    return samples


def test_cp_separe_extrados_et_intrados(config, tmp_path):
    section = eb.write_profile_section(load_yaml(config), tmp_path)
    cp = eb.cp_distribution(_fake_samples(section), section, 20.0)
    assert cp["upper"] and cp["lower"]
    # L'extrados est en dépression, l'intrados en surpression près du bord
    # d'attaque.
    assert min(c for _, c in cp["upper"]) < -1.0
    assert max(c for _, c in cp["lower"]) > 0.5


def test_cp_moyenne_les_doublons_denvergure(config, tmp_path):
    # Cinq points par abscisse en entrée : la sortie doit être une courbe, pas
    # un nuage — c'est ce qui divisait la taille du SVG par trente.
    section = eb.write_profile_section(load_yaml(config), tmp_path)
    samples = _fake_samples(section)
    cp = eb.cp_distribution(samples, section, 20.0, bins=60)
    assert len(cp["upper"]) <= 60
    assert len(cp["upper"]) < len(samples) / 5


def test_cp_reste_dans_la_corde(config, tmp_path):
    section = eb.write_profile_section(load_yaml(config), tmp_path)
    cp = eb.cp_distribution(_fake_samples(section), section, 20.0)
    for xc, _ in cp["upper"] + cp["lower"]:
        assert -0.05 <= xc <= 1.05


# ─────────────────────────────────────────────────────────────
# Figures
# ─────────────────────────────────────────────────────────────


def test_graphique_svg_valide():
    svg = plots.chart(
        [{"points": [(0, 1), (1, 2), (2, 1.5)], "label": "essai"}],
        title="test", x_label="x", y_label="y",
    )
    assert svg.startswith("<svg") and svg.rstrip().endswith("</svg>")
    assert "essai" in svg


def test_graphique_sans_donnees():
    svg = plots.chart([], title="rien")
    assert "aucune donnée" in svg


def test_axe_inverse_pour_le_cp():
    """Avec l'axe inversé, une valeur plus NÉGATIVE se dessine plus haut."""
    svg = plots.chart(
        [{"points": [(0.0, 0.0), (1.0, -2.0)]}],
        invert_y=True, y_range=(-2.0, 0.0),
    )
    coords = [
        float(part.split(",")[1])
        for part in svg.split('d="M')[1].split('"')[0].replace("L", " ").split()
    ]
    assert coords[1] < coords[0]   # y écran plus petit = plus haut


def test_profil_dessine(config, tmp_path):
    section = eb.write_profile_section(load_yaml(config), tmp_path)
    svg = plots.airfoil_outline(section["upper"], section["lower"], title="profil")
    assert svg.startswith("<svg")
    assert "profil" in svg


def test_figures_ecrites(serie, config, tmp_path):
    section = eb.write_profile_section(load_yaml(config), tmp_path)
    figures = eb.build_figures(
        tmp_path, mp.history(serie), section, {}, None, 0
    )
    assert "optimization_progress" in figures
    assert "profile_shape" in figures
    for relative in figures.values():
        assert (tmp_path / relative).is_file()


# ─────────────────────────────────────────────────────────────
# Lecture physique
# ─────────────────────────────────────────────────────────────


def test_explication_incidence():
    notes = eb.explain_physics(
        {"aoa": 0.0, "camber": 0.02, "thickness": 0.12, "chord": 300.0},
        {"aoa": 5.04, "camber": 0.02, "thickness": 0.12, "chord": 300.0},
        None, {"Cd": 0.025, "Cl": 0.76}, None,
    )
    texte = " ".join(notes)
    assert "Incidence" in texte
    assert "sommet de la courbe" in texte     # 5.04° tombe dans la plage utile
    assert "cambrure" not in texte.lower()    # inchangée : rien à en dire


def test_explication_amincissement():
    notes = eb.explain_physics(
        {"aoa": 0.0, "thickness": 0.12, "camber": 0.02, "chord": 300.0},
        {"aoa": 0.0, "thickness": 0.10, "camber": 0.02, "chord": 300.0},
        None, {"Cd": 0.02, "Cl": 0.3}, None,
    )
    assert any("aminci" in n for n in notes)


def test_explication_du_compromis_favorable():
    notes = eb.explain_physics(
        {"aoa": 0.0}, {"aoa": 5.0},
        {"Cl": 0.25, "Cd": 0.031}, {"Cl": 0.77, "Cd": 0.026}, None,
    )
    texte = " ".join(notes)
    # Portance en hausse ET traînée en baisse : la phrase doit le dire sans
    # tourner à « n'augmente que de -18 % ».
    assert "baisse de" in texte
    assert "n'augmente que de -" not in texte


def test_explication_du_pic_de_depression():
    notes = eb.explain_physics(
        {"aoa": 0.0}, {"aoa": 5.0}, None, {"Cd": 0.02, "Cl": 0.7},
        {"upper": [(0.03, -1.83), (0.5, -0.4)], "lower": [(0.03, 0.9)]},
    )
    assert any("-1.83" in n and "3 %" in n for n in notes)


def test_aucun_commentaire_sans_changement():
    assert eb.explain_physics(
        {"aoa": 5.0, "camber": 0.02}, {"aoa": 5.0, "camber": 0.02},
        None, {"Cd": 0.02, "Cl": 0.7}, None,
    ) == []


# ─────────────────────────────────────────────────────────────
# Rapport
# ─────────────────────────────────────────────────────────────


def test_rapport_complet(serie, config, tmp_path):
    design = load_yaml(config)
    record = eb.best_iteration(serie)
    section = eb.write_profile_section(design, tmp_path)
    report = eb.build_report(
        design, design, record, {"Cd": 0.02, "Cl": 0.7, "Cl_Cd": 35.0},
        None, mp.history(serie), section, {}, [], ["**Une note**"],
        False, True, serie / "iter_0000", None,
    )
    for attendu in ("# Design optimisé", "## Performances", "## Paramètres",
                    "## Déroulé de l'optimisation", "Pas de fichier STEP",
                    "## Ce que valent ces chiffres", "Une note"):
        assert attendu in report


def test_les_barres_verticales_ne_cassent_pas_le_tableau(serie, config, tmp_path):
    """Un message d'échec contenant « | » désalignait tout le tableau."""
    design = load_yaml(config)
    history = [{
        "iteration": 0, "success": False, "status": "CFD_FAILED",
        "error_message": "checkMesh : 1 contrôle en échec | skewness 4.03",
        "Cd": None, "Cl": None, "Cl_Cd": None,
    }]
    section = eb.write_profile_section(design, tmp_path)
    report = eb.build_report(
        design, design, {"iteration": 0}, {"Cd": 0.02, "Cl": 0.7, "Cl_Cd": 35.0},
        None, history, section, {}, [], [], False, False,
        serie / "iter_0000", None,
    )
    ligne = next(
        line for line in report.splitlines()
        if line.startswith("| 0") and "checkMesh" in line
    )
    assert ligne.count("|") == 6      # 5 colonnes, pas une de plus


def test_html_autonome(serie, config, tmp_path):
    design = load_yaml(config)
    section = eb.write_profile_section(design, tmp_path)
    figures = eb.build_figures(tmp_path, mp.history(serie), section, {}, None, 0)
    report = eb.build_report(
        design, design, eb.best_iteration(serie),
        {"Cd": 0.02, "Cl": 0.7, "Cl_Cd": 35.0}, None, mp.history(serie),
        section, figures, [], [], False, True, serie / "iter_0000", None,
    )
    html = eb.markdown_to_html(report, tmp_path, "essai")
    assert html.startswith("<!doctype html>")
    assert "<table>" in html and "</table>" in html
    # Les SVG sont intégrés, pas référencés : le fichier doit survivre à un
    # déplacement. Le dossier `figures/` reste cité dans le texte, ce qui est
    # normal — c'est une référence EXTERNE d'image qui serait fautive.
    assert "<svg" in html
    assert 'src="figures/' not in html
    assert "<img src=" not in html or "base64" in html


def test_html_echappe_le_code():
    html = eb.markdown_to_html("Voir `a < b` et **gras**.", Path("."), "t")
    assert "<code>a &lt; b</code>" in html
    assert "<strong>gras</strong>" in html


# ─────────────────────────────────────────────────────────────
# Export complet
# ─────────────────────────────────────────────────────────────


def test_export_complet(serie, tmp_path):
    output = tmp_path / "livraison"
    summary = eb.export_best(serie, output, visuals=False)

    for name in ("README.md", "report.html", "geometry.stl",
                 "design_params.yaml", "results.json",
                 "profile_section.csv", "profile_section.dat"):
        assert (output / name).is_file(), name
    assert (output / "figures").is_dir()
    assert summary["iteration"] == eb.best_iteration(serie)["iteration"]
    assert summary["has_step"] is False


def test_export_sans_paraview_le_signale(serie, tmp_path, monkeypatch):
    monkeypatch.setattr(eb.shutil, "which", lambda name: None)
    summary = eb.export_best(serie, tmp_path / "sortie", visuals=True)
    # Le rapport sort quand même : les visuels sont un plus, pas une condition.
    assert (tmp_path / "sortie" / "README.md").is_file()
    assert summary["visuals_error"]


def test_export_dossier_par_defaut_horodate(serie, tmp_path, monkeypatch):
    monkeypatch.setattr(eb, "RESULTS_ROOT", tmp_path / "results")
    summary = eb.export_best(serie, None, visuals=False)
    assert Path(summary["output"]).parent.name.startswith("run_")
    assert Path(summary["output"]).name == "best_design"


def test_export_refuse_une_requalification_incoherente(serie, tmp_path, config):
    """Livrer la géométrie d'un design avec les chiffres d'un autre serait pire
    qu'un échec : le dossier aurait l'air normal."""
    autre = tmp_path / "autre"
    autre.mkdir()
    design = load_yaml(config)
    design["parameters"]["chord"]["value"] = 411.0
    (autre / "design_params.yaml").write_text(yaml.safe_dump(design), encoding="utf-8")
    (autre / "results.json").write_text(json.dumps({"Cd": 0.02, "Cl": 0.8}),
                                        encoding="utf-8")
    with pytest.raises(eb.ExportError) as exc:
        eb.export_best(serie, tmp_path / "sortie", qualified_dir=autre, visuals=False)
    assert "chord" in str(exc.value)


def test_export_utilise_les_chiffres_de_la_requalification(serie, tmp_path):
    """Les coefficients du réglage fin doivent primer sur ceux d'exploration."""
    best = eb.best_iteration(serie)
    source = eb.iteration_dir(serie, best["iteration"])
    qualifie = tmp_path / "qualifie"
    qualifie.mkdir()
    for name in ("design_params.yaml", "geometry.stl"):
        (qualifie / name).write_bytes((source / name).read_bytes())
    (qualifie / "results.json").write_text(
        json.dumps({"Cd": 0.011, "Cl": 0.9, "Cl_Cd": 81.8, "mesh_ok": True}),
        encoding="utf-8",
    )
    summary = eb.export_best(serie, tmp_path / "sortie", qualified_dir=qualifie,
                             visuals=False)
    assert summary["Cl_Cd"] == pytest.approx(81.8)
    assert (tmp_path / "sortie" / "results_exploration.json").is_file()


def test_cli_export(serie, tmp_path, capsys):
    code = eb.main(["--iterations-dir", str(serie), "--output",
                    str(tmp_path / "sortie"), "--no-visuals", "--no-case"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["output"]


def test_cli_export_serie_vide(tmp_path, capsys):
    vide = tmp_path / "vide"
    vide.mkdir()
    assert eb.main(["--iterations-dir", str(vide), "--no-visuals"]) == 1
    assert "error" in json.loads(capsys.readouterr().err)


# ─────────────────────────────────────────────────────────────
# Comparaison avant / après
# ─────────────────────────────────────────────────────────────


def test_reference_par_defaut_est_la_premiere_iteration(serie):
    baseline, regime = eb.resolve_baseline(serie, mp.history(serie), None, False)
    assert baseline.name == "iter_0000"
    assert regime == "même régime"


def test_reference_explicite(serie, tmp_path):
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "results.json").write_text(json.dumps({"Cd": 0.03, "Cl": 0.25}),
                                      encoding="utf-8")
    baseline, regime = eb.resolve_baseline(serie, mp.history(serie), ref, True)
    assert baseline == ref
    assert regime == "réglage fin"


def test_reference_explicite_sans_resultats(serie, tmp_path):
    vide = tmp_path / "sans"
    vide.mkdir()
    with pytest.raises(eb.ExportError):
        eb.resolve_baseline(serie, mp.history(serie), vide, True)


def test_regime_signale_quand_les_mesures_different(serie):
    # Design requalifié au réglage fin, référence restée en exploration : la
    # comparaison doit le dire, sous peine d'annoncer un gain qui mélange deux
    # maillages.
    _, regime = eb.resolve_baseline(serie, mp.history(serie), None, True)
    assert regime == "exploration"


def test_la_comparaison_utilise_le_meme_regime_des_deux_cotes(serie, tmp_path):
    """Le gain ne doit jamais mélanger un maillage fin et un maillage rapide."""
    best = eb.best_iteration(serie)
    source = eb.iteration_dir(serie, best["iteration"])
    qualifie = tmp_path / "qualifie"
    qualifie.mkdir()
    for name in ("design_params.yaml", "geometry.stl"):
        (qualifie / name).write_bytes((source / name).read_bytes())
    # Chiffres « réglage fin » volontairement très différents.
    (qualifie / "results.json").write_text(
        json.dumps({"Cd": 0.011, "Cl": 0.9, "Cl_Cd": 81.8, "mesh_ok": True}),
        encoding="utf-8",
    )
    summary = eb.export_best(serie, tmp_path / "sortie", qualified_dir=qualifie,
                             visuals=False)
    comparison = summary["comparison"]
    assert comparison["regime"] == "exploration"
    # Le « après » de la comparaison est le chiffre d'EXPLORATION, pas 81.8.
    assert comparison["after"] != pytest.approx(81.8)
    # Mais l'en-tête du rapport garde bien la requalification.
    assert summary["Cl_Cd"] == pytest.approx(81.8)


def test_la_lecture_physique_ne_melange_pas_les_regimes(serie, tmp_path):
    """Non-régression : le rapport a annoncé une traînée en baisse alors qu'au
    même régime elle augmentait de 51 %. La cause : un Cd d'exploration comparé
    à un Cd de réglage fin."""
    best = eb.best_iteration(serie)
    source = eb.iteration_dir(serie, best["iteration"])
    qualifie = tmp_path / "qualifie"
    qualifie.mkdir()
    for name in ("design_params.yaml", "geometry.stl"):
        (qualifie / name).write_bytes((source / name).read_bytes())
    # Au réglage fin, tout est plus bas : comparer ces chiffres à ceux
    # d'exploration ferait croire à un progrès qui n'existe pas.
    (qualifie / "results.json").write_text(
        json.dumps({"Cd": 0.004, "Cl": 0.3, "Cl_Cd": 75.0, "mesh_ok": True}),
        encoding="utf-8",
    )
    eb.export_best(serie, tmp_path / "sortie", qualified_dir=qualifie, visuals=False)
    report = (tmp_path / "sortie" / "README.md").read_text(encoding="utf-8")

    seed = json.loads(
        (eb.iteration_dir(serie, 0) / "results.json").read_text(encoding="utf-8")
    )
    # Le Cd de reglage fin (0.004) ne doit pas servir de terme de comparaison.
    if "compromis chiffré" in report:
        ligne = next(l for l in report.splitlines() if "compromis chiffré" in l)
        attendu = (float(best["Cd"]) - seed["Cd"]) / abs(seed["Cd"]) * 100
        assert f"{attendu:+.0f} %" in ligne


def test_le_rendu_du_seed_necrase_pas_celui_du_design(tmp_path, monkeypatch):
    """Non-régression : ParaView écrit toujours sous les mêmes noms. Renommer
    après coup effaçait les images du design optimisé, et le côte à côte du
    rapport pointait dans le vide."""
    figures = tmp_path / "figures"
    figures.mkdir()
    (figures / "pressure_field.png").write_bytes(b"design optimise")

    def _fake_pvbatch(command, **kwargs):
        # Le script écrit dans le dossier qu'on lui donne (avant-dernier
        # argument avant U_inf et rho).
        target = Path(command[-3])
        target.mkdir(parents=True, exist_ok=True)
        (target / "pressure_field.png").write_bytes(b"seed")

        class Proc:
            returncode = 0
            stdout = stderr = ""

        return Proc()

    monkeypatch.setattr(eb.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(eb.subprocess, "run", _fake_pvbatch)
    monkeypatch.setattr(eb, "PARAVIEW_SCRIPT", tmp_path / "render.py")
    (tmp_path / "render.py").write_text("", encoding="utf-8")
    # Le rendu exige un maillage : on le simule.
    (tmp_path / "case" / "constant" / "polyMesh").mkdir(parents=True)

    images, error = eb.render_paraview(
        tmp_path / "case", tmp_path, 20.0, 1.225, prefix="seed_"
    )
    assert error is None
    assert images == ["seed_pressure_field.png"]
    # Les deux images coexistent, et chacune a son contenu.
    assert (figures / "pressure_field.png").read_bytes() == b"design optimise"
    assert (figures / "seed_pressure_field.png").read_bytes() == b"seed"
    assert not (figures / "_render_tmp").exists()


def test_figures_de_comparaison(serie, tmp_path):
    summary = eb.export_best(serie, tmp_path / "sortie", visuals=False)
    figures = tmp_path / "sortie" / "figures"
    for name in ("comparison_sections.svg", "comparison_overlay.svg",
                 "comparison_performance.svg"):
        assert (figures / name).is_file(), name
    assert summary["comparison"] is not None


def test_le_seed_est_archive_dans_comparison(serie, tmp_path):
    eb.export_best(serie, tmp_path / "sortie", visuals=False)
    comparison = tmp_path / "sortie" / "comparison"
    assert (comparison / "seed_results.json").is_file()
    assert (comparison / "seed_design_params.yaml").is_file()
    assert (comparison / "profile_section.csv").is_file()


def test_le_rapport_contient_la_comparaison(serie, tmp_path):
    eb.export_best(serie, tmp_path / "sortie", visuals=False)
    report = (tmp_path / "sortie" / "README.md").read_text(encoding="utf-8")
    assert "## Avant / après" in report
    assert "| **Portance Cl** |" in report
    assert "même régime CFD" in report


def test_comparaison_desactivable(serie, tmp_path):
    summary = eb.export_best(serie, tmp_path / "sortie", visuals=False,
                             compare=False)
    assert summary["comparison"] is None
    assert not (tmp_path / "sortie" / "comparison").exists()


# ── Les graphiques de comparaison ────────────────────────────


def test_barres_avant_apres():
    svg = plots.comparison_bars([
        {"label": "Traînée", "before": 0.031, "after": 0.026, "better": "lower",
         "format": ".5f"},
    ])
    assert svg.startswith("<svg")
    assert "-16.1 %" in svg
    assert "mieux" in svg


def test_une_trainee_qui_baisse_est_un_gain():
    """Sans la notion de sens, une barre plus courte se lirait comme une perte."""
    svg = plots.comparison_bars([
        {"label": "Cd", "before": 0.031, "after": 0.026, "better": "lower"},
    ])
    assert "#2e7d32" in svg          # vert : amélioration
    assert "moins bien" not in svg


def test_une_trainee_qui_monte_est_une_perte():
    svg = plots.comparison_bars([
        {"label": "Cd", "before": 0.026, "after": 0.031, "better": "lower"},
    ])
    assert "#c1440e" in svg
    assert "moins bien" in svg


def test_une_portance_qui_monte_est_un_gain():
    svg = plots.comparison_bars([
        {"label": "Cl", "before": 0.25, "after": 0.77, "better": "higher"},
    ])
    assert "#2e7d32" in svg
    assert "+208.0 %" in svg


def test_barres_sans_donnees():
    assert "aucune donnée" in plots.comparison_bars(
        [{"label": "x", "before": None, "after": 1.0}]
    )


def test_sections_cote_a_cote_partagent_lechelle(config, tmp_path):
    """Deux profils de cordes différentes doivent se dessiner à la même échelle,
    sinon la figure gomme précisément ce qu'elle doit montrer."""
    petit = eb.write_profile_section(load_yaml(config), tmp_path)
    grand_design = load_yaml(config)
    grand_design["parameters"]["chord"]["value"] = 400.0
    grand = eb.write_profile_section(grand_design, tmp_path)

    svg = plots.airfoil_comparison(
        {**petit, "label": "seed", "caption": ""},
        {**grand, "label": "optimisé", "caption": ""},
    )
    # Les deux contours sont dans le même SVG, tracés avec un unique facteur :
    # celui de droite doit couvrir plus de largeur que celui de gauche.
    chemins = [p.split('"')[0] for p in svg.split('<path d="')[1:]]
    assert len(chemins) == 2

    def largeur(path: str) -> float:
        xs = [float(pt.split(",")[0]) for pt in
              path.replace("M", " ").replace("L", " ").replace("Z", "").split()]
        return max(xs) - min(xs)

    assert largeur(chemins[1]) > largeur(chemins[0]) * 1.25


def test_superposition_des_sections(config, tmp_path):
    section = eb.write_profile_section(load_yaml(config), tmp_path)
    svg = plots.airfoil_overlay(
        {**section, "label": "seed"}, {**section, "label": "optimisé"},
        title="superposition",
    )
    assert svg.count("<path") == 2
    assert "stroke-dasharray" in svg      # le seed est en pointillés


# ── Rendu côte à côte en HTML ────────────────────────────────


def test_html_cote_a_cote(tmp_path):
    figures = tmp_path / "figures"
    figures.mkdir()
    for name in ("a.svg", "b.svg"):
        (figures / name).write_text('<svg xmlns="http://www.w3.org/2000/svg"/>',
                                    encoding="utf-8")
    markdown = (
        "<!-- side-by-side -->\n"
        "![Avant](figures/a.svg)\n"
        "![Après](figures/b.svg)\n"
        "<!-- /side-by-side -->\n"
    )
    html = eb.markdown_to_html(markdown, tmp_path, "t")
    assert '<div class="side-by-side">' in html
    assert html.count("<figure>") == 2
    assert ".side-by-side { display: flex" in html.replace("  ", " ")


def test_les_marqueurs_disparaissent_du_markdown(tmp_path):
    """Un lecteur Markdown ignore ces commentaires : les images s'empilent."""
    html = eb.markdown_to_html("<!-- side-by-side -->\n<!-- /side-by-side -->\n",
                               tmp_path, "t")
    assert "side-by-side -->" not in html


# ─────────────────────────────────────────────────────────────
# Branchement automatique en fin de boucle
# ─────────────────────────────────────────────────────────────


def test_la_boucle_exporte_automatiquement(config, iterations, tmp_path, monkeypatch):
    analytic_cfd(monkeypatch)
    output = tmp_path / "auto"
    summary = loop.run_loop(
        config, REAL_CFD, iterations, max_iterations=3, strategy="local",
        geometry_backend="internal", export_output=output, visuals=False,
    )
    assert "export" in summary
    assert (output / "README.md").is_file()
    assert summary["export"]["iteration"] is not None


def test_export_desactivable(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    summary = loop.run_loop(
        config, REAL_CFD, iterations, max_iterations=2, strategy="local",
        geometry_backend="internal", export_best=False,
    )
    assert "export" not in summary


def test_un_export_qui_echoue_ne_casse_pas_la_boucle(
    config, iterations, monkeypatch
):
    analytic_cfd(monkeypatch)

    def _boom(*args, **kwargs):
        raise RuntimeError("disque plein")

    monkeypatch.setattr(eb, "export_best", _boom)
    summary = loop.run_loop(
        config, REAL_CFD, iterations, max_iterations=2, strategy="local",
        geometry_backend="internal", visuals=False,
    )
    # Les résultats sont archivés : l'export se relance à la main.
    assert summary["successes"] == 2
    assert "disque plein" in summary["export_error"]


def test_cli_export_best_sur_serie_existante(serie, tmp_path, capsys):
    code = loop.main(["--iterations-dir", str(serie), "--export-best",
                      "--export-output", str(tmp_path / "sortie"),
                      "--no-visuals"])
    assert code == 0
    assert (tmp_path / "sortie" / "README.md").is_file()
