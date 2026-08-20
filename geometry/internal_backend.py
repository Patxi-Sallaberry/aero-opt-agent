"""Producteur de géométrie interne — calcul direct, sans CAO (Master Doc §2 A).

Toujours disponible : il ne dépend que de Python. C'est le chemin fiable par
défaut, et celui qui rend la boucle d'optimisation autonome, l'API Fusion
n'ayant pas de mode headless.

Il ne réimplémente rien : il appelle `fusion.parametric_driver.drive()` dans
son mode interne, déjà éprouvé par la v1.0. L'interface normalise la sortie ;
la production de la forme, elle, reste au même endroit qu'avant — c'est ce qui
garantit que le comportement ne dérive pas au passage.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from fusion import parametric_driver as driver

from geometry.base import GeometryBackend, GeometryResult, register_backend
from geometry.common import result_from_status


@register_backend
class InternalBackend(GeometryBackend):
    """Profil calculé et écrit directement en STL, en mètres."""

    name = "internal"
    description = (
        "calcul direct du profil et écriture du STL, sans CAO — toujours "
        "disponible"
    )

    @classmethod
    def available(cls) -> bool:
        # Aucun outil externe : le calcul est en Python pur.
        return True

    def generate(
        self,
        design_params: Mapping[str, Any] | Path | str,
        output_dir: Path,
        **options: Any,
    ) -> GeometryResult:
        status = driver.drive(
            config_path=_config_path(design_params, output_dir),
            iterations_root=Path(output_dir).parent,
            geometry_backend=driver.GEOMETRY_BACKEND_INTERNAL,
            dry_run=bool(options.get("dry_run", False)),
        )
        return result_from_status(status, self.name)


def _config_path(
    design_params: Mapping[str, Any] | Path | str, output_dir: Path
) -> Path:
    """Chemin d'un `design_params.yaml` à donner au driver.

    L'interface accepte un dictionnaire ; le driver, lui, travaille sur un
    fichier — qu'il valide et journalise. Un dictionnaire est donc matérialisé
    à côté de la géométrie, ce qui a l'avantage de laisser dans le dossier de
    sortie la configuration exacte ayant servi.
    """
    if isinstance(design_params, (str, Path)):
        return Path(design_params)

    from pipeline.utils import save_design_params

    target = Path(output_dir) / "design_params.yaml"
    save_design_params(design_params, target)
    return target
