"""Re-paramétrisation d'un profil ingéré vers des variables de conception.

    python3 profiles/reparameterize.py naca2412.dat
    python3 profiles/reparameterize.py naca2412.dat -o configs/design_params.yaml
    python3 profiles/reparameterize.py naca2412.dat --order 9 --chord 300

Enchaîne : ingestion (Phase 2) → ajustement CST → **porte de reconstruction**
→ écriture du `design_params.yaml`.

La porte est le point critique du §4 : *« ceci empêche l'optimiseur de partir
d'une mauvaise représentation »*. Sans elle, une optimisation peut se dérouler
parfaitement pendant des heures sur une forme qui n'est pas celle qu'on a
fournie — et rien dans les résultats ne le signalerait, puisque toute la chaîne
en aval fonctionne. C'est le même mode de défaillance silencieuse que la
géométrie inchangée de la v1.0, et il se traite de la même façon : mesurer, et
refuser.

Le fichier écrit porte `parameterization: cst`, ce qui permettra à la Phase 4
de savoir comment reconstruire la géométrie.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from profiles.cst import (  # noqa: E402
    DEFAULT_ORDER,
    CSTProfile,
    ReconstructionError,
    fit_profile,
    reconstruction_error,
)
from profiles.loader import load_profile  # noqa: E402
from profiles.profile import Profile  # noqa: E402
from profiles.validation import validate_profile  # noqa: E402

#: Seuil de REFUS, en fraction de corde. Le §4 en propose 0,05 % pour l'écart
#: maximal ; au delà, la forme optimisée ne serait plus celle qu'on a fournie.
MAX_ERROR_REJECT = 5e-4

#: Seuil d'AVERTISSEMENT : l'ajustement passe, mais mérite un coup d'œil. Placé
#: à 60 % du refus. Le calage tient compte d'un plancher structurel : sur un
#: profil CAMBRÉ, la ligne de cambrure apporte des puissances entières de x que
#: la forme √x · polynôme de CST ne sait qu'approcher, et l'écart maximal
#: plafonne vers 2,5 × 10⁻⁴ de corde — passer de l'ordre 7 à l'ordre 13 ne le
#: fait descendre que de 3,8 à 2,6 × 10⁻⁴. Un seuil plus serré déclencherait
#: donc sur tout profil cambré sain, et l'avertissement ne voudrait plus rien
#: dire. Sur un profil symétrique, l'ordre 7 tient déjà 6 × 10⁻⁵.
MAX_ERROR_WARN = 3e-4

#: Seuils sur l'écart moyen, plusieurs fois plus serrés : un écart maximal
#: isolé au nez ou au bord de fuite est tolérable, un écart moyen élevé signale
#: un ajustement qui rate la forme partout.
MEAN_ERROR_REJECT = 1e-4
MEAN_ERROR_WARN = 6e-5

#: Amplitude minimale accordée à un coefficient pour l'optimisation. Sans ce
#: plancher, un coefficient proche de zéro serait borné dans un intervalle
#: quasi nul et l'optimiseur ne pourrait plus y toucher.
COEFFICIENT_FLOOR = 0.02

#: Demi-amplitude des bornes, en proportion de l'échelle du coefficient.
COEFFICIENT_SPAN = 0.5

#: A₀ gouverne le rayon de bord d'attaque (r = A₀²/2). On lui laisse une marge
#: plus étroite : un nez qui s'aiguise fait décrocher brutalement, et un nez
#: qui s'arrondit trop cesse d'être un profil.
LEADING_COEFFICIENT_SPAN = 0.25

STATUS_OK = "OK"
STATUS_INGESTION_FAILED = "INGESTION_ECHOUEE"
STATUS_INVALID_PROFILE = "PROFIL_INVALIDE"
STATUS_FIT_FAILED = "AJUSTEMENT_IMPOSSIBLE"
STATUS_GATE_REJECTED = "RECONSTRUCTION_REFUSEE"


@dataclass
class ReparameterizationResult:
    """Compte rendu complet : ajustement, erreur, verdict, paramètres."""

    success: bool
    status: str = STATUS_OK
    message: str = ""
    profile: Profile | None = None
    fitted: CSTProfile | None = None
    error: ReconstructionError | None = None
    design_params: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "success": self.success,
            "status": self.status,
            "message": self.message,
            "warnings": self.warnings,
        }
        if self.error is not None:
            data["reconstruction_error"] = self.error.as_dict()
        if self.fitted is not None:
            data["cst"] = self.fitted.measures()
            data["coefficients"] = {
                "upper": self.fitted.upper.coefficients,
                "lower": self.fitted.lower.coefficients,
            }
        if self.profile is not None:
            data["original"] = self.profile.measures()
        return data


# ─────────────────────────────────────────────────────────────────────────────
# Porte de reconstruction
# ─────────────────────────────────────────────────────────────────────────────


def check_reconstruction(
    error: ReconstructionError,
    max_reject: float = MAX_ERROR_REJECT,
    max_warn: float = MAX_ERROR_WARN,
    mean_reject: float = MEAN_ERROR_REJECT,
    mean_warn: float = MEAN_ERROR_WARN,
) -> tuple[bool, list[str], list[str]]:
    """Juge l'ajustement. Retourne (accepté, erreurs, avertissements)."""
    errors: list[str] = []
    warnings: list[str] = []

    if error.max_error > max_reject:
        errors.append(
            f"écart maximal de {error.max_error:.2e} corde "
            f"({error.max_error * 100:.4f} % c) à {error.max_error_position:.1%} "
            f"de corde sur l'{error.max_error_surface}, au delà du seuil de "
            f"{max_reject:.0e} : la forme reconstruite n'est pas celle du "
            f"fichier, et optimiser depuis là porterait sur autre chose"
        )
    elif error.max_error > max_warn:
        warnings.append(
            f"écart maximal de {error.max_error:.2e} corde à "
            f"{error.max_error_position:.1%} sur l'{error.max_error_surface} — "
            f"acceptable, mais un ordre plus élevé le réduirait"
        )

    if error.mean_error > mean_reject:
        errors.append(
            f"écart moyen de {error.mean_error:.2e} corde, au delà du seuil de "
            f"{mean_reject:.0e} : l'ajustement rate la forme partout, pas "
            f"seulement en un point"
        )
    elif error.mean_error > mean_warn:
        warnings.append(f"écart moyen de {error.mean_error:.2e} corde")

    return not errors, errors, warnings


