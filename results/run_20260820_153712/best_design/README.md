# Design optimisé — `wing_v01`

Meilleure des **30 itérations** de la série `iterations` (30 réussies, 0 échouée), retenue sur l'objectif `maximize_Cl_Cd`.

## Performances

| | Cd | Cl | Cl/Cd |
|---|---|---|---|
| Départ (itération 0) | 0.05081 | 0.17500 | 3.44 |
| **Optimisé (itération 29)** | **0.05144** | **1.07531** | **20.90** |

**Gain de finesse : +506.9 %**

Maillage : ? cellules, non-orthogonalité —, skewness —. Coefficients moyennés sur ? itérations, écart-type relatif — sur Cd — **encore instables**.

## Paramètres : départ → arrivée

| paramètre | départ | arrivée | écart | bornes |
|---|---|---|---|---|
| `chord` | 300 | **298.53** mm | -0.5 % | 220 … 420 |
| `thickness` | 0.12 | **0.08** unitless | -33.3 % | 0.08 … 0.2 |
| `camber` | 0.02 | **0.0350769** unitless | +75.4 % | 0 … 0.09 |
| `span` | 80 | **80** mm | inchangé | 79 … 81 |
| `aoa` | 0 | **5.04** deg | +5.04 | -2 … 12 |

## Avant / après

Le seed de départ face au design retenu, tous deux mesurés **dans le même régime CFD** (même régime) — comparer un maillage fin à un maillage d'exploration gonflerait le gain sans qu'il soit réel.

![Performances avant / après](figures/comparison_performance.svg)

| | seed | optimisé | écart |
|---|---|---|---|
| **Portance Cl** | 0.1750 | **1.0753** | +514.5 % ✓ |
| **Traînée Cd** | 0.05081 | **0.05144** | +1.2 % ✗ |
| **Finesse Cl/Cd** | 3.44 | **20.90** | +506.9 % ✓ |

![Sections avant / après](figures/comparison_sections.svg)

Les deux sections sont dessinées à la **même échelle** : mises chacune à la taille de son cadre, elles paraîtraient identiques et l'écart de corde comme l'incidence passeraient inaperçus.

![Sections superposées](figures/comparison_overlay.svg)

> Contours du seed non produits : champs du seed indisponibles (maillage purgé) : les contours avant / après demandent de réévaluer le seed avec `keep_case_after_run: true`

## Pourquoi cette forme est meilleure

