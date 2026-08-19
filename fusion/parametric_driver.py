"""Driver paramétrique Fusion 360 — Phase 1 (Master Document §3.2).

Rôle : appliquer les valeurs de `configs/design_params.yaml` au design Fusion,
obtenir la géométrie correspondante, puis l'exporter en STEP dans
`data/iterations/iter_XXXX/geometry.step`.

Ce script ne fait QUE cela. Il ne connaît ni OpenFOAM, ni l'agent, ni le master
pipeline, et il n'écrit jamais dans `configs/`.

─────────────────────────────────────────────────────────────────────────────
DEUX STRATÉGIES DE GÉOMÉTRIE
─────────────────────────────────────────────────────────────────────────────
`rebuild` (défaut) : les User Parameters sont mis à jour, PUIS la géométrie est
reconstruite depuis la configuration — profil NACA 4 chiffres retracé à la
corde, l'épaisseur et la cambrure demandées, tourné de l'incidence, extrudé sur
l'envergure. Nécessaire dès que le modèle n'est pas réellement piloté par ses
paramètres : c'est le cas du seed livré, dont le profil est une spline passant
par des points calculés en dur et dont l'extrusion est une longueur brute.
Modifier ses User Parameters n'y déplace pas un seul point — sans rebuild, le
STEP serait identique à chaque itération et l'agent optimiserait dans le vide.

`parameters` : mise à jour des User Parameters puis `computeAll()`. Réservé aux
modèles dont les cotes sont réellement contraintes par les paramètres.

Choix via `--geometry-mode` ou la variable FUSION_GEOMETRY_MODE.

─────────────────────────────────────────────────────────────────────────────
EXÉCUTION DANS FUSION 360
─────────────────────────────────────────────────────────────────────────────
Fusion appelle automatiquement `run(context)`. Deux façons de le lancer :

  - Utilities > ADD-INS > Scripts and Add-Ins > Scripts > (+) > ajouter ce
    fichier, puis Run ;
  - depuis le Text Commands window de Fusion.

Le document utilisé est, par ordre de préférence :
  1. le document actif, s'il contient un design paramétrique ;
  2. sinon, le seed `fusion/seed_design.f3d` importé dans un nouveau document.
Mettre FUSION_FORCE_SEED_IMPORT=1 pour toujours repartir du seed.

─────────────────────────────────────────────────────────────────────────────
EXÉCUTION HORS FUSION (mode simulation)
─────────────────────────────────────────────────────────────────────────────
Lancé par un interpréteur Python normal, le module `adsk` est absent : le driver
bascule alors en MODE SIMULATION. Il lit et valide la configuration, construit
les expressions Fusion et calcule les chemins de sortie, puis s'arrête avant
tout appel API. Utile pour vérifier une configuration sans ouvrir Fusion :

    python3 fusion/parametric_driver.py --dry-run
    python3 fusion/parametric_driver.py --dry-run --config configs/design_params.yaml

─────────────────────────────────────────────────────────────────────────────
STATUT RETOURNÉ
─────────────────────────────────────────────────────────────────────────────
`run()` retourne un dict et écrit le même contenu dans
`data/iterations/iter_XXXX/fusion_status.json` — car dans Fusion le script
s'exécute dans un autre processus que le pipeline, qui ne peut donc pas
récupérer une valeur de retour :

    {
      "success": true,
      "status": "OK",
      "iteration": 0,
      "design_id": "wing_v01",
      "step_path": "data/iterations/iter_0000/geometry.step",
      "applied_parameters": {"chord_mm": {"expression": "300 mm", ...}},
      "error_message": null,
      "warnings": []
    }

Variables d'environnement reconnues (voir .env.example) :
    DESIGN_PARAMS_PATH, ITERATIONS_DIR, FUSION_SEED_PATH,
    FUSION_FORCE_SEED_IMPORT, FUSION_GEOMETRY_MODE, LOG_LEVEL
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

# ─────────────────────────────────────────────────────────────────────────────
# API Fusion — absente hors de Fusion, d'où l'import gardé.
# ─────────────────────────────────────────────────────────────────────────────
try:  # pragma: no cover - dépend de l'hôte d'exécution
    import adsk.core
    import adsk.fusion

    FUSION_AVAILABLE = True
except ImportError:  # pragma: no cover
    adsk = None  # type: ignore[assignment]
    FUSION_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Constantes
# ─────────────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[1]

DEFAULT_CONFIG_PATH = REPO_ROOT / "configs" / "design_params.yaml"
DEFAULT_ITERATIONS_DIR = REPO_ROOT / "data" / "iterations"
DEFAULT_SEED_PATH = REPO_ROOT / "fusion" / "seed_design.f3d"

STEP_FILENAME = "geometry.step"
STATUS_FILENAME = "fusion_status.json"
LOG_FILENAME = "fusion_driver.log"

# Statuts possibles. Le pipeline (Phase 3) et l'agent (Phase 4) s'appuient sur
# ces chaînes : elles font partie du contrat, ne pas les renommer à la légère.
STATUS_OK = "OK"
STATUS_CONFIG_ERROR = "CONFIG_ERROR"
STATUS_FUSION_UNAVAILABLE = "FUSION_UNAVAILABLE"
STATUS_NO_DESIGN = "NO_DESIGN"
STATUS_SEED_MISSING = "SEED_MISSING"
STATUS_SEED_IMPORT_FAILED = "SEED_IMPORT_FAILED"
STATUS_PARAM_NOT_FOUND = "PARAM_NOT_FOUND"
STATUS_PARAM_SET_FAILED = "PARAM_SET_FAILED"
STATUS_RECOMPUTE_FAILED = "RECOMPUTE_FAILED"
STATUS_GEOMETRY_EMPTY = "GEOMETRY_EMPTY"
STATUS_REBUILD_FAILED = "REBUILD_FAILED"
STATUS_EXPORT_FAILED = "EXPORT_FAILED"
STATUS_DRY_RUN = "DRY_RUN"
STATUS_UNEXPECTED_ERROR = "UNEXPECTED_ERROR"

# ── Stratégie de géométrie ───────────────────────────────────────────────────
# "parameters" : on met à jour les User Parameters et on laisse Fusion
#                recalculer. N'a de sens que si le modèle est RÉELLEMENT
#                paramétrique (cotes contraintes par les paramètres).
# "rebuild"    : on met à jour les User Parameters (le contrat §3.2 reste
#                honoré, et un humain qui ouvre le fichier voit les bonnes
#                valeurs) PUIS on reconstruit la géométrie à partir de
#                design_params.yaml.
#
# Le seed livré exige "rebuild" : son profil est une spline passant par des
# points calculés en dur, et son extrusion une longueur brute — modifier les
# User Parameters n'y déplace pas un seul point. C'est donc le défaut.
GEOMETRY_MODE_PARAMETERS = "parameters"
GEOMETRY_MODE_REBUILD = "rebuild"
GEOMETRY_MODES = (GEOMETRY_MODE_REBUILD, GEOMETRY_MODE_PARAMETERS)
DEFAULT_GEOMETRY_MODE = GEOMETRY_MODE_REBUILD

# ── Reconstruction du profil (mode "rebuild") ────────────────────────────────
# Paramètres de FORME, propriété du concepteur : ils ne sont ni dans
# design_params.yaml (l'agent n'a pas à y toucher) ni dans cfd_settings.yaml
# (ce n'est pas de la CFD). Les changer change la famille de profil.
NACA_CAMBER_POSITION = 0.4      # p — position de la cambrure max (NACA 24xx)
NACA_PROFILE_POINTS = 80        # points par surface : compromis fidélité/poids

REBUILD_PARAMETERS = ("chord", "thickness", "camber", "span", "aoa")

# Noms posés sur ce que le driver crée, pour pouvoir le retrouver et le
# supprimer à l'itération suivante.
REBUILD_SKETCH_NAME = "AERO_OPT_PROFILE"
REBUILD_BODY_NAME = "AERO_OPT_WING"
REBUILD_ATTRIBUTE_GROUP = "aeroOpt"
REBUILD_ATTRIBUTE_NAME = "generated"

# Géométrie du seed d'origine (script NACA_Parametric) : à purger au premier
# rebuild, sinon elle cohabiterait avec la nouvelle et le STEP contiendrait
# deux ailes.
LEGACY_GEOMETRY_NAMES = ("NACA_2412_Profile", "Wing_Section")

# Conversions vers les unités internes de Fusion (cm et radians).
LENGTH_TO_CM: dict[str, float] = {
    "mm": 0.1,
    "cm": 1.0,
    "m": 100.0,
    "in": 2.54,
    "ft": 30.48,
}
ANGLE_TO_RAD: dict[str, float] = {
    "deg": math.pi / 180.0,
    "rad": 1.0,
}

# Unités que Fusion accepte comme suffixe d'expression. La clé est ce qui peut
# être écrit dans le YAML, la valeur ce qui est envoyé à Fusion.
UNIT_ALIASES: dict[str, str] = {
    "mm": "mm",
    "millimeter": "mm",
    "millimetre": "mm",
    "cm": "cm",
    "centimeter": "cm",
    "m": "m",
    "meter": "m",
    "metre": "m",
    "in": "in",
    "inch": "in",
    "ft": "ft",
    "foot": "ft",
    "deg": "deg",
    "degree": "deg",
    "degrees": "deg",
    "rad": "rad",
    "radian": "rad",
}

# Valeurs signalant un paramètre sans dimension : l'expression est alors un
# nombre nu (un ratio, un compte...).
UNITLESS_TOKENS = frozenset({"", "-", "none", "unitless", "ratio", "count", "nd"})

# Tolérance relative pour vérifier que Fusion a bien encaissé la valeur
# demandée après évaluation de l'expression.
VALUE_TOL_REL = 1e-6
VALUE_TOL_ABS = 1e-9

LOGGER_NAME = "fusion.parametric_driver"
logger = logging.getLogger(LOGGER_NAME)


class DriverError(Exception):
    """Échec attendu du driver, porteur d'un statut du contrat ci-dessus."""

    def __init__(self, status: str, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.details = details


# ─────────────────────────────────────────────────────────────────────────────
# Journalisation
# ─────────────────────────────────────────────────────────────────────────────


class _FusionLogHandler(logging.Handler):
    """Renvoie les logs vers la console texte de Fusion.

    Volontairement non bloquant : aucune boîte de dialogue, sans quoi une
    boucle d'optimisation automatique resterait figée sur un clic.
    """

    def __init__(self, app: Any) -> None:
        super().__init__()
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover
        try:
            self._app.log(self.format(record))
        except Exception:
            pass  # un log qui casse ne doit jamais casser le driver


def setup_logging(log_file: Path | None = None, app: Any = None) -> logging.Logger:
    """Configure le logger du driver : stderr, + fichier d'itération, + Fusion.

    Rejoué à chaque appel de `run()` (les handlers précédents sont retirés),
    car le fichier de log change à chaque itération.
    """
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file, mode="a", encoding="utf-8")
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except OSError as exc:
            logger.warning("Journal fichier indisponible (%s) : %s", log_file, exc)

    if app is not None:  # pragma: no cover - uniquement dans Fusion
        fusion_handler = _FusionLogHandler(app)
        fusion_handler.setFormatter(logging.Formatter("[aero-opt] %(message)s"))
        logger.addHandler(fusion_handler)

    return logger


