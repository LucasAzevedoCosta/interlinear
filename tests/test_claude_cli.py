"""Claude pela assinatura: o provedor que roda o Claude Code (`claude -p`).

Um `claude` falso, apontado por INTERLINEAR_CLAUDE, faz o papel da CLI: grava
os argumentos e o ambiente com que foi chamado e responde conforme FAKE_MODE.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai import translator as translator_module  # noqa: E402
from app.ai.base import (  # noqa: E402
    Completion,
    Message,
    PlanLimitError,
    Provider,
    ProviderConfig,
    ProviderError,
    Usage,
)
from app.ai.providers import _reset_time, build_provider  # noqa: E402
from app.ai.translator import TranslationSettings, Translator  # noqa: E402
from app.core.blocks import Block  # noqa: E402

FAKE_CLI = r'''#!{python}
import json, os, sys, time
mode = os.environ.get("FAKE_MODE", "ok")
log = os.environ["FAKE_LOG"]
stdin = sys.stdin.read()
with open(log, "a") as out:
    out.write(json.dumps({{
        "args": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(),
        "api_key": "ANTHROPIC_API_KEY" in os.environ,
    }}) + "\n")
if sys.argv[1:3] == ["auth", "status"]:
    status = {{"loggedIn": mode != "logged_out", "authMethod": "apiKey" if mode == "api_key" else "claude.ai",
              "subscriptionType": "max", "loginExpiresAt": "2027-01-01T00:00:00Z"}}
    print(json.dumps(status)); sys.exit(0)
if mode == "slow":
    time.sleep(5)
results = {{
    "ok": (False, "⟦1⟧\nOlá."),
    "limit": (True, "Claude AI usage limit reached|1900000000"),
    "login": (True, "Invalid API key · Please run /login"),
    "boom": (True, "Something odd"),
}}
is_error, result = results.get(mode, results["ok"])
print(json.dumps({{"type": "result", "subtype": "success", "is_error": is_error, "result": result,
                  "api_error_status": None,
                  "usage": {{"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 7}}}}))
'''


@pytest.fixture
def cli(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLI.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("INTERLINEAR_CLAUDE", str(script))
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-nao-deve-ser-usada")

    def calls() -> list[dict]:
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    return calls


def _provider(**config) -> Provider:
    return build_provider(ProviderConfig(kind="claude_cli", model="sonnet", **config))


def _complete(provider: Provider) -> Completion:
    messages = [Message("system", "Você é um tradutor."), Message("user", "⟦1⟧\nHello.")]
    return asyncio.run(provider.complete(messages, client=None))  # type: ignore[arg-type]


def test_traduz_pela_cli_isolada_e_sem_chave_de_api(cli):
    completion = _complete(_provider())

    assert completion.text == "⟦1⟧\nOlá."
    assert (completion.usage.input_tokens, completion.usage.output_tokens) == (15, 7)
    status, call = cli()
    assert status["args"] == ["auth", "status"]  # confere a conta antes do primeiro lote
    args = call["args"]
    assert args[:3] == ["-p", "--output-format", "json"]
    assert args[args.index("--model") + 1] == "sonnet"
    assert args[args.index("--system-prompt") + 1] == "Você é um tradutor."
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--setting-sources") + 1] == ""
    assert {"--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"} <= set(args)
    assert call["stdin"] == "⟦1⟧\nHello."
    assert "interlinear-claude-" in call["cwd"]  # pasta vazia, sem CLAUDE.md
    assert call["api_key"] is False  # a chave do ambiente não chega à CLI


def test_confere_a_conta_uma_vez_por_provedor(cli):
    provider = _provider()
    _complete(provider)
    _complete(provider)
    assert [c["args"][0] for c in cli()] == ["auth", "-p", "-p"]


@pytest.mark.parametrize(
    ("mode", "message"),
    [("logged_out", "não está logado"), ("api_key", "chave de API")],
)
def test_conta_errada_interrompe_a_tarefa(cli, monkeypatch, mode, message):
    monkeypatch.setenv("FAKE_MODE", mode)
    with pytest.raises(ProviderError, match=message) as error:
        _complete(_provider())
    assert error.value.fatal


def test_limite_do_plano_vira_pausa_com_hora(cli, monkeypatch):
    provider = _provider()
    _complete(provider)  # conta conferida
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(PlanLimitError) as error:
        _complete(provider)
    assert error.value.resets_at == 1900000000.0


def test_login_expirado_no_meio_interrompe(cli, monkeypatch):
    provider = _provider()
    _complete(provider)
    monkeypatch.setenv("FAKE_MODE", "login")
    with pytest.raises(ProviderError, match="não está logado") as error:
        _complete(provider)
    assert error.value.fatal


def test_erro_desconhecido_pode_ser_repetido(cli, monkeypatch):
    provider = _provider()
    _complete(provider)
    monkeypatch.setenv("FAKE_MODE", "boom")
    with pytest.raises(ProviderError, match="Something odd") as error:
        _complete(provider)
    assert error.value.retryable and not error.value.fatal


def test_cli_que_nao_responde_e_interrompida(cli, monkeypatch):
    provider = _provider(timeout=0.5)
    _complete(provider)
    monkeypatch.setenv("FAKE_MODE", "slow")
    started = time.time()
    with pytest.raises(ProviderError, match="não respondeu") as error:
        _complete(provider)
    assert time.time() - started < 3
    assert error.value.retryable


def test_sem_claude_code_instalado(monkeypatch):
    monkeypatch.setenv("INTERLINEAR_CLAUDE", "/nao/existe/claude")
    with pytest.raises(ProviderError, match="Claude Code não encontrado") as error:
        _complete(_provider())
    assert error.value.fatal


def test_hora_de_renovacao_pelo_texto():
    now = datetime(2026, 11, 12, 10, 0).timestamp()
    assert _reset_time("limit reached|1900000000", now) == 1900000000.0
    assert _reset_time("5-hour limit reached ∙ resets 3pm", now) == datetime(2026, 11, 12, 15, 0).timestamp()
    assert _reset_time("resets at 9:30am", now) == datetime(2026, 11, 13, 9, 30).timestamp()
    assert _reset_time("limit reached, try later", now) is None


class _LimitedOnce(Provider):
    """Bate no limite do plano na primeira chamada; depois traduz."""

    def __init__(self) -> None:
        super().__init__(ProviderConfig(kind="fake"))
        self.calls = 0

    async def complete(self, messages, *, client) -> Completion:
        self.calls += 1
        if self.calls == 1:
            raise PlanLimitError("limit reached", resets_at=time.time() + 0.2)
        return Completion(text="⟦1⟧\nOlá.\n\n⟦2⟧\nTchau.", usage=Usage(1, 1))

    async def list_models(self, *, client) -> list[str]:
        return []


def test_tarefa_espera_o_limite_renovar_e_continua(monkeypatch):
    monkeypatch.setattr(translator_module, "PLAN_LIMIT_MARGIN", 0.0)
    blocks = [Block(kind="paragraph", text="Hello."), Block(kind="paragraph", text="Bye.")]
    notes: list[str] = []

    async def run():
        async with Translator(_LimitedOnce(), TranslationSettings()) as translator:
            return await translator.translate_blocks(
                blocks, on_progress=lambda p: notes.append(p.current_note)
            )

    progress = asyncio.run(run())

    assert (progress.done, progress.failed) == (2, 0)
    assert [b.translated for b in blocks] == ["Olá.", "Tchau."]
    assert notes[0].startswith("Limite do plano atingido. Continua às")
    assert notes[-1] == ""


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.deps import reset_caches

    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path / "dados"))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client
    reset_caches()


def _status(api) -> dict:
    return api.get("/api/claude/status").json()["claude"]


def test_status_logado_mostra_plano_e_validade(cli, api, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DOCKER", "1")
    status = _status(api)
    assert status == {
        "installed": True,
        "docker": True,
        # No Docker não há janela para abrir: o caminho é a aba Exec.
        "can_open_terminal": False,
        "logged_in": True,
        "subscription": True,
        "plan": "max",
        "expires_at": "2027-01-01T00:00:00Z",
        "error": "",
    }
    # Só o status: nada de pedido ao modelo.
    assert [c["args"][:2] for c in cli()] == [["auth", "status"]]


@pytest.mark.parametrize(
    ("mode", "logged_in", "subscription"),
    [("logged_out", False, False), ("api_key", True, False)],
)
def test_status_sem_a_assinatura(cli, api, monkeypatch, mode, logged_in, subscription):
    monkeypatch.setenv("FAKE_MODE", mode)
    status = _status(api)
    assert (status["logged_in"], status["subscription"]) == (logged_in, subscription)


def test_status_sem_claude_code(api, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_CLAUDE", "/nao/existe/claude")
    monkeypatch.delenv("INTERLINEAR_DOCKER", raising=False)
    status = _status(api)
    assert (status["installed"], status["docker"], status["logged_in"]) == (False, False, False)


def test_deslogado_no_docker_ensina_o_caminho_do_docker_desktop(cli, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DOCKER", "1")
    monkeypatch.setenv("FAKE_MODE", "logged_out")
    with pytest.raises(ProviderError, match="aba Exec"):
        _complete(_provider())


def test_testar_conexao_mostra_o_plano(cli, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.deps import reset_caches

    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path / "dados"))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as client:
        created = client.post(
            "/api/providers", json={"kind": "claude_cli", "label": "Claude", "model": "haiku"}
        ).json()["provider"]
        assert created["has_key"] is False
        result = client.post(f"/api/providers/{created['id']}/test").json()
        models = client.get(f"/api/providers/{created['id']}/models").json()["models"]
    reset_caches()

    assert result["ok"] and result["plan"] == "max"
    assert [m["id"] for m in models] == ["haiku", "sonnet", "opus"]
    assert all(m["note"] for m in models)


# --------------------------------------------------------------------------
# Entrar: a janela de terminal que o botão do site abre
# --------------------------------------------------------------------------

# Faz o papel do Terminal.app (ou do gnome-terminal): só anota o que recebeu.
FAKE_TERMINAL = r"""#!{python}
import os, sys
with open(os.environ["FAKE_TERMINAL_LOG"], "a") as out:
    out.write("\t".join(sys.argv[1:]) + "\n")
