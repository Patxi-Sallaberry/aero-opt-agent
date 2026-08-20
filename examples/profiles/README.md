# Profils d'exemple

Deux profils au format **Selig** (une ligne de titre, puis `x y` du bord de
fuite vers l'extrados, le bord d'attaque, l'intrados, et retour au bord de
fuite), en corde unitaire.

| Fichier | Profil | Épaisseur | Cambrure |
|---|---|---|---|
| `naca2412.dat` | NACA 2412 | 12 % | 2 % à 40 % de corde |
| `naca0012.dat` | NACA 0012 | 12 % | aucune (symétrique) |

Ils sont calculés à partir des équations analytiques NACA à 4 chiffres, sur 161
points en répartition cosinus. Cela les rend utiles au delà de la
démonstration : leur épaisseur, leur cambrure et leur rayon de nez
(r = 1,1019 · t² = 0,01587 c) sont connus exactement, ce qui permet de vérifier
l'ajustement CST contre la géométrie véritable plutôt que contre la sortie du
code.

Le couple symétrique / cambré n'est pas décoratif non plus : plusieurs
propriétés de la paramétrisation ne se distinguent que sur l'un des deux — le
rayon de nez, notamment, ne se lit pas de la même façon selon que le profil est
cambré ou non (voir `CSTProfile.leading_edge_radius`).

```bash
python3 -m profiles.reparameterize examples/profiles/naca2412.dat --order 7
```
