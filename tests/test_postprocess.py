"""Tests du post-traitement CFD (Phase 2), sans OpenFOAM.

Les fichiers de coefficients et les journaux checkMesh sont fabriqués ici, dans
les deux conventions rencontrées selon les versions d'OpenFOAM :

  - `postProcessing/forceCoeffs/0/coefficient.dat` (ESI récentes)
  - `postProcessing/forceCoeffs/0/forceCoeffs.dat` (versions plus anciennes)

L'ordre des colonnes diffère entre les deux : c'est précisément pourquoi la
lecture est pilotée par l'en-tête et non par la position.
"""

import json
import math
from pathlib import Path

import pytest

from openfoam import postprocess as pp

ROOT = Path(__file__).resolve().parents[1]
REAL_DESIGN = ROOT / "configs" / "design_params.yaml"
REAL_CFD = ROOT / "configs" / "cfd_settings.yaml"


# ─────────────────────────────────────────────────────────────
# Fabrication de sorties OpenFOAM
# ─────────────────────────────────────────────────────────────


def write_coefficients(
    iteration_dir: Path,
    cd_series,
    cl_series,
    style: str = "esi",
    filename: str | None = None,
) -> Path:
    """Écrit un fichier de coefficients dans la convention demandée."""
    out = iteration_dir / "cfd" / "postProcessing" / "forceCoeffs" / "0"
    out.mkdir(parents=True, exist_ok=True)

    if style == "esi":
        name = filename or "coefficient.dat"
        header = "# Time Cd Cd(f) Cd(r) Cl Cl(f) Cl(r) CmPitch"
        rows = [
            f"{i + 1} {cd} {cd / 2} {cd / 2} {cl} {cl / 2} {cl / 2} 0.01"
            for i, (cd, cl) in enumerate(zip(cd_series, cl_series))
        ]
    else:
        # Ancienne convention : Cm en deuxième colonne, avant Cd et Cl.
        name = filename or "forceCoeffs.dat"
        header = "# Time Cm Cd Cl Cl(f) Cl(r)"
        rows = [
            f"{i + 1} 0.01 {cd} {cl} {cl / 2} {cl / 2}"
            for i, (cd, cl) in enumerate(zip(cd_series, cl_series))
        ]

    path = out / name
    path.write_text(
        "\n".join(["# Force coefficients", header, *rows]) + "\n", encoding="utf-8"
    )
    return path


def write_check_mesh(iteration_dir: Path, ok: bool = True, n_failed: int = 3,
                     cells: int = 254000) -> Path:
    logs = iteration_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    body = f"""
Mesh stats
    points:           123456
    faces:            789012
    cells:            {cells}

Checking geometry...
"""
    body += "\nMesh OK.\n" if ok else f"\n***Failed {n_failed} mesh checks.\n"
    path = logs / "checkMesh.log"
    path.write_text(body, encoding="utf-8")
    return path


def write_solver_log(iteration_dir: Path, converged: bool = True,
                     last_time: int = 830) -> Path:
    logs = iteration_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    lines = [f"Time = {t}\n" for t in range(1, last_time + 1)]
    if converged:
        lines.append("SIMPLE solution converged in 830 iterations\n")
    path = logs / "simpleFoam.log"
    path.write_text("".join(lines), encoding="utf-8")
    return path


def converged_series(target: float, n: int = 400, noise: float = 0.0005):
    """Série qui converge vers `target` avec une oscillation résiduelle."""
    return [
        target * (1 + 0.5 * math.exp(-i / 50.0) + noise * math.sin(i / 3.0))
        for i in range(n)
    ]


@pytest.fixture
def iteration_dir(tmp_path) -> Path:
    d = tmp_path / "iter_0000"
    d.mkdir()
    return d


@pytest.fixture
def run_ok(iteration_dir) -> Path:
    write_coefficients(iteration_dir, converged_series(0.043), converged_series(1.18))
    write_check_mesh(iteration_dir, ok=True)
    write_solver_log(iteration_dir, converged=True)
    return iteration_dir


# ─────────────────────────────────────────────────────────────
# Lecture des coefficients
# ─────────────────────────────────────────────────────────────


