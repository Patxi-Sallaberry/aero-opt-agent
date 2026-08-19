# MASTER DOCUMENTATION — Core First (v1.0)
## Système d’Optimisation Aérodynamique Agentique
### Fusion 360 + OpenFOAM + LLM Orchestrator

**Version :** 1.0 – Core First  
**Date :** 19 août 2026  
**Objectif de cette version :** Avoir le plus rapidement possible un système **fiable et fonctionnel** qui prend un design CAO paramétrique Fusion 360 et optimise automatiquement sa forme pour un objectif aérodynamique.  
**Trajectoire :** Une fois le Core stable et prouvé sur un vrai design, on montera vers les fonctionnalités avancées (multi-fidélité, Active Learning, multi-agents complets, Pareto…).

**Destinataire :** Claude Code  
**Statut :** Source unique de vérité pour la construction du Core. Lis ce document entièrement avant de commencer.

---

## 0. Objectif clair du système (Core)

1. L’utilisateur fournit un **modèle Fusion 360 paramétrique** (seed `.f3d`) avec des User Parameters bien définis.
2. Le système lit un fichier `design_params.yaml` (valeurs actuelles + bornes + max_delta).
3. Il modifie les paramètres dans Fusion → exporte un STEP.
4. Il lance une simulation OpenFOAM automatique.
5. Il extrait les coefficients aérodynamiques (Cd, Cl, Cl/Cd…).
6. Un agent LLM analyse les résultats et propose de **nouvelles valeurs de paramètres** (dans les bornes et sans dépasser max_delta).
7. On boucle jusqu’à atteindre l’objectif ou un budget d’itérations.

**C’est de l’optimisation de forme paramétrique agentique.**  
Le Core doit être simple, robuste et fiable avant d’ajouter de la complexité.

---

## 1. Architecture Core (simple et efficace)

```
┌─────────────────────────────────────────────┐
│           ORCHESTRATEUR (LLM / Claude Code) │
│  - Lit results.json + design_params.yaml    │
│  - Analyse et propose nouveaux paramètres   │
│  - Écrit uniquement design_params.yaml      │
└────────────────────┬────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────┐
│              MASTER PIPELINE                │
│  1. Charge design_params.yaml               │
│  2. Driver Fusion → modifie paramètres      │
│  3. Export STEP                             │
│  4. Validation géométrie                    │
│  5. OpenFOAM (maillage + calcul)            │
│  6. Post-processing → results.json          │
│  7. Archivage dans data/iterations/         │
└─────────────────────────────────────────────┘
```

**Règle d’or :**  
L’agent LLM ne touche **jamais** au code du pipeline.  
Il n’édite **que** le fichier `configs/design_params.yaml`.

---

## 2. Structure de dépôt (exacte)

```
aero-opt-agent/
├── README.md
├── MASTER_DOCUMENTATION_AGENTIC_AERO_OPTIMIZATION.md   # CE FICHIER
├── requirements.txt
├── .env.example
├── .gitignore
├── configs/
│   ├── design_params.yaml              # ← SEUL fichier modifié par l’agent
│   └── cfd_settings.yaml
├── fusion/
│   ├── seed_design.f3d                 # Fourni par l’utilisateur
│   └── parametric_driver.py            # Script Python Fusion
├── openfoam/
│   ├── templates/                      # Case OpenFOAM de base
│   ├── run_cfd.sh
│   └── postprocess.py
├── pipeline/
│   ├── master_pipeline.py              # Point d’entrée unique
│   ├── geometry_validator.py
│   └── utils.py
├── agent/
│   ├── orchestrator.py                 # Boucle principale (ou instructions pour Claude Code)
│   └── prompts/
│       └── system_prompt.md
├── data/
│   └── iterations/                     # iter_0001/, iter_0002/...
├── tests/
└── scripts/
    └── run_loop.py
```

---

## 3. Spécifications Core

### 3.1 design_params.yaml (contrat strict)

```yaml
iteration: 0
design_id: "wing_v01"

parameters:
  chord_mm:
    value: 300.0
    min: 220.0
    max: 420.0
    max_delta_pct: 7.0
    unit: "mm"
  aoa_deg:
    value: 4.0
    min: -1.0
    max: 10.0
    max_delta_pct: 12.0
    unit: "deg"
  # Ajouter ici TOUS les User Parameters du modèle Fusion

constraints:
  topology_preserving: true
  min_wall_thickness_mm: 1.5

objectives:
  primary: "maximize_Cl_Cd"     # ou "maximize_downforce" / "minimize_Cd"
```

