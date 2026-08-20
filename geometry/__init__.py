"""Producteurs de géométrie, derrière une interface unique (Master Doc v1.5 §2).

    from geometry import get_backend

    backend = get_backend("auto")            # ou "internal", ou "fusion"
    result = backend.generate(design_params, output_dir)
    if result.success:
        mailler(result.stl_path)

Le pipeline — CFD, optimiseur, rapport — ne connaît que `GeometryBackend` et
`GeometryResult`. Quel producteur travaille derrière est un choix de
configuration, et ajouter le prochain ne demande de toucher à aucun de ces
consommateurs.

Importer ce paquet enregistre les backends livrés. Un backend tiers s'ajoute
par `register_backend`.
"""

from geometry.base import (  # noqa: F401
    BACKEND_AUTO,
    GeometryBackend,
    GeometryResult,
    NoBackendAvailable,
    UnknownBackend,
    available_backends,
    backend_names,
    configuration_choices,
    describe_backends,
    get_backend,
    register_backend,
    resolve,
)

# L'import a pour effet d'enregistrer les backends : il n'est donc pas
# superflu, malgré les apparences.
from geometry import fusion_backend, internal_backend  # noqa: F401,E402
from geometry.fusion_backend import FusionBackend  # noqa: F401,E402
from geometry.internal_backend import InternalBackend  # noqa: F401,E402

__all__ = [
    "BACKEND_AUTO",
    "FusionBackend",
    "GeometryBackend",
    "GeometryResult",
    "InternalBackend",
    "NoBackendAvailable",
    "UnknownBackend",
    "available_backends",
    "backend_names",
    "configuration_choices",
    "describe_backends",
    "get_backend",
    "register_backend",
    "resolve",
]
