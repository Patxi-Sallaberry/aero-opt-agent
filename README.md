# aero-opt-agent — Optimisation Aérodynamique Agentique

**Fusion 360 + OpenFOAM + Orchestrateur LLM** — version **Core First v1.0**.

Le système prend un design CAO paramétrique Fusion 360, fait varier ses User
Parameters, simule l'écoulement sous OpenFOAM, et laisse un agent LLM proposer
les paramètres de l'itération suivante — dans les bornes, sans jamais toucher au
code du pipeline.

La spécification qui fait loi est
[`MASTER_DOCUMENTATION_AGENTIC_AERO_OPTIMIZATION.md`](MASTER_DOCUMENTATION_AGENTIC_AERO_OPTIMIZATION.md).
Ce README ne la remplace pas : en cas de divergence, le Master Document gagne.

---

## Etat d'avancement

| Phase | Contenu | Statut |
|-------|---------|--------|
| 0 | Fondations : structure, Git, configs, validation de `design_params.yaml` | **Terminée** |
| 1 | Geometry — `fusion/parametric_driver.py` | **Terminée** (validation sur seed réel en attente) |
| 2 | OpenFOAM — template de case, `run_cfd.sh`, `postprocess.py` | **Terminée et validée sur solveur réel** |
| 3 | Master Pipeline — enchaînement, validation, archivage | À faire |
| 4 | Agent + boucle d'optimisation | À faire |
| 5 | Durcissement, messages d'erreur, tests sur design réel | À faire |

---

## Règle d'or

> L'agent LLM ne touche **jamais** au code du pipeline.
> Il n'édite **que** `configs/design_params.yaml`.

Tout le reste — paramètres CFD, templates OpenFOAM, code Python — appartient au
concepteur. La validation de `pipeline/utils.py` fait respecter cette frontière :
une proposition d'agent qui desserre une borne, change une unité ou ajoute un
paramètre est rejetée.

---

## Installation

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt

cp .env.example .env      # puis remplir les clés / chemins
```

OpenFOAM est une dépendance **système** (non pip) ; Fusion 360 exécute le
driver dans son propre interpréteur Python embarqué.

```bash
# OpenFOAM (ESI) — fournit simpleFoam, snappyHexMesh, checkMesh
curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash
sudo apt-get install openfoam2506-default
```

Puis renseigner `FOAM_BASHRC` dans `.env`. En WSL ou en conteneur on est root,
et OpenMPI refuse alors de démarrer : `run_cfd.sh` lève cette protection
lui-même et le signale dans le journal.

---

## Structure du dépôt

```
.
├── configs/
│   ├── design_params.yaml     ← SEUL fichier modifié par l'agent
│   └── cfd_settings.yaml         conditions CFD, jamais touchées par l'agent
├── fusion/
│   ├── seed_design.f3d           fourni par l'utilisateur (non versionné)
│   └── parametric_driver.py      paramètres → recalcul → STEP  Phase 1 ✔
├── openfoam/
│   ├── templates/external_aero/  case de base            Phase 2 ✔
│   ├── case_builder.py           YAML → case dimensionné Phase 2 ✔
│   ├── run_cfd.sh                orchestration           Phase 2 ✔
│   └── postprocess.py            → results.json          Phase 2 ✔
├── pipeline/
│   ├── master_pipeline.py        point d'entrée unique   Phase 3
│   ├── geometry_validator.py                             Phase 3
│   └── utils.py                  chargement + validation Phase 0 ✔
├── agent/
│   ├── orchestrator.py           boucle principale       Phase 4
│   └── prompts/system_prompt.md                          Phase 4
├── data/iterations/              iter_0001/, iter_0002/… (non versionné)
├── tests/
└── scripts/run_loop.py                                   Phase 4
```

---

## Le contrat `design_params.yaml`

Trois familles de règles, appliquées par `pipeline/utils.py` :

1. **Structure** — clés obligatoires, types stricts, aucune clé inconnue.
   Chaque paramètre porte exactement `value`, `min`, `max`, `max_delta_pct`, `unit`.
2. **Bornes** — `min < max` et `min <= value <= max`.
3. **max_delta_pct** — une nouvelle `value` ne peut s'écarter de la dernière
   itération **réussie** de plus de `max_delta_pct` %. Quand la valeur
   précédente est nulle (le delta relatif n'a alors aucun sens — cas d'un angle
   d'incidence à 0°), le budget retombe sur `max_delta_pct` % de l'amplitude
   `max - min`.

Entre deux itérations, seule `value` peut changer : `min`, `max`,
`max_delta_pct` et `unit` sont figés, l'ensemble des paramètres aussi, et
`iteration` doit croître strictement.

### Valider une configuration

```bash
# Structure + bornes
python3 pipeline/utils.py configs/design_params.yaml

# + règle max_delta_pct contre la dernière itération réussie
python3 pipeline/utils.py configs/design_params.yaml \
    --previous data/iterations/iter_0003/design_params.yaml

