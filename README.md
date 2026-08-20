# aero-opt-agent — Optimisation Aérodynamique Agentique

**v1.5 « Universal 2D » — optimisation automatique de forme aérodynamique :
n'importe quel profil 2D → CFD OpenFOAM → nouveaux paramètres, en boucle, sans
intervention.**

Vous donnez un profil — un fichier de coordonnées téléchargé, un modèle Fusion
paramétrique, ou quatre chiffres NACA — et un objectif. Le système le
re-paramétrise, construit la géométrie, la maille, lance le calcul, lit les
coefficients aérodynamiques, propose une forme meilleure, et recommence. À la
fin il vous rend un dossier avec la géométrie optimisée, les champs CFD, un
rapport illustré, et de quoi reprendre le design dans Fusion 360.

## Optimiser n'importe quel profil, en trois commandes

```bash
# 1. récupérer un profil — ici le Clark Y de la base UIUC
curl -o clarky.dat "http://airfoiltools.com/airfoil/seligdatfile?airfoil=clarky-il"

# 2. le re-paramétriser en coefficients CST optimisables
python3 -m profiles.reparameterize clarky.dat \
    --chord 300 --span 80 --aoa 3 -o configs/design_params.yaml

# 3. optimiser
python3 scripts/run_loop.py --max-iterations 20 \
    --cfd-settings configs/cfd_settings_fast.yaml
```

Le dossier `results/run_*/best_design/` apparaît tout seul à la fin, avec son
`report.html` et son `FUSION_RETURN.md`.

## Ce que la v1.5 ajoute à la v1.0

La v1.0 optimisait une famille de profils NACA à quatre chiffres — trois
paramètres de forme. La v1.5 accepte **une forme quelconque** et la décrit par
vingt-quatre coefficients CST, sans rien perdre de ce qui précède.

| | v1.0 | v1.5 |
|---|---|---|
| Entrée | 4 chiffres NACA | + fichiers `.dat` / `.csv` (Selig, Lednicer, CSV) |
| Description de la forme | épaisseur, cambrure | + 24 coefficients de Kulfan |
| Producteurs de géométrie | interne, Fusion | les deux, derrière une interface commune |
| Fidélité contrôlée | emprise du STL | + porte de reconstruction, aller-retour STL |
| Retour en CAO | section CSV | + `FUSION_RETURN.md` et script Fusion généré |

**La v1.0 n'a pas été modifiée** : elle vit sur sa propre branche, figée au tag
`v1.0-stable`.

## Résultats obtenus

**Clark Y ingéré depuis la base UIUC** — 121 points, re-paramétré en 24
coefficients CST, **25 itérations, 0 échec**, corde 300 mm, incidence 3° :

| | départ | optimisé | |
|---|---|---|---|
| Portance Cl | 0,7659 | **0,7784** | +1,6 % |
| Traînée Cd | 0,02725 | **0,02653** | −2,6 % |
| **Finesse Cl/Cd** | **28,11** | **29,34** | **+4,4 %** |

Le gain est modeste, et c'est le résultat honnête : le Clark Y est un profil
éprouvé depuis 1922, et à 3° d'incidence il travaille déjà près de son optimum
de finesse. La recherche l'a vérifié en chiffres — porter l'incidence à 4,68°
gagne 20 % de portance mais 24 % de traînée, donc perd. Ce que la v1.5 apporte
ici n'est pas un gain spectaculaire, c'est la capacité d'affiner une forme que
la v1.0 ne savait pas même décrire.

**[→ Rapport complet, profil Clark Y](docs/example_report_clarky/README.md)**

**NACA 2412 à incidence nulle** (v1.0, paramétrisation à quatre chiffres) —
**22 itérations en 27 minutes** :

| | seed | optimisé | |
|---|---|---|---|
| Portance Cl | 0,2274 | **0,7657** | +237 % |
| Traînée Cd | 0,01693 | **0,02563** | +51 % |
| **Finesse Cl/Cd** | **13,43** | **29,88** | **+122 %** |

Ici le point de départ était délibérément loin de l'optimum, d'où l'ampleur du
gain. L'incidence trouvée — 5,04° — est celle qu'on attend physiquement pour la
finesse maximale d'un profil cambré.

**[→ Rapport complet, profil NACA](docs/example_report/README.md)**
(sections avant/après, distributions de pression, lignes de courant)

---

## Installation

**Prérequis** : Linux ou WSL, Python 3.10+, et OpenFOAM. Fusion 360 est
facultatif — sans lui, le système calcule la géométrie lui-même.

```bash
git clone https://github.com/<votre-compte>/aero-opt-agent.git
cd aero-opt-agent

python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt

# OpenFOAM (ESI) — fournit simpleFoam, snappyHexMesh, checkMesh
curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash
sudo apt-get install openfoam2506-default

# facultatif : visuels CFD du rapport
sudo apt-get install paraview xvfb

cp .env.example .env        # puis renseigner FOAM_BASHRC
```

Vérifier que tout répond :

```bash
python3 -m pytest tests/ -q                       # 726 tests, ~30 s
python3 pipeline/utils.py configs/design_params.yaml --show-ranges
```

## Lancer une optimisation

```bash
python3 scripts/run_loop.py --max-iterations 20 \
    --cfd-settings configs/cfd_settings_fast.yaml
```

C'est tout. Comptez environ une minute par itération avec ce préréglage. La
boucle s'arrête d'elle-même sur stagnation, et **survit aux échecs** : une
itération dont le maillage casse est archivée, la stratégie resserre le pas, et
la suivante repart de la meilleure forme connue.

Ce qui s'affiche pendant qu'elle tourne :

```
[loop] iter   0 | Cd 0.03107 | Cl 0.25266 | Cl/Cd 8.13 | 84.8s  <- meilleur
[loop]      proposition [local] chord 300->321
[loop] iter   1 | Cd 0.03097 | Cl 0.24549 | Cl/Cd 7.93 | 78.6s
```

