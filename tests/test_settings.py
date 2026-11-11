"""Preferências e guarda das chaves de API."""

from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import ProviderProfile, Settings, SettingsStore, data_root  # noqa: E402
from app.deps import reset_caches  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    for name in ("OPENAI", "ANTHROPIC", "GOOGLE", "COMPAT"):
        monkeypatch.delenv(f"INTERLINEAR_{name}_KEY", raising=False)
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


def _add_provider(client, **extra):
    payload = {"kind": "openai", "model": "m", "api_key": "sk-secreta-1234", **extra}
    response = client.post("/api/providers", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_api_nunca_devolve_a_chave_inteira(client):
    created = _add_provider(client)

    for body in (created, client.get("/api/settings").json()):
        assert "sk-secreta-1234" not in str(body)

    provider = client.get("/api/settings").json()["settings"]["providers"][0]
    assert "api_key" not in provider
    assert provider["has_key"] is True
    assert provider["key_hint"] == "…1234"


@pytest.mark.skipif(sys.platform == "win32", reason="Windows não tem permissões POSIX")
def test_arquivo_de_chaves_so_o_dono_le(client, tmp_path):
    _add_provider(client)

    mode = stat.S_IMODE((tmp_path / "settings.json").stat().st_mode)
    assert mode == 0o600


def test_variavel_de_ambiente_tem_prioridade(monkeypatch):
    settings = Settings()
    profile = ProviderProfile(id="p", label="p", kind="openai", api_key="da-tela")

    assert settings.resolved_key(profile) == "da-tela"
    monkeypatch.setenv("INTERLINEAR_OPENAI_KEY", "do-ambiente")
    assert settings.resolved_key(profile) == "do-ambiente"

    # Tipos sem variável própria (locais) usam a chave salva.
    local = ProviderProfile(id="l", label="l", kind="ollama", api_key="")
    assert settings.resolved_key(local) == ""


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        ("darwin", Path("casa/Library/Application Support/interlinear")),
        ("win32", Path("roaming/interlinear")),
        ("linux", Path("xdg/interlinear")),
    ],
)
def test_pasta_de_dados_segue_o_sistema(platform, expected, tmp_path, monkeypatch):
    monkeypatch.delenv("INTERLINEAR_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "casa")
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))

    assert data_root() == tmp_path / expected


def test_preferencias_corrompidas_nao_impedem_de_abrir(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ isto não é json", encoding="utf-8")

    settings = SettingsStore(path).load()
    assert settings.providers == []
    assert settings.target_language == "português brasileiro"


def test_preferencias_numericas_sao_limitadas(client):
    response = client.put(
        "/api/settings",
        json={"concurrency": 99, "batch_chars": 10, "context_blocks": -3},
    )
    settings = response.json()["settings"]
    assert settings["concurrency"] == 16
    assert settings["batch_chars"] == 500
    assert settings["context_blocks"] == 0


def test_glossario_ignora_termos_vazios(client):
    response = client.put(
        "/api/settings", json={"glossary": {"Shire": "Condado", "  ": "x"}}
    )
    assert response.json()["settings"]["glossary"] == {"Shire": "Condado"}


def test_apagar_o_provedor_ativo_escolhe_outro(client):
    first = _add_provider(client)["provider"]["id"]
    second = _add_provider(client, api_key="sk-outra-9999")["provider"]["id"]

    settings = client.delete(f"/api/providers/{first}").json()["settings"]
    assert settings["active_provider_id"] == second
