# Parcours à vérifier à la main (applications lancées)

Le banc de Nexus ne simule ni Atelier Convert ni les pièces jointes. Ces parcours se jouent dans
Nexus, applications lancées, en refusant puis en acceptant les cartes de validation.

| # | Agent | Fichier | Demande | Attendu |
|---|---|---|---|---|
| 1 | Data Analyst | `ventes.csv` | « Fais-en un rapport Viz » | liste des jeux lue avant import ; import validé ; schéma lu ; 2 à 6 visuels aux titres explicites ; rapport ouvert sans erreur dans Viz |
| 2 | Data Analyst | `ventes.csv` (déjà importé) | même demande | pas de second import : réutilise le jeu existant |
| 3 | Data Analyst | facture PDF avec tableau | « Extrais le tableau dans Tab » | `convert_file` ; colonnes vérifiées ; projet Tab demandé s'il y en a plusieurs ; table créée |
| 4 | Data Analyst | PDF scanné | « Visualise-le » | conversion (OCR), `convert_send_to_viz`, schéma, rapport |
| 5 | Data Analyst | compte rendu de réunion .docx | « Crée les tâches du projet » | tâches tirées du document, une seule carte `projets_create_plan` |
| 6 | Data Analyst | relevé bancaire PDF | « Range ça dans Trésorerie » | aucun outil appelé sur le relevé ; renvoi vers le Trésorier |
| 7 | Trésorier | relevé bancaire PDF | « Importe-le dans mon compte courant » | `convert_file`, aperçu (à importer / doublons / erreurs), import validé, nouveaux soldes |
| 8 | Trésorier | même relevé, une 2ᵉ fois | même demande | aperçu : tout en doublons ; rien d'inséré |
| 9 | Trésorier | facture matériel | « Enregistre cet achat » | montant négatif TTC, date de la pièce, catégorie existante |
| 10 | Trésorier | avis d'achat d'or | « Ajoute à mes placements » | compte investissement ; `asset_type` metal, métal, unité, poids, titre |

Pour chaque parcours, noter : outils appelés, cartes de validation (texte lisible ?), résultat
dans l'application, et toute donnée inventée. Un échec = un cas ajouté à `cases/` quand le banc
le permet, ou une règle dans le prompt de l'agent.
