"""Les tests s'exécutent contre le backend d'Atelier Nexus (paquet `app`).

Chemin du backend : variable `NEXUS_BACKEND`, sinon un clone voisin `../Atelier_Nexus/backend`
(ou `../atelier_nexus/backend`). Aucun appel réseau : applications simulées par
`httpx.MockTransport`, comme dans les tests de Nexus.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CANDIDATES = [
    os.environ.get("NEXUS_BACKEND", ""),
    str(ROOT.parent / "Atelier_Nexus" / "backend"),
    str(ROOT.parent / "atelier_nexus" / "backend"),
]
NEXUS_BACKEND = next((Path(p) for p in _CANDIDATES if p and (Path(p) / "app").is_dir()), None)
if NEXUS_BACKEND is None:
    raise RuntimeError(
        "Backend d'Atelier Nexus introuvable : clonez Atelier_Nexus à côté de ce dépôt "
        "ou définissez NEXUS_BACKEND=/chemin/vers/Atelier_Nexus/backend."
    )
sys.path.insert(0, str(NEXUS_BACKEND))
sys.path.insert(0, str(ROOT / "src"))

# Réglages déterministes : aucun .env local, Convert configuré sur une adresse fictive
os.environ.setdefault("CONVERT_BASE_URL", "http://127.0.0.1:8010/api")
os.environ["TRIGGERS_ENABLED"] = "false"

import atelier_datanalyst  # noqa: E402

atelier_datanalyst.register()
