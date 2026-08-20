"""Émulation de l'API Fusion 360 (`adsk`), pour exécuter le vrai driver sans Fusion.

Fusion 360 n'a pas de mode headless : sans cela, `_get_design`,
`_rebuild_geometry`, `_export_step` et `_export_stl` ne seraient jamais
exécutés en test, alors que ce sont eux qui portent le risque.

Ce module installe `adsk`, `adsk.core` et `adsk.fusion` dans `sys.modules`,
avec juste ce que le driver appelle. Les objets reproduisent le comportement
DOCUMENTÉ de Fusion, en particulier :

- `Parameter.value` rend la valeur en unités internes (cm, radians) ;
- `extrude.name` nomme la FEATURE, pas le corps — le corps reçoit un nom
  autogénéré « Body1 ». C'est exactement ce qui a fait survivre la géométrie du
  seed à la première purge ;
- l'export STL écrit un vrai fichier, en millimètres, de sorte que la chaîne
  OpenFOAM en aval puisse réellement le consommer.

LIMITE ASSUMÉE : un faux ne valide que la logique du driver, pas ma lecture de
l'API Fusion. Le bug `evaluateExpression` de la Phase 1 est passé au travers de
206 tests parce que la doublure encodait la même erreur de compréhension que le
code. Les comportements reproduits ici sont donc calqués sur la documentation
Autodesk, et non sur ce que le driver attend.
"""

from __future__ import annotations

import math
import struct
import sys
import types
from pathlib import Path

# Unités internes de Fusion.
LENGTH_TO_CM = {"mm": 0.1, "cm": 1.0, "m": 100.0, "in": 2.54, "ft": 30.48}
ANGLE_TO_RAD = {"deg": math.pi / 180.0, "rad": 1.0}

# Unité d'écriture des STL par Fusion.
STL_UNIT_PER_CM = 10.0  # cm -> mm


# ─────────────────────────────────────────────────────────────────────────────
# Attributs
# ─────────────────────────────────────────────────────────────────────────────


class Attributes:
    def __init__(self) -> None:
        self._values: dict[tuple[str, str], str] = {}

    def add(self, group: str, name: str, value: str) -> None:
        self._values[(group, name)] = value

    def itemByName(self, group: str, name: str):
        return self._values.get((group, name))


# ─────────────────────────────────────────────────────────────────────────────
# Géométrie
# ─────────────────────────────────────────────────────────────────────────────


class Point3D:
    def __init__(self, x: float, y: float, z: float) -> None:
        self.x, self.y, self.z = float(x), float(y), float(z)

    @classmethod
    def create(cls, x, y, z):
        return cls(x, y, z)

    def asArray(self):
        return (self.x, self.y, self.z)


class ObjectCollection:
    def __init__(self) -> None:
        self._items: list = []

    @classmethod
    def create(cls):
        return cls()

    def add(self, item) -> None:
        self._items.append(item)

    def item(self, index):
        return self._items[index]

    @property
    def count(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)


class ValueInput:
    def __init__(self, value, is_real: bool) -> None:
        self.value = value
        self.is_real = is_real

    @classmethod
    def createByReal(cls, value):
        return cls(float(value), True)

    @classmethod
    def createByString(cls, text):
        return cls(str(text), False)


class Entity:
    """Base des entités supprimables et porteuses d'attributs."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes = Attributes()
        self._collection: "EntityCollection | None" = None
        self.deletable = True

    def deleteMe(self) -> bool:
        if not self.deletable:
            raise RuntimeError(f"'{self.name}' ne peut pas etre supprime")
        if self._collection is not None:
            self._collection._remove(self)
        return True


class EntityCollection:
    def __init__(self, items=None) -> None:
        self._items: list[Entity] = []
        for item in items or []:
            self._add(item)

    def _add(self, item: Entity) -> Entity:
        item._collection = self
        self._items.append(item)
        return item

    def _remove(self, item: Entity) -> None:
        if item in self._items:
            self._items.remove(item)

    def item(self, index: int) -> Entity:
        return self._items[index]

    @property
    def count(self) -> int:
        return len(self._items)

    @property
    def names(self) -> list[str]:
        return [i.name for i in self._items]


class BRepBody(Entity):
    """Corps solide. Porte de quoi reconstruire un maillage pour l'export."""

    def __init__(self, name: str, sections=None, z_range=(0.0, 0.0)) -> None:
        super().__init__(name)
        # sections : {"upper": [(x, y)...], "lower": [...]} en centimètres
        self.sections = sections or {"upper": [], "lower": []}
        self.z_range = z_range