def test_lecture_convention_esi(iteration_dir):
    path = write_coefficients(iteration_dir, [0.04] * 10, [1.1] * 10, style="esi")
    columns = pp.parse_coefficient_file(path)
    assert columns["Cd"][0] == pytest.approx(0.04)
    assert columns["Cl"][0] == pytest.approx(1.1)


def test_lecture_ancienne_convention(iteration_dir):
    # Cd et Cl n'y sont pas dans les mêmes colonnes : la lecture par en-tête
    # doit s'en moquer.
    path = write_coefficients(iteration_dir, [0.04] * 10, [1.1] * 10, style="legacy")
    columns = pp.parse_coefficient_file(path)
    assert columns["Cd"][0] == pytest.approx(0.04)
    assert columns["Cl"][0] == pytest.approx(1.1)


def test_les_deux_conventions_donnent_le_meme_resultat(tmp_path):
    cd, cl = converged_series(0.05), converged_series(0.9)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    write_coefficients(a, cd, cl, style="esi")
    write_coefficients(b, cd, cl, style="legacy")
    for d in (a, b):
        write_check_mesh(d, ok=True)
    ra = pp.postprocess(a, REAL_DESIGN, REAL_CFD)
    rb = pp.postprocess(b, REAL_DESIGN, REAL_CFD)
    assert ra["Cd"] == pytest.approx(rb["Cd"])
    assert ra["Cl"] == pytest.approx(rb["Cl"])


def test_en_tete_absent(iteration_dir):
    out = iteration_dir / "cfd" / "postProcessing" / "forceCoeffs" / "0"
    out.mkdir(parents=True)
    (out / "coefficient.dat").write_text("1 0.04 1.1\n2 0.04 1.1\n", encoding="utf-8")
    with pytest.raises(pp.PostProcessError) as exc:
        pp.parse_coefficient_file(out / "coefficient.dat")
    assert exc.value.status == pp.STATUS_COEFFICIENTS_UNREADABLE


def test_colonne_cd_absente(iteration_dir):
    out = iteration_dir / "cfd" / "postProcessing" / "forceCoeffs" / "0"
    out.mkdir(parents=True)
    p = out / "coefficient.dat"
    p.write_text("# Time Cl CmPitch\n1 1.1 0.01\n", encoding="utf-8")
    with pytest.raises(pp.PostProcessError):
        pp.average_coefficients(pp.parse_coefficient_file(p), p, 200, 0.02)


def test_aucun_fichier_de_coefficients(iteration_dir):
    (iteration_dir / "cfd").mkdir()
    with pytest.raises(pp.PostProcessError) as exc:
        pp.find_coefficient_file(iteration_dir / "cfd")
    assert exc.value.status == pp.STATUS_NO_COEFFICIENTS


# ─────────────────────────────────────────────────────────────
# Moyennes et stabilité
# ─────────────────────────────────────────────────────────────


def test_moyenne_sur_la_fenetre(iteration_dir):
    # 100 valeurs à 1.0 puis 100 à 2.0 : une fenêtre de 100 ne doit voir que 2.0.
    path = write_coefficients(iteration_dir, [1.0] * 100 + [2.0] * 100,
                              [1.0] * 100 + [2.0] * 100)
    result = pp.average_coefficients(pp.parse_coefficient_file(path), path, 100, 1.0)
    assert result["Cd"] == pytest.approx(2.0)
    assert result["averaging_window"] == 100
    assert result["samples"] == 200


def test_fenetre_plus_grande_que_la_serie(iteration_dir):
    path = write_coefficients(iteration_dir, [0.04] * 10, [1.1] * 10)
    result = pp.average_coefficients(pp.parse_coefficient_file(path), path, 500, 0.02)
    assert result["averaging_window"] == 10


def test_serie_stable_declaree_stable(iteration_dir):
    path = write_coefficients(iteration_dir, converged_series(0.043),
                              converged_series(1.18))
    result = pp.average_coefficients(pp.parse_coefficient_file(path), path, 200, 0.02)
    assert result["coefficients_stable"] is True
    assert result["Cd"] == pytest.approx(0.043, rel=0.02)
    assert result["Cl"] == pytest.approx(1.18, rel=0.02)