# ─────────────────────────────────────────────────────────────────────────────
# Lecture de la configuration
# ─────────────────────────────────────────────────────────────────────────────


def _parse_scalar(token: str) -> Any:
    """Convertit un scalaire YAML (sous-ensemble) en objet Python."""
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    lowered = token.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "~", ""):
        return None
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        pass
    return token


def parse_simple_yaml(text: str) -> dict:
    """Lecteur YAML minimal, replet de limites assumées.

    Fusion 360 embarque son propre interpréteur Python sans PyYAML, et on ne
    peut pas y faire de `pip install`. Ce lecteur couvre exactement la forme de
    `design_params.yaml` : mappings imbriqués, scalaires, commentaires.

    Il ne gère ni les listes, ni les ancres, ni les chaînes multilignes, ni les
    documents multiples, et lève DriverError s'il en rencontre — mieux vaut un
    refus net qu'une lecture silencieusement fausse. Dès que PyYAML est
    disponible, c'est lui qui est utilisé (voir `load_config`).
    """
    root: dict = {}
    # Pile de (indentation, mapping) ; le sommet reçoit les clés courantes.
    stack: list[tuple[int, dict]] = [(-1, root)]

    for lineno, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if line.lstrip().startswith("- "):
            raise DriverError(
                STATUS_CONFIG_ERROR,
                f"ligne {lineno} : listes YAML non supportées par le lecteur "
                f"minimal — installer PyYAML dans l'environnement Fusion",
            )
        if line.strip() in ("---", "..."):
            raise DriverError(
                STATUS_CONFIG_ERROR,
                f"ligne {lineno} : documents YAML multiples non supportés",
            )

        indent = len(line) - len(line.lstrip())
        if ":" not in line:
            raise DriverError(
                STATUS_CONFIG_ERROR,
                f"ligne {lineno} : '{raw_line.strip()}' n'est pas une paire clé: valeur",
            )
        key, _, rest = line.strip().partition(":")
        key = key.strip()
        if not key:
            raise DriverError(STATUS_CONFIG_ERROR, f"ligne {lineno} : clé vide")

        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack:
            raise DriverError(
                STATUS_CONFIG_ERROR, f"ligne {lineno} : indentation incohérente"
            )
        parent = stack[-1][1]

        if rest.strip() == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _parse_scalar(rest)

    return root


def load_config(config_path: Path) -> dict:
    """Charge et valide `design_params.yaml`.

    Deux chemins, dans cet ordre :

    1. PyYAML + `pipeline.utils` disponibles (cas normal hors Fusion, et dans
       Fusion si PyYAML y est installé) : lecture PyYAML et validation
       COMPLÈTE de la Phase 0 — structure, bornes, cohérence des unités.
    2. Repli embarqué : lecteur YAML minimal + contrôles réduits à ce dont le
       driver dépend réellement (noms, valeurs numériques, bornes). On ne
       construit jamais une géométrie hors bornes, même en mode dégradé.
    """
    if not config_path.is_file():
        raise DriverError(
            STATUS_CONFIG_ERROR, f"configuration introuvable : {config_path}"
        )

    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise DriverError(
            STATUS_CONFIG_ERROR, f"lecture impossible de {config_path} : {exc}"
        ) from exc

    data: dict
    try:
        import yaml  # type: ignore[import-untyped]

        loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise DriverError(
                STATUS_CONFIG_ERROR,
                f"{config_path} : la racine doit être un mapping",
            )
        data = loaded
        parser_used = "PyYAML"
    except ImportError:
        data = parse_simple_yaml(text)
        parser_used = "lecteur minimal embarqué"
    except Exception as exc:  # yaml.YAMLError et dérivés
        if isinstance(exc, DriverError):
            raise
        raise DriverError(
            STATUS_CONFIG_ERROR, f"{config_path} : YAML illisible — {exc}"
        ) from exc

    validator_used = _validate_config(data, config_path)
    logger.info(
        "Configuration chargée : %s (%s, validation %s)",
        config_path,
        parser_used,
        validator_used,
    )
    return data


