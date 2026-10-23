"""Atelier Trésorerie en écriture (extension Agent_DatAnalyst) — données SENSIBLES.

Tous ces outils sont `sensitive` : l'agent qui les utilise (Trésorier) est verrouillé sur le
modèle local, sans repli vers Claude. Chaque écriture attend la validation de l'utilisateur et
porte l'en-tête `X-Requested-With` exigé par Trésorerie (`api.py`, garde anti-CSRF).

Routes lues dans le dépôt d'Atelier Trésorerie : `POST /api/accounts`, `/transactions`,
`/holdings`, `/import/preview` (multipart, ne modifie rien), `/import/commit`. Montants envoyés
en euros (« -12.50 », `finance.to_cents`), reçus en centimes.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import PurePath
from typing import Any, Literal

import httpx
from app.schemas.tresorerie import Account, AccountType, Transaction
from app.tools.base import BaseTool, ToolContext, ToolInput, ToolOutput
from app.tools.formatting import euros, table_for_model
from app.tools.http import AppError
from app.tools.tresorerie import DAY, TresorerieClient
from pydantic import BaseModel, ConfigDict, Field

from atelier_datanalyst.convert import ConvertClient
from atelier_datanalyst.files import FileGetter

#: En-tête anti-CSRF exigé par Atelier Trésorerie sur toute requête qui modifie des données
WRITE_HEADERS = {"X-Requested-With": "atelier-nexus"}
#: Fichiers lus par l'import de Trésorerie
IMPORT_TYPES = {
    ".csv": "text/csv",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class _R(BaseModel):
    model_config = ConfigDict(extra="ignore")


AssetType = Literal["action", "etf", "fonds", "obligation", "crypto", "metal", "autre"]
Metal = Literal["or", "argent", "platine", "palladium"]
HoldingUnit = Literal["titre", "g", "oz", "piece"]
ImportRowStatus = Literal["ok", "duplicate", "error"]


class CreatedHolding(_R):
    id: int
    name: str
    asset_type: str
    quantity: float
    value_cents: int = 0
    cost_basis_cents: int = 0


class ImportRow(_R):
    line: int
    status: ImportRowStatus
    error: str | None = None
    date: str | None = None
    label: str | None = None
    amount_cents: int | None = None
    category_id: int | None = None
    category_name: str | None = None
    import_hash: str | None = None


class ImportSummary(_R):
    total: int
    ok: int
    duplicates: int
    errors: int


class ImportPreview(_R):
    filename: str
    account_id: int
    headers: list[str] = Field(default_factory=list)
    mapping_errors: dict[str, str] | None = None
    rows: list[ImportRow] = Field(default_factory=list)
    summary: ImportSummary


class ImportCommit(_R):
    inserted: int
    skipped: int


class TresoWriteClient(TresorerieClient):
    """Client Trésorerie de Nexus (`from_settings` hérité) + écritures."""

    # ─── Écritures (appelées seulement après validation) ──────────

    async def _write(self, path: str, body: dict[str, Any]) -> Any:
        return await self.request(
            "POST",
            path,
            json_body={k: v for k, v in body.items() if v is not None},
            headers=WRITE_HEADERS,
        )

    async def create_account(self, body: dict[str, Any]) -> Account:
        return self.parse(Account, await self._write("accounts", body))

    async def create_transaction(self, body: dict[str, Any]) -> Transaction:
        return self.parse(Transaction, await self._write("transactions", body))

    async def create_holding(self, body: dict[str, Any]) -> CreatedHolding:
        return self.parse(CreatedHolding, await self._write("holdings", body))

    async def import_preview(self, filename: str, content: bytes, account_id: int) -> ImportPreview:
        """Aperçu d'import (ne modifie rien ; POST multipart, d'où l'en-tête anti-CSRF)."""
        media_type = IMPORT_TYPES.get(PurePath(filename).suffix.lower(), "application/octet-stream")
        try:
            response = await self._http.post(
                "import/preview",
                files={"file": (filename, content, media_type)},
                data={"account_id": str(account_id)},
                headers=WRITE_HEADERS,
            )
        except httpx.HTTPError as exc:
            raise AppError(
                f"{self.label} injoignable : {self._unavailable_hint()}", app=self.app
            ) from exc
        if response.status_code >= 400:
            raise AppError(
                f"{self.label} : {self._detail(response) or f'erreur {response.status_code}'}",
                app=self.app,
            )
        return self.parse(ImportPreview, response.json())

    async def import_commit(self, account_id: int, rows: list[dict[str, Any]]) -> ImportCommit:
        return self.parse(
            ImportCommit,
            await self._write("import/commit", {"account_id": account_id, "rows": rows}),
        )