class Profile:
    def __init__(self, sketch: "Sketch") -> None:
        self.sketch = sketch


class ProfileCollection:
    def __init__(self, sketch: "Sketch") -> None:
        self.sketch = sketch

    @property
    def count(self) -> int:
        # Fusion ne détecte un profil que si le contour est fermé : deux
        # splines qui se rejoignent au bord d'attaque et au bord de fuite.
        return 1 if self.sketch.is_closed() else 0

    def item(self, index: int) -> Profile:
        return Profile(self.sketch)


class SketchFittedSplines:
    def __init__(self, sketch: "Sketch") -> None:
        self.sketch = sketch

    def add(self, collection: ObjectCollection):
        points = [(p.x, p.y) for p in collection]
        self.sketch.splines.append(points)
        return points


class SketchCurves:
    def __init__(self, sketch: "Sketch") -> None:
        self.sketchFittedSplines = SketchFittedSplines(sketch)


class Sketch(Entity):
    def __init__(self, name: str = "Sketch1") -> None:
        super().__init__(name)
        self.splines: list[list[tuple[float, float]]] = []
        self.sketchCurves = SketchCurves(self)
        self.profiles = ProfileCollection(self)

    def is_closed(self) -> bool:
        if len(self.splines) != 2:
            return False
        a, b = self.splines
        if not a or not b:
            return False
        tol = 1e-9
        # b est l'intrados parcouru en sens inverse : il doit finir où a
        # commence, et commencer où a finit.
        return (
            abs(a[0][0] - b[-1][0]) < tol and abs(a[0][1] - b[-1][1]) < tol
            and abs(a[-1][0] - b[0][0]) < tol and abs(a[-1][1] - b[0][1]) < tol
        )

    def sections(self) -> dict[str, list[tuple[float, float]]]:
        upper = self.splines[0]
        lower = list(reversed(self.splines[1]))
        return {"upper": upper, "lower": lower}


class SketchCollection(EntityCollection):
    def add(self, plane) -> Sketch:
        return self._add(Sketch(f"Sketch{self.count + 1}"))


# ─────────────────────────────────────────────────────────────────────────────
# Features
# ─────────────────────────────────────────────────────────────────────────────


class FeatureOperations:
    NewBodyFeatureOperation = "NewBody"
    JoinFeatureOperation = "Join"
    CutFeatureOperation = "Cut"


class FeatureHealthStates:
    HealthyFeatureHealthState = 0
    RolledBackFeatureHealthState = 1
    ErrorFeatureHealthState = 2
    WarningFeatureHealthState = 3
    UnknownFeatureHealthState = 4


class ExtrudeInput:
    def __init__(self, profile: Profile, operation: str) -> None:
        self.profile = profile
        self.operation = operation
        self.distance: float = 0.0

    def setDistanceExtent(self, is_symmetric: bool, value: ValueInput) -> None:
        self.distance = float(value.value)


class ExtrudeFeature(Entity):
    def __init__(self, name: str, bodies: EntityCollection) -> None:
        super().__init__(name)
        self.bodies = bodies
        self.healthState = FeatureHealthStates.HealthyFeatureHealthState
        self.errorOrWarningMessage = ""


