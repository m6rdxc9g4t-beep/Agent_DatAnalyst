"""Atelier Viz en écriture : importer un fichier joint, publier une table, créer un rapport.

Lectures (schéma d'un jeu) par le client Data Via de Nexus (`app/tools/data_via.py`) ; écritures
par `VizWriteClient`, propre à l'extension, sur les seules routes d'Atelier Viz lues dans son
dépôt : `POST /api/datasets` (multipart), `POST /api/atelier/datasets` (nœud atelier-node/1),
`POST /api/ai/visuals/repair` (validation **sans IA**), `POST /api/reports`, `PUT /api/reports/{id}`.

Le rapport est composé par l'agent puis validé par Atelier Viz : aucune donnée ne part vers l'IA
de Viz, et un champ inconnu ou une agrégation impossible est refusé avant tout enregistrement.
"""

from __future__ import annotations

import difflib
import uuid
from collections.abc import Callable, Mapping
from pathlib import PurePath
from typing import Any, ClassVar, Literal

import httpx
from app.core.config import Settings
from app.schemas.data_via import Agg, ColumnType, DatasetProfile, DateBin
from app.tools.base import BaseTool, ToolContext, ToolInput, ToolOutput
from app.tools.data_via import DataViaClient, DataViaError
from app.tools.http import AppClient, AppError
from pydantic import BaseModel, ConfigDict, Field

from atelier_datanalyst.files import FileGetter, file_field


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ─── Écritures (Atelier Viz) ─────────────────────────────────────
# Miroir de `schemas/atelier.py` (UpsertTableResult), `schemas/builder.py` (catalogue, réparation)
# et `schemas/report.py` (rapport) d'Atelier Viz.

VizVisualType = Literal[
    "column",
    "bar",
    "line",
    "area",
    "pie",
    "donut",
    "treemap",
    "funnel",
    "waterfall",
    "kpi",
    "table",
]


class UpsertTableResult(_Response):
    dataset_id: str
    name: str
    rows: int
    action: Literal["created", "updated"]


class RoleSpec(_Request):
    id: str
    label: str
    kind: Literal["dimension", "measure"]
    max: int | None = None
    required: bool = False
    types: list[ColumnType] | None = None


class VisualTypeSpec(_Request):
    type: VizVisualType
    label: str
    roles: list[RoleSpec]
    options: list[dict[str, Any]] = Field(default_factory=list)


class FieldBinding(BaseModel):
    """Champ placé dans un rôle d'un visuel (miroir de `FieldBinding` d'Atelier Viz)."""

    model_config = ConfigDict(extra="ignore")

    field: str
    agg: Agg | None = None
    bin: DateBin | None = None
    label: str | None = None


class ProposedVisual(_Response):
    type: str
    title: str | None = None
    roles: dict[str, list[FieldBinding]]
    options: dict[str, Any] = Field(default_factory=dict)


class RepairedVisual(_Response):
    visual: ProposedVisual | None = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class RepairResponse(_Response):
    results: list[RepairedVisual]


class VizReport(_Response):
    id: str
    name: str
    dataset_id: str | None = None
    pages: list[dict[str, Any]] = Field(default_factory=list)


