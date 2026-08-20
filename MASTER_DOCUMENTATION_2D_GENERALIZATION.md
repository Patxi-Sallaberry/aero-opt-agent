# MASTER DOCUMENTATION — Universal 2D Aerodynamic Optimizer (v1.5)
## Freeze v1.0 → Build the best possible 2D system → Keep Fusion + Internal backends → Enable clean return to Fusion

**Document version :** 1.1 (Ultra-detailed Masterclass)  
**Date :** 20 August 2026  
**Audience :** Claude Code  
**Status :** Absolute single source of truth.  
**Non-negotiable rule :** The existing working system (v1.0) must never be modified. All work is done on an isolated copy.

---

## 0. Strategic Intent (Locked)

### Goal of this phase
Create the most reliable, usable and complete **2D parametric aerodynamic shape optimization system** possible, with these concrete capabilities:

1. Accept multiple forms of 2D input (parametric, point cloud, simple CAD)
2. Optimize the shape for a clear aerodynamic objective
3. Work with **both** backends:
   - Fusion 360 (when available)
   - Internal geometry calculator (always available, already proven)
4. At the end of the optimization, produce a geometry that can be **cleanly re-imported into Fusion** so the user can continue working on it manually
5. Keep the excellent automatic reporting already developed
6. Prepare clean interfaces so that the future 3D version can reuse as much as possible

### Working method
- Create a full copy of the current project → `aero-opt-agent-v1.5-universal-2d` (or equivalent branch)
- Never touch the original v1.0
- Develop exclusively in the copy

---

## 1. North Star User Experience

**Ideal flow the system must support:**

```
User provides a 2D profile
        ↓
System re-parameterizes it safely
        ↓
Autonomous optimization loop (Fusion or Internal backend)
        ↓
Best design package is generated
        ↓
User can:
  - View full HTML report + visuals
  - Take the optimized profile and open/edit it in Fusion
  - Continue manual design work from the optimized shape
```

The return path to Fusion is a first-class requirement, not an afterthought.

---

## 2. Dual Geometry Backend (Critical Design)

The system must maintain **two geometry backends** behind a single clean interface.

### Backend A — Internal (always available)
- Already implemented and validated in v1.0
- Generates profile from parameters (NACA / CST / equivalent)
- Writes STL (and profile coordinates) directly
- Used when Fusion is not available or for fast iteration
- Must remain the default reliable path

### Backend B — Fusion 360
- Uses the existing parametric driver approach
- Reads `design_params.yaml`
- Updates User Parameters
- Exports STEP + STL
- Preferred when the user wants a real CAD model

### Interface contract
```python
class GeometryBackend:
    def generate(self, design_params: dict, output_dir: Path) -> GeometryResult:
        """
        Returns:
          - success: bool
          - stl_path: Path | None
          - step_path: Path | None
          - profile_coordinates: np.ndarray | None
          - message: str
        """
```

The rest of the pipeline (CFD, optimizer, reporting) must only talk to this interface.  
Switching backend must be a configuration choice (`geometry_backend: "auto" | "internal" | "fusion"`).

---

## 3. Input Modes (Universal 2D)

### Mode 1 — Native parametric (already working)
- Existing NACA / internal parametric model
- Highest reliability
- Keep fully supported

### Mode 2 — Ordered point profile (CSV / DAT)
- Standard airfoil format (x, y upper + lower or closed)
- Most important new mode for universality
- Pipeline:
  1. Load and clean points
  2. Ensure closed, consistent orientation, trailing-edge handling
  3. Fit a stable parametric model (CST recommended)
  4. Validate reconstruction error
  5. Write initial `design_params.yaml`

### Mode 3 — Simple 2D CAD (STEP / DXF)
- Extract outer contour
- Convert to ordered points
- Then same path as Mode 2

### Mode 4 — Existing Fusion design
- Direct use of the Fusion backend when the model is already parametric

**Priority order for implementation:** Mode 1 (keep) → Mode 2 (CSV/DAT) → Mode 3 → Mode 4.

---

## 4. Re-parameterization (the heart of reliability)

**Never optimize raw point coordinates.**

### Primary method (recommended)
**CST (Class-Shape Transformation)**  
- Standard in aerodynamic literature
- Smooth by construction
- Low number of coefficients (typically 6–12 per surface or combined)
- Good control of leading-edge radius and trailing-edge behaviour

### Fallback
- Thickness + camber mode decomposition
- Or B-spline with strong regularization and few control points

### Mandatory reconstruction gate
After fitting:
- Reconstruct the profile from the parameters
- Compute maximum and mean geometric error vs original
- Reject or warn if error > threshold (e.g. 0.05 % chord for max error)
- This prevents the optimizer from starting from a bad representation

### Design variables rules
- Every variable has `value`, `min`, `max`, `max_delta_pct`
- Leading-edge radius, max thickness, trailing-edge thickness must stay under explicit control
- Initial parameters must be able to recover the uploaded shape closely

---

## 5. Return to Fusion (Clean Round-Trip)

This is a key requirement you asked for.

At the end of an optimization the system must make it easy to continue working in Fusion.

### Method 1 — Preferred when Fusion backend was used
- The best `design_params.yaml` is kept
- User re-opens the original parametric Fusion model
- Runs the driver once with the best parameters
- Obtains a native Fusion design that matches the optimized shape
- Can then edit features, add fillets, change 3D operations, etc.

### Method 2 — When only Internal backend was used
The system must still produce CAD-friendly outputs:

1. **High-quality ordered profile coordinates** (`profile_section.csv` / `.dat`)
2. Clear instructions in the report:
   - How to import the point cloud into Fusion as a sketch
   - How to fit a spline and extrude / revolve
3. Optional helper script that generates a simple Fusion script to rebuild the sketch from the CSV

### Method 3 — Hybrid (best of both)
- Always write the best parameters in a form that the Fusion driver understands
- If a matching parametric Fusion seed exists, the user can regenerate a native model in one click
- Otherwise fall back to the clean profile import path

**The report.html must contain a dedicated section: “How to continue this design in Fusion 360”.**

---

## 6. Full Architecture (v1.5)

```
                    ┌─────────────────────────┐
                    │   Input Profile         │
                    │ (CSV / DAT / STEP /     │
                    │  Parametric / Fusion)   │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  Ingestion + Cleaning   │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  Re-parameterization    │
                    │  (CST + validation)     │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  design_params.yaml     │  ← only file modified by optimizer
                    └───────────┬─────────────┘
                                │
              ┌─────────────────┴─────────────────┐
              │                                   │
   ┌──────────▼──────────┐             ┌──────────▼──────────┐
   │  Internal Backend   │             │   Fusion Backend    │
   │  (always available) │             │  (when available)   │
   └──────────┬──────────┘             └──────────┬──────────┘
              │                                   │
              └─────────────────┬─────────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  OpenFOAM CFD Pipeline  │
                    │  (existing, robust)     │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  Optimizer / Agent      │
                    └───────────┬─────────────┘
                                │
                    ┌───────────▼─────────────┐
                    │  Best Design Package    │
                    │  + Fusion return path   │
                    └─────────────────────────┘
```

---

## 7. Reliability Gates (must stay extremely strict)

1. Input profile validity (closed, manifold, sensible thickness)
2. Reconstruction error after parameterization
3. Geometry generation success
4. Mesh quality (existing checkMesh logic)
5. Solver convergence + physical plausibility
6. Parameter change magnitude (`max_delta_pct`)

Any failure → clear logging + conservative reaction from the optimizer.  
Target success rate on clean inputs: **≥ 95 %**.

---

## 8. Reporting Requirements (Portfolio grade)

Every run must produce:

```
results/run_YYYYMMDD_HHMMSS/best_design/
├── report.html                 # self-contained, beautiful
├── geometry.stl
├── profile_section.csv         # for Fusion / XFOIL re-import
├── profile_section.dat
├── design_params.yaml          # best parameters
├── results.json
├── figures/
│   ├── before_after_overlay.svg
│   ├── cp_comparison.*
│   └── ...
├── cfd/                        # full case for ParaView
└── FUSION_RETURN.md            # exact instructions to reopen in Fusion
```

The before/after comparison must be visually obvious.

---

## 9. Implementation Roadmap (ordered & detailed)

### Phase 0 — Isolation (mandatory first step)
- Create full copy of current project
- Name it clearly (`...-v1.5-universal-2d`)
- Run the existing end-to-end test inside the copy
- Confirm original v1.0 is untouched
- Tag original as `v1.0-stable`

### Phase 1 — Clean GeometryBackend interface
- Extract current internal + Fusion logic behind a common interface
- Configuration switch: `auto | internal | fusion`
- All existing tests must still pass

### Phase 2 — Profile ingestion (CSV/DAT)
- Robust loader
- Orientation, closing, basic smoothing options
- Validation

### Phase 3 — CST (or chosen) re-parameterization
- Fitting algorithm
- Reconstruction error metric + gate
- Writing of initial `design_params.yaml`

### Phase 4 — Geometry generation from new parameters
- Internal backend produces profile + STL
- Fusion backend remains compatible
- Round-trip test: parameters → geometry → parameters

### Phase 5 — Full loop integration
- Existing optimizer works with new parameter schema
- End-to-end optimization on a classic airfoil (e.g. from UIUC database)

### Phase 6 — Fusion return path + reporting polish
- `FUSION_RETURN.md` + section inside `report.html`
- Before/After figures of high quality
- Profile export in multiple formats

### Phase 7 — Documentation & demo
- README with clear examples
- “Optimize any airfoil in 3 commands”
- Notes on how this prepares the 3D path

---

## 10. Success Criteria

v1.5 is done when:

- [ ] Original v1.0 still runs perfectly
- [ ] A CSV/DAT airfoil can be ingested and re-parameterized with low error
- [ ] An optimization of ≥ 15 iterations completes reliably
- [ ] Both Internal and Fusion backends can generate geometry
- [ ] A complete best_design package is produced automatically
- [ ] Clear, working instructions exist to continue the optimized design in Fusion
- [ ] Code structure is clean enough for a future 3D backend

---

## 11. Direct Instructions to Claude Code

1. Read this document in full.
2. Execute **Phase 0** first and report confirmation that the copy works and the original is safe.
3. Then proceed phase by phase without skipping.
4. Keep the dual-backend design clean.
5. Treat the “return to Fusion” path as a core feature.
6. Maintain the same high reliability standards that made v1.0 successful.
7. At the end of each phase, give a short status + how to test.

**Final goal of this stage:**  
A universal, extremely reliable 2D aerodynamic optimizer that accepts real profiles, works with both Fusion and the internal engine, produces portfolio-grade outputs, and lets the user take the optimized shape back into Fusion for manual work — while leaving a clean road toward 3D.

This document is the law.

**End of Master Documentation v1.1 — Universal 2D**
