# aero-opt-agent — Agentic Aerodynamic Shape Optimisation

**Automatic aerodynamic shape optimisation: a 2D profile → OpenFOAM CFD → a
better shape, in a loop, unattended.**

You hand it a profile — a coordinate file downloaded from a database, a 2D
drawing, a CAD part, a parametric Fusion model, or four NACA digits — and an
objective. The system re-parameterises it, builds the geometry, meshes it, runs
the solver, reads the aerodynamic coefficients, proposes a better shape, and
starts again. At the end you get a folder with the optimised geometry, the CFD
fields, an illustrated report, and everything needed to carry the design back
into CAD.

Python, no heavy dependencies. 788 tests.

**No proprietary software is required.** Fusion 360 is optional, and so is
everything else that costs money — see [CAD without Fusion](#cad-without-fusion).

---

## Two versions

`main` carries **v1.5 “Universal 2D”**, the most complete state of the project.
**v1.0** was not replaced: it is a **direct ancestor** of v1.5, its NACA path
still runs unchanged inside v1.5, and tests guarantee it. v1.5 is v1.0 *plus*
capabilities.

| | **v1.0** — NACA profiles | **v1.5** — Universal 2D |
|---|---|---|
| Input | 4 NACA digits | + `.dat` / `.csv` (Selig, Lednicer, CSV), `.dxf` drawings, `.step` parts |
| Shape description | thickness, camber | + 24 Kulfan (CST) coefficients |
| Geometry producers | internal, Fusion 360 | both, behind one interface |
| Fidelity checks | STL bounding box | + reconstruction gate, STL round-trip |
| CAD output | STL only | + **STEP**: a real solid, openable as is |
| Return to CAD | CSV section | + `FUSION_RETURN.md` and a generated CAD script |
| Reference result | NACA 2412: L/D **13.43 → 29.88** (+122 %), 22 iterations | Clark Y: L/D **28.11 → 29.34** (+4.4 %), 25 iterations, **0 failures** |
| Tag | `v1.0-stable` | `v1.5.0` |

The v1.0 code stays available exactly as it was, frozen at its tag:

```bash
git checkout v1.0-stable
```

## See a report the system wrote

### → **[patxi-sallaberry.github.io/aero-opt-agent](https://patxi-sallaberry.github.io/aero-opt-agent/)**

The system writes its own report at the end of a run: starting parameters
against final ones, coefficient history iteration by iteration, before/after
sections, pressure distributions, CFD fields, and a plain-language reading of
the physics — derived from measured differences, never from boilerplate. Two
real runs are published, exactly as the system produced them:

| | report | what it shows |
|---|---|---|
| **v1.5** | [Clark Y](https://patxi-sallaberry.github.io/aero-opt-agent/example_report_clarky/report.html) | a real UIUC-database profile, ingested from a point file, re-parameterised into 24 CST coefficients, 25 iterations without a single failure |
| **v1.0** | [NACA 2412](https://patxi-sallaberry.github.io/aero-opt-agent/example_report/report.html) | four-digit parameterisation, 22 iterations, +122 % lift-to-drag |

Neither was written by hand: the Clark Y report is the direct output of
`scripts/export_best.py`, prose and physics commentary included.

These files are **self-contained**: figures and CFD images embedded, no external
resource. They are also versioned in the repository
(`docs/example_report*/report.html`), so they work offline after a clone. The
same reports in Markdown, which GitHub renders directly:
[Clark Y](docs/example_report_clarky/README.md) ·
[NACA 2412](docs/example_report/README.md).

The Clark Y folder also holds
[`geometry.step`](docs/example_report_clarky/geometry.step) — the optimised
solid, openable as is in Fusion 360, FreeCAD or SolidWorks.

## Optimise any profile, in three commands

```bash
# 1. get a profile — here the Clark Y from the UIUC database
curl -o clarky.dat "http://airfoiltools.com/airfoil/seligdatfile?airfoil=clarky-il"

# 2. re-parameterise it into optimisable CST coefficients
python3 -m profiles.reparameterize clarky.dat \
    --chord 300 --span 80 --aoa 3 -o configs/design_params.yaml

# 3. optimise
python3 scripts/run_loop.py --max-iterations 20 \
    --cfd-settings configs/cfd_settings_fast.yaml
```

The `results/run_*/best_design/` folder appears on its own at the end, with its
`report.html` and its `FUSION_RETURN.md`.

## Results obtained

**Clark Y ingested from the UIUC database** — 121 points, re-parameterised into
24 CST coefficients, **25 iterations, 0 failures**, 300 mm chord, 3° incidence:

| | start | optimised | |
|---|---|---|---|
| Lift Cl | 0.7659 | **0.7784** | +1.6 % |
| Drag Cd | 0.02725 | **0.02653** | −2.6 % |
| **Lift-to-drag Cl/Cd** | **28.11** | **29.34** | **+4.4 %** |

The gain is modest, and that is the honest result: the Clark Y has been in use
since 1922, and at 3° incidence it already works close to its best lift-to-drag
ratio. The search confirmed it in numbers — raising incidence to 4.68° gains
20 % of lift but 24 % of drag, so it loses. What v1.5 brings here is not a
spectacular gain, it is the ability to refine a shape that v1.0 could not even
describe.

**NACA 2412 at zero incidence** (v1.0, four-digit parameterisation) —
**22 iterations in 27 minutes**:

| | seed | optimised | |
|---|---|---|---|
| Lift Cl | 0.2274 | **0.7657** | +237 % |
| Drag Cd | 0.01693 | **0.02563** | +51 % |
| **Lift-to-drag Cl/Cd** | **13.43** | **29.88** | **+122 %** |

Here the starting point was deliberately far from the optimum, hence the size of
the gain. The incidence found — 5.04° — is the one physics predicts for maximum
lift-to-drag on a cambered profile.

---

## Installation

**Requirements**: Linux or WSL, Python 3.10+, and OpenFOAM. Everything else is
optional.

```bash
git clone https://github.com/Patxi-Sallaberry/aero-opt-agent.git
cd aero-opt-agent

python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt

# OpenFOAM (ESI) — provides simpleFoam, snappyHexMesh, checkMesh
curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash
sudo apt-get install openfoam2506-default

# optional: CFD visuals in the report
sudo apt-get install paraview xvfb

# optional: STEP in and out (~2 GB, OpenCASCADE)
python3 -m pip install -r requirements-cad.txt

cp .env.example .env        # then fill in FOAM_BASHRC
```

Check that everything answers:

```bash
python3 -m pytest tests/ -q                       # 788 tests, ~40 s
python3 pipeline/utils.py configs/design_params.yaml --show-ranges
```

---

## CAD without Fusion

**There is no “Fusion API key.”** The Fusion scripting API (`adsk`) is not
called from outside — it runs *inside* the desktop application. You drop the
script in via *Utilities → ADD-INS → Scripts and Add-Ins*, and press **Run**.
Nothing to register, nothing to pay for. (Autodesk does sell cloud credentials
under *Autodesk Platform Services*, but that is a different product entirely and
this project does not use it.)

So the only reason Fusion would block you is not having Fusion. It is not
needed:

| you want | with Fusion | **without Fusion — fully open source** |
|---|---|---|
| Build the geometry | Fusion backend | **internal backend** — pure Python, always available |
| A real CAD solid out | Fusion STEP export | **`geometry.step`** written by the internal backend |
| Read a CAD part in | — | **`.step` / `.dxf` ingestion** |
| Open and edit the result | Fusion 360 | **FreeCAD**, or any CAD that reads STEP |

The optional CAD kernel is [CadQuery](https://github.com/CadQuery/cadquery)
(Apache 2.0), which wraps **OpenCASCADE** — the same geometry kernel FreeCAD is
built on. It is what makes the STEP path work in both directions:

```bash
python3 -m pip install -r requirements-cad.txt
```

Once installed, every run writes a `geometry.step` next to the STL:

```
STEP written: 5 faces, 583040 mm³
```

Five faces, not eight hundred facets. **FreeCAD or Fusion opens it with a
double-click** as a native solid: you can fillet it, change the span, put it in
an assembly. Measured fidelity: the original points lie within **5 × 10⁻⁵ of
chord** of the written surface, and the volume matches the profile area to
within 2 % — a unit mistake would show up immediately.

> Do not open `geometry.stl` instead. CAD packages will read it, but they turn
> it into a mesh body of several hundred flat facets, useless for design work.
> The STL is there for the solver and for 3D printing.

The kernel weighs close to 2 GB, because it embeds all of OpenCASCADE. That is
why it lives in a separate `requirements-cad.txt`: **without it everything still
works** — the STL is written, the CFD runs, the optimisation completes, the
report comes out. Only STEP is missing, and its absence is stated along with the
command that fixes it, rather than disguised as an unrecognised format.

---

## Running an optimisation

```bash
python3 scripts/run_loop.py --max-iterations 20 \
    --cfd-settings configs/cfd_settings_fast.yaml
```

That is all. Expect roughly one minute per iteration with this preset. The loop
stops itself on stagnation, and **survives failures**: an iteration whose mesh
breaks is archived, the strategy shortens its step, and the next one restarts
from the best known shape.

What it prints while running:

```
[loop] iter   0 | Cd 0.03107 | Cl 0.25266 | Cl/Cd 8.13 | 84.8s  <- best
[loop]      proposal [local] chord 300->321
[loop] iter   1 | Cd 0.03097 | Cl 0.24549 | Cl/Cd 7.93 | 78.6s
```

To change what gets optimised, edit `configs/design_params.yaml`: starting
values, bounds, and objective (`maximize_Cl_Cd`, `minimize_Cd` or
`maximize_downforce`).

## Getting the result

**The folder is created automatically at the end of a run**:

```
results/run_YYYYMMDD_HHMMSS/best_design/
├── report.html            ← open this one
├── README.md              the same report, in Markdown
├── FUSION_RETURN.md       how to carry the design back into CAD
├── rebuild_in_fusion.py   CAD script that redraws the profile
├── geometry.step          the CAD solid, in millimetres ← open this in CAD
├── geometry.stl           the mesh for the solver, in metres
├── profile_section.csv    the section as simulated
├── profile_chord.dat      the profile straightened, for XFOIL / XFLR5
├── design_params.yaml     the exact parameters, replayable
├── results.json           the coefficients
├── figures/               curves and CFD images
├── comparison/            the seed, for the before/after comparison
└── cfd/                   the complete OpenFOAM case (ParaView)
```

On a run that has already finished:

```bash
python3 scripts/run_loop.py --report        # the trajectory, in the console
python3 scripts/run_loop.py --export-best   # (re)generate the folder
```

---

Two specifications govern this project: the v1.0 one,
[`MASTER_DOCUMENTATION_AGENTIC_AERO_OPTIMIZATION.md`](MASTER_DOCUMENTATION_AGENTIC_AERO_OPTIMIZATION.md),
and the v1.5 one,
[`MASTER_DOCUMENTATION_2D_GENERALIZATION.md`](MASTER_DOCUMENTATION_2D_GENERALIZATION.md).
Where this README and they disagree, they win. Deliberate departures are listed
at the end of this document.

---

## What one iteration does

```
design_params.yaml
        │
        ▼
  ① contract validation        pipeline/utils.py
        │
        ▼
  ② geometry                   fusion/parametric_driver.py
        │                      (Fusion 360, or the internal producer)
        ▼
  ③ geometry check             pipeline/geometry_validator.py
        │
        ▼
  ④ meshing + CFD              openfoam/run_cfd.sh
        │                      blockMesh → snappyHexMesh → checkMesh → simpleFoam
        ▼
  ⑤ coefficients               openfoam/postprocess.py → results.json
        │
        ▼
  ⑥ archiving                  data/iterations/iter_XXXX/
        │
        ▼
  ⑦ proposal                   agent/orchestrator.py → design_params.yaml
```

The orchestrator writes **only** `configs/design_params.yaml`. That is the
golden rule of the master document, and validation enforces it: a proposal that
loosens a bound, changes a unit or adds a parameter is rejected before it
reaches the disk.

---

## Layout

```
configs/
  design_params.yaml          ← the ONLY file the agent modifies
  design_params_clarky.yaml      example from a real profile (24 CST coeff.)
  cfd_settings.yaml              CFD conditions (fine settings)
  cfd_settings_fast.yaml         exploration preset, ~60 s/iteration
  cfd_settings_demo.yaml         the fast one, but keeps cases (for visuals)
examples/profiles/               example profiles (NACA, Clark Y, E387, S1223)
profiles/
  loader.py                      reads Selig / Lednicer / CSV / DXF / STEP
  dxf.py                         contour extraction from a 2D drawing
  profile.py                     normalised profile + geometric measures
  validation.py                  validity checks
  cst.py                         Kulfan parameterisation + fitting
  reparameterize.py              file → coefficients → design_params.yaml
  geometry.py                    coefficients → contour + shape check
  roundtrip.py                   STL read back → deviation from the original
geometry/
  base.py                        GeometryBackend interface + registry
  internal_backend.py            internal producer (always available)
  fusion_backend.py              Fusion 360 producer
  step_io.py                     STEP in and out (optional kernel)
  common.py                      report normalisation
fusion/
  seed_design.f3d                Fusion model (not versioned)
  parametric_driver.py           geometry: Fusion or internal production
openfoam/
  templates/external_aero/       parameterised OpenFOAM case
  case_builder.py                YAML → dimensioned case
  run_cfd.sh                     run orchestration
  postprocess.py                 → results.json
pipeline/
  utils.py                       contract loading + validation
  geometry_validator.py          exported geometry check
  master_pipeline.py             entry point for one iteration
agent/
  prompts/system_prompt.md       agent instructions
  orchestrator.py                parameter proposals
scripts/
  run_loop.py                    optimisation loop
data/iterations/                 archives (not versioned)
tests/                           788 tests
```

---

## The `design_params.yaml` contract

Three families of rules, enforced by `pipeline/utils.py`:

1. **Structure** — required keys, strict types, no unknown key.
2. **Bounds** — `min < max` and `min ≤ value ≤ max`.
3. **max_delta_pct** — the change from the last **successful** iteration stays
   within this percentage.

   Two cases force that budget to be measured against the `max - min` span
   rather than against the value: when the previous value is **zero** (the
   relative percentage is undefined, and the parameter would be frozen for
   good), and when the **bounds straddle zero**. The second case deserves an
   explanation, because it trapped a real optimisation: for an incidence bounded
   to [−2°, 12°], going from 0 to −1.68° costs one iteration, but coming back
   costs **eight**, each step being limited to 12 % of the current value. The
   search explored one direction and could no longer leave it. A percentage of a
   quantity that changes sign constrains nothing meaningful; referred to the
   span, the budget becomes symmetric again — which is what a safety rule has to
   be.

Between two iterations, only `value` may change.

### Two parameterisations in the same contract

The optional `parameterization` field is `naca` (default, v1.0) or `cst` (v1.5).
It is not trusted on its own: the parameterisation is **recognised from the
parameters present**, and checked against the declared one. A hand-written file
whose header lies about its contents is rejected on the spot, rather than
producing a silently wrong shape three steps later.

| | `naca` | `cst` |
|---|---|---|
| Shape parameters | `thickness`, `camber` | `cst_upper_0…N`, `cst_lower_0…N` |
| Physical parameters | `chord`, `span`, `aoa` | identical |
| Thickness and camber | given as input | **measured** on the reconstructed shape |

The optional `provenance` block carries what describes the shape without being
an optimisation variable: the source file, the CST order, the incidence removed
at ingestion, and the **trailing-edge ordinates**. Those live there because the
contract requires `min < max` — a frozen quantity has no place among the
variables — and without them a profile with an open trailing edge would be
rebuilt closed.

```bash
python3 pipeline/utils.py configs/design_params.yaml --show-ranges
```

```
chord:     [279, 321] mm          thickness: [0.1104, 0.1296]
camber:    [0.018, 0.022]         span:      [79.2, 80.8] mm
aoa:       [-1.68, 1.68] deg
```

### Archiving and disk space

`execution.keep_case_after_run` decides what survives an iteration: `true` keeps
everything, `"dicts"` deletes mesh and fields while keeping the dictionaries and
the logs, `false` deletes the whole case. A complete case weighs about twenty
megabytes — over fifty unattended iterations, several gigabytes — while it
regenerates in seconds from the STL and the dictionaries. The fast preset uses
`"dicts"`.

`span` is **held fixed on purpose**: the computation is quasi-2D, so span has no
effect on Cd and Cl. Letting it vary would spend iterations for nothing. To make
it meaningful, switch `domain.spanwise_treatment` to `full_3d` and reopen its
bounds.

---

## Ingesting an existing profile

```bash
python3 profiles/loader.py my_profile.dat            # read and measure
python3 profiles/validation.py my_profile.dat        # + validity checks
python3 profiles/loader.py my_profile.dat --json     # machine-readable output
```

```
Profil          : NACA 2412
Format          : selig
Points          : 202 (101 extrados, 101 intrados)
Épaisseur max   : 0.1200 c à 29.2% de corde
Cambrure max    : +0.0200 c à 39.8% de corde
Bord de fuite   : 0.00000 c
Rayon de nez    : 0.01459 c
Incidence retirée : -3.000°
```

> The tools still print in French. Translating them is tracked as follow-up
> work; the output above is shown verbatim rather than an idealised English
> version that the code does not actually produce.

### Three conventions, none of them declared

Airfoil files have been circulating for forty years in three incompatible
formats, which no header identifies:

| Format | What it looks like |
|---|---|
| **Selig** | one continuous contour: trailing edge → upper surface → nose → lower surface → trailing edge. The most common (UIUC, XFOIL). |
| **Lednicer** | a two-number header — the point counts — then each surface from nose to tail. |
| **CSV** | `x, y` columns, optionally preceded by a surface column. This is what the project exports. |
| **DXF** | a drawing, not a point list. Recognised by extension, handled separately (below). |

Selig and Lednicer look exactly alike: two columns of numbers. Only the way the
abscissa evolves separates them — it decreases then rises in one, rises twice in
the other. Recognition therefore works on the shape of the sequence, never on
the file extension.

### What ingestion normalises

The profile comes out with the nose at the origin, the trailing edge at (1, 0),
and unit chord. Two things are removed and **reported**:

- **scale** — a file in millimetres is brought back to unit chord, the original
  chord being preserved;
- **incidence** — many files have a few degrees baked into their coordinates.
  But incidence is a design parameter here: leaving it in the geometry would
  count it twice.

The transform applied is kept: `profile.transform.restore(point)` brings any
point back into the original file's frame.

### What validation refuses — and what it lets through

Fatal: open contour, surfaces that cross, a surface folded back on itself, a
self-intersecting contour, thickness outside [1 %, 40 %].

Merely reported: thick trailing edge, strong camber, very sharp nose, low point
density. **These are design choices, not errors** — refusing everything unusual
would rule out half of all real profiles.

Nothing raises: loading and validation both return a report, like
`GeometryBackend.generate`. A questionable file must not interrupt a loop.

### Starting from a 2D drawing (DXF)

```bash
python3 profiles/loader.py my_drawing.dxf
python3 -m profiles.reparameterize my_drawing.dxf --chord 300
```

A DXF is not a coordinate list: it is a drawing. Entities appear in the order
they were drawn, each in whichever direction the hand went, the contour is often
cut into pieces, and there is a title block sitting next to it. None of that is
an anomaly — it is what CAD produces.

Reading therefore reconstructs without assuming anything about the file: it
joins the pieces by endpoint proximity, **reversing them when needed**, keeps
the largest contour among those it finds, starts it at the point of maximum
abscissa — the trailing edge — and traverses it in the direction that goes along
the upper surface first. After that the path is exactly that of a point file,
cleaning and validation included.

Read: `LWPOLYLINE`, `POLYLINE`/`VERTEX`, `LINE`, `ARC`, `CIRCLE` and `SPLINE`.
Any unsupported entity is **reported** — if the contour depended on it, you need
to know.

The example file `examples/profiles/naca2412.dxf` is deliberately uncooperative:
seventeen shuffled polylines, reversed traversal direction, a closing line and a
stray title block. It returns the original profile bit for bit, and the same CST
coefficients to within 10⁻⁸.

### Starting from a STEP

```bash
python3 -m pip install -r requirements-cad.txt   # once, ~2 GB
python3 -m profiles.reparameterize my_part.step --chord 300
```

A STEP contains no contour: it describes a B-Rep topology, where the shape is
derived from faces, edges and chained NURBS curves. Where a DXF can be decoded
by hand, this one needs a **CAD kernel** — hence the optional dependency, which
embeds all of OpenCASCADE.

Reading keeps the largest **planar** face — the section of a prism, or the
drawing itself — then discretises its outer wire edge by edge, tightening on
curves and not on straight lines.

Without the kernel, a `.step` does not fail with an “unrecognised format”
message that would send you looking in the wrong place: the message names the
dependency and reminds you that a DXF export from CAD reads without it.

---

## CST re-parameterisation

A point file is not optimisable: two hundred free coordinates give two hundred
variables, and nothing stops the shape from turning into a saw blade. It must
first be described by a small number of numbers that still mean something.

```bash
python3 -m profiles.reparameterize examples/profiles/clarky.dat \
    --chord 300 --span 80 --aoa 3 -o configs/design_params.yaml
```

```
Profil            : CLARK Y AIRFOIL
Ajustement CST    : ordre 11, 24 coefficients (12 par surface)

                      original      reconstruit      écart
  Épaisseur max       0.117055      0.117149     +9.40e-05
  Cambrure max        0.034310      0.034383     +7.30e-05
  Rayon de nez        0.013195      0.012017     -1.18e-03

Écarts au profil d'origine (distance géométrique, en corde)
  maximal         : 3.251e-04 (0.0325 % c) à 3.0% sur l'extrados
  moyen           : 7.595e-05

Porte de reconstruction : FRANCHIE (seuil 5e-04 corde)
```

### The method

Each surface is written `ζ(ψ) = C(ψ)·S(ψ) + ψ·Δζ_te`, where `C(ψ) = √ψ·(1−ψ)` is
the **class function** and `S` a sum of Bernstein polynomials. Three properties
follow, and they are what makes optimisation safe:

- **the shape is smooth by construction** — a Bernstein sum cannot ripple
  between points, where a free spline does so at the first step too many;
- **the physics is in the formulation** — the `0.5` exponent imposes the
  square-root nose of a rounded leading edge, the `1` exponent a sharp trailing
  edge. These behaviours cannot be lost during optimisation;
- **the fit is linear** — no starting point, no random draw. Two fits on the
  same points give the same result bit for bit.

### The reconstruction gate

The file is accepted only if the reconstructed shape stays within
**5 × 10⁻⁴ of chord** of the original (maximum deviation) and **10⁻⁴** on
average. Without this gate an optimisation can run perfectly for hours on a
shape that is not the one supplied — and nothing in the results would say so,
since the whole downstream chain works.

The deviation is a **geometric distance**, not a vertical one. At the leading
edge the surface slope exceeds 6: a vertical deviation there is several times
the real distance, and would reject a perfectly good fit.

A refusal says what to do next:

```
reconstruction refusée : écart maximal de 1.09e-03 corde à 4.0% sur l'extrados
[...] — 12 points sur 122 dépassent le seuil
— l'ordre 11 franchirait la porte : relancer avec --order 11
```

### Choosing the order

The default order is **11**, giving 24 coefficients. It was selected by
cross-validation on real UIUC profiles — fit on every other point, measure on
the held-out ones. Out-of-sample error tracks fitting error down to about five
points per coefficient, then diverges: on the E387, order 13 shows 8.6 × 10⁻⁴ on
its own points and 1.12 × 10⁻³ on the ones it never saw. At that point it is
fitting the file's noise.

That is also why a coarse file cannot be “fixed” by raising the order: the
E387's 61 points are refused at every reasonable order, and the message advises
a denser file rather than an overfit.

### The fallback method, measured then dropped

§4 proposes, should CST fail, a **thickness + camber** decomposition: fit `t(x)`
with a square-root basis and `yc(x)` with an integer-power basis separately. The
idea is attractive — it is exactly the nature of the two terms, and it explains
CST's structural floor on cambered profiles.

It was implemented as a joint fit on the original points, at equal coefficient
count, and measured:

| profile | order | CST, max dev. | fallback, max dev. |
|---|---|---|---|
| NACA 2412 | 11 | **2.92 × 10⁻⁴** | 5.55 × 10⁻⁴ |
| Clark Y | 11 | **3.25 × 10⁻⁴** | 6.58 × 10⁻⁴ |
| E387 | 11 | **1.01 × 10⁻³** | 1.35 × 10⁻³ |
| S1223 | 11 | 1.56 × 10⁻³ | **1.37 × 10⁻³** |

CST wins on three profiles out of four. The fallback only wins on the S1223,
which is very heavily cambered — and **not by enough to pass the gate**, which
sits at 5 × 10⁻⁴. So it rescues none of the cases it was meant to rescue.

The code was not kept. Adding a second parameterisation across the whole chain —
variables, bounds, reconstruction, reporting — for zero rescued cases would only
have bought extra surface for bugs. The result is recorded here because it
answers the question, and a future reader should not have to measure it again.

### Bounds with a geometric meaning

On a real profile the coefficients are not of the same order: the Clark Y has
some worth 0.13 and others worth 3.27, the shape holding together through
cancellation between large terms. Proportional bounds — “±50 % of its value” —
would give the second a margin of ±1.63, that is **65 % of chord of
displacement**: the optimiser's first probe would destroy the profile.

The geometric effect of a variation `δ` of coefficient `i` is exactly
`max_ψ [C(ψ)·Bᵢ(ψ)] · δ`. So the relation is inverted: each coefficient receives
the margin that gives it the **same geometric authority** as the others — 1.5 %
of chord, and 0.6 % for the two end coefficients, which hold the nose radius and
the trailing-edge angle.

Verified: across forty-eight bounds pushed to their extremes, none produces an
invalid profile.

### Three quantities under explicit control

§4 requires the **leading-edge radius**, the **maximum thickness** and the
**trailing-edge thickness** to stay under explicit control. The coefficient
bounds already contain them indirectly — each one only moves its surface by
1.5 % of chord — but indirectly is not enough: nothing stops two coefficients
from conspiring in the same direction, and above all **nothing would report
it**. A profile thinning from 12 % to 6 % over twenty iterations would pass
every other gate.

Re-parameterisation therefore writes explicit bounds, set at ±40 % of the
original shape:

```yaml
constraints:
  topology_preserving: true
  min_wall_thickness_mm: 1.5
  min_thickness_ratio: 0.070289      # these five keys are optional;
  max_thickness_ratio: 0.164009      # v1.0 files do not have them
  min_leading_edge_radius: 0.00721   # and remain valid
  max_leading_edge_radius: 0.016824
  max_trailing_edge_thickness: 0.02
```

They are checked **at every iteration, on the reconstructed shape**, by
`pipeline/geometry_validator.py`. A violation fails the iteration like an
aberrant geometry — plainly, rather than letting it drift.

### Round-trip

The gate judges the fit. It says nothing about what is **written to disk**:
between the coefficients and the STL sit a scaling, a unit conversion, an
incidence rotation and a triangulation.

```bash
python3 -m profiles.roundtrip results/run_*/best_design/geometry.stl \
    clarky.dat --chord 300 --aoa 3
```

The tool reads the file back, extracts its section and measures it against the
original profile — trusting nothing that was used to write it. It is the only
check that would catch a unit mix-up.

---

## Geometry

### The `GeometryBackend` interface

Everything downstream — CFD, optimiser, report — talks to a single interface.
Which producer works behind it is a configuration choice.

```python
from geometry import get_backend

backend = get_backend("auto")            # or "internal", or "fusion"
result  = backend.generate(design_params, output_dir)

result.success              # bool
result.stl_path             # Path | None
result.step_path            # Path | None — a CAD model, if there is one
result.profile_coordinates  # closed contour, in metres
result.message              # what happened, in plain words
```

`generate` **never raises**: an expected failure is a result, not an exception.
That is what lets the loop archive the failed iteration, draw a conclusion from
it, and carry on.

### The two producers shipped

| Producer | What it does | When |
|----------|--------------|------|
| `fusion` | Updates the User Parameters, rebuilds the geometry, exports STEP + STL | The script runs **inside** Fusion 360 |
| `internal` | Computes the profile and writes the STL in metres, without Fusion; writes STEP too when the CAD kernel is installed | Everywhere else — this is what makes the loop autonomous |

`auto` (default) asks each backend whether it is available and keeps the first
usable one, Fusion first. **The Fusion API has no headless mode**: without the
internal producer, every iteration would wait for a human to click *Run*. Both
paths share the same profile function, hence the same shape.

Asking about availability **before** starting avoids discovering Fusion's
absence after five minutes of meshing.

```bash
python3 pipeline/master_pipeline.py --geometry-backend internal
python3 scripts/run_loop.py         --geometry-backend fusion
```

```python
import geometry
geometry.describe_backends()   # name, availability, description
geometry.resolve("auto")       # what "auto" picks here
```

### Adding a producer

Three moves, and not one line of the pipeline to change:

```python
from geometry import GeometryBackend, GeometryResult, register_backend

@register_backend
class MyBackend(GeometryBackend):
    name = "my_backend"

    @classmethod
    def available(cls) -> bool:
        return True                       # are the required tools present?

    def generate(self, design_params, output_dir, **options):
        ...
        return GeometryResult(success=True, stl_path=..., message="...")
```

It becomes selectable through `--geometry-backend my_backend` straight away, and
appears in the command help: the choices are read from the registry.

### Two strategies, inside Fusion

`rebuild` (default) updates the User Parameters **then rebuilds** the geometry.
`parameters` does the recompute only, and makes sense only if the model is
genuinely driven by its dimensions.

The shipped seed requires `rebuild`: its generator does create the 5 User
Parameters, but it draws its profile with a spline through hard-coded points and
extrudes it over a raw length. **Changing its parameters does not move a single
point** — without `rebuild`, every iteration would export identical geometry and
the agent would optimise thin air, with no error to say so.

### Starting from an existing Fusion model

This is Mode 4 of §3, and it needs no particular code: if your model is
**genuinely** parametric — its dimensions driven by User Parameters — the driver
only has to update them and let Fusion recompute.

```bash
export FUSION_GEOMETRY_BACKEND=fusion
export FUSION_GEOMETRY_MODE=parameters
```

The `parameters` names in `design_params.yaml` must then match the model's User
Parameters **exactly**; a missing name fails the iteration with `PARAM_NOT_FOUND`
rather than going unnoticed.

Watch out for the trap the shipped seed illustrates: many models *expose* User
Parameters without the geometry depending on them. In `parameters` mode they
would export an identical shape at every iteration, and the agent would optimise
thin air with no error to say so. When in doubt keep `rebuild` — the geometry
fingerprint check catches the case, but only after spending an iteration.

### Running the driver inside Fusion

1. Open the model (or drop the seed at `fusion/seed_design.f3d`).
2. *Utilities → ADD-INS → Scripts and Add-Ins → Scripts → (+)*, point at
   `fusion/parametric_driver.py`, then **Run**.

Outside Fusion, `--dry-run` validates the configuration and computes the planned
geometry without writing anything.

### Units

Nobody guarantees the unit an STL is written in. Rather than assume it,
`case_builder.py` **measures** it: it compares the file's extent to the geometry
requested and, if the discrepancy matches a common factor, rescales the STL and
says so. If the discrepancy is explained by no unit factor, it is not a unit
problem but a geometry problem, and the iteration is refused.

---

## CFD

```bash
openfoam/run_cfd.sh --iteration-dir data/iterations/iter_0000
openfoam/run_cfd.sh --iteration-dir ... --dry-run     # build the case only
openfoam/run_cfd.sh --iteration-dir ... --mesh-only   # stop after checkMesh
```

### The case is dimensioned, not copied

`case_builder.py` derives from `design_params.yaml`: domain and cell sizes (in
multiples of chord), `k` and `omega` (from turbulence intensity), the
`locationInMesh` point, and above all **`Aref` and `lRef`, recomputed at every
iteration**. Freezing the reference area while the chord varies would move Cd
and Cl when only the normalisation had changed — the agent would be optimising a
computational artefact.

### Frame

```
+X = chord, from leading edge to trailing edge
+Y = thickness, and lift
+Z = span
```

Incidence is carried by the **geometry** — the profile is rotated at build time
— and not by the flow direction. The flow stays aligned with +X from one
iteration to the next, so the lift and drag directions never move.

### Mesh quality

`checkMesh` is run **without** `-allGeometry`: that mode flags concave cells,
which every snappyHexMesh mesh with boundary layers produces and which the
solver handles without difficulty — it would fail nearly every iteration. The
useful verdict comes from comparison against the thresholds in
`cfd_settings.yaml`: non-orthogonality, skewness, aspect ratio.

Those thresholds account for a measured fact: **snappyHexMesh is not
deterministic in parallel**. Three successive meshes of the same geometry gave
54.5 / 68.5 / 69.1 for maximum non-orthogonality. A threshold set too tight
would fail an iteration at random, and the optimisation would no longer be
reproducible — hence 75.

A skewness defect is tolerated when it is both **rare and contained**: at most
one ten-thousandth of the faces, never more than twenty, and a maximum below 10.
A thickened trailing edge produces a handful of warped cells that no preset
resolves — three faces out of 258,814 do not make a mesh unusable. The defect is
reported, not hidden, and the tolerance extends to nothing else: a negative-volume
cell remains fatal.

### `results.json`

Written in **all** cases, success or failure: it is the only feedback channel to
the pipeline and the agent. On failure, `Cd`/`Cl`/`Cl_Cd` are `null` and never
`0.0` — a zero would propagate through the loop as a legitimate measurement.

`converged` summarises the two conditions that make a point usable: a validated
mesh and stabilised coefficients.

### Two presets

| | `cfd_settings.yaml` | `cfd_settings_fast.yaml` |
|---|---|---|
| Duration | ~15 min | ~60 s |
| Mesh | 8 cells/chord, levels 3-4, boundary layers | 6 cells/chord, levels 2-3, no layers |
| Iterations | 2000 | 500 |
| Use | qualify a shape | explore |

An optimisation does not compare values against reality, it **ranks shapes**. A
systematic bias does not change that ranking, so the fast preset is fine for
exploration. Re-qualify the best design with the fine settings before quoting a
number.

### Reference result

OpenFOAM v2506, NACA 2412 at zero incidence, Re = 4 × 10⁵, fine settings:

| | |
|---|---|
| Cells | 168,312 |
| checkMesh | non-ortho 54.5 · skewness 2.6 · aspect ratio 14.5 |
| **Cl** | **0.2275** — NACA 2412 theory at α = 0°: ≈ 0.25 |
| **Cd** | **0.0170** |
| Stability | relative standard deviation 4 × 10⁻⁵ over 200 iterations |

The Cl is right. The **Cd is overestimated by a factor of ≈ 2**: `kOmegaSST`
assumes a turbulent boundary layer from the leading edge, which is false at
Re = 4 × 10⁵ where a good part of the upper surface stays laminar. Acceptable
for comparing shapes, not for quoting an absolute drag.

### Reference optimisation

22 iterations in 27 minutes, fast preset, local strategy, unattended:

| | seed | best (iteration 21) |
|---|---|---|
| chord | 300 mm | 343.5 mm |
| thickness | 0.120 | 0.113 |
| camber | 0.020 | 0.020 |
| aoa | 0° | 5.04° |
| **Cl/Cd** | **8.13** | **20.45** — **+151 %** |

21 successful iterations, one rejected by `checkMesh` (skewness 4.03), from
which the strategy recovered by shortening its step.

The best design **re-qualified at the fine settings** gives Cd 0.02563,
Cl 0.76572, **Cl/Cd 29.88** against 13.41 for the seed: **+123 %**. The fast
preset announced +151 %, the fine settings confirm +123 % — same direction, same
order of magnitude. That is the check that matters: the ranking of shapes
established during exploration holds at the accurate settings.

The incidence found, 5°, is the one physics predicts for maximum lift-to-drag on
a cambered profile. The seed, at 0°, was on the flank of the curve.

---

## The agent

```bash
python3 agent/orchestrator.py --dry-run --explain
```

### Two strategies

`llm` — Claude reads the history and reasons about the shape
(`ANTHROPIC_API_KEY` required). A proposal rejected by validation is sent back
**with the error message**, which names the offending parameter and gives the
admissible interval; three attempts, then it gives up.

`local` — deterministic pattern search, no key and no network.

`auto` (default) asks the agent and falls back to local search if it is
unavailable. That fallback is not a stopgap: without it, a missing key or a
network outage would halt an optimisation lasting several hours.

### What local search does

From the best known point, it probes one parameter in one direction. If that
pays, it **continues in the same direction** — a line search, without which a
parameter would only advance one step every 2n iterations. Otherwise it tries
the other direction, then the next parameter; when everything has been probed
without gain, the step is halved.

Three refinements that came from observation:

- **It does not teleport to the best point.** The `max_delta_pct` budget is
  measured from the last *successful* iteration, not from the best one. When the
  two differ, the search moves towards it as far as the contract allows.
- **A parameter with no measured effect is abandoned.** Two attempts that change
  nothing in the objective are enough: each evaluation costs several minutes.
- **On a large design space, probe order stops being a detail.** With five
  parameters a full cycle costs ten iterations and everything gets tried. With
  twenty-seven it costs fifty-four: whatever is probed last is never probed.
  Parameters whose influence is known in advance — incidence, then chord — go
  first among the unexplored ones. On a real Clark Y run, `aoa` had never had
  its turn in eleven iterations.

On a real 22-iteration optimisation it took lift-to-drag from 8.13 to 20.45 (see
above). It also has limits worth knowing: it is a greedy, coordinate-by-
coordinate descent. It does not exploit **couplings** — optimal camber depends
on incidence — and can only meet them by rotation. That is precisely where an
agent that knows aerodynamics does better, and why the `llm` strategy remains
the main path.

---

## The loop

```bash
python3 scripts/run_loop.py --max-iterations 20 --strategy auto \
    --cfd-settings configs/cfd_settings_fast.yaml
```

It is built to run unattended:

- **it does not stop on a failure** — the failed iteration is archived, the
  strategy shortens its step, the next one restarts from the best known shape.
  Only consecutive failures, a sign of something structural, interrupt it;
- **it is resumable** — all state lives in `data/iterations/`;
- **it stops itself** on stagnation, rather than burning the budget on numerical
  noise;
- **Ctrl-C** finishes the current iteration then exits cleanly — cutting in the
  middle of an OpenFOAM run would leave an archive that resuming would misread.

```bash
python3 scripts/run_loop.py --report     # read a run that already happened
python3 scripts/run_loop.py --resume     # resume without overwriting archives
```

A summary is written to `data/iterations/optimization_summary.json`; `--report`
prints the full trajectory — what moved, what it gave, where it failed — and the
best design's parameters.

---

## The deliverable folder — options

The folder's contents and how to get it are described
[above](#getting-the-result); this section covers fine-tuning the export.

```bash
python3 scripts/export_best.py --iterations-dir data/iterations
python3 scripts/run_loop.py --export-best         # on a run already done
python3 scripts/run_loop.py --no-export           # disable
python3 scripts/run_loop.py --no-visuals          # without ParaView
python3 scripts/export_best.py --no-case          # without the mesh (light)
```

The export cannot make a successful optimisation fail: if something goes wrong,
the results stay archived in `data/iterations/` and the command can be rerun by
hand.

### Before / after

The report systematically sets the seed against the retained design: sections
side by side **at the same scale** then superimposed, performance bars per
quantity, Cp distributions overlaid, and pressure fields and streamlines placed
next to each other **with the same colour scale**.

One rule governs that section: **both sides must be measured in the same CFD
regime**. Comparing an exploration seed against a design re-qualified at the fine
settings would inflate the gain without it being real — and it can go as far as
reversing a conclusion: on the reference design, drag appeared to *fall* by 18 %
when regimes were mixed, whereas at constant regime it **rises by 51 %**.
Lift-to-drag still gains 122 %, because lift triples.

```bash
# the default reference is the run's first iteration; if the best design was
# re-qualified, supply a seed measured in the same regime
python3 scripts/export_best.py --iterations-dir data/iterations \
    --qualified-dir data/qualify/iter_0000 \
    --baseline-dir data/baseline/iter_0000
```

Without `--baseline-dir`, the comparison falls back to the exploration numbers on
**both** sides and says so in the report. The regime used is always written down
explicitly.

The folder contains:

| | |
|---|---|
| `README.md` | the complete report |
| `report.html` | the same, **self-contained** — inline SVG, base64 images |
| `geometry.step` | the CAD solid, when a kernel is available |
| `geometry.stl` | the geometry, in metres |
| `profile_section.csv` / `.dat` | the section as simulated, incidence included |
| `profile_chord.dat` | the profile **straightened**, unit chord — for XFOIL / XFLR5 |
| `design_params.yaml` | the exact parameters, replayable |
| `results.json` | the coefficients |
| `FUSION_RETURN.md` | how to carry this design back into CAD |
| `rebuild_in_fusion.py` | CAD script that redraws the profile and extrudes it |
| `figures/` | SVG curves and CFD images |
| `cfd/` | the OpenFOAM case, with `best_design.foam` for ParaView |
| `logs/` | the logs of each step |

The report gives starting parameters against final ones, the evolution of Cd, Cl
and Cl/Cd iteration by iteration, the pressure distribution over the profile, and
a **physical reading** of what changed — derived from measured differences, never
from boilerplate: a parameter that did not move is not commented on.

The CFD visuals — Cp field, streamlines, velocity magnitude — are rendered by
ParaView in batch (`scripts/paraview_render.py`, `pvbatch` under `xvfb-run` to
work without a display). The script is copied into the folder: it stays
replayable without the rest of the system. If ParaView is missing, the report
still comes out, with its curves and the reason the images are absent.

---

## Carrying the design back into CAD

An optimisation that only returns an STL is a design dead end. An STL is a
faceted solid of several hundred flat faces: you can print it, but you can
neither fillet it properly nor change one of its dimensions.

Every export therefore writes a **`FUSION_RETURN.md`** detailing the routes, and
`report.html` carries a dedicated section.

| route | what you get | when to choose it |
|---|---|---|
| **Open `geometry.step`** | native solid, one double-click | **the simplest**, if the CAD kernel is installed — works in FreeCAD too |
| **Replay the parameters** | native model, full CAD history | as soon as a starting model exists |
| **`rebuild_in_fusion.py` script** | sketch + extrusion, hands off | no starting model, no CAD kernel |
| **Import `profile_section.csv`** | sketch drawn by hand | to stay in control, or use another CAD package |

**The shortest route, if `requirements-cad.txt` is installed**: the folder
contains a `geometry.step`, which any CAD package opens directly — *File → Open*,
or drag and drop. It is a real B-Rep solid of a few faces, not a mesh: you can
fillet it, change its span, assemble it. No conversion, no script. **FreeCAD
reads it just as well as Fusion**, which means this route costs nothing.

> Do not confuse this with importing the STL. CAD will open that too, but it
> turns it into a mesh body of several hundred flat facets — useless for design
> work. The STL is there for the solver and for printing.

```bash
# route 1 — the driver rebuilds the shape and exports STEP and STL
cp results/run_*/best_design/design_params.yaml configs/design_params.yaml
# then, in Fusion: Utilities → ADD-INS → Scripts → fusion/parametric_driver.py

# route 2 — standalone script, coordinates included
# Utilities → ADD-INS → Scripts → + → rebuild_in_fusion.py → Run
```

The driver accepts both parameterisations: on a `cst` file it rebuilds the shape
from the Kulfan coefficients. Its drawing code only handles points, so the Fusion
route needs no special support.

The generated script draws **one spline per surface** rather than a single one
over the whole contour: at the leading edge the curve turns back on itself, and a
single spline would put an inflection point there instead of a nose — precisely
the region that decides stall.

Two traps the document names explicitly:

- **incidence is already in the coordinates** of `profile_section`; a downstream
  setup that applies it again would count it twice. That is what
  `profile_chord.dat` is for;
- **do not convert the STL into a solid**, for the reason given above.

The transfer is verifiable, not merely described:

```bash
python3 -m profiles.roundtrip exported.stl profile_section.dat --chord 300
```

---

## Archiving

Every iteration, **successful or not**, leaves in `data/iterations/iter_XXXX/`:

```
design_params.yaml     the EXACT configuration that produced this result
geometry.stl / .step   the geometry
results.json           the coefficients, or the cause of failure
iteration.json         the pipeline's report
fusion_status.json     the driver's report
logs/                  one log per step
cfd/                   the complete OpenFOAM case
```

Copying the configuration is essential: the working file will already have been
rewritten by the agent at the next iteration, and an archived result without its
parameters cannot be tied to anything.

---

## Tests

```bash
python3 -m pytest tests/ -q          # 788 tests, ~40 s
```

What is covered with no external dependency: contract validation, Fusion units
and expressions, STL reading, case dimensioning, template rendering, coefficient
reading in both OpenFOAM conventions, detection of unchanged geometry, pipeline
sequencing, search convergence on an analytic model, and parsing of the agent's
replies.

The STEP tests run **both ways**: with the CAD kernel they check the result,
without it they check that the refusal is clean and names the dependency. A test
that merely skipped would leave uncovered the path most installations will take.

The Fusion driver is exercised against an **emulation of the `adsk` API**
(`tests/fake_adsk.py`): `run()`, the rebuild, the purge and the exports really do
run, on a document that reproduces the one from the first real run.

> **Acknowledged limitation.** A fake validates the driver's logic, not its
> reading of the Fusion API. A Phase 1 bug — `evaluateExpression` returns
> internal units — slipped past 206 tests because the double encoded the same
> misunderstanding as the code. Only a run inside Fusion settles that kind of
> question.

---

## Deliberate departures from the master documents

| Departure | Why |
|---|---|
| `openfoam/case_builder.py`, a file outside the prescribed layout | The case is **derived** from the parameters (domain, cells, k, omega, Aref). Doing that arithmetic in bash over YAML would have been the most fragile part of the system. |
| Internal geometry producer | The Fusion API has no headless mode. Without it, no autonomous loop is possible. Fusion stays the reference whenever it is available. |
| Local strategy as agent fallback | An optimisation lasting hours cannot depend on an API key or on the network. |
| `configs/cfd_settings_fast.yaml` | The fine settings cost 15 min per iteration, that is 5 h for 20 iterations. |
| `rebuild` mode by default | The shipped seed is not genuinely driven by its parameters (see above). |
| Coordinates as lists of tuples, not `np.ndarray` | The driver runs in Fusion's embedded interpreter, where numpy is not guaranteed. Nothing here would benefit from it. |
| STEP behind an optional dependency | `cadquery` embeds all of OpenCASCADE: close to two gigabytes. Imposing that on someone who only wants CFD would be disproportionate; everything works without it, and its absence is stated along with the command that fixes it. |
| §4 fallback method not adopted | Measured then dropped: the thickness/camber decomposition beats CST on one profile out of four tested, and never by enough to pass the gate. See above. |
| Trailing-edge ordinates in `provenance`, not `parameters` | The contract requires `min < max`: a quantity that describes the shape without being optimisable has no place among the variables. |
| Tolerance for a rare, contained skewness defect | A thickened trailing edge produces a few warped faces that no preset resolves. Three faces out of 258,814 do not make a mesh unusable; the defect is reported, not hidden. |

---

## What is not in the repository

- **`fusion/seed_design.f3d`** — the Fusion model. A binary specific to each
  project, so not versioned. Without it the system computes the geometry itself:
  nothing blocks.
- **`.env`** — your paths and your API key. Start from `.env.example`.
- **`ANTHROPIC_API_KEY`** — for the LLM strategy. Without it the loop runs on
  local search, which needs neither key nor network.
- **`data/iterations/` and `results/`** — outputs, regenerable. Curated examples
  are kept in [`docs/`](docs/).

## Adapting it to your geometry

Three routes, from the simplest to the most invasive:

1. **Start from a point file** — this is what v1.5 added, and it covers any
   profile published or drawn elsewhere. See
   [CST re-parameterisation](#cst-re-parameterisation).
2. **Go through Fusion** — model whatever you like, expose User Parameters, and
   run the driver from Fusion in `parameters` mode. The system then only drives
   your dimensions.
3. **Add a parameterisation** — `profile_from_parameters()` in
   `fusion/parametric_driver.py` recognises the parameterisation from its
   parameters and returns a “plan”; everything downstream (validation, meshing,
   CFD, loop, report) is independent of the shape.

In all cases `design_params.yaml` must list the parameters with their bounds, and
the names must match exactly.

---

## Where v1.5 stands

The §10 criteria of the master document, and what backs them:

| criterion | state | evidence |
|---|---|---|
| v1.0 still runs | ✅ | its code is frozen at tag `v1.0-stable`, never modified, and still exercised within v1.5 |
| A CSV/DAT profile ingested and re-parameterised with low error | ✅ | Clark Y: 3.25 × 10⁻⁴ of chord maximum deviation |
| The three shape quantities under explicit control | ✅ | bounds written and checked at every iteration |
| An optimisation of ≥ 15 iterations completes reliably | ✅ | 25 iterations, **0 failures** |
| Both geometry producers work | ✅ | internal exercised continuously; Fusion covered by an `adsk` API emulation |
| A complete `best_design` package produced automatically | ✅ | [versioned example](docs/example_report_clarky/) |
| Clear, working instructions to continue the design in CAD | ✅ | `FUSION_RETURN.md` + generated script + round-trip check |
| A structure ready for a 3D backend | ✅ | see below |

The four input modes of §3:

| mode | state |
|---|---|
| 1 — native parametric (NACA) | ✅ kept from v1.0 |
| 2 — ordered points (CSV/DAT) | ✅ Selig, Lednicer, CSV |
| 3 — 2D CAD | ✅ **DXF** with no dependency; ✅ **STEP** with `requirements-cad.txt` |
| 4 — existing Fusion design | ✅ `FUSION_GEOMETRY_BACKEND=fusion` in `parameters` mode |

**The only substantive reservation concerns Fusion.** The producer and the
return path are exercised against an **emulation** of the `adsk` API, not against
Fusion 360 — which has no headless mode and therefore cannot be driven from an
automated loop. A fake validates the driver's logic, not its reading of the API:
a bug of exactly that kind once slipped past 206 tests because the double encoded
the same misunderstanding as the code. Confirming both requires a real Fusion
session, and nothing else can settle it.

Note that this reservation does not affect the open-source path: the internal
producer, the STEP export and the STEP ingestion are all exercised for real,
because OpenCASCADE runs headless.

---

## Towards 3D (v2.0)

v1.5 deliberately stayed in 2D, but its architecture was built so that moving to
3D is an addition rather than a rewrite. Three things prepare the ground:

**The `GeometryBackend` interface assumes nothing about dimensionality.** It
returns a `GeometryResult` with an STL and a report; a producer that stacks
several twisted sections would fill it the same way. A third backend registers
itself with a decorator, without touching anything else — a test verifies this on
a fictitious backend.

**The parameterisation is recognised, not assumed.** The `parameterization` field
and detection from parameter names leave room for a `cst3d` value without
breaking existing files. A 3D shape is naturally described as several sets of CST
coefficients along the span, plus twist and taper laws.

**What is already generic stays that way.** OpenFOAM case dimensioning derives
from the bounding box, not from a 2D assumption; the reconstruction gate, the
round-trip check and the geometric-authority bounds all transpose station by
station.

What will have to change, on the other hand: the symmetry planes of the quasi-2D
case will give way to a real 3D domain, the CFD cost per iteration will rise by
an order of magnitude, and pattern search over twenty-four variables will
probably have to give way to a gradient-based method — that, and not the
geometry, is where the real obstacle lies.

---

## A note on language

This README is in English, and so is everything the system **produces**: the
optimisation reports, the `FUSION_RETURN.md`, the generated CAD script, the
figure labels.

What remains in French is the **code itself** — its comments, its docstrings,
and the console output of the command-line tools. That is the author's
language, and translating it is follow-up work rather than an oversight. The
console excerpts shown in this README are therefore reproduced **verbatim**, so
that what you read here is what the software actually prints.

## Licence

No licence is declared to date: all rights reserved by default. Add a `LICENSE`
file if you wish to allow reuse.
