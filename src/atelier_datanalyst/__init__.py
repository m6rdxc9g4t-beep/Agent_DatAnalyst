"""Agent_DatAnalyst — extension d'Atelier Nexus.

Un agent qui part d'un fichier glissé dans le chat et le fait circuler dans la suite Atelier :
Convert (conversion), Tab (tables), Viz (jeux et rapports), Projets (plan de travail) ; et un
agent Trésorier, 100 % local, pour Atelier Trésorerie (comptes, relevés, achats, placements).

Activation dans Nexus : installer ce paquet dans l'environnement du backend, puis
`NEXUS_PLUGINS=atelier_datanalyst` dans `backend/.env` (voir README).
"""

from __future__ import annotations

__version__ = "0.1.0"

_registered = False


def register() -> None:
    """Appelé une fois par Nexus (`app/plugins.py`) : boîtes à outils, libellés, agents."""
    global _registered
    if _registered:
        return
    from atelier_datanalyst.toolsets import register_toolsets

    register_toolsets()
    from atelier_datanalyst import agents  # noqa: F401  (l'import enregistre les agents)

    _registered = True