Pour changer ce qui est optimisé, éditez `configs/design_params.yaml` :
valeurs de départ, bornes, et objectif (`maximize_Cl_Cd`, `minimize_Cd` ou
`maximize_downforce`).

## Récupérer le résultat

**Le dossier est créé automatiquement en fin de série** :

```
results/run_AAAAMMJJ_HHMMSS/best_design/
├── report.html            ← ouvrez celui-ci
├── README.md              le même rapport, en Markdown
├── FUSION_RETURN.md       comment reprendre le design en CAO
├── rebuild_in_fusion.py   script Fusion qui retrace le profil
├── geometry.stl           la géométrie optimisée, en mètres
├── profile_section.csv    la section telle que simulée
├── profile_chord.dat      le profil redressé, pour XFOIL / XFLR5
├── design_params.yaml     les paramètres exacts, rejouables
├── results.json           les coefficients
├── figures/               courbes et images CFD
├── comparison/            le seed, pour la comparaison avant/après
└── cfd/                   le case OpenFOAM complet (ParaView)
```

Le rapport contient les paramètres de départ face aux paramètres finaux,
l'évolution des coefficients itération par itération, les sections avant/après,
les distributions de pression, les champs CFD, et une lecture physique de ce
qui a changé.

Sur une série déjà exécutée :

```bash
python3 scripts/run_loop.py --report        # la trajectoire, en console
python3 scripts/run_loop.py --export-best   # (re)générer le dossier
```

---

Deux spécifications font loi : celle de la v1.0,
[`MASTER_DOCUMENTATION_AGENTIC_AERO_OPTIMIZATION.md`](MASTER_DOCUMENTATION_AGENTIC_AERO_OPTIMIZATION.md),
et celle de la v1.5,
[`MASTER_DOCUMENTATION_2D_GENERALIZATION.md`](MASTER_DOCUMENTATION_2D_GENERALIZATION.md).
En cas de
divergence, ce sont elles qui gagnent. Les écarts assumés sont listés en fin de
document.

---

## Ce que fait une itération

```
design_params.yaml
        │
        ▼
  ① validation du contrat        pipeline/utils.py
        │
        ▼
  ② géométrie                    fusion/parametric_driver.py
        │                        (Fusion 360, ou producteur interne)
        ▼
  ③ contrôle de la géométrie     pipeline/geometry_validator.py
        │
        ▼
  ④ maillage + CFD               openfoam/run_cfd.sh
        │                        blockMesh → snappyHexMesh → checkMesh → simpleFoam
        ▼
  ⑤ coefficients                 openfoam/postprocess.py → results.json
        │
        ▼
  ⑥ archivage                    data/iterations/iter_XXXX/
        │
        ▼
  ⑦ proposition                  agent/orchestrator.py → design_params.yaml
```

L'orchestrateur n'écrit **que** `configs/design_params.yaml`. C'est la règle
d'or du Master Document, et la validation la fait respecter : une proposition
qui desserre une borne, change une unité ou ajoute un paramètre est rejetée
avant d'atteindre le disque.

---

## Structure

```
configs/
  design_params.yaml          ← SEUL fichier modifié par l'agent
  design_params_clarky.yaml      exemple issu d'un profil réel (24 coeff. CST)
  cfd_settings.yaml              conditions CFD (réglage fin)
  cfd_settings_fast.yaml         préréglage d'exploration, ~60 s/itération
  cfd_settings_demo.yaml         le rapide, mais qui garde les cases (visuels)
examples/profiles/               profils d'exemple (NACA, Clark Y, E387, S1223)
profiles/
  loader.py                      lecture Selig / Lednicer / CSV
  profile.py                     profil normalisé + mesures géométriques
  validation.py                  contrôles de validité
  cst.py                         paramétrisation de Kulfan + ajustement
  reparameterize.py              fichier → coefficients → design_params.yaml
  geometry.py                    coefficients → contour + contrôle de forme
  roundtrip.py                   STL relu → écart au profil d'origine
geometry/
  base.py                        interface GeometryBackend + registre
  internal_backend.py            producteur interne (toujours disponible)
  fusion_backend.py              producteur Fusion 360
  common.py                      normalisation des comptes rendus
fusion/
  seed_design.f3d                modèle Fusion (non versionné)
  parametric_driver.py           géométrie : Fusion ou production interne
openfoam/
  templates/external_aero/       case OpenFOAM paramétré
  case_builder.py                YAML → case dimensionné
  run_cfd.sh                     orchestration du calcul
  postprocess.py                 → results.json
pipeline/
  utils.py                       chargement + validation du contrat
  geometry_validator.py          contrôle de la géométrie exportée
  master_pipeline.py             point d'entrée d'une itération
agent/
  prompts/system_prompt.md       instructions de l'agent
  orchestrator.py                proposition des paramètres
scripts/
  run_loop.py                    boucle d'optimisation
data/iterations/                 archives (non versionné)
tests/                           726 tests
```

---

## Le contrat `design_params.yaml`

Trois familles de règles, appliquées par `pipeline/utils.py` :

1. **Structure** — clés obligatoires, types stricts, aucune clé inconnue.
2. **Bornes** — `min < max` et `min ≤ value ≤ max`.
3. **max_delta_pct** — l'écart avec la dernière itération **réussie** ne dépasse
   pas ce pourcentage.

   Deux cas imposent de mesurer ce budget sur l'amplitude `max - min` plutôt
   que sur la valeur : quand la valeur précédente est **nulle** (le pourcentage
   relatif est indéfini, et le paramètre serait figé à jamais), et quand les
   **bornes encadrent zéro**. Ce second cas mérite une explication, parce qu'il
   a piégé une optimisation réelle : pour une incidence bornée à [-2°, 12°],
   passer de 0 à -1,68° coûte une itération, mais revenir en coûte **huit**,
   chaque pas étant limité à 12 % de la valeur courante. La recherche explorait
   une direction et ne pouvait plus en sortir. Un pourcentage d'une grandeur qui
   change de signe ne contraint rien de sensé ; rapporté à l'amplitude, le
   budget redevient symétrique — ce qu'une règle de sécurité doit être.

Entre deux itérations, seule `value` peut changer.

