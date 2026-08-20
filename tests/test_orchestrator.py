"""Tests de l'orchestrateur et de la boucle (Phase 4).

La CFD est remplacée par un modèle analytique : chaque « simulation » calcule un
Cl et un Cd depuis les paramètres, avec un optimum connu d'avance. On peut donc
vérifier ce qu'aucune exécution réelle ne permettrait — que la recherche
CONVERGE vers cet optimum, en un nombre raisonnable d'itérations.

Le client Claude est également doublé : les tests portent sur la construction du
contexte, la lecture de la réponse, le refus d'une proposition hors bornes et le
repli automatique, pas sur le modèle lui-même.
"""

import copy
import json
import math
from pathlib import Path

import pytest
import yaml

from agent import orchestrator as orch
from agent.orchestrator import ProposalError
from pipeline import master_pipeline as mp
from pipeline.utils import load_yaml, validate_design_params
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


# ─────────────────────────────────────────────────────────────
# CFD analytique : un optimum connu, donc vérifiable
# ─────────────────────────────────────────────────────────────

OPTIMUM = {"aoa": 5.0, "camber": 0.05, "thickness": 0.10, "chord": 300.0}


def analytic_cfd(monkeypatch, noise: float = 0.0, fail_when=None):
    """Remplace run_cfd par un modèle dont l'optimum est connu.

    Cl croît avec l'incidence puis décroche ; Cd croît avec l'épaisseur et le
    carré de l'incidence. Cl/Cd présente donc un maximum net, atteint pour
    `OPTIMUM`.
    """
    def _run(iteration_dir, config_path, cfd_settings_path, timeout_s=None):
        design = load_yaml(config_path)
        p = {n: float(s["value"]) for n, s in design["parameters"].items()}

        if fail_when is not None and fail_when(p):
            results = {
                "iteration": design["iteration"], "success": False,
                "status": "MESH_CHECK_FAILED", "Cd": None, "Cl": None,
                "Cl_Cd": None, "mesh_ok": False,
                "error_message": "maillage invalide",
            }
            Path(iteration_dir, "results.json").write_text(json.dumps(results))
            return False, "maillage invalide"

        aoa, camber = p["aoa"], p["camber"]
        thickness = p["thickness"]
        cl = 0.11 * (aoa - (aoa - OPTIMUM["aoa"]) ** 2 * 0.06) + 12.0 * camber + 0.1
        cd = (
            0.008
            + 0.35 * thickness
            + 0.0006 * aoa ** 2
            + 0.9 * (camber - OPTIMUM["camber"]) ** 2
        )
        if noise:
            cd *= 1 + noise * math.sin(design["iteration"] * 7.3)

        results = {
            "iteration": design["iteration"], "success": True, "status": "OK",
            "Cd": cd, "Cl": cl, "Cl_Cd": cl / cd, "mesh_ok": True,
            "converged": True, "error_message": None,
        }
        Path(iteration_dir, "results.json").write_text(json.dumps(results))
        return True, "CFD terminée"

    monkeypatch.setattr(mp, "run_cfd", _run)


def evaluate(config: Path, iterations: Path) -> dict:
    return mp.run_iteration(config, REAL_CFD, iterations, geometry_backend="internal")


def set_values(config: Path, iteration=None, **values):
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    if iteration is not None:
        data["iteration"] = iteration
    for name, value in values.items():
        data["parameters"][name]["value"] = value
    config.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ─────────────────────────────────────────────────────────────
# Paramètres manœuvrables
# ─────────────────────────────────────────────────────────────


def test_span_est_reconnu_comme_fige(config):
    free = orch.free_parameters(load_yaml(config)["parameters"])
    assert "span" not in free
    assert set(free) == {"chord", "thickness", "camber", "aoa"}


def test_un_parametre_aux_bornes_larges_est_libre(config):
    data = load_yaml(config)
    assert "chord" in orch.free_parameters(data["parameters"])


# ─────────────────────────────────────────────────────────────
# Recherche locale
# ─────────────────────────────────────────────────────────────


def test_exploration_prudente_sans_evaluation(config, iterations):
    # Sans point mesuré — départ, ou série d'échecs — la boucle doit pouvoir
    # continuer : s'arrêter là la priverait de toute chance de se rattraper.
    values, reason = orch.propose_local(load_yaml(config), [], iterations)
    assert values
    assert "aucune itération réussie" in reason


