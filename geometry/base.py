"""Interface commune aux producteurs de géométrie (Master Doc v1.5 §2).

Tout ce qui est en aval — CFD, optimiseur, rapport — ne parle qu'à cette
interface. Un backend est une façon d'obtenir une géométrie à partir de
`design_params.yaml` : par un noyau CAO, par un calcul direct, ou demain par
autre chose. Le reste du système n'a pas à savoir laquelle.

Ajouter un backend tient en trois gestes :

    from geometry import GeometryBackend, GeometryResult, register_backend

    class MonBackend(GeometryBackend):
        name = "mon_backend"

        @classmethod
        def available(cls) -> bool:
            return True                     # les outils nécessaires sont là ?

        def generate(self, design_params, output_dir):
            ...
            return GeometryResult(success=True, stl_path=..., message="...")

    register_backend(MonBackend)

Il devient alors sélectionnable par configuration, sans qu'aucune ligne du
pipeline ne change.

DEUX ÉCARTS ASSUMÉS au texte du Master Document, tous deux motivés :

1. `profile_coordinates` est une liste de couples, pas un `np.ndarray`. Le
   projet n'a aucune dépendance à numpy, et le driver s'exécute dans
   l'interpréteur embarqué de Fusion où l'on ne peut rien installer. Ajouter
   numpy pour un type de retour coûterait cette contrainte pour rien. Si
   l'ajustement CST de la Phase 3 le réclame, il le fera dans le module
   d'ingestion, qui tourne hors de Fusion.

2. Les coordonnées sont en **mètres**, comme le STL. Le millimètre reste la
   convention des exports CAO (`profile_section.csv`), mais une interface
   programmatique gagne à rester en unités SI, et la conversion appartient à
   celui qui exporte.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

# Noms de configuration reconnus.
BACKEND_AUTO = "auto"

# Ordre de préférence quand la configuration dit "auto" : le premier backend
# disponible gagne. Fusion vient en tête parce qu'il produit un vrai modèle
# CAO ; le calculateur interne ferme la marche car il est toujours disponible.
AUTO_PREFERENCE = ("fusion", "internal")


@dataclass
class GeometryResult:
    """Ce qu'un backend rend au pipeline.

    `raw` conserve le compte rendu complet du producteur. Le pipeline
    l'archive tel quel : c'est ce qui permet de remonter à ce qui s'est passé
    dans le backend sans que l'interface ait à modéliser chacun de leurs
    détails.
    """

    success: bool
    message: str = ""
    status: str = ""
    stl_path: Path | None = None
    step_path: Path | None = None
    profile_coordinates: list[tuple[float, float]] | None = None
    backend: str = ""
    warnings: list[str] = field(default_factory=list)
    geometry: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_cad(self) -> bool:
        """Vrai si un modèle CAO exploitable a été produit.

        Le retour vers Fusion (§5) en dépend : sans STEP, le chemin de retour
        passe par les coordonnées du profil plutôt que par un fichier CAO.
        """
        return self.step_path is not None and Path(self.step_path).is_file()


class GeometryBackend(ABC):
    """Producteur de géométrie.

    Une instance est bon marché : elle ne porte pas d'état entre deux
    générations. Toute la configuration passe par `generate`.
    """

    #: Nom sous lequel le backend est sélectionnable en configuration.
    name: str = ""

    #: Description courte, affichée dans les diagnostics.
    description: str = ""

    @classmethod
    def available(cls) -> bool:
        """Le backend peut-il tourner ici et maintenant ?

        C'est ce qui permet à `auto` de choisir sans essayer puis échouer :
        interroger avant de lancer un maillage évite de découvrir l'absence de
        Fusion après cinq minutes de calcul.
        """
        return True

    @abstractmethod
    def generate(
        self,
        design_params: Mapping[str, Any] | Path | str,
        output_dir: Path,
        **options: Any,
    ) -> GeometryResult:
        """Produit la géométrie décrite par `design_params` dans `output_dir`.

        Args:
            design_params: la configuration, ou le chemin d'un
                `design_params.yaml`. Les deux formes sont acceptées parce que
                le pipeline travaille sur un fichier — qu'il archive — quand
                l'ingestion et les tests manipulent des dictionnaires.
            output_dir: où écrire géométrie, journal et compte rendu.
            **options: réglages propres au backend.

        Returns:
            Un `GeometryResult`. **Ne lève pas** : un échec attendu est un
            résultat, pas une exception — c'est ce qui permet à la boucle
            d'optimisation d'archiver l'itération ratée et de continuer.
        """

    def __repr__(self) -> str:  # pragma: no cover - confort de débogage
        return f"<{type(self).__name__} name={self.name!r}>"


# ─────────────────────────────────────────────────────────────────────────────
# Registre
# ─────────────────────────────────────────────────────────────────────────────

_REGISTRY: dict[str, type[GeometryBackend]] = {}


class UnknownBackend(Exception):
    """Nom de backend inconnu du registre."""


class NoBackendAvailable(Exception):
    """Aucun backend utilisable ici."""


def register_backend(backend: type[GeometryBackend]) -> type[GeometryBackend]:
    """Enregistre un backend. Utilisable comme décorateur."""
    if not backend.name:
        raise ValueError(f"{backend.__name__} doit définir un attribut `name`")
    _REGISTRY[backend.name] = backend
    return backend


def backend_names() -> list[str]:
    """Noms enregistrés, dans l'ordre alphabétique."""
    return sorted(_REGISTRY)


def configuration_choices() -> list[str]:
    """Valeurs acceptables pour `geometry_backend`, `auto` compris."""
    return [BACKEND_AUTO] + backend_names()


def available_backends() -> list[str]:
    """Noms des backends réellement utilisables sur cette machine."""
    return [name for name in backend_names() if _REGISTRY[name].available()]


def resolve(name: str | None = None) -> str:
    """Traduit une valeur de configuration en nom de backend concret.

    `auto` retient le premier disponible selon `AUTO_PREFERENCE`, puis
    n'importe quel autre backend enregistré qui se déclare disponible — de
    sorte qu'un backend ajouté plus tard soit pris en compte sans toucher à
    cette fonction.
    """
    wanted = (name or BACKEND_AUTO).strip().lower()

    if wanted != BACKEND_AUTO:
        if wanted not in _REGISTRY:
            raise UnknownBackend(
                f"backend de géométrie inconnu : {wanted!r} — attendu "
                f"{configuration_choices()}"
            )
        return wanted

    for candidate in AUTO_PREFERENCE:
        if candidate in _REGISTRY and _REGISTRY[candidate].available():
            return candidate
    for candidate in backend_names():
        if _REGISTRY[candidate].available():
            return candidate

    raise NoBackendAvailable(
        "aucun producteur de géométrie disponible — enregistrés : "
        f"{backend_names()}"
    )


def get_backend(name: str | None = None, **kwargs: Any) -> GeometryBackend:
    """Instancie le backend désigné (ou celui que `auto` retient)."""
    return _REGISTRY[resolve(name)](**kwargs)


def describe_backends() -> list[dict[str, Any]]:
    """État de chaque backend, pour les diagnostics et les rapports."""
    return [
        {
            "name": name,
            "available": _REGISTRY[name].available(),
            "description": _REGISTRY[name].description,
        }
        for name in backend_names()
    ]