### Deux paramétrisations dans le même contrat

Le champ facultatif `parameterization` vaut `naca` (défaut, v1.0) ou `cst`
(v1.5). Il ne fait pas foi à lui seul : la paramétrisation est **reconnue aux
paramètres présents**, et confrontée à celle qui est déclarée. Un fichier
écrit à la main dont l'en-tête ment sur son contenu est refusé sur-le-champ,
plutôt que de produire une forme silencieusement fausse trois étapes plus loin.

| | `naca` | `cst` |
|---|---|---|
| Paramètres de forme | `thickness`, `camber` | `cst_upper_0…N`, `cst_lower_0…N` |
| Paramètres physiques | `chord`, `span`, `aoa` | identiques |
| Épaisseur et cambrure | données en entrée | **mesurées** sur la forme reconstruite |

Le bloc facultatif `provenance` porte ce qui décrit la forme sans être une
variable d'optimisation : le fichier d'origine, l'ordre CST, l'incidence
retirée à l'ingestion, et les **ordonnées de bord de fuite**. Ces dernières y
vivent parce que le contrat exige `min < max` — une grandeur figée n'a pas sa
place dans `parameters` — et sans elles un profil à bord de fuite ouvert serait
reconstruit fermé.

```bash
python3 pipeline/utils.py configs/design_params.yaml --show-ranges
```

```
chord:     [279, 321] mm          thickness: [0.1104, 0.1296]
camber:    [0.018, 0.022]         span:      [79.2, 80.8] mm
aoa:       [-1.68, 1.68] deg
```

### Archivage et place disque

`execution.keep_case_after_run` décide de ce qui survit à l'itération :
`true` garde tout, `"dicts"` supprime maillage et champs en conservant les
dictionnaires et les journaux, `false` supprime le case entier. Un case complet
pèse une vingtaine de méga-octets — sur cinquante itérations sans surveillance,
plusieurs gigaoctets — alors qu'il se régénère en quelques secondes depuis le
STL et les dictionnaires. Le préréglage rapide utilise `"dicts"`.

`span` est **tenu fixe à dessein** : le calcul est quasi-2D, l'envergure n'y a
aucun effet sur Cd et Cl. La laisser varier ferait dépenser des itérations pour
un gain nul. Pour la rendre influente, passer `domain.spanwise_treatment` à
`full_3d` puis rouvrir ses bornes.

---

## Ingérer un profil existant

```bash
python3 profiles/loader.py mon_profil.dat            # lire et mesurer
python3 profiles/validation.py mon_profil.dat        # + contrôles de validité
python3 profiles/loader.py mon_profil.dat --json     # sortie exploitable
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

### Trois conventions, aucune déclarée

Les fichiers de profils circulent depuis quarante ans dans trois formats
incompatibles, qu'aucun en-tête n'identifie :

| Format | Ce à quoi il ressemble |
|---|---|
| **Selig** | un contour continu : bord de fuite → extrados → nez → intrados → bord de fuite. Le plus répandu (UIUC, XFOIL). |
| **Lednicer** | un en-tête de deux nombres — les comptes de points —, puis chaque surface du nez vers la queue. |
| **CSV** | colonnes `x, y`, éventuellement précédées d'une colonne de surface. C'est ce que ce projet exporte. |

Selig et Lednicer ont exactement la même allure : deux colonnes de nombres.
Seule la façon dont l'abscisse évolue les sépare — elle décroît puis remonte
dans l'un, monte deux fois dans l'autre. La reconnaissance porte donc sur la
forme de la séquence, jamais sur l'extension du fichier.

### Ce que l'ingestion normalise

Le profil sort avec le nez à l'origine, le bord de fuite à (1, 0), et la corde
unitaire. Deux choses sont retirées et **signalées** :

- **l'échelle** — un fichier en millimètres est ramené à une corde unitaire, la
  corde d'origine étant conservée ;
- **l'incidence** — beaucoup de fichiers ont quelques degrés figés dans leurs
  coordonnées. Or l'incidence est ici un paramètre de conception : la laisser
  dans la géométrie la compterait deux fois.

La transformation appliquée est conservée : `profile.transform.restore(point)`
ramène n'importe quel point dans le repère du fichier d'origine.

### Ce que la validation refuse — et ce qu'elle laisse passer

Rédhibitoire : contour ouvert, surfaces qui se croisent, surface repliée sur
elle-même, contour auto-intersectant, épaisseur hors de [1 %, 40 %].

Simplement signalé : bord de fuite épais, forte cambrure, nez très aigu,
faible densité de points. **Ce sont des choix de conception, pas des erreurs** —
refuser tout ce qui sort de l'ordinaire interdirait la moitié des profils
réels.

Rien ne lève : le chargement comme la validation rendent un compte rendu, à
l'image de `GeometryBackend.generate`. Un fichier douteux ne doit pas
interrompre une boucle.

---

## Re-paramétrisation CST

Un fichier de points n'est pas optimisable : deux cents coordonnées libres
donnent deux cents variables, et rien n'empêche la forme de devenir une scie.
Il faut d'abord la décrire par un petit nombre de nombres qui gardent un sens.

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

### La méthode

Chaque surface s'écrit `ζ(ψ) = C(ψ)·S(ψ) + ψ·Δζ_bf`, où `C(ψ) = √ψ·(1−ψ)` est
la **fonction de classe** et `S` une somme de polynômes de Bernstein. Trois
propriétés en découlent, et ce sont elles qui rendent l'optimisation sûre :

- **la forme est lisse par construction** — une somme de Bernstein ne peut pas
  onduler entre les points, là où une spline libre le fait au premier pas de
  trop ;
- **la physique est dans la formulation** — l'exposant `0.5` impose un nez en
  racine carrée, l'exposant `1` un bord de fuite pointu. Ces comportements ne
  peuvent pas être perdus en cours d'optimisation ;
- **l'ajustement est linéaire** — pas de point de départ, pas de tirage
  aléatoire. Deux ajustements sur les mêmes points donnent le même résultat au
  bit près.

### La porte de reconstruction

Le fichier n'est accepté que si la forme reconstruite reste à moins de
**5 × 10⁻⁴ de corde** du fichier d'origine (écart maximal) et **10⁻⁴** en
moyenne. Sans cette porte, une optimisation peut se dérouler parfaitement
pendant des heures sur une forme qui n'est pas celle qu'on a fournie — et rien
dans les résultats ne le signalerait, puisque toute la chaîne en aval
fonctionne.

L'écart est une **distance géométrique**, pas un écart vertical. Au bord
d'attaque la pente de la surface dépasse 6 : un écart vertical y vaut plusieurs
fois la distance réelle, et ferait refuser un ajustement parfaitement bon.

Un refus dit quoi faire :

```
reconstruction refusée : écart maximal de 1.09e-03 corde à 4.0% sur l'extrados
[...] — 12 points sur 122 dépassent le seuil
— l'ordre 11 franchirait la porte : relancer avec --order 11
```

### Choisir l'ordre

L'ordre par défaut est **11**, soit 24 coefficients. Il a été retenu par
validation croisée sur des profils réels de la base UIUC — ajustement sur un
point sur deux, mesure sur les points retenus. L'erreur hors échantillon suit
celle d'ajustement jusqu'à environ cinq points par coefficient, puis décroche :
sur l'E387, l'ordre 13 affiche 8,6 × 10⁻⁴ sur ses propres points et
1,12 × 10⁻³ sur ceux qu'il n'a pas vus. Il épouse alors le bruit du fichier.

C'est aussi pourquoi un fichier grossier ne se « répare » pas en montant
l'ordre : les 61 points de l'E387 sont refusés à tous les ordres raisonnables,
et le message conseille un fichier plus dense plutôt qu'un surajustement.

### Des bornes qui ont un sens géométrique

Sur un profil réel, les coefficients ne sont pas du même ordre : le Clark Y en
a qui valent 0,13 et d'autres 3,27, la forme tenant par compensation entre
grands termes. Des bornes proportionnelles — « ±50 % de sa valeur » —
donneraient au second une marge de ±1,63, soit **65 % de corde de
déplacement** : la première sonde de l'optimiseur détruirait le profil.

L'effet géométrique d'une variation `δ` du coefficient `i` vaut exactement
`max_ψ [C(ψ)·Bᵢ(ψ)] · δ`. La relation est donc inversée : chaque coefficient
reçoit la marge qui lui donne la **même autorité géométrique** que les autres —
1,5 % de corde, et 0,6 % pour les deux coefficients d'extrémité, qui tiennent
le rayon de nez et l'angle de bord de fuite.

Vérifié : sur quarante-huit bornes poussées à l'extrême, aucune ne produit un
profil invalide.

### Aller-retour

La porte juge l'ajustement. Elle ne dit rien de ce qui est **écrit sur le
disque** : entre les coefficients et le STL se glissent une mise à l'échelle,
une conversion d'unités, une rotation d'incidence et une triangulation.

```bash
python3 -m profiles.roundtrip results/run_*/best_design/geometry.stl \
    clarky.dat --chord 300 --aoa 3