def test_serie_oscillante_declaree_instable(iteration_dir):
    cd = [0.04 * (1 + 0.4 * math.sin(i / 2.0)) for i in range(400)]
    path = write_coefficients(iteration_dir, cd, [1.1] * 400)
    result = pp.average_coefficients(pp.parse_coefficient_file(path), path, 200, 0.02)
    assert result["coefficients_stable"] is False
    assert result["Cd_rel_std"] > 0.02


def test_rapport_cl_cd(iteration_dir):
    path = write_coefficients(iteration_dir, [0.05] * 50, [1.0] * 50)
    result = pp.average_coefficients(pp.parse_coefficient_file(path), path, 50, 1.0)
    assert result["Cl_Cd"] == pytest.approx(20.0)


def test_cd_nul_ne_divise_pas_par_zero(iteration_dir):
    path = write_coefficients(iteration_dir, [0.0] * 50, [1.0] * 50)
    result = pp.average_coefficients(pp.parse_coefficient_file(path), path, 50, 1.0)
    assert result["Cl_Cd"] is None


def test_divergence_detectee(iteration_dir):
    path = write_coefficients(iteration_dir, [float("nan")] * 50, [1.0] * 50)
    with pytest.raises(pp.PostProcessError) as exc:
        pp.average_coefficients(pp.parse_coefficient_file(path), path, 50, 0.02)
    assert exc.value.status == pp.STATUS_NOT_FINITE


def test_divergence_infinie_detectee(iteration_dir):
    path = write_coefficients(iteration_dir, [float("inf")] * 50, [1.0] * 50)
    with pytest.raises(pp.PostProcessError):
        pp.average_coefficients(pp.parse_coefficient_file(path), path, 50, 0.02)


# ─────────────────────────────────────────────────────────────
# checkMesh
# ─────────────────────────────────────────────────────────────


def test_checkmesh_ok(iteration_dir):
    write_check_mesh(iteration_dir, ok=True)
    mesh = pp.read_check_mesh(iteration_dir / "logs" / "checkMesh.log")
    assert mesh["mesh_ok"] is True
    assert mesh["n_cells"] == 254000


def test_checkmesh_en_echec(iteration_dir):
    write_check_mesh(iteration_dir, ok=False, n_failed=4)
    mesh = pp.read_check_mesh(iteration_dir / "logs" / "checkMesh.log")
    assert mesh["mesh_ok"] is False
    assert "4" in mesh["mesh_message"]


def test_checkmesh_non_execute(iteration_dir):
    mesh = pp.read_check_mesh(iteration_dir / "logs" / "checkMesh.log")
    assert mesh["mesh_ok"] is False and mesh["mesh_checked"] is False


def test_convergence_du_solveur(iteration_dir):
    write_solver_log(iteration_dir, converged=True, last_time=830)
    info = pp.read_solver_convergence(iteration_dir / "logs", "simpleFoam")
    assert info["solver_converged"] is True
    assert info["solver_iterations"] == 830


def test_solveur_non_converge(iteration_dir):
    write_solver_log(iteration_dir, converged=False, last_time=2000)
    info = pp.read_solver_convergence(iteration_dir / "logs", "simpleFoam")
    assert info["solver_converged"] is False
    assert info["solver_iterations"] == 2000


# ─────────────────────────────────────────────────────────────
# results.json
# ─────────────────────────────────────────────────────────────

CONTRACT_KEYS = ("iteration", "success", "Cd", "Cl", "Cl_Cd", "mesh_ok",
                 "error_message")


def test_resultat_complet(run_ok):
    result = pp.postprocess(run_ok, REAL_DESIGN, REAL_CFD)
    assert result["success"] is True
    assert result["status"] == pp.STATUS_OK
    assert result["iteration"] == 0
    assert result["design_id"] == "wing_v01"
    assert result["mesh_ok"] is True
    assert result["converged"] is True
    assert result["Cd"] == pytest.approx(0.043, rel=0.02)
    assert result["Cl"] == pytest.approx(1.18, rel=0.02)
    assert result["Cl_Cd"] == pytest.approx(1.18 / 0.043, rel=0.03)
    assert result["error_message"] is None