# ─────────────────────────────────────────────────────────────────────────────
# Variables de conception
# ─────────────────────────────────────────────────────────────────────────────


def _coefficient_bounds(
    value: float, span: float = COEFFICIENT_SPAN
) -> tuple[float, float]:
    """Bornes autour d'un coefficient ajusté.

    L'échelle est plafonnée par le bas : un coefficient nul enfermé dans des
    bornes nulles serait gelé, et l'optimiseur perdrait une variable sans le
    savoir.
    """
    scale = max(abs(value), COEFFICIENT_FLOOR)
    return value - span * scale, value + span * scale


def build_design_params(
    fitted: CSTProfile,
    profile: Profile,
    chord_mm: float = 300.0,
    span_mm: float = 80.0,
    aoa_deg: float = 0.0,
    objective: str = "maximize_Cl_Cd",
    design_id: str | None = None,
    coefficient_delta_pct: float = 10.0,
) -> dict[str, Any]:
    """Compose le `design_params.yaml` à partir de l'ajustement.

    Chaque coefficient devient une variable de conception avec ses bornes et
    son budget de variation, comme l'exige le §4. Les grandeurs physiques —
    corde, envergure, incidence — restent des paramètres à part : elles ne se
    déduisent pas de la forme normalisée, et l'incidence en a été retirée à
    l'ingestion précisément pour être pilotée ici.
    """
    parameters: dict[str, Any] = {}

    for label, surface in (("upper", fitted.upper), ("lower", fitted.lower)):
        for index, coefficient in enumerate(surface.coefficients):
            # A₀ tient le rayon de bord d'attaque : sa marge est plus étroite.
            span_factor = (
                LEADING_COEFFICIENT_SPAN if index == 0 else COEFFICIENT_SPAN
            )
            low, high = _coefficient_bounds(coefficient, span_factor)
            parameters[f"cst_{label}_{index}"] = {
                "value": round(coefficient, 8),
                "min": round(low, 8),
                "max": round(high, 8),
                "max_delta_pct": coefficient_delta_pct,
                "unit": "unitless",
            }

    trailing_gap = fitted.upper.trailing_edge - fitted.lower.trailing_edge
    parameters["chord"] = {
        "value": float(chord_mm), "min": round(chord_mm * 0.7, 3),
        "max": round(chord_mm * 1.4, 3), "max_delta_pct": 7.0, "unit": "mm",
    }
    parameters["span"] = {
        "value": float(span_mm), "min": round(span_mm * 0.99, 3),
        "max": round(span_mm * 1.01, 3), "max_delta_pct": 1.0, "unit": "mm",
    }
    parameters["aoa"] = {
        "value": float(aoa_deg), "min": -2.0, "max": 12.0,
        "max_delta_pct": 12.0, "unit": "deg",
    }

    return {
        "iteration": 0,
        "design_id": design_id or _identifier(profile.name),
        "parameterization": "cst",
        "parameters": parameters,
        "constraints": {
            "topology_preserving": True,
            "min_wall_thickness_mm": 1.5,
        },
        "objectives": {"primary": objective},
        "provenance": {
            "source": str(profile.source) if profile.source else None,
            "cst_order": fitted.order,
            # Les deux ordonnées, et pas seulement leur écart : un bord de
            # fuite ouvert n'est pas forcément symétrique, et sur un profil
            # cambré il ne se referme pas non plus sur l'axe. Elles vivent ici
            # plutôt que dans `parameters` parce que le contrat exige
            # `min < max` — une grandeur qui décrit la forme sans être une
            # variable d'optimisation n'y a pas de place.
            "trailing_edge_upper": round(fitted.upper.trailing_edge, 8),
            "trailing_edge_lower": round(fitted.lower.trailing_edge, 8),
            "trailing_edge_gap": round(trailing_gap, 8),
            "original_chord_mm": profile.metadata.get("chord_mm"),
            "removed_incidence_deg": round(profile.transform.rotation_deg, 4),
        },
    }