```

L'outil relit le fichier, en extrait la section et la mesure contre le profil
d'origine — sans faire confiance à ce qui a servi à l'écrire. C'est le seul
contrôle qui attraperait une confusion d'unités.

---

## Géométrie

### L'interface `GeometryBackend`

Tout ce qui est en aval — CFD, optimiseur, rapport — ne parle qu'à une seule
interface. Quel producteur travaille derrière est un choix de configuration.

```python
from geometry import get_backend

backend = get_backend("auto")            # ou "internal", ou "fusion"
result  = backend.generate(design_params, output_dir)

result.success              # bool
result.stl_path             # Path | None
result.step_path            # Path | None — un modèle CAO, s'il y en a un
result.profile_coordinates  # contour fermé, en mètres
result.message              # ce qui s'est passé, en clair
```

`generate` **ne lève jamais** : un échec attendu est un résultat, pas une
exception. C'est ce qui permet à la boucle d'archiver l'itération ratée, d'en
tirer une conséquence, et de continuer.

### Les deux producteurs livrés

| Producteur | Ce qu'il fait | Quand |
|------------|---------------|-------|
| `fusion` | Met à jour les User Parameters, reconstruit la géométrie, exporte STEP + STL | Le script tourne **dans** Fusion 360 |
| `internal` | Calcule le profil et écrit le STL en mètres, sans Fusion | Partout ailleurs — c'est ce qui rend la boucle autonome |

`auto` (défaut) interroge chaque backend sur sa disponibilité et retient le
premier utilisable, Fusion d'abord. **L'API Fusion n'a pas de mode headless** :
sans le producteur interne, chaque itération attendrait qu'un humain clique sur
*Run*. Les deux chemins partagent la même fonction de profil, donc la même
forme ; le mode interne ne produit simplement pas de STEP, faute de noyau CAO.

Demander la disponibilité **avant** de lancer évite de découvrir l'absence de
Fusion après cinq minutes de maillage.

```bash
python3 pipeline/master_pipeline.py --geometry-backend internal
python3 scripts/run_loop.py         --geometry-backend fusion
```

```python
import geometry
geometry.describe_backends()   # nom, disponibilité, description
geometry.resolve("auto")       # ce que « auto » retient ici
```

### Ajouter un producteur

Trois gestes, et aucune ligne du pipeline à modifier :

```python
from geometry import GeometryBackend, GeometryResult, register_backend

@register_backend
class MonBackend(GeometryBackend):
    name = "mon_backend"

    @classmethod
    def available(cls) -> bool:
        return True                       # les outils nécessaires sont là ?

    def generate(self, design_params, output_dir, **options):
        ...
        return GeometryResult(success=True, stl_path=..., message="...")