class ExtrudeFeatures:
    def __init__(self, component: "Component") -> None:
        self.component = component
        self._counter = 0

    def createInput(self, profile: Profile, operation: str) -> ExtrudeInput:
        return ExtrudeInput(profile, operation)

    def add(self, ext_input: ExtrudeInput) -> ExtrudeFeature:
        self._counter += 1
        sketch = ext_input.profile.sketch
        # Fusion nomme le CORPS automatiquement ; `extrude.name` ne renomme que
        # la feature. C'est ce décalage qui a laissé survivre le corps du seed.
        body = BRepBody(
            f"Body{len(self.component.bRepBodies._items) + 1}",
            sections=sketch.sections(),
            z_range=(0.0, ext_input.distance),
        )
        self.component.bRepBodies._add(body)
        feature = ExtrudeFeature(f"Extrude{self._counter}", EntityCollection([body]))
        # Le corps appartient au composant : la collection de la feature ne
        # doit pas s'en approprier la suppression.
        body._collection = self.component.bRepBodies
        self.component.features._features.append(feature)
        return feature


class Features:
    def __init__(self, component: "Component") -> None:
        self.extrudeFeatures = ExtrudeFeatures(component)
        self._features: list[ExtrudeFeature] = []

    def itemByName(self, name: str):
        return next((f for f in self._features if f.name == name), None)


# ─────────────────────────────────────────────────────────────────────────────
# Composant, paramètres, export
# ─────────────────────────────────────────────────────────────────────────────


class Component:
    def __init__(self) -> None:
        self.name = "root"
        self.sketches = SketchCollection()
        self.bRepBodies = EntityCollection()
        self.occurrences = EntityCollection()
        self.xYConstructionPlane = object()
        self.features = Features(self)


class Parameter:
    """User Parameter Fusion.

    `value` rend la valeur en unités INTERNES (cm, rad, nombre nu) — c'est le
    point que la Phase 1 avait mal compris.
    """

    def __init__(self, name: str, expression: str, unit: str = "") -> None:
        self.name = name
        self.unit = unit
        self.attributes = Attributes()
        self._expression = expression

    @property
    def expression(self) -> str:
        return self._expression

    @expression.setter
    def expression(self, text: str) -> None:
        self._parse(text)  # une expression invalide est refusée, comme Fusion
        self._expression = text

    def _parse(self, text: str) -> float:
        parts = str(text).split()
        try:
            number = float(parts[0])
        except (ValueError, IndexError) as exc:
            raise RuntimeError(f"expression invalide : {text!r}") from exc
        unit = parts[1] if len(parts) > 1 else self.unit
        if not unit:
            return number
        if unit in LENGTH_TO_CM:
            return number * LENGTH_TO_CM[unit]
        if unit in ANGLE_TO_RAD:
            return number * ANGLE_TO_RAD[unit]
        raise RuntimeError(f"unité inconnue : {unit!r}")

    @property
    def value(self) -> float:
        return self._parse(self._expression)


class UserParameters:
    def __init__(self, params: list[Parameter]) -> None:
        self._params = list(params)

    def itemByName(self, name: str):
        return next((p for p in self._params if p.name == name), None)

    def item(self, index: int) -> Parameter:
        return self._params[index]

    @property
    def count(self) -> int:
        return len(self._params)


class UnitsManager:
    """`evaluateExpression` rend la valeur en unités internes (cm, rad)."""

    internalUnits = "cm"

    def evaluateExpression(self, expression: str, units: str | None = None) -> float:
        parts = str(expression).split()
        number = float(parts[0])
        unit = parts[1] if len(parts) > 1 else units
        if not unit:
            return number
        if unit in LENGTH_TO_CM:
            return number * LENGTH_TO_CM[unit]
        if unit in ANGLE_TO_RAD:
            return number * ANGLE_TO_RAD[unit]
        raise RuntimeError(f"unité inconnue : {unit!r}")


class ExportOptions:
    def __init__(self, kind: str, filename: str, geometry) -> None:
        self.kind = kind
        self.filename = filename
        self.geometry = geometry
        self.isBinaryFormat = True
        self.meshRefinement = None


class MeshRefinementSettings:
    MeshRefinementHigh = 0
    MeshRefinementMedium = 1
    MeshRefinementLow = 2