class VizWriteClient(AppClient):
    """Écritures dans Atelier Viz (adresse : `DATA_VIA_BASE_URL` de Nexus)."""

    app = "viz"
    label = "Atelier Viz"
    error_keys = ("detail", "message", "error")

    @classmethod
    def from_settings(
        cls, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> VizWriteClient | None:
        if not settings.data_via_base_url:
            return None
        return cls(
            settings.data_via_base_url, timeout=settings.data_via_timeout, transport=transport
        )

    def _detail(self, response: httpx.Response) -> str | None:
        try:
            body = response.json()
        except ValueError:
            return None
        detail = body.get("detail") if isinstance(body, dict) else None
        if isinstance(detail, list):  # erreurs de validation FastAPI
            return "; ".join(str(d.get("msg", d)) for d in detail if isinstance(d, dict))
        return super()._detail(response)

    async def upload_dataset(
        self, filename: str, content: bytes, media_type: str
    ) -> DatasetProfile:
        """CSV, XLSX ou JSON → nouveau jeu de données (profil complet en retour)."""
        data = await self.request(
            "POST", "datasets", files={"file": (filename, content, media_type)}
        )
        return self.parse(DatasetProfile, data)

    async def upsert_table(
        self, name: str, key: str, table: dict[str, Any], *, idempotency_key: str | None = None
    ) -> UpsertTableResult:
        """Table `atelier-table/1` → jeu créé, ou remplacé s'il porte déjà cette clé."""
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        data = await self.request(
            "POST",
            "atelier/datasets",
            json_body={"name": name, "key": key, "table": table},
            headers=headers,
        )
        return self.parse(UpsertTableResult, data)

    async def repair_visuals(
        self, dataset_id: str, catalog: list[VisualTypeSpec], visuals: list[dict[str, Any]]
    ) -> RepairResponse:
        data = await self.request(
            "POST",
            "ai/visuals/repair",
            json_body={
                "dataset_id": dataset_id,
                "catalog": [c.model_dump(mode="json") for c in catalog],
                "visuals": visuals,
            },
        )
        return self.parse(RepairResponse, data)

    async def create_report(self, name: str, dataset_id: str) -> VizReport:
        data = await self.request(
            "POST", "reports", json_body={"name": name, "dataset_id": dataset_id}
        )
        return self.parse(VizReport, data)

    async def save_report(self, report_id: str, body: dict[str, Any]) -> VizReport:
        if not report_id or not all(c.isalnum() or c in "-_" for c in report_id):
            raise AppError(f"Identifiant de rapport invalide : {report_id!r}", app=self.app)
        data = await self.request("PUT", f"reports/{report_id}", json_body=body)
        return self.parse(VizReport, data)


def _suggest(name: str, candidates: list[str]) -> str:
    close = difflib.get_close_matches(name, candidates, n=3, cutoff=0.5)
    return f" Vouliez-vous dire : {', '.join(close)} ?" if close else ""


# ═══ Outils (validation requise) ══════════════════════════════════

#: Formats lus par `POST /api/datasets` d'Atelier Viz (`services/data/parsing.py`)
VIZ_FILE_TYPES = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".json": "application/json",
}

_AXIS = RoleSpec(id="category", label="Axe", kind="dimension", max=4, required=True)
_LEGEND = RoleSpec(id="series", label="Légende", kind="dimension", max=1)
_VALUES = RoleSpec(id="values", label="Valeurs", kind="measure", required=True)
_ONE_VALUE = RoleSpec(id="values", label="Valeurs", kind="measure", max=1, required=True)

#: Miroir du registre des visuels d'Atelier Viz (`frontend/src/visuals/registry.ts`),
#: limité aux visuels qu'un agent compose sans réglage fin.
VIZ_CATALOG: tuple[VisualTypeSpec, ...] = (
    VisualTypeSpec(type="column", label="Histogramme", roles=[_AXIS, _LEGEND, _VALUES]),
    VisualTypeSpec(type="bar", label="Barres horizontales", roles=[_AXIS, _LEGEND, _VALUES]),
    VisualTypeSpec(type="line", label="Courbes", roles=[_AXIS, _LEGEND, _VALUES]),
    VisualTypeSpec(type="area", label="Aires", roles=[_AXIS, _LEGEND, _VALUES]),
    VisualTypeSpec(type="pie", label="Secteurs", roles=[_AXIS, _ONE_VALUE]),
    VisualTypeSpec(type="donut", label="Anneau", roles=[_AXIS, _ONE_VALUE]),
    VisualTypeSpec(type="treemap", label="Treemap", roles=[_AXIS, _ONE_VALUE]),
    VisualTypeSpec(type="funnel", label="Entonnoir", roles=[_AXIS, _ONE_VALUE]),
    VisualTypeSpec(type="waterfall", label="Cascade", roles=[_AXIS, _ONE_VALUE]),
    VisualTypeSpec(
        type="kpi",
        label="Carte KPI",
        roles=[
            _ONE_VALUE,
            RoleSpec(id="trend", label="Tendance (date)", kind="dimension", max=1, types=["date"]),
        ],
    ),
    VisualTypeSpec(
        type="table",
        label="Table",
        roles=[
            RoleSpec(id="columns", label="Colonnes", kind="dimension"),
            RoleSpec(id="values", label="Valeurs", kind="measure"),
        ],
    ),
)
VISUAL_LABELS = {spec.type: spec.label for spec in VIZ_CATALOG}

