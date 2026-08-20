# Profils d'exemple

Tous au format **Selig** : une ligne de titre, puis `x y` du bord de fuite vers
l'extrados, le bord d'attaque, l'intrados, et retour au bord de fuite.

## Profils analytiques

| Fichier | Profil | Points | Épaisseur | Cambrure |
|---|---|---|---|---|
| `naca2412.dat` | NACA 2412 | 161 | 12 % | 2 % à 40 % de corde |
| `naca0012.dat` | NACA 0012 | 161 | 12 % | aucune (symétrique) |

Calculés à partir des équations NACA à 4 chiffres, en répartition cosinus. Cela
les rend utiles au delà de la démonstration : leur épaisseur, leur cambrure et
leur rayon de nez (r = 1,1019 · t² = 0,01587 c) sont connus **exactement**, ce
qui permet de vérifier l'ajustement CST contre la géométrie véritable plutôt
que contre la sortie du code.

Le couple symétrique / cambré n'est pas décoratif non plus : le rayon de nez ne
se lit pas de la même façon selon que le profil est cambré ou non (voir
`CSTProfile.leading_edge_radius`).

## Profils réels

Coordonnées publiées, issues de la base de profils de l'université de
l'Illinois (UIUC) via [airfoiltools.com](http://airfoiltools.com).

| Fichier | Profil | Points | Ce qu'il apporte |
|---|---|---|---|
| `clarky.dat` | Clark Y | 121 | le classique à intrados plat, avec un bord de fuite épaissi |
| `e387.dat` | Eppler E387 | 61 | **fichier trop grossier** — refusé par la porte |
| `s1223.dat` | Selig S1223 | 81 | forte portance, très cambré — refusé aussi |

Les deux refus sont délibérément conservés. Ils documentent la limite réelle de
la méthode et servent de cas de test : un fichier de soixante et un points ne
décrit pas assez finement son bord d'attaque pour qu'un ajustement CST y reste
sous 5 × 10⁻⁴ de corde, et **monter l'ordre n'y change rien** — la validation
croisée montre que l'ordre 13 y épouse le bruit du fichier plutôt que sa forme.

C'est aussi sur ces profils réels que l'ordre par défaut a été recalibré : il
était fixé à 7, valeur suffisante pour un NACA — une famille à trois paramètres
— et insuffisante pour les trois.

```bash
python3 -m profiles.reparameterize examples/profiles/clarky.dat --chord 300
python3 -m profiles.reparameterize examples/profiles/e387.dat     # refusé, et dit pourquoi
```
