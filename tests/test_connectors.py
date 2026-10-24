"""Connecteurs de l'extension sur faux serveurs fidèles aux contrats des applications.

Routes, corps et en-têtes lus dans les dépôts : Convert (`routes/atelier.py`, `conversions.py`),
Viz (`routes/datasets.py`, `ai.py` → `visuals/repair`, `reports.py`, `atelier.py`), Projets
(`routes/projects.ts`, `hierarchy.ts`, `work-items.ts`), Trésorerie (`api.py`, `importer.py`).
Charges utiles de Projets et Trésorerie : réponses réelles enregistrées dans les tests de Nexus.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from app.schemas.attachments import AttachmentInfo
from app.tools import ToolContext
from app.tools.data_via import DataViaClient
from app.tools.files import ConversationFiles

from atelier_datanalyst.convert import ConvertClient, convert_read_tools, convert_write_tools
from atelier_datanalyst.files import ConversationFilesTool
from atelier_datanalyst.projets import PlanClient, projets_plan_tools
from atelier_datanalyst.tresorerie import TresoWriteClient, tresorerie_write_tools
from atelier_datanalyst.viz import VizWriteClient, viz_write_tools
from tests.conftest import NEXUS_BACKEND
from tests.fake_data_via import DATASET_ID, PROFILE, FakeDataVia

FIX = Path(str(NEXUS_BACKEND)) / "tests" / "fixtures"


def fixture(app: str, name: str) -> Any:
    return json.loads((FIX / app / f"{name}.json").read_text())


# ═══ Fichiers joints ══════════════════════════════════════════════


class FakeStore:
    def __init__(self, files: dict[str, tuple[str, str, bytes]]) -> None:
        self.files = files
        self.asked: list[str] = []

    async def get(self, attachment_id: str) -> tuple[AttachmentInfo, bytes] | None:
        self.asked.append(attachment_id)
        if attachment_id not in self.files:
            return None
        name, media_type, data = self.files[attachment_id]
        info = AttachmentInfo(
            id=attachment_id,
            name=name,
            media_type=media_type,
            kind="document",
            size=len(data),
            meta={"rows": 3} if name.endswith(".csv") else {},
            created_at=datetime(2026, 10, 23, tzinfo=UTC),
        )
        return info, data


CSV = b"date;region;chiffre_affaires\n2026-01-02;Bretagne;120\n"
PDF = b"%PDF-1.7 releve"
STORE = {
    "a" * 32: ("ventes_2026.csv", "text/csv", CSV),
    "b" * 32: ("Relevé octobre.pdf", "application/pdf", PDF),
    "c" * 32: ("secret.csv", "text/csv", b"x"),  # autre conversation : jamais citée
}


def files(*ids: str) -> ConversationFiles:
    return ConversationFiles(FakeStore(STORE), list(ids or ("a" * 32, "b" * 32)))


async def test_files_tool_lists_and_explains_when_empty() -> None:
    out = await ConversationFilesTool(files).invoke({}, ToolContext())
    assert "2 fichier(s)" in out.content and "ventes_2026.csv" in out.content
    assert CSV.decode() not in out.content  # le contenu ne part jamais vers le modèle
    empty = await ConversationFilesTool(lambda: ConversationFiles(None, [])).invoke(
        {}, ToolContext()
    )
    assert "Aucun fichier" in empty.content


# ═══ Atelier Convert ══════════════════════════════════════════════

CONV_ID = "0123456789ab"  # relevé bancaire reconnu (`statement`)
SALES_ID = "fedcba987654"  # tableau ordinaire
TABLE = {
    "format": "atelier-table/1",
    "name": "releve_octobre",
    "label": "Relevé octobre",
    "columns": [
        {"key": "date", "label": "Date", "type": "date"},
        {"key": "libelle", "label": "Libellé", "type": "string"},
        {"key": "montant", "label": "Montant", "type": "number"},
    ],
    "rows": [
        {"date": "2026-10-01", "libelle": f"Opération {i}", "montant": -10.0 - i} for i in range(3)
    ],
    "total": 3,
    "truncated": False,
}


class FakeConvert:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, method = request.url.path.removeprefix("/api"), request.method
        if (method, path) == ("GET", "/health"):
            return httpx.Response(
                200, json={"status": "ok", "app": "atelier-convert", "version": "1.4.0"}
            )
        if (method, path) == ("GET", "/conversions"):
            return httpx.Response(
                200,
                json=[
                    {
                        "id": CONV_ID, "name": "Relevé octobre", "created_at": "2026-10-23T08:00:00Z",
                        "updated_at": "2026-10-23T08:00:00Z",
                        "source": {"filename": "releve.pdf", "kind": "pdf", "size": 1200, "sha256": "x"},
                        "detected": "Relevé bancaire · 3 opérations", "rows": 3, "columns": 3,
                        "statement": {"transactions": 3, "start": "2026-10-01", "end": "2026-10-03",
                                      "total_debit": 33.0, "total_credit": 0.0, "balance_check": "ok"},
                    },
                    {
                        "id": SALES_ID, "name": "Ventes 2026", "created_at": "2026-10-23T08:00:00Z",
                        "updated_at": "2026-10-23T08:00:00Z",
                        "source": {"filename": "ventes.xlsx", "kind": "xlsx", "size": 900, "sha256": "y"},
                        "detected": "Tableau · 3 lignes", "rows": 3, "columns": 3, "statement": None,
                    },
                ],
            )  # fmt: skip
        if (method, path) == ("POST", "/atelier/conversions"):
            body = json.loads(request.content)
            assert base64.b64decode(body["content_base64"]) == PDF
            return httpx.Response(
                201,
                json={"conversion_id": CONV_ID, "name": "Relevé octobre", "rows": 3, "columns": 3,
                      "detected": "Relevé bancaire · 3 opérations", "warnings": []},
            )  # fmt: skip
        if method == "GET" and path in (
            f"/atelier/conversions/{CONV_ID}/table",
            f"/atelier/conversions/{SALES_ID}/table",
        ):
            return httpx.Response(200, json=TABLE)
        if (method, path) == ("GET", "/tab"):
            return httpx.Response(
                200,
                json={"found": True, "directory": "/Users/x/Atelier_Tab",
                      "projects": [{"id": "proj_ab12", "name": "Ventes", "workspace": "Pro", "tables": 2}]},
            )  # fmt: skip
        if (method, path) == ("POST", f"/atelier/conversions/{CONV_ID}/send-to-tab"):
            return httpx.Response(
                200,
                json={"table_id": "tbl_1", "table_name": "Relevé", "project_id": "proj_ab12",
                      "project_name": "Ventes", "rows": 3, "columns": 3, "file": "data/x.json"},
            )  # fmt: skip
        if (method, path) == ("GET", f"/conversions/{CONV_ID}/export"):
            assert request.url.params["format"] == "xlsx"
            return httpx.Response(200, content=b"PK-xlsx")
        return httpx.Response(404, json={"detail": "Conversion introuvable.", "code": "NOT_FOUND"})


@pytest.fixture
def convert() -> tuple[ConvertClient, FakeConvert]:
    fake = FakeConvert()
    return ConvertClient("http://convert.test/api", transport=httpx.MockTransport(fake)), fake


async def test_convert_read_tools(convert: tuple[ConvertClient, FakeConvert]) -> None:
    client, _ = convert
    tools = {t.name: t for t in convert_read_tools(client)}
    assert not any(t.requires_approval for t in tools.values())
    ctx = ToolContext()
    listing = await tools["convert_list_conversions"].invoke({}, ctx)
    assert CONV_ID in listing.content and "Relevé bancaire" in listing.content
    table = await tools["convert_read_table"].invoke({"conversion_id": SALES_ID}, ctx)
    assert (
        "3 ligne(s)" in table.content
        and table.artifacts
        and table.data["format"] == "atelier-table/1"
    )
    projects = await tools["convert_tab_projects"].invoke({}, ctx)
    assert "proj_ab12" in projects.content and "/Users/x" not in projects.content
    bad = await tools["convert_read_table"].invoke({"conversion_id": "../etc"}, ctx)
    assert bad.is_error  # identifiant refusé avant tout appel


async def test_convert_writes_need_approval_and_are_idempotent(
    convert: tuple[ConvertClient, FakeConvert],
) -> None:
    client, fake = convert
    viz = FakeVizWrites()
    writer = VizWriteClient("http://viz.test/api", transport=viz.transport)
    tools = {t.name: t for t in convert_write_tools(client, files, lambda: writer)}
    assert all(t.requires_approval for t in tools.values())
    assert tools["convert_file"].summarize({"file": "Relevé octobre.pdf"}) == (
        "Convertir le fichier « Relevé octobre.pdf » en table dans Atelier Convert"
    )
    out = await tools["convert_file"].invoke({"file": "relevé octobre.pdf"}, ToolContext())
    assert not out.is_error and CONV_ID in out.content and "agent Trésorier" in out.content
    key = fake.requests[-1].headers["Idempotency-Key"]
    await tools["convert_file"].invoke({"file": "b" * 32}, ToolContext())
    assert fake.requests[-1].headers["Idempotency-Key"] == key  # même fichier = même clé

    sent = await tools["convert_send_to_tab"].invoke(
        {"conversion_id": CONV_ID, "project_id": "proj_ab12", "table_name": "Relevé"}, ToolContext()
    )
    assert "créée dans le projet « Ventes »" in sent.content
    assert json.loads(fake.requests[-1].content) == {
        "project_id": "proj_ab12",
        "table_name": "Relevé",
    }

    to_viz = await tools["convert_send_to_viz"].invoke({"conversion_id": CONV_ID}, ToolContext())
    assert not to_viz.is_error and "créé dans Atelier Viz" in to_viz.content
    upsert = json.loads(viz.requests[-1].content)
    assert upsert["key"] == f"convert:{CONV_ID}" and len(upsert["table"]["rows"]) == 3
    assert viz.requests[-1].headers["Idempotency-Key"].startswith("nexus:")


async def test_convert_send_to_viz_without_viz_is_a_clear_error(
    convert: tuple[ConvertClient, FakeConvert],
) -> None:
    from app.tools.http import AppNotConfiguredError

    def no_viz() -> VizWriteClient:
        raise AppNotConfiguredError("DATA_VIA_BASE_URL n'est pas défini.", app="viz")

    client, _ = convert
    tool = next(
        t for t in convert_write_tools(client, files, no_viz) if t.name == "convert_send_to_viz"
    )
    out = await tool.invoke({"conversion_id": CONV_ID}, ToolContext())
    assert out.is_error and "DATA_VIA_BASE_URL" in out.content


async def test_convert_health_checks_identity() -> None:
    def other(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "ok", "app": "atelier-viz"})

    client = ConvertClient("http://convert.test/api", transport=httpx.MockTransport(other))
    with pytest.raises(Exception, match="n'est pas Atelier Convert"):
        await client.health()


# ═══ Atelier Viz : import et rapport ══════════════════════════════


class FakeVizWrites(FakeDataVia):
    """Faux Viz : lecture (FakeDataVia) + écritures d'Agent_DatAnalyst."""

    def __init__(self) -> None:
        super().__init__()
        self.saved: dict[str, Any] | None = None
        self.alter = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path.removeprefix("/api"), request.method
        if (method, path) == ("POST", "/datasets"):
            self.requests.append(request)
            assert b'filename="ventes_2026.csv"' in request.content and CSV in request.content
            return httpx.Response(201, json=PROFILE)
        if (method, path) == ("POST", "/atelier/datasets"):
            self.requests.append(request)
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={"dataset_id": "ds_releve", "name": body["name"],
                      "rows": len(body["table"]["rows"]), "action": "created"},
            )  # fmt: skip
        if (method, path) == ("POST", "/ai/visuals/repair"):
            # Comme le vrai `repair_items` de Viz : il CORRIGE sans refuser (mesure en trop
            # tronquée, agrégation remplacée…) ; `self.alter` simule une correction silencieuse.
            self.requests.append(request)
            body = json.loads(request.content)
            assert {c["type"] for c in body["catalog"]} >= {"column", "kpi", "table"}
            results = []
            for v in body["visuals"]:
                roles = {
                    k: [{"agg": None, "bin": None, **b} for b in items]
                    for k, items in v["roles"].items()
                }
                if self.alter:
                    roles["values"] = roles["values"][:1]
                results.append(
                    {"visual": {**v, "roles": roles, "options": {"labels": True}}, "warnings": []}
                )
            return httpx.Response(200, json={"results": results})
        if (method, path) == ("POST", "/reports"):
            self.requests.append(request)
            body = json.loads(request.content)
            return httpx.Response(201, json={"id": "rep_1", "name": body["name"], "pages": []})
        if (method, path) == ("PUT", "/reports/rep_1"):
            self.requests.append(request)
            self.saved = json.loads(request.content)
            return httpx.Response(200, json={"id": "rep_1", **self.saved})
        return super().handler(request)