# ═══ Outils (validation requise) ══════════════════════════════════


def _amount(value: float) -> str:
    """Euros → saisie acceptée par Trésorerie (« -12.50 ») : jamais d'arrondi flottant affiché."""
    return f"{value:.2f}"


class _TresoTool[InputT: ToolInput](BaseTool[InputT]):
    expected_errors = (AppError,)
    sensitive = True

    def __init__(self, client: TresoWriteClient) -> None:
        self.client = client


class _TresoWriteTool[InputT: ToolInput](_TresoTool[InputT]):
    requires_approval = True
    output_format = "json"


class CreateAccountInput(ToolInput):
    name: str = Field(min_length=1, max_length=80)
    type: AccountType = Field(
        default="courant", description="courant, epargne, investissement, credit ou especes."
    )
    institution: str | None = Field(default=None, max_length=80, description="Banque ou courtier.")
    opening_balance: float = Field(
        default=0, ge=-1e10, le=1e10, description="Solde d'ouverture, en euros."
    )


class CreateAccountTool(_TresoWriteTool[CreateAccountInput]):
    name = "tresorerie_create_account"
    description = (
        "Crée un compte dans Atelier Trésorerie (courant, épargne, investissement, crédit, "
        "espèces). Vérifie d'abord avec tresorerie_accounts qu'il n'existe pas déjà."
    )
    input_model = CreateAccountInput

    def approval_summary(self, params: CreateAccountInput) -> str:
        bank = f" chez {params.institution}" if params.institution else ""
        return (
            f"Créer le compte {params.type} « {params.name} »{bank}, solde d'ouverture "
            f"{_amount(params.opening_balance)} €"
        )

    async def run(self, params: CreateAccountInput, ctx: ToolContext) -> ToolOutput:
        account = await self.client.create_account(
            {
                "name": params.name,
                "type": params.type,
                "institution": params.institution,
                "opening_balance": _amount(params.opening_balance),
            }
        )
        return ToolOutput(
            content=f"Compte #{account.id} « {account.name} » ({account.type_label}) créé, "
            f"solde {euros(account.balance_cents)}.",
            data={"account_id": account.id, "name": account.name},
        )


class AddTransactionInput(ToolInput):
    account_id: int = Field(ge=1, description="Compte (tresorerie_accounts).")
    date: str = Field(pattern=DAY, description="AAAA-MM-JJ.")
    label: str = Field(min_length=1, max_length=200)
    amount: float = Field(
        ge=-1e10,
        le=1e10,
        description="En euros : négatif = dépense (achat de matériel…), positif = entrée.",
    )
    category_id: int | None = Field(
        default=None, ge=1, description="Catégorie (tresorerie_categories)."
    )
    notes: str | None = Field(default=None, max_length=1000)


class AddTransactionTool(_TresoWriteTool[AddTransactionInput]):
    name = "tresorerie_add_transaction"
    description = (
        "Saisit une opération (dépense ou entrée) sur un compte d'Atelier Trésorerie : achat de "
        "matériel, facture, remboursement… Montant négatif = sortie."
    )
    input_model = AddTransactionInput

    def approval_summary(self, params: AddTransactionInput) -> str:
        return (
            f"Saisir « {params.label} » le {params.date} : {_amount(params.amount)} € "
            f"sur le compte #{params.account_id}"
        )

    async def run(self, params: AddTransactionInput, ctx: ToolContext) -> ToolOutput:
        if params.amount == 0:
            return ToolOutput(content="Montant nul refusé par Trésorerie.", is_error=True)
        tx = await self.client.create_transaction(
            {
                "account_id": params.account_id,
                "date": params.date,
                "label": params.label,
                "amount": _amount(params.amount),
                "category_id": params.category_id,
                "notes": params.notes,
            }
        )
        return ToolOutput(
            content=f"Opération #{tx.id} « {tx.label} » ({euros(tx.amount_cents)}) enregistrée "
            f"sur « {tx.account_name} »"
            + (f", catégorie {tx.category_name}." if tx.category_name else "."),
            data={"transaction_id": tx.id, "amount_cents": tx.amount_cents},
        )


