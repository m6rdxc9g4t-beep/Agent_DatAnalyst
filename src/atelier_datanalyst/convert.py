"""Connecteur Atelier Convert (extension Agent_DatAnalyst) : PDF, relevés bancaires, Excel, CSV, OFX/QIF → tables typées.

Contrat : nœud `atelier-node/1` d'Atelier Convert (docs/SUITE.md §7, port 8010) et sa route
d'historique `GET /api/conversions`. Rien d'autre n'est appelé.

- Lecture : historique des conversions, lignes d'une conversion, projets d'Atelier Tab.
- Écriture (validation requise) : convertir un fichier joint, envoyer une conversion dans
  Atelier Tab (nouvelle table), ou dans Atelier Viz (jeu de données, mis à jour au renvoi).

Les envois portent un `Idempotency-Key` stable : rejouer une étape validée ne crée pas de doublon.
Un relevé bancaire reste une donnée financière : l'agent Data Analyst oriente vers l'agent
Trésorier (modèle local) pour l'analyser.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable
from typing import Any, ClassVar, Literal

import httpx
from app.core.net import is_loopback
from app.tools.base import Artifact, BaseTool, ToolContext, ToolInput, ToolOutput
from app.tools.data_via import DataViaError, markdown_table
from app.tools.formatting import atelier_table
from app.tools.http import AppClient, AppError, AppNotConfiguredError, AppRequestError
from pydantic import BaseModel, ConfigDict, Field

from atelier_datanalyst.files import FileGetter, file_field, human_size
from atelier_datanalyst.settings import DatAnalystSettings
from atelier_datanalyst.viz import VizWriteClient

# ═══ Schémas (miroir de backend/app/schemas/convert.py et atelier.py d'Atelier Convert) ═══

#: Identifiant d'une conversion : 12 caractères hexadécimaux
CONVERSION_ID_PATTERN = r"^[0-9a-f]{12}$"
#: Plafond d'une page de table `atelier-table/1` côté Convert
TABLE_MAX_LIMIT = 5000


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ConvertHealth(_Response):
    status: str
    app: str
    version: str | None = None


class SourceInfo(_Response):
    filename: str
    kind: str
    size: int
    pages: int | None = None


class StatementSummary(_Response):
    """Relevé bancaire reconnu (colonnes normalisées)."""

    transactions: int
    start: str | None = None
    end: str | None = None
    total_debit: float = 0.0
    total_credit: float = 0.0
    balance_check: Literal["ok", "mismatch", "unknown"] = "unknown"


class TabSend(_Response):
    at: str
    table_id: str
    table_name: str
    project_id: str
    project_name: str
    rows: int


class ConversionSummary(_Response):
    id: str
    name: str
    created_at: str
    updated_at: str
    source: SourceInfo
    detected: str
    rows: int
    columns: int
    statement: StatementSummary | None = None
    sent: list[TabSend] = Field(default_factory=list)


class ConvertFileResult(_Response):
    conversion_id: str
    name: str
    detected: str
    rows: int
    columns: int
    warnings: list[str] = Field(default_factory=list)


class SendToTabResult(_Response):
    table_id: str
    table_name: str
    project_id: str
    project_name: str
    rows: int
    columns: int


AtelierType = Literal["string", "number", "date", "datetime", "boolean"]


class AtelierColumn(_Response):
    key: str
    label: str
    type: AtelierType


class AtelierTable(_Response):
    """Table d'échange `atelier-table/1` (docs/SUITE.md §7.2)."""

    format: Literal["atelier-table/1"] = "atelier-table/1"
    name: str
    label: str | None = None
    columns: list[AtelierColumn]
    rows: list[dict[str, Any]] = Field(default_factory=list)
    total: int | None = None
    truncated: bool = False


class TabProject(_Response):
    id: str
    name: str
    workspace: str
    tables: int


class TabStatus(_Response):
    """Dossier de données d'Atelier Tab vu par Convert (le chemin n'est jamais montré au modèle)."""

    found: bool
    projects: list[TabProject] = Field(default_factory=list)
    detail: str | None = None


MAX_ROWS_FOR_MODEL = 50
#: Plafond d'un envoi vers Atelier Viz (pages de 5 000 lignes lues chez Convert)
MAX_ROWS_TO_VIZ = 50_000


