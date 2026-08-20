"""Rendu des visuels CFD par ParaView, en lot et sans affichage.

    pvbatch scripts/paraview_render.py <case_dir> <sortie> [U_inf] [rho]
    xvfb-run -a pvbatch scripts/paraview_render.py ...   (si le rendu réclame un X)

Produit trois images :

    pressure_field.png    champ de Cp autour du profil
    streamlines.png       lignes de courant colorées par la vitesse
    velocity_field.png    module de la vitesse, où le sillage se lit

Ce fichier s'exécute dans l'interpréteur de ParaView, pas dans celui du
projet : il n'importe rien du dépôt et ne dépend que du module `paraview`.
C'est aussi pour cela qu'il est livré tel quel dans le dossier de résultats —
il reste rejouable par l'utilisateur, avec ou sans le reste du système.

Le plan de coupe est pris à mi-envergure : le calcul étant quasi-2D, il y est
représentatif de tout l'écoulement.
"""

import sys

from paraview.simple import (  # type: ignore[import-not-found]
    ColorBy,
    Calculator,
    GetActiveViewOrCreate,
    GetColorTransferFunction,
    GetScalarBar,
    Hide,
    FeatureEdges,
    HideScalarBarIfNotNeeded,
    OpenFOAMReader,
    SaveScreenshot,
    Show,
    Slice,
    StreamTracer,
    Tube,
    UpdatePipeline,
)


def foam_file(case_dir):
    """Chemin du fichier `.foam` que le lecteur ParaView attend.

    ParaView n'ouvre pas un dossier de case : il lui faut un fichier, vide, qui
    lui sert seulement de point d'entrée. On le crée s'il manque.
    """
    import os

    if os.path.isfile(case_dir):
        return case_dir
    for name in sorted(os.listdir(case_dir)):
        if name.endswith(".foam"):
            return os.path.join(case_dir, name)
    path = os.path.join(case_dir, "case.foam")
    open(path, "w").close()
    return path