@pytest.fixture
def viz() -> tuple[dict[str, Any], FakeVizWrites]:
    """Outils d'écriture Viz : écritures par VizWriteClient, schéma relu par le client Data Via."""
    fake = FakeVizWrites()
    writer = VizWriteClient("http://viz.test/api", transport=fake.transport)
    reader = DataViaClient("http://viz.test/api", transport=fake.transport)
    return {t.name: t for t in viz_write_tools(writer, files, lambda: reader)}, fake


async def test_viz_import_file(viz: tuple[dict[str, Any], FakeVizWrites]) -> None:
    tools, _ = viz
    assert tools["data_via_import_file"].requires_approval
    out = await tools["data_via_import_file"].invoke({"file": "ventes_2026.csv"}, ToolContext())
    assert not out.is_error and "Jeu de données créé" in out.content
    assert "chiffre_affaires" in out.content and out.data["dataset_id"] == DATASET_ID
    pdf = await tools["data_via_import_file"].invoke({"file": "Relevé octobre.pdf"}, ToolContext())
    assert pdf.is_error and "convert_file" in pdf.content


REPORT = {
    "dataset_id": DATASET_ID,
    "name": "Ventes 2026",
    "visuals": [
        {"type": "kpi", "title": "Chiffre d'affaires total", "values": [{"field": "chiffre_affaires"}]},
        {"type": "line", "title": "CA par mois", "category": ["date"], "date_bin": "month",
         "values": [{"field": "chiffre_affaires", "agg": "sum"}]},
        {"type": "bar", "title": "CA par région", "category": ["region"], "series": None,
         "values": [{"field": "chiffre_affaires"}]},
    ],
}  # fmt: skip


