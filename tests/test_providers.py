"""Provedores de IA contra respostas simuladas com httpx.MockTransport.

Nada sai para a rede. Cada teste confere o formato que o provedor envia e o
que ele extrai da resposta, que é onde as APIs diferem entre si.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai import base  # noqa: E402
from app.ai.base import Message, ProviderConfig, ProviderError, extract_error_message  # noqa: E402
from app.ai.providers import build_provider  # noqa: E402

MESSAGES = [
    Message(role="system", content="Você traduz."),
    Message(role="user", content="Hello"),
]


def _run(kind: str, handler, *, call="complete", **config):
    """Executa uma chamada do provedor com um transporte simulado."""
    requests: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handler(request)

    provider = build_provider(ProviderConfig(kind=kind, **config))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(recording)) as client:
            if call == "complete":
                return await provider.complete(MESSAGES, client=client)
            if call == "retry":
                return await provider.complete_with_retry(MESSAGES, client=client)
            return await provider.list_models(client=client)

    return asyncio.run(go()), requests


@pytest.fixture
def no_sleep(monkeypatch):
    """As novas tentativas esperam segundos; nos testes, nada."""

    async def instant(_seconds):
        return None

    monkeypatch.setattr(base.asyncio, "sleep", instant)


# ------------------------------------------------------------------ formatos


def test_openai_envia_bearer_e_le_choices():
    def handler(request):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "Olá"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        })

    result, requests = _run("openai", handler, api_key="sk-1", model="gpt-x")

    request = requests[0]
    assert str(request.url) == "https://api.openai.com/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-1"
    body = json.loads(request.content)
    assert body["model"] == "gpt-x"
    assert body["messages"][0] == {"role": "system", "content": "Você traduz."}
    assert (result.text, result.usage.input_tokens, result.usage.output_tokens) == ("Olá", 7, 2)


def test_endpoint_local_sem_chave_nao_manda_authorization():
    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    _, requests = _run("lmstudio", handler, base_url="http://host:1234/v1/", model="m")

    assert str(requests[0].url) == "http://host:1234/v1/chat/completions"
    assert "authorization" not in requests[0].headers


def test_anthropic_separa_o_system_e_junta_blocos_de_texto():
    def handler(request):
        return httpx.Response(200, json={
            "content": [
                {"type": "text", "text": "Ol"},
                {"type": "thinking", "thinking": "ignorar"},
                {"type": "text", "text": "á"},
            ],
            "usage": {"input_tokens": 5, "output_tokens": 1},
        })

    result, requests = _run("anthropic", handler, api_key="sk-ant", model="claude-x")

    request = requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant"
    assert request.headers["anthropic-version"]
    body = json.loads(request.content)
    assert body["system"] == "Você traduz."
    assert body["messages"] == [{"role": "user", "content": "Hello"}]
    assert result.text == "Olá"
    assert result.usage.input_tokens == 5


def test_google_usa_cabecalho_e_tira_prefixo_do_modelo():
    def handler(request):
        return httpx.Response(200, json={
            "candidates": [{"content": {"parts": [{"text": "Olá"}]}}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 1},
        })

    result, requests = _run("google", handler, api_key="g-key", model="models/gemini-x")

    request = requests[0]
    assert request.url.path == "/v1beta/models/gemini-x:generateContent"
    # A chave vai no cabeçalho, não na URL, para não aparecer em logs.
    assert request.headers["x-goog-api-key"] == "g-key"
    assert "key=" not in str(request.url)
    body = json.loads(request.content)
    assert body["systemInstruction"]["parts"][0]["text"] == "Você traduz."
    assert body["contents"] == [{"role": "user", "parts": [{"text": "Hello"}]}]
    assert (result.text, result.usage.output_tokens) == ("Olá", 1)


def test_google_bloqueado_vira_erro_com_motivo():
    def handler(request):
        return httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})

    with pytest.raises(ProviderError, match="SAFETY"):
        _run("google", handler, api_key="k", model="gemini-x")


def test_ollama_lista_modelos_pela_api_nativa():
    def handler(request):
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen:14b"}, {"name": "gemma:12b"}]})

    models, _ = _run("ollama", handler, call="models", base_url="http://host:11434/v1")
    assert models == ["gemma:12b", "qwen:14b"]


def test_google_lista_so_modelos_que_geram_texto():
    def handler(request):
        return httpx.Response(200, json={"models": [
            {"name": "models/gemini-x", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/embed-y", "supportedGenerationMethods": ["embedContent"]},
        ]})

    models, _ = _run("google", handler, call="models", api_key="k")
    assert models == ["gemini-x"]


def test_provedor_desconhecido():
    with pytest.raises(ProviderError, match="desconhecido"):
        build_provider(ProviderConfig(kind="nada"))


# -------------------------------------------------------------------- erros


def test_401_nao_e_retentavel_e_traz_a_mensagem_do_provedor():
    def handler(request):
        return httpx.Response(401, json={"error": {"message": "Invalid API key"}})

    with pytest.raises(ProviderError) as info:
        _run("openai", handler, api_key="ruim", model="m")

    assert info.value.status == 401
    assert info.value.retryable is False
    assert str(info.value) == "HTTP 401: Invalid API key"


@pytest.mark.parametrize("status", [429, 500, 503])
def test_limite_e_falha_do_servidor_sao_retentaveis(status):
    def handler(request):
        return httpx.Response(status, text="tente depois")

    with pytest.raises(ProviderError) as info:
        _run("openai", handler, model="m")
    assert info.value.retryable is True


def test_nova_tentativa_depois_de_429(no_sleep):
    responses = iter([
        httpx.Response(429, json={"error": {"message": "rate limit"}}),
        httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]}),
    ])

    result, requests = _run("openai", lambda _r: next(responses), call="retry", model="m")

    assert result.text == "ok"
    assert len(requests) == 2


def test_sem_nova_tentativa_depois_de_401(no_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(401, json={"error": {"message": "nope"}})

    with pytest.raises(ProviderError):
        _run("openai", handler, call="retry", model="m")
    assert len(calls) == 1


def test_falha_de_rede_desiste_depois_das_tentativas(no_sleep):
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ConnectError("recusada", request=request)

    with pytest.raises(ProviderError, match="Falha de rede"):
        _run("ollama", handler, call="retry", model="m")
    assert len(calls) == 4


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"error": {"message": "m1"}}, "m1"),
        ({"error": {"type": "overloaded_error"}}, "overloaded_error"),
        ({"error": "texto"}, "texto"),
        ({"message": "m2"}, "m2"),
        (["lista"], "fallback"),
        (None, "fallback"),
    ],
)
def test_extract_error_message(payload, expected):
    assert extract_error_message(payload, "fallback") == expected