def _validate_config(data: Mapping[str, Any], config_path: Path) -> str:
    """Valide la configuration ; retourne le nom du validateur employé."""
    try:
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from pipeline.utils import (  # type: ignore[import-not-found]
            validate_design_params,
        )
    except ImportError:
        _validate_config_minimal(data, config_path)
        return "minimale (pipeline.utils indisponible)"

    try:
        report = validate_design_params(data, path=config_path)
    except Exception as exc:  # pragma: no cover - garde-fou
        raise DriverError(
            STATUS_CONFIG_ERROR, f"validation impossible : {exc}"
        ) from exc

    for warning in report.warnings:
        logger.warning("Configuration : %s", warning)
    if not report.ok:
        raise DriverError(
            STATUS_CONFIG_ERROR,
            f"{config_path} viole le contrat design_params : "
            + " | ".join(report.errors),
            details=report.errors,
        )
    return "complète (pipeline.utils)"


def _validate_config_minimal(data: Mapping[str, Any], config_path: Path) -> None:
    """Contrôles minimaux quand la validation Phase 0 n'est pas importable.

    Volontairement restreint à ce dont le driver a besoin pour ne pas produire
    une géométrie fausse : identité de l'itération, valeurs numériques, unités
    exploitables, et surtout respect des bornes min/max.
    """
    errors: list[str] = []

    iteration = data.get("iteration")
    if not isinstance(iteration, int) or isinstance(iteration, bool) or iteration < 0:
        errors.append(f"iteration : entier >= 0 attendu, obtenu {iteration!r}")

    design_id = data.get("design_id")
    if not isinstance(design_id, str) or not design_id.strip():
        errors.append(f"design_id : chaîne non vide attendue, obtenu {design_id!r}")

    params = data.get("parameters")
    if not isinstance(params, dict) or not params:
        errors.append("parameters : mapping non vide attendu")
    else:
        for name, spec in params.items():
            if not isinstance(spec, dict):
                errors.append(f"parameters.{name} : mapping attendu")
                continue
            numeric_ok = True
            for key in ("value", "min", "max"):
                val = spec.get(key)
                if (
                    isinstance(val, bool)
                    or not isinstance(val, (int, float))
                    or not math.isfinite(float(val))
                ):
                    errors.append(
                        f"parameters.{name}.{key} : nombre fini attendu, obtenu {val!r}"
                    )
                    numeric_ok = False
            unit = spec.get("unit")
            if not isinstance(unit, str):
                errors.append(f"parameters.{name}.unit : chaîne attendue, obtenu {unit!r}")
            if numeric_ok:
                value, vmin, vmax = (
                    float(spec["value"]),
                    float(spec["min"]),
                    float(spec["max"]),
                )
                if vmin >= vmax:
                    errors.append(
                        f"parameters.{name} : min ({vmin:g}) doit être < max ({vmax:g})"
                    )
                if not (vmin - 1e-9 <= value <= vmax + 1e-9):
                    errors.append(
                        f"parameters.{name} : value {value:g} hors bornes "
                        f"[{vmin:g}, {vmax:g}]"
                    )

    if errors:
        raise DriverError(
            STATUS_CONFIG_ERROR,
            f"{config_path} viole le contrat design_params : " + " | ".join(errors),
            details=errors,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Expressions et chemins
# ─────────────────────────────────────────────────────────────────────────────


def normalize_unit(unit: Any) -> str | None:
    """Traduit l'unité du YAML en suffixe Fusion. None = grandeur sans unité."""
    if unit is None:
        return None
    text = str(unit).strip()
    if text.lower() in UNITLESS_TOKENS:
        return None
    resolved = UNIT_ALIASES.get(text) or UNIT_ALIASES.get(text.lower())
    if resolved is None:
        raise DriverError(
            STATUS_CONFIG_ERROR,
            f"unité inconnue : {unit!r} — attendu l'une de "
            f"{sorted(set(UNIT_ALIASES.values()))} ou une valeur sans dimension "
            f"({sorted(t for t in UNITLESS_TOKENS if t)})",
        )
    return resolved


def format_number(value: float) -> str:
    """Formate un nombre pour Fusion : séparateur décimal '.', pas de notation
    scientifique parasite, précision suffisante pour la CAO."""
    # Les booléens sont des int en Python : les laisser passer transformerait
    # une faute de frappe du YAML en expression Fusion « 1 mm ».
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DriverError(
            STATUS_CONFIG_ERROR, f"valeur numérique attendue, obtenu {value!r}"
        )
    number = float(value)
    if not math.isfinite(number):
        raise DriverError(STATUS_CONFIG_ERROR, f"valeur non finie : {value!r}")
    text = f"{number:.10f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-") else "0"


def build_expression(value: float, unit: Any) -> str:
    """Construit l'expression Fusion, par exemple `300 mm`, `-1.5 deg`, `2.5`."""
    resolved = normalize_unit(unit)
    number = format_number(value)
    return number if resolved is None else f"{number} {resolved}"


def iteration_dir(iteration: int, iterations_root: Path) -> Path:
    """Répertoire d'une itération : `data/iterations/iter_0007`."""
    if isinstance(iteration, bool) or not isinstance(iteration, int) or iteration < 0:
        raise DriverError(
            STATUS_CONFIG_ERROR, f"numéro d'itération invalide : {iteration!r}"
        )
    return iterations_root / f"iter_{iteration:04d}"


def _relative_to_repo(path: Path) -> str:
    """Chemin relatif au dépôt quand c'est possible, absolu sinon."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _env_path(name: str, default: Path) -> Path:
    """Lit un chemin depuis l'environnement ; relatif = relatif au dépôt."""
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    candidate = Path(raw.strip()).expanduser()
    return candidate if candidate.is_absolute() else (REPO_ROOT / candidate)


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def resolve_geometry_mode(requested: str | None = None) -> str:
    """Détermine la stratégie de géométrie (argument > environnement > défaut)."""
    mode = (requested or os.environ.get("FUSION_GEOMETRY_MODE") or "").strip().lower()
    if not mode:
        return DEFAULT_GEOMETRY_MODE
    if mode not in GEOMETRY_MODES:
        raise DriverError(
            STATUS_CONFIG_ERROR,
            f"mode de géométrie inconnu : {mode!r} — attendu {list(GEOMETRY_MODES)}",
        )
    return mode


# ─────────────────────────────────────────────────────────────────────────────
# Reconstruction de la géométrie (mode "rebuild")
# ─────────────────────────────────────────────────────────────────────────────


