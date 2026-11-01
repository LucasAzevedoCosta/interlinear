"""Rotas de projeto: importação, exportação e o loop de eventos livre."""

from __future__ import annotations

import sys
import threading
import time
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.blocks import Block  # noqa: E402
from app.deps import reset_caches  # noqa: E402
from app.routes import projects as project_routes  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


@pytest.fixture(scope="module")
def pdf_bytes() -> bytes:
    from make_fixture import gerar

    destino = ROOT / "tests" / "fixtures" / "livro_teste.pdf"
    if not destino.exists():
        gerar(destino)
    return destino.read_bytes()


def test_importa_pdf_e_exporta_epub(client, pdf_bytes):
    response = client.post(
        "/api/projects", files={"file": ("livro.pdf", pdf_bytes, "application/pdf")}
    )
    assert response.status_code == 200, response.text
    project = response.json()["project"]
    assert project["source_kind"] == "pdf"
    assert project["total_translatable"] > 0

    exported = client.get(f"/api/projects/{project['id']}/export?translated=false")
    assert exported.status_code == 200
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        assert archive.read("mimetype") == b"application/epub+zip"


def test_extensao_invalida_e_recusada(client):
    response = client.post("/api/projects", files={"file": ("x.txt", b"oi", "text/plain")})
    assert response.status_code == 400


def test_importacao_lenta_nao_trava_o_servidor(client, monkeypatch):
    def extracao_lenta(path, source_name=""):
        time.sleep(1.5)  # CPU ocupada, como o pdfminer num livro grande
        return [Block(kind="paragraph", text="Texto.")], "Lento"

    monkeypatch.setattr(project_routes, "extract_blocks", extracao_lenta)

    importacao = threading.Thread(
        target=lambda: client.post(
            "/api/projects", files={"file": ("lento.pdf", b"%PDF-1.4", "application/pdf")}
        )
    )
    importacao.start()
    time.sleep(0.2)  # a importação já está extraindo

    inicio = time.monotonic()
    assert client.get("/api/health").status_code == 200
    espera = time.monotonic() - inicio
    importacao.join()

    assert espera < 0.5, f"/api/health esperou {espera:.2f}s pela importação"