class ConvertClient(AppClient):
    app = "convert"
    label = "Atelier Convert"
    error_keys = ("detail", "message", "error")

    @classmethod
    def from_settings(
        cls,
        settings: DatAnalystSettings,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        base_url: str | None | Literal["extension"] = "extension",
    ) -> ConvertClient | None:
        """`base_url` : adresse lue dans les réglages de Nexus (`convert_url`), ou « extension »
        pour celle de l'extension. Adresse de bouclage seulement : relevés et fichiers ne quittent
        jamais la machine (comme le nœud de Nexus, docs/SUITE.md §5.1)."""
        url = settings.convert_base_url if base_url == "extension" else base_url
        if not url:
            return None
        if not is_loopback(url):
            # « Non configurée » : la boîte est écartée avec la raison, l'agent reste utilisable
            raise AppNotConfiguredError(
                f"Atelier Convert doit être joint à une adresse locale (127.0.0.1 ou localhost), "
                f"pas {url} : les relevés et les fichiers ne quittent pas la machine.",
                app=cls.app,
            )
        # Conversion d'un PDF scanné (OCR) : plus long qu'une lecture
        return cls(url, timeout=settings.convert_timeout, transport=transport)

    async def health(self) -> ConvertHealth:
        health = self.parse(ConvertHealth, await self.request("GET", "health"))
        if health.app != "atelier-convert":
            raise AppRequestError(
                f"Le service sur {self.base_url} n'est pas Atelier Convert ({health.app}).",
                app=self.app,
            )
        return health

    async def conversions(self) -> list[ConversionSummary]:
        return self.parse_list(ConversionSummary, await self.request("GET", "conversions"))

    async def table(
        self, conversion_id: str, *, offset: int = 0, limit: int = 1000
    ) -> AtelierTable:
        return self.parse(
            AtelierTable,
            await self.request(
                "GET",
                f"atelier/conversions/{conversion_id}/table",
                params={"offset": offset, "limit": limit},
            ),
        )

    async def tab_status(self) -> TabStatus:
        return self.parse(TabStatus, await self.request("GET", "tab"))

    async def convert(
        self, filename: str, content: bytes, *, idempotency_key: str
    ) -> ConvertFileResult:
        body = {"filename": filename, "content_base64": base64.b64encode(content).decode()}
        return self.parse(
            ConvertFileResult,
            await self.request(
                "POST",
                "atelier/conversions",
                json_body=body,
                headers={"Idempotency-Key": idempotency_key},
            ),
        )

    async def send_to_tab(
        self, conversion_id: str, project_id: str, table_name: str | None, *, idempotency_key: str
    ) -> SendToTabResult:
        return self.parse(
            SendToTabResult,
            await self.request(
                "POST",
                f"atelier/conversions/{conversion_id}/send-to-tab",
                json_body={"project_id": project_id, "table_name": table_name},
                headers={"Idempotency-Key": idempotency_key},
            ),
        )

    async def export(self, conversion_id: str, fmt: str = "xlsx") -> bytes:
        """Fichier exporté (`xlsx` : valeurs typées, sans ambiguïté de séparateur décimal)."""
        return await self.download(f"conversions/{conversion_id}/export", params={"format": fmt})


def idempotency_key(*parts: str) -> str:
    """Clé stable (une même étape rejouée = un seul effet), sans donnée lisible."""
    return "nexus:" + hashlib.sha256("|".join(parts).encode()).hexdigest()[:40]


class _ConvertTool[InputT: ToolInput](BaseTool[InputT]):
    expected_errors: ClassVar[tuple[type[Exception], ...]] = (AppError, DataViaError)

    def __init__(self, client: ConvertClient) -> None:
        self.client = client


# ═══ Lecture ═════════════════════════════════════════════════════


class NoInput(ToolInput):
    pass


class ConversionInput(ToolInput):
    conversion_id: str = Field(
        pattern=CONVERSION_ID_PATTERN,
        description="Identifiant de la conversion (12 caractères hexadécimaux).",
    )


class ListConversionsTool(_ConvertTool[NoInput]):
    name = "convert_list_conversions"
    description = "Historique d'Atelier Convert : conversions récentes (fichier, ce qui a été reconnu, lignes)."
    input_model = NoInput
    output_format = "json"

    async def run(self, params: NoInput, ctx: ToolContext) -> ToolOutput:
        items = await self.client.conversions()
        if not items:
            return ToolOutput(content="Aucune conversion dans Atelier Convert.", data=[])
        rows = [
            [c.id, c.name, c.detected, c.rows, c.columns, c.updated_at[:10]] for c in items[:30]
        ]
        table = markdown_table(["id", "nom", "reconnu", "lignes", "colonnes", "modifiée"], rows)
        more = f" (30 plus récentes sur {len(items)})" if len(items) > 30 else ""
        data = [{"id": c.id, "name": c.name, "rows": c.rows, "detected": c.detected} for c in items]
        return ToolOutput(content=f"{len(items)} conversion(s){more} :\n{table}", data=data)


class ReadTableInput(ConversionInput):
    limit: int = Field(default=200, ge=1, le=TABLE_MAX_LIMIT, description="Lignes à lire.")


