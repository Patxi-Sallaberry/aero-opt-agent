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
        config_path = _config_path(design_params, output_dir)
        status = driver.drive(
            config_path=config_path,
            iterations_root=Path(output_dir).parent,
            geometry_backend=driver.GEOMETRY_BACKEND_INTERNAL,
            dry_run=bool(options.get("dry_run", False)),
        )
        result = result_from_status(status, self.name)
        if result.success and options.get("step", True):
            _add_step(result, config_path, Path(output_dir))
        return result


def _add_step(
    result: GeometryResult, config_path: Path, output_dir: Path
) -> None:
    """Ajoute un STEP au résultat quand un noyau CAO est disponible.

    Le STL suffit à la CFD, mais pas à la conception : c'est un maillage de
    facettes planes qu'aucune CAO ne sait recoter. Le STEP, lui, décrit des
    SURFACES — le profil y devient une face réglée unique, et Fusion l'ouvre
    comme un solide natif, éditable.

    Silencieux si le noyau n'est pas installé : c'est une dépendance
    facultative de deux gigaoctets, et le système doit tourner sans elle
    exactement comme avant. Un échec d'écriture ne fait pas échouer la
    géométrie non plus — le STL, lui, est bien là, et la CFD n'attend que lui.
    """
    from geometry.step_io import available, write_step

    if not available():
        return

    try:
        from pipeline.utils import load_yaml

        design = load_yaml(config_path)
        plan = driver.profile_from_parameters(
            design["parameters"],
            design.get("parameterization"),
            design.get("provenance"),
        )
    except Exception as exc:  # pragma: no cover - configuration déjà validée
        result.warnings.append(f"STEP non écrit : configuration illisible ({exc})")
        return

    # Le plan est en centimètres, la CAO travaille en millimètres.
    upper = [(x * 10.0, y * 10.0) for x, y in plan["profile"]["upper"]]
    lower = [(x * 10.0, y * 10.0) for x, y in plan["profile"]["lower"]]

    written = write_step(
        upper, lower, plan["span_cm"] * 10.0, output_dir / "geometry.step"
    )
    result.warnings.extend(written.warnings)
    if written.success:
        result.step_path = written.path
        result.raw["step_path"] = str(written.path)
        result.raw["step_faces"] = written.faces
        result.raw["step_volume_mm3"] = written.volume_mm3
    else:
        result.warnings.append(f"STEP non écrit : {written.message}")


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