```

Il devient aussitôt sélectionnable par `--geometry-backend mon_backend`, et
apparaît dans l'aide des commandes : les choix sont lus dans le registre.

### Deux stratégies, dans Fusion

`rebuild` (défaut) met à jour les User Parameters **puis reconstruit** la
géométrie. `parameters` se contente du recalcul, et n'a de sens que si le modèle
est réellement piloté par ses cotes.

Le seed livré exige `rebuild` : son générateur crée bien les 5 User Parameters,
mais trace son profil par une spline passant par des points calculés en dur et
l'extrude sur une longueur brute. **Modifier ses paramètres n'y déplace pas un
point** — sans `rebuild`, chaque itération exporterait une géométrie identique
et l'agent optimiserait dans le vide, sans la moindre erreur pour le signaler.

### Lancer le driver dans Fusion

1. Ouvrir le modèle (ou déposer le seed en `fusion/seed_design.f3d`).
2. *Utilities → ADD-INS → Scripts and Add-Ins → Scripts → (+)*, pointer
   `fusion/parametric_driver.py`, puis **Run**.

Hors Fusion, `--dry-run` valide la configuration et calcule la géométrie prévue
sans rien écrire.

### Unités

L'unité d'écriture d'un STL n'est garantie par personne. Plutôt que de la
supposer, `case_builder.py` la **mesure** : il compare l'étendue du fichier à la
géométrie demandée et, si l'écart correspond à un facteur usuel, remet le STL à
l'échelle en le signalant. Si l'écart ne s'explique par aucun facteur d'unité,
ce n'est pas un problème d'unité mais de géométrie, et l'itération est refusée.

---

## CFD

```bash
openfoam/run_cfd.sh --iteration-dir data/iterations/iter_0000
openfoam/run_cfd.sh --iteration-dir ... --dry-run     # construit le case seul
openfoam/run_cfd.sh --iteration-dir ... --mesh-only   # s'arrête après checkMesh
```

### Le case est dimensionné, pas recopié

`case_builder.py` déduit de `design_params.yaml` : la taille du domaine et des
mailles (en multiples de corde), `k` et `omega` (depuis l'intensité turbulente),
le point `locationInMesh`, et surtout **`Aref` et `lRef`, recalculés à chaque
itération**. Figer la surface de référence pendant que la corde varie ferait
bouger les Cd et Cl alors que seule la normalisation aurait changé — l'agent
optimiserait un artefact de calcul.

### Repère

```
+X = corde, du bord d'attaque vers le bord de fuite
+Y = épaisseur, et portance
+Z = envergure
```

L'incidence est portée par la **géométrie** — le profil est tourné à la
construction — et non par la direction de l'écoulement. Celui-ci reste aligné
sur +X d'une itération à l'autre, donc les directions de portance et de traînée
ne bougent jamais.

### Qualité de maillage

`checkMesh` est lancé **sans** `-allGeometry` : ce mode signale les cellules
concaves, que tout maillage snappyHexMesh avec couches limites produit et que le
solveur encaisse sans difficulté — il ferait échouer presque toutes les
itérations. Le verdict utile vient de la comparaison aux seuils de
`cfd_settings.yaml` : non-orthogonalité, skewness, aspect ratio.

Ces seuils tiennent compte d'un fait mesuré : **snappyHexMesh n'est pas
déterministe en parallèle**. Trois maillages successifs de la même géométrie ont
donné 54,5 / 68,5 / 69,1 de non-orthogonalité maximale. Un seuil trop serré
ferait échouer une itération au hasard, et l'optimisation ne serait plus
reproductible — d'où 75.

### `results.json`

Écrit dans **tous** les cas, succès comme échec : c'est le seul canal de retour
vers le pipeline et l'agent. En cas d'échec, `Cd`/`Cl`/`Cl_Cd` valent `null` et
jamais `0.0` — un zéro se propagerait dans la boucle comme une mesure légitime.

`converged` résume les deux conditions qui rendent un point exploitable : un
maillage validé et des coefficients stabilisés.

### Deux préréglages

| | `cfd_settings.yaml` | `cfd_settings_fast.yaml` |
|---|---|---|
| Durée | ~15 min | ~60 s |
| Maillage | 8 mailles/corde, niveaux 3-4, couches limites | 6 mailles/corde, niveaux 2-3, sans couches |
| Itérations | 2000 | 500 |
| Usage | qualifier une forme | explorer |

Une optimisation ne compare pas des valeurs à la réalité, elle **classe des
formes**. Un biais systématique ne change pas ce classement, donc le préréglage
rapide convient à l'exploration. Revalider le meilleur design avec le réglage
fin avant d'en tirer un chiffre.

### Résultat de référence

OpenFOAM v2506, NACA 2412 à incidence nulle, Re = 4 × 10⁵, réglage fin :

| | |
|---|---|
| Cellules | 168 312 |
| checkMesh | non-ortho 54,5 · skewness 2,6 · aspect ratio 14,5 |
| **Cl** | **0,2275** — théorie NACA 2412 à α = 0° : ≈ 0,25 |
| **Cd** | **0,0170** |
| Stabilité | écart-type relatif 4 × 10⁻⁵ sur 200 itérations |

Le Cl est juste. Le **Cd est surestimé d'un facteur ≈ 2** : `kOmegaSST` suppose
la couche limite turbulente dès le bord d'attaque, ce qui est faux à
Re = 4 × 10⁵ où une bonne partie de l'extrados reste laminaire. Acceptable pour
comparer des formes, pas pour annoncer une traînée absolue.

### Optimisation de référence

22 itérations en 27 minutes, préréglage rapide, stratégie locale, sans
intervention :

| | seed | meilleur (itération 21) |
|---|---|---|
| chord | 300 mm | 343,5 mm |
| thickness | 0,120 | 0,113 |
| camber | 0,020 | 0,020 |
| aoa | 0° | 5,04° |
| **Cl/Cd** | **8,13** | **20,45** — **+151 %** |

21 itérations réussies, une écartée par `checkMesh` (skewness 4,03) dont la
stratégie s'est remise en resserrant le pas.

Le meilleur design **requalifié au réglage fin** donne Cd 0,02563, Cl 0,76572,
**Cl/Cd 29,88** contre 13,41 pour le seed : **+123 %**. Le préréglage rapide
annonçait +151 %, le réglage fin en confirme +123 % — même direction, même
ordre de grandeur. C'est la vérification qui compte : le classement des formes
établi en exploration tient au réglage précis.

L'incidence trouvée, 5°, est celle qu'on attend physiquement pour la finesse
maximale d'un profil cambré. Le seed, à 0°, était sur le flanc de la courbe.

---

## L'agent

```bash
python3 agent/orchestrator.py --dry-run --explain
```

### Deux stratégies

`llm` — Claude lit l'historique et raisonne sur la forme (`ANTHROPIC_API_KEY`
requise). Une proposition refusée par la validation lui est renvoyée **avec le
message d'erreur**, qui nomme le paramètre fautif et donne l'intervalle
admissible ; trois tentatives, puis abandon.

`local` — recherche par motif déterministe, sans clé ni réseau.

`auto` (défaut) interroge l'agent et retombe sur la recherche locale s'il est
indisponible. Ce repli n'est pas un pis-aller : sans lui, une clé absente ou une
coupure réseau arrêterait une optimisation de plusieurs heures.

### Ce que fait la recherche locale

Depuis le meilleur point connu, elle sonde un paramètre dans une direction. Si
ça paye, elle **poursuit dans la même direction** — une recherche linéaire, sans
quoi un paramètre n'avancerait que d'un pas toutes les 2n itérations. Sinon elle
essaie l'autre sens, puis le paramètre suivant ; quand tout a été sondé sans
gain, le pas est divisé par deux.

Deux raffinements qui viennent de l'observation :

- **On ne se téléporte pas au meilleur point.** Le budget `max_delta_pct` se
  mesure depuis la dernière itération *réussie*, pas depuis la meilleure. Quand
  les deux diffèrent, la recherche s'en rapproche autant que le contrat
  l'autorise.
- **Un paramètre sans effet mesuré est abandonné.** Deux essais qui ne changent
  rien à l'objectif suffisent : chaque évaluation coûte plusieurs minutes.

Sur une optimisation réelle de 22 itérations, elle a fait passer la finesse de
8,13 à 20,45 (voir plus haut). Elle a aussi ses limites, qu'il vaut mieux
connaître : c'est une descente gloutonne, coordonnée par coordonnée. Elle
n'exploite pas les **couplages** — la cambrure optimale dépend de l'incidence —
et ne peut que les rencontrer par rotation. C'est précisément là qu'un agent
qui connaît l'aérodynamique fait mieux, et c'est pourquoi la stratégie `llm`
reste la voie principale.

---

## La boucle

```bash
python3 scripts/run_loop.py --max-iterations 20 --strategy auto \
    --cfd-settings configs/cfd_settings_fast.yaml