# Intervalle réellement admissible à la prochaine itération, par paramètre
python3 pipeline/utils.py configs/design_params.yaml --show-ranges
```

Codes de retour : `0` valide, `1` contrat violé, `2` fichier illisible ou absent.
Toutes les violations sont listées en une seule passe, pour que l'agent corrige
d'un coup.

### Depuis Python

```python
from pipeline.utils import load_design_params, validate_design_params, allowed_range

cfg = load_design_params("configs/design_params.yaml")   # lève si invalide

report = validate_design_params(proposal, previous=last_successful)
if not report.ok:
    print(report.format())        # feedback à renvoyer à l'agent
```

---

## Le driver Fusion (`fusion/parametric_driver.py`)

Il applique les valeurs du YAML au design Fusion, obtient la géométrie
correspondante, et exporte `data/iterations/iter_XXXX/geometry.step`.

### Deux stratégies de géométrie

| Mode | Ce qu'il fait | Quand |
|------|---------------|-------|
| `rebuild` (défaut) | Met à jour les User Parameters, **puis reconstruit** la géométrie : profil NACA 4 chiffres retracé à la corde, l'épaisseur et la cambrure demandées, tourné de l'incidence, extrudé sur l'envergure. | Modèle dont les cotes ne sont pas réellement pilotées par ses paramètres — **le cas du seed livré**. |
| `parameters` | Met à jour les User Parameters et laisse Fusion recalculer. | Modèle réellement paramétrique. |

Le seed (`fusion/seed_design.f3d`, généré par un script NACA) crée bien ses
5 User Parameters, mais trace son profil par une spline passant par des points
calculés en dur et l'extrude sur une longueur brute. **Modifier ses paramètres
n'y déplace pas un point** : sans `rebuild`, chaque itération exporterait un
STEP identique et l'agent optimiserait dans le vide, sans la moindre erreur
pour le signaler.

Choix du mode : `--geometry-mode`, ou `FUSION_GEOMETRY_MODE` dans `.env`.

La forme du profil (famille NACA, position de cambrure `p = 0.4`, 80 points par
surface) est fixée dans `parametric_driver.py` : c'est une décision de
conception, ni de l'agent, ni de la CFD.

### Dans Fusion 360

1. Ouvrir le modèle paramétrique (ou déposer le seed en `fusion/seed_design.f3d`).
2. **Utilities > ADD-INS > Scripts and Add-Ins > Scripts > (+)** et pointer ce
   fichier, puis **Run**.

Le document actif est utilisé en priorité ; à défaut, le seed `.f3d` est importé
dans un nouveau document. `FUSION_FORCE_SEED_IMPORT=1` force toujours le seed.

Les noms de `parameters` dans `design_params.yaml` doivent correspondre
**exactement** aux User Parameters Fusion (*Modify > Change Parameters*). Sinon
le driver s'arrête sans rien modifier et liste les noms disponibles.

### Hors Fusion — mode simulation

Sans le module `adsk`, le driver valide la configuration, construit les
expressions et calcule les chemins, sans appeler aucune API :

```bash
python3 fusion/parametric_driver.py --dry-run
```

Codes de retour : `0` succès, `1` échec, `3` mode simulation (aucun STEP produit).

### Statut retourné

Le driver ne lève jamais : il retourne un dict et écrit le même contenu dans
`data/iterations/iter_XXXX/fusion_status.json` (avec le journal
`fusion_driver.log`). C'est le canal de retour vers le master pipeline, qui
s'exécute dans un autre processus que Fusion.

`status` vaut `OK`, `DRY_RUN`, ou l'une des causes d'échec : `CONFIG_ERROR`,
`SEED_MISSING`, `SEED_IMPORT_FAILED`, `NO_DESIGN`, `PARAM_NOT_FOUND`,
`PARAM_SET_FAILED`, `RECOMPUTE_FAILED`, `REBUILD_FAILED`, `GEOMETRY_EMPTY`,
`EXPORT_FAILED`, `FUSION_UNAVAILABLE`, `UNEXPECTED_ERROR`.

En mode `rebuild`, le statut porte aussi `geometry` : corde et envergure en cm,
ratios appliqués, incidence, et l'emprise de la géométrie produite.

---

## La chaîne CFD (`openfoam/`)

```bash
openfoam/run_cfd.sh --iteration-dir data/iterations/iter_0000
openfoam/run_cfd.sh --iteration-dir ... --dry-run     # construit le case sans calculer
openfoam/run_cfd.sh --iteration-dir ... --mesh-only   # s'arrête après checkMesh
```

Enchaînement : `case_builder.py` → `blockMesh` → `surfaceFeatureExtract` →
`snappyHexMesh` → `checkMesh` (obligatoire) → `simpleFoam` → `postprocess.py`.

### Ce que le case emprunte à chaque itération

`case_builder.py` ne recopie pas des constantes : il **dimensionne** le case à
partir de `design_params.yaml`. Domaine et taille de maille en multiples de
corde, `k` et `omega` depuis l'intensité turbulente, et surtout :

- **`Aref` et `lRef` sont recalculés à chaque itération** (corde × envergure du
  domaine, et corde). Figer la surface de référence pendant que la corde varie
  ferait bouger les Cd/Cl alors que seule la normalisation aurait changé.
- **La boîte englobante du STL est comparée à la géométrie demandée.** C'est le
  garde-fou qui détecte qu'une itération a exporté une géométrie inchangée ou
  mal mise à l'échelle — avant de dépenser une heure de calcul.

### Envergure et quasi-2D

Par défaut (`domain.spanwise_treatment: symmetry`), le domaine occupe une
tranche **intérieure** à l'aile, bornée par deux plans de symétrie que la
géométrie traverse : pas de bout d'aile, donc pas de tourbillon marginal. C'est
le modèle correct pour un tronçon de profil.

**Conséquence : `span` n'a alors aucun effet aérodynamique.** Le fixer plutôt
que le laisser à l'agent, ou passer en `full_3d` — au prix d'un maillage bien
plus lourd, et avec un allongement de 0,27 qui n'a plus grand-chose d'une aile.

### `results.json`

Écrit dans **tous** les cas, succès comme échec — c'est le seul canal de retour
vers le pipeline et l'agent. En cas d'échec, `Cd`/`Cl`/`Cl_Cd` valent `null` et
jamais `0.0` : un zéro se propagerait dans la boucle comme une mesure légitime.

`converged` résume les deux conditions qui rendent un point exploitable : un
maillage validé par checkMesh et des coefficients stabilisés (écart-type relatif
sur la fenêtre de moyenne sous `coeff_stability_tol`).

### Version d'OpenFOAM

Le template vise **`simpleFoam`** : OpenFOAM.com (ESI, v2212+) ou OpenFOAM.org
jusqu'à v10. Les versions OpenFOAM.org ≥ 11 ont remplacé `simpleFoam` par
`foamRun -solver incompressibleFluid` — `run_cfd.sh` le détecte et le dit.
Définir `FOAM_BASHRC` dans `.env`.

### Géométrie et unités

snappyHexMesh ne lit pas le STEP. Le driver Fusion exporte donc **`geometry.stl`
en plus du `geometry.step`** — gmsh n'est plus dans le chemin critique, il ne
sert que de secours si l'export STL a échoué.

L'unité d'écriture d'un STL n'est garantie par personne. Plutôt que de la
supposer, `case_builder.py` la **mesure** : il compare l'étendue du fichier à
la géométrie demandée et, si l'écart correspond à un facteur usuel (mm, cm),
remet le STL à l'échelle et le signale. Si l'écart ne s'explique par aucun
facteur d'unité, ce n'est pas un problème d'unité mais de géométrie, et
l'itération est refusée.

### Qualité de maillage

`run_cfd.sh` lance `checkMesh` **sans** `-allGeometry` : ce mode signale les
cellules concaves, que tout maillage snappyHexMesh avec couches limites
produit, et que le solveur encaisse sans difficulté — il ferait échouer
pratiquement toutes les itérations.

Le verdict utile vient de la comparaison aux seuils de `cfd_settings.yaml`
(`mesh.check_mesh`) : non-orthogonalité, skewness, aspect ratio. Un maillage
peut être « Mesh OK » pour OpenFOAM tout en étant trop dégradé pour qu'on
fasse confiance aux coefficients.

Ces seuils tiennent compte d'un fait mesuré : **snappyHexMesh n'est pas
déterministe en parallèle**. Trois maillages successifs de la même géométrie
ont donné 54,5 / 68,5 / 69,1 de non-orthogonalité maximale. Un seuil trop
serré ferait donc échouer une itération au hasard, et l'optimisation ne serait
plus reproductible.

### Résultat de référence

Chaîne validée de bout en bout sur OpenFOAM v2506, NACA 2412 à incidence nulle,
Re = 4 × 10⁵ :

| | |
|---|---|
| Cellules | 168 312 |
| checkMesh | non-ortho 54,5 · skewness 2,6 · aspect ratio 14,5 |
| **Cl** | **0,2275** (théorie NACA 2412 à α = 0° : ≈ 0,25) |
| **Cd** | **0,0170** |
| Cl/Cd | 13,4 |
| Stabilité | écart-type relatif 4 × 10⁻⁵ sur les 200 dernières itérations |

Le Cl est juste. Le **Cd est surestimé d'un facteur ≈ 2** par rapport aux
mesures en soufflerie : `kOmegaSST` suppose la couche limite turbulente dès le
bord d'attaque, ce qui est faux à Re = 4 × 10⁵ où une bonne partie de
l'extrados est encore laminaire. Pour de l'optimisation — qui compare des
formes entre elles — ce biais systématique est acceptable ; pour une valeur
absolue de traînée, il ne l'est pas.

---

## Tests

```bash
python3 -m pytest tests/ -v
```

---

## À fournir par l'utilisateur

- `fusion/seed_design.f3d` — le modèle Fusion 360 paramétrique de départ, dont
  les User Parameters portent exactement les noms listés dans
  `configs/design_params.yaml`.
- `ANTHROPIC_API_KEY` dans `.env` (nécessaire à partir de la Phase 4).