class ReadTableTool(_ConvertTool[ReadTableInput]):
    name = "convert_read_table"
    description = (
        "Lit la table produite par une conversion (colonnes typées et lignes). Tu reçois au plus "
        f"{MAX_ROWS_FOR_MODEL} lignes ; la table lue s'affiche dans le panneau Artefacts."
    )
    input_model = ReadTableInput
    output_format = "atelier-table/1"

    async def run(self, params: ReadTableInput, ctx: ToolContext) -> ToolOutput:
        table = await self.client.table(params.conversion_id, limit=params.limit)
        keys = [c.key for c in table.columns]
        shown = [[row.get(k) for k in keys] for row in table.rows[:MAX_ROWS_FOR_MODEL]]
        total = table.total if table.total is not None else len(table.rows)
        cols = ", ".join(f"`{c.key}` ({c.type})" for c in table.columns)
        head = f"Table « {table.label or table.name} » : {total} ligne(s). Colonnes : {cols}."
        if len(table.rows) > len(shown):
            head += f" {len(shown)} premières lignes ci-dessous."
        artifact = Artifact(
            kind="data_preview",
            title=f"Convert — {table.label or table.name}",
            content=json.dumps(
                {"columns": [c.model_dump() for c in table.columns], "rows": table.rows},
                ensure_ascii=False,
                default=str,
            ),
            language="json",
            metadata={"source": "convert", "conversion_id": params.conversion_id},
        )
        data = atelier_table(
            table.name,
            table.label or table.name,
            [(c.key, c.label, c.type) for c in table.columns],
            [[row.get(k) for k in keys] for row in table.rows],
            total=table.total,
            truncated=table.truncated,
        )
        return ToolOutput(
            content=f"{head}\n{markdown_table(keys, shown)}", artifacts=[artifact], data=data
        )


class TabProjectsTool(_ConvertTool[NoInput]):
    name = "convert_tab_projects"
    description = (
        "Projets d'Atelier Tab (identifiant « proj_… », espace, nombre de tables) où "
        "convert_send_to_tab peut créer une table."
    )
    input_model = NoInput
    output_format = "json"

    async def run(self, params: NoInput, ctx: ToolContext) -> ToolOutput:
        status = await self.client.tab_status()
        if not status.found:
            return ToolOutput(
                content=f"Atelier Convert ne trouve pas les données d'Atelier Tab. {status.detail or ''}".strip(),
                is_error=True,
            )
        if not status.projects:
            return ToolOutput(
                content="Aucun projet dans Atelier Tab : créez-en un dans Tab.", data=[]
            )
        rows = [[p.id, p.workspace, p.name, p.tables] for p in status.projects]
        return ToolOutput(
            content=markdown_table(["id", "espace", "projet", "tables"], rows),
            data=[p.model_dump() for p in status.projects],
        )


# ═══ Écriture (validation requise) ════════════════════════════════


class ConvertFileInput(ToolInput):
    file: str = file_field()


class ConvertFileTool(_ConvertTool[ConvertFileInput]):
    name = "convert_file"
    description = (
        "Convertit un fichier joint à la conversation (PDF, PDF scanné, relevé bancaire, XLSX, "
        "XLS, CSV, TXT, OFX, QIF, JSON) en table typée dans Atelier Convert. Renvoie "
        "l'identifiant de la conversion et ce qui a été reconnu. Action soumise à la validation "
        "de l'utilisateur."
    )
    input_model = ConvertFileInput
    requires_approval = True
    output_format = "json"

    def __init__(self, client: ConvertClient, files: FileGetter) -> None:
        super().__init__(client)
        self.files = files

    def approval_summary(self, params: ConvertFileInput) -> str:
        return f"Convertir le fichier « {params.file} » en table dans Atelier Convert"

    async def run(self, params: ConvertFileInput, ctx: ToolContext) -> ToolOutput:
        info, content = await self.files().load(params.file)
        result = await self.client.convert(
            info.name, content, idempotency_key=idempotency_key("convert_file", info.id)
        )
        lines = [
            f"« {info.name} » ({human_size(info.size)}) converti : {result.detected}. "
            f"Conversion {result.conversion_id}, {result.rows} ligne(s), {result.columns} colonne(s)."
        ]
        if result.warnings:
            lines.append("Avertissements : " + " ; ".join(result.warnings))
        if "relevé" in result.detected.lower():
            lines.append(
                "Relevé bancaire : donnée financière. Pour l'analyser ou l'importer dans Atelier "
                "Trésorerie, passe par l'agent Trésorier (modèle local)."
            )
        return ToolOutput(content="\n".join(lines), data=result.model_dump())


