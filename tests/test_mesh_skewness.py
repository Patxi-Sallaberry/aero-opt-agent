"""Phase 5 — tolérer un défaut de maillage local sans ouvrir la porte en grand.

Un Clark Y a un bord de fuite épaissi de 0,0012 corde. Aucun des deux
préréglages CFD ne place assez de cellules en travers d'un intervalle aussi
mince, et snappyHexMesh y écrase quelques cellules : trois faces gauchies sur
258 814, soit un millième de pour cent. Refuser le maillage entier pour cela
arrêtait la boucle dès l'itération zéro — alors que le solveur converge sans
peine, et qu'OpenFOAM lui-même écrit que ces faces « PEUVENT » dégrader le
résultat.

Le risque de la correction est évident : un seuil qu'on assouplit une fois
finit par tout laisser passer. Ces cas fixent donc les deux conditions qui
doivent rester nécessaires — le défaut doit être à la fois RARE et CONTENU —
et vérifient qu'un maillage réellement mauvais est toujours refusé.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openfoam.postprocess import (
    SKEW_HARD_CEILING,
    SKEW_TOLERATED_COUNT,
    SKEW_TOLERATED_FRACTION,
    read_check_mesh,
)

LIMITS = {"max_non_orthogonality": 75.0, "max_skewness": 4.0}


def journal(
    tmp_path: Path,
    skewness: float = 4.24,
    skew_faces: int | None = 3,
    faces: int = 258814,
    cells: int = 81510,
    non_ortho: float = 72.9,
    extra_failures: str = "",
    failed_checks: int | None = 1,
) -> Path:
    """Fabrique un journal checkMesh crédible."""
    lines = [
        "Mesh stats",
        f"    cells:            {cells}",
        f"    faces:            {faces}",
        "Checking geometry...",
        f"    Mesh non-orthogonality Max: {non_ortho} average: 8.2",
    ]
    if skew_faces is None:
        lines.append(f"    Max skewness = {skewness}, OK.")
    else:
        lines.append(
            f" ***Max skewness = {skewness}, {skew_faces} highly skew faces "
            f"detected which may impair the quality of the results"
        )
        lines.append("  <<Writing 3 skew faces to set skewFaces")
    lines.append("    Max aspect ratio = 33.5 OK.")
    if extra_failures:
        lines.append(extra_failures)
    if failed_checks is None:
        lines.append("Mesh OK.")
    else:
        lines.append(f"Failed {failed_checks} mesh checks.")

    path = tmp_path / "checkMesh.log"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Ce qui est toléré
# ─────────────────────────────────────────────────────────────────────────────


def test_trois_faces_gauchies_sur_deux_cent_mille_sont_tolerees(tmp_path):
    """Le cas réel : le bord de fuite épaissi du Clark Y."""
    info = read_check_mesh(journal(tmp_path), LIMITS)
    assert info["mesh_ok"]
    assert info["skewness_tolerated"]


def test_le_defaut_tolere_est_dit_explicitement(tmp_path):
    """Passer un contrôle en échec sous silence serait pire que le refuser."""
    info = read_check_mesh(journal(tmp_path), LIMITS)
    assert info["mesh_warnings"]
    assert "toléré" in info["mesh_message"]
    assert "3 face(s) gauchie(s)" in info["mesh_message"]


def test_le_nombre_de_faces_gauchies_est_rapporte(tmp_path):
    info = read_check_mesh(journal(tmp_path, skew_faces=7), LIMITS)
    assert info["n_skew_faces"] == 7
    assert info["n_faces"] == 258814


# ─────────────────────────────────────────────────────────────────────────────
# Ce qui ne l'est pas
# ─────────────────────────────────────────────────────────────────────────────


def test_un_defaut_etendu_est_refuse(tmp_path):
    """Modéré mais partout : c'est un maillage franchement mauvais."""
    info = read_check_mesh(
        journal(tmp_path, skewness=4.2, skew_faces=5000), LIMITS
    )
    assert not info["mesh_ok"]
    assert not info["skewness_tolerated"]


def test_un_defaut_rare_mais_violent_est_refuse(tmp_path):
    """Trois faces à 30 de skewness : il y a une cellule retournée quelque part."""
    info = read_check_mesh(
        journal(tmp_path, skewness=30.0, skew_faces=3), LIMITS
    )
    assert not info["mesh_ok"]
    assert not info["skewness_tolerated"]


def test_le_plafond_dur_ne_se_negocie_pas(tmp_path):
    """Même une seule face au delà du plafond fait refuser."""
    info = read_check_mesh(
        journal(tmp_path, skewness=SKEW_HARD_CEILING + 0.1, skew_faces=1),
        LIMITS,
    )
    assert not info["mesh_ok"]


def test_la_tolerance_ne_couvre_pas_les_autres_controles(tmp_path):
    """Une cellule à volume négatif reste rédhibitoire, skewness ou pas."""
    info = read_check_mesh(
        journal(tmp_path, extra_failures=" ***Zero or negative cell volume "
                                         "detected.  Minimum negative volume: -1e-18"),
        LIMITS,
    )
    assert not info["mesh_ok"]
    assert any("contrôle(s) en échec" in p for p in info["mesh_problems"])


def test_la_non_orthogonalite_reste_jugee_normalement(tmp_path):
    """La tolérance porte sur la skewness seule, pas sur la qualité en général."""
    info = read_check_mesh(
        journal(tmp_path, non_ortho=88.0, skew_faces=None, failed_checks=None),
        LIMITS,
    )
    assert not info["mesh_ok"]
    assert any("non-orthogonalité" in p for p in info["mesh_problems"])


def test_sans_l_etendue_on_refuse(tmp_path):
    """Un journal qui ne dit pas combien de faces sont gauchies ne permet pas
    de juger : dans le doute, le maillage est refusé."""
    path = tmp_path / "checkMesh.log"
    path.write_text(
        "    cells:            81510\n"
        "    Mesh non-orthogonality Max: 40 average: 8\n"
        " ***Max skewness = 4.5, quelque chose d'illisible\n"
        "Failed 1 mesh checks.\n",
        encoding="utf-8",
    )
    info = read_check_mesh(path, LIMITS)
    assert not info["mesh_ok"]


def test_un_maillage_sain_reste_sain(tmp_path):
    info = read_check_mesh(
        journal(tmp_path, skewness=1.8, skew_faces=None, failed_checks=None),
        LIMITS,
    )
    assert info["mesh_ok"]
    assert not info["mesh_warnings"]
    assert not info["skewness_tolerated"]


# ─────────────────────────────────────────────────────────────────────────────
# Les seuils eux-mêmes
# ─────────────────────────────────────────────────────────────────────────────


def test_les_deux_conditions_sont_bien_necessaires(tmp_path):
    """Rareté ET modération : ni l'une ni l'autre ne suffit seule."""
    rare_mais_violent = read_check_mesh(
        journal(tmp_path, skewness=20.0, skew_faces=1), LIMITS
    )
    modere_mais_partout = read_check_mesh(
        journal(tmp_path, skewness=4.1, skew_faces=SKEW_TOLERATED_COUNT + 1),
        LIMITS,
    )
    assert not rare_mais_violent["skewness_tolerated"]
    assert not modere_mais_partout["skewness_tolerated"]


def test_la_tolerance_reste_infime():
    """Si ces seuils dérivaient, le contrôle perdrait son sens."""
    assert SKEW_TOLERATED_FRACTION <= 1e-4
    assert SKEW_TOLERATED_COUNT <= 50
    assert SKEW_HARD_CEILING <= 10.0