async def test_viz_create_report(viz: tuple[dict[str, Any], FakeVizWrites]) -> None:
    tools, fake = viz
    report = tools["data_via_create_report"]
    assert report.requires_approval
    assert report.summarize(REPORT) == (
        "Créer dans Atelier Viz le rapport « Ventes 2026 » (3 visuel(s)) — Carte KPI « Chiffre "
        "d'affaires total » : somme de chiffre_affaires ; Courbes « CA par mois » : somme de "
        "chiffre_affaires par date (mois) ; Barres horizontales « CA par région » : somme de "
        "chiffre_affaires par region"
    )
    out = await report.invoke(REPORT, ToolContext())
    assert not out.is_error and "rep_1" in out.content
    # schéma relu au moment d'écrire, avant la validation par Viz
    paths = [r.url.path for r in fake.requests]
    assert paths.index(f"/api/datasets/{DATASET_ID}") < paths.index("/api/ai/visuals/repair")
    assert fake.saved is not None
    page = fake.saved["pages"][0]
    assert page["size"] == {"width": 1280, "height": 720}
    kpi, line, bar = page["visuals"]
    assert kpi["roles"] == {"values": [{"field": "chiffre_affaires", "agg": "sum"}]}
    assert line["roles"]["category"] == [{"field": "date", "bin": "month"}]
    assert bar["layout"] == {"x": 20, "y": 370, "w": 610, "h": 330}  # 2 colonnes
    assert bar["options"]["sort"] == "value_desc" and bar["options"]["labels"] is True
    assert all(v["dataset_id"] == DATASET_ID for v in page["visuals"])


