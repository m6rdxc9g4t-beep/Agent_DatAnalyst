# Architecture

## Pourquoi une extension de Nexus

Nexus fournit déjà ce qu'un agent sérieux exige : boucle d'agent, routeur Claude / Ollama,
validation humaine des écritures, verrou local des données sensibles, artefacts, historique.
Refaire une boucle à côté (comme le `agent.py` du module 1) dupliquerait ces garde-fous.
Agent_DatAnalyst ajoute donc à Nexus, de l'extérieur : des connecteurs (outils), deux agents
et leurs prompts. Nexus le charge parce qu'il est nommé dans `NEXUS_PLUGINS`.

Ce que Nexus apporte (PR #29) et que l'extension ne peut pas faire seule :

| Crochet | Rôle |
|---|---|
| `app/plugins.py` | importe `atelier_datanalyst` et appelle `register()` |
| `env.conversation_files()` | fichiers joints de la conversation en cours, et seulement eux |
| `env.client(nom, fabrique)` | clients HTTP de l'extension fermés en fin d'exécution |

## Deux agents

| | Data Analyst Atelier (`datanalyst`) | Trésorier (`tresorier`) |
|---|---|---|
| Moteur | au choix (Claude ou local) | **local imposé** |
| Applications | Convert, Viz, Tab, Projets | Trésorerie, Convert |
| Itérations max | 16 | 14 |

La séparation découle d'une règle de Nexus : un agent qui touche une boîte `sensitive` est
verrouillé sur Ollama. Un agent unique serait donc local pour tout, y compris Viz ; deux agents
gardent Claude là où il apporte le plus (composition de rapports, plans de travail) et la
confidentialité là où elle est exigée. La délégation de Nexus ne permet pas à un orchestrateur
sur Claude d'appeler un agent local (sa réponse repartirait chez Claude) : le Data Analyst
**oriente** l'utilisateur vers le Trésorier au lieu de déléguer.

## Modules

| Module | Contenu | Routes appelées (lues dans le dépôt de l'app) |
|---|---|---|
| `files.py` | `conversation_files` | — (Nexus) |
| `convert.py` | client + 6 outils | `GET /conversions`, `GET /tab`, `GET /conversions/{id}/export`, `GET /atelier/conversions/{id}/table`, `POST /atelier/conversions`, `POST /atelier/conversions/{id}/send-to-tab` |
| `viz.py` | `VizWriteClient` + 2 outils | `POST /datasets`, `POST /atelier/datasets`, `POST /ai/visuals/repair`, `POST /reports`, `PUT /reports/{id}` ; schéma lu par le client Data Via de Nexus |
| `projets.py` | `PlanClient` + `projets_create_plan` | `POST /projects`, `/projects/{id}/phases`, `/phases/{id}/epics`, `/epics/{id}/stories`, `/stories/{id}/work-items` |
| `tresorerie.py` | `TresoWriteClient` + 5 outils | `POST /accounts`, `/transactions`, `/holdings`, `/import/preview`, `/import/commit` |
| `toolsets.py` | boîtes, libellés, identité de Convert | — |
| `agents.py` | les deux agents | — |

## Parcours types

**Fichier de ventes → rapport Viz.** `conversation_files` → `data_via_list_datasets` (pas de
doublon) → `data_via_import_file` *(validation)* → `data_via_describe_dataset` →
`data_via_query` (chiffres clés) → `data_via_create_report` *(validation)*. Le rapport : une
page 16:9, visuels en grille de deux colonnes, options par défaut du registre de Viz.

**PDF → table Tab.** `convert_file` *(validation)* → `convert_read_table` →
`convert_tab_projects` → `convert_send_to_tab` *(validation)*.

**PDF → visualisation.** `convert_file` → `convert_send_to_viz` (clé `convert:<id>` : un renvoi
met le jeu à jour) → `data_via_describe_dataset` → `data_via_create_report`.

**Fichier → tâches.** L'agent lit le fichier (texte extrait par Nexus), `projets_list_projects`,
puis `projets_create_plan` *(une validation pour tout le plan)*. Table déjà dans Tab avec une
colonne titre : import Tab → Projets fourni par Nexus (sans doublon au réimport).

**Relevé PDF → Trésorerie (Trésorier, local).** `convert_file` → `tresorerie_preview_import`
(`conversion_id` : Convert exporte en xlsx, Trésorerie calcule l'aperçu) →
`tresorerie_import_statement` *(validation ; refait l'aperçu et n'insère que les lignes « ok »)*.

## Limites connues

- Atelier Viz n'a pas de route de création de rapport « métier » : l'extension compose le JSON
  du rapport (miroir de `schemas/report.py`) et le fait valider par `visuals/repair`. Une
  évolution du registre des visuels de Viz demande de mettre à jour `VIZ_CATALOG`.
- `data_via_import_file` crée toujours un nouveau jeu (la route d'import de fichier de Viz n'a
  pas de clé) ; le prompt impose de vérifier la liste des jeux avant.
- Un plan Projets n'est pas transactionnel : en cas d'échec au milieu, l'outil liste ce qui a
  déjà été créé et demande de compléter plutôt que de tout relancer.
- Le banc d'évaluation de Nexus ne simule ni Convert ni les pièces jointes :
  [evals/parcours-manuels.md](../evals/parcours-manuels.md).
