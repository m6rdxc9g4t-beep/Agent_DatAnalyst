"""Outil `conversation_files` : les fichiers glissés dans la conversation.

L'accès lui-même (`env.conversation_files()`) est fourni par Nexus, qui limite les fichiers à
ceux cités par la conversation en cours. Le contenu n'est jamais renvoyé au modèle ici.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.tools.base import BaseTool, ToolContext, ToolInput, ToolOutput
from app.tools.files import ConversationFiles
from app.tools.http import AppError
from pydantic import Field

FileGetter = Callable[[], ConversationFiles]


def file_field(
    description: str = "Nom du fichier joint (ou son identifiant, voir conversation_files).",
) -> Any:
    return Field(min_length=1, max_length=255, description=description)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} o"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} Ko"
    return f"{size / (1024 * 1024):.1f} Mo"


class NoInput(ToolInput):
    pass


class ConversationFilesTool(BaseTool[NoInput]):
    name = "conversation_files"
    description = (
        "Liste les fichiers joints à la conversation (nom, type, taille). À appeler avant "
        "d'envoyer un fichier à une application de la suite."
    )
    input_model = NoInput
    expected_errors = (AppError,)
    output_format = "json"

    def __init__(self, files: FileGetter) -> None:
        self.files = files

    async def run(self, params: NoInput, ctx: ToolContext) -> ToolOutput:
        infos = await self.files().list()
        if not infos:
            return ToolOutput(content="Aucun fichier joint à cette conversation.", data=[])
        lines = [f"{len(infos)} fichier(s) joint(s) :"]
        for info in infos:
            details = [info.media_type, human_size(info.size)]
            details += [
                f"{k} = {info.meta[k]}" for k in ("rows", "sheets", "pages") if k in info.meta
            ]
            lines.append(f"- « {info.name} » (id {info.id}) — {', '.join(details)}")
        data = [
            {"id": i.id, "name": i.name, "media_type": i.media_type, "size": i.size} for i in infos
        ]
        return ToolOutput(content="\n".join(lines), data=data)


def files_tools(files: FileGetter) -> list[BaseTool[Any]]:
    return [ConversationFilesTool(files)]