def _spec(parameters: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    spec = parameters.get(name)
    if not isinstance(spec, dict):
        raise DriverError(
            STATUS_CONFIG_ERROR,
            f"le mode '{GEOMETRY_MODE_REBUILD}' exige le paramètre '{name}' dans "
            f"design_params.yaml — attendus : {list(REBUILD_PARAMETERS)}",
        )
    return spec


def to_cm(spec: Mapping[str, Any], name: str) -> float:
    """Convertit une longueur du YAML vers les cm internes de Fusion."""
    unit = normalize_unit(spec.get("unit"))
    if unit not in LENGTH_TO_CM:
        raise DriverError(
            STATUS_CONFIG_ERROR,
            f"parameters.{name} : longueur attendue, unité {spec.get('unit')!r} "
            f"reçue — attendu l'une de {sorted(LENGTH_TO_CM)}",
        )
    return float(spec["value"]) * LENGTH_TO_CM[unit]


def to_rad(spec: Mapping[str, Any], name: str) -> float:
    """Convertit un angle du YAML vers les radians internes de Fusion."""
    unit = normalize_unit(spec.get("unit"))
    if unit not in ANGLE_TO_RAD:
        raise DriverError(
            STATUS_CONFIG_ERROR,
            f"parameters.{name} : angle attendu, unité {spec.get('unit')!r} "
            f"reçue — attendu l'une de {sorted(ANGLE_TO_RAD)}",
        )
    return float(spec["value"]) * ANGLE_TO_RAD[unit]


def to_dimensionless(spec: Mapping[str, Any], name: str) -> float:
    """Lit un ratio ; refuse une unité, qui trahirait une erreur de modèle."""
    if normalize_unit(spec.get("unit")) is not None:
        raise DriverError(
            STATUS_CONFIG_ERROR,
            f"parameters.{name} : grandeur sans dimension attendue (ratio), "
            f"unité {spec.get('unit')!r} reçue",
        )
    return float(spec["value"])


def to_internal(spec: Mapping[str, Any], name: str) -> float:
    """Valeur attendue dans les unités internes de Fusion.

    Fusion stocke tout en centimètres et en radians ; une grandeur sans
    dimension est stockée telle quelle. C'est le référentiel de comparaison de
    `_verify_parameter` : il ne dépend ni de l'unité du document, ni de la
    sémantique — ambiguë — du second argument d'`evaluateExpression`.
    """
    unit = normalize_unit(spec.get("unit"))
    value = float(spec["value"])
    if unit is None:
        return value
    if unit in LENGTH_TO_CM:
        return value * LENGTH_TO_CM[unit]
    if unit in ANGLE_TO_RAD:
        return value * ANGLE_TO_RAD[unit]
    raise DriverError(
        STATUS_CONFIG_ERROR,
        f"parameters.{name} : unité '{unit}' non convertible en unités internes "
        f"Fusion — attendu une longueur {sorted(LENGTH_TO_CM)}, un angle "
        f"{sorted(ANGLE_TO_RAD)}, ou une grandeur sans dimension",
    )


def naca4_profile(
    chord_cm: float,
    thickness: float,
    camber: float,
    aoa_rad: float = 0.0,
    n_points: int = NACA_PROFILE_POINTS,
    camber_position: float = NACA_CAMBER_POSITION,
) -> dict[str, list[tuple[float, float]]]:
    """Points d'un profil NACA 4 chiffres, en centimètres, incidence appliquée.

    `thickness` (t/c) et `camber` (m/c) sont des ratios ; `chord_cm` porte
    l'échelle. L'incidence est appliquée analytiquement : les points sont
    tournés de -aoa autour du bord d'attaque (origine), l'écoulement restant
    dirigé selon +X. Une rotation calculée ici plutôt qu'une feature Move
    Fusion évite d'ajouter une entrée fragile dans la timeline à chaque
    itération.

    Returns:
        {"upper": [(x, y), ...], "lower": [...]} — extrados du bord d'attaque
        vers le bord de fuite, intrados en sens inverse, pour former un
        contour fermé.
    """
    if not chord_cm > 0:
        raise DriverError(STATUS_REBUILD_FAILED, f"corde non positive : {chord_cm}")
    if not thickness > 0:
        raise DriverError(
            STATUS_REBUILD_FAILED, f"épaisseur relative non positive : {thickness}"
        )
    if camber < 0:
        raise DriverError(STATUS_REBUILD_FAILED, f"cambrure négative : {camber}")
    if n_points < 10:
        raise DriverError(STATUS_REBUILD_FAILED, f"n_points trop faible : {n_points}")
    p = float(camber_position)
    if not 0.0 < p < 1.0:
        raise DriverError(
            STATUS_REBUILD_FAILED, f"position de cambrure hors ]0, 1[ : {p}"
        )

    cos_a, sin_a = math.cos(-aoa_rad), math.sin(-aoa_rad)

    def rotate(x: float, y: float) -> tuple[float, float]:
        return x * cos_a - y * sin_a, x * sin_a + y * cos_a

    upper: list[tuple[float, float]] = []
    lower: list[tuple[float, float]] = []

    for i in range(n_points + 1):
        xc = i / n_points
        x = chord_cm * xc

        # Distribution d'épaisseur (bord de fuite fermé).
        yt = (
            5.0
            * thickness
            * chord_cm
            * (
                0.2969 * math.sqrt(xc)
                - 0.1260 * xc
                - 0.3516 * xc**2
                + 0.2843 * xc**3
                - 0.1036 * xc**4
            )
        )

        # Ligne de cambrure. yc est mise à l'échelle de la corde, comme yt :
        # sans ce facteur, la cambrure serait ~1/chord fois trop faible et le
        # paramètre `camber` n'aurait quasiment aucun effet.
        if camber == 0.0:
            yc = 0.0
            dyc = 0.0
        elif xc < p:
            yc = chord_cm * camber * (xc / (p * p)) * (2.0 * p - xc)
            dyc = (2.0 * camber / (p * p)) * (p - xc)
        else:
            yc = (
                chord_cm
                * camber
                * ((1.0 - xc) / ((1.0 - p) ** 2))
                * (1.0 + xc - 2.0 * p)
            )
            dyc = (2.0 * camber / ((1.0 - p) ** 2)) * (p - xc)

        theta = math.atan(dyc)
        upper.append(rotate(x - yt * math.sin(theta), yc + yt * math.cos(theta)))
        lower.append(rotate(x + yt * math.sin(theta), yc - yt * math.cos(theta)))

    return {"upper": upper, "lower": lower}


def profile_from_parameters(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Traduit design_params.yaml en une description géométrique complète.

    Fonction pure : aucun appel Fusion. C'est elle qui est exécutée en mode
    simulation pour vérifier la reconstruction sans ouvrir Fusion.
    """
    missing = [n for n in REBUILD_PARAMETERS if n not in parameters]
    if missing:
        raise DriverError(
            STATUS_CONFIG_ERROR,
            f"le mode '{GEOMETRY_MODE_REBUILD}' exige les paramètres "
            f"{list(REBUILD_PARAMETERS)} ; manquant(s) : {missing}",
        )

    chord_cm = to_cm(_spec(parameters, "chord"), "chord")
    span_cm = to_cm(_spec(parameters, "span"), "span")
    thickness = to_dimensionless(_spec(parameters, "thickness"), "thickness")
    camber = to_dimensionless(_spec(parameters, "camber"), "camber")
    aoa_rad = to_rad(_spec(parameters, "aoa"), "aoa")

    if not span_cm > 0:
        raise DriverError(
            STATUS_REBUILD_FAILED, f"envergure non positive : {span_cm} cm"
        )

    profile = naca4_profile(chord_cm, thickness, camber, aoa_rad)
    xs = [x for x, _ in profile["upper"] + profile["lower"]]
    ys = [y for _, y in profile["upper"] + profile["lower"]]

    return {
        "profile": profile,
        "chord_cm": chord_cm,
        "span_cm": span_cm,
        "thickness": thickness,
        "camber": camber,
        "aoa_rad": aoa_rad,
        "aoa_deg": math.degrees(aoa_rad),
        "n_points": NACA_PROFILE_POINTS,
        "camber_position": NACA_CAMBER_POSITION,
        "bbox_cm": {
            "x_min": min(xs), "x_max": max(xs),
            "y_min": min(ys), "y_max": max(ys),
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Opérations Fusion
# ─────────────────────────────────────────────────────────────────────────────


def _get_design(app: Any) -> Any:  # pragma: no cover - nécessite Fusion
    """Retourne le Design Fusion à piloter (document actif, ou seed importé)."""
    seed_path = _env_path("FUSION_SEED_PATH", DEFAULT_SEED_PATH)
    force_seed = _env_flag("FUSION_FORCE_SEED_IMPORT")

    if not force_seed:
        product = app.activeProduct
        design = adsk.fusion.Design.cast(product)
        if design is not None:
            doc_name = getattr(app.activeDocument, "name", "(sans nom)")
            logger.info("Design actif utilisé : %s", doc_name)
            return design
        logger.info("Aucun design paramétrique actif — repli sur le seed.")

    if not seed_path.is_file():
        raise DriverError(
            STATUS_SEED_MISSING,
            f"seed Fusion introuvable : {seed_path}. Ouvrir le modèle dans "
            f"Fusion avant de lancer le script, ou déposer le fichier .f3d à "
            f"cet emplacement (voir FUSION_SEED_PATH dans .env).",
        )

    logger.info("Import du seed : %s", seed_path)
    try:
        import_manager = app.importManager
        options = import_manager.createFusionArchiveImportOptions(str(seed_path))
        document = import_manager.importToNewDocument(options)
        if document is None:
            raise DriverError(
                STATUS_SEED_IMPORT_FAILED,
                f"Fusion n'a pas pu importer {seed_path} (aucun document créé)",
            )
    except DriverError:
        raise
    except Exception as exc:
        raise DriverError(
            STATUS_SEED_IMPORT_FAILED,
            f"échec de l'import du seed {seed_path} : {exc}. Ouvrir le modèle "
            f"manuellement dans Fusion puis relancer le script.",
        ) from exc

    design = adsk.fusion.Design.cast(app.activeProduct)
    if design is None:
        raise DriverError(
            STATUS_NO_DESIGN,
            "le document importé ne contient pas de design paramétrique Fusion",
        )
    return design


def _apply_parameters(
    design: Any, parameters: Mapping[str, Any]
) -> tuple[dict[str, dict], list[str]]:  # pragma: no cover - nécessite Fusion
    """Applique toutes les valeurs aux User Parameters, ou aucune.

    En cas d'échec sur un paramètre, les expressions déjà modifiées sont
    restaurées : un design à moitié mis à jour produirait une géométrie qui ne
    correspond à aucune ligne de `design_params.yaml`, donc un résultat CFD
    impossible à interpréter.
    """
    user_params = design.userParameters
    warnings: list[str] = []

    # 1. Résolution des noms — tous les manquants sont signalés d'un coup.
    resolved: dict[str, Any] = {}
    missing: list[str] = []
    for name in parameters:
        param = user_params.itemByName(name)
        if param is None:
            missing.append(name)
        else:
            resolved[name] = param
    if missing:
        available = sorted(user_params.item(i).name for i in range(user_params.count))
        raise DriverError(
            STATUS_PARAM_NOT_FOUND,
            f"User Parameter(s) absent(s) du modèle Fusion : {missing}. "
            f"Paramètres disponibles : {available}. Corriger les noms dans "
            f"design_params.yaml ou dans Fusion (Modify > Change Parameters).",
            details={"missing": missing, "available": available},
        )

    # 2. Application, avec mémorisation pour rollback.
    original: dict[str, str] = {}
    applied: dict[str, dict] = {}
    try:
        for name, spec in parameters.items():
            param = resolved[name]
            expression = build_expression(spec["value"], spec.get("unit"))
            original[name] = param.expression
            previous_expression = param.expression

            try:
                param.expression = expression
            except Exception as exc:
                raise DriverError(
                    STATUS_PARAM_SET_FAILED,
                    f"Fusion a refusé l'expression '{expression}' pour le "
                    f"paramètre '{name}' : {exc}",
                ) from exc

            check = _verify_parameter(design, param, name, spec, expression)
            warnings.extend(check)
            applied[name] = {
                "expression": expression,
                "previous_expression": previous_expression,
                "requested_value": float(spec["value"]),
                "unit": spec.get("unit"),
                "fusion_unit": getattr(param, "unit", None),
            }
            logger.info(
                "Paramètre %-20s %s -> %s", name, previous_expression, expression
            )
    except Exception:
        _restore_parameters(user_params, original)
        raise

    return applied, warnings


def _verify_parameter(
    design: Any, param: Any, name: str, spec: Mapping[str, Any], expression: str
) -> list[str]:  # pragma: no cover - nécessite Fusion
    """Relit la valeur après affectation et signale tout écart.

    Fusion peut accepter une expression et l'interpréter dans une autre unité
    que celle déclarée : sans cette relecture, un design_params en millimètres
    appliqué à un paramètre en pouces passerait totalement inaperçu.

    La comparaison se fait dans les UNITÉS INTERNES de Fusion — centimètres,
    radians, et nombre nu pour les grandeurs sans dimension. C'est le seul
    référentiel non ambigu : `Parameter.value` est documenté comme rendant la
    valeur en unités internes, et `evaluateExpression` fait de même (son
    second argument sert à interpréter une expression SANS unité, il ne choisit
    pas l'unité de sortie — « 300 mm » y rend donc 30, pas 300).
    """
    warnings: list[str] = []
    declared_unit = normalize_unit(spec.get("unit"))
    requested = float(spec["value"])
    fusion_unit = getattr(param, "unit", None)

    if declared_unit is not None:
        if fusion_unit and fusion_unit != declared_unit:
            warnings.append(
                f"parameters.{name} : unité déclarée '{declared_unit}' != unité "
                f"Fusion '{fusion_unit}' — la conversion est faite par Fusion, "
                f"vérifier que c'est bien voulu"
            )
    elif fusion_unit and str(fusion_unit).strip():
        # Le YAML annonce une grandeur sans dimension, mais Fusion attend une
        # unité : l'expression nue sera interprétée dans CETTE unité. La
        # comparaison de valeur ci-dessous tranchera, l'avertissement dit où
        # regarder.
        warnings.append(
            f"parameters.{name} : déclaré sans unité dans design_params.yaml "
            f"mais le User Parameter Fusion est en '{fusion_unit}' — "
            f"l'expression nue sera interprétée en '{fusion_unit}'"
        )

    expected = to_internal(spec, name)

    try:
        actual = param.value
    except Exception as exc:
        warnings.append(
            f"parameters.{name} : relecture impossible de '{expression}' ({exc})"
        )
        return warnings

    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        warnings.append(
            f"parameters.{name} : relecture non numérique ({actual!r}) — "
            f"vérification de valeur ignorée"
        )
        return warnings

    tolerance = max(VALUE_TOL_ABS, abs(expected) * VALUE_TOL_REL)
    if abs(float(actual) - expected) > tolerance:
        internal_unit = (
            "cm" if declared_unit in LENGTH_TO_CM
            else "rad" if declared_unit in ANGLE_TO_RAD
            else "sans dimension"
        )
        raise DriverError(
            STATUS_PARAM_SET_FAILED,
            f"parameters.{name} : Fusion a retenu {float(actual):g} au lieu de "
            f"{expected:g} ({internal_unit}, unités internes) pour l'expression "
            f"'{expression}' = {requested:g} {declared_unit or 'sans unité'} — "
            f"incohérence d'unité ou paramètre piloté par une autre expression",
        )
    return warnings


def _restore_parameters(
    user_params: Any, original: Mapping[str, str]
) -> None:  # pragma: no cover - nécessite Fusion
    """Restaure les expressions d'origine après un échec partiel."""
    if not original:
        return
    logger.warning(
        "Échec en cours d'application — restauration de %d paramètre(s).",
        len(original),
    )
    for name, expression in original.items():
        try:
            param = user_params.itemByName(name)
            if param is not None:
                param.expression = expression
        except Exception as exc:
            logger.error(
                "Restauration impossible de '%s' vers '%s' : %s. Le modèle "
                "Fusion est peut-être dans un état intermédiaire — le recharger "
                "depuis le seed avant l'itération suivante.",
                name,
                expression,
                exc,
            )


def _recompute(design: Any) -> list[str]:  # pragma: no cover - nécessite Fusion
    """Force le recalcul complet et vérifie la santé de la timeline."""
    logger.info("Recalcul du modèle...")
    try:
        design.computeAll()
    except Exception as exc:
        raise DriverError(
            STATUS_RECOMPUTE_FAILED,
            f"échec du recalcul après mise à jour des paramètres : {exc}. Les "
            f"nouvelles valeurs cassent probablement une contrainte ou une "
            f"esquisse — proposer une variation plus conservative.",
        ) from exc

    try:
        adsk.doEvents()
    except Exception:
        pass

    warnings = _check_timeline_health(design)
    logger.info("Recalcul terminé.")
    return warnings


def _check_timeline_health(design: Any) -> list[str]:  # pragma: no cover
    """Inspecte la timeline : une feature en erreur invalide la géométrie.

    `computeAll()` ne lève pas toujours : Fusion peut marquer une feature en
    échec et continuer. Sans ce contrôle, on exporterait un STEP obsolète ou
    incomplet, et la CFD tournerait sur la mauvaise forme.
    """
    warnings: list[str] = []
    timeline = getattr(design, "timeline", None)
    if timeline is None:
        return warnings

    health_states = getattr(adsk.fusion, "FeatureHealthStates", None)
    if health_states is None:
        return warnings

    failed: list[str] = []
    for index in range(timeline.count):
        item = timeline.item(index)
        try:
            state = item.healthState
        except Exception:
            continue
        if state == health_states.ErrorFeatureHealthState:
            failed.append(f"{item.name} ({item.errorOrWarningMessage})")
        elif state == health_states.WarningFeatureHealthState:
            warnings.append(f"timeline : {item.name} — {item.errorOrWarningMessage}")

    if failed:
        raise DriverError(
            STATUS_RECOMPUTE_FAILED,
            "features en erreur après recalcul : " + " | ".join(failed) + ". "
            "Les valeurs demandées cassent le modèle — proposer une variation "
            "plus conservative.",
            details={"failed_features": failed},
        )
    return warnings


def _tag_generated(entity: Any) -> None:  # pragma: no cover - nécessite Fusion
    """Marque une entité comme produite par le driver, pour la repurger."""
    try:
        entity.attributes.add(
            REBUILD_ATTRIBUTE_GROUP, REBUILD_ATTRIBUTE_NAME, "1"
        )
    except Exception as exc:
        logger.warning(
            "Marquage impossible de '%s' (%s) — le repérage retombera sur le nom.",
            getattr(entity, "name", "?"),
            exc,
        )


def _is_driver_generated(entity: Any) -> bool:  # pragma: no cover
    """Vrai si l'entité vient du driver, ou du générateur d'origine du seed."""
    try:
        if (
            entity.attributes.itemByName(
                REBUILD_ATTRIBUTE_GROUP, REBUILD_ATTRIBUTE_NAME
            )
            is not None
        ):
            return True
    except Exception:
        pass
    name = getattr(entity, "name", "")
    return name in (REBUILD_SKETCH_NAME, REBUILD_BODY_NAME) or name in LEGACY_GEOMETRY_NAMES


def _purge_previous_geometry(root: Any) -> int:  # pragma: no cover - nécessite Fusion
    """Supprime la géométrie de l'itération précédente.

    Sans cette purge, chaque itération empilerait une aile de plus dans le
    document et le STEP exporté en contiendrait plusieurs — la CFD tournerait
    alors sur une géométrie absurde sans que rien ne le signale.

    Ne touche QUE ce que le driver a créé (attribut `aeroOpt`) ou ce que le
    générateur du seed a laissé (noms connus). Le reste du document est la
    propriété de l'utilisateur.
    """
    removed = 0

    # Les esquisses d'abord : Fusion supprime en cascade les features qui les
    # consomment, donc l'extrusion et son corps partent avec.
    sketches = [root.sketches.item(i) for i in range(root.sketches.count)]
    for sketch in reversed(sketches):
        if not _is_driver_generated(sketch):
            continue
        name = sketch.name
        try:
            sketch.deleteMe()
            removed += 1
            logger.info("Purge de l'esquisse '%s'.", name)
        except Exception as exc:
            raise DriverError(
                STATUS_REBUILD_FAILED,
                f"suppression impossible de l'esquisse '{name}' : {exc}",
            ) from exc

    bodies = [root.bRepBodies.item(i) for i in range(root.bRepBodies.count)]
    for body in reversed(bodies):
        if not _is_driver_generated(body):
            continue
        name = body.name
        try:
            body.deleteMe()
            removed += 1
            logger.info("Purge du corps '%s'.", name)
        except Exception as exc:
            raise DriverError(
                STATUS_REBUILD_FAILED,
                f"suppression impossible du corps '{name}' : {exc}",
            ) from exc

    leftovers = [
        root.bRepBodies.item(i).name
        for i in range(root.bRepBodies.count)
        if _is_driver_generated(root.bRepBodies.item(i))
    ]
    if leftovers:
        raise DriverError(
            STATUS_REBUILD_FAILED,
            f"géométrie de l'itération précédente encore présente après purge : "
            f"{leftovers} — le STEP contiendrait plusieurs ailes",
        )
    return removed


def _rebuild_geometry(
    design: Any, parameters: Mapping[str, Any]
) -> tuple[dict, list[str]]:  # pragma: no cover - nécessite Fusion
    """Reconstruit l'aile à partir de design_params.yaml.

    Purge la géométrie précédente, retrace le profil NACA aux nouvelles
    valeurs, l'extrude sur `span` et marque le résultat pour la purge suivante.
    L'incidence est déjà intégrée aux points du profil.
    """
    root = design.rootComponent
    warnings: list[str] = []
    plan = profile_from_parameters(parameters)

    logger.info(
        "Reconstruction : corde %.3f cm, envergure %.3f cm, t/c %.4f, m/c %.4f, "
        "incidence %.3f deg",
        plan["chord_cm"], plan["span_cm"], plan["thickness"], plan["camber"],
        plan["aoa_deg"],
    )

    removed = _purge_previous_geometry(root)
    if removed == 0:
        warnings.append(
            "aucune géométrie précédente à purger — premier passage, ou "
            "géométrie non reconnue (ni attribut 'aeroOpt', ni nom connu)"
        )

    try:
        sketch = root.sketches.add(root.xYConstructionPlane)
        sketch.name = REBUILD_SKETCH_NAME

        upper = adsk.core.ObjectCollection.create()
        for x, y in plan["profile"]["upper"]:
            upper.add(adsk.core.Point3D.create(x, y, 0))
        lower = adsk.core.ObjectCollection.create()
        for x, y in reversed(plan["profile"]["lower"]):
            lower.add(adsk.core.Point3D.create(x, y, 0))

        sketch.sketchCurves.sketchFittedSplines.add(upper)
        sketch.sketchCurves.sketchFittedSplines.add(lower)
    except DriverError:
        raise
    except Exception as exc:
        raise DriverError(
            STATUS_REBUILD_FAILED, f"tracé du profil impossible : {exc}"
        ) from exc

    if sketch.profiles.count == 0:
        raise DriverError(
            STATUS_REBUILD_FAILED,
            "le contour du profil ne s'est pas refermé — extrados et intrados "
            "ne se rejoignent pas ; valeurs d'épaisseur ou de cambrure "
            "probablement extrêmes",
        )

    try:
        extrudes = root.features.extrudeFeatures
        ext_input = extrudes.createInput(
            sketch.profiles.item(0),
            adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        )
        ext_input.setDistanceExtent(
            False, adsk.core.ValueInput.createByReal(plan["span_cm"])
        )
        extrude = extrudes.add(ext_input)
        extrude.name = REBUILD_BODY_NAME
    except Exception as exc:
        raise DriverError(
            STATUS_REBUILD_FAILED, f"extrusion impossible : {exc}"
        ) from exc

    if extrude.bodies.count == 0:
        raise DriverError(
            STATUS_REBUILD_FAILED, "l'extrusion n'a produit aucun corps"
        )

    _tag_generated(sketch)
    for i in range(extrude.bodies.count):
        body = extrude.bodies.item(i)
        body.name = REBUILD_BODY_NAME
        _tag_generated(body)

    try:
        adsk.doEvents()
    except Exception:
        pass

    summary = {
        key: plan[key]
        for key in (
            "chord_cm", "span_cm", "thickness", "camber", "aoa_deg",
            "n_points", "camber_position", "bbox_cm",
        )
    }
    summary["purged_entities"] = removed
    logger.info("Reconstruction terminée (%d corps).", extrude.bodies.count)
    return summary, warnings


def _export_step(
    design: Any, target: Path
) -> None:  # pragma: no cover - nécessite Fusion
    """Exporte la géométrie du composant racine en STEP, et vérifie le résultat."""
    root = design.rootComponent

    body_count = root.bRepBodies.count
    occurrence_count = root.occurrences.count
    if body_count == 0 and occurrence_count == 0:
        raise DriverError(
            STATUS_GEOMETRY_EMPTY,
            "le design ne contient aucun corps ni composant après recalcul — "
            "rien à exporter",
        )
    logger.info(
        "Géométrie : %d corps, %d occurrence(s) au niveau racine.",
        body_count,
        occurrence_count,
    )

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DriverError(
            STATUS_EXPORT_FAILED, f"création impossible de {target.parent} : {exc}"
        ) from exc

    # Un export qui échoue ne doit pas laisser croire au succès en réutilisant
    # le STEP d'une itération précédente.
    if target.exists():
        try:
            target.unlink()
        except OSError as exc:
            raise DriverError(
                STATUS_EXPORT_FAILED,
                f"impossible de supprimer le STEP précédent {target} : {exc}",
            ) from exc

    logger.info("Export STEP : %s", target)
    try:
        export_manager = design.exportManager
        options = export_manager.createSTEPExportOptions(str(target), root)
        if not export_manager.execute(options):
            raise DriverError(
                STATUS_EXPORT_FAILED,
                f"Fusion a signalé un échec d'export STEP vers {target}",
            )
    except DriverError:
        raise
    except Exception as exc:
        raise DriverError(
            STATUS_EXPORT_FAILED, f"export STEP impossible vers {target} : {exc}"
        ) from exc

    if not target.is_file():
        raise DriverError(
            STATUS_EXPORT_FAILED,
            f"export déclaré réussi mais {target} est absent du disque",
        )
    size = target.stat().st_size
    if size == 0:
        raise DriverError(STATUS_EXPORT_FAILED, f"STEP exporté vide : {target}")
    logger.info("STEP écrit : %s (%.1f Ko)", target, size / 1024.0)


# ─────────────────────────────────────────────────────────────────────────────
# Statut
# ─────────────────────────────────────────────────────────────────────────────


def _build_status(
    success: bool,
    status: str,
    *,
    iteration: int | None = None,
    design_id: str | None = None,
    config_path: Path | None = None,
    step_path: Path | None = None,
    applied: Mapping[str, dict] | None = None,
    error_message: str | None = None,
    details: Any = None,
    warnings: list[str] | None = None,
    geometry_mode: str | None = None,
    geometry: Mapping[str, Any] | None = None,
) -> dict:
    """Assemble le dict de statut retourné et sérialisé en JSON."""
    return {
        "success": success,
        "status": status,
        "iteration": iteration,
        "design_id": design_id,
        "config_path": _relative_to_repo(config_path) if config_path else None,
        "step_path": _relative_to_repo(step_path) if step_path else None,
        "geometry_mode": geometry_mode,
        "geometry": dict(geometry) if geometry else None,
        "applied_parameters": dict(applied) if applied else {},
        "error_message": error_message,
        "error_details": details,
        "warnings": list(warnings or []),
        "fusion_available": FUSION_AVAILABLE,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_status(status: Mapping[str, Any], directory: Path) -> Path | None:
    """Écrit `fusion_status.json` — canal de retour vers le master pipeline.

    Dans Fusion, le driver s'exécute dans un processus distinct : ce fichier
    est le seul moyen pour la Phase 3 de connaître l'issue de l'opération.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / STATUS_FILENAME
        target.write_text(
            json.dumps(status, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        return target
    except (OSError, TypeError, ValueError) as exc:
        logger.error("Écriture du statut impossible dans %s : %s", directory, exc)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Point d'entrée
# ─────────────────────────────────────────────────────────────────────────────


def drive(
    config_path: Path | None = None,
    iterations_root: Path | None = None,
    dry_run: bool = False,
    app: Any = None,
    geometry_mode: str | None = None,
) -> dict:
    """Exécute le cycle complet : config -> paramètres -> recalcul -> STEP.

    Args:
        config_path: chemin de design_params.yaml. Défaut : DESIGN_PARAMS_PATH
            si défini dans l'environnement, sinon `configs/design_params.yaml`.
        iterations_root: racine d'archivage. Défaut : ITERATIONS_DIR ou
            `data/iterations`.
        dry_run: n'appelle aucune API Fusion ; valide la configuration,
            construit les expressions et calcule les chemins. Forcé à True
            quand `adsk` n'est pas importable.
        app: instance `adsk.core.Application` déjà obtenue (injectée par
            `run()`), utilisée pour rediriger les logs vers Fusion.
        geometry_mode: "rebuild" (défaut) reconstruit la géométrie depuis la
            configuration ; "parameters" se contente de mettre à jour les User
            Parameters et de recalculer — réservé aux modèles réellement
            paramétriques. À défaut, FUSION_GEOMETRY_MODE puis le défaut.

    Returns:
        Le dict de statut (voir docstring du module). Ne lève pas : toute
        erreur attendue devient un statut `success: False` exploitable par le
        pipeline et par l'agent.
    """
    config_path = config_path or _env_path("DESIGN_PARAMS_PATH", DEFAULT_CONFIG_PATH)
    iterations_root = iterations_root or _env_path(
        "ITERATIONS_DIR", DEFAULT_ITERATIONS_DIR
    )
    simulated = dry_run or not FUSION_AVAILABLE

    setup_logging(app=app)  # journal console le temps de connaître l'itération

    iteration: int | None = None
    design_id: str | None = None
    out_dir: Path | None = None
    step_path: Path | None = None
    geometry: dict | None = None
    warnings: list[str] = []
    resolved_mode: str | None = None

    try:
        geometry_mode = resolve_geometry_mode(geometry_mode)
        resolved_mode = geometry_mode
        config = load_config(config_path)
        iteration = config["iteration"]
        design_id = config.get("design_id")
        parameters = config["parameters"]

        out_dir = iteration_dir(iteration, iterations_root)
        step_path = out_dir / STEP_FILENAME
        out_dir.mkdir(parents=True, exist_ok=True)

        # Le journal bascule vers le dossier de l'itération dès qu'il est connu.
        setup_logging(log_file=out_dir / LOG_FILENAME, app=app)
        logger.info(
            "=== Driver Fusion — itération %04d, design '%s'%s ===",
            iteration,
            design_id,
            " [MODE SIMULATION]" if simulated else "",
        )

        if simulated:
            planned = {
                name: {
                    "expression": build_expression(spec["value"], spec.get("unit")),
                    "requested_value": float(spec["value"]),
                    "unit": spec.get("unit"),
                }
                for name, spec in parameters.items()
            }
            for name, info in planned.items():
                logger.info("Prévu : %-20s -> %s", name, info["expression"])

            # En mode rebuild, la reconstruction est calculable hors Fusion :
            # on la déroule vraiment, ce qui valide la géométrie prévue sans
            # ouvrir la CAO.
            if geometry_mode == GEOMETRY_MODE_REBUILD:
                plan = profile_from_parameters(parameters)
                geometry = {
                    key: plan[key]
                    for key in (
                        "chord_cm", "span_cm", "thickness", "camber", "aoa_deg",
                        "n_points", "camber_position", "bbox_cm",
                    )
                }
                logger.info(
                    "Profil calculé : %d points/surface, emprise x [%.3f, %.3f] cm, "
                    "y [%.3f, %.3f] cm, extrusion %.3f cm",
                    plan["n_points"],
                    plan["bbox_cm"]["x_min"], plan["bbox_cm"]["x_max"],
                    plan["bbox_cm"]["y_min"], plan["bbox_cm"]["y_max"],
                    plan["span_cm"],
                )

            reason = (
                "dry-run demandé"
                if dry_run
                else "module adsk indisponible (hors Fusion 360)"
            )
            logger.warning(
                "Aucune action Fusion effectuée (%s). STEP attendu : %s",
                reason,
                step_path,
            )
            status = _build_status(
                False,
                STATUS_DRY_RUN,
                iteration=iteration,
                design_id=design_id,
                config_path=config_path,
                step_path=step_path,
                applied=planned,
                error_message=f"mode simulation — {reason} ; aucun STEP produit",
                warnings=warnings,
                geometry_mode=geometry_mode,
                geometry=geometry,
            )
            write_status(status, out_dir)
            return status

        # ── Chemin réel Fusion ────────────────────────────────────────────
        if app is None:  # pragma: no cover - nécessite Fusion
            app = adsk.core.Application.get()
            if app is None:
                raise DriverError(
                    STATUS_FUSION_UNAVAILABLE,
                    "adsk.core.Application.get() a retourné None — script non "
                    "exécuté depuis Fusion 360 ?",
                )
            setup_logging(log_file=out_dir / LOG_FILENAME, app=app)

        design = _get_design(app)  # pragma: no cover

        # Les User Parameters sont mis à jour dans les DEUX modes : c'est le
        # contrat §3.2, et un humain qui rouvre le document doit y lire les
        # valeurs de l'itération, quelle que soit la façon dont la géométrie
        # a été obtenue.
        applied, param_warnings = _apply_parameters(design, parameters)
        warnings.extend(param_warnings)

        if geometry_mode == GEOMETRY_MODE_REBUILD:
            geometry, rebuild_warnings = _rebuild_geometry(design, parameters)
            warnings.extend(rebuild_warnings)
        else:
            warnings.extend(_recompute(design))

        _export_step(design, step_path)

        logger.info("=== Itération %04d : succès ===", iteration)
        status = _build_status(
            True,
            STATUS_OK,
            iteration=iteration,
            design_id=design_id,
            config_path=config_path,
            step_path=step_path,
            applied=applied,
            warnings=warnings,
            geometry_mode=geometry_mode,
            geometry=geometry,
        )
        write_status(status, out_dir)
        return status

    except DriverError as exc:
        logger.error("ÉCHEC [%s] : %s", exc.status, exc.message)
        status = _build_status(
            False,
            exc.status,
            iteration=iteration,
            design_id=design_id,
            config_path=config_path,
            step_path=step_path,
            error_message=exc.message,
            details=exc.details,
            warnings=warnings,
            geometry_mode=resolved_mode,
        )
        if out_dir is not None:
            write_status(status, out_dir)
        return status

    except Exception as exc:  # garde-fou : le driver ne remonte jamais brut
        trace = traceback.format_exc()
        logger.error("ÉCHEC INATTENDU : %s\n%s", exc, trace)
        status = _build_status(
            False,
            STATUS_UNEXPECTED_ERROR,
            iteration=iteration,
            design_id=design_id,
            config_path=config_path,
            step_path=step_path,
            error_message=f"{type(exc).__name__}: {exc}",
            details=trace,
            warnings=warnings,
            geometry_mode=resolved_mode,
        )
        if out_dir is not None:
            write_status(status, out_dir)
        return status


def run(context: Any = None) -> dict:  # pragma: no cover - appelé par Fusion
    """Point d'entrée appelé par Fusion 360 lors du Run du script."""
    app = None
    if FUSION_AVAILABLE:
        try:
            app = adsk.core.Application.get()
        except Exception:
            app = None
    return drive(app=app)


def stop(context: Any = None) -> None:  # pragma: no cover - appelé par Fusion
    """Appelé par Fusion à l'arrêt du script : ferme proprement les journaux."""
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    """CLI hors Fusion — sert au mode simulation et au diagnostic."""
    parser = argparse.ArgumentParser(
        prog="fusion/parametric_driver.py",
        description=(
            "Driver paramétrique Fusion 360. Hors de Fusion, seul le mode "
            "simulation est possible (validation + expressions + chemins)."
        ),
    )
    parser.add_argument(
        "--config",
        default=None,
        help="chemin de design_params.yaml (défaut : configs/design_params.yaml)",
    )
    parser.add_argument(
        "--iterations-dir",
        default=None,
        help="racine d'archivage des itérations (défaut : data/iterations)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="n'appelle aucune API Fusion, même si le module adsk est disponible",
    )
    parser.add_argument(
        "--geometry-mode",
        choices=GEOMETRY_MODES,
        default=None,
        help=(
            f"'{GEOMETRY_MODE_REBUILD}' (défaut) reconstruit la géométrie depuis "
            f"la configuration ; '{GEOMETRY_MODE_PARAMETERS}' se contente de "
            f"mettre à jour les User Parameters"
        ),
    )
    args = parser.parse_args(argv)

    status = drive(
        config_path=Path(args.config) if args.config else None,
        iterations_root=Path(args.iterations_dir) if args.iterations_dir else None,
        dry_run=args.dry_run,
        geometry_mode=args.geometry_mode,
    )
    print(json.dumps(status, indent=2, ensure_ascii=False, default=str))

    if status["success"]:
        return 0
    return 3 if status["status"] == STATUS_DRY_RUN else 1


if __name__ == "__main__":
    raise SystemExit(main())
