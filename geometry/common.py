"""Traduction du compte rendu du driver vers `GeometryResult`.

Les deux backends partagent ce code : ils s'appuient sur le même driver, dont
le compte rendu est un dictionnaire riche. Cette fonction en extrait ce que
l'interface expose, sans rien perdre — le dictionnaire complet reste dans
`raw`, que le pipeline archive tel quel.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from geometry.base import GeometryResult

REPO_ROOT = Path(__file__).resolve().parents[1]


def result_from_status(status: Mapping[str, Any], backend: str) -> GeometryResult:
    """Normalise le compte rendu de `parametric_driver.drive()`."""
    return GeometryResult(
        success=bool(status.get("success")),
        message=str(
            status.get("error_message")
            or f"géométrie produite pour l'itération {status.get('iteration')}"
        ),
        status=str(status.get("status") or ""),
        stl_path=_resolve(status.get("stl_path")),
        step_path=_resolve(status.get("step_path")),
        profile_coordinates=_coordinates(status),
        backend=backend,
        warnings=list(status.get("warnings") or []),
        geometry=status.get("geometry"),
        raw=dict(status),
    )


def _resolve(value: Any) -> Path | None:
    """Rend un chemin absolu et existant, ou rien.

    Le driver écrit des chemins relatifs au dépôt quand il le peut. Un
    consommateur de l'interface ne devrait pas avoir à deviner par rapport à
    quoi ; et un chemin qui ne correspond à aucun fichier vaut mieux absent
    qu'affiché — le mode simulation en annonce, par exemple, sans rien écrire.
    """
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path if path.is_file() else None


def _coordinates(status: Mapping[str, Any]) -> list[tuple[float, float]] | None:
    """Contour du profil en mètres, extrados puis intrados inversé.

    Le contour est RECALCULÉ à partir des paramètres que le driver rapporte
    avoir appliqués, plutôt que lu dans son compte rendu : celui-ci ne porte
    qu'un résumé — corde, épaisseur, emprise — et y ajouter cent soixante
    points alourdirait le `fusion_status.json` de chaque itération pour un
    besoin qui n'existe qu'ici. Le recalcul repart des mêmes valeurs et de la
    même fonction de profil : il donne exactement la forme produite.

    Fermé et parcouru dans un seul sens, ce contour s'utilise directement pour
    un tracé ou un import CAO.

    Le recalcul reprend aussi les ordonnées de bord de fuite que le driver a
    rapportées. Elles décrivent la forme sans être des variables d'optimisation
    — le contrat des paramètres exige `min < max`, ce qui interdit d'y loger
    une grandeur figée — et sans elles un profil à bord de fuite ouvert serait
    recalculé fermé, donc faux.
    """
    applied = status.get("applied_parameters")
    if not isinstance(applied, Mapping) or not applied:
        return None

    parameters = {
        name: {"value": spec.get("requested_value"), "unit": spec.get("unit")}
        for name, spec in applied.items()
        if isinstance(spec, Mapping) and spec.get("requested_value") is not None
    }

    geometry = status.get("geometry")
    provenance = {}
    if isinstance(geometry, Mapping):
        provenance = {
            key: geometry[key]
            for key in ("trailing_edge_upper", "trailing_edge_lower")
            if key in geometry
        }

    try:
        from fusion.parametric_driver import profile_from_parameters

        plan = profile_from_parameters(parameters, provenance=provenance)
    except Exception:
        # Une forme dont le contour n'est pas calculable ici reste une
        # géométrie valide : le STL, lui, a bien été écrit. On rend simplement
        # les coordonnées indisponibles.
        return None

    upper = plan["profile"]["upper"]
    lower = plan["profile"]["lower"]
    contour = [(x / 100.0, y / 100.0) for x, y in upper]
    contour += [(x / 100.0, y / 100.0) for x, y in reversed(lower)]
    return contour