class AddHoldingInput(ToolInput):
    account_id: int = Field(ge=1, description="Compte de type investissement de préférence.")
    name: str = Field(
        min_length=1, max_length=120, description="Ex. « MSCI World », « Lingot 100 g »."
    )
    asset_type: AssetType = Field(
        description="action, etf, fonds, obligation, crypto, metal, autre."
    )
    quantity: float = Field(ge=0, le=1e12)
    cost_basis: float | None = Field(
        default=None, ge=0, le=1e10, description="Prix de revient TOTAL, en euros."
    )
    price: float | None = Field(
        default=None, ge=0, le=1e10, description="Cours unitaire actuel, en euros."
    )
    symbol: str | None = Field(default=None, max_length=20)
    isin: str | None = Field(default=None, pattern=r"^[A-Za-z]{2}[A-Za-z0-9]{9}[0-9]$")
    metal: Metal | None = Field(
        default=None, description="Métal précieux : or, argent, platine, palladium."
    )
    unit: HoldingUnit | None = Field(default=None, description="titre, g, oz ou piece.")
    weight_g: float | None = Field(
        default=None, gt=0, le=1e7, description="Poids d'une unité de métal, en grammes."
    )
    purity: float | None = Field(
        default=None, gt=0, le=1000, description="Titre du métal en millièmes (999,9…)."
    )
    notes: str | None = Field(default=None, max_length=1000)


class AddHoldingTool(_TresoWriteTool[AddHoldingInput]):
    name = "tresorerie_add_holding"
    description = (
        "Ajoute une position de placement à un compte d'Atelier Trésorerie : action, ETF, fonds, "
        "obligation, crypto ou métal précieux (or, argent… avec poids et titre)."
    )
    input_model = AddHoldingInput

    def approval_summary(self, params: AddHoldingInput) -> str:
        cost = f", prix de revient {_amount(params.cost_basis)} €" if params.cost_basis else ""
        return (
            f"Ajouter la position {params.asset_type} « {params.name} » ({params.quantity:g}) "
            f"au compte #{params.account_id}{cost}"
        )

    async def run(self, params: AddHoldingInput, ctx: ToolContext) -> ToolOutput:
        if params.asset_type == "metal" and params.metal is None:
            return ToolOutput(
                content="Préciser le métal (or, argent, platine, palladium).", is_error=True
            )
        body: dict[str, Any] = params.model_dump(exclude={"cost_basis", "price"}, exclude_none=True)
        if params.cost_basis is not None:
            body["cost_basis"] = _amount(params.cost_basis)
        if params.price is not None:
            body["price"] = _amount(params.price)
        holding = await self.client.create_holding(body)
        return ToolOutput(
            content=f"Position #{holding.id} « {holding.name} » ajoutée ({holding.quantity:g}, "
            f"valeur {euros(holding.value_cents)}).",
            data={"holding_id": holding.id},
        )


class ImportInput(ToolInput):
    account_id: int = Field(ge=1, description="Compte qui reçoit les opérations.")
    file: str | None = Field(
        default=None, max_length=255, description="Fichier joint CSV ou XLSX (sinon conversion_id)."
    )
    conversion_id: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{12}$",
        description="Conversion d'Atelier Convert (relevé PDF converti avec convert_file).",
    )


class _ImportTool(_TresoTool[ImportInput]):
    def __init__(
        self,
        client: TresoWriteClient,
        files: FileGetter,
        convert: Callable[[], ConvertClient],
    ) -> None:
        super().__init__(client)
        self.files = files
        self.convert = convert

    def _source(self, params: ImportInput) -> str:
        return f"« {params.file} »" if params.file else f"la conversion {params.conversion_id}"

    async def _preview(self, params: ImportInput) -> ImportPreview | ToolOutput:
        if (params.file is None) == (params.conversion_id is None):
            return ToolOutput(content="Donner soit file, soit conversion_id.", is_error=True)
        if params.conversion_id:
            content = await self.convert().export(params.conversion_id, "xlsx")
            filename = f"conversion-{params.conversion_id}.xlsx"
        else:
            assert params.file is not None
            info, content = await self.files().load(params.file)
            filename = info.name
            if PurePath(filename).suffix.lower() not in IMPORT_TYPES:
                return ToolOutput(
                    content="Trésorerie importe des CSV ou XLSX : convertis d'abord le fichier avec "
                    "convert_file, puis importe la conversion (conversion_id).",
                    is_error=True,
                )
        return await self.client.import_preview(filename, content, params.account_id)


