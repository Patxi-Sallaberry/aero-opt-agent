# Optimised design — `CLARK_Y_AIRFOIL`

Best of the **25 iterations** of run `iterations_clarky` (25 succeeded, 0 failed), selected on objective `maximize_Cl_Cd`.

## Performance

| | Cd | Cl | Cl/Cd |
|---|---|---|---|
| Start (iteration 0) | 0.02725 | 0.76590 | 28.11 |
| **Optimised (iteration 24)** | **0.02653** | **0.77841** | **29.34** |

**Lift-to-drag gain: +4.4 %**

Mesh: 81594 cells, non-orthogonality 74.4, skewness 4.52. Coefficients averaged over 120 iterations, relative standard deviation 1.2e-04 on Cd — stabilised.

## Parameters: start → finish

| parameter | start | finish | change | bounds |
|---|---|---|---|---|
| `cst_upper_0` | 0.129395 | **0.129395** unitless | unchanged | 0.0804316 … 0.178358 |
| `cst_upper_1` | 0.458745 | **0.454158** unitless | -1.0 % | 0.324895 … 0.592596 |
| `cst_upper_2` | -0.412568 | **-0.412568** unitless | unchanged | -0.55456 … -0.270577 |
| `cst_upper_3` | 1.67908 | **1.67908** unitless | unchanged | 1.52856 … 1.82959 |
| `cst_upper_4` | -2.16544 | **-2.20571** unitless | -1.9 % | -2.3257 … -2.00519 |
| `cst_upper_5` | 3.26636 | **3.26636** unitless | unchanged | 3.0945 … 3.43821 |
| `cst_upper_6` | -2.67847 | **-2.67847** unitless | unchanged | -2.86468 … -2.49226 |
| `cst_upper_7` | 2.43253 | **2.43253** unitless | unchanged | 2.22782 … 2.63725 |
| `cst_upper_8` | -1.01333 | **-1.01333** unitless | unchanged | -1.24332 … -0.783351 |
| `cst_upper_9` | 0.781425 | **0.781425** unitless | unchanged | 0.513899 … 1.04895 |
| `cst_upper_10` | 0.0775655 | **0.0775655** unitless | unchanged | -0.25476 … 0.409891 |
| `cst_upper_11` | 0.273441 | **0.270706** unitless | -1.0 % | 0.0777792 … 0.469102 |
| `cst_lower_0` | -0.17699 | **-0.17699** unitless | unchanged | -0.225954 … -0.128027 |
| `cst_lower_1` | 0.00131412 | **0.0280843** unitless | +0.02677 (+10 % of the range) | -0.132537 … 0.135165 |
| `cst_lower_2` | -0.283066 | **-0.283066** unitless | unchanged | -0.425057 … -0.141074 |
| `cst_lower_3` | 0.275626 | **0.275626** unitless | unchanged | 0.12511 … 0.426143 |
| `cst_lower_4` | -0.540046 | **-0.540046** unitless | unchanged | -0.700301 … -0.379791 |
| `cst_lower_5` | 0.51587 | **0.51587** unitless | unchanged | 0.34402 … 0.687721 |
| `cst_lower_6` | -0.59732 | **-0.537588** unitless | +10.0 % | -0.783533 … -0.411108 |
| `cst_lower_7` | 0.37281 | **0.369082** unitless | -1.0 % | 0.168098 … 0.577522 |
| `cst_lower_8` | -0.291223 | **-0.291223** unitless | unchanged | -0.521205 … -0.0612405 |
| `cst_lower_9` | 0.0696012 | **0.123106** unitless | +76.9 % | -0.197925 … 0.337127 |
| `cst_lower_10` | -0.0745027 | **-0.0745027** unitless | unchanged | -0.406828 … 0.257822 |
| `cst_lower_11` | -0.0298241 | **-0.0298241** unitless | unchanged | -0.225486 … 0.165837 |
| `chord` | 300 | **300** mm | unchanged | 210 … 420 |
| `span` | 80 | **80** mm | unchanged | 79.2 … 80.8 |
| `aoa` | 3 | **3** deg | unchanged | -2 … 12 |

## Before / after

The starting seed against the retained design, both measured **in the same CFD regime** (same regime) — comparing a fine mesh against an exploration mesh would inflate the gain without it being real.

![Performance before / after](figures/comparison_performance.svg)