class SendToTabInput(ConversionInput):
    project_id: str = Field(
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Projet d'Atelier Tab (« proj_… », voir convert_tab_projects).",
    )
    table_name: str | None = Field(
        default=None, max_length=200, description="Nom de la nouvelle table."
    )


class SendToTabTool(_ConvertTool[SendToTabInput]):
    name = "convert_send_to_tab"
    description = (
        "Crée dans Atelier Tab une NOUVELLE table à partir d'une conversion (colonnes typées). "
        "Action soumise à la validation de l'utilisateur."
    )
    input_model = SendToTabInput
    requires_approval = True
    output_format = "json"

    def approval_summary(self, params: SendToTabInput) -> str:
        name = f" « {params.table_name} »" if params.table_name else ""
        return (
            f"Créer la table{name} dans le projet {params.project_id} d'Atelier Tab "
            f"à partir de la conversion {params.conversion_id}"
        )

    async def run(self, params: SendToTabInput, ctx: ToolContext) -> ToolOutput:
        result = await self.client.send_to_tab(
            params.conversion_id,
            params.project_id,
            params.table_name,
            idempotency_key=idempotency_key(
                "send_to_tab", params.conversion_id, params.project_id, params.table_name or ""
            ),
        )
        return ToolOutput(
            content=(
                f"Table « {result.table_name} » créée dans le projet « {result.project_name} » "
                f"d'Atelier Tab : {result.rows} ligne(s), {result.columns} colonne(s)."
            ),
            data=result.model_dump(),
        )


class SendToVizInput(ConversionInput):
    name: str | None = Field(
        default=None,
        max_length=200,
        description="Nom du jeu dans Atelier Viz (défaut : la conversion).",
    )


class SendToVizTool(_ConvertTool[SendToVizInput]):
    name = "convert_send_to_viz"
    description = (
        "Envoie la table d'une conversion dans Atelier Viz comme jeu de données (renvoyer la "
        "même conversion met le jeu à jour, sans doublon). Ensuite : data_via_describe_dataset. "
        "Action soumise à la validation de l'utilisateur."
    )
    input_model = SendToVizInput
    requires_approval = True
    output_format = "json"

    def __init__(self, client: ConvertClient, viz: Callable[[], VizWriteClient]) -> None:
        super().__init__(client)
        self.viz = viz

    def approval_summary(self, params: SendToVizInput) -> str:
        name = f" « {params.name} »" if params.name else ""
        return f"Envoyer la conversion {params.conversion_id} dans Atelier Viz comme jeu de données{name}"

    async def run(self, params: SendToVizInput, ctx: ToolContext) -> ToolOutput:
        viz = self.viz()  # lève si Atelier Viz n'est pas configuré
        first = await self.client.table(params.conversion_id, limit=TABLE_MAX_LIMIT)
        rows = list(first.rows)
        page = first
        while page.truncated and len(rows) < MAX_ROWS_TO_VIZ:
            page = await self.client.table(
                params.conversion_id, offset=len(rows), limit=TABLE_MAX_LIMIT
            )
            if not page.rows:
                break
            rows += page.rows
        cut = len(rows) >= MAX_ROWS_TO_VIZ and page.truncated
        table = first.model_copy(
            update={"rows": rows[:MAX_ROWS_TO_VIZ], "truncated": False, "total": None}
        )
        name = params.name or first.label or first.name
        result = await viz.upsert_table(
            name,
            f"convert:{params.conversion_id}",
            table.model_dump(mode="json", exclude_none=True),
            idempotency_key=idempotency_key(
                "send_to_viz", params.conversion_id, name, str(len(rows))
            ),
        )
        headers = viz.last_headers
        replayed = headers is not None and headers.get("idempotency-replayed") == "true"
        verb = (
            "déjà envoyé (inchangé)"
            if replayed
            else ("créé" if result.action == "created" else "mis à jour")
        )
        text = (
            f"Jeu « {result.name} » {verb} dans Atelier Viz (id {result.dataset_id}), "
            f"{result.rows} ligne(s). Consulte son schéma avec data_via_describe_dataset."
        )
        if cut:
            text += f" Attention : envoi limité aux {MAX_ROWS_TO_VIZ} premières lignes."
        return ToolOutput(content=text, data=result.model_dump())


def convert_read_tools(client: ConvertClient) -> list[BaseTool[Any]]:
    return [ListConversionsTool(client), ReadTableTool(client), TabProjectsTool(client)]


def convert_write_tools(
    client: ConvertClient,
    files: FileGetter,
    viz: Callable[[], VizWriteClient],
) -> list[BaseTool[Any]]:
    return [ConvertFileTool(client, files), SendToTabTool(client), SendToVizTool(client, viz)]
