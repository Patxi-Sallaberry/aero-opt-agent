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
    ERROR_FLOOR,
    CSTProfile,
    CSTSurface,
    ReconstructionError,
    cosine_stations,
    distance_to_curve,
    fit_profile,
    fit_surface,
    reconstruction_error,
)
from profiles.geometry import (  # noqa: F401
    CST_PROFILE_POINTS,
    ContourError,
    collect_coefficients,
    cst_contour,
    cst_measures,
    cst_profile,
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
from profiles.roundtrip import (  # noqa: F401
    ROUNDTRIP_TOLERANCE,
    RoundTripReport,
    check_roundtrip,
    extract_section,
    reference_contour,
)
from profiles.validation import ValidationReport, validate_profile  # noqa: F401

__all__ = [
    "CST_PROFILE_POINTS",
    "DEFAULT_ORDER",
    "ERROR_FLOOR",
    "ROUNDTRIP_TOLERANCE",
    "ContourError",
    "CSTProfile",
    "CSTSurface",
    "FORMAT_CSV",
    "ReconstructionError",
    "RoundTripReport",
    "check_roundtrip",
    "collect_coefficients",
    "cst_contour",
    "cst_measures",
    "cst_profile",
    "distance_to_curve",
    "extract_section",
    "reference_contour",
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
