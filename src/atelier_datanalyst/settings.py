"""Réglages propres à l'extension, lus dans le même `.env` que Nexus (champs inconnus ignorés).

Les adresses d'Atelier Viz, Projets et Trésorerie viennent des réglages de Nexus
(`DATA_VIA_BASE_URL`, `PROJETS_BASE_URL`, `TRESORERIE_BASE_URL`) : une seule source de vérité.
Celle d'Atelier Convert aussi quand Nexus la connaît (`CONVERT_BASE_URL`, nœud des workflows) :
`convert_url` la lit dans les réglages de Nexus (rechargés à chaud depuis la roue dentée), et
retombe sur ce fichier pour un Nexus plus ancien.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatAnalystSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    #: Atelier Convert (docs/SUITE.md §2 : API sur 8010) ; vide = connecteur désactivé
    convert_base_url: str | None = "http://127.0.0.1:8010/api"
    #: Délai d'une conversion (PDF scanné : reconnaissance de caractères)
    convert_timeout: float = 120.0

    @field_validator("convert_base_url", mode="before")
    @classmethod
    def _empty_is_none(cls, value: object) -> object:
        return None if value == "" else value


@lru_cache
def get_settings() -> DatAnalystSettings:
    return DatAnalystSettings()


_UNSET = object()


def convert_url(nexus_settings: object) -> str | None:
    """Adresse d'Atelier Convert : celle de Nexus s'il la déclare (même variable, rechargée à
    chaud), sinon celle de l'extension."""
    value = getattr(nexus_settings, "convert_base_url", _UNSET)
    if value is _UNSET:
        return get_settings().convert_base_url
    return value or None  # type: ignore[return-value]