"""


@pytest.fixture
def terminal(tmp_path, monkeypatch):
    """Um terminal falso no lugar do do sistema, e o Docker desligado."""
    from app.ai import providers

    launcher = tmp_path / "terminal-falso"
    launcher.write_text(FAKE_TERMINAL.format(python=sys.executable))
    launcher.chmod(0o755)
    log = tmp_path / "terminal.log"
    monkeypatch.setenv("FAKE_TERMINAL_LOG", str(log))
    monkeypatch.delenv("INTERLINEAR_DOCKER", raising=False)
    monkeypatch.setattr(providers, "terminal_launcher", lambda: [str(launcher)])
    # O guarda contra cliques repetidos é global ao processo.
    monkeypatch.setattr(providers, "_login_opened_at", 0.0)

    def opened() -> list[str]:
        return log.read_text().splitlines() if log.exists() else []

    return opened


def _login_script(tmp_path) -> Path:
    name = "entrar-no-claude.bat" if sys.platform == "win32" else "entrar-no-claude.command"
    return tmp_path / "dados" / name


# O que segura a janela aberta depois do login, para o usuário ler o resultado.
PAUSE = "pause" if sys.platform == "win32" else "read -r _"


def test_botao_abre_o_login_do_claude_code_numa_janela(cli, api, terminal, tmp_path):
    body = api.post("/api/claude/login").json()

    assert body["login"]["already_open"] is False
    # A janela foi aberta com o script, e o script chama o binário oficial.
    script = _login_script(tmp_path)
    assert terminal() == [str(script)]
    content = script.read_text()
    assert "auth login" in content and "auth logout" not in content
    # A janela não pode fechar sozinha: o usuário precisa ler o resultado.
    assert PAUSE in content
    # Abrir a janela não fala com o modelo: só o status, para saber se precisa sair antes.
    assert [c["args"][:2] for c in cli()] == [["auth", "status"], ["auth", "status"]]


def test_status_diz_que_da_para_entrar_por_aqui(cli, api, terminal):
    assert _status(api)["can_open_terminal"] is True


def test_dois_cliques_nao_abrem_duas_janelas(cli, api, terminal):
    first = api.post("/api/claude/login").json()["login"]
    second = api.post("/api/claude/login").json()["login"]

    assert (first["already_open"], second["already_open"]) == (False, True)
    assert len(terminal()) == 1


def test_entrar_por_cima_de_chave_de_api_sai_antes(cli, api, terminal, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "api_key")

    api.post("/api/claude/login")

    assert "auth logout" in _login_script(tmp_path).read_text()


def test_no_docker_o_botao_nao_existe_e_a_rota_manda_para_o_exec(cli, api, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DOCKER", "1")

    assert _status(api)["can_open_terminal"] is False
    response = api.post("/api/claude/login")
    assert response.status_code == 400
    assert "aba Exec" in response.json()["detail"]


def test_entrar_sem_claude_code_instalado(api, terminal, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_CLAUDE", "/nao/existe/claude")

    response = api.post("/api/claude/login")

    assert response.status_code == 400
    assert "Claude Code não encontrado" in response.json()["detail"]
    assert terminal() == []


def test_sem_terminal_para_abrir_ensina_o_comando(cli, api, monkeypatch):
    from app.ai import providers

    monkeypatch.delenv("INTERLINEAR_DOCKER", raising=False)
    monkeypatch.setattr(providers, "terminal_launcher", lambda: None)
    monkeypatch.setattr(providers, "_login_opened_at", 0.0)

    response = api.post("/api/claude/login")

    assert response.status_code == 400
    assert "claude auth login" in response.json()["detail"]


def test_perfil_da_assinatura_nao_guarda_chave(api):
    created = api.post(
        "/api/providers",
        json={"kind": "claude_cli", "label": "Claude", "model": "sonnet", "api_key": "sk-nao"},
    ).json()["provider"]

    assert created["has_key"] is False
    assert not created["key_hint"]
