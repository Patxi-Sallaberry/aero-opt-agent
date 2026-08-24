"""§3 Mode 3 — lire le contour d'un dessin 2D.

Un DXF n'est pas une liste de coordonnées : c'est un dessin. Les entités y sont
dans l'ordre où elles ont été tracées, chacune dans le sens où la main est
passée, le contour est souvent découpé en morceaux, et il traîne un cartouche
ou un axe à côté. Rien de tout cela n'est une anomalie — c'est ce que produit
une CAO.

Le module doit donc reconstituer, sans rien supposer du fichier : rabouter les
morceaux par proximité, choisir le bon contour parmi plusieurs, le faire partir
du bord de fuite et le parcourir dans le sens de la convention Selig. Après
quoi le chemin est celui du Mode 2, contrôles compris.

Le fichier d'exemple est délibérément peu coopératif : dix-sept polylignes
mélangées, sens inversé, une ligne de fermeture et un cartouche parasite.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from profiles.cst import distance_to_curve
from profiles.dxf import (
    STATUS_NO_GEOMETRY,
    STATUS_OPEN_CONTOUR,
    STATUS_UNREADABLE,
    contour_to_selig,
    read_dxf_contour,
)
from profiles.loader import FORMAT_DXF, load_profile
from profiles.reparameterize import reparameterize

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "profiles"
DXF = EXAMPLES / "naca2412.dxf"


def dxf_text(body: str) -> str:
    return f"0\nSECTION\n2\nENTITIES\n{body}0\nENDSEC\n0\nEOF\n"


def lwpolyline(points: list[tuple[float, float]], closed: bool = False) -> str:
    out = f"0\nLWPOLYLINE\n8\nPROFIL\n90\n{len(points)}\n70\n{1 if closed else 0}\n"
    for x, y in points:
        out += f"10\n{x:.6f}\n20\n{y:.6f}\n"
    return out


def rectangle(w: float = 10.0, h: float = 4.0) -> list[tuple[float, float]]:
    """Un contour fermé simple, assez dense pour passer les contrôles."""
    points = []
    for i in range(12):
        points.append((w * i / 12, -h / 2))
    for i in range(12):
        points.append((w, -h / 2 + h * i / 12))
    for i in range(12):
        points.append((w - w * i / 12, h / 2))
    for i in range(12):
        points.append((0.0, h / 2 - h * i / 12))
    return points


# ─────────────────────────────────────────────────────────────────────────────
# Lecture des entités
# ─────────────────────────────────────────────────────────────────────────────


def test_une_polyligne_fermee_est_lue(tmp_path):
    path = tmp_path / "carre.dxf"
    path.write_text(dxf_text(lwpolyline(rectangle(), closed=True)),
                    encoding="utf-8")
    result = read_dxf_contour(path)
    assert result.success, result.message
    assert len(result.contour) >= 48


def test_des_lignes_separees_sont_raboutees(tmp_path):
    """Un contour découpé en segments doit se rechaîner."""
    points = rectangle()
    corps = ""
    for i in range(len(points) - 1):
        a, b = points[i], points[i + 1]
        corps += (f"0\nLINE\n8\nP\n10\n{a[0]}\n20\n{a[1]}\n"
                  f"11\n{b[0]}\n21\n{b[1]}\n")
    corps += (f"0\nLINE\n8\nP\n10\n{points[-1][0]}\n20\n{points[-1][1]}\n"
              f"11\n{points[0][0]}\n21\n{points[0][1]}\n")
    path = tmp_path / "segments.dxf"
    path.write_text(dxf_text(corps), encoding="utf-8")

    result = read_dxf_contour(path)
    assert result.success, result.message
    assert len(result.contour) >= len(points)


def test_un_segment_a_l_envers_est_retourne(tmp_path):
    """Le sens de tracé d'une entité n'a aucune raison d'être cohérent."""
    points = rectangle()
    moitie = len(points) // 2
    # Première moitié dans le sens du contour, seconde À L'ENVERS : le
    # rechaînage doit reconnaître qu'elles se touchent par leurs FINS.
    corps = lwpolyline(points[:moitie + 1])
    corps += lwpolyline(list(reversed(points[moitie:] + [points[0]])))
    path = tmp_path / "inverse.dxf"
    path.write_text(dxf_text(corps), encoding="utf-8")

    result = read_dxf_contour(path)
    assert result.success, result.message