async def test_viz_report_refused_before_writing(viz: tuple[dict[str, Any], FakeVizWrites]) -> None:
    tools, fake = viz
    create = tools["data_via_create_report"]

    async def refused(visual: dict[str, Any], expected: str) -> None:
        out = await create.invoke({**REPORT, "visuals": [visual]}, ToolContext())
        assert out.is_error and expected in out.content, out.content

    # contrôles locaux (règles de Viz), avec suggestion
    await refused({"type": "bar", "title": "x", "category": ["regoin"],
                   "values": [{"field": "chiffre_affaires"}]}, "Vouliez-vous dire : region")  # fmt: skip
    await refused({"type": "pie", "title": "Part", "category": ["region"],
                   "values": [{"field": "chiffre_affaires"}, {"field": "cout"}]}, "exactement une mesure")  # fmt: skip
    await refused({"type": "kpi", "title": "Total", "values": [{"field": "region", "agg": "sum"}]},
                  "« sum » impossible sur « region »")  # fmt: skip
    await refused({"type": "line", "title": "x", "values": [{"field": "cout"}]}, "axe")
    await refused({"type": "pie", "title": "x", "category": ["region"], "series": "region",
                   "values": [{"field": "cout"}]}, "légende")  # fmt: skip
    assert not any(r.url.path.startswith("/api/ai") for r in fake.requests)  # Viz pas appelé

    # Viz corrigerait en silence ce que l'utilisateur a validé : rien n'est écrit
    fake.alter = True
    await refused({"type": "column", "title": "CA et coût", "category": ["region"],
                   "values": [{"field": "chiffre_affaires"}, {"field": "cout"}]}, "modifierait")  # fmt: skip
    assert not any(r.url.path.endswith("/reports") for r in fake.requests)


