"""Testes do caminho completo: PDF → blocos → capítulos → EPUB → blocos.

O teste que mais importa é o de ida e volta: se um livro sobrevive à conversão
para EPUB e à leitura de volta sem perder parágrafos, a edição e a tradução
podem confiar na estrutura.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.core.epub_read import read_epub  # noqa: E402
from app.core.epub_write import inline_html, write_epub  # noqa: E402
from app.core.pdf_extract import extract_blocks  # noqa: E402
from app.core.structure import split_into_chapters  # noqa: E402


@pytest.fixture(scope="module")
def pdf_path() -> Path:
    from make_fixture import gerar

    destino = ROOT / "tests" / "fixtures" / "livro_teste.pdf"
    if not destino.exists():
        gerar(destino)
    return destino


@pytest.fixture(scope="module")
def extracted(pdf_path: Path):
    return extract_blocks(str(pdf_path))


# --------------------------------------------------------------- extração


def test_extrai_blocos(extracted):
    blocks, _ = extracted
    assert len(blocks) >= 15


def test_remove_cabecalho_e_numero_de_pagina(extracted):
    blocks, _ = extracted
    textos = [b.text for b in blocks]
    assert not any("A OFICINA DE RELÓGIOS" in t for t in textos)
    # Nenhum bloco é só um número de página.
    assert not any(t.strip().isdigit() for t in textos)


def test_detecta_tres_titulos(extracted):
    blocks, _ = extracted
    titulos = [b for b in blocks if b.kind == "heading1"]
    assert len(titulos) == 3
    assert titulos[0].text.startswith("Capítulo 1")


def test_dialogo_nao_vira_lista(extracted):
    """Travessão abre fala em português; tratá-lo como marcador quebra o livro."""
    blocks, _ = extracted
    falas = [b for b in blocks if b.text.startswith("—")]
    assert falas, "a fixture precisa conter diálogo"
    assert all(b.kind == "paragraph" for b in falas)


def test_junta_paragrafo_partido_entre_paginas(extracted):
    """Nenhum parágrafo deve começar com letra minúscula: sinal de corte errado."""
    blocks, _ = extracted
    corpo = [b for b in blocks if b.kind == "paragraph"]
    quebrados = [b for b in corpo if b.text[:1].islower()]
    assert not quebrados, f"parágrafos cortados: {[b.text[:40] for b in quebrados]}"


# ------------------------------------------------------------- capítulos


def test_divide_em_capitulos(extracted):
    blocks, _ = extracted
    chapters = split_into_chapters(blocks)
    assert len(chapters) == 3
    assert all(c.blocks for c in chapters)


def test_nenhum_bloco_perdido_na_divisao(extracted):
    blocks, _ = extracted
    chapters = split_into_chapters(blocks)
    total = sum(len(c.blocks) for c in chapters)
    assert total == len(blocks)


def test_divisao_sem_heading1_usa_nivel_seguinte():
    blocks = [
        Block(kind="heading2", text="Uma seção"),
        *[Block(kind="paragraph", text="Texto de corpo. " * 12) for _ in range(3)],
        Block(kind="heading2", text="Outra seção"),
        *[Block(kind="paragraph", text="Mais texto de corpo. " * 12) for _ in range(3)],
    ]
    chapters = split_into_chapters(blocks)
    assert len(chapters) == 2


# ------------------------------------------------------------------ EPUB


@pytest.fixture
def documento(extracted) -> Document:
    blocks, title = extracted
    doc = Document(title="A Oficina de Relógios", author="Teste", language="pt-BR")
    doc.chapters = split_into_chapters(blocks)
    return doc


def test_epub_tem_estrutura_valida(documento: Document, tmp_path: Path):
    saida = write_epub(documento, tmp_path / "livro.epub")

    with zipfile.ZipFile(saida) as z:
        nomes = z.namelist()
        # O mimetype precisa ser o primeiro item e ficar sem compressão.
        assert nomes[0] == "mimetype"
        assert z.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert z.read("mimetype") == b"application/epub+zip"

        assert "META-INF/container.xml" in nomes
        assert "OEBPS/content.opf" in nomes
        assert "OEBPS/nav.xhtml" in nomes
        assert "OEBPS/toc.ncx" in nomes
        assert sum(1 for n in nomes if n.startswith("OEBPS/text/")) == 3


def test_xhtml_dos_capitulos_e_bem_formado(documento: Document, tmp_path: Path):
    from lxml import etree

    saida = write_epub(documento, tmp_path / "livro.epub")
    with zipfile.ZipFile(saida) as z:
        for nome in (n for n in z.namelist() if n.endswith(".xhtml")):
            etree.fromstring(z.read(nome))  # levanta se o XML estiver quebrado


def test_ida_e_volta_preserva_paragrafos(documento: Document, tmp_path: Path):
    saida = write_epub(documento, tmp_path / "livro.epub")
    relido = read_epub(saida)

    assert len(relido.chapters) == len(documento.chapters)
    assert relido.title == documento.title

    originais = [b.text for b in documento.all_blocks() if b.text.strip()]
    voltaram = [b.text for b in relido.all_blocks() if b.text.strip()]
    assert voltaram == originais


def test_exporta_traducao_quando_existe(documento: Document, tmp_path: Path):
    for bloco in documento.all_blocks():
        if bloco.kind == "paragraph":
            bloco.translated = "Texto traduzido de teste."

    traduzido = read_epub(write_epub(documento, tmp_path / "t.epub", use_translation=True))
    original = read_epub(write_epub(documento, tmp_path / "o.epub", use_translation=False))

    textos_traduzidos = [b.text for b in traduzido.all_blocks() if b.kind == "paragraph"]
    assert set(textos_traduzidos) == {"Texto traduzido de teste."}

    textos_originais = [b.text for b in original.all_blocks() if b.kind == "paragraph"]
    assert "Texto traduzido de teste." not in textos_originais


# ----------------------------------------------------------- segurança XHTML


def test_inline_html_preserva_formatacao_permitida():
    resultado = inline_html("um <em>itálico</em> e um <strong>negrito</strong>")
    assert "<em>itálico</em>" in resultado
    assert "<strong>negrito</strong>" in resultado


def test_inline_html_neutraliza_marcacao_perigosa():
    resultado = inline_html('texto <script>alert(1)</script> fim')
    assert "<script>" not in resultado
    assert "&lt;script&gt;" in resultado


def test_inline_html_escapa_e_comercial():
    assert inline_html("Tom & Jerry") == "Tom &amp; Jerry"


def test_lista_vira_ul_valido():
    from app.core.epub_write import chapter_to_xhtml
    from lxml import etree

    capitulo = Chapter(
        title="Lista",
        blocks=[
            Block(kind="paragraph", text="Antes"),
            Block(kind="list_item", text="Primeiro"),
            Block(kind="list_item", text="Segundo"),
            Block(kind="paragraph", text="Depois"),
        ],
    )
    xhtml = chapter_to_xhtml(capitulo)
    # lxml exige bytes quando o documento declara a codificação.
    etree.fromstring(xhtml.encode("utf-8"))
    assert xhtml.count("<ul>") == 1 and xhtml.count("</ul>") == 1


# ---------------------------------------------------------------- tradutor


def test_lotes_respeitam_orcamento():
    from app.ai.translator import build_batches

    blocos = [Block(kind="paragraph", text="x" * 400) for _ in range(10)]
    lotes = build_batches(blocos, max_chars=1000)
    assert all(sum(len(b.text) for b in lote) <= 1400 for lote in lotes)
    assert sum(len(lote) for lote in lotes) == 10


def test_parser_de_segmentos_aceita_resposta_correta():
    from app.ai.translator import _parse_segments

    resposta = "⟦1⟧\nPrimeiro\n\n⟦2⟧\nSegundo"
    assert _parse_segments(resposta, 2) == ["Primeiro", "Segundo"]


def test_parser_de_segmentos_recusa_contagem_errada():
    from app.ai.translator import _parse_segments

    assert _parse_segments("⟦1⟧\nSó um", 2) is None
    assert _parse_segments("⟦2⟧\nA\n\n⟦1⟧\nB", 2) is None  # fora de ordem
    assert _parse_segments("⟦1⟧\n\n⟦2⟧\nB", 2) is None  # segmento vazio