| | seed | optimised | change |
|---|---|---|---|
| **Lift Cl** | 0.7659 | **0.7784** | +1.6 % ✓ |
| **Drag Cd** | 0.02725 | **0.02653** | -2.6 % ✓ |
| **Lift-to-drag Cl/Cd** | 28.11 | **29.34** | +4.4 % ✓ |

![Sections before / after](figures/comparison_sections.svg)

Both sections are drawn at the **same scale**: each fitted to its own frame, they would look identical, and both the chord difference and the incidence would go unnoticed.

![Superimposed sections](figures/comparison_overlay.svg)

### Pressure, before and after

![Cp before / after](figures/comparison_cp.svg)

The area between the upper-surface curve and the lower-surface curve *is* the lift. The optimised design deepens its upper-surface suction and spreads it along the chord: that is where the extra lift is won.

### The fields, side by side

<!-- side-by-side -->
![Seed — pressure_field](figures/seed_pressure_field.png)
![Optimised — pressure_field](figures/pressure_field.png)
<!-- /side-by-side -->

<!-- side-by-side -->
![Seed — streamlines](figures/seed_streamlines.png)
![Optimised — streamlines](figures/streamlines.png)
<!-- /side-by-side -->

Same colour scale on both sides — that is the condition for the comparison to mean anything. The upper-surface suction, in blue, is markedly stronger and more extensive after optimisation.

## Why this shape is better

- **Camber increased** (0.0344 → 0.0351). Camber shifts the whole lift curve: a cambered profile already lifts at zero incidence. It is paid for in form drag and in pitching moment, hence the existence of an optimum rather than endless growth.
- **Profile thinned** (0.1171 → 0.1118 relative thickness). A thinner profile disturbs the flow less and drags less. The counterpart is structural — less inertia, so less stiffness — and a more abrupt stall, since a sharper leading edge copes badly with high incidence.
- **The trade-off in numbers**: lift gains +2 % *and* drag falls by 3 %. Both terms improve at once — a favourable case, which here comes from the reference area following the chord.
- **What the pressure distribution shows**: the suction peak reaches Cp = -1.46 at 11 % of chord. The upper surface does the work — the suction there pulls the profile upwards, and it weighs far more than the lower-surface overpressure. A very deep peak followed by an abrupt recovery would signal separation; a gradual recovery, as here, indicates flow that is still attached.

## Course of the optimisation

![Lift-to-drag over the iterations](figures/optimization_progress.svg)

![Cd and Cl over the iterations](figures/coefficients_progress.svg)

| iteration | Cd | Cl | Cl/Cd | status |
|---|---|---|---|---|
| 0 | 0.02725 | 0.76590 | 28.11 | OK |
| 1 | 0.03389 | 0.92242 | 27.22 | OK |
| 2 | 0.02747 | 0.76614 | 27.89 | OK |
| 3 | 0.03175 | 0.73545 | 23.17 | OK |
| 4 | 0.02720 | 0.76577 | 28.15 | OK |
| 5 | 0.02732 | 0.75113 | 27.49 | OK |
| 6 | 0.02946 | 0.72521 | 24.62 | OK |
| 7 | 0.03097 | 0.75162 | 24.27 | OK |
| 8 | 0.02884 | 0.69534 | 24.11 | OK |
| 9 | 0.02962 | 0.77330 | 26.11 | OK |
| 10 | 0.02818 | 0.71942 | 25.53 | OK |
| 11 | 0.02885 | 0.75603 | 26.21 | OK |
| 12 | 0.02685 | 0.75245 | 28.02 | OK |
| 13 | 0.02683 | 0.75766 | 28.24 | OK |
| 14 | 0.02710 | 0.76027 | 28.06 | OK |
| 15 | 0.02716 | 0.76116 | 28.03 | OK |
| 16 | 0.02700 | 0.75817 | 28.08 | OK |
| 17 | 0.02711 | 0.77172 | 28.46 | OK |
| 18 | 0.02699 | 0.76300 | 28.27 | OK |
| 19 | 0.02713 | 0.77821 | 28.69 | OK |
| 20 | 0.02710 | 0.77084 | 28.44 | OK |
| 21 | 0.02665 | 0.74813 | 28.07 | OK |
| 22 | 0.02888 | 0.71330 | 24.70 | OK |
| 23 | 0.03106 | 0.77341 | 24.90 | OK |
| 24 ⭐ | 0.02653 | 0.77841 | 29.34 | OK |

