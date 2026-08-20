"""Ingestion de profils 2D (Master Doc v1.5 §3, Mode 2).

    from profiles import load_profile, validate_profile

    ingestion = load_profile("naca2412.dat")
    if ingestion.success:
        rapport = validate_profile(ingestion.profile)

Charge un fichier de coordonnées — Selig, Lednicer ou CSV —, le nettoie, le
normalise, et dit s'il décrit un profil exploitable. Ce que la Phase 3 y
ajoutera : l'ajustement d'un modèle paramétrique et l'écriture du
`design_params.yaml` initial.

Rien ne lève : le chargement comme la validation rendent un compte rendu, à
l'image de `GeometryBackend.generate`. Un fichier douteux ne doit pas
interrompre une boucle d'optimisation.
"""

from profiles.cst import (  # noqa: F401
    DEFAULT_ORDER,
    CSTProfile,
    CSTSurface,
    ReconstructionError,
    cosine_stations,
    fit_profile,
    fit_surface,
    reconstruction_error,
)
from profiles.loader import (  # noqa: F401
    FORMAT_CSV,
    FORMAT_LEDNICER,
    FORMAT_SELIG,
    FORMAT_UNKNOWN,
    IngestionResult,
    detect_format,
    load_profile,
)
from profiles.profile import Profile, ProfileTransform  # noqa: F401
from profiles.reparameterize import (  # noqa: F401
    ReparameterizationResult,
    build_design_params,
    check_reconstruction,
    reparameterize,
)
from profiles.validation import ValidationReport, validate_profile  # noqa: F401

__all__ = [
    "DEFAULT_ORDER",
    "CSTProfile",
    "CSTSurface",
    "FORMAT_CSV",
    "ReconstructionError",
    "ReparameterizationResult",
    "build_design_params",
    "check_reconstruction",
    "cosine_stations",
    "fit_profile",
    "fit_surface",
    "reconstruction_error",
    "reparameterize",
    "FORMAT_LEDNICER",
    "FORMAT_SELIG",
    "FORMAT_UNKNOWN",
    "IngestionResult",
    "Profile",
    "ProfileTransform",
    "ValidationReport",
    "detect_format",
    "load_profile",
    "validate_profile",
]
