"""Configuração da aplicação e guarda das chaves de API.

As chaves ficam num arquivo local com permissão 0600, fora do repositório, e
só saem da máquina para o endpoint do próprio provedor. A API nunca devolve a
chave inteira para a interface: devolve só os últimos caracteres, o suficiente
para o usuário reconhecer qual chave está ali.

Quem preferir não gravar chave nenhuma pode definir as variáveis de ambiente
INTERLINEAR_OPENAI_KEY, INTERLINEAR_ANTHROPIC_KEY e INTERLINEAR_GOOGLE_KEY, que têm prioridade.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

APP_NAME = "interlinear"

_ENV_KEYS = {
    "openai": "INTERLINEAR_OPENAI_KEY",
    "anthropic": "INTERLINEAR_ANTHROPIC_KEY",
    "google": "INTERLINEAR_GOOGLE_KEY",
    "openai_compat": "INTERLINEAR_COMPAT_KEY",
}


def data_root() -> Path:
    """Pasta de dados. Respeita INTERLINEAR_DATA_DIR quando definida."""
    override = os.environ.get("INTERLINEAR_DATA_DIR")
    if override:
        return Path(override).expanduser()

    # O lugar convencional muda por sistema: Application Support no macOS,
    # %APPDATA% no Windows e XDG no resto. os.uname() não existe no Windows.
    home = Path.home()
    if sys.platform == "darwin":
        return home / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / APP_NAME


@dataclass
class ProviderProfile:
    """Um provedor configurado pelo usuário, pronto para usar."""

    id: str
    label: str
    kind: str
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096

    def redacted(self) -> dict[str, Any]:
        data = asdict(self)
        key = data.pop("api_key", "") or ""
        data["has_key"] = bool(key)
        data["key_hint"] = f"…{key[-4:]}" if len(key) >= 4 else ""
        return data


@dataclass
class Settings:
    providers: list[ProviderProfile] = field(default_factory=list)
    active_provider_id: str = ""
    # Preferências de tradução que persistem entre projetos.
    source_language: str = "inglês"
    target_language: str = "português brasileiro"
    style_notes: str = ""
    # Glossário global: termos que devem sair iguais em todo o livro.
    glossary: dict[str, str] = field(default_factory=dict)
    batch_chars: int = 3500
    concurrency: int = 3
    context_blocks: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "providers": [asdict(p) for p in self.providers],
            "active_provider_id": self.active_provider_id,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "style_notes": self.style_notes,
            "glossary": self.glossary,
            "batch_chars": self.batch_chars,
            "concurrency": self.concurrency,
            "context_blocks": self.context_blocks,
        }

    def redacted(self) -> dict[str, Any]:
        data = self.to_dict()
        data["providers"] = [p.redacted() for p in self.providers]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Settings":
        known = {f for f in ProviderProfile.__dataclass_fields__}
        providers = [
            ProviderProfile(**{k: v for k, v in item.items() if k in known})
            for item in data.get("providers", [])
            if isinstance(item, dict) and item.get("id")
        ]
        return cls(
            providers=providers,
            active_provider_id=data.get("active_provider_id", ""),
            source_language=data.get("source_language", "inglês"),
            target_language=data.get("target_language", "português brasileiro"),
            style_notes=data.get("style_notes", ""),
            glossary={
                str(k): str(v)
                for k, v in (data.get("glossary") or {}).items()
            },
            batch_chars=int(data.get("batch_chars", 3500)),
            concurrency=int(data.get("concurrency", 3)),
            context_blocks=int(data.get("context_blocks", 1)),
        )

    def get_provider(self, provider_id: str | None = None) -> ProviderProfile | None:
        target = provider_id or self.active_provider_id
        if not target:
            return self.providers[0] if self.providers else None
        return next((p for p in self.providers if p.id == target), None)

    def resolved_key(self, profile: ProviderProfile) -> str:
        """Chave efetiva: variável de ambiente tem prioridade sobre o arquivo."""
        env_name = _ENV_KEYS.get(profile.kind)
        if env_name:
            from_env = os.environ.get(env_name)
            if from_env:
                return from_env
        return profile.api_key


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (data_root() / "settings.json")

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            return Settings.from_dict(json.loads(self.path.read_text("utf-8")))
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            # Preferências corrompidas não devem impedir o app de abrir.
            return Settings()

    def save(self, settings: Settings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(settings.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Só o dono lê e escreve: o arquivo guarda chaves de API.
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