```

Elle est faite pour tourner sans surveillance :

- **elle ne s'arrête pas sur un échec** — l'itération ratée est archivée, la
  stratégie resserre le pas, la suivante repart de la meilleure forme connue.
  Seuls des échecs consécutifs, signe d'un problème de fond, l'interrompent ;
- **elle est reprenable** — tout l'état vit dans `data/iterations/` ;
- **elle s'arrête d'elle-même** sur stagnation, plutôt que de brûler le budget
  sur du bruit numérique ;
- **Ctrl-C** termine l'itération en cours puis sort proprement — couper au
  milieu d'un run OpenFOAM laisserait une archive que la reprise lirait mal.

```bash
python3 scripts/run_loop.py --report     # lire une série déjà exécutée
python3 scripts/run_loop.py --resume     # reprendre sans écraser d'archive
```

Bilan écrit dans `data/iterations/optimization_summary.json` ; `--report`
affiche la trajectoire complète — ce qui a bougé, ce que ça a donné, où ça a
échoué — et les paramètres du meilleur design.

---

## Le dossier livrable — options

Le contenu du dossier et la façon de le récupérer sont décrits
[plus haut](#récupérer-le-résultat) ; cette section traite du réglage fin de
l'export.

```bash
python3 scripts/export_best.py --iterations-dir data/iterations
python3 scripts/run_loop.py --export-best         # sur une série déjà faite
python3 scripts/run_loop.py --no-export           # désactiver
python3 scripts/run_loop.py --no-visuals          # sans ParaView
python3 scripts/export_best.py --no-case          # sans le maillage (léger)
```

L'export ne peut pas faire échouer une optimisation réussie : en cas de
problème, les résultats restent archivés dans `data/iterations/` et la commande
se relance à la main.

### Avant / après

Le rapport oppose systématiquement le seed au design retenu : sections côte à
côte **à la même échelle** puis superposées, barres de performance par
grandeur, distributions de Cp confondues, et champs de pression et lignes de
courant juxtaposés **avec la même échelle de couleurs**.

Une seule règle gouverne cette section : **les deux côtés doivent être mesurés
dans le même régime CFD**. Comparer un seed d'exploration à un design
requalifié au réglage fin gonflerait le gain sans qu'il soit réel — et cela
peut aller jusqu'à inverser une conclusion : sur le design de référence, la
traînée paraissait *baisser* de 18 % en mélangeant les régimes, alors qu'à
régime constant elle **augmente de 51 %**. La finesse gagne quand même 122 %,
parce que la portance, elle, triple.

```bash
# la référence par défaut est la première itération de la série ; si le
# meilleur design a été requalifié, fournir un seed mesuré dans le même régime
python3 scripts/export_best.py --iterations-dir data/iterations \
    --qualified-dir data/qualify/iter_0000 \
    --baseline-dir data/baseline/iter_0000
