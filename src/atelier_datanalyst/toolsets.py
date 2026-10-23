"""Boîtes à outils de l'extension, enregistrées dans le catalogue de Nexus."""

from __future__ import annotations

from app.suite.apps import APP_COLORS, APP_NAMES
from app.tools.environment import ToolEnvironment
from app.tools.labels import TOOL_LABELS
from app.tools.toolsets import Toolset, register_toolset

from atelier_datanalyst.convert import ConvertClient, convert_read_tools, convert_write_tools
from atelier_datanalyst.files import files_tools
from atelier_datanalyst.projets import PlanClient, projets_plan_tools
from atelier_datanalyst.settings import get_settings
from atelier_datanalyst.tresorerie import TresoWriteClient, tresorerie_write_tools
from atelier_datanalyst.viz import VizWriteClient, viz_write_tools

#: Libellés lisibles (cartes de validation, `APPROVAL_MODE=every_step`)
LABELS = {
    "conversation_files": "Fichiers joints",
    "convert_list_conversions": "Convert · conversions",
    "convert_read_table": "Convert · lecture d'une table",
    "convert_tab_projects": "Convert · projets d'Atelier Tab",
    "convert_file": "Convert · conversion d'un fichier",
    "convert_send_to_tab": "Convert · envoi vers Tab",
    "convert_send_to_viz": "Convert · envoi vers Viz",
    "data_via_import_file": "Data Via · import d'un fichier",
    "data_via_create_report": "Data Via · création d'un rapport",
    "projets_create_plan": "Projets · création d'un plan",
    "tresorerie_preview_import": "Trésorerie · aperçu d'import",
    "tresorerie_create_account": "Trésorerie · création d'un compte",
    "tresorerie_add_transaction": "Trésorerie · saisie d'une opération",
    "tresorerie_add_holding": "Trésorerie · ajout d'un placement",
    "tresorerie_import_statement": "Trésorerie · import d'un relevé",
}


def _convert(env: ToolEnvironment) -> ConvertClient:
    def make() -> ConvertClient:
        client = ConvertClient.from_settings(get_settings(), env.transport("convert"))
        if client is None:
            from app.tools.http import AppNotConfiguredError

            raise AppNotConfiguredError("CONVERT_BASE_URL n'est pas défini.", app="convert")
        return client

    return env.client("convert", make)


def _viz(env: ToolEnvironment) -> VizWriteClient:
    def make() -> VizWriteClient:
        client = VizWriteClient.from_settings(env.settings, env.transport("data_via"))
        if client is None:
            from app.tools.http import AppNotConfiguredError

            raise AppNotConfiguredError("DATA_VIA_BASE_URL n'est pas défini.", app="viz")
        return client

    return env.client("viz_write", make)


def _plan(env: ToolEnvironment) -> PlanClient:
    def make() -> PlanClient:
        # `from_settings` hérité de ProjetsClient construit bien un PlanClient (cls(...))
        client = PlanClient.from_settings(env.settings, env.transport("projets"))
        if not isinstance(client, PlanClient):
            from app.tools.http import AppNotConfiguredError

            raise AppNotConfiguredError("PROJETS_BASE_URL n'est pas défini.", app="projets")
        return client

    return env.client("projets_plan", make)


def _treso(env: ToolEnvironment) -> TresoWriteClient:
    def make() -> TresoWriteClient:
        client = TresoWriteClient.from_settings(env.settings, env.transport("tresorerie"))
        if not isinstance(client, TresoWriteClient):
            from app.tools.http import AppNotConfiguredError

            raise AppNotConfiguredError("TRESORERIE_BASE_URL n'est pas défini.", app="tresorerie")
        return client

    return env.client("tresorerie_write", make)


def register_toolsets() -> None:
    TOOL_LABELS.update(LABELS)
    APP_NAMES.setdefault("convert", "Atelier Convert")
    APP_COLORS.setdefault("convert", "#C9951D")  # docs/SUITE.md §1.7 : or bronze

    register_toolset(
        Toolset(
            id="fichiers",
            label="Fichiers joints",
            description="Fichiers glissés dans la conversation, à envoyer aux applications de la suite.",
            build=lambda env: files_tools(env.conversation_files),
        )
    )
    register_toolset(
        Toolset(
            id="convert",
            label="Atelier Convert",
            description="Conversions de fichiers (PDF, relevés, Excel) en tables typées, projets d'Atelier Tab, en lecture.",
            build=lambda env: convert_read_tools(_convert(env)),
            is_configured=lambda _s: bool(get_settings().convert_base_url),
            app="convert",
        )
    )
    register_toolset(
        Toolset(
            id="convert_write",
            label="Atelier Convert — conversions et envois",
            description="Convertir un fichier joint, envoyer une conversion dans Atelier Tab ou Atelier Viz (avec validation).",
            build=lambda env: convert_write_tools(
                _convert(env), env.conversation_files, lambda: _viz(env)
            ),
            is_configured=lambda _s: bool(get_settings().convert_base_url),
            app="convert",
            writes=True,
        )
    )
    register_toolset(
        Toolset(
            id="data_via_write",
            label="Atelier Viz — import et rapports",
            description="Importer un fichier joint comme jeu de données, créer un rapport avec ses visuels (avec validation).",
            build=lambda env: viz_write_tools(_viz(env), env.conversation_files, env.data_via),
            is_configured=lambda s: bool(s.data_via_base_url),
            app="viz",
            writes=True,
        )
    )
    register_toolset(
        Toolset(
            id="projets_plan",
            label="Atelier Projets — plan de travail",
            description="Créer projet, phase, epic, story et tâches en une seule validation.",
            build=lambda env: projets_plan_tools(_plan(env)),
            is_configured=lambda s: bool(s.projets_base_url),
            app="projets",
            writes=True,
        )
    )
    register_toolset(
        Toolset(
            id="tresorerie_write",
            label="Atelier Trésorerie — saisies",
            description="Créer un compte, saisir une opération, ajouter un placement (titres, métaux), importer un relevé (avec validation). Données sensibles : modèle local imposé.",
            build=lambda env: tresorerie_write_tools(
                _treso(env), env.conversation_files, lambda: _convert(env)
            ),
            is_configured=lambda s: bool(s.tresorerie_base_url),
            app="tresorerie",
            sensitive=True,
            writes=True,
        )
    )