#: Options par défaut du registre de Viz (le Studio les applique aussi à l'ouverture)
VISUAL_DEFAULTS: dict[str, dict[str, Any]] = {
    "column": {"legend": True, "labels": False, "sort": "category"},
    "bar": {"legend": True, "labels": True, "sort": "value_desc"},
    "line": {"legend": True, "labels": False, "sort": "category", "smooth": False, "markers": True},
    "area": {"legend": True, "labels": False, "sort": "category", "smooth": True},
    "pie": {"legend": True, "labels": True, "sort": "value_desc"},
    "donut": {"legend": True, "labels": False, "sort": "value_desc"},
    "treemap": {"sort": "value_desc", "topN": 30},
    "funnel": {"sort": "value_desc"},
    "waterfall": {"sort": "category"},
    "kpi": {"higherIsBetter": True},
    "table": {},
}
_SERIES_TYPES = {"column", "bar", "line", "area"}

PAGE_WIDTH = 1280
_MARGIN, _GAP, _CELL_H = 20, 20, 330
_CELL_W = (PAGE_WIDTH - 2 * _MARGIN - _GAP) // 2


def _layout(index: int) -> dict[str, int]:
    """Grille de deux colonnes sur une page 16:9 (pixels de page)."""
    row, col = divmod(index, 2)
    return {
        "x": _MARGIN + col * (_CELL_W + _GAP),
        "y": _MARGIN + row * (_CELL_H + _GAP),
        "w": _CELL_W,
        "h": _CELL_H,
    }


def _short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


class _DataViaWriteTool[InputT: ToolInput](BaseTool[InputT]):
    expected_errors: ClassVar[tuple[type[Exception], ...]] = (DataViaError, AppError)
    requires_approval = True

    def __init__(
        self,
        client: VizWriteClient,
        files: FileGetter,
        data_via: Callable[[], DataViaClient],
    ) -> None:
        self.client = client
        self.files = files
        self.data_via = data_via


class ImportFileInput(ToolInput):
    file: str = file_field()


class ImportFileTool(_DataViaWriteTool[ImportFileInput]):
    name = "data_via_import_file"
    description = (
        "Importe un fichier joint à la conversation (CSV, XLSX ou JSON) comme NOUVEAU jeu de "
        "données dans Atelier Viz (identifiant, colonnes). "
        "PDF ou relevé bancaire : le convertir d'abord avec Atelier Convert (convert_file), puis "
        "convert_send_to_viz. Action soumise à la validation de l'utilisateur."
    )
    input_model = ImportFileInput
    output_format = "json"

    def approval_summary(self, params: ImportFileInput) -> str:
        return (
            f"Importer le fichier « {params.file} » dans Atelier Viz comme nouveau jeu de données"
        )

    async def run(self, params: ImportFileInput, ctx: ToolContext) -> ToolOutput:
        info, content = await self.files().load(params.file)
        suffix = PurePath(info.name).suffix.lower()
        media_type = VIZ_FILE_TYPES.get(suffix)
        if media_type is None:
            return ToolOutput(
                content=(
                    f"Atelier Viz ne lit pas les fichiers « {suffix or 'sans extension'} » "
                    "(CSV, XLSX ou JSON). Convertis d'abord le fichier avec `convert_file`, puis "
                    "envoie la table avec `convert_send_to_viz`."
                ),
                is_error=True,
            )
        profile = await self.client.upload_dataset(info.name, content, media_type)
        cols = ", ".join(f"`{c.name}` ({c.type})" for c in profile.columns)
        return ToolOutput(
            content=f"Jeu de données créé dans Atelier Viz : « {profile.name} » (id {profile.id}), "
            f"{profile.rows} lignes. Colonnes : {cols}. Appelle data_via_describe_dataset avant "
            "toute requête.",
            data={"dataset_id": profile.id, "name": profile.name, "rows": profile.rows},
        )