@pytest.mark.parametrize("key", CONTRACT_KEYS)
def test_cles_du_contrat_presentes_en_succes(run_ok, key):
    assert key in pp.postprocess(run_ok, REAL_DESIGN, REAL_CFD)


@pytest.mark.parametrize("key", CONTRACT_KEYS)
def test_cles_du_contrat_presentes_en_echec(iteration_dir, key):
    assert key in pp.build_failure(iteration_dir, "MESH_CHECK_FAILED", "boum",
                                   REAL_DESIGN)


def test_echec_ne_fabrique_aucun_coefficient(iteration_dir):
    # Un 0.0 se propagerait dans la boucle comme une mesure légitime.
    result = pp.build_failure(iteration_dir, "SOLVER_FAILED", "diverge", REAL_DESIGN)
    assert result["Cd"] is None and result["Cl"] is None and result["Cl_Cd"] is None
    assert result["success"] is False
    assert result["error_message"] == "diverge"


def test_maillage_invalide_rend_le_resultat_non_converge(iteration_dir):
    write_coefficients(iteration_dir, converged_series(0.043), converged_series(1.18))
    write_check_mesh(iteration_dir, ok=False)
    result = pp.postprocess(iteration_dir, REAL_DESIGN, REAL_CFD)
    assert result["success"] is True      # des coefficients ont bien été lus
    assert result["mesh_ok"] is False
    assert result["converged"] is False   # mais ils ne sont pas fiables
    assert "warning" in result


def test_resultat_serialisable(run_ok, tmp_path):
    result = pp.postprocess(run_ok, REAL_DESIGN, REAL_CFD)
    path = pp.write_results(run_ok, result)
    assert json.loads(path.read_text(encoding="utf-8"))["Cd"] == pytest.approx(
        result["Cd"]
    )


def test_le_fichier_le_plus_recent_est_choisi(iteration_dir):
    import os
    import time

    old = write_coefficients(iteration_dir, [0.09] * 50, [0.5] * 50,
                             filename="coefficient.dat")
    time.sleep(0.01)
    new_dir = iteration_dir / "cfd" / "postProcessing" / "forceCoeffs" / "500"
    new_dir.mkdir(parents=True)
    new = new_dir / "coefficient.dat"
    new.write_text(
        "# Time Cd Cl\n" + "\n".join(f"{i} 0.04 1.2" for i in range(50)),
        encoding="utf-8",
    )
    os.utime(old, (time.time() - 100, time.time() - 100))
    assert pp.find_coefficient_file(iteration_dir / "cfd") == new


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def test_cli_ecrit_results_json(run_ok, capsys):
    code = pp.main(["--iteration-dir", str(run_ok),
                    "--design-params", str(REAL_DESIGN),
                    "--cfd-settings", str(REAL_CFD)])
    assert code == 0
    results = json.loads((run_ok / "results.json").read_text(encoding="utf-8"))
    assert results["success"] is True and results["Cd"] > 0


def test_cli_mode_echec(iteration_dir, capsys):
    code = pp.main(["--iteration-dir", str(iteration_dir),
                    "--design-params", str(REAL_DESIGN),
                    "--failure-status", "MESH_CHECK_FAILED",
                    "--failure-message", "checkMesh : 4 controles en echec"])
    assert code == 0
    results = json.loads((iteration_dir / "results.json").read_text(encoding="utf-8"))
    assert results["success"] is False
    assert results["status"] == "MESH_CHECK_FAILED"
    assert results["Cd"] is None


def test_cli_ecrit_results_json_meme_sans_coefficients(iteration_dir, capsys):
    # Le pipeline ne lit que results.json : un run qui s'arrête sans en laisser
    # rendrait l'itération illisible en aval.
    code = pp.main(["--iteration-dir", str(iteration_dir),
                    "--design-params", str(REAL_DESIGN)])
    assert code == 1
    results = json.loads((iteration_dir / "results.json").read_text(encoding="utf-8"))
    assert results["status"] == pp.STATUS_NO_COEFFICIENTS
    assert results["success"] is False