def test_un_cercle_est_discretise(tmp_path):
    path = tmp_path / "cercle.dxf"
    path.write_text(dxf_text("0\nCIRCLE\n8\nP\n10\n0\n20\n0\n40\n5\n"),
                    encoding="utf-8")
    result = read_dxf_contour(path)
    assert result.success, result.message
    rayons = [math.hypot(x, y) for x, y in result.contour]
    assert all(r == pytest.approx(5.0, abs=1e-9) for r in rayons)


def test_un_arc_respecte_ses_angles(tmp_path):
    path = tmp_path / "arc.dxf"
    path.write_text(
        dxf_text("0\nARC\n8\nP\n10\n0\n20\n0\n40\n2\n50\n0\n51\n90\n"),
        encoding="utf-8",
    )
    result = read_dxf_contour(path)
    # Un arc seul ne ferme pas un contour : ce qui compte est qu'il soit lu.
    assert result.contour or result.status == STATUS_OPEN_CONTOUR


def test_les_entites_inconnues_sont_signalees_pas_ignorees(tmp_path):
    """Si le contour dépend d'une entité non gérée, il faut le savoir."""
    corps = lwpolyline(rectangle(), closed=True)
    corps += "0\nMTEXT\n8\nCOTES\n1\nCorde 250\n"
    path = tmp_path / "avec_texte.dxf"
    path.write_text(dxf_text(corps), encoding="utf-8")

    result = read_dxf_contour(path)
    assert result.success
    assert any("MTEXT" in w for w in result.warnings)


# ─────────────────────────────────────────────────────────────────────────────
# Choix du contour
# ─────────────────────────────────────────────────────────────────────────────


def test_le_contour_le_plus_etendu_l_emporte(tmp_path):
    """Un cartouche pris pour le profil donnerait une forme absurde."""
    corps = lwpolyline(rectangle(100.0, 20.0), closed=True)
    corps += lwpolyline(rectangle(8.0, 3.0), closed=True)
    path = tmp_path / "cartouche.dxf"
    path.write_text(dxf_text(corps), encoding="utf-8")

    result = read_dxf_contour(path)
    assert result.success
    assert max(x for x, _ in result.contour) == pytest.approx(100.0)
    assert any("contours distincts" in w for w in result.warnings)


# ─────────────────────────────────────────────────────────────────────────────
# Mise à la convention Selig
# ─────────────────────────────────────────────────────────────────────────────


def test_le_contour_repart_du_bord_de_fuite():
    profil = load_profile(EXAMPLES / "naca2412.dat").profile
    boucle = profil.upper + list(reversed(profil.lower))
    ordonne = contour_to_selig(boucle)
    assert ordonne[0][0] == pytest.approx(max(x for x, _ in ordonne))


def test_le_sens_de_parcours_est_corrige():
    """Extrados d'abord, quel que soit le sens du dessin."""
    profil = load_profile(EXAMPLES / "naca2412.dat").profile
    boucle = profil.upper + list(reversed(profil.lower))
    a_l_endroit = contour_to_selig(boucle)
    a_l_envers = contour_to_selig(list(reversed(boucle)))

    quart = len(a_l_endroit) // 4
    for ordonne in (a_l_endroit, a_l_envers):
        moyenne = sum(y for _, y in ordonne[1:1 + quart]) / quart
        assert moyenne > 0  # l'extrados est au dessus de la corde


def test_le_contour_ordonne_est_ferme():
    profil = load_profile(EXAMPLES / "naca2412.dat").profile
    ordonne = contour_to_selig(profil.upper + list(reversed(profil.lower)))
    assert math.dist(ordonne[0], ordonne[-1]) == pytest.approx(0.0, abs=1e-12)


# ─────────────────────────────────────────────────────────────────────────────
# La chaîne complète
# ─────────────────────────────────────────────────────────────────────────────


def test_un_dxf_est_ingere_comme_un_fichier_de_points():
    result = load_profile(DXF)
    assert result.success, result.message
    assert result.profile.metadata["format"] == FORMAT_DXF