def _identifier(name: str) -> str:
    """Rend un nom de profil acceptable comme `design_id`."""
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in name.strip()
    ).strip("_")
    return cleaned[:48] or "profil"


# ─────────────────────────────────────────────────────────────────────────────
# Enchaînement complet
# ─────────────────────────────────────────────────────────────────────────────


def reparameterize(
    path: str | Path,
    order: int = DEFAULT_ORDER,
    chord_mm: float = 300.0,
    span_mm: float = 80.0,
    aoa_deg: float = 0.0,
    objective: str = "maximize_Cl_Cd",
    max_reject: float = MAX_ERROR_REJECT,
    strict: bool = True,
) -> ReparameterizationResult:
    """Ingère un profil, l'ajuste en CST, et vérifie la reconstruction.

    Args:
        strict: quand l'écart dépasse le seuil, refuser (défaut) ou seulement
            avertir. Le refus est le comportement voulu : c'est la porte du §4.
    """
    ingestion = load_profile(path)
    if not ingestion.success:
        return ReparameterizationResult(
            False, STATUS_INGESTION_FAILED, ingestion.message
        )

    profile = ingestion.profile
    warnings = list(ingestion.warnings)

    validity = validate_profile(profile)
    if not validity.valid:
        return ReparameterizationResult(
            False, STATUS_INVALID_PROFILE,
            "profil refusé à la validation : " + " | ".join(validity.errors),
            profile=profile, warnings=warnings + validity.warnings,
        )
    warnings.extend(validity.warnings)

    try:
        fitted = fit_profile(profile.upper, profile.lower, order, profile.name)
    except ValueError as exc:
        return ReparameterizationResult(
            False, STATUS_FIT_FAILED, str(exc), profile=profile, warnings=warnings
        )

    error = reconstruction_error(fitted, profile.upper, profile.lower)
    accepted, gate_errors, gate_warnings = check_reconstruction(
        error, max_reject=max_reject
    )
    warnings.extend(gate_warnings)

    if not accepted and strict:
        return ReparameterizationResult(
            False, STATUS_GATE_REJECTED,
            "reconstruction refusée : " + " | ".join(gate_errors),
            profile=profile, fitted=fitted, error=error, warnings=warnings,
        )
    if not accepted:
        warnings.extend(f"(non bloquant) {e}" for e in gate_errors)

    design_params = build_design_params(
        fitted, profile, chord_mm, span_mm, aoa_deg, objective
    )

    return ReparameterizationResult(
        True, STATUS_OK,
        f"profil « {profile.name} » re-paramétré en {fitted.n_coefficients} "
        f"coefficients CST, écart maximal {error.max_error:.2e} corde",
        profile=profile, fitted=fitted, error=error,
        design_params=design_params, warnings=warnings,
    )