```

Sans `--baseline-dir`, la comparaison retombe sur les chiffres d'exploration
des **deux** côtés et le dit dans le rapport. Le régime employé est toujours
écrit noir sur blanc.

Le dossier contient :

| | |
|---|---|
| `README.md` | le rapport complet |
| `report.html` | le même, **autonome** — SVG intégrés, images en base64 |
| `geometry.stl` | la géométrie, en mètres |
| `geometry.step` | si la CAO en a produit un |
| `profile_section.csv` / `.dat` | la section telle que simulée, incidence comprise |
| `profile_chord.dat` | le profil **redressé**, corde unitaire — pour XFOIL / XFLR5 |
| `design_params.yaml` | les paramètres exacts, rejouables |
| `results.json` | les coefficients |
| `FUSION_RETURN.md` | comment reprendre ce design en CAO |
| `rebuild_in_fusion.py` | script Fusion qui retrace le profil et l'extrude |
| `figures/` | courbes SVG et images CFD |
| `cfd/` | le case OpenFOAM, avec `best_design.foam` pour ParaView |
| `logs/` | les journaux de chaque étape |

Le rapport donne les paramètres de départ face aux paramètres finaux,
l'évolution de Cd, Cl et Cl/Cd itération par itération, la distribution de
pression sur le profil, et une **lecture physique** de ce qui a changé —
déduite des écarts mesurés, jamais d'un texte générique : un paramètre qui n'a
pas bougé n'est pas commenté.

Les visuels CFD — champ de Cp, lignes de courant, module de la vitesse — sont
rendus par ParaView en lot (`scripts/paraview_render.py`, `pvbatch` sous
`xvfb-run` pour se passer d'affichage). Le script est copié dans le dossier :
il reste rejouable sans le reste du système. S'il manque ParaView, le rapport
sort quand même, avec ses courbes et la raison de l'absence des images.

L'export ne peut pas faire échouer une optimisation réussie : en cas de
problème, les résultats restent archivés et la commande se relance à la main.

---

## Reprendre le design dans Fusion 360

Une optimisation qui ne rend qu'un STL est un cul-de-sac de conception. Un STL
est un solide facetté de plusieurs centaines de faces planes : on peut
l'imprimer, on ne peut ni y poser un congé propre, ni en changer une cote.

Chaque export écrit donc un **`FUSION_RETURN.md`** qui détaille trois voies, et
le `report.html` en porte une section dédiée.

| voie | ce qu'on obtient | quand la choisir |
|---|---|---|
| **Rejouer les paramètres** | modèle natif, historique CAO complet | dès qu'un modèle de départ existe |
| **Script `rebuild_in_fusion.py`** | esquisse + extrusion, sans intervention | sans modèle de départ |
| **Importer `profile_section.csv`** | esquisse tracée à la main | pour garder la main, ou une autre CAO |

```bash
# voie 1 — le driver reconstruit la forme et exporte STEP et STL
cp results/run_*/best_design/design_params.yaml configs/design_params.yaml
# puis, dans Fusion : Utilities → ADD-INS → Scripts → fusion/parametric_driver.py

