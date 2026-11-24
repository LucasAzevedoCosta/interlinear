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
    ModelInfo,
    PlanLimitError,
    Provider,
    ProviderConfig,
    ProviderError,
    Usage,
    classify_status,
    extract_error_message,
    newest_first,
)

# Servidores locais, rodando no computador fora do container. Lá dentro,
# "localhost" é o próprio container; host.docker.internal é o computador.
OLLAMA_URL = os.environ.get("INTERLINEAR_OLLAMA_URL", "http://host.docker.internal:11434/v1")
LMSTUDIO_URL = os.environ.get("INTERLINEAR_LMSTUDIO_URL", "http://host.docker.internal:1234/v1")

# Modelos que não geram texto de conversa e só poluem o seletor: embeddings,
# áudio, imagem, moderação e os antigos de completar texto.
_NOT_TEXT = re.compile(
    r"embed|tts|whisper|transcri|dall-e|image|moderation|realtime|audio|sora"
    r"|\b(?:babbage|davinci)",
    re.IGNORECASE,
)


def _epoch(value: Any) -> float:
    """Data de lançamento como época: aceita número ou texto ISO 8601."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0

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
        return newest_first([
            ModelInfo(
                id=str(item["id"]),
                name=str(item.get("name") or ""),  # OpenRouter dá o nome
                created=_epoch(item.get("created")),
            )
            for item in items
            if isinstance(item, dict) and item.get("id") and not _NOT_TEXT.search(str(item["id"]))
        ])


class OllamaProvider(OpenAICompatProvider):
    """Ollama local. Completa pelo endpoint compatível, lista pelo nativo."""

    default_base_url = OLLAMA_URL

    async def list_models(self, *, client: httpx.AsyncClient) -> list[ModelInfo]:
        root = self.base_url.removesuffix("/v1")
        response = await client.get(f"{root}/api/tags", timeout=30.0)
        if response.status_code >= 400:
            raise ProviderError(_error_text(response), status=response.status_code)
        models = response.json().get("models", [])
        # A data aqui é a do download, não a do lançamento: a ordem fica alfabética.
        return sorted(
            (
                ModelInfo(id=str(m["name"]), note=_ollama_note(m))
                for m in models
                if isinstance(m, dict) and m.get("name") and not _NOT_TEXT.search(str(m["name"]))
            ),
            key=lambda m: m.id,
        )


def _ollama_note(model: dict[str, Any]) -> str:
    details = model.get("details") if isinstance(model.get("details"), dict) else {}
    parts = [str(details.get("parameter_size") or "")]
    size = model.get("size")
    if isinstance(size, (int, float)) and size > 0:
        parts.append(f"{size / 1e9:.1f} GB")
    return " · ".join(p for p in parts if p)


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
        return newest_first([
            ModelInfo(
                id=str(m["id"]),
                name=str(m.get("display_name") or ""),
                created=_epoch(m.get("created_at")),
            )
            for m in data
            if isinstance(m, dict) and m.get("id")
        ])


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
            (
                ModelInfo(
                    id=str(m.get("name", "")).removeprefix("models/"),
                    name=str(m.get("displayName") or ""),
                )
                for m in models
                if isinstance(m, dict)
                and "generateContent" in (m.get("supportedGenerationMethods") or [])
                and not _NOT_TEXT.search(str(m.get("name", "")))
            ),
            key=lambda m: m.id,
        )


# --------------------------------------------------------------------------
# Claude pela assinatura (Claude Code)
# --------------------------------------------------------------------------


# Modelos aceitos pelo Claude Code como apelido do mais recente de cada linha.
CLAUDE_CLI_MODELS = [
    ModelInfo("haiku", "Haiku", note="O mais rápido; gasta menos do limite do plano."),
    ModelInfo("sonnet", "Sonnet", note="Equilíbrio entre qualidade e limite. O padrão."),
    ModelInfo("opus", "Opus", note="A melhor tradução; gasta o limite bem mais rápido."),
]

_RESET_EPOCH = re.compile(r"\|\s*(\d{10})\b")
_RESET_CLOCK = re.compile(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", re.I)
_LIMIT_WORDS = ("usage limit", "limit reached", "limit will reset", "out of extra usage")
_LOGIN_WORDS = ("/login", "not logged in", "invalid api key", "please log in")


def claude_path() -> str | None:
    return shutil.which("claude")


def claude_login_hint() -> str:
    return (
        "No Docker Desktop, abra o container interlinear, aba Exec, e rode "
        "`claude auth login` (ou, no terminal, `docker exec -it interlinear "
        "claude auth login`)."
    )


def claude_executable() -> str:
    """O caminho do Claude Code, ou um erro que diz como instalar."""
    path = claude_path()
    if path is None:
        raise ProviderError(
            "Esta imagem do Interlinear foi construída sem o Claude Code. Reconstrua "
            "com INTERLINEAR_WITH_CLAUDE=1 (veja docs/provedores.md).",
            fatal=True,
        )
    return path


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
        return claude_executable()

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

    async def auth_status(self) -> dict[str, Any]:
        """O `claude auth status`: logado ou não, forma de login, plano. Sem token."""
        code, out, err = await self._run(["auth", "status"])
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict) or (code != 0 and not data):
            raise ProviderError(f"Claude Code: {(err or out).strip()[:300]}", fatal=True)
        return data

    async def account(self) -> dict[str, Any]:
        """A conta em que o Claude Code está logado: plano e forma de login."""
        data = await self.auth_status()
        if not data.get("loggedIn"):
            raise ProviderError(
                f"O Claude Code não está logado. {claude_login_hint()}", fatal=True
            )
        if data.get("authMethod") != "claude.ai":
            raise ProviderError(
                "O Claude Code está entrando com chave de API, não com a assinatura. "
                "Para usar a chave, cadastre o provedor Anthropic; para usar a "
                f"assinatura, saia (`claude auth logout`) e entre de novo. {claude_login_hint()}",
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

    async def list_models(self, *, client: httpx.AsyncClient) -> list[ModelInfo]:
        return list(CLAUDE_CLI_MODELS)


def _cli_error(message: str, *, status: Any = None) -> ProviderError:
    """Traduz a mensagem de erro do Claude Code no erro certo para o tradutor."""
    lowered = message.lower()
    if any(word in lowered for word in _LIMIT_WORDS):
        return PlanLimitError(f"Limite do plano: {message[:300]}", resets_at=_reset_time(message))
    if any(word in lowered for word in _LOGIN_WORDS):
        return ProviderError(f"O Claude Code não está logado. {claude_login_hint()}", fatal=True)
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


async def claude_status() -> dict[str, Any]:
    """Estado do Claude Code para o painel "Conectar" das configurações.

    Só lê o `claude auth status`. O login acontece fora do Interlinear, pelo
    fluxo da Anthropic; aqui nada passa pelo token nem pelo código de login.
    """
    installed = claude_path() is not None
    status: dict[str, Any] = {
        "installed": installed,
        "logged_in": False,
        "subscription": False,
        "plan": "",
        "expires_at": None,
        "error": "",
    }
    if not installed:
        return status
    try:
        data = await ClaudeCliProvider(ProviderConfig(kind="claude_cli")).auth_status()
    except ProviderError as exc:
        status["error"] = str(exc)
        return status
    logged_in = bool(data.get("loggedIn"))
    status.update(
        logged_in=logged_in,
        subscription=logged_in and data.get("authMethod") == "claude.ai",
        plan=data.get("subscriptionType") or "",
        expires_at=data.get("loginExpiresAt"),
    )
    return status


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
        # Poucos modelos e sempre os mesmos: a tela mostra como opções.
        "fixed_models": True,
        "experimental": True,
        "hint": (
            "Usa o Claude Code, logado com a sua conta, e os limites do seu plano "
            "Pro ou Max, sem chave de API. Experimental: as regras da Anthropic para esse uso mudaram várias "
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