# ═══ Atelier Projets : plan de travail ════════════════════════════


class FakeProjetsPlan:
    def __init__(self, fail_on_task: str | None = None) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_on_task = fail_on_task

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api")
        body = json.loads(request.content)
        self.calls.append((path, body))
        if path == "/projects":
            return httpx.Response(
                201, json=fixture("projets", "projects")[0] | {"id": 7, "name": body["name"]}
            )
        if path.endswith("/work-items"):
            if body["title"] == self.fail_on_task:
                return httpx.Response(400, json={"message": "Titre refusé"})
            detail = fixture("projets", "work_item_12345") | {
                "id": 300 + len(self.calls), "title": body["title"], "type": body["type"], "status": "OPEN",
            }  # fmt: skip
            return httpx.Response(201, json=detail)
        return httpx.Response(201, json={"id": 40 + len(self.calls), "title": body["title"]})


PLAN = {
    "project_name": "Analyse ventes 2026",
    "epic": "Tableau de bord",
    "story": "Rapport mensuel",
    "tasks": [
        {"title": "Nettoyer les données", "estimated_hours": 2},
        {"title": "Construire le rapport", "type": "FEATURE", "due_date": "2026-11-15"},
    ],
}


async def test_projets_create_plan_builds_the_whole_hierarchy() -> None:
    fake = FakeProjetsPlan()
    client = PlanClient("http://projets.test/api", transport=httpx.MockTransport(fake))
    (tool,) = projets_plan_tools(client)
    assert tool.requires_approval
    assert "« Nettoyer les données », « Construire le rapport »" in tool.summarize(PLAN)
    out = await tool.invoke(PLAN, ToolContext())
    assert not out.is_error, out.content
    assert [p for p, _ in fake.calls] == [
        "/projects", "/projects/7/phases", "/phases/42/epics", "/epics/43/stories",
        "/stories/44/work-items", "/stories/44/work-items",
    ]  # fmt: skip
    assert fake.calls[1][1] == {"title": "Plan d'action"}
    assert fake.calls[4][1] == {
        "type": "TASK",
        "title": "Nettoyer les données",
        "estimatedHours": 2,
    }
    assert out.data["story_id"] == 44 and len(out.data["work_items"]) == 2


