"""Interface comum para provedores de IA.

Todos os provedores recebem mensagens no formato de papéis (system/user) e
devolvem texto. Isolar isso numa interface significa que o tradutor não sabe
nem se importa se está falando com a OpenAI, com a Anthropic ou com um modelo
rodando em localhost.
"""

from __future__ import annotations

import asyncio
import logging
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)


class ProviderError(RuntimeError):
    """Falha ao falar com o provedor."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class ProviderConfig:
    """Configuração de um provedor, vinda das preferências do usuário."""

    kind: str  # "openai" | "anthropic" | "google" | "openai_compat" | "ollama"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    temperature: float = 0.2
    max_tokens: int = 4096
    timeout: float = 180.0
    extra_headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
        )


@dataclass
class Completion:
    text: str
    usage: Usage = field(default_factory=Usage)


class Provider(ABC):
    """Contrato que todo provedor implementa."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @abstractmethod
    async def complete(
        self, messages: list[Message], *, client: httpx.AsyncClient
    ) -> Completion:
        """Envia as mensagens e devolve a resposta do modelo."""

    @abstractmethod
    async def list_models(self, *, client: httpx.AsyncClient) -> list[str]:
        """Lista os modelos disponíveis, para preencher o seletor da interface."""

    # -- utilidades compartilhadas ---------------------------------------

    async def complete_with_retry(
        self,
        messages: list[Message],
        *,
        client: httpx.AsyncClient,
        attempts: int = 4,
    ) -> Completion:
        """Tenta de novo com espera exponencial em falhas temporárias.

        Limites de taxa e instabilidade de rede são a regra, não a exceção,
        quando se traduz um livro inteiro. Desistir no primeiro 429 jogaria
        fora horas de trabalho.
        """
        delay = 2.0
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return await self.complete(messages, client=client)
            except ProviderError as exc:
                last_error = exc
                if not exc.retryable or attempt == attempts:
                    raise
                log.warning(
                    "Tentativa %d/%d falhou (%s). Nova tentativa em %.1fs",
                    attempt, attempts, exc, delay,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc
                if attempt == attempts:
                    raise ProviderError(f"Falha de rede: {exc}", retryable=True) from exc
                log.warning("Erro de rede (%s). Nova tentativa em %.1fs", exc, delay)

            # Jitter evita que várias requisições voltem ao mesmo tempo.
            await asyncio.sleep(delay + random.uniform(0, delay * 0.3))
            delay = min(delay * 2, 60.0)

        raise ProviderError(f"Esgotadas as tentativas: {last_error}")


def classify_status(status: int) -> bool:
    """Diz se vale a pena tentar de novo para um dado código HTTP."""
    return status == 429 or 500 <= status < 600


def extract_error_message(payload: Any, fallback: str) -> str:
    """Tira a mensagem de erro de formatos de resposta variados."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("type") or fallback)
        if isinstance(error, str):
            return error
        if "message" in payload:
            return str(payload["message"])
    return fallback
