# CLAUDE.md — Agent_DatAnalyst

Extension d'Atelier Nexus (paquet `atelier_datanalyst`, chargé par `NEXUS_PLUGINS`). Elle ajoute
des connecteurs (Convert, écritures Viz, plan Projets, saisies Trésorerie) et deux agents :
Data Analyst Atelier et Trésorier. Architecture : docs/architecture.md.

## Commandes
- Tests : `pytest` (Atelier_Nexus cloné à côté, ou `NEXUS_BACKEND=/chemin/backend`)
- Lint : `ruff check src tests` ; format : `ruff format src tests`
- Types : `MYPYPATH=src:../Atelier_Nexus/backend mypy`
- Évaluations : voir evals/cases/datanalyst.toml (banc de Nexus)

## Règles
- Le code s'exécute DANS le backend de Nexus : on importe `app.*` (BaseTool, AppClient,
  register_toolset, register_agent…), on ne recopie pas Nexus. Nouveau besoin côté Nexus =
  PR sur Atelier_Nexus documentée dans docs/extensions.md, jamais un accès à un attribut privé.
- Aucun endpoint inventé : toute route appelée est lue dans le dépôt de l'application et citée
  dans la docstring du module. Schémas Pydantic : réponses `extra="ignore"`, requêtes
  `extra="forbid"`.
- Tout outil qui écrit : `requires_approval = True` + `approval_summary()` concret en français.
  Tout outil Trésorerie : `sensitive = True` (et sa boîte). Le Data Analyst ne reçoit jamais de
  boîte sensible.
- Fichiers joints : uniquement par `env.conversation_files()` ; leur contenu ne part jamais dans
  le `content` d'un outil.
- Écritures rejouables : `Idempotency-Key` stable quand l'application l'accepte.
- Tests sans réseau (httpx.MockTransport), faux serveurs fidèles aux contrats ; chaque nouvel
  outil a un libellé dans `toolsets.LABELS`.
- Branche par évolution (`feat/…`), jamais de push sur `main`.