class ExportManager:
    """Écrit de vrais fichiers, pour que la chaîne aval soit réellement testée."""

    def __init__(self, component: Component) -> None:
        self.component = component
        self.fail_step = False
        self.fail_stl = False

    def createSTEPExportOptions(self, filename: str, geometry=None) -> ExportOptions:
        return ExportOptions("step", filename, geometry or self.component)

    def createSTLExportOptions(self, geometry, filename: str) -> ExportOptions:
        return ExportOptions("stl", filename, geometry)

    def execute(self, options: ExportOptions) -> bool:
        if options.kind == "step":
            if self.fail_step:
                return False
            return self._write_step(Path(options.filename))
        if self.fail_stl:
            return False
        return self._write_stl(Path(options.filename), options.isBinaryFormat)

    def _bodies(self) -> list[BRepBody]:
        return [b for b in self.component.bRepBodies._items if isinstance(b, BRepBody)]

    def _write_step(self, path: Path) -> bool:
        # Un STEP plausible, non exploitable geometriquement : seul le STL sert
        # a la CFD, le STEP est le livrable contractuel.
        bodies = self._bodies()
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "ISO-10303-21;",
            "HEADER;",
            "FILE_DESCRIPTION((''),'2;1');",
            f"FILE_NAME('{path.name}','2026-01-01T00:00:00',(''),(''),'','','');",
            "FILE_SCHEMA(('AUTOMOTIVE_DESIGN'));",
            "ENDSEC;",
            "DATA;",
        ]
        for i, body in enumerate(bodies, start=1):
            lines.append(f"#{i}=MANIFOLD_SOLID_BREP('{body.name}',#{i + 100});")
        lines += ["ENDSEC;", "END-ISO-10303-21;"]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    def _triangles(self) -> list[tuple]:
        """Triangule les corps : surfaces extrudees + faces d'extremite."""
        tris: list[tuple] = []
        for body in self._bodies():
            upper = body.sections.get("upper") or []
            lower = body.sections.get("lower") or []
            if len(upper) < 2 or len(lower) < 2:
                continue
            z0, z1 = body.z_range
            for surface in (upper, lower):
                for i in range(len(surface) - 1):
                    a, b = surface[i], surface[i + 1]
                    p1 = (a[0], a[1], z0)
                    p2 = (b[0], b[1], z0)
                    p3 = (b[0], b[1], z1)
                    p4 = (a[0], a[1], z1)
                    tris.append((p1, p2, p3))
                    tris.append((p1, p3, p4))
            n = min(len(upper), len(lower))
            for z in (z0, z1):
                for i in range(n - 1):
                    u1, u2 = upper[i], upper[i + 1]
                    l1, l2 = lower[i], lower[i + 1]
                    tris.append(((u1[0], u1[1], z), (u2[0], u2[1], z),
                                 (l2[0], l2[1], z)))
                    tris.append(((u1[0], u1[1], z), (l2[0], l2[1], z),
                                 (l1[0], l1[1], z)))
        return tris

    def _write_stl(self, path: Path, binary: bool) -> bool:
        tris = self._triangles()
        if not tris:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        # Fusion ecrit ses STL en millimetres, pas dans ses unites internes.
        s = STL_UNIT_PER_CM

        if binary:
            payload = b"fake fusion stl".ljust(80, b"\0") + struct.pack("<I", len(tris))
            for tri in tris:
                payload += struct.pack("<3f", 0.0, 0.0, 0.0)
                for p in tri:
                    payload += struct.pack("<3f", p[0] * s, p[1] * s, p[2] * s)
                payload += struct.pack("<H", 0)
            path.write_bytes(payload)
        else:
            lines = ["solid wing"]
            for tri in tris:
                lines += ["  facet normal 0 0 0", "    outer loop"]
                lines += [
                    f"      vertex {p[0] * s:.8e} {p[1] * s:.8e} {p[2] * s:.8e}"
                    for p in tri
                ]
                lines += ["    endloop", "  endfacet"]
            lines.append("endsolid wing")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True