async def test_projets_create_plan_reports_partial_failure_and_validates_input() -> None:
    fake = FakeProjetsPlan(fail_on_task="Construire le rapport")
    client = PlanClient("http://projets.test/api", transport=httpx.MockTransport(fake))
    (tool,) = projets_plan_tools(client)
    out = await tool.invoke(PLAN, ToolContext())
    assert out.is_error and "Déjà créé : projet #7" in out.content and "TASK #" in out.content
    both = await tool.invoke({**PLAN, "project_id": 1}, ToolContext())
    assert both.is_error and "soit project_id" in both.content


# ═══ Atelier Trésorerie : saisies (sensibles) ═════════════════════


class FakeTresoWrites:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("X-Requested-With") is None:
            return httpx.Response(403, json={"error": "En-tête X-Requested-With manquant."})
        path = request.url.path.removeprefix("/api")
        if path == "/accounts":
            body = json.loads(request.content)
            account = fixture("tresorerie", "accounts")["items"][0]
            return httpx.Response(
                201, json=account | {"id": 9, "name": body["name"], "balance_cents": 50000}
            )
        if path == "/transactions":
            body = json.loads(request.content)
            tx = fixture("tresorerie", "transactions")["items"][0]
            cents = round(float(body["amount"]) * 100)
            return httpx.Response(
                201, json=tx | {"id": 501, "label": body["label"], "amount_cents": cents}
            )
        if path == "/holdings":
            body = json.loads(request.content)
            return httpx.Response(
                201,
                json={"id": 12, "name": body["name"], "asset_type": body["asset_type"],
                      "quantity": body["quantity"], "value_cents": 1420000, "cost_basis_cents": 1420000},
            )  # fmt: skip
        if path == "/import/preview":
            assert b'name="account_id"' in request.content and b"PK-xlsx" in request.content
            rows = [
                {"line": 2, "status": "ok", "date": "2026-10-01", "label": "Loyer",
                 "amount_cents": -80000, "category_id": 2, "category_name": "Logement", "import_hash": "a" * 64},
                {"line": 3, "status": "duplicate", "date": "2026-10-02", "label": "Courses",
                 "amount_cents": -4200, "import_hash": "b" * 64},
                {"line": 4, "status": "error", "error": "Montant vide ou nul"},
            ]  # fmt: skip
            return httpx.Response(
                200,
                json={"filename": "x.xlsx", "account_id": 1, "headers": ["Date", "Libellé", "Montant"],
                      "mapping_errors": None, "rows": rows,
                      "summary": {"total": 3, "ok": 1, "duplicates": 1, "errors": 1}},
            )  # fmt: skip
        if path == "/import/commit":
            body = json.loads(request.content)
            return httpx.Response(201, json={"inserted": len(body["rows"]), "skipped": 0})
        return httpx.Response(404, json={"error": "Ressource introuvable"})


@pytest.fixture
def treso_writes(
    convert: tuple[ConvertClient, FakeConvert],
) -> tuple[dict[str, Any], FakeTresoWrites]:
    fake = FakeTresoWrites()
    client = TresoWriteClient("http://treso.test/api", transport=httpx.MockTransport(fake))
    tools = tresorerie_write_tools(client, files, lambda: convert[0])
    return {t.name: t for t in tools}, fake


