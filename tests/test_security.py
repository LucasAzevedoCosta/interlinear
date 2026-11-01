"""Proteções da API local contra sites abertos no navegador do usuário."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.deps import reset_caches  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1:8000") as test_client:
        yield test_client
    reset_caches()


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "localhost"])
def test_hosts_locais_sao_aceitos(client, host):
    assert client.get("/api/health", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("host", ["ataque.exemplo:8000", "ataque.exemplo", "127.0.0.1.ataque.exemplo"])
def test_host_estranho_e_recusado(client, host):
    # DNS rebinding: o domínio do atacante passou a apontar para 127.0.0.1.
    response = client.get("/api/settings", headers={"Host": host})
    assert response.status_code == 400


def test_post_de_outra_origem_e_recusado(client):
    response = client.post(
        "/api/providers",
        json={"kind": "openai"},
        headers={"Origin": "https://ataque.exemplo"},
    )
    assert response.status_code == 403


def test_post_da_propria_interface_e_aceito(client):
    response = client.post(
        "/api/providers",
        json={"kind": "openai", "model": "m"},
        headers={"Origin": "http://127.0.0.1:8000"},
    )
    assert response.status_code == 200


def test_trocar_endereco_exige_redigitar_a_chave(client):
    created = client.post(
        "/api/providers",
        json={
            "kind": "openai",
            "model": "m",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-segredo",
        },
    ).json()["provider"]

    desvio = client.post(
        "/api/providers",
        json={
            "id": created["id"],
            "kind": "openai",
            "model": "m",
            "base_url": "https://coletor.exemplo/v1",
            "api_key": "",
        },
    )
    assert desvio.status_code == 400

    # Mesmo endereço (até com barra no fim): manter a chave continua valendo.
    troca_modelo = client.post(
        "/api/providers",
        json={
            "id": created["id"],
            "kind": "openai",
            "model": "outro",
            "base_url": "https://api.openai.com/v1/",
            "api_key": "",
        },
    )
    assert troca_modelo.status_code == 200
    assert troca_modelo.json()["provider"]["has_key"] is True
