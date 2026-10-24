"""L'extension vue depuis Nexus : chargement, catalogue, agents, garde-fous."""

from __future__ import annotations

from typing import Any

import pytest
from app.agents import AgentCatalog
from app.agents.registry import describe, list_agent_classes
from app.core.config import Settings
from app.core.llm.errors import ProviderNotAllowedError
from app.plugins import load_plugins
from app.tools.environment import ToolEnvironment
from app.tools.labels import TOOL_LABELS
from app.tools.toolsets import build_tools, get_toolset, is_sensitive

import atelier_datanalyst
from atelier_datanalyst import settings as ext_settings
from tests.fakes_router import make_router


def agent(name: str) -> Any:
    return next(cls for cls in list_agent_classes() if cls.name == name)


def env(**overrides: Any) -> ToolEnvironment:
    return ToolEnvironment(Settings(_env_file=None, **overrides))  # type: ignore[call-arg]


def test_nexus_loads_the_extension_once() -> None:
    (state,) = load_plugins(["atelier_datanalyst"])
    assert state.loaded and state.error is None
    atelier_datanalyst.register()  # appel répété : sans effet (pas de double enregistrement)
    names = [cls.name for cls in list_agent_classes()]
    # Le Data Analyst de Nexus est complété, pas doublé ; le Trésorier est un agent à part
    assert "datanalyst" not in names and names.count("tresorier") == 1
    assert agent("data_analyst").toolsets.count("convert_write") == 1


async def test_agents_appear_in_the_catalog_after_the_builtin_ones() -> None:
    infos = await AgentCatalog().infos(make_router(), env())
    names = [i.name for i in infos]
    assert names[0] == "assistant" and {"data_analyst", "tresorier"} <= set(names)
    analyst = next(i for i in infos if i.name == "data_analyst")
    assert analyst.category == "Données"
    assert any("rapport Atelier Viz" in s for s in analyst.starters)


def test_data_analyst_runs_on_claude_and_never_touches_tresorerie() -> None:
    cls = agent("data_analyst")
    assert not is_sensitive(list(cls.toolsets))
    assert not any(get_toolset(t).app == "tresorerie" for t in cls.toolsets)
    for needed in ("analyse", "fichiers", "convert_write", "data_via_write", "tab", "projets_plan"):
        assert needed in cls.toolsets
    assert "Trésorier" in cls.system_prompt  # oriente les finances vers l'agent local


def test_tresorier_is_locked_on_the_local_model() -> None:
    cls = agent("tresorier")
    assert is_sensitive(list(cls.toolsets))
    instance = cls(make_router())
    assert describe(instance).local_only
    with pytest.raises(ProviderNotAllowedError):
        instance.resolve_provider("anthropic")


async def test_every_write_needs_approval_and_every_tool_has_a_label() -> None:
    ids = [
        "fichiers",
        "convert",
        "convert_write",
        "data_via_write",
        "projets_plan",
        "tresorerie_write",
    ]
    e = env(
        data_via_base_url="http://viz.test/api",
        projets_base_url="http://projets.test/api",
        tresorerie_base_url="http://treso.test/api",
    )
    tools, unavailable = build_tools(ids, e)
    assert unavailable == []
    for tool in tools:
        assert tool.name in TOOL_LABELS, tool.name
    by_name = {t.name: t for t in tools}
    read_only = {"conversation_files", "convert_list_conversions", "convert_read_table",
                 "convert_tab_projects", "tresorerie_preview_import"}  # fmt: skip
    assert {n for n, t in by_name.items() if not t.requires_approval} == read_only
    treso = [t for t in tools if t.name.startswith("tresorerie_")]
    assert treso and all(t.sensitive for t in treso)
    await e.aclose()


async def test_unconfigured_convert_is_skipped_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONVERT_BASE_URL", "")
    ext_settings.get_settings.cache_clear()
    try:
        e = env()
        tools, unavailable = build_tools(["fichiers", "convert", "convert_write"], e)
        assert [t.name for t in tools] == ["conversation_files"]
        assert {u.id for u in unavailable} == {"convert", "convert_write"}
        await e.aclose()
    finally:
        ext_settings.get_settings.cache_clear()


async def test_convert_address_comes_from_nexus_and_must_be_local() -> None:
    """Adresse de Convert : celle des réglages de Nexus quand il la déclare (rechargée à chaud),
    sinon celle de l'extension ; une adresse non locale écarte la boîte avec la raison (relevés
    et fichiers ne quittent pas la machine), sans casser l'agent."""
    from types import SimpleNamespace

    from atelier_datanalyst.settings import convert_url

    assert convert_url(SimpleNamespace(convert_base_url="http://127.0.0.1:9010/api")) == (
        "http://127.0.0.1:9010/api"
    )
    assert convert_url(SimpleNamespace(convert_base_url="")) is None  # vidé dans la roue dentée
    assert convert_url(SimpleNamespace()) == ext_settings.get_settings().convert_base_url
    if "convert_base_url" not in Settings.model_fields:
        pytest.skip("Nexus sans CONVERT_BASE_URL : seule l'adresse de l'extension compte")
    e = env(convert_base_url="https://convert.example.org/api")
    tools, unavailable = build_tools(["convert", "convert_write"], e)
    assert tools == [] and {u.id for u in unavailable} == {"convert", "convert_write"}
    assert all("adresse locale" in u.reason for u in unavailable)
    await e.aclose()


def test_statements_box_is_sensitive_and_only_for_local_agents() -> None:
    from app.agents.registry import get_agent_class

    assert get_toolset("convert_releves").sensitive and not get_toolset("convert").sensitive
    treasurer = get_agent_class("tresorier")
    assert "convert_releves" in treasurer.toolsets and "convert" not in treasurer.toolsets
    assert treasurer.privacy == "local_only"
    for cls in list_agent_classes():
        if "convert_releves" in cls.toolsets:
            assert cls.privacy == "local_only", cls.name
