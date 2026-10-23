"""Réglages propres à l'extension, lus dans le même `.env` que Nexus (champs inconnus ignorés).

Les adresses d'Atelier Viz, Projets et Trésorerie viennent des réglages de Nexus
(`DATA_VIA_BASE_URL`, `PROJETS_BASE_URL`, `TRESORERIE_BASE_URL`) : une seule source de vérité.
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
