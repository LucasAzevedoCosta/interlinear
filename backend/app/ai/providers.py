"""Implementações concretas dos provedores de IA.

Quatro formatos de API cobrem praticamente tudo que existe hoje:

  openai_compat  OpenAI, OpenRouter, Groq, DeepSeek, Together, LM Studio,
                 vLLM e qualquer endpoint que fale o dialeto /chat/completions
  anthropic      API da Anthropic (formato /v1/messages)
  google         Gemini (formato :generateContent)
  ollama         Ollama local, que fala openai_compat mas lista modelos à parte

Nenhuma chave é enviada para lugar nenhum além do endpoint do próprio provedor.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from .base import (
    Completion,
    Message,
    PlanLimitError,
    Provider,
    ProviderConfig,
    ProviderError,
    Usage,
    classify_status,
    extract_error_message,
)

# Servidores locais. Dentro de um container, "localhost" é o próprio container,
# então o Docker aponta essas variáveis para host.docker.internal.
OLLAMA_URL = os.environ.get("INTERLINEAR_OLLAMA_URL", "http://localhost:11434/v1")
LMSTUDIO_URL = os.environ.get("INTERLINEAR_LMSTUDIO_URL", "http://localhost:1234/v1")

# --------------------------------------------------------------------------
# Dialeto OpenAI
# --------------------------------------------------------------------------


class OpenAICompatProvider(Provider):
    """Qualquer endpoint compatível com /v1/chat/completions."""

    default_base_url = "https://api.openai.com/v1"

    @property
    def base_url(self) -> str:
        return (self.config.base_url or self.default_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        headers.update(self.config.extra_headers)
        return headers

    async def complete(
        self, messages: list[Message], *, client: httpx.AsyncClient
    ) -> Completion:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }

        response = await client.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
            timeout=self.config.timeout,
        )

        if response.status_code >= 400:
            raise ProviderError(
                _error_text(response),
                status=response.status_code,
                retryable=classify_status(response.status_code),
            )

        data = response.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"Resposta em formato inesperado: {data}") from exc

        usage_data = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_data.get("prompt_tokens") or 0),
            output_tokens=int(usage_data.get("completion_tokens") or 0),
        )
        return Completion(text=text, usage=usage)

    async def list_models(self, *, client: httpx.AsyncClient) -> list[str]:
        response = await client.get(
            f"{self.base_url}/models", headers=self._headers(), timeout=30.0
        )
        if response.status_code >= 400:
            raise ProviderError(_error_text(response), status=response.status_code)

        data = response.json()
        items = data.get("data") if isinstance(data, dict) else data
        if not isinstance(items, list):
            return []
        return sorted(
            str(item.get("id"))
            for item in items
            if isinstance(item, dict) and item.get("id")
        )


class OllamaProvider(OpenAICompatProvider):
    """Ollama local. Completa pelo endpoint compatível, lista pelo nativo."""

    default_base_url = OLLAMA_URL

    async def list_models(self, *, client: httpx.AsyncClient) -> list[str]:
        root = self.base_url.removesuffix("/v1")
        response = await client.get(f"{root}/api/tags", timeout=30.0)
        if response.status_code >= 400:
            raise ProviderError(_error_text(response), status=response.status_code)
        models = response.json().get("models", [])
        return sorted(
            str(m.get("name")) for m in models if isinstance(m, dict) and m.get("name")
        )


class LMStudioProvider(OpenAICompatProvider):
    """LM Studio expõe o servidor local em /v1 sem exigir chave."""

    default_base_url = LMSTUDIO_URL


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class AnthropicProvider(Provider):
    """API da Anthropic. O prompt de sistema vai num campo próprio."""

    default_base_url = "https://api.anthropic.com/v1"
    api_version = "2023-06-01"

    @property
    def base_url(self) -> str:
        return (self.config.base_url or self.default_base_url).rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.config.api_key,
            "anthropic-version": self.api_version,
        }
        headers.update(self.config.extra_headers)
        return headers

    async def complete(
        self, messages: list[Message], *, client: httpx.AsyncClient
    ) -> Completion:
        system_parts = [m.content for m in messages if m.role == "system"]
        turns = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": turns,
            "max_tokens": self.config.max_tokens,
            "temperature": self.config.temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        response = await client.post(
            f"{self.base_url}/messages",
            headers=self._headers(),
            json=payload,
            timeout=self.config.timeout,
        )

        if response.status_code >= 400:
            raise ProviderError(
                _error_text(response),
                status=response.status_code,
                retryable=classify_status(response.status_code),
            )

        data = response.json()
        blocks = data.get("content") or []
        text = "".join(
            block.get("text", "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

        usage_data = data.get("usage") or {}
        usage = Usage(
            input_tokens=int(usage_data.get("input_tokens") or 0),
            output_tokens=int(usage_data.get("output_tokens") or 0),
        )
        return Completion(text=text, usage=usage)

    async def list_models(self, *, client: httpx.AsyncClient) -> list[str]:
        response = await client.get(
            f"{self.base_url}/models", headers=self._headers(), timeout=30.0
        )
        if response.status_code >= 400:
            raise ProviderError(_error_text(response), status=response.status_code)
        data = response.json().get("data", [])
        return sorted(
            str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")
        )


# --------------------------------------------------------------------------
# Google Gemini
# --------------------------------------------------------------------------


class GoogleProvider(Provider):
    """Gemini. A chave vai como parâmetro de consulta e o formato é próprio."""

    default_base_url = "https://generativelanguage.googleapis.com/v1beta"

    @property
    def base_url(self) -> str:
        return (self.config.base_url or self.default_base_url).rstrip("/")

    async def complete(
        self, messages: list[Message], *, client: httpx.AsyncClient
    ) -> Completion:
        system_parts = [m.content for m in messages if m.role == "system"]
        contents = [
            {
                "role": "model" if m.role == "assistant" else "user",
                "parts": [{"text": m.content}],
            }
            for m in messages
            if m.role in ("user", "assistant")
        ]

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": self.config.temperature,
                "maxOutputTokens": self.config.max_tokens,
            },
        }
        if system_parts:
            payload["systemInstruction"] = {
                "parts": [{"text": "\n\n".join(system_parts)}]
            }

        model = self.config.model.removeprefix("models/")
        response = await client.post(
            f"{self.base_url}/models/{model}:generateContent",
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self.config.api_key,
                **self.config.extra_headers,
            },
            json=payload,
            timeout=self.config.timeout,
        )

        if response.status_code >= 400:
            raise ProviderError(
                _error_text(response),
                status=response.status_code,
                retryable=classify_status(response.status_code),
            )

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            reason = (data.get("promptFeedback") or {}).get("blockReason")
            raise ProviderError(
                f"Resposta vazia do Gemini{f' (bloqueada: {reason})' if reason else ''}"
            )

        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))

        usage_data = data.get("usageMetadata") or {}
        usage = Usage(
            input_tokens=int(usage_data.get("promptTokenCount") or 0),
            output_tokens=int(usage_data.get("candidatesTokenCount") or 0),
        )
        return Completion(text=text, usage=usage)

    async def list_models(self, *, client: httpx.AsyncClient) -> list[str]:
        response = await client.get(
            f"{self.base_url}/models",
            headers={"x-goog-api-key": self.config.api_key},
            timeout=30.0,
        )
        if response.status_code >= 400:
            raise ProviderError(_error_text(response), status=response.status_code)

        models = response.json().get("models", [])
        return sorted(
            str(m.get("name", "")).removeprefix("models/")
            for m in models
            if isinstance(m, dict)
            and "generateContent" in (m.get("supportedGenerationMethods") or [])
        )


# --------------------------------------------------------------------------
# Claude pela assinatura (Claude Code)
# --------------------------------------------------------------------------


# Modelos aceitos pelo Claude Code como apelido do mais recente de cada linha.
CLAUDE_CLI_MODELS = ["haiku", "sonnet", "opus"]

_RESET_EPOCH = re.compile(r"\|\s*(\d{10})\b")
_RESET_CLOCK = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
_LIMIT_WORDS = ("usage limit", "limit reached", "limit will reset", "out of extra usage")
_LOGIN_WORDS = ("/login", "not logged in", "invalid api key", "please log in")


def claude_path() -> str | None:
    return shutil.which(os.environ.get("INTERLINEAR_CLAUDE", "claude"))


class ClaudeCliProvider(Provider):
    """Claude pela assinatura (Pro/Max), sem chave de API. Experimental.

    Não fala com a API: roda o Claude Code oficial, instalado e logado pelo
    próprio usuário, em modo não interativo (`claude -p`), e lê a resposta.
    O Interlinear nunca vê nem guarda o token do login; a Anthropic proíbe
    que terceiros façam isso. Veja docs/provedores.md antes de usar: o uso sai
    dos limites do plano, e as regras da Anthropic para uso programático da
    assinatura mudaram várias vezes.

    Cada chamada roda isolada: sem ferramentas, sem as configurações, hooks,
    servidores MCP e skills do usuário, numa pasta vazia, e com o nosso prompt
    no lugar do prompt do Claude Code. ANTHROPIC_API_KEY sai do ambiente para
    a CLI não cair na cobrança por token.
    """

    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)
        self._checked_account = False

    @property
    def model(self) -> str:
        return self.config.model or "sonnet"

    def _executable(self) -> str:
        path = claude_path()
        if path is None:
            raise ProviderError(
                "Claude Code não encontrado. Instale (claude.com/claude-code) e entre "
                "com a sua conta rodando `claude` no terminal. Dentro do Docker ele não "
                "está disponível: rode o Interlinear direto na máquina.",
                fatal=True,
            )
        return path

    @staticmethod
    def _env() -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if key not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        }

    async def _run(self, args: list[str], stdin: str = "", timeout: float = 30.0) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory(prefix="interlinear-claude-") as folder:
            process = await asyncio.create_subprocess_exec(
                self._executable(),
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=folder,  # sem CLAUDE.md de projeto nenhum
                env=self._env(),
            )
            try:
                out, err = await asyncio.wait_for(
                    process.communicate(stdin.encode()), timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                raise ProviderError(
                    f"O Claude Code não respondeu em {timeout:.0f}s", retryable=True
                ) from None
        return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def account(self) -> dict[str, Any]:
        """A conta em que o Claude Code está logado: plano e forma de login."""
        code, out, err = await self._run(["auth", "status"])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = {}
        if code != 0 and not data:
            raise ProviderError(f"Claude Code: {(err or out).strip()[:300]}", fatal=True)
        if not data.get("loggedIn"):
            raise ProviderError(
                "O Claude Code não está logado. Rode `claude` no terminal e entre com a sua conta.",
                fatal=True,
            )
        if data.get("authMethod") != "claude.ai":
            raise ProviderError(
                "O Claude Code está entrando com chave de API, não com a assinatura. "
                "Para usar a chave, cadastre o provedor Anthropic; para usar a "
                "assinatura, entre com `claude` → /login → conta Claude.",
                fatal=True,
            )
        return {"plan": data.get("subscriptionType") or "", "method": data.get("authMethod")}

    async def complete(
        self, messages: list[Message], *, client: httpx.AsyncClient
    ) -> Completion:
        # Uma vez por tarefa: a conta não muda no meio de um livro.
        if not self._checked_account:
            await self.account()
            self._checked_account = True

        system = "\n\n".join(m.content for m in messages if m.role == "system")
        prompt = "\n\n".join(m.content for m in messages if m.role != "system")
        code, out, err = await self._run(
            [
                "-p",
                "--output-format", "json",
                "--model", self.model,
                "--system-prompt", system,
                "--tools", "",
                "--setting-sources", "",
                "--strict-mcp-config",
                "--disable-slash-commands",
                "--no-session-persistence",
            ],
            stdin=prompt,
            timeout=self.config.timeout,
        )

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            raise _cli_error((err or out).strip() or f"Claude Code saiu com código {code}")
        if data.get("is_error") or data.get("subtype") != "success":
            raise _cli_error(str(data.get("result") or err or data.get("subtype")),
                             status=data.get("api_error_status"))

        usage = data.get("usage") or {}
        return Completion(
            text=str(data.get("result") or ""),
            usage=Usage(
                input_tokens=int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
            ),
        )

    async def list_models(self, *, client: httpx.AsyncClient) -> list[str]:
        return list(CLAUDE_CLI_MODELS)


def _cli_error(message: str, *, status: Any = None) -> ProviderError:
    """Traduz a mensagem de erro do Claude Code no erro certo para o tradutor."""
    lowered = message.lower()
    if any(word in lowered for word in _LIMIT_WORDS):
        return PlanLimitError(f"Limite do plano: {message[:300]}", resets_at=_reset_time(message))
    if any(word in lowered for word in _LOGIN_WORDS):
        return ProviderError(
            "O Claude Code não está logado. Rode `claude` no terminal e entre com a sua conta.",
            fatal=True,
        )
    code = int(status) if isinstance(status, int) or str(status).isdigit() else None
    return ProviderError(
        f"Claude Code: {message[:300]}",
        status=code,
        retryable=code is None or classify_status(code),
    )


def _reset_time(message: str, now: float | None = None) -> float | None:
    """Quando o limite renova, pelo texto do Claude Code. None se não disser.

    Aceita o formato antigo ("…|1731430800") e o horário ("resets 3pm",
    "resets at 3:30pm"), tomado como hora local da próxima ocorrência.
    """
    if match := _RESET_EPOCH.search(message):
        return float(match.group(1))
    if match := _RESET_CLOCK.search(message):
        hour = int(match.group(1)) % 12 + (12 if match.group(3).lower() == "pm" else 0)
        minute = int(match.group(2) or 0)
        current = datetime.fromtimestamp(now if now is not None else time.time())
        target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= current:
            target += timedelta(days=1)
        return target.timestamp()
    return None


# --------------------------------------------------------------------------
# Registro
# --------------------------------------------------------------------------

_PROVIDERS: dict[str, type[Provider]] = {
    "openai": OpenAICompatProvider,
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
    "ollama": OllamaProvider,
    "lmstudio": LMStudioProvider,
    "claude_cli": ClaudeCliProvider,
}

# Metadados para a interface: o que mostrar e o que pedir ao usuário.
PROVIDER_CATALOG = [
    {
        "kind": "openai",
        "label": "OpenAI",
        "needs_key": True,
        "default_base_url": "https://api.openai.com/v1",
        "hint": "Chave em platform.openai.com",
    },
    {
        "kind": "anthropic",
        "label": "Anthropic",
        "needs_key": True,
        "default_base_url": "https://api.anthropic.com/v1",
        "hint": "Chave no console da Anthropic",
    },
    {
        "kind": "google",
        "label": "Google Gemini",
        "needs_key": True,
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "hint": "Chave no Google AI Studio. Tem camada gratuita generosa.",
    },
    {
        "kind": "openai_compat",
        "label": "Endpoint compatível (OpenRouter, DeepSeek, Groq…)",
        "needs_key": True,
        "default_base_url": "https://openrouter.ai/api/v1",
        "hint": "Qualquer serviço que fale o formato da OpenAI.",
    },
    {
        "kind": "lmstudio",
        "label": "LM Studio (local)",
        "needs_key": False,
        "default_base_url": LMSTUDIO_URL,
        "hint": "Ligue o servidor local na aba Developer do LM Studio.",
    },
    {
        "kind": "ollama",
        "label": "Ollama (local)",
        "needs_key": False,
        "default_base_url": OLLAMA_URL,
        "hint": "Precisa do Ollama rodando na máquina.",
    },
    {
        "kind": "claude_cli",
        "label": "Claude pela assinatura (experimental)",
        "needs_key": False,
        "needs_base_url": False,
        "default_base_url": "",
        "default_model": "sonnet",
        "experimental": True,
        "hint": (
            "Usa o Claude Code instalado e logado nesta máquina, e os limites do "
            "seu plano Pro ou Max, sem chave de API. Não funciona dentro do Docker. "
            "Experimental: as regras da Anthropic para esse uso mudaram várias "
            "vezes, e o uso pesado pode restringir a conta. Veja docs/provedores.md."
        ),
    },
]


def build_provider(config: ProviderConfig) -> Provider:
    provider_class = _PROVIDERS.get(config.kind)
    if provider_class is None:
        raise ProviderError(f"Provedor desconhecido: {config.kind}")
    return provider_class(config)


def _error_text(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except (json.JSONDecodeError, ValueError):
        payload = None
    detail = extract_error_message(payload, response.text[:300] or "sem detalhes")
    return f"HTTP {response.status_code}: {detail}"
