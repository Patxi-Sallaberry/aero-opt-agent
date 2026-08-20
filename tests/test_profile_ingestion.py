"""Tests de l'ingestion de profils 2D (Phase 2, Master Doc v1.5 §3 Mode 2).

Les fichiers d'essai sont engendrés à partir de `naca4_profile()`, la fonction
de profil du projet : l'épaisseur et la cambrure attendues sont donc connues
exactement, et les mesures de l'ingestion se vérifient contre elles plutôt que
contre des valeurs recopiées.

Les cas pathologiques — surfaces inversées, contour ouvert, profil replié,
auto-intersection — sont construits à la main : ce sont eux qui décident si la
validation sert à quelque chose.
"""

import json
import math
from pathlib import Path

import pytest

from fusion.parametric_driver import naca4_profile
from profiles import (
    FORMAT_CSV,
    FORMAT_LEDNICER,
    FORMAT_SELIG,
    Profile,
    detect_format,
    load_profile,
    validate_profile,
)
from profiles import loader as ld
from profiles import validation as vd

ROOT = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────
# Fabrication de fichiers d'essai
# ─────────────────────────────────────────────────────────────


def naca_surfaces(thickness=0.12, camber=0.02, n=80, chord=1.0, aoa_deg=0.0):
    """Extrados et intrados d'un NACA 4 chiffres, du nez vers la queue."""
    plan = naca4_profile(chord, thickness, camber, math.radians(aoa_deg), n)
    return plan["upper"], plan["lower"]