def test_bornes_serrees_valent_parametre_fige():
    serre = {"value": 80.0, "min": 79.0, "max": 81.0, "max_delta_pct": 1.0,
             "unit": "mm"}
    large = {"value": 300.0, "min": 220.0, "max": 420.0, "max_delta_pct": 7.0,
             "unit": "mm"}
    assert orch.free_parameters({"span": serre, "chord": large}) == ["chord"]


def test_sans_parametre_manoeuvrable(config, iterations):
    design = load_yaml(config)
    for spec in design["parameters"].values():
        spec["min"] = spec["value"] * 0.999 - 1e-6
        spec["max"] = spec["value"] * 1.001 + 1e-6
    design["parameters"].pop("aoa")   # valeur nulle : cas non décidable
    with pytest.raises(ProposalError) as exc:
        orch.propose_local(design, [], iterations)
    assert "manœuvrable" in str(exc.value)


def test_proposition_valide(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    result = orch.propose(config, iterations, strategy="local")
    assert result["strategy"] == "local"
    assert result["iteration"] == 1
    assert result["changed"]


def test_la_proposition_respecte_le_budget(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    before = load_yaml(config)
    orch.propose(config, iterations, strategy="local")
    after = load_yaml(config)
    report = validate_design_params(after, previous=before)
    assert report.ok, report.format()


def test_la_proposition_est_ecrite(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    orch.propose(config, iterations, strategy="local")
    assert load_yaml(config)["iteration"] == 1


def test_dry_run_necrit_rien(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    avant = config.read_text(encoding="utf-8")
    orch.propose(config, iterations, strategy="local", write=False)
    assert config.read_text(encoding="utf-8") == avant


def test_la_recherche_repart_du_meilleur_point(config, iterations, monkeypatch):
    # Une itération médiocre ne doit pas devenir la nouvelle base.
    analytic_cfd(monkeypatch)
    set_values(config, iteration=0, aoa=4.0)
    evaluate(config, iterations)
    meilleur = load_yaml(config)["parameters"]["aoa"]["value"]

    set_values(config, iteration=1, aoa=1.0)   # nettement moins bon
    evaluate(config, iterations)

    result = orch.propose(config, iterations, strategy="local", write=False)
    # Le budget se mesure depuis la dernière itération réussie (1.0). Les
    # bornes de `aoa` encadrent zéro, donc le budget vaut 12 % de l'amplitude
    # (14 deg), soit 1.68 : on peut remonter jusqu'à 2.68, donc atteindre 4.0
    # reste hors de portée en une itération.
    assert 1.0 < result["values"]["aoa"] <= 2.68
    assert "retour progressif" in result["rationale"]


def test_le_pas_se_resserre_apres_un_echec(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    large = orch.propose(config, iterations, strategy="local", write=False)

    # On archive un échec après le meilleur point.
    failed = iterations / "iter_0001"
    failed.mkdir()
    (failed / "iteration.json").write_text(json.dumps({
        "iteration": 1, "success": False, "status": "MESH_CHECK_FAILED",
        "stage": "cfd", "error_message": "maillage invalide",
    }), encoding="utf-8")

    set_values(config, iteration=1)
    petit = orch.propose(config, iterations, strategy="local", write=False)

    name = next(iter(large["changed"]))
    base = load_yaml(config)["parameters"][name]["value"]
    assert abs(petit["values"][name] - base) <= abs(large["values"][name] - base)


def test_tous_les_parametres_sont_sondes(config, iterations, monkeypatch):
    """Sur une série sans progrès, la recherche doit balayer chaque paramètre."""
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)

    touches = set()
    for i in range(1, 6):
        result = orch.propose(config, iterations, strategy="local", write=False)
        touches.update(result["changed"])
        # On archive un résultat identique (aucun gain) pour forcer la rotation.
        directory = iterations / f"iter_{i:04d}"
        directory.mkdir(exist_ok=True)
        (directory / "iteration.json").write_text(json.dumps({
            "iteration": i, "success": True, "status": "OK", "objective": 1.0,
        }), encoding="utf-8")
        (directory / "design_params.yaml").write_text(
            yaml.safe_dump(result["config"]), encoding="utf-8"
        )
        set_values(config, iteration=i)
    assert len(touches) >= 3


# ─────────────────────────────────────────────────────────────
# Convergence — le vrai test de la stratégie
# ─────────────────────────────────────────────────────────────


def test_la_recherche_locale_converge(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    summary = loop.run_loop(
        config, REAL_CFD, iterations, max_iterations=30, strategy="local",
        geometry_backend="internal", stagnation_patience=12,
    )
    assert summary["successes"] >= 20
    assert summary["best_objective"] is not None

    premier = summary["history"][0]["objective"]
    assert summary["best_objective"] > premier
    assert summary["improvement_pct"] > 15.0


def test_la_recherche_approche_loptimum(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    summary = loop.run_loop(
        config, REAL_CFD, iterations, max_iterations=30, strategy="local",
        geometry_backend="internal", stagnation_patience=12,
    )
    best = orch.parameters_of({"iteration": summary["best_iteration"]}, iterations)
    valeurs = {n: float(s["value"]) for n, s in best.items()}

    # Le budget max_delta_pct limite chaque pas : on vérifie la DIRECTION du
    # déplacement, pas l'arrivée exacte à l'optimum. La recherche suit d'abord
    # la cambrure, qui paye le plus, puis l'incidence — c'est le comportement
    # attendu d'une descente gloutonne.
    assert valeurs["camber"] > 0.03, "la cambrure doit progresser"
    assert valeurs["aoa"] > 1.5, "l'incidence doit progresser vers 5 deg"
    assert summary["improvement_pct"] > 100.0


def test_un_point_deja_evalue_nest_jamais_repropose(config, iterations, monkeypatch):
    """Non-régression : la boucle réelle s'est arrêtée sur ce défaut.

    Après une sonde infructueuse, la stratégie reproposait exactement le point
    déjà simulé, et la boucle s'arrêtait sur « la cible coïncide avec
    l'itération précédente » — budget gaspillé.
    """
    analytic_cfd(monkeypatch)
    proposes = []
    for i in range(10):
        record = evaluate(config, iterations)
        assert record["success"], record["error_message"]
        proposes.append(
            {n: round(v, 6) for n, v in
             orch.propose(config, iterations, strategy="local")["values"].items()}
        )
    # Chaque proposition doit être un point neuf.
    empreintes = [tuple(sorted(p.items())) for p in proposes]
    assert len(set(empreintes)) == len(empreintes)


def test_lordre_de_sondage_suit_la_sensibilite(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    loop.run_loop(config, REAL_CFD, iterations, max_iterations=10,
                  strategy="local", geometry_backend="internal",
                  stagnation_patience=10)
    points = orch.evaluated_points(mp.history(iterations), iterations)
    parametres = load_yaml(config)["parameters"]
    free = ["chord", "thickness", "camber", "aoa"]
    effets = orch._sensitivities(points, parametres, free)
    ordre = orch._probe_order(free, points, parametres)

    # Les paramètres déjà mesurés sont classés par effet décroissant.
    mesures = [name for name in ordre if name in effets]
    assert mesures == sorted(mesures, key=lambda n: effets[n], reverse=True)
    # Et ceux jamais essayés passent devant, pour être mesurés.
    inconnus = [name for name in ordre if name not in effets]
    assert ordre[:len(inconnus)] == inconnus


def test_les_parametres_sans_effet_sont_abandonnes(config):
    # Trois points : `chord` bouge sans rien changer, `aoa` bouge et l'objectif
    # suit. Le premier ne mérite plus qu'on dépense une CFD dessus.
    parametres = load_yaml(config)["parameters"]
    base = {n: float(s["value"]) for n, s in parametres.items()}
    points = [
        {"iteration": 0, "objective": 10.0, "values": dict(base)},
        {"iteration": 1, "objective": 10.0,
         "values": {**base, "chord": 315.0}},
        {"iteration": 2, "objective": 10.0,
         "values": {**base, "chord": 285.0}},
        {"iteration": 3, "objective": 14.0,
         "values": {**base, "aoa": 1.5}},
        {"iteration": 4, "objective": 17.0,
         "values": {**base, "aoa": 3.0}},
    ]
    inertes = orch._inert_parameters(
        points, parametres, ["chord", "thickness", "camber", "aoa"]
    )
    assert "chord" in inertes
    assert "aoa" not in inertes


def test_un_parametre_sonde_une_seule_fois_reste_en_jeu(config):
    # Une seule observation ne suffit pas à condamner un paramètre.
    parametres = load_yaml(config)["parameters"]
    base = {n: float(s["value"]) for n, s in parametres.items()}
    points = [
        {"iteration": 0, "objective": 10.0, "values": dict(base)},
        {"iteration": 1, "objective": 10.0, "values": {**base, "chord": 315.0}},
    ]
    assert orch._inert_parameters(points, parametres, ["chord"]) == set()


def test_la_boucle_survit_aux_echecs(config, iterations, monkeypatch):
    # Toute cambrure au dessus de 0.03 fait « échouer le maillage » — alors que
    # l'optimum, lui, est à 0.05 : la recherche va donc buter dessus.
    analytic_cfd(monkeypatch, fail_when=lambda p: p["camber"] > 0.03)
    summary = loop.run_loop(
        config, REAL_CFD, iterations, max_iterations=20, strategy="local",
        geometry_backend="internal", max_consecutive_failures=4,
        stagnation_patience=20,
    )
    assert summary["failures"] > 0
    assert summary["successes"] > 0
    # La boucle ne s'arrête pas au premier échec.
    assert summary["iterations_run"] > 3


def test_les_echecs_consecutifs_arretent_la_boucle(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch, fail_when=lambda p: True)
    summary = loop.run_loop(
        config, REAL_CFD, iterations, max_iterations=20, strategy="local",
        geometry_backend="internal", max_consecutive_failures=3,
    )
    assert summary["stop_reason"] == loop.STOP_CONSECUTIVE_FAILURES
    assert summary["iterations_run"] == 3


def test_larret_sur_stagnation(config, iterations, monkeypatch):
    # Un modèle plat : aucun réglage ne change rien.
    def _flat(iteration_dir, config_path, cfd_settings_path, timeout_s=None):
        design = load_yaml(config_path)
        results = {"iteration": design["iteration"], "success": True, "status": "OK",
                   "Cd": 0.02, "Cl": 1.0, "Cl_Cd": 50.0, "mesh_ok": True,
                   "converged": True, "error_message": None}
        Path(iteration_dir, "results.json").write_text(json.dumps(results))
        return True, "ok"

    monkeypatch.setattr(mp, "run_cfd", _flat)
    summary = loop.run_loop(
        config, REAL_CFD, iterations, max_iterations=30, strategy="local",
        geometry_backend="internal", stagnation_patience=4,
    )
    assert summary["stop_reason"] == loop.STOP_STAGNATION
    assert summary["iterations_run"] < 30


def test_bilan_ecrit_sur_disque(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    loop.run_loop(config, REAL_CFD, iterations, max_iterations=4, strategy="local",
                  geometry_backend="internal")
    summary = json.loads(
        (iterations / loop.SUMMARY_FILENAME).read_text(encoding="utf-8")
    )
    assert summary["iterations_run"] == 4
    assert len(summary["history"]) == 4


def test_toutes_les_iterations_restent_valides(config, iterations, monkeypatch):
    """Aucune itération de la série ne doit violer le contrat."""
    analytic_cfd(monkeypatch)
    loop.run_loop(config, REAL_CFD, iterations, max_iterations=12, strategy="local",
                  geometry_backend="internal", stagnation_patience=12)

    archives = sorted(iterations.glob("iter_*/design_params.yaml"))
    assert len(archives) >= 10
    precedent = None
    for archive in archives:
        data = load_yaml(archive)
        report = validate_design_params(data, previous=precedent)
        assert report.ok, f"{archive.name} : {report.format()}"
        precedent = data


# ─────────────────────────────────────────────────────────────
# Stratégie LLM (client doublé)
# ─────────────────────────────────────────────────────────────


class FakeBlock:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.content = [FakeBlock(text)]


class FakeClient:
    """Client Claude doublé : rend des réponses préparées, garde les appels."""

    def __init__(self, replies) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return FakeResponse(self.replies.pop(0) if self.replies else "{}")


def reply(values: dict, reasoning: str = "essai") -> str:
    return json.dumps({
        "reasoning": reasoning, "parameters": values,
        "expected_effect": "meilleur Cl/Cd", "confidence": "medium",
    })


def test_llm_proposition_acceptee(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    client = FakeClient([reply({"aoa": 1.2})])
    result = orch.propose(config, iterations, strategy="llm", client=client)
    assert result["strategy"] == "llm"
    assert result["values"]["aoa"] == pytest.approx(1.2)
    assert "meilleur Cl/Cd" in result["rationale"]


def test_le_contexte_contient_lessentiel(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    client = FakeClient([reply({"aoa": 1.0})])
    orch.propose(config, iterations, strategy="llm", client=client, write=False)

    envoye = json.loads(
        client.calls[0]["messages"][0]["content"].split("\n\n", 1)[1].rsplit("\n\n", 1)[0]
    )
    assert envoye["objective"] == "maximize_Cl_Cd"
    assert "allowed_range" in envoye["parameters"]["aoa"]
    assert envoye["parameters"]["span"]["frozen"] is True
    assert envoye["history"][0]["Cl_Cd"] is not None


def test_le_prompt_systeme_est_transmis(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    client = FakeClient([reply({"aoa": 1.0})])
    orch.propose(config, iterations, strategy="llm", client=client, write=False)
    assert "max_delta_pct" in client.calls[0]["system"]


def test_proposition_hors_bornes_refusee_puis_corrigee(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    client = FakeClient([reply({"chord": 9999.0}), reply({"chord": 315.0})])
    result = orch.propose(config, iterations, strategy="llm", client=client)
    assert result["values"]["chord"] == pytest.approx(315.0)
    # L'erreur de validation lui a bien été renvoyée.
    assert "refusée" in client.calls[1]["messages"][-1]["content"]


def test_proposition_hors_budget_refusee(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    # 300 -> 400 dépasse largement les 7 % autorisés.
    client = FakeClient([reply({"chord": 400.0}), reply({"chord": 318.0})])
    result = orch.propose(config, iterations, strategy="llm", client=client)
    assert result["values"]["chord"] == pytest.approx(318.0)


def test_lagent_ne_peut_pas_desserrer_les_bornes(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    client = FakeClient([reply({"aoa": 1.0})])
    orch.propose(config, iterations, strategy="llm", client=client)
    apres = load_yaml(config)["parameters"]["aoa"]
    assert apres["min"] == -2.0 and apres["max"] == 12.0
    assert apres["max_delta_pct"] == 12.0


def test_reponse_illisible_puis_correcte(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    client = FakeClient(["je pense qu'il faudrait augmenter l'incidence",
                         reply({"aoa": 1.0})])
    result = orch.propose(config, iterations, strategy="llm", client=client)
    assert result["values"]["aoa"] == pytest.approx(1.0)


def test_json_entoure_de_texte_est_recupere(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    client = FakeClient(["Voici ma proposition :\n```json\n"
                         + reply({"aoa": 1.0}) + "\n```\nQu'en penses-tu ?"])
    result = orch.propose(config, iterations, strategy="llm", client=client)
    assert result["values"]["aoa"] == pytest.approx(1.0)


def test_trois_echecs_epuisent_lagent(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    client = FakeClient([reply({"chord": 9999.0})] * 3)
    with pytest.raises(ProposalError):
        orch.propose(config, iterations, strategy="llm", client=client)


def test_parametre_inconnu_refuse(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    client = FakeClient([reply({"winglet": 3.0})] * 3)
    with pytest.raises(ProposalError):
        orch.propose(config, iterations, strategy="llm", client=client)


# ─────────────────────────────────────────────────────────────
# Repli automatique
# ─────────────────────────────────────────────────────────────


def test_repli_sans_cle_api(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    evaluate(config, iterations)
    result = orch.propose(config, iterations, strategy="auto")
    assert result["strategy"] == "local"
    assert any("repli" in note for note in result["notes"])


def test_repli_si_lagent_echoue(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)

    class BrokenClient(FakeClient):
        def create(self, **kwargs):
            raise RuntimeError("502 Bad Gateway")

    result = orch.propose(config, iterations, strategy="auto",
                          client=BrokenClient([]))
    assert result["strategy"] == "local"
    assert any("502" in note for note in result["notes"])


def test_strategie_llm_explicite_ne_replie_pas(config, iterations, monkeypatch):
    analytic_cfd(monkeypatch)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    evaluate(config, iterations)
    with pytest.raises(ProposalError):
        orch.propose(config, iterations, strategy="llm")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def test_cli_orchestrateur(config, iterations, monkeypatch, capsys):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    code = orch.main(["--config", str(config), "--iterations-dir", str(iterations),
                      "--strategy", "local", "--dry-run"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["strategy"] == "local"


def test_cli_orchestrateur_explique(config, iterations, monkeypatch, capsys):
    analytic_cfd(monkeypatch)
    evaluate(config, iterations)
    orch.main(["--config", str(config), "--iterations-dir", str(iterations),
               "--strategy", "local", "--dry-run", "--explain"])
    sortie = capsys.readouterr().out
    assert "stratégie : local" in sortie
    assert "recherche par motif" in sortie


def test_cli_orchestrateur_sans_historique(config, iterations, capsys):
    # Sans historique, l'orchestrateur explore au lieu d'abandonner.
    code = orch.main(["--config", str(config), "--iterations-dir", str(iterations),
                      "--strategy", "local", "--dry-run"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["changed"]


def test_cli_boucle(config, iterations, monkeypatch, capsys):
    analytic_cfd(monkeypatch)
    code = loop.main(["--config", str(config), "--cfd-settings", str(REAL_CFD),
                      "--iterations-dir", str(iterations), "--max-iterations", "3",
                      "--strategy", "local", "--geometry-backend", "internal"])
    assert code == 0
    assert "Itérations" in capsys.readouterr().err
