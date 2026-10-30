"""Instâncias compartilhadas da aplicação.

Mantidas num módulo próprio para que as rotas não precisem importar umas às
outras e para que os testes possam trocar a pasta de dados antes de montar o app.
"""

from __future__ import annotations

from functools import lru_cache

from .config import SettingsStore, data_root
from .core.storage import ProjectStore


@lru_cache(maxsize=1)
def get_store() -> ProjectStore:
    return ProjectStore(data_root() / "projects")


@lru_cache(maxsize=1)
def get_settings_store() -> SettingsStore:
    return SettingsStore()


def reset_caches() -> None:
    """Usado pelos testes depois de mudar INTERLINEAR_DATA_DIR."""
    get_store.cache_clear()
    get_settings_store.cache_clear()