# voie 2 — script autonome, coordonnées incluses
# Utilities → ADD-INS → Scripts → + → rebuild_in_fusion.py → Run
```

Le driver accepte les deux paramétrisations : sur un fichier `cst`, il
reconstruit la forme depuis les coefficients de Kulfan. Son tracé ne manipule
que des points, la voie Fusion n'a donc besoin d'aucun code particulier.

Le script généré trace **une spline par surface** plutôt qu'une seule sur tout
le contour : au bord d'attaque la courbe rebrousse, et une spline unique y
placerait un point d'inflexion au lieu d'un nez — soit exactement la zone qui
décide du décrochage.

Deux pièges que le document nomme explicitement :

- **l'incidence est déjà dans les coordonnées** de `profile_section` ; un
  montage aval qui l'applique lui-même la compterait deux fois. C'est à cela
  que sert `profile_chord.dat` ;
- **ne pas convertir le STL en solide**, pour la raison dite plus haut.

La reprise est vérifiable, pas seulement décrite :

```bash
python3 -m profiles.roundtrip export_fusion.stl profile_section.dat --chord 300
```

---

## Archivage

Chaque itération, **réussie ou non**, laisse dans `data/iterations/iter_XXXX/` :

```
design_params.yaml     la configuration EXACTE ayant produit ce résultat
geometry.stl / .step   la géométrie
results.json           les coefficients, ou la cause de l'échec
iteration.json         le compte rendu du pipeline
fusion_status.json     le compte rendu du driver
logs/                  un journal par étape
cfd/                   le case OpenFOAM complet
```

La copie de la configuration est indispensable : le fichier de travail aura
déjà été réécrit par l'agent à l'itération suivante, et un résultat archivé sans
ses paramètres n'est rattachable à rien.

---

## Tests

```bash
python3 -m pytest tests/ -q          # 726 tests, ~30 s
```

Ce qui est couvert sans dépendance externe : validation du contrat, unités et
expressions Fusion, lecture des STL, dimensionnement du case, rendu des
templates, lecture des coefficients dans les deux conventions OpenFOAM,
détection d'une géométrie inchangée, enchaînement du pipeline, convergence de la
recherche sur un modèle analytique, et lecture des réponses de l'agent.

Le driver Fusion est exécuté contre une **émulation de l'API `adsk`**
(`tests/fake_adsk.py`) : `run()`, la reconstruction, la purge et les exports
tournent réellement, sur un document qui reproduit celui du premier run réel.

> **Limite assumée.** Un faux valide la logique du driver, pas la lecture de
> l'API Fusion. Un bug de la Phase 1 — `evaluateExpression` rend des unités
> internes — est passé au travers de 206 tests parce que la doublure encodait la
> même erreur de compréhension que le code. Seul un run dans Fusion tranche ce
> genre de question.

---

## Écarts assumés au Master Document

| Écart | Pourquoi |
|---|---|
| `openfoam/case_builder.py`, fichier hors structure | Le case se **déduit** des paramètres (domaine, mailles, k, omega, Aref). Faire ce calcul en bash sur du YAML aurait été la partie la plus fragile du système. |
| Producteur de géométrie interne | L'API Fusion n'a pas de mode headless. Sans lui, aucune boucle autonome n'est possible. Fusion reste la référence dès qu'il est disponible. |
| Stratégie locale en repli de l'agent | Une optimisation de plusieurs heures ne peut pas dépendre d'une clé d'API ou du réseau. |
| `configs/cfd_settings_fast.yaml` | Le réglage fin coûte 15 min par itération, soit 5 h pour 20 itérations. |
| Mode `rebuild` par défaut | Le seed livré n'est pas réellement piloté par ses paramètres (voir plus haut). |
| Coordonnées en listes de tuples, pas en `np.ndarray` | Le driver tourne dans l'interpréteur embarqué de Fusion, où numpy n'est pas garanti. Aucune opération n'en tire profit ici. |
| Modes 3 et 4 du §3 (STEP/DXF, design Fusion existant) non implémentés | Ils demandent un noyau CAO ou une session Fusion pilotable. Les modes 1 et 2 couvrent l'usage visé, et l'interface `GeometryBackend` laisse la place. |
| Ordonnées de bord de fuite dans `provenance`, pas dans `parameters` | Le contrat exige `min < max` : une grandeur qui décrit la forme sans être optimisable n'a pas sa place parmi les variables. |
| Tolérance sur un défaut de skewness rare et contenu | Un bord de fuite épaissi produit quelques faces gauchies qu'aucun préréglage ne résout. Trois faces sur 258 814 ne rendent pas un maillage inexploitable ; le défaut est rapporté, pas tu. |

---

## Ce qui n'est pas dans le dépôt

- **`fusion/seed_design.f3d`** — le modèle Fusion. Binaire propre à chaque
  projet, donc non versionné. Sans lui, le système calcule la géométrie
  lui-même : rien ne bloque.
- **`.env`** — vos chemins et votre clé d'API. Partez de `.env.example`.
- **`ANTHROPIC_API_KEY`** — pour la stratégie LLM. Sans elle, la boucle tourne
  en recherche locale, qui ne demande ni clé ni réseau.
- **`data/iterations/` et `results/`** — des sorties, régénérables. Un exemple
  de rendu est conservé dans [`docs/example_report/`](docs/example_report/).

## Adapter à votre géométrie

Trois voies, de la plus simple à la plus intrusive :

1. **Partir d'un fichier de points** — c'est ce que la v1.5 a ajouté, et cela
   couvre tout profil publié ou dessiné ailleurs. Voir
   [Re-paramétrisation CST](#re-paramétrisation-cst).
2. **Passer par Fusion** — modélisez ce que vous voulez, exposez des User
   Parameters, et lancez le driver depuis Fusion en mode `parameters`. Le
   système ne fait alors que piloter vos cotes.
3. **Ajouter une paramétrisation** — `profile_from_parameters()` dans
   `fusion/parametric_driver.py` reconnaît la paramétrisation à ses paramètres
   et rend un « plan » ; tout l'aval (validation, maillage, CFD, boucle,
   rapport) est indépendant de la forme.

Dans tous les cas, `design_params.yaml` doit lister les paramètres avec leurs
bornes, et les noms doivent correspondre exactement.

---

## Où en est la v1.5

Les critères du §10 du document maître, et ce qui les atteste :

| critère | état | preuve |
|---|---|---|
| La v1.0 tourne toujours | ✅ | branche figée au tag `v1.0-stable`, jamais modifiée |
| Un profil CSV/DAT ingéré et re-paramétré à faible erreur | ✅ | Clark Y : 3,25 × 10⁻⁴ de corde en écart maximal |
| Une optimisation de ≥ 15 itérations aboutit de façon fiable | ✅ | 25 itérations, **0 échec** |
| Les deux producteurs de géométrie fonctionnent | ✅ | interne exercé en continu ; Fusion couvert par une émulation de l'API `adsk` |
| Un paquet `best_design` complet est produit automatiquement | ✅ | [exemple versionné](docs/example_report_clarky/) |
| Des instructions claires et vérifiables pour reprendre en CAO | ✅ | `FUSION_RETURN.md` + script généré + contrôle d'aller-retour |
| Une structure prête pour un backend 3D | ✅ | voir ci-dessous |

Ce qui reste hors périmètre, assumé : les modes 3 et 4 du §3 — ingestion d'un
STEP/DXF et d'un design Fusion existant — demandent un noyau CAO ou une session
Fusion pilotable, que rien ici ne fournit.

Une réserve honnête sur le producteur Fusion : il est exercé contre une
**émulation** de l'API `adsk`, pas contre Fusion. Un faux valide la logique du
driver, pas la lecture de l'API — un bug de ce genre est déjà passé au travers
de 206 tests parce que la doublure encodait la même erreur de compréhension que
le code.

---

## Vers la 3D (v2.0)

La v1.5 est délibérément restée en 2D, mais son architecture a été construite
pour que le passage à la 3D soit une addition et non une réécriture. Trois
points préparent le terrain :

**L'interface `GeometryBackend` ne suppose rien de la dimension.** Elle rend un
`GeometryResult` avec un STL et un compte rendu ; un producteur qui empile
plusieurs sections vrillées le remplirait de la même façon. Un troisième
backend s'enregistre avec un décorateur, sans toucher au reste — un test le
vérifie sur un backend fictif.

**La paramétrisation est reconnue, pas supposée.** Le champ `parameterization`
et la détection par les noms de paramètres laissent la place à une valeur
`cst3d` sans casser les fichiers existants. Un profil 3D se décrit
naturellement comme plusieurs jeux de coefficients CST le long de l'envergure,
plus une loi de vrillage et d'effilement.

**Ce qui est déjà générique le reste.** Le dimensionnement du case OpenFOAM se
déduit de l'emprise, pas d'une hypothèse 2D ; la porte de reconstruction, le
contrôle d'aller-retour et les bornes à autorité géométrique se transposent
station par station.

Ce qui devra changer, en revanche : les plans de symétrie du case quasi-2D
laisseront place à un vrai domaine 3D, le coût CFD par itération montera d'un
ordre de grandeur, et la recherche par motif sur vingt-quatre variables devra
probablement céder la place à une méthode qui exploite les gradients — c'est
là, et pas dans la géométrie, que se trouve le vrai obstacle.

## Licence

Aucune licence n'est déclarée à ce jour : tous droits réservés par défaut.
Ajoutez un fichier `LICENSE` si vous souhaitez autoriser la réutilisation.