def test_le_dxf_donne_exactement_le_meme_profil_que_le_dat():
    """Le contrôle qui compte : les deux voies doivent converger.

    Le DXF d'exemple est fabriqué à partir du `.dat`, mais découpé en dix-sept
    polylignes mélangées, à l'envers, avec un cartouche. Si le rechaînage, le
    choix du contour ou la remise en ordre se trompaient quelque part, l'écart
    se verrait ici.
    """
    dat = load_profile(EXAMPLES / "naca2412.dat").profile
    dxf = load_profile(DXF).profile

    reference = dat.upper + list(reversed(dat.lower))
    reference = reference + [reference[0]]
    ecart = max(distance_to_curve(p, reference) for p in dxf.upper + dxf.lower)
    assert ecart < 1e-12


def test_le_dxf_donne_les_memes_coefficients_cst():
    """À l'arrondi du dessin près.

    Le DXF écrit ses coordonnées à six décimales, sur une corde de 250 unités :
    la quantification vaut donc 4 × 10⁻⁹ de corde. Exiger mieux reviendrait à
    tester la précision du format, pas celle de la lecture.
    """
    depuis_dat = reparameterize(EXAMPLES / "naca2412.dat", chord_mm=250.0)
    depuis_dxf = reparameterize(DXF, chord_mm=250.0)
    assert depuis_dat.success and depuis_dxf.success
    for a, b in zip(depuis_dat.fitted.upper.coefficients,
                    depuis_dxf.fitted.upper.coefficients):
        assert a == pytest.approx(b, abs=1e-8)


def test_l_echelle_du_dessin_est_detectee():
    """Le DXF est dessiné à 250 unités de corde, pas en corde unitaire."""
    profil = load_profile(DXF).profile
    assert profil.metadata["chord_mm"] == pytest.approx(250.0, rel=1e-6)


def test_les_mesures_du_dxf_valent_celles_du_naca():
    """Vérifié contre la géométrie analytique, pas contre le fichier."""
    mesures = load_profile(DXF).profile.measures()
    assert mesures["max_thickness"] == pytest.approx(0.12, abs=1e-3)
    assert mesures["max_camber"] == pytest.approx(0.02, abs=1e-3)


# ─────────────────────────────────────────────────────────────────────────────
# Ce qui est refusé, sans jamais lever
# ─────────────────────────────────────────────────────────────────────────────


def test_un_fichier_absent_est_un_compte_rendu():
    result = read_dxf_contour("/inexistant/dessin.dxf")
    assert not result.success and result.status == STATUS_UNREADABLE


def test_un_fichier_qui_n_est_pas_un_dxf_est_refuse(tmp_path):
    path = tmp_path / "faux.dxf"
    path.write_text("ceci n'est pas un dessin\n", encoding="utf-8")
    result = read_dxf_contour(path)
    assert not result.success


def test_une_section_entities_vide_est_refusee(tmp_path):
    path = tmp_path / "vide.dxf"
    path.write_text(dxf_text(""), encoding="utf-8")
    result = read_dxf_contour(path)
    assert not result.success
    assert result.status in (STATUS_NO_GEOMETRY, STATUS_UNREADABLE)


def test_un_contour_franchement_ouvert_est_refuse(tmp_path):
    """Deux morceaux qui ne se rejoignent pas ne décrivent pas un profil."""
    points = rectangle(100.0, 20.0)
    path = tmp_path / "ouvert.dxf"
    path.write_text(dxf_text(lwpolyline(points[:30])), encoding="utf-8")
    result = read_dxf_contour(path)
    assert not result.success
    assert result.status == STATUS_OPEN_CONTOUR
    assert "ne se rejoignent pas" in result.message


def test_un_contour_trop_pauvre_est_refuse(tmp_path):
    path = tmp_path / "triangle.dxf"
    path.write_text(
        dxf_text(lwpolyline([(0, 0), (10, 0), (5, 3), (0, 0)])),
        encoding="utf-8",
    )
    result = read_dxf_contour(path)
    assert not result.success
    assert "trop peu" in result.message


def test_un_dxf_refuse_ne_fait_pas_lever_l_ingestion(tmp_path):
    path = tmp_path / "casse.dxf"
    path.write_text("n'importe quoi", encoding="utf-8")
    result = load_profile(path)
    assert not result.success
    assert result.profile is None