- **Incidence augmentée de 5.04°** (0.00° → 5.04°). C'est le levier le plus direct sur la portance : incliner le profil dévie davantage l'écoulement vers le bas, et la réaction de cette déviation *est* la portance. La traînée induite croît en gros comme le carré de la portance, si bien que la finesse passe par un maximum — typiquement entre 4° et 6° pour un profil cambré — puis s'effondre au décrochage. La valeur retenue, 5.04°, tombe dans cette plage : la recherche a trouvé le sommet de la courbe.
- **Cambrure accentuée** (0.0200 → 0.0351). La cambrure décale toute la courbe de portance : un profil cambré porte déjà à incidence nulle. Elle se paie en traînée de forme et en moment de tangage, d'où l'existence d'un optimum plutôt qu'une croissance sans fin.
- **Profil aminci** (0.1200 → 0.0800 d'épaisseur relative). Un profil plus fin perturbe moins l'écoulement et traîne moins. La contrepartie est structurelle — moins d'inertie, donc moins de rigidité — et un décrochage plus brutal, le bord d'attaque plus aigu supportant mal les fortes incidences.
- **Corde portée à 298.5 mm** (depuis 300.0). Elle agit par deux voies : le nombre de Reynolds augmente, ce qui abaisse légèrement le coefficient de frottement, et la surface de référence change — elle est recalculée à chaque itération, sans quoi la comparaison des coefficients n'aurait aucun sens.
- **Le compromis chiffré** : la portance gagne +514 % pour seulement +1 % de traînée. C'est exactement ce que cherche une optimisation de finesse — non pas traîner moins, mais porter beaucoup plus pour un supplément de traînée modeste.

## Déroulé de l'optimisation

![Finesse au fil des itérations](figures/optimization_progress.svg)

![Cd et Cl au fil des itérations](figures/coefficients_progress.svg)

| itération | Cd | Cl | Cl/Cd | statut |
|---|---|---|---|---|
| 0 | 0.05081 | 0.17500 | 3.44 | OK |
| 1 | 0.05081 | 0.17500 | 3.44 | OK |
| 2 | 0.05071 | 0.19900 | 3.92 | OK |
| 3 | 0.05082 | 0.17260 | 3.40 | OK |
| 4 | 0.05072 | 0.19636 | 3.87 | OK |
| 5 | 0.05071 | 0.19900 | 3.92 | OK |
| 6 | 0.05407 | 0.19900 | 3.68 | OK |
| 7 | 0.05240 | -0.11531 | -2.20 | OK |
| 8 | 0.05060 | 0.22540 | 4.45 | OK |
| 9 | 0.05396 | 0.22540 | 4.18 | OK |
| 10 | 0.05033 | 0.22540 | 4.48 | OK |
| 11 | 0.04699 | 0.22540 | 4.80 | OK |
| 12 | 0.04392 | 0.22540 | 5.13 | OK |
| 13 | 0.04109 | 0.22540 | 5.48 | OK |
| 14 | 0.03850 | 0.22540 | 5.86 | OK |
| 15 | 0.03660 | 0.22540 | 6.16 | OK |
| 16 | 0.03672 | 0.19636 | 5.35 | OK |
| 17 | 0.03830 | 0.49955 | 13.04 | OK |
| 18 | 0.04338 | 0.73934 | 17.04 | OK |
| 19 | 0.05185 | 0.94189 | 18.17 | OK |
| 20 | 0.06371 | 1.10717 | 17.38 | OK |
| 21 | 0.05197 | 0.91314 | 17.57 | OK |
| 22 | 0.05186 | 0.93901 | 18.11 | OK |
| 23 | 0.05409 | 0.94189 | 17.41 | OK |
| 24 | 0.05174 | 0.97064 | 18.76 | OK |
| 25 | 0.04328 | 0.76809 | 17.75 | OK |
| 26 | 0.05398 | 0.97064 | 17.98 | OK |
| 27 | 0.05164 | 1.00226 | 19.41 | OK |
| 28 | 0.05154 | 1.03705 | 20.12 | OK |
| 29 ⭐ | 0.05144 | 1.07531 | 20.90 | OK |

## L'écoulement

> Visuels CFD non produits : case OpenFOAM absent du dossier exporté

## Ce que valent ces chiffres

Le modèle de turbulence `kOmegaSST` suppose la couche limite turbulente dès le bord d'attaque. À Re ≈ 4 × 10⁵, une bonne part de l'extrados est encore laminaire : **la traînée est surestimée**, d'un facteur qui peut approcher 2. Ces valeurs classent correctement des formes entre elles — ce qu'exige une optimisation — mais ne constituent pas une prédiction de traînée absolue. Pour un chiffre publiable, il faut un modèle avec transition laminaire-turbulent, ou une soufflerie.

## Contenu du dossier

| fichier | quoi |
|---|---|
| `geometry.stl` | la géométrie, **en mètres**, telle que simulée |
| `profile_section.csv` | section 2D en millimètres |
| `profile_section.dat` | même section au format profil (XFOIL, XFLR5) |
| `design_params.yaml` | les paramètres exacts, rejouables |
| `results.json` | les coefficients |
| `report.html` | ce rapport, autonome, pour un navigateur |
| `figures/` | courbes et images |
| `logs/` | journaux de chaque étape |

### Pas de fichier STEP

Cette géométrie a été produite par le calculateur interne, qui écrit directement un STL : sans noyau CAO, il ne peut pas générer de STEP. Deux façons d'en obtenir un :

1. **Depuis Fusion 360** — copier `design_params.yaml` dans `configs/`, ouvrir le modèle, lancer `fusion/parametric_driver.py` (*Utilities → ADD-INS → Scripts and Add-Ins*). Le driver reconstruit exactement cette forme et exporte STEP **et** STL.
2. **En repartant de la section** — importer `profile_section.csv` comme nuage de points en CAO, y passer une spline, extruder sur l'envergure. C'est la voie à préférer pour de la conception : on récupère une géométrie propre et paramétrable, là où une conversion de STL ne donnerait qu'un solide facetté de plusieurs centaines de faces.

## Ouvrir les fichiers

```bash
# la géométrie (STL en mètres)
paraview geometry.stl

# reprendre l'optimisation depuis ce design
cp design_params.yaml configs/design_params.yaml
python3 scripts/run_loop.py --max-iterations 20 \
    --cfd-settings configs/cfd_settings_fast.yaml
```

---

Exporté le 20/08/2026 à 13:37 UTC par `scripts/export_best.py`.
