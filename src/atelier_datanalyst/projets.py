"""Atelier Projets : créer un plan de travail complet en une validation (extension Agent_DatAnalyst).

Routes d'Atelier Projets lues dans son dépôt (`routes/projects.ts`, `hierarchy.ts`,
`work-items.ts`) : `POST /projects`, `POST /projects/{id}/phases`, `POST /phases/{id}/epics`,
`POST /epics/{id}/stories`, `POST /stories/{id}/work-items`. Les éléments réutilisent le client et
les schémas du connecteur Projets de Nexus.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Literal

from app.schemas.projets import Priority, Project, WorkItemCreate, WorkItemType
from app.tools.base import BaseTool, ToolContext, ToolInput, ToolOutput
from app.tools.http import AppError, safe_id
from app.tools.projets import ProjetsClient, work_item_record
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    startDate: date | None = None
    dueDate: date | None = None


class ContainerCreate(BaseModel):
    """Phase, epic ou story (`ContainerCreateSchema` d'Atelier Projets)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    description: str | None = None


class Container(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int
    title: str


class PlanClient(ProjetsClient):
    """Client Projets de Nexus (`from_settings` hérité) + création de projet et d'arborescence."""

    async def create_project(self, body: ProjectCreate) -> Project:
        data = await self.request(
            "POST", "projects", json_body=body.model_dump(mode="json", exclude_none=True)
        )
        return self.parse(Project, data)

    async def create_container(
        self, level: Literal["phase", "epic", "story"], parent_id: int, body: ContainerCreate
    ) -> Container:
        path = {
            "phase": f"projects/{safe_id(parent_id)}/phases",
            "epic": f"phases/{safe_id(parent_id)}/epics",
            "story": f"epics/{safe_id(parent_id)}/stories",
        }[level]
        data = await self.request(
            "POST", path, json_body=body.model_dump(mode="json", exclude_none=True)
        )
        return self.parse(Container, data)


class PlanTask(ToolInput):
    title: str = Field(min_length=1, max_length=200)
    type: WorkItemType = "TASK"
    description: str | None = Field(
        default=None, max_length=4000, description="Markdown : objectif + critères d'acceptation."
    )
    priority: Priority | None = None
    estimated_hours: float | None = Field(default=None, gt=0, le=10_000)
    due_date: date | None = None


class CreatePlanInput(ToolInput):
    project_id: int | None = Field(
        default=None,
        ge=1,
        description="Projet existant (projets_list_projects) ; sinon project_name.",
    )
    project_name: str | None = Field(
        default=None, min_length=1, max_length=200, description="Nom d'un NOUVEAU projet."
    )
    project_description: str | None = Field(default=None, max_length=4000)
    phase: str = Field(default="Plan d'action", min_length=1, max_length=200)
    epic: str = Field(
        min_length=1, max_length=200, description="Thème du lot (ex. « Analyse des ventes »)."
    )
    story: str = Field(min_length=1, max_length=200, description="Livrable attendu.")
    tasks: list[PlanTask] = Field(min_length=1, max_length=30)

    @model_validator(mode="after")
    def _one_project(self) -> CreatePlanInput:
        if (self.project_id is None) == (self.project_name is None):
            raise ValueError(
                "Donner soit project_id (projet existant), soit project_name (nouveau)."
            )
        return self


SHOWN_TASKS = 8


class CreatePlanTool(BaseTool[CreatePlanInput]):
    name = "projets_create_plan"
    description = (
        "Crée un plan de travail complet dans Atelier Projets : (nouveau projet ou projet "
        "existant) › phase › epic › story › 1 à 30 tâches. Pour des tâches tirées d'un fichier ou "
        "d'une analyse. Une seule validation de l'utilisateur pour l'ensemble."
    )
    input_model = CreatePlanInput
    requires_approval = True
    output_format = "json"
    expected_errors = (AppError,)

    def __init__(self, client: PlanClient) -> None:
        self.client = client

    def approval_summary(self, params: CreatePlanInput) -> str:
        where = (
            f"le nouveau projet « {params.project_name} »"
            if params.project_name
            else f"le projet {params.project_id}"
        )
        titles = ", ".join(f"« {t.title} »" for t in params.tasks[:SHOWN_TASKS])
        more = (
            f" et {len(params.tasks) - SHOWN_TASKS} autre(s)"
            if len(params.tasks) > SHOWN_TASKS
            else ""
        )
        return (
            f"Créer dans {where} : phase « {params.phase} » › epic « {params.epic} » › story "
            f"« {params.story} », avec {len(params.tasks)} élément(s) : {titles}{more}"
        )

    async def run(self, params: CreatePlanInput, ctx: ToolContext) -> ToolOutput:
        created: list[str] = []
        try:
            if params.project_name:
                project = await self.client.create_project(
                    ProjectCreate(name=params.project_name, description=params.project_description)
                )
                project_id = project.id
                created.append(f"projet #{project.id} « {project.name} »")
            else:
                assert params.project_id is not None
                project_id = params.project_id
            phase = await self.client.create_container(
                "phase", project_id, ContainerCreate(title=params.phase)
            )
            created.append(f"phase #{phase.id}")
            epic = await self.client.create_container(
                "epic", phase.id, ContainerCreate(title=params.epic)
            )
            created.append(f"epic #{epic.id}")
            story: Container = await self.client.create_container(
                "story", epic.id, ContainerCreate(title=params.story)
            )
            created.append(f"story #{story.id}")
            items = []
            for task in params.tasks:
                item = await self.client.create_work_item(
                    story.id,
                    WorkItemCreate(
                        type=task.type,
                        title=task.title,
                        description=task.description,
                        priority=task.priority,
                        estimatedHours=task.estimated_hours,
                        dueDate=task.due_date,
                    ),
                )
                items.append(item)
                created.append(f"{item.type} #{item.id}")
        except AppError as exc:
            done = ", ".join(created) or "rien"
            return ToolOutput(
                content=f"Plan interrompu : {exc} Déjà créé : {done}. Ne relance pas tout : "
                "complète avec projets_create_work_item.",
                is_error=True,
            )
        lines = [
            f"Plan créé dans le projet {project_id} : phase « {params.phase} » › epic "
            f"« {params.epic} » › story #{story.id} « {params.story} », {len(items)} élément(s)."
        ]
        lines += [f"- #{i.id} {i.type} « {i.title} »" for i in items[:SHOWN_TASKS]]
        return ToolOutput(
            content="\n".join(lines),
            data={
                "project_id": project_id,
                "story_id": story.id,
                "work_items": [work_item_record(i) for i in items],
            },
        )


def projets_plan_tools(client: PlanClient) -> list[BaseTool[Any]]:
    return [CreatePlanTool(client)]