#: Agrégations d'un visuel (`AGGS` d'Atelier Viz, `services/ai/builder.py`) : pas de mesure
#: calculée du modèle (`calc`), que la validation des visuels de Viz ne reconnaît pas.
VisualAgg = Literal["sum", "avg", "median", "min", "max", "count", "count_distinct"]
#: Agrégations permises par type de colonne (`AGGS_BY_TYPE` d'Atelier Viz)
AGGS_BY_TYPE: dict[str, set[str]] = {
    "number": {"sum", "avg", "median", "min", "max", "count", "count_distinct"},
    "boolean": {"sum", "avg", "count"},
    "date": {"min", "max", "count", "count_distinct"},
    "string": {"count", "count_distinct", "min", "max"},
}
AGG_LABELS = {
    "sum": "somme", "avg": "moyenne", "median": "médiane", "min": "min", "max": "max",
    "count": "nombre", "count_distinct": "nombre distinct",
}  # fmt: skip
BIN_LABELS = {
    "year": "année",
    "quarter": "trimestre",
    "month": "mois",
    "week": "semaine",
    "day": "jour",
}
#: Visuels à une seule mesure (rôle « values » avec max = 1 dans le registre de Viz)
ONE_VALUE_TYPES = {"pie", "donut", "treemap", "funnel", "waterfall", "kpi"}


class VisualValue(ToolInput):
    field: str = Field(min_length=1, max_length=200, description="Colonne mesurée.")
    agg: VisualAgg = Field(
        default="sum",
        description="sum, avg, median, min, max (nombres) ; count, count_distinct (tout type).",
    )


class VisualSpec(ToolInput):
    type: VizVisualType = Field(
        description="column, bar, line, area, pie, donut, treemap, funnel, waterfall, kpi, table."
    )
    title: str = Field(min_length=1, max_length=120)
    category: list[str] = Field(
        default_factory=list,
        max_length=4,
        description="Axe (dimensions, plusieurs = hiérarchie). Table : colonnes affichées. "
        "KPI : une date facultative pour la tendance.",
    )
    values: list[VisualValue] = Field(default_factory=list, max_length=3)
    series: str | None = Field(
        default=None,
        max_length=200,
        description="Légende (une dimension) : column, bar, line, area.",
    )
    date_bin: DateBin | None = Field(
        default=None,
        description="Période d'un axe de type date : year, quarter, month (défaut), week, day.",
    )


class CreateReportInput(ToolInput):
    dataset_id: str = Field(
        pattern=r"^[A-Za-z0-9_-]+$", description="Jeu Atelier Viz (data_via_list_datasets)."
    )
    name: str = Field(min_length=1, max_length=200, description="Nom du rapport.")
    visuals: list[VisualSpec] = Field(min_length=1, max_length=6)


def _visual_roles(spec: VisualSpec, types: Mapping[str, str]) -> dict[str, list[dict[str, Any]]]:
    def dim(name: str) -> dict[str, Any]:
        binding: dict[str, Any] = {"field": name}
        if types.get(name) == "date":  # période explicite : Viz imposerait « month » sans le dire
            binding["bin"] = spec.date_bin or "month"
        return binding

    dims = [dim(f) for f in spec.category]
    vals = [{"field": v.field, "agg": v.agg} for v in spec.values]
    if spec.type == "table":
        return {"columns": dims, "values": vals}
    if spec.type == "kpi":
        roles: dict[str, list[dict[str, Any]]] = {"values": vals}
        if dims:
            roles["trend"] = dims[:1]
        return roles
    roles = {"category": dims, "values": vals}
    if spec.series and spec.type in _SERIES_TYPES:
        roles["series"] = [{"field": spec.series}]
    return roles