def write_design_params(design_params: dict[str, Any], target: Path) -> Path:
    """Écrit le fichier après validation par le contrat du projet.

    On passe par le validateur plutôt que par un `yaml.dump` direct : un
    fichier qui ne satisfait pas le contrat serait refusé plus tard, au milieu
    d'une itération, alors qu'il peut l'être ici.
    """
    from pipeline.utils import save_design_params

    return save_design_params(design_params, target)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="profiles/reparameterize.py",
        description="Ajuste un profil en CST et écrit un design_params.yaml.",
    )
    parser.add_argument("fichier")
    parser.add_argument("-o", "--output", default=None,
                        help="où écrire le design_params.yaml")
    parser.add_argument("--order", type=int, default=DEFAULT_ORDER,
                        help=f"ordre des polynômes (défaut : {DEFAULT_ORDER})")
    parser.add_argument("--chord", type=float, default=300.0,
                        help="corde physique en millimètres")
    parser.add_argument("--span", type=float, default=80.0)
    parser.add_argument("--aoa", type=float, default=0.0)
    parser.add_argument("--objective", default="maximize_Cl_Cd")
    parser.add_argument("--max-error", type=float, default=MAX_ERROR_REJECT,
                        help="seuil de refus sur l'écart maximal, en corde")
    parser.add_argument("--no-strict", action="store_true",
                        help="avertir au lieu de refuser si la porte échoue")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    result = reparameterize(
        args.fichier, args.order, args.chord, args.span, args.aoa,
        args.objective, args.max_error, strict=not args.no_strict,
    )

    if args.json:
        print(json.dumps(result.summary(), indent=2, ensure_ascii=False, default=str))
    else:
        _print_report(result, args.order)

    if not result.success:
        return 1

    if args.output:
        target = write_design_params(result.design_params, Path(args.output))
        print(f"\ndesign_params écrit : {target}")
    return 0


def _print_report(result: ReparameterizationResult, order: int) -> None:
    if result.profile is not None:
        print(f"Profil            : {result.profile.name}")
    if not result.success:
        print(f"ÉCHEC [{result.status}] {result.message}", file=sys.stderr)
        for warning in result.warnings:
            print(f"  [avertissement] {warning}", file=sys.stderr)
        return

    fitted, error, original = result.fitted, result.error, result.profile.measures()
    cst = fitted.measures()

    print(f"Ajustement CST    : ordre {order}, "
          f"{fitted.n_coefficients} coefficients "
          f"({len(fitted.upper.coefficients)} par surface)")
    print()
    print("                      original      reconstruit      écart")
    for label, key in (
        ("Épaisseur max", "max_thickness"),
        ("Cambrure max", "max_camber"),
        ("Rayon de nez", "leading_edge_radius"),
    ):
        before, after = original[key], cst[key]
        print(f"  {label:<17} {before:>10.6f}    {after:>10.6f}    "
              f"{after - before:>+10.2e}")

    print()
    print("Écarts au profil d'origine (distance géométrique, en corde)")
    print(f"  maximal         : {error.max_error:.3e} "
          f"({error.max_error * 100:.4f} % c) à {error.max_error_position:.1%} "
          f"sur l'{error.max_error_surface}")
    print(f"  moyen           : {error.mean_error:.3e}")
    print(f"  RMS             : {error.rms_error:.3e}")
    print(f"  (écart vertical maximal, pour mémoire : "
          f"{error.max_vertical_error:.3e})")
    print()
    print("Coefficients CST")
    print("  extrados : " + "  ".join(f"{c:+.5f}" for c in fitted.upper.coefficients))
    print("  intrados : " + "  ".join(f"{c:+.5f}" for c in fitted.lower.coefficients))
    print()
    print(f"Porte de reconstruction : FRANCHIE "
          f"(seuil {MAX_ERROR_REJECT:.0e} corde)")
    for warning in result.warnings:
        print(f"  [avertissement] {warning}")


if __name__ == "__main__":
    raise SystemExit(main())
