# Carrying this design back into CAD

The result of an optimisation is only useful if you can carry on working with it. This document gives the ways to bring the optimised shape back into CAD, from the cleanest to the most universal.

## The shape to reproduce

| quantity | value |
|---|---|
| chord | 300.00 mm |
| span | 80.00 mm |
| incidence | 3.00° |
| relative thickness | 0.1118 |
| relative camber | 0.0351 |
| parameterisation | `cst` |
| source profile | `examples/profiles/clarky.dat` |
| CST order | 11 (24 coefficients) |

**Incidence is already in the coordinates.** The exported section is the one that was simulated, tilt included. If a downstream setup applies an incidence of its own, start from the straightened profile instead, otherwise it would be counted twice.

## Shortest route — open `geometry.step`

*File → Open*, or drag and drop the file into your CAD package. It is a real B-Rep solid of a few faces: you can fillet it, change its span, assemble it. No conversion, no script. **FreeCAD reads it just as well as Fusion.**

> **Do not open `geometry.stl` instead.** CAD will read it, but it turns it into a mesh body of several hundred flat facets, useless for design work. The STL is there for the solver and for printing.

This route does not give a *parametric* model: the solid has no feature history. For that, see route 1.

## Route 1 — replay the parameters (recommended)

This is the only route that gives a **parametric** model: an editable feature history, not a frozen import.

```bash
cp design_params.yaml <project>/configs/design_params.yaml
```

Then, in Fusion: open the starting model, go to *Utilities → ADD-INS → Scripts and Add-Ins*, and run `fusion/parametric_driver.py`. The driver rebuilds exactly this shape and exports STEP and STL.

> The driver accepts both parameterisations. On a `cst` file it rebuilds the shape from the Kulfan coefficients — its drawing code only handles points, so the Fusion route needs no extra support.

> This optimisation ran on the **internal** computer, without Fusion. The parameters remain perfectly replayable: it is the same file that describes the shape on both sides.

## Route 2 — ready-to-run script

`rebuild_in_fusion.py` contains the coordinates of THIS profile and draws the sketch on its own, then extrudes it over the span.

1. Fusion 360 → *Utilities → ADD-INS → Scripts and Add-Ins*
2. **Scripts** tab → **+** → choose `rebuild_in_fusion.py`
3. **Run**

The script draws **one spline per surface** rather than a single one over the whole contour: at the leading edge the curve turns back on itself, and a single spline would put an inflection point there instead of a nose — which would damage precisely the region that decides stall.

No file to locate, no unit to convert: the coordinates are written into the script, in centimetres, the internal unit of the Fusion API.

## Route 3 — import the section by hand

Useful if you prefer to stay in control, or to work in another CAD package.

1. Open `profile_section.csv` — three columns: `surface`, `x_mm`, `y_mm`.
2. In Fusion: *Insert → Insert Manufacturing Model* or a point-import add-in; otherwise, draw a spline by entering the points.
3. Fit a spline through the points of each surface.
4. Close the trailing edge, then extrude over 80.0 mm.

`profile_section.dat` carries the same points in the standard airfoil format.

`profile_chord.dat` carries the profile **straightened**, at unit chord. That is the one to give XFOIL or XFLR5: those tools drive the incidence themselves, and feeding them an already-tilted section would count it twice — the whole polar would be shifted with nothing to say so.

## What is better avoided

**Converting `geometry.stl` into a solid.** CAD can do it, but the result is a mesh of several hundred flat faces: impossible to fillet properly, impossible to re-dimension. The STL is there for simulation and printing, not for design.

## Checking that the transfer is faithful

After rebuilding, export an STL from your CAD package and compare it against the section in THIS folder:

```bash
python3 -m profiles.roundtrip exported.stl profile_chord.dat \
    --chord 300.0 --aoa 3.00
```

The tool reads the file back, extracts its section and measures its distance to the profile — trusting nothing that was used to write it. A deviation beyond 2 × 10⁻³ of chord signals an error of scale, unit or orientation.

The reference must be `profile_chord.dat` or `profile_section.dat`, **not the starting profile**. The design has been optimised: it departs from its starting point by design, and the tool would flag that intended difference as a defect.

Measured on the solid in this folder: the deviation between `design_params.yaml` and `geometry.stl` is of the order of 10⁻⁶ of chord — the generation chain is exact; what remains to be checked is what CAD makes of it.

