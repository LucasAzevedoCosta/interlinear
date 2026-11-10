"""Importação em segundo plano: progresso, cancelamento, opções e resumo."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import importer  # noqa: E402
from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.core.importer import ImportOptions, build_summary, page_ranges  # noqa: E402
from app.core.pdf_extract import ExtractionReport  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402


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


def _start(client: TestClient, name: str, data: bytes, **form: str) -> dict:
    response = client.post(
        "/api/imports", files={"file": (name, data, "application/octet-stream")}, data=form
    )
    assert response.status_code == 200, response.text
    return response.json()["import"]


def _wait(client: TestClient, job_id: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/imports/{job_id}").json()["import"]
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("A importação não terminou a tempo")


def test_importa_em_segundo_plano_e_grava_o_resumo(client, pdf_bytes):
    job = _wait(client, _start(client, "livro.pdf", pdf_bytes, title="Meu título")["id"])

    assert job["status"] == "done", job["error"]
    assert job["stage"] == "save"
    project = client.get(f"/api/projects/{job['project_id']}").json()
    assert project["document"]["title"] == "Meu título"
    summary = project["project"]["import_summary"]
    assert summary["source_kind"] == "pdf"
    assert summary["pages"] > 1
    assert summary["chapters"] == len(project["document"]["chapters"])
    assert summary["words"] > 0 and summary["paragraphs"] > 0
    assert summary["ocr_pages"] == 0


def test_cancelar_para_na_proxima_pagina_e_nao_cria_projeto(client, monkeypatch):
    pages_read: list[int] = []
    started = threading.Event()

    def slow_extraction(path, *, on_progress, **_):
        for page in range(1, 101):
            on_progress("read", page, 100)
            pages_read.append(page)
            started.set()
            time.sleep(0.01)
        return [Block(kind="paragraph", text="Texto.")], "Lento"

    monkeypatch.setattr(importer, "extract_blocks", slow_extraction)
    job = _start(client, "lento.pdf", b"%PDF-1.4")
    assert started.wait(5)

    client.post(f"/api/imports/{job['id']}/cancel").raise_for_status()
    job = _wait(client, job["id"])

    assert job["status"] == "stopped"
    assert len(pages_read) < 100
    assert job["project_id"] is None
    assert get_store().list_projects() == []


def test_progresso_aparece_enquanto_importa(client, monkeypatch):
    gate = threading.Event()

    def gated_extraction(path, *, on_progress, **_):
        on_progress("ocr", 7, 30)
        gate.wait(5)
        return [Block(kind="paragraph", text="Texto.")], "Lento"

    monkeypatch.setattr(importer, "extract_blocks", gated_extraction)
    job = _start(client, "lento.pdf", b"%PDF-1.4")
    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get(f"/api/imports/{job['id']}").json()["import"]
        if job["stage"] == "ocr":
            break
        time.sleep(0.01)

    assert (job["stage"], job["done"], job["total"]) == ("ocr", 7, 30)
    assert [j["id"] for j in client.get("/api/imports").json()["imports"]] == [job["id"]]
    gate.set()
    assert _wait(client, job["id"])["status"] == "done"
    assert client.get("/api/imports").json()["imports"] == []


def test_arquivo_ilegivel_vira_erro_da_tarefa(client):
    job = _wait(client, _start(client, "quebrado.pdf", b"isto nao e um pdf")["id"])
    assert job["status"] == "error"
    assert "Não foi possível ler o arquivo" in job["error"]


@pytest.mark.parametrize(
    "form",
    [{"split_on": "heading9"}, {"ocr_mode": "talvez"}, {"ocr_languages": "por; rm -rf /"}],
)
def test_opcoes_invalidas_sao_recusadas(client, form):
    response = client.post(
        "/api/imports", files={"file": ("x.pdf", b"%PDF-1.4", "application/pdf")}, data=form
    )
    assert response.status_code == 400


def test_opcoes_disponiveis(client):
    data = client.get("/api/imports/options").json()
    assert set(data["ocr"]) == {"available", "languages", "default"}
    assert data["max_upload_bytes"] > 0


def test_opcoes_chegam_ao_extrator(client, monkeypatch):
    received: dict = {}

    def spy(path, **kwargs):
        received.update(kwargs)
        return [Block(kind="heading2", text="Parte"), Block(kind="paragraph", text="Texto.")], "T"

    monkeypatch.setattr(importer, "extract_blocks", spy)
    job = _start(
        client, "x.pdf", b"%PDF-1.4", ocr_mode="force", ocr_languages="por", split_on="heading2"
    )
    assert _wait(client, job["id"])["status"] == "done"
    assert received["ocr_mode"] == "force" and received["ocr_languages"] == "por"


def test_paginas_em_trechos():
    assert page_ranges([9, 1, 2, 3, 7, 10]) == "1–3, 7, 9–10"
    assert page_ranges(list(range(1, 40, 2)), limit=3) == "1, 3, 5, …"


def test_resumo_avisa_o_que_merece_conferir():
    words = " ".join(["palavra"] * 6000)
    document = Document(chapters=[Chapter(title="Tudo", blocks=[
        Block(kind="heading1", text="Tudo"),
        Block(kind="paragraph", text=words),
        Block(kind="footnote", text="1 Nota."),
        Block(kind="table", text="a | b"),
    ])])
    report = ExtractionReport(
        pages=40, ocr_pages=[3], scanned_without_ocr=[5, 6, 7], ocr_empty=[3],
        skipped_images=[12, 12],
    )

    summary = build_summary(document, report, ImportOptions(), 2.04)

    assert summary["pages"] == 40 and summary["ocr_pages"] == 1
    assert (summary["headings"], summary["paragraphs"], summary["footnotes"], summary["tables"]) == (
        1, 1, 1, 1,
    )
    assert summary["seconds"] == 2.0
    assert summary["warnings"] == [
        "Páginas 5–7 parecem escaneadas e ficaram sem texto: o Tesseract não está instalado.",
        "O OCR não achou texto na página 3.",
        "2 imagens ficaram de fora, num formato que o conversor não leva para o EPUB (página 12).",
        "O livro inteiro virou um capítulo só. Divida-o na etapa de conversão com "
        "\"Começar capítulo aqui\", ou importe de novo separando os capítulos por "
        "outro nível de título.",
    ]


def test_resumo_de_epub_nao_fala_de_paginas():
    document = Document(chapters=[Chapter(title="Um", blocks=[Block(kind="paragraph", text="Oi.")])])
    summary = build_summary(document, None, ImportOptions(), 0.1)
    assert summary["source_kind"] == "epub" and summary["pages"] is None
    assert summary["warnings"] == []