class Design:
    def __init__(self, parameters: list[Parameter], component: Component | None = None):
        self.rootComponent = component or Component()
        self.userParameters = UserParameters(parameters)
        self.unitsManager = UnitsManager()
        self.exportManager = ExportManager(self.rootComponent)
        self.timeline = None
        self.compute_calls = 0
        self.compute_raises = False

    @classmethod
    def cast(cls, product):
        return product if isinstance(product, cls) else None

    def computeAll(self) -> None:
        self.compute_calls += 1
        if self.compute_raises:
            raise RuntimeError("recalcul impossible")


class Document:
    def __init__(self, name: str, design: Design) -> None:
        self.name = name
        self.design = design


class ImportManager:
    def __init__(self, app: "Application") -> None:
        self.app = app
        self.imported: list[str] = []
        self.fail = False

    def createFusionArchiveImportOptions(self, path: str):
        return {"path": path}

    def importToNewDocument(self, options):
        if self.fail:
            raise RuntimeError("import impossible")
        self.imported.append(options["path"])
        design = Design(list(self.app.seed_parameters))
        self.app.activeDocument = Document("seed_design.f3d", design)
        self.app.activeProduct = design
        return self.app.activeDocument


class UserInterface:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def messageBox(self, text: str, title: str = "") -> int:
        self.messages.append((title, text))
        return 0


class Application:
    _instance: "Application | None" = None

    def __init__(self, design: Design | None = None) -> None:
        self.activeProduct = design
        self.activeDocument = Document("seed_design.f3d", design) if design else None
        self.userInterface = UserInterface()
        self.importManager = ImportManager(self)
        self.logs: list[str] = []
        self.seed_parameters: list[Parameter] = []

    def log(self, message: str) -> None:
        self.logs.append(message)

    @classmethod
    def get(cls):
        return cls._instance


# ─────────────────────────────────────────────────────────────────────────────
# Installation dans sys.modules
# ─────────────────────────────────────────────────────────────────────────────


def install(design: Design | None = None) -> Application:
    """Installe le faux `adsk` et rend l'application active."""
    core = types.ModuleType("adsk.core")
    core.Application = Application
    core.Point3D = Point3D
    core.ObjectCollection = ObjectCollection
    core.ValueInput = ValueInput

    fusion = types.ModuleType("adsk.fusion")
    fusion.Design = Design
    fusion.FeatureOperations = FeatureOperations
    fusion.FeatureHealthStates = FeatureHealthStates
    fusion.MeshRefinementSettings = MeshRefinementSettings

    adsk = types.ModuleType("adsk")
    adsk.core = core
    adsk.fusion = fusion
    adsk.doEvents = lambda: None

    sys.modules["adsk"] = adsk
    sys.modules["adsk.core"] = core
    sys.modules["adsk.fusion"] = fusion

    app = Application(design)
    Application._instance = app
    return app


def uninstall() -> None:
    for name in ("adsk.core", "adsk.fusion", "adsk"):
        sys.modules.pop(name, None)
    Application._instance = None


# ─────────────────────────────────────────────────────────────────────────────
# Documents de départ
# ─────────────────────────────────────────────────────────────────────────────


def seed_parameters() -> list[Parameter]:
    """Les User Parameters du seed réel."""
    return [
        Parameter("chord", "300 mm", "mm"),
        Parameter("thickness", "0.12", ""),
        Parameter("camber", "0.02", ""),
        Parameter("span", "80 mm", "mm"),
        Parameter("aoa", "0 deg", "deg"),
    ]


def seed_design() -> Design:
    """Reproduit le document du premier run réel.

    Une esquisse nommée par le générateur, et un corps que Fusion a nommé
    « Body1 » — c'est ce corps-là qui avait survécu à la purge et donné un
    STEP à deux ailes.
    """
    design = Design(seed_parameters())
    root = design.rootComponent
    root.sketches._add(Sketch("NACA_2412_Profile"))
    root.bRepBodies._add(
        BRepBody(
            "Body1",
            sections={
                "upper": [(0.0, 0.0), (15.0, 1.8), (30.0, 0.0)],
                "lower": [(0.0, 0.0), (15.0, -1.8), (30.0, 0.0)],
            },
            z_range=(0.0, 8.0),
        )
    )
    return design