class PreviewImportTool(_ImportTool):
    name = "tresorerie_preview_import"
    description = (
        "Aperçu, sans rien enregistrer, de l'import d'un relevé dans un compte : opérations "
        "reconnues, doublons, lignes en erreur, catégories devinées. Toujours avant "
        "tresorerie_import_statement."
    )
    input_model = ImportInput
    output_format = "json"

    async def run(self, params: ImportInput, ctx: ToolContext) -> ToolOutput:
        preview = await self._preview(params)
        if isinstance(preview, ToolOutput):
            return preview
        if preview.mapping_errors:
            problems = "; ".join(f"{k} : {v}" for k, v in preview.mapping_errors.items())
            return ToolOutput(
                content=f"Colonnes non reconnues ({problems}). En-têtes du fichier : "
                f"{', '.join(preview.headers)}. Import à faire depuis l'écran Import de Trésorerie.",
                is_error=True,
            )
        s = preview.summary
        lines = [
            f"Aperçu de {self._source(params)} vers le compte #{params.account_id} : {s.total} ligne(s), "
            f"{s.ok} à importer, {s.duplicates} doublon(s) ignoré(s), {s.errors} en erreur."
        ]
        ok = [r for r in preview.rows if r.status == "ok"]
        if ok:
            table, _ = table_for_model(
                ["date", "libellé", "montant", "catégorie"],
                [[r.date, r.label, euros(r.amount_cents), r.category_name or "—"] for r in ok],
                noun="opération",
            )
            lines.append(table)
        errors = [r for r in preview.rows if r.status == "error"][:10]
        lines += [f"- ligne {r.line} : {r.error}" for r in errors]
        return ToolOutput(content="\n".join(lines), data=s.model_dump())


class ImportStatementTool(_ImportTool):
    name = "tresorerie_import_statement"
    description = (
        "Importe dans un compte les opérations valides d'un relevé (fichier joint ou conversion "
        "Atelier Convert). Doublons et lignes en erreur ignorés. Après tresorerie_preview_import."
    )
    input_model = ImportInput
    requires_approval = True
    output_format = "json"

    def approval_summary(self, params: ImportInput) -> str:
        return (
            f"Importer les opérations de {self._source(params)} dans le compte #{params.account_id} "
            "d'Atelier Trésorerie (doublons ignorés)"
        )

    async def run(self, params: ImportInput, ctx: ToolContext) -> ToolOutput:
        preview = await self._preview(params)
        if isinstance(preview, ToolOutput):
            return preview
        rows = [
            {
                "date": r.date,
                "label": r.label,
                "amount_cents": r.amount_cents,
                "category_id": r.category_id,
                "import_hash": r.import_hash,
            }
            for r in preview.rows
            if r.status == "ok"
        ]
        if not rows:
            return ToolOutput(
                content=f"Rien à importer : {preview.summary.duplicates} doublon(s), "
                f"{preview.summary.errors} ligne(s) en erreur.",
                data={"inserted": 0, "skipped": 0},
            )
        result = await self.client.import_commit(params.account_id, rows)
        return ToolOutput(
            content=f"{result.inserted} opération(s) importée(s) dans le compte #{params.account_id}"
            f"{f', {result.skipped} déjà présente(s)' if result.skipped else ''}.",
            data=result.model_dump(),
        )


def tresorerie_write_tools(
    client: TresoWriteClient,
    files: FileGetter,
    convert: Callable[[], ConvertClient],
) -> list[BaseTool[Any]]:
    """Écritures dans Atelier Trésorerie : sensibles (modèle local) et soumises à validation."""
    return [
        CreateAccountTool(client),
        AddTransactionTool(client),
        AddHoldingTool(client),
        PreviewImportTool(client, files, convert),
        ImportStatementTool(client, files, convert),
    ]
