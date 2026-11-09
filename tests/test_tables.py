"""Tabelas: detecção no PDF, saída no EPUB e junção no editor."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from reportlab.lib.pagesizes import A5  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.core.blocks import TRANSLATABLE, Block, Chapter, Document  # noqa: E402
from app.core.epub_write import chapter_to_xhtml  # noqa: E402
from app.core.pdf_extract import extract_blocks  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402

WIDTH, HEIGHT = A5
X = 40
LEADING = 14

PROSE = [
    "O relojoeiro anotava tudo num caderno de capa dura, com a letra miúda",
    "de quem aprendeu a escrever em papel caro e nunca perdeu o costume.",
]


def _prose(c: canvas.Canvas, y: float) -> float:
    c.setFont("Times-Roman", 11)
    c.drawString(X + 14, y, PROSE[0])
    c.drawString(X, y - LEADING, PROSE[1])
    return y - LEADING * 3


@pytest.fixture(scope="module")
def pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "tabelas.pdf"
    c = canvas.Canvas(str(path), pagesize=A5)

    # Página 1: texto, tabela de 3 colunas com cabeçalho em negrito e preços
    # alinhados à direita, texto.
    y = _prose(c, HEIGHT - 60)
    c.setFont("Times-Bold", 10)
    for x, label in ((X, "Peça"), (X + 140, "Ano"), (X + 220, "Preço")):
        c.drawString(x, y, label)
    c.setFont("Times-Roman", 10)
    rows = [("Pêndulo de latão", "1931", "12,00"), ("Mola real", "1945", "3,50"),
            ("Escape de âncora", "1902", "140,00"), ("Mostrador", "1931", "8,75")]
    for row in rows:
        y -= LEADING
        c.drawString(X, y, row[0])
        c.drawString(X + 140, y, row[1])
        c.drawRightString(X + 260, y, row[2])
    _prose(c, y - LEADING * 2)
    c.showPage()

    # Página 2: tabela de 2 colunas curtas (glossário), que não pode virar
    # leitura em duas colunas.
    y = _prose(c, HEIGHT - 60)
    c.setFont("Times-Roman", 10)
    for term, meaning in [("escape", "peça que solta"), ("mola", "guarda a força"),
                          ("pêndulo", "marca o tempo"), ("coroa", "dá corda")]:
        c.drawString(X, y, term)
        c.drawString(X + 170, y, meaning)
        y -= LEADING
    _prose(c, y - LEADING)
    c.showPage()
    c.save()
    return path


@pytest.fixture(scope="module")
def blocks(pdf):
    found, _ = extract_blocks(str(pdf))
    return found


def test_tabela_de_tres_colunas_vira_um_bloco(blocks):
    page_one = [b for b in blocks if b.page == 1]
    assert [b.kind for b in page_one] == ["paragraph", "table", "paragraph"]
    table = page_one[1]
    assert table.text == (
        "Peça | Ano | Preço\n"
        "Pêndulo de latão | 1931 | 12,00\n"
        "Mola real | 1945 | 3,50\n"
        "Escape de âncora | 1902 | 140,00\n"
        "Mostrador | 1931 | 8,75"
    )
    assert table.meta["header"] is True


def test_tabela_de_duas_colunas_nao_vira_leitura_em_colunas(blocks):
    page_two = [b for b in blocks if b.page == 2]
    assert [b.kind for b in page_two] == ["paragraph", "table", "paragraph"]
    assert page_two[1].text.split("\n")[0] == "escape | peça que solta"
    assert page_two[1].meta["header"] is False
    assert page_two[0].text == " ".join(PROSE)


def test_texto_corrido_nao_vira_tabela(blocks):
    assert all(b.kind != "table" for b in blocks if "relojoeiro" in b.text)


def test_tabela_e_traduzivel():
    assert "table" in TRANSLATABLE


def test_epub_monta_table():
    table = Block(
        kind="table",
        text="Peça | Ano\nMola <em>real</em> | 1945\nSó uma célula",
        translated="Part | Year\nMain <em>spring</em> | 1945\nOne cell only",
        meta={"header": True},
    )
    xhtml = chapter_to_xhtml(Chapter(title="C", blocks=[table]))
    etree.fromstring(xhtml.encode())
    assert "<tr><th>Part</th><th>Year</th></tr>" in xhtml
    assert "<tr><td>Main <em>spring</em></td><td>1945</td></tr>" in xhtml
    # Linha que perdeu um "|" na tradução é completada, não entorta a tabela.
    assert "<tr><td>One cell only</td><td></td></tr>" in xhtml


def test_epub_escapa_celulas():
    table = Block(kind="table", text="<img src=x onerror=alert(1)> | b\nc | d")
    xhtml = chapter_to_xhtml(Chapter(title="C", blocks=[table]), use_translation=False)
    assert "<img" not in xhtml
    assert "&lt;img" in xhtml


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


def test_juntar_tabela_partida_na_quebra_de_pagina(client):
    blocks = [
        Block(kind="table", text="Peça | Ano\nMola-", translated="Part | Year\nSpring-", id="t1"),
        Block(kind="table", text="real | 1945", translated="main | 1945", id="t2"),
    ]
    document = Document(chapters=[Chapter(title="Um", blocks=blocks, id="c1")])
    project = get_store().create(document, source_kind="epub", source_filename="x.epub").id

    response = client.post(f"/api/projects/{project}/blocks/t1/merge-next")

    assert response.status_code == 200
    merged = get_store().load_document(project).chapters[0].blocks
    assert len(merged) == 1
    assert merged[0].text == "Peça | Ano\nMola-\nreal | 1945"
    assert merged[0].translated == "Part | Year\nSpring-\nmain | 1945"
