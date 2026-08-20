"""Producteur de géométrie Fusion 360 (Master Doc §2 B).

Met à jour les User Parameters du modèle, obtient la géométrie, exporte STEP
et STL. C'est la voie à préférer quand on veut un vrai modèle CAO — et donc
celle qui rend le retour vers Fusion (§5) immédiat, puisqu'elle produit
directement un fichier que Fusion sait rouvrir.

**Elle exige que le processus tourne DANS Fusion** : l'API `adsk` n'a pas de
mode headless. `available()` le vérifie, ce qui permet à `auto` de retomber sur
le calculateur interne sans échouer au bout de cinq minutes de calcul.

Comme le backend interne, il ne réimplémente rien : il appelle le driver de la
v1.0 dans son mode Fusion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from fusion import parametric_driver as driver

from geometry.base import GeometryBackend, GeometryResult, register_backend
from geometry.common import result_from_status
from geometry.internal_backend import _config_path


@register_backend
class FusionBackend(GeometryBackend):
    """Géométrie produite par Fusion 360, avec export STEP et STL."""

    name = "fusion"
    description = (
        "modèle CAO Fusion 360, exports STEP et STL — exige que le script "
        "tourne dans Fusion (l'API n'a pas de mode headless)"
    )

    @classmethod
    def available(cls) -> bool:
        return bool(driver.FUSION_AVAILABLE)

    def generate(
        self,
        design_params: Mapping[str, Any] | Path | str,
        output_dir: Path,
        **options: Any,
    ) -> GeometryResult:
        status = driver.drive(
            config_path=_config_path(design_params, output_dir),
            iterations_root=Path(output_dir).parent,
            geometry_backend=driver.GEOMETRY_BACKEND_FUSION,
            geometry_mode=options.get("geometry_mode"),
            dry_run=bool(options.get("dry_run", False)),
            app=options.get("app"),
        )
        return result_from_status(status, self.name)
