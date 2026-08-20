# aero-opt-agent — Optimisation Aérodynamique Agentique

**Fusion 360 + OpenFOAM + Orchestrateur LLM** — version **Core First v1.0**.

Le système part d'un design paramétrique, en produit la géométrie, la simule
sous OpenFOAM, lit les coefficients aérodynamiques, et propose les paramètres de
l'itération suivante. En boucle, sans intervention.

La spécification qui fait loi est
[`MASTER_DOCUMENTATION_AGENTIC_AERO_OPTIMIZATION.md`](MASTER_DOCUMENTATION_AGENTIC_AERO_OPTIMIZATION.md).
En cas de divergence, c'est elle qui gagne. Les écarts assumés sont listés
en fin de document.

---

## Démarrage

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt

# OpenFOAM (ESI) — fournit simpleFoam, snappyHexMesh, checkMesh
curl -s https://dl.openfoam.com/add-debian-repo.sh | sudo bash
sudo apt-get install openfoam2506-default

cp .env.example .env        # puis renseigner FOAM_BASHRC
```

Une itération :

```bash
python3 pipeline/master_pipeline.py
```

Une optimisation complète :

```bash
python3 scripts/run_loop.py --max-iterations 20 \
    --cfd-settings configs/cfd_settings_fast.yaml
```

C'est tout. La boucle produit la géométrie, maille, calcule, lit les résultats,
propose de nouveaux paramètres et recommence — jusqu'au budget d'itérations, à
la stagnation, ou à une série d'échecs.

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
  cfd_settings.yaml              conditions CFD (réglage fin)
  cfd_settings_fast.yaml         préréglage d'exploration, ~60 s/itération
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
tests/                           431 tests
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

## Géométrie

### Deux producteurs

| Producteur | Ce qu'il fait | Quand |
|------------|---------------|-------|
| `fusion` | Met à jour les User Parameters, reconstruit la géométrie, exporte STEP + STL | Le script tourne **dans** Fusion 360 |
| `internal` | Calcule le profil et écrit le STL en mètres, sans Fusion | Partout ailleurs — c'est ce qui rend la boucle autonome |

`auto` (défaut) choisit Fusion si son API est importable, le producteur interne
sinon. **L'API Fusion n'a pas de mode headless** : sans le producteur interne,
chaque itération attendrait qu'un humain clique sur *Run*. Les deux chemins
partagent la même fonction de profil, donc la même forme ; le mode interne ne
produit simplement pas de STEP, faute de noyau CAO.

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

Sur un modèle CFD analytique à optimum connu, la recherche améliore l'objectif
de **+657 %** en 25 itérations.

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
python3 -m pytest tests/ -q          # 431 tests, ~8 s
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

---

## À fournir

- `fusion/seed_design.f3d` — le modèle Fusion, si l'on veut passer par la CAO
  plutôt que par le producteur interne.
- `ANTHROPIC_API_KEY` dans `.env` — pour la stratégie LLM. Sans elle, la boucle
  tourne en recherche locale.
