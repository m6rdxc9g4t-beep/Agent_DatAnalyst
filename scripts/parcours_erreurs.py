"""Cas d'erreur et variantes, contre les applications LANCÉES (données de démonstration).

    cd Atelier_Nexus/backend
    NEXUS_PLUGINS=atelier_datanalyst PYTHONPATH=. python ../../Agent_DatAnalyst/scripts/parcours_erreurs.py

Attendu : refus clairs, et aucun rapport créé quand un visuel est refusé.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import httpx
from app.agents import list_agent_classes
from app.core.config import Settings
from app.schemas.attachments import AttachmentInfo
from app.tools import ToolContext
from app.tools.environment import ToolEnvironment
from app.tools.files import ConversationFiles
from app.tools.toolsets import build_tools

RUN = datetime.now().strftime("%H%M%S")
RELEVE_CSV = (
    "Date;Libellé;Montant\n"
    f"05/10/2026;Facture Leroy Merlin perceuse {RUN};-129,90\n"
    f"07/10/2026;Salaire octobre {RUN};2 450,00\n"
    f"09/10/2026;Abonnement logiciel {RUN};-12,00\n"
).encode()
FILES = {
    "e" * 31 + "1": ("releve-octobre.csv", "text/csv", RELEVE_CSV),
    "e" * 31 + "2": ("photo.png", "image/png", b"\x89PNG fake"),
}


class Store:
    async def get(self, attachment_id: str):
        name, media, data = FILES[attachment_id]
        return AttachmentInfo(
            id=attachment_id,
            name=name,
            media_type=media,
            kind="document",
            size=len(data),
            meta={},
            created_at=datetime.now(UTC),
        ), data


RESULTS: list[tuple[str, bool, bool]] = []


def check(step: str, out, expect_error: bool, must: str = "") -> None:
    ok = out.is_error == expect_error and must in out.content
    RESULTS.append((step, ok, out.is_error))
    print(
        f"\n── {step} [{'conforme' if ok else 'INATTENDU'}] (erreur={out.is_error})\n{out.content[:500]}"
    )


async def main() -> None:
    env = ToolEnvironment(Settings())
    env.files = ConversationFiles(Store(), list(FILES))
    agents = {c.name: c for c in list_agent_classes()}
    tools, _ = build_tools([*agents["data_analyst"].toolsets, *agents["tresorier"].toolsets], env)
    t = {x.name: x for x in tools}
    ctx = ToolContext()
    ds = httpx.get("http://127.0.0.1:8000/api/datasets").json()
    ventes = next(d["id"] for d in ds if d["name"] == "ventes_2026.csv")

    check(
        "rapport : colonne mal orthographiée (refus local avec suggestion)",
        await t["data_via_create_report"].invoke(
            {
                "dataset_id": ventes,
                "name": "x",
                "visuals": [
                    {
                        "type": "bar",
                        "title": "CA",
                        "category": ["regoin"],
                        "values": [{"field": "chiffre_affaires"}],
                    }
                ],
            },
            ctx,
        ),
        True,
        "region",
    )
    before = len(httpx.get("http://127.0.0.1:8000/api/reports").json())
    check(
        "rapport : secteurs à 2 mesures (refus local)",
        await t["data_via_create_report"].invoke(
            {
                "dataset_id": ventes,
                "name": "x",
                "visuals": [
                    {
                        "type": "pie",
                        "title": "Part",
                        "category": ["region"],
                        "values": [{"field": "chiffre_affaires"}, {"field": "cout"}],
                    }
                ],
            },
            ctx,
        ),
        True,
        "exactement une mesure",
    )
    check(
        "rapport : somme d'une colonne texte (refus local)",
        await t["data_via_create_report"].invoke(
            {
                "dataset_id": ventes,
                "name": "x",
                "visuals": [
                    {"type": "kpi", "title": "Total", "values": [{"field": "region", "agg": "sum"}]}
                ],
            },
            ctx,
        ),
        True,
        "impossible sur « region »",
    )
    after = len(httpx.get("http://127.0.0.1:8000/api/reports").json())
    print(f"   rapports dans Viz : {before} → {after} (aucun créé attendu)")
    RESULTS.append(("aucun rapport créé sur refus", before == after, False))
    check(
        "rapport : jeu inexistant",
        await t["data_via_create_report"].invoke(
            {
                "dataset_id": "inconnu",
                "name": "x",
                "visuals": [{"type": "kpi", "title": "T", "values": [{"field": "a"}]}],
            },
            ctx,
        ),
        True,
        "",
    )
    check(
        "import Viz d'une image",
        await t["data_via_import_file"].invoke({"file": "photo.png"}, ctx),
        True,
        "convert_file",
    )
    check(
        "fichier absent",
        await t["convert_file"].invoke({"file": "budget.xlsx"}, ctx),
        True,
        "releve-octobre.csv",
    )
    check("Convert sur une image", await t["convert_file"].invoke({"file": "photo"}, ctx), True, "")
    check(
        "send_to_tab : projet inconnu",
        await t["convert_send_to_tab"].invoke(
            {"conversion_id": "000000000000", "project_id": "proj_x"}, ctx
        ),
        True,
        "",
    )
    check(
        "aperçu import Trésorerie d'un CSV joint",
        await t["tresorerie_preview_import"].invoke(
            {"account_id": 2, "file": "releve-octobre.csv"}, ctx
        ),
        False,
        "3 à importer",
    )
    check(
        "import Trésorerie du CSV",
        await t["tresorerie_import_statement"].invoke(
            {"account_id": 2, "file": "releve-octobre.csv"}, ctx
        ),
        False,
        "3 opération(s) importée(s)",
    )
    check(
        "opération sur compte inexistant",
        await t["tresorerie_add_transaction"].invoke(
            {"account_id": 999, "date": "2026-10-20", "label": "x", "amount": -1}, ctx
        ),
        True,
        "",
    )
    check(
        "plan dans un projet existant (#1)",
        await t["projets_create_plan"].invoke(
            {
                "project_id": 1,
                "phase": "Suivi",
                "epic": "Ventes",
                "story": "Rapport",
                "tasks": [{"title": "Brancher le rapport Viz"}],
            },
            ctx,
        ),
        False,
        "projet 1",
    )
    check(
        "plan dans un projet inexistant",
        await t["projets_create_plan"].invoke(
            {"project_id": 999, "epic": "x", "story": "y", "tasks": [{"title": "z"}]}, ctx
        ),
        True,
        "Déjà créé : rien",
    )
    await env.aclose()
    bad = [s for s, ok, _ in RESULTS if not ok]
    print("\n═══ INATTENDUS :", bad or "aucun")


asyncio.run(main())
