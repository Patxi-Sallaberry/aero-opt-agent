"""§3 Mode 3 et §5 — le STEP, dans les deux sens, via un noyau CAO libre.

Deux besoins que rien d'autre ne couvrait.

**Écrire un vrai solide.** Le STL est un maillage de facettes planes : on peut
le simuler et l'imprimer, mais importé dans une CAO il reste un maillage —
impossible d'y poser un congé ou d'en changer une cote. Un STEP décrit des
SURFACES, et Fusion l'ouvre comme un solide natif.

**Lire un STEP.** Le contour n'y est pas écrit : il se déduit d'une topologie
de faces, d'arêtes et de courbes NURBS. Cela demande un noyau, pas un parseur —
d'où la dépendance, là où le DXF s'en passe.

`cadquery` pèse près de deux gigaoctets et reste **facultatif**. Chaque cas
ci-dessous doit donc valoir dans les deux situations : avec le noyau, il vérifie
le résultat ; sans lui, il vérifie que le refus est propre et explicite. Un test
qui se contenterait de sauter laisserait le chemin dégradé sans couverture —
alors que c'est celui que rencontrera la majorité des installations.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from geometry.step_io import (
    MAX_SPLINE_POINTS,
    available,
    read_step_contour,
    write_step,
)
from profiles.cst import distance_to_curve
from profiles.loader import FORMAT_STEP, load_profile

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "profiles"
CHORD = 300.0
SPAN = 80.0

besoin_de_cao = pytest.mark.skipif(
    not available(), reason="noyau CAO absent (pip install cadquery)"
)


def surfaces(name: str = "clarky") -> tuple[list, list]:
    """Profil d'exemple, mis à l'échelle en millimètres."""
    profile = load_profile(EXAMPLES / f"{name}.dat").profile
    return (
        [(x * CHORD, y * CHORD) for x, y in profile.upper],
        [(x * CHORD, y * CHORD) for x, y in profile.lower],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sans le noyau : refuser proprement
# ─────────────────────────────────────────────────────────────────────────────


def test_l_absence_du_noyau_est_dite_et_nommee(tmp_path, monkeypatch):
    """Le message doit donner la commande, pas seulement constater le manque."""
    import geometry.step_io as step_io

    monkeypatch.setattr(step_io, "available", lambda: False)
    result = step_io.write_step(*surfaces(), SPAN, tmp_path / "x.step")
    assert not result.success
    assert "pip install cadquery" in result.message


def test_un_step_en_entree_sans_noyau_oriente_vers_le_dxf(tmp_path, monkeypatch):
    """Un « format non reconnu » enverrait chercher au mauvais endroit."""
    import geometry.step_io as step_io

    monkeypatch.setattr(step_io, "available", lambda: False)
    faux = tmp_path / "piece.step"
    faux.write_text("ISO-10303-21;\n", encoding="utf-8")

    result = load_profile(faux)
    assert not result.success
    assert "cadquery" in result.message
    assert "DXF" in result.message


def test_le_reste_du_systeme_ne_depend_pas_du_noyau(tmp_path, monkeypatch):
    """Sans STEP, la géométrie reste produite et la boucle continue.

    C'est la garantie qui rend la dépendance acceptable : deux gigaoctets ne
    doivent pas être la condition d'un système qui tournait sans eux.
    """
    import geometry.internal_backend as backend
    from profiles.reparameterize import reparameterize, write_design_params

    monkeypatch.setattr("geometry.step_io.available", lambda: False)

    fit = reparameterize(EXAMPLES / "clarky.dat", chord_mm=CHORD, span_mm=SPAN)
    assert fit.success, fit.message
    config = write_design_params(fit.design_params, tmp_path / "design_params.yaml")

    out = tmp_path / "iterations" / "iter_0000"
    out.mkdir(parents=True)
    result = backend.InternalBackend().generate(config, out)

    assert result.success, result.message
    assert result.stl_path.is_file()
    assert result.step_path is None


# ─────────────────────────────────────────────────────────────────────────────
# Écriture
# ─────────────────────────────────────────────────────────────────────────────


@besoin_de_cao
def test_le_step_ecrit_est_un_solide_ferme(tmp_path):
    cible = tmp_path / "aile.step"
    result = write_step(*surfaces(), SPAN, cible)
    assert result.success, result.message
    assert cible.is_file() and cible.stat().st_size > 10_000
    assert result.volume_mm3 > 0


@besoin_de_cao
def test_le_volume_correspond_a_la_geometrie_demandee(tmp_path):
    """Vérifié contre l'aire du profil, pas contre la sortie du noyau.

    L'aire d'un profil courant vaut environ 0,68 × t × c² ; à 11,7 %
    d'épaisseur sur 300 mm de corde et 80 mm d'envergure, cela fait de l'ordre
    de 5,7 × 10⁵ mm³. Une erreur d'unité — millimètres pris pour des mètres —
    se verrait immédiatement.
    """
    result = write_step(*surfaces(), SPAN, tmp_path / "aile.step")
    attendu = 0.68 * 0.117 * CHORD ** 2 * SPAN
    assert result.volume_mm3 == pytest.approx(attendu, rel=0.05)


@besoin_de_cao
def test_le_solide_a_peu_de_faces():
    """C'est tout l'intérêt du STEP : des surfaces, pas des centaines de facettes."""
    import tempfile

    result = write_step(*surfaces(), SPAN,
                        Path(tempfile.mkdtemp()) / "a.step")
    assert result.success
    assert result.faces <= 8


@besoin_de_cao
def test_un_bord_de_fuite_ferme_ne_degenere_pas(tmp_path):
    """Le NACA 2412 a un bord de fuite fermé : les deux surfaces s'y touchent.

    Répéter ce point unique dans la seconde spline crée une arête de longueur
    nulle, que le noyau refuse — c'est le cas qui a fait échouer la première
    version, silencieusement, sur un message d'erreur vide.
    """
    haut, bas = surfaces("naca2412")
    assert math.dist(haut[-1], bas[-1]) < 1e-9   # bord de fuite bien fermé
    result = write_step(haut, bas, SPAN, tmp_path / "naca.step")
    assert result.success, result.message


@besoin_de_cao
def test_un_bord_de_fuite_ouvert_reste_plat(tmp_path):
    """Le Clark Y a un bord de fuite épaissi : c'est une face, pas un arrondi.

    Laisser la spline passer d'une lèvre à l'autre l'arrondirait — et
    l'arrondir change la traînée de culot, qui est précisément ce qu'un bord de
    fuite épais coûte.
    """
    haut, bas = surfaces("clarky")
    assert math.dist(haut[-1], bas[-1]) > 0.1   # bord de fuite bien ouvert
    result = write_step(haut, bas, SPAN, tmp_path / "clarky.step")
    assert result.success, result.message
    # Deux bouts d'extrusion + la face plate du bord de fuite.
    assert result.faces >= 5


@besoin_de_cao
def test_un_contour_trop_dense_est_allege(tmp_path):
    """Une spline qui interpole trois cents points ondule entre eux."""
    haut = [(CHORD * i / 400, 10.0 + i * 0.01) for i in range(401)]
    bas = [(CHORD * i / 400, -10.0 - i * 0.01) for i in range(401)]
    result = write_step(haut, bas, SPAN, tmp_path / "dense.step")
    assert result.success, result.message
    assert any("allégé" in w for w in result.warnings)


def test_une_envergure_nulle_est_refusee(tmp_path):
    """Et le refus ne dépend pas de la présence du noyau.

    Une envergure nulle est une erreur de l'appelant, pas de son installation.
    Si le contrôle venait après la vérification du noyau, un même appel fautif
    dirait deux choses différentes selon la machine.
    """
    result = write_step(*surfaces(), 0.0, tmp_path / "plat.step")
    assert not result.success
    assert "envergure" in result.message


def test_des_surfaces_vides_sont_refusees(tmp_path):
    result = write_step([], [], SPAN, tmp_path / "vide.step")
    assert not result.success
    assert "trop pauvres" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# Lecture
# ─────────────────────────────────────────────────────────────────────────────


@besoin_de_cao
def test_le_contour_relu_epouse_le_profil_ecrit(tmp_path):
    """L'aller-retour complet : points → solide → points.

    On mesure dans le sens qui a un sens — les points d'ORIGINE doivent se
    trouver sur la surface reconstruite. Le sens inverse mesurerait surtout la
    grossièreté de la polyligne de départ, qui coupe l'arc du nez.
    """
    haut, bas = surfaces()
    cible = tmp_path / "aile.step"
    assert write_step(haut, bas, SPAN, cible).success

    relu = read_step_contour(cible)
    assert relu.success, relu.message

    contour = relu.contour + [relu.contour[0]]
    ecart = max(distance_to_curve(p, contour) for p in haut + bas)
    assert ecart / CHORD < 3e-4


@besoin_de_cao
def test_un_step_se_charge_comme_un_profil(tmp_path):
    """Écrit puis relu par l'ingestion : le Mode 3 boucle sur lui-même."""
    cible = tmp_path / "aile.step"
    write_step(*surfaces(), SPAN, cible)

    result = load_profile(cible)
    assert result.success, result.message
    assert result.profile.metadata["format"] == FORMAT_STEP

    mesures = result.profile.measures()
    assert mesures["max_thickness"] == pytest.approx(0.117, abs=3e-3)
    assert mesures["max_camber"] == pytest.approx(0.034, abs=3e-3)


@besoin_de_cao
def test_la_corde_du_step_est_retrouvee(tmp_path):
    """Le fichier est en millimètres ; l'ingestion doit le voir."""
    cible = tmp_path / "aile.step"
    write_step(*surfaces(), SPAN, cible)
    profil = load_profile(cible).profile
    assert profil.metadata["chord_mm"] == pytest.approx(CHORD, rel=1e-3)


def test_un_step_absent_est_un_compte_rendu():
    result = read_step_contour("/inexistant/piece.step")
    assert not result.success
    if available():
        assert "introuvable" in result.message


@besoin_de_cao
def test_un_step_illisible_est_un_compte_rendu(tmp_path):
    casse = tmp_path / "casse.step"
    casse.write_text("ceci n'est pas un STEP", encoding="utf-8")
    result = read_step_contour(casse)
    assert not result.success
    assert "illisible" in result.message


# ─────────────────────────────────────────────────────────────────────────────
# Réglages
# ─────────────────────────────────────────────────────────────────────────────


def test_la_limite_de_points_reste_raisonnable():
    """Assez pour décrire un profil, assez peu pour que la spline ne gondole pas."""
    assert 60 <= MAX_SPLINE_POINTS <= 200