## The flow

![Cp distribution](figures/cp_distribution.svg)

Cp axis inverted, as convention requires: the upper curve is the upper surface, in suction. The area between the two curves is the lift.

![Pressure field around the profile](figures/pressure_field.png)

**Pressure field.** The red under the leading edge is the stagnation point, where the flow comes to rest (Cp = +1). The blue above is the suction that lifts the profile.

![Velocity magnitude and wake](figures/velocity_field.png)

**Velocity magnitude.** The wake can be read behind the trailing edge; the thinner it is, the less the profile drags.

![Streamlines](figures/streamlines.png)

**Streamlines**, coloured by velocity. The acceleration over the upper surface is the counterpart of the suction: this is Bernoulli's theorem, where accelerating fluid sees its pressure drop.

### Solver convergence

![Convergence](figures/solver_convergence.svg)

Flat curves towards the end are the condition for the coefficients to mean anything.

## What these numbers are worth

The `kOmegaSST` turbulence model assumes a turbulent boundary layer from the leading edge. At Re ≈ 4 × 10⁵ a good part of the upper surface is still laminar: **drag is overestimated**, by a factor that can approach 2. These values rank shapes against each other correctly — which is what an optimisation requires — but do not constitute a prediction of absolute drag. For a publishable figure you need a model with laminar-turbulent transition, or a wind tunnel.

## Folder contents

| file | what |
|---|---|
| `geometry.stl` | the geometry, **in metres**, as simulated |
| `geometry.step` | the same, as a CAD solid |
| `profile_section.csv` | 2D section in millimetres |
| `profile_section.dat` | same section in airfoil format |
| `profile_chord.dat` | profile **straightened**, unit chord — for XFOIL / XFLR5 |
| `design_params.yaml` | the exact parameters, replayable |
| `results.json` | the coefficients |
| `report.html` | this report, self-contained, for a browser |
| `FUSION_RETURN.md` | how to carry this design back into CAD |
| `rebuild_in_fusion.py` | CAD script that redraws the profile |
| `figures/` | curves and images |
| `cfd/` | OpenFOAM case: mesh and final fields |
| `logs/` | logs of each step |

## Continuing this design in CAD

An optimisation that only returns an STL is a design dead end: a faceted solid of several hundred faces can neither be filleted properly nor re-dimensioned. Several routes bring this shape back into editable CAD, detailed in **`FUSION_RETURN.md`**.

| route | what you get | when to choose it |
|---|---|---|
| **0. Open `geometry.step`** | native solid, one double-click | the simplest — works in FreeCAD as well as Fusion |
| **1. Replay the parameters** | native model, full history | as soon as a starting model exists — the only genuinely parametric route |
| **2. `rebuild_in_fusion.py` script** | sketch + extrusion, hands off | no starting model; nothing to locate or convert |
| **3. Import `profile_section.csv`** | sketch drawn by hand | to stay in control, or work in another CAD package |

```bash
# route 1: replay the parameters in Fusion
cp design_params.yaml <project>/configs/design_params.yaml
# then Utilities → ADD-INS → Scripts → fusion/parametric_driver.py

# route 2: standalone script
# Utilities → ADD-INS → Scripts → + → rebuild_in_fusion.py → Run
```

**Incidence is already in the coordinates** of the exported section: this is the geometry that was actually simulated. If a downstream setup applies an incidence of its own, it would be counted twice.

## Opening the files

```bash
# the geometry (STL in metres)
paraview geometry.stl

# the CFD fields
paraview cfd/best_design.foam

# regenerate the visuals after a change
xvfb-run -a pvbatch paraview_render.py cfd figures 20 1.225

# resume the optimisation from this design
cp design_params.yaml configs/design_params.yaml
python3 scripts/run_loop.py --max-iterations 20 \
    --cfd-settings configs/cfd_settings_fast.yaml
```

In ParaView, the final time step carries `U` (velocity), `p` (**kinematic** pressure, in m²/s² — multiply by ρ = 1.225 kg/m³ for pascals), `k`, `omega` and `nut`. The `wing` patch is the wing surface.

---

Exported on 24/08/2026 at 19:41 UTC by `scripts/export_best.py`.