def _check_visual(spec: VisualSpec, profile: DatasetProfile) -> list[str]:
    """Contrôles locaux (règles du registre et de la validation de Viz) : erreurs claires et
    suggestions AVANT d'appeler Viz."""
    columns = {c.name: c.type for c in profile.columns}
    names = list(columns)
    where = f"« {spec.title} » ({VISUAL_LABELS[spec.type]})"
    errors = []
    for name in [*spec.category, *([spec.series] if spec.series else [])]:
        if name not in columns:
            errors.append(f"{where} : colonne inconnue « {name} ».{_suggest(name, names)}")
    for v in spec.values:
        if v.field not in columns:
            errors.append(f"{where} : colonne inconnue « {v.field} ».{_suggest(v.field, names)}")
        elif v.agg not in AGGS_BY_TYPE[columns[v.field]]:
            allowed = ", ".join(sorted(AGGS_BY_TYPE[columns[v.field]]))
            errors.append(
                f"{where} : « {v.agg} » impossible sur « {v.field} » ({columns[v.field]}) ; "
                f"possibles : {allowed}."
            )
    if len(set(spec.category)) != len(spec.category) or len({v.field for v in spec.values}) != len(
        spec.values
    ):
        errors.append(f"{where} : un même champ est placé deux fois.")
    if spec.type in ONE_VALUE_TYPES and len(spec.values) != 1:
        errors.append(f"{where} : exactement une mesure (values).")
    if spec.type in _SERIES_TYPES and not spec.values:
        errors.append(f"{where} : au moins une mesure (values).")
    if spec.type != "table" and spec.type != "kpi" and not spec.category:
        errors.append(f"{where} : un axe (category) est obligatoire.")
    if spec.type == "kpi" and (
        len(spec.category) > 1 or any(columns.get(c) not in (None, "date") for c in spec.category)
    ):
        errors.append(f"{where} : un KPI n'accepte qu'une date de tendance en category.")
    if spec.type == "table" and not spec.category and not spec.values:
        errors.append(f"{where} : une table affiche au moins une colonne ou une mesure.")
    if spec.series and spec.type not in _SERIES_TYPES:
        errors.append(f"{where} : légende (series) réservée à column, bar, line, area.")
    return errors


def _bindings(
    roles: Mapping[str, list[Any]],
) -> dict[str, list[tuple[str, str | None, str | None]]]:
    """Forme comparable des rôles : (champ, agrégation, période), dans l'ordre."""

    def key(b: Any) -> tuple[str, str | None, str | None]:
        if isinstance(b, dict):
            return (b["field"], b.get("agg"), b.get("bin"))
        return (b.field, b.agg, b.bin)

    return {role: [key(b) for b in items] for role, items in roles.items() if items}


def _describe(spec: VisualSpec) -> str:
    """« Courbes « CA par mois » : somme de chiffre_affaires par date (mois) »."""
    measures = ", ".join(f"{AGG_LABELS[v.agg]} de {v.field}" for v in spec.values) or "—"
    axis = ", ".join(spec.category)
    if axis and spec.type != "kpi":
        measures += f" par {axis}"
        if spec.date_bin:
            measures += f" ({BIN_LABELS[spec.date_bin]})"
    if spec.series and spec.type in _SERIES_TYPES:
        measures += f" et {spec.series}"
    return f"{VISUAL_LABELS[spec.type]} « {spec.title} » : {measures}"


