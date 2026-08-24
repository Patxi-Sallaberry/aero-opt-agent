# Reprendre ce design dans Fusion 360

Le résultat d'une optimisation n'est utile que si l'on peut continuer à le travailler. Ce document donne les trois façons de ramener la forme optimisée dans Fusion, de la plus propre à la plus universelle.

## La forme à reproduire

| grandeur | valeur |
|---|---|
| corde | 300.00 mm |
| envergure | 80.00 mm |
| incidence | 3.00° |
| épaisseur relative | 0.1118 |
| cambrure relative | 0.0351 |
| paramétrisation | `cst` |
| profil d'origine | `examples/profiles/clarky.dat` |
| ordre CST | 11 (24 coefficients) |

**L'incidence est déjà dans les coordonnées.** La section exportée est celle qui a été simulée, profil incliné compris. Si le montage aval applique lui-même une incidence, il faut partir du profil redressé, sans quoi elle serait comptée deux fois.

## Voie la plus courte — ouvrir `geometry.step`

*File → Open*, ou glisser-déposer le fichier dans Fusion. C'est un vrai solide B-Rep de quelques faces : on peut y poser un congé, en changer l'envergure, l'assembler. Aucune conversion, aucun script.

> **Ne pas ouvrir `geometry.stl` à la place.** Fusion sait le lire, mais il en fait un corps maillé de plusieurs centaines de facettes planes, inutilisable pour de la conception. Le STL est là pour le solveur et l'impression.

Cette voie ne rend pas un modèle *paramétrique* : le solide n'a pas d'historique de features. Pour cela, voir la voie 1.

## Voie 1 — rejouer les paramètres (recommandée)

C'est la seule voie qui rend un modèle **paramétrique** : un historique de features modifiable, pas une importation figée.

```bash
cp design_params.yaml <projet>/configs/design_params.yaml
```

Puis, dans Fusion : ouvrir le modèle de départ, aller dans *Utilities → ADD-INS → Scripts and Add-Ins*, et lancer `fusion/parametric_driver.py`. Le driver reconstruit exactement cette forme et exporte STEP et STL.

> Le driver accepte les deux paramétrisations. Sur un fichier `cst`, il reconstruit la forme depuis les coefficients de Kulfan — le tracé ne manipule que des points, la voie Fusion n'a donc besoin d'aucun code supplémentaire.

> Cette optimisation a tourné sur le calculateur **interne**, sans Fusion. Les paramètres restent parfaitement rejouables : c'est le même fichier qui décrit la forme des deux côtés.

## Voie 2 — script prêt à l'emploi

`rebuild_in_fusion.py` contient les coordonnées de CE profil et trace l'esquisse tout seul, puis l'extrude sur l'envergure.

1. Fusion 360 → *Utilities → ADD-INS → Scripts and Add-Ins*
2. Onglet **Scripts** → **+** → choisir `rebuild_in_fusion.py`
3. **Run**

Le script trace **une spline par surface** plutôt qu'une seule sur tout le contour : au bord d'attaque la courbe rebrousse, et une spline unique y placerait un point d'inflexion au lieu d'un nez — ce qui abîmerait précisément la zone qui décide du décrochage.

Aucun fichier à localiser, aucune unité à convertir : les coordonnées sont écrites dans le script, en centimètres, l'unité interne de l'API Fusion.

## Voie 3 — importer la section à la main

Utile si l'on préfère garder la main, ou travailler dans un autre logiciel de CAO.

1. Ouvrir `profile_section.csv` — trois colonnes : `surface`, `x_mm`, `y_mm`.
2. Dans Fusion : *Insert → Insert Manufacturing Model* ou un add-in d'import de points ; sinon, tracer une spline en saisissant les points.
3. Passer une spline ajustée par les points de chaque surface.
4. Refermer le bord de fuite, puis extruder sur 80.0 mm.

`profile_section.dat` porte les mêmes points au format profil standard.

`profile_chord.dat` porte le profil **redressé**, en corde unitaire. C'est celui qu'il faut donner à XFOIL ou XFLR5 : ces outils pilotent eux-mêmes l'incidence, et leur fournir une section déjà inclinée la compterait deux fois — tout le polaire serait décalé sans que rien ne le signale.

## Ce qu'il vaut mieux éviter

**Convertir `geometry.stl` en solide.** Fusion sait le faire, mais le résultat est un maillage de plusieurs centaines de faces planes : impossible d'y poser un congé propre, impossible d'en changer une cote. Le STL est là pour la simulation et l'impression, pas pour la conception.

## Vérifier que la reprise est fidèle

Après reconstruction, exporter un STL depuis Fusion et le comparer à la section de CE dossier :

```bash
python3 -m profiles.roundtrip export_fusion.stl profile_chord.dat \
    --chord 300.0 --aoa 3.00
```

L'outil relit le fichier, en extrait la section et mesure sa distance au profil — sans faire confiance à ce qui a servi à l'écrire. Un écart au delà de 2 × 10⁻³ de corde signale une erreur d'échelle, d'unité ou d'orientation.

La référence doit être `profile_chord.dat` ou `profile_section.dat`, **pas le profil de départ**. Le design a été optimisé : il s'écarte de son point de départ à dessein, et l'outil signalerait cet écart voulu comme un défaut.

Mesuré sur le solide de ce dossier : l'écart entre `design_params.yaml` et `geometry.stl` est de l'ordre de 10⁻⁶ de corde — la chaîne de génération est exacte, ce qui reste à vérifier est ce que Fusion en fait.