def render(case_dir, output_dir, u_inf=20.0, rho=1.225):
    case_file = foam_file(case_dir)
    reader = OpenFOAMReader(registrationName="case", FileName=case_file)
    reader.MeshRegions = ["internalMesh"]
    reader.CellArrays = ["U", "p", "k", "omega", "nut"]
    reader.UpdatePipeline()

    times = reader.TimestepValues or [0.0]
    last = times[-1]

    view = GetActiveViewOrCreate("RenderView")
    view.ViewSize = [1400, 800]
    view.OrientationAxesVisibility = 0
    view.CameraParallelProjection = 1
    view.Background = [1.0, 1.0, 1.0]
    view.UseColorPaletteForBackground = 0

    bounds = reader.GetDataInformation().GetBounds()
    z_mid = (bounds[4] + bounds[5]) / 2.0

    # Coupe à mi-envergure : l'écoulement est quasi-2D, elle le résume.
    cut = Slice(registrationName="mid", Input=reader)
    cut.SliceType = "Plane"
    cut.SliceType.Origin = [0.0, 0.0, z_mid]
    cut.SliceType.Normal = [0.0, 0.0, 1.0]
    UpdatePipeline(time=last, proxy=cut)

    # p est une pression cinématique (m2/s2) : Cp = p / (0.5 U_inf^2).
    cp = Calculator(registrationName="Cp", Input=cut)
    cp.AttributeType = "Point Data"
    cp.ResultArrayName = "Cp"
    cp.Function = "p/%.10f" % (0.5 * u_inf * u_inf)
    UpdatePipeline(time=last, proxy=cp)

    # Cadrage serré sur le profil et son sillage proche.
    chord = 0.0
    wing = None
    try:
        wing = OpenFOAMReader(registrationName="wing", FileName=case_file)
        wing.MeshRegions = ["patch/wing"]
        wing.UpdatePipeline()
        wb = wing.GetDataInformation().GetBounds()
        chord = max(wb[1] - wb[0], 1e-6)
        center_x = (wb[0] + wb[1]) / 2.0
        center_y = (wb[2] + wb[3]) / 2.0
    except Exception:
        center_x, center_y = 0.0, 0.0
        chord = max(bounds[1] - bounds[0], 1e-6) / 20.0

    view.CameraPosition = [center_x + chord * 0.4, center_y, z_mid + 10 * chord]
    view.CameraFocalPoint = [center_x + chord * 0.4, center_y, z_mid]
    view.CameraViewUp = [0.0, 1.0, 0.0]
    view.CameraParallelScale = chord * 1.15

    written = []

    # ── 1. Champ de Cp ────────────────────────────────────────────────────
    display = Show(cp, view)
    display.Representation = "Surface"
    ColorBy(display, ("POINTS", "Cp"))
    lut = GetColorTransferFunction("Cp")
    lut.ApplyPreset("Cool to Warm", True)
    # Plage SYMÉTRIQUE, et fixe. Une palette divergente place son neutre au
    # milieu de la plage : avec [-2, 1], le blanc tomberait à Cp = -0,5 et
    # l'écoulement non perturbé, à Cp = 0, apparaîtrait déjà coloré — on lirait
    # une surpression là où il n'y en a pas. Fixe, parce que deux images ne se
    # comparent que si elles partagent leur échelle.
    lut.RescaleTransferFunction(-2.0, 2.0)
    bar = GetScalarBar(lut, view)
    bar.Title = "Cp"
    bar.ComponentTitle = ""
    bar.TitleColor = [0.1, 0.1, 0.1]
    bar.LabelColor = [0.1, 0.1, 0.1]
    display.SetScalarBarVisibility(view, True)
    SaveScreenshot(output_dir + "/pressure_field.png", view,
                   ImageResolution=[1400, 800], TransparentBackground=0)
    written.append("pressure_field.png")

    # ── 2. Module de la vitesse ──────────────────────────────────────────
    ColorBy(display, ("POINTS", "U", "Magnitude"))
    u_lut = GetColorTransferFunction("U")
    u_lut.ApplyPreset("Viridis (matplotlib)", True)
    u_lut.RescaleTransferFunction(0.0, u_inf * 1.6)
    u_bar = GetScalarBar(u_lut, view)
    u_bar.Title = "|U| (m/s)"
    u_bar.ComponentTitle = ""
    u_bar.TitleColor = [0.1, 0.1, 0.1]
    u_bar.LabelColor = [0.1, 0.1, 0.1]
    display.SetScalarBarVisibility(view, True)
    view.CameraParallelScale = chord * 1.8
    SaveScreenshot(output_dir + "/velocity_field.png", view,
                   ImageResolution=[1400, 800], TransparentBackground=0)
    written.append("velocity_field.png")

    # ── 3. Lignes de courant ─────────────────────────────────────────────
    # La coupe est masquée : ses facettes sont EXACTEMENT dans le plan des
    # lignes de courant, et le conflit de profondeur hachait celles-ci en
    # pointillés. On montre à la place la surface de l'aile, qui est un solide
    # et se détache donc proprement.
    display.SetScalarBarVisibility(view, False)
    Hide(cp, view)
    HideScalarBarIfNotNeeded(lut, view)

    # Silhouette du profil : le contour du TROU que l'aile laisse dans la
    # coupe. C'est la façon la plus sûre de la tracer — la coupe se rend
    # correctement, alors que la surface du patch, elle, se prête mal au rendu
    # dans ce montage. Le bord extérieur du domaine est également extrait, mais
    # il tombe très au delà du cadrage.
    outline = FeatureEdges(registrationName="contour", Input=cut)
    outline.BoundaryEdges = 1
    outline.FeatureEdges = 0
    outline.NonManifoldEdges = 0
    outline.ManifoldEdges = 0
    UpdatePipeline(time=last, proxy=outline)

    outline_display = Show(outline, view)
    outline_display.Representation = "Surface"
    outline_display.ColorArrayName = [None, ""]
    outline_display.DiffuseColor = [0.1, 0.1, 0.1]
    outline_display.AmbientColor = [0.1, 0.1, 0.1]
    outline_display.LineWidth = 3.0

    tracer = StreamTracer(registrationName="lignes", Input=cp, SeedType="Line")
    tracer.Vectors = ["POINTS", "U"]
    # Semis vertical en amont du bord d'attaque : les lignes balaient alors
    # l'extrados et l'intrados, et le contournement se voit.
    tracer.SeedType.Point1 = [center_x - chord * 0.9, center_y - chord * 0.7, z_mid]
    tracer.SeedType.Point2 = [center_x - chord * 0.9, center_y + chord * 0.7, z_mid]
    tracer.SeedType.Resolution = 28
    tracer.IntegrationDirection = "BOTH"
    tracer.MaximumStreamlineLength = chord * 10.0
    UpdatePipeline(time=last, proxy=tracer)

    tube = Tube(registrationName="tube", Input=tracer)
    tube.Radius = chord * 0.004
    UpdatePipeline(time=last, proxy=tube)

    tube_display = Show(tube, view)
    ColorBy(tube_display, ("POINTS", "U", "Magnitude"))
    u_lut.RescaleTransferFunction(0.0, u_inf * 1.6)
    tube_display.SetScalarBarVisibility(view, True)
    view.CameraParallelScale = chord * 0.85
    SaveScreenshot(output_dir + "/streamlines.png", view,
                   ImageResolution=[1400, 800], TransparentBackground=0)
    written.append("streamlines.png")

    Hide(tube, view)
    return written


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: pvbatch paraview_render.py <case_dir> <output_dir> "
              "[U_inf] [rho]")
        raise SystemExit(2)

    case = sys.argv[1]
    out = sys.argv[2]
    velocity = float(sys.argv[3]) if len(sys.argv) > 3 else 20.0
    density = float(sys.argv[4]) if len(sys.argv) > 4 else 1.225

    files = render(case, out, velocity, density)
    print("images:" + ",".join(files))