class CreateReportTool(_DataViaWriteTool[CreateReportInput]):
    name = "data_via_create_report"
    description = (
        "Crée un rapport Atelier Viz d'une page avec 1 à 6 visuels (histogramme, barres, courbes, "
        "aires, secteurs, anneau, treemap, entonnoir, cascade, KPI, table) sur un jeu Atelier "
        "Viz. Les champs sont vérifiés contre le schéma du jeu, puis par Atelier Viz, avant "
        "l'enregistrement. "
        "Action soumise à la validation de l'utilisateur."
    )
    input_model = CreateReportInput
    output_format = "json"

    def approval_summary(self, params: CreateReportInput) -> str:
        visuals = " ; ".join(_describe(v) for v in params.visuals)
        return (
            f"Créer dans Atelier Viz le rapport « {params.name} » "
            f"({len(params.visuals)} visuel(s)) — {visuals}"
        )

    async def run(self, params: CreateReportInput, ctx: ToolContext) -> ToolOutput:
        # Schéma relu au moment d'écrire : jamais de rapport sur des colonnes supposées
        profile = await self.data_via().get_dataset(params.dataset_id)
        errors = [e for spec in params.visuals for e in _check_visual(spec, profile)]
        if errors:
            return ToolOutput(content="Rapport refusé :\n- " + "\n- ".join(errors), is_error=True)

        types = {c.name: c.type for c in profile.columns}
        sent_roles = [_visual_roles(s, types) for s in params.visuals]
        proposed: list[dict[str, Any]] = [
            {"type": s.type, "title": s.title, "roles": roles}
            for s, roles in zip(params.visuals, sent_roles, strict=True)
        ]
        checked = await self.client.repair_visuals(profile.id, list(VIZ_CATALOG), proposed)
        problems = [
            f"« {spec.title} » : {'; '.join(r.errors)}"
            for spec, r in zip(params.visuals, checked.results, strict=False)
            if r.visual is None
        ]
        if problems or len(checked.results) != len(params.visuals):
            detail = "\n- ".join(problems) or "réponse incomplète d'Atelier Viz"
            return ToolOutput(
                content=f"Atelier Viz refuse ces visuels (rien n'a été créé) :\n- {detail}",
                is_error=True,
            )
        # Fermé par défaut : la validation de Viz CORRIGE en silence (mesure en trop tronquée,
        # agrégation remplacée, champ ignoré). L'utilisateur a validé CES visuels : si Viz
        # changerait quoi que ce soit, rien n'est écrit et le modèle repropose.
        changed = []
        for spec, roles, result in zip(params.visuals, sent_roles, checked.results, strict=True):
            assert result.visual is not None
            if result.warnings or _bindings(roles) != _bindings(result.visual.roles):
                detail = "; ".join(result.warnings) or "rôles modifiés par la validation de Viz"
                changed.append(f"« {spec.title} » : {detail}")
        if changed:
            return ToolOutput(
                content="Atelier Viz modifierait ces visuels (rien n'a été créé) ; corrige-les "
                "et repropose le rapport :\n- " + "\n- ".join(changed),
                is_error=True,
            )

        visuals: list[dict[str, Any]] = []
        for index, result in enumerate(checked.results):
            v = result.visual
            assert v is not None  # vérifié ci-dessus
            visuals.append(
                {
                    "id": _short_id("v"),
                    "type": v.type,
                    "title": v.title,
                    "layout": _layout(index),
                    "dataset_id": profile.id,
                    "roles": {
                        role: [b.model_dump(exclude_none=True) for b in bindings]
                        for role, bindings in v.roles.items()
                    },
                    "options": {**VISUAL_DEFAULTS.get(v.type, {}), **v.options},
                    "filters": [],
                }
            )
        rows = (len(visuals) + 1) // 2
        height = max(720, 2 * _MARGIN + rows * _CELL_H + (rows - 1) * _GAP)
        report = await self.client.create_report(params.name, profile.id)
        body = {
            "name": params.name,
            "dataset_id": profile.id,
            "pages": [
                {
                    "id": _short_id("p"),
                    "name": "Synthèse",
                    "visuals": visuals,
                    "filters": [],
                    "size": {"width": PAGE_WIDTH, "height": height},
                }
            ],
            "theme": {},
            "bookmarks": [],
        }
        saved = await self.client.save_report(report.id, body)
        return ToolOutput(
            content=f"Rapport « {saved.name} » créé dans Atelier Viz (id {saved.id}), "
            f"{len(visuals)} visuel(s) sur la page « Synthèse ». À ouvrir dans Atelier Viz › Rapports.",
            data={
                "report_id": saved.id,
                "name": saved.name,
                "dataset_id": profile.id,
                "visuals": [{"type": v["type"], "title": v["title"]} for v in visuals],
            },
        )


def viz_write_tools(
    client: VizWriteClient, files: FileGetter, data_via: Callable[[], DataViaClient]
) -> list[BaseTool[Any]]:
    """Écritures dans Atelier Viz, toutes soumises à la validation de l'utilisateur."""
    return [ImportFileTool(client, files, data_via), CreateReportTool(client, files, data_via)]