def write_selig(path: Path, upper, lower, title="NACA 2412") -> Path:
    """Format Selig : bord de fuite → extrados → nez → intrados → bord de fuite."""
    lines = [title]
    lines += [f"{x:12.6f}{y:12.6f}" for x, y in reversed(upper)]
    lines += [f"{x:12.6f}{y:12.6f}" for x, y in lower[1:]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_lednicer(path: Path, upper, lower, title="NACA 2412") -> Path:
    """Format Lednicer : en-tête de comptes, puis chaque surface du nez à la queue."""
    lines = [title, f"{len(upper):.1f}  {len(lower):.1f}", ""]
    lines += [f"{x:12.6f}{y:12.6f}" for x, y in upper]
    lines.append("")
    lines += [f"{x:12.6f}{y:12.6f}" for x, y in lower]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_csv(path: Path, upper, lower) -> Path:
    """Le CSV que ce projet exporte lui-même, avec colonne de surface."""
    lines = ["surface,x_mm,y_mm"]
    lines += [f"extrados,{x:.6f},{y:.6f}" for x, y in upper]
    lines += [f"intrados,{x:.6f},{y:.6f}" for x, y in lower]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def selig(tmp_path) -> Path:
    upper, lower = naca_surfaces()
    return write_selig(tmp_path / "naca2412.dat", upper, lower)


@pytest.fixture
def lednicer(tmp_path) -> Path:
    upper, lower = naca_surfaces()
    return write_lednicer(tmp_path / "naca2412_led.dat", upper, lower)


@pytest.fixture
def csv_file(tmp_path) -> Path:
    upper, lower = naca_surfaces()
    return write_csv(tmp_path / "naca2412.csv", upper, lower)


# ─────────────────────────────────────────────────────────────
# Reconnaissance du format
# ─────────────────────────────────────────────────────────────


def test_selig_reconnu(selig):
    result = load_profile(selig)
    assert result.success, result.message
    assert result.metadata["format"] == FORMAT_SELIG


def test_lednicer_reconnu(lednicer):
    result = load_profile(lednicer)
    assert result.success, result.message
    assert result.metadata["format"] == FORMAT_LEDNICER


def test_csv_reconnu(csv_file):
    result = load_profile(csv_file)
    assert result.success, result.message
    assert result.metadata["format"] == FORMAT_CSV


def test_le_nom_dun_csv_nest_pas_une_ligne_de_donnees(csv_file):
    """Dans un CSV étiqueté, chaque ligne commence par un mot : sans filtre, le
    profil s'appellerait « extrados,0.000000,0.000000 »."""
    result = load_profile(csv_file)
    assert result.profile.name == csv_file.stem
    assert "extrados" not in result.profile.name


def test_le_csv_exporte_par_le_projet_se_relit(tmp_path):
    """Aller-retour sur le format que ce projet écrit lui-même : c'est le
    chemin de retour vers la CAO du §5, il doit être sûr."""
    exemple = ROOT / "docs" / "example_report" / "profile_section.csv"
    if not exemple.is_file():
        pytest.skip("exemple de rapport absent")

    result = load_profile(exemple)
    assert result.success, result.message
    assert result.metadata["format"] == FORMAT_CSV
    # Le design optimisé de la v1.0 : épaisseur 0.1134, cambrure 0.02.
    assert result.profile.max_thickness == pytest.approx(0.1134, abs=2e-3)
    assert result.profile.max_camber == pytest.approx(0.02, abs=2e-3)
    assert validate_profile(result.profile).valid


def test_les_trois_formats_donnent_le_meme_profil(selig, lednicer, csv_file):
    """Le point qui compte : la convention du fichier ne doit rien changer à la
    géométrie qu'on en tire."""
    mesures = [
        load_profile(path).profile.measures()
        for path in (selig, lednicer, csv_file)
    ]
    for reference, autre in zip(mesures, mesures[1:]):
        assert autre["max_thickness"] == pytest.approx(
            reference["max_thickness"], abs=1e-6
        )
        assert autre["max_camber"] == pytest.approx(
            reference["max_camber"], abs=1e-6
        )


def test_une_seule_surface_nest_pas_un_profil(tmp_path):
    """Une suite d'abscisses monotone ne décrit pas un contour fermé."""
    upper, _ = naca_surfaces()
    path = tmp_path / "moitie.dat"
    path.write_text(
        "moitie\n" + "\n".join(f"{x:.6f} {y:.6f}" for x, y in upper),
        encoding="utf-8",
    )
    result = load_profile(path)
    assert not result.success
    assert result.status == ld.STATUS_FORMAT_UNKNOWN
    assert "une seule surface" in result.message


def test_detection_sur_trop_peu_de_points():
    assert detect_format([(0.0, 0.0), (1.0, 0.0)]) == ld.FORMAT_UNKNOWN


# ─────────────────────────────────────────────────────────────
# Mesures, contre la vérité connue
# ─────────────────────────────────────────────────────────────


def test_epaisseur_retrouvee(tmp_path):
    upper, lower = naca_surfaces(thickness=0.12, camber=0.0)
    result = load_profile(write_selig(tmp_path / "p.dat", upper, lower))
    assert result.profile.max_thickness == pytest.approx(0.12, rel=0.02)


def test_position_de_lepaisseur_maximale(tmp_path):
    # La distribution NACA place son maximum vers 30 % de corde.
    upper, lower = naca_surfaces(thickness=0.15, camber=0.0)
    result = load_profile(write_selig(tmp_path / "p.dat", upper, lower))
    assert result.profile.max_thickness_position == pytest.approx(0.30, abs=0.04)


def test_cambrure_retrouvee(tmp_path):
    upper, lower = naca_surfaces(thickness=0.12, camber=0.04)
    result = load_profile(write_selig(tmp_path / "p.dat", upper, lower))
    assert result.profile.max_camber == pytest.approx(0.04, rel=0.05)


def test_profil_symetrique_sans_cambrure(tmp_path):
    upper, lower = naca_surfaces(thickness=0.12, camber=0.0)
    result = load_profile(write_selig(tmp_path / "p.dat", upper, lower))
    assert abs(result.profile.max_camber) < 1e-6


def test_rayon_de_bord_dattaque(tmp_path):
    """Pour un NACA 4 chiffres, le rayon de nez vaut 1,1019 t² de corde.

    C'est une valeur connue : elle permet de vérifier l'estimation, et pas
    seulement son sens de variation.
    """
    for thickness in (0.08, 0.12, 0.16):
        upper, lower = naca_surfaces(thickness=thickness, camber=0.0)
        result = load_profile(
            write_selig(tmp_path / f"p{thickness}.dat", upper, lower)
        )
        attendu = 1.1019 * thickness ** 2
        estime = result.profile.leading_edge_radius()
        # Sous-estimation d'environ 7 %, constante : le fichier d'essai est à
        # pas régulier et ne décrit pas le nez. C'est documenté comme tel.
        assert estime == pytest.approx(attendu, rel=0.15), f"t/c = {thickness}"
        assert estime < attendu, "le biais connu est une sous-estimation"


# ─────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────


def test_normalisation_dune_corde_en_millimetres(tmp_path):
    upper, lower = naca_surfaces(chord=300.0)
    result = load_profile(write_selig(tmp_path / "mm.dat", upper, lower))

    assert result.success
    profile = result.profile
    assert profile.upper[0][0] == pytest.approx(0.0, abs=1e-9)
    assert profile.upper[-1][0] == pytest.approx(1.0, abs=1e-6)
    assert profile.transform.scale == pytest.approx(300.0, rel=1e-6)
    assert profile.metadata["chord_mm"] == pytest.approx(300.0, rel=1e-6)
    assert any("non normalisé" in w for w in result.warnings)
    # Les grandeurs relatives ne changent pas avec l'unité.
    assert profile.max_thickness == pytest.approx(0.12, rel=0.02)


def test_lincidence_figee_dans_le_fichier_est_retiree(tmp_path):
    """Une incidence dans les coordonnées serait comptée deux fois : elle est
    un paramètre de conception à part entière."""
    upper, lower = naca_surfaces(aoa_deg=6.0)
    result = load_profile(write_selig(tmp_path / "incline.dat", upper, lower))

    assert result.success
    # `naca4_profile` tourne de -aoa : la rotation retirée est donc négative.
    assert result.profile.transform.rotation_deg == pytest.approx(-6.0, abs=0.05)
    assert any("incidence" in w for w in result.warnings)
    # Après redressement, le bord de fuite est revenu sur l'axe.
    assert result.profile.upper[-1][1] == pytest.approx(0.0, abs=1e-6)


def test_la_transformation_permet_de_revenir_au_fichier(tmp_path):
    upper, lower = naca_surfaces(chord=250.0, aoa_deg=4.0)
    result = load_profile(write_selig(tmp_path / "p.dat", upper, lower))
    profile = result.profile

    restaure = profile.transform.restore(profile.upper[10])
    assert restaure[0] == pytest.approx(upper[10][0], abs=1e-6)
    assert restaure[1] == pytest.approx(upper[10][1], abs=1e-6)


# ─────────────────────────────────────────────────────────────
# Nettoyage
# ─────────────────────────────────────────────────────────────


def test_doublons_supprimes(tmp_path):
    upper, lower = naca_surfaces()
    lines = ["avec doublons"]
    for x, y in reversed(upper):
        lines.append(f"{x:.6f} {y:.6f}")
        lines.append(f"{x:.6f} {y:.6f}")      # chaque point écrit deux fois
    lines += [f"{x:.6f} {y:.6f}" for x, y in lower[1:]]
    path = tmp_path / "doublons.dat"
    path.write_text("\n".join(lines), encoding="utf-8")

    result = load_profile(path)
    assert result.success, result.message
    assert any("doublon" in w for w in result.warnings)
    assert result.profile.max_thickness == pytest.approx(0.12, rel=0.02)


def test_bord_de_fuite_presque_ferme_est_referme(tmp_path):
    upper, lower = naca_surfaces()
    upper = list(upper)
    upper[-1] = (upper[-1][0], upper[-1][1] + 2e-5)
    result = load_profile(write_selig(tmp_path / "p.dat", upper, lower))
    assert result.profile.trailing_edge_gap < 1e-9
    assert any("refermé" in w for w in result.warnings)


def test_bord_de_fuite_epais_est_conserve(tmp_path):
    """Un bord de fuite épais est un choix de conception, pas un défaut : on ne
    doit surtout pas le refermer en douce."""
    upper, lower = naca_surfaces()
    upper = list(upper)
    lower = list(lower)
    upper[-1] = (upper[-1][0], upper[-1][1] + 0.005)
    lower[-1] = (lower[-1][0], lower[-1][1] - 0.005)
    result = load_profile(write_selig(tmp_path / "p.dat", upper, lower))

    assert result.profile.trailing_edge_gap == pytest.approx(0.01, abs=2e-3)
    assert any("conservé tel quel" in w for w in result.warnings)


def test_separateur_point_virgule_et_virgule_decimale(tmp_path):
    upper, lower = naca_surfaces()
    lines = ["profil europeen"]
    for x, y in list(reversed(upper)) + list(lower[1:]):
        lines.append(f"{x:.6f};{y:.6f}".replace(".", ","))
    path = tmp_path / "eu.csv"
    path.write_text("\n".join(lines), encoding="utf-8")

    result = load_profile(path)
    assert result.success, result.message
    assert result.profile.max_thickness == pytest.approx(0.12, rel=0.02)


def test_commentaires_et_lignes_vides_ignores(tmp_path):
    upper, lower = naca_surfaces()
    lines = ["# un commentaire", "", "mon profil", "! autre commentaire"]
    lines += [f"{x:.6f} {y:.6f}" for x, y in reversed(upper)]
    lines += [f"{x:.6f} {y:.6f}" for x, y in lower[1:]]
    path = tmp_path / "commente.dat"
    path.write_text("\n".join(lines), encoding="utf-8")

    result = load_profile(path)
    assert result.success, result.message
    assert result.profile.name == "mon profil"


# ─────────────────────────────────────────────────────────────
# Erreurs de chargement — jamais d'exception
# ─────────────────────────────────────────────────────────────


def test_fichier_absent(tmp_path):
    result = load_profile(tmp_path / "rien.dat")
    assert not result.success
    assert result.status == ld.STATUS_FILE_MISSING
    assert result.profile is None


def test_fichier_vide(tmp_path):
    path = tmp_path / "vide.dat"
    path.write_text("", encoding="utf-8")
    result = load_profile(path)
    assert not result.success
    assert result.status == ld.STATUS_TOO_FEW_POINTS


def test_fichier_de_texte(tmp_path):
    path = tmp_path / "texte.dat"
    path.write_text("ceci n'est pas\nun profil du tout\n", encoding="utf-8")
    result = load_profile(path)
    assert not result.success
    assert result.profile is None


def test_fichier_binaire_ne_fait_pas_planter(tmp_path):
    path = tmp_path / "binaire.dat"
    path.write_bytes(bytes(range(256)) * 8)
    result = load_profile(path)          # ne doit pas lever
    assert not result.success


def test_valeurs_non_finies_ignorees(tmp_path):
    upper, lower = naca_surfaces()
    lines = ["avec nan"]
    lines += [f"{x:.6f} {y:.6f}" for x, y in reversed(upper)]
    lines.append("nan nan")
    lines.append("inf 0.5")
    lines += [f"{x:.6f} {y:.6f}" for x, y in lower[1:]]
    path = tmp_path / "nan.dat"
    path.write_text("\n".join(lines), encoding="utf-8")

    result = load_profile(path)
    assert result.success, result.message
    assert result.profile.max_thickness == pytest.approx(0.12, rel=0.02)


# ─────────────────────────────────────────────────────────────
# Validation
# ─────────────────────────────────────────────────────────────


def test_profil_sain_valide(selig):
    report = validate_profile(load_profile(selig).profile)
    assert report.valid, report.format()
    assert report.errors == []


def test_surfaces_inversees_refusees(tmp_path):
    """Extrados et intrados intervertis : l'épaisseur devient négative."""
    upper, lower = naca_surfaces()
    profile = Profile(upper=list(lower), lower=list(upper))
    report = validate_profile(profile)
    assert not report.valid
    assert any("croisent" in e or "inversées" in e for e in report.errors)


def test_profil_trop_fin_refuse(tmp_path):
    upper, lower = naca_surfaces(thickness=0.005, camber=0.0)
    result = load_profile(write_selig(tmp_path / "fin.dat", upper, lower))
    report = validate_profile(result.profile)
    assert not report.valid
    assert any("sous le minimum" in e for e in report.errors)


def test_profil_trop_epais_refuse(tmp_path):
    upper, lower = naca_surfaces(thickness=0.55, camber=0.0)
    result = load_profile(write_selig(tmp_path / "epais.dat", upper, lower))
    report = validate_profile(result.profile)
    assert not report.valid
    assert any("corps épais" in e for e in report.errors)


def test_contour_ouvert_au_nez_refuse():
    upper, lower = naca_surfaces()
    decale = [(x, y) for x, y in lower]
    decale[0] = (decale[0][0] + 0.02, decale[0][1] - 0.02)
    report = validate_profile(Profile(upper=list(upper), lower=decale))
    assert not report.valid
    assert any("bord d'attaque ouvert" in e for e in report.errors)


def test_bord_de_fuite_beant_refuse():
    upper, lower = naca_surfaces()
    upper = [(x, y) for x, y in upper]
    upper[-1] = (upper[-1][0], upper[-1][1] + 0.10)
    report = validate_profile(Profile(upper=upper, lower=list(lower)))
    assert not report.valid
    assert any("bord de fuite ouvert" in e for e in report.errors)


def test_surface_repliee_refusee():
    upper, lower = naca_surfaces()
    plie = [(x, y) for x, y in upper]
    plie[40], plie[41] = plie[41], plie[40]     # deux points intervertis
    report = validate_profile(Profile(upper=plie, lower=list(lower)))
    assert not report.valid
    assert any("replie" in e for e in report.errors)


def test_auto_intersection_detectee():
    """Un contour qui se recroise ne donnera jamais un volume maillable."""
    contour = [(0.0, 0.0), (1.0, 0.1), (1.0, -0.1), (0.0, 0.0)]
    assert vd._self_intersection(contour) is None      # ce quadrilatère est sain

    croise = [(0.0, 0.0), (1.0, 0.1), (0.0, 0.1), (1.0, 0.0), (0.0, 0.0)]
    assert vd._self_intersection(croise) is not None


def test_bord_de_fuite_epais_seulement_signale(tmp_path):
    upper, lower = naca_surfaces()
    upper = list(upper)
    lower = list(lower)
    upper[-1] = (upper[-1][0], upper[-1][1] + 0.006)
    lower[-1] = (lower[-1][0], lower[-1][1] - 0.006)
    report = validate_profile(Profile(upper=upper, lower=lower))
    assert report.valid                       # légitime
    assert any("bord de fuite épais" in w for w in report.warnings)


def test_forte_cambrure_signalee_sans_etre_refusee(tmp_path):
    upper, lower = naca_surfaces(thickness=0.12, camber=0.18)
    result = load_profile(write_selig(tmp_path / "cambre.dat", upper, lower))
    report = validate_profile(result.profile)
    assert report.valid
    assert any("cambrure" in w for w in report.warnings)


def test_profil_grossier_signale(tmp_path):
    upper, lower = naca_surfaces(n=12)
    result = load_profile(write_selig(tmp_path / "grossier.dat", upper, lower))
    report = validate_profile(result.profile)
    assert any("grossièrement" in w for w in report.warnings)


def test_trop_peu_de_points_refuse():
    report = validate_profile(Profile(
        upper=[(0.0, 0.0), (0.5, 0.05), (1.0, 0.0)],
        lower=[(0.0, 0.0), (0.5, -0.05), (1.0, 0.0)],
    ))
    assert not report.valid
    assert any("trop peu de points" in e for e in report.errors)


# ─────────────────────────────────────────────────────────────
# Formes dérivées
# ─────────────────────────────────────────────────────────────


def test_le_contour_est_ferme(selig):
    contour = load_profile(selig).profile.contour()
    assert contour[0] == pytest.approx(contour[-1], abs=1e-12)


def test_selig_repart_du_bord_de_fuite(selig):
    points = load_profile(selig).profile.selig()
    assert points[0][0] == pytest.approx(1.0, abs=1e-6)
    assert points[-1][0] == pytest.approx(1.0, abs=1e-6)
    milieu = points[len(points) // 2]
    assert milieu[0] < 0.05                   # le nez est au milieu


def test_aller_retour_selig(tmp_path, selig):
    """Réécrire un profil chargé et le relire doit rendre la même chose."""
    profile = load_profile(selig).profile
    reecrit = tmp_path / "aller_retour.dat"
    reecrit.write_text(
        profile.name + "\n"
        + "\n".join(f"{x:.8f} {y:.8f}" for x, y in profile.selig()),
        encoding="utf-8",
    )
    relu = load_profile(reecrit)
    assert relu.success, relu.message
    assert relu.profile.max_thickness == pytest.approx(
        profile.max_thickness, abs=1e-6
    )
    assert relu.profile.n_points == profile.n_points


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────


def test_cli_chargement(selig, capsys):
    assert ld.main([str(selig)]) == 0
    sortie = capsys.readouterr().out
    assert "Épaisseur max" in sortie
    assert "NACA 2412" in sortie


def test_cli_chargement_json(selig, capsys):
    assert ld.main([str(selig), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["success"] is True
    assert data["measures"]["max_thickness"] == pytest.approx(0.12, rel=0.02)


def test_cli_chargement_en_echec(tmp_path, capsys):
    assert ld.main([str(tmp_path / "absent.dat")]) == 1
    assert "ÉCHEC" in capsys.readouterr().err


def test_cli_validation(selig, capsys):
    assert vd.main([str(selig)]) == 0
    assert "VALIDE" in capsys.readouterr().out


def test_cli_validation_refus(tmp_path, capsys):
    upper, lower = naca_surfaces(thickness=0.005, camber=0.0)
    path = write_selig(tmp_path / "fin.dat", upper, lower)
    assert vd.main([str(path)]) == 1
    assert "REFUSÉ" in capsys.readouterr().out


def test_cli_validation_fichier_illisible(tmp_path, capsys):
    assert vd.main([str(tmp_path / "absent.dat")]) == 2
