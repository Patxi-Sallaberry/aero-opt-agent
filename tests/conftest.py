"""Configuration commune des tests.

Deux garanties :
  - la racine du dépôt est importable (`import pipeline.utils`) ;
  - aucun test n'écrit dans l'arborescence du dépôt.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _results_hors_du_depot(tmp_path, monkeypatch):
    """Redirige le dossier de livraison par défaut vers un dossier temporaire.

    La boucle exporte le meilleur design en fin de série, et sa destination par
    défaut est `results/` à la racine. Sans cette redirection, chaque test qui
    fait tourner une boucle laisse un dossier horodaté dans le dépôt — sept
    s'y étaient accumulés, avec 147 fichiers suivis par git.
    """
    from scripts import export_best

    monkeypatch.setattr(export_best, "RESULTS_ROOT", tmp_path / "results")