**Règles obligatoires pour l’agent :**
- Ne jamais sortir des `[min, max]`
- Ne jamais dépasser `max_delta_pct` par rapport à la dernière itération réussie
- Toujours proposer des valeurs numériques cohérentes

### 3.2 Driver Fusion (`fusion/parametric_driver.py`)

- Ouvre le design (ou le document actif)
- Lit `design_params.yaml`
- Pour chaque paramètre : met à jour `userParameters.itemByName(...).expression`
- Exporte en STEP dans `data/iterations/iter_XXXX/geometry.step`
- Retourne succès / échec

### 3.3 Pipeline OpenFOAM

- Template de case external aero propre
- snappyHexMesh + checkMesh
- simpleFoam (ou pimpleFoam)
- Extraction des forceCoeffs (Cd, Cl, Cl/Cd…)
- Sortie standardisée : `results.json`

Exemple de `results.json` :

```json
{
  "iteration": 5,
  "success": true,
  "Cd": 0.043,
  "Cl": 1.18,
  "Cl_Cd": 27.4,
  "mesh_ok": true,
  "error_message": null
}
```

### 3.4 Master Pipeline (`pipeline/master_pipeline.py`)

Fonction unique :

```python
def run_iteration(config_path: str) -> dict:
    # 1. Valider config
    # 2. Appeler parametric_driver Fusion
    # 3. Valider géométrie
    # 4. Lancer OpenFOAM
    # 5. Post-process → results.json
    # 6. Archiver
    # 7. Retourner results
```

### 3.5 Agent / Orchestrateur

- Lit le dernier `results.json` + le `design_params.yaml` actuel
- Analyse les résultats par rapport à l’objectif
- Propose un nouveau `design_params.yaml` respectant strictement les bornes et max_delta
- Écrit le fichier
- Lance `python pipeline/master_pipeline.py`
- Répète

**Prompt système** (à placer dans `agent/prompts/system_prompt.md`) :  
Doit forcer le respect des contraintes, un raisonnement simple sur les résultats, et un output structuré.

---

## 4. Mesures de robustesse (obligatoires dès le Core)

1. Bornes min/max + max_delta_pct dans le YAML
2. Validation géométrie après export STEP
3. checkMesh obligatoire
4. En cas d’échec → feedback clair + l’agent doit proposer une modification plus conservative
5. Archivage de chaque itération (même échouée)

---

## 5. Plan d’implémentation (ordre strict)

**Phase 0 – Fondations**
- Structure de dossiers exacte
- Git + .gitignore
- requirements.txt
- Schema / validation de design_params.yaml
- Fichiers de config de base

**Phase 1 – Geometry (Fusion)**
- `parametric_driver.py` complet
- Test avec le seed (une fois fourni)

**Phase 2 – OpenFOAM**
- Template de case
- `run_cfd.sh` + `postprocess.py`
- results.json propre

**Phase 3 – Master Pipeline**
- Enchaînement complet + validation + archivage
- Smoke test end-to-end

**Phase 4 – Agent + Boucle**
- Prompt système
- Orchestrateur / boucle
- Première optimisation réelle sur un design

**Phase 5 – Durcissement**
- Amélioration des messages d’erreur
- README clair
- Premiers tests sur un vrai design Formula Student / aéro

---

## 6. Ce qui viendra après (roadmap vers le meilleur outil)

Une fois le Core stable et prouvé :
- Multi-fidélité (coarse → fine)
- Active Learning + Surrogate
- Multi-agents spécialisés
- Pareto multi-objectif
- Design Automation cloud (headless)
- Self-improvement
- Observabilité avancée

Le Core est conçu pour que ces évolutions s’ajoutent proprement.

---

## 7. Instructions finales pour Claude Code

1. Lis **intégralement** ce document avant de commencer.
2. Respecte strictement la structure de dossiers et le découplage (l’agent ne touche que `design_params.yaml`).
3. Commence uniquement par la Phase 0.
4. Quand la Phase 0 est terminée et testée, arrête-toi et fais un rapport.
5. Demande le fichier seed `.f3d` et les clés API quand tu en auras besoin.
6. Priorise toujours la robustesse et la simplicité dans cette version Core.

**Objectif de cette version :**  
Avoir le plus vite possible un système qui prend un design Fusion et optimise réellement sa forme aérodynamique de façon fiable.

Ce document est la loi pour le Core.

**Fin du Master Document – Core First v1.0**
