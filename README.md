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
| 1 | Geometry — `fusion/parametric_driver.py` | À faire |
| 2 | OpenFOAM — template de case, `run_cfd.sh`, `postprocess.py` | À faire |
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

---

## Structure du dépôt

```
.
├── configs/
│   ├── design_params.yaml     ← SEUL fichier modifié par l'agent
│   └── cfd_settings.yaml         conditions CFD, jamais touchées par l'agent
├── fusion/
│   ├── seed_design.f3d           fourni par l'utilisateur (non versionné)
│   └── parametric_driver.py      Phase 1
├── openfoam/
│   ├── templates/                case de base            Phase 2
│   ├── run_cfd.sh                                        Phase 2
│   └── postprocess.py            → results.json          Phase 2
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
