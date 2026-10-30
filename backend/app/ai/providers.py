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

import json
from typing import Any

import httpx

from .base import (
    Completion,
    Message,
    Provider,
    ProviderConfig,
    ProviderError,
    Usage,
    classify_status,
    extract_error_message,
)

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

    default_base_url = "http://localhost:11434/v1"

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

    default_base_url = "http://localhost:1234/v1"


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
# Registro
# --------------------------------------------------------------------------

_PROVIDERS: dict[str, type[Provider]] = {
    "openai": OpenAICompatProvider,
    "openai_compat": OpenAICompatProvider,
    "anthropic": AnthropicProvider,
    "google": GoogleProvider,
    "ollama": OllamaProvider,
    "lmstudio": LMStudioProvider,
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
        "default_base_url": "http://localhost:1234/v1",
        "hint": "Ligue o servidor local na aba Developer do LM Studio.",
    },
    {
        "kind": "ollama",
        "label": "Ollama (local)",
        "needs_key": False,
        "default_base_url": "http://localhost:11434/v1",
        "hint": "Precisa do Ollama rodando na máquina.",
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