async def test_tresorerie_writes_are_sensitive_validated_and_csrf_safe(
    treso_writes: tuple[dict[str, Any], FakeTresoWrites],
) -> None:
    tools, fake = treso_writes
    assert all(t.sensitive for t in tools.values())
    assert {n for n, t in tools.items() if not t.requires_approval} == {"tresorerie_preview_import"}
    ctx = ToolContext()
    acc = await tools["tresorerie_create_account"].invoke(
        {
            "name": "PEA",
            "type": "investissement",
            "institution": "Boursorama",
            "opening_balance": 500,
        },
        ctx,
    )
    assert "Compte #9 « PEA »" in acc.content
    assert json.loads(fake.requests[-1].content) == {
        "name": "PEA", "type": "investissement", "institution": "Boursorama", "opening_balance": "500.00",
    }  # fmt: skip
    buy = {
        "account_id": 1,
        "date": "2026-10-20",
        "label": "LDLC — écran 27 pouces",
        "amount": -349.9,
    }
    assert tools["tresorerie_add_transaction"].summarize(buy) == (
        "Saisir « LDLC — écran 27 pouces » le 2026-10-20 : -349.90 € sur le compte #1"
    )
    tx = await tools["tresorerie_add_transaction"].invoke(buy, ctx)
    assert (
        "-349.90 €" in tx.content and json.loads(fake.requests[-1].content)["amount"] == "-349.90"
    )
    gold = {"account_id": 9, "name": "Lingot 100 g", "asset_type": "metal", "metal": "or",
            "unit": "piece", "weight_g": 100, "purity": 999.9, "quantity": 2, "cost_basis": 14200}  # fmt: skip
    held = await tools["tresorerie_add_holding"].invoke(gold, ctx)
    assert not held.is_error and json.loads(fake.requests[-1].content)["cost_basis"] == "14200.00"
    no_metal = await tools["tresorerie_add_holding"].invoke({**gold, "metal": None}, ctx)
    assert no_metal.is_error
    assert all(r.headers["X-Requested-With"] == "atelier-nexus" for r in fake.requests)


async def test_tresorerie_import_from_a_conversion_commits_only_valid_rows(
    treso_writes: tuple[dict[str, Any], FakeTresoWrites],
    convert: tuple[ConvertClient, FakeConvert],
) -> None:
    tools, fake = treso_writes
    args = {"account_id": 1, "conversion_id": CONV_ID}
    preview = await tools["tresorerie_preview_import"].invoke(args, ToolContext())
    assert "1 à importer, 1 doublon(s) ignoré(s), 1 en erreur" in preview.content
    assert "ligne 4 : Montant vide ou nul" in preview.content
    assert not any(r.url.path.endswith("/commit") for r in fake.requests)  # l'aperçu n'écrit rien
    done = await tools["tresorerie_import_statement"].invoke(args, ToolContext())
    assert "1 opération(s) importée(s)" in done.content
    commit = json.loads(fake.requests[-1].content)
    assert commit["account_id"] == 1 and [r["label"] for r in commit["rows"]] == ["Loyer"]
    pdf = await tools["tresorerie_preview_import"].invoke(
        {"account_id": 1, "file": "Relevé octobre.pdf"}, ToolContext()
    )
    assert pdf.is_error and "convert_file" in pdf.content
    neither = await tools["tresorerie_preview_import"].invoke({"account_id": 1}, ToolContext())
    assert neither.is_error


async def test_statement_rows_never_reach_a_model_that_may_be_claude(
    convert: tuple[ConvertClient, FakeConvert],
) -> None:
    """Boîte `convert` (Data Analyst, Claude possible) : un relevé bancaire n'est pas lu, sans
    même demander sa table ; boîte sensible `convert_releves` (Trésorier, local) : il l'est."""
    client, fake = convert
    ctx = ToolContext()
    tools = {t.name: t for t in convert_read_tools(client)}
    refused = await tools["convert_read_table"].invoke({"conversion_id": CONV_ID}, ctx)
    assert "relevé bancaire" in refused.content and "Trésorier" in refused.content
    assert not refused.artifacts and "Opération" not in refused.content
    assert not [r for r in fake.requests if r.url.path.endswith(f"{CONV_ID}/table")]
    assert not tools["convert_read_table"].sensitive
    local = {t.name: t for t in convert_read_tools(client, statements=True)}
    assert local["convert_read_table"].sensitive
    table = await local["convert_read_table"].invoke({"conversion_id": CONV_ID}, ctx)
    assert "Opération 0" in table.content
