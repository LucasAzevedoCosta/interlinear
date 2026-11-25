"""Áreas de trabalho da bancada (disposição dos painéis)."""

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

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


def test_sem_nada_salvo_devolve_null(client):
    response = client.get("/api/ui/workspace")
    assert response.status_code == 200
    assert response.json() == {"workspace": None}


def test_grava_e_le_de_volta(client, tmp_path):
    payload = {
        "version": 1,
        "active": "padrao",
        "workspaces": {"minha": {"name": "Minha área", "layout": {"grid": {}}}},
    }
    assert client.put("/api/ui/workspace", json=payload).status_code == 200
    assert client.get("/api/ui/workspace").json() == {"workspace": payload}
    assert (tmp_path / "workspace.json").exists()
    assert not (tmp_path / "workspace.json.tmp").exists()


def test_arquivo_corrompido_vira_null(client, tmp_path):
    (tmp_path / "workspace.json").write_text("{ não é json", encoding="utf-8")
    assert client.get("/api/ui/workspace").json() == {"workspace": None}


def test_json_que_nao_e_objeto_vira_null(client, tmp_path):
    (tmp_path / "workspace.json").write_text("[1, 2]", encoding="utf-8")
    assert client.get("/api/ui/workspace").json() == {"workspace": None}


def test_recusa_corpo_que_nao_e_objeto(client):
    assert client.put("/api/ui/workspace", json=[1, 2]).status_code == 422


def test_recusa_disposicao_grande_demais(client, tmp_path):
    response = client.put("/api/ui/workspace", json={"lixo": "x" * (300 * 1024)})
    assert response.status_code == 413
    assert not (tmp_path / "workspace.json").exists()
