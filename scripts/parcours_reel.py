"""Parcours réel des outils d'Agent_DatAnalyst contre les applications LANCÉES.

ÉCRIT DE VRAIES DONNÉES (jeu et rapport Viz, conversions, table Tab, projet, compte, opérations,
placements, import de relevé) : à jouer sur des données de démonstration, jamais sur ses comptes.

    cd Atelier_Nexus/backend
    NEXUS_PLUGINS=atelier_datanalyst PYTHONPATH=. python ../../Agent_DatAnalyst/scripts/parcours_reel.py

Fichiers PDF : `CONVERT_FIXTURES` (défaut : ../Atelier_Convert/backend/tests/fixtures).
Projet Tab cible : `TAB_PROJECT_ID` (défaut : le premier projet vu par Convert).
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
import random
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.agents import list_agent_classes  # charge l'extension (NEXUS_PLUGINS)
from app.core.config import Settings
from app.schemas.attachments import AttachmentInfo
from app.tools import ToolContext
from app.tools.environment import ToolEnvironment
from app.tools.files import ConversationFiles
from app.tools.toolsets import build_tools

CONVERT_FIX = Path(
    os.environ.get("CONVERT_FIXTURES", "../../Atelier_Convert/backend/tests/fixtures")
)
FAILS: list[str] = []


def ventes_csv() -> bytes:
    rnd = random.Random(7)
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    w.writerow(["date", "region", "produit", "chiffre_affaires", "cout"])
    d0 = date(2026, 1, 1)
    for _i in range(240):
        d = d0 + timedelta(days=rnd.randrange(270))
        ca = round(rnd.uniform(80, 4000), 2)
        w.writerow(
            [
                d.isoformat(),
                rnd.choice(["Bretagne", "Occitanie", "Île-de-France", "Normandie"]),
                rnd.choice(["Atelier", "Formation", "Conseil"]),
                ca,
                round(ca * rnd.uniform(0.4, 0.8), 2),
            ]
        )
    return out.getvalue().encode()


FILES = {
    "f" * 31 + "1": ("ventes_2026.csv", "text/csv", ventes_csv()),
    "f" * 31 + "2": ("tableau.pdf", "application/pdf", (CONVERT_FIX / "tableau.pdf").read_bytes()),
    "f" * 31 + "3": (
        "releve-octobre.pdf",
        "application/pdf",
        (CONVERT_FIX / "releve-montant-solde.pdf").read_bytes(),
    ),
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


def show(step: str, out) -> None:
    status = "ERREUR" if out.is_error else "ok"
    print(f"\n── {step} [{status}]\n{out.content[:900]}")
    if out.is_error:
        FAILS.append(step)


async def main() -> None:
    names = [c.name for c in list_agent_classes()]
    assert "datanalyst" in names and "tresorier" in names, names
    env = ToolEnvironment(Settings())
    env.files = ConversationFiles(Store(), list(FILES))
    agent = {c.name: c for c in list_agent_classes()}
    tools, unavailable = build_tools(list(agent["datanalyst"].toolsets), env)
    treso, _ = build_tools(list(agent["tresorier"].toolsets), env)
    t = {x.name: x for x in [*tools, *treso]}
    print("Boîtes indisponibles :", [u.label for u in unavailable])
    ctx = ToolContext()
    run = lambda name, args=None: t[name].invoke(args or {}, ctx)  # noqa: E731

    # 1. Fichiers joints
    show("conversation_files", await run("conversation_files"))

    # 2. CSV → Viz → rapport
    show("data_via_list_datasets (avant)", await run("data_via_list_datasets"))
    imp = await run("data_via_import_file", {"file": "ventes_2026.csv"})
    show("data_via_import_file", imp)
    ds = imp.data["dataset_id"] if imp.data else None
    if ds:
        show(
            "data_via_describe_dataset", await run("data_via_describe_dataset", {"dataset_id": ds})
        )
        show(
            "data_via_query",
            await run(
                "data_via_query",
                {
                    "dataset_id": ds,
                    "dimensions": [{"field": "region"}],
                    "measures": [{"field": "chiffre_affaires", "agg": "sum"}],
                },
            ),
        )
        rep = await run(
            "data_via_create_report",
            {
                "dataset_id": ds,
                "name": "Ventes 2026 — synthèse",
                "visuals": [
                    {
                        "type": "kpi",
                        "title": "Chiffre d'affaires total",
                        "values": [{"field": "chiffre_affaires"}],
                    },
                    {
                        "type": "line",
                        "title": "CA par mois",
                        "category": ["date"],
                        "date_bin": "month",
                        "values": [{"field": "chiffre_affaires"}],
                    },
                    {
                        "type": "bar",
                        "title": "CA par région",
                        "category": ["region"],
                        "values": [{"field": "chiffre_affaires"}],
                    },
                    {
                        "type": "column",
                        "title": "CA par produit et région",
                        "category": ["produit"],
                        "series": "region",
                        "values": [{"field": "chiffre_affaires"}],
                    },
                    {
                        "type": "donut",
                        "title": "Part des produits",
                        "category": ["produit"],
                        "values": [{"field": "chiffre_affaires"}],
                    },
                    {
                        "type": "table",
                        "title": "Détail par région",
                        "category": ["region", "produit"],
                        "values": [{"field": "chiffre_affaires"}, {"field": "cout"}],
                    },
                ],
            },
        )
        show("data_via_create_report", rep)
        if rep.data:
            import httpx

            r = httpx.get(f"http://127.0.0.1:8000/api/reports/{rep.data['report_id']}")
            report = r.json()
            print(
                "Rapport relu dans Viz :",
                r.status_code,
                len(report["pages"][0]["visuals"]),
                "visuels",
            )
            # Le moteur des visuels de Viz doit savoir calculer chaque visuel
            for v in report["pages"][0]["visuals"]:
                q = {"dataset_id": ds, "dimensions": [], "measures": []}
                for _role, bindings in v["roles"].items():
                    for b in bindings:
                        if b.get("agg"):
                            q["measures"].append({"field": b["field"], "agg": b["agg"]})
                        else:
                            q["dimensions"].append(
                                {"field": b["field"], **({"bin": b["bin"]} if b.get("bin") else {})}
                            )
                qr = httpx.post("http://127.0.0.1:8000/api/query", json=q)
                print(
                    f"   visuel {v['type']:<6} « {v['title']} » → requête Viz {qr.status_code}"
                    + (
                        f" ({len(qr.json().get('rows', []))} lignes)"
                        if qr.status_code == 200
                        else f" {qr.text[:200]}"
                    )
                )
                if qr.status_code != 200:
                    FAILS.append(f"requête du visuel {v['title']}")

    # 3. PDF → Convert → Tab et Viz
    conv = await run("convert_file", {"file": "tableau.pdf"})
    show("convert_file (tableau.pdf)", conv)
    cid = conv.data["conversion_id"] if conv.data else None
    if cid:
        show(
            "convert_read_table",
            await run("convert_read_table", {"conversion_id": cid, "limit": 20}),
        )
        projects = await run("convert_tab_projects")
        show("convert_tab_projects", projects)
        tab_project = os.environ.get("TAB_PROJECT_ID") or (projects.data or [{}])[0].get("id")
        if tab_project:
            show(
                "convert_send_to_tab",
                await run(
                    "convert_send_to_tab",
                    {
                        "conversion_id": cid,
                        "project_id": tab_project,
                        "table_name": "Tableau du PDF",
                    },
                ),
            )
        sv = await run("convert_send_to_viz", {"conversion_id": cid})
        show("convert_send_to_viz", sv)
        sv2 = await run("convert_send_to_viz", {"conversion_id": cid})
        show("convert_send_to_viz (renvoi : pas de doublon)", sv2)
        again = await run("convert_file", {"file": "tableau.pdf"})
        show("convert_file (2e fois : même conversion)", again)
        if again.data and again.data["conversion_id"] != cid:
            FAILS.append("convert_file non idempotent")

    # 4. Projets : plan complet
    show("projets_list_projects", await run("projets_list_projects"))
    plan = await run(
        "projets_create_plan",
        {
            "project_name": "Tableau de bord ventes 2026",
            "project_description": "Rapport mensuel des ventes, tiré de ventes_2026.csv.",
            "epic": "Analyse des ventes",
            "story": "Rapport mensuel dans Atelier Viz",
            "tasks": [
                {
                    "title": "Nettoyer les données de ventes",
                    "estimated_hours": 2,
                    "priority": "HIGH",
                },
                {
                    "title": "Construire le rapport mensuel",
                    "type": "FEATURE",
                    "estimated_hours": 6,
                    "due_date": "2026-11-15",
                },
                {"title": "Présenter le tableau de bord à l'équipe", "estimated_hours": 1},
            ],
        },
    )
    show("projets_create_plan", plan)
    if plan.data:
        show(
            "projets_structure",
            await run("projets_structure", {"project_id": plan.data["project_id"]}),
        )

    # 5. Trésorerie (outils du Trésorier)
    show("tresorerie_accounts", await run("tresorerie_accounts"))
    acc = await run(
        "tresorerie_create_account",
        {
            "name": "PEA test",
            "type": "investissement",
            "institution": "Courtier",
            "opening_balance": 1000,
        },
    )
    show("tresorerie_create_account", acc)
    pea = acc.data["account_id"] if acc.data else None
    show(
        "tresorerie_add_transaction",
        await run(
            "tresorerie_add_transaction",
            {
                "account_id": 1,
                "date": "2026-10-20",
                "label": "LDLC — écran 27 pouces",
                "amount": -349.9,
            },
        ),
    )
    if pea:
        show(
            "tresorerie_add_holding (or)",
            await run(
                "tresorerie_add_holding",
                {
                    "account_id": pea,
                    "name": "Lingot or 100 g",
                    "asset_type": "metal",
                    "metal": "or",
                    "unit": "piece",
                    "weight_g": 100,
                    "purity": 999.9,
                    "quantity": 2,
                    "cost_basis": 14200,
                },
            ),
        )
        show(
            "tresorerie_add_holding (ETF)",
            await run(
                "tresorerie_add_holding",
                {
                    "account_id": pea,
                    "name": "MSCI World",
                    "asset_type": "etf",
                    "isin": "IE00BK5BQT80",
                    "quantity": 10,
                    "cost_basis": 950,
                    "price": 101.2,
                },
            ),
        )
    rel = await run("convert_file", {"file": "releve-octobre.pdf"})
    show("convert_file (relevé)", rel)
    if rel.data:
        args = {"account_id": 1, "conversion_id": rel.data["conversion_id"]}
        show("tresorerie_preview_import", await run("tresorerie_preview_import", args))
        show("tresorerie_import_statement", await run("tresorerie_import_statement", args))
        show(
            "tresorerie_import_statement (2e fois)", await run("tresorerie_import_statement", args)
        )
    show("tresorerie_accounts (après)", await run("tresorerie_accounts"))
    await env.aclose()
    print("\n═══ ÉCHECS :", FAILS or "aucun")
    sys.exit(1 if FAILS else 0)


asyncio.run(main())
