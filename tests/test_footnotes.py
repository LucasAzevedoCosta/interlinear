"""Notas de rodapé: extração do PDF, chamada no texto e saída no EPUB."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from reportlab.lib.pagesizes import A5  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.core.blocks import TRANSLATABLE, Block, Chapter  # noqa: E402
from app.core.epub_write import chapter_to_xhtml  # noqa: E402
from app.core.pdf_extract import extract_blocks  # noqa: E402

WIDTH, HEIGHT = A5
X = 40
BODY, NOTE = 11, 8


def _line(c: canvas.Canvas, y: float, runs: list[tuple[str, bool]], *, indent: bool = False) -> None:
    """Uma linha de corpo; os trechos com True saem sobrescritos (chamada)."""
    text = c.beginText(X + (14 if indent else 0), y)
    for chunk, raised in runs:
        text.setFont("Times-Roman", 7 if raised else BODY)
        text.setRise(4 if raised else 0)
        text.textOut(chunk)
    c.drawText(text)


def _note(c: canvas.Canvas, y: float, marker: str, text: str) -> None:
    line = c.beginText(X, y)
    line.setFont("Times-Roman", 6)
    line.setRise(3)
    line.textOut(marker)
    line.setFont("Times-Roman", NOTE)
    line.setRise(0)
    line.textOut(text)
    c.drawText(line)


def _note_line(c: canvas.Canvas, y: float, text: str) -> None:
    c.setFont("Times-Roman", NOTE)
    c.drawString(X, y, text)


@pytest.fixture(scope="module")
def pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "notas.pdf"
    c = canvas.Canvas(str(path), pagesize=A5)

    # Página 1: título, parágrafo com chamada, parágrafo que continua na
    # página 2, e uma nota de duas linhas no pé.
    c.setFont("Times-Bold", 18)
    c.drawString(X, HEIGHT - 80, "Capítulo 1")
    _line(c, HEIGHT - 120, [("O relojoeiro guardava o pêndulo", False), ("1", True),
                            (" numa gaveta.", False)], indent=True)
    _line(c, HEIGHT - 134, [("A menina voltou no dia seguinte e perguntou", False)], indent=True)
    _line(c, HEIGHT - 148, [("pelo relógio, que ainda estava desmontado sobre a", False)])
    _note(c, 70, "1", " Pêndulo de latão, do modelo que a fábrica deixou de")
    _note_line(c, 60, "produzir em 1931, quando o dono morreu.")
    c.showPage()

    # Página 2: o parágrafo termina, outro traz duas chamadas, notas "*" e "2".
    _line(c, HEIGHT - 60, [("bancada, esperando a peça que faltava.", False)])
    _line(c, HEIGHT - 74, [("Teodoro disse que era o modelo antigo", False), ("*", True),
                           (" e que a peça", False)], indent=True)
    _line(c, HEIGHT - 88, [("viria de Lisboa", False), ("2", True), (" no fim do mês.", False)])
    _note(c, 70, "*", " Nota do tradutor: o modelo de 1931.")
    _note(c, 60, "2", " A loja da Rua do Ouro.")
    c.showPage()

    # Página 3: legenda pequena no pé, sem marcador. Não é nota.
    _line(c, HEIGHT - 60, [("Ninguém mais tocou no assunto.", False)], indent=True)
    c.setFont("Times-Italic", NOTE)
    c.drawString(X, 70, "Desenho de Teodoro, guardado na gaveta da oficina.")
    c.showPage()
    c.save()
    return path


@pytest.fixture(scope="module")
def blocks(pdf):
    found, _ = extract_blocks(str(pdf))
    return found


def _notes(blocks):
    return [b for b in blocks if b.kind == "footnote"]


def test_notas_viram_blocos_com_marcador(blocks):
    notes = _notes(blocks)
    assert [n.meta["marker"] for n in notes] == ["1", "*", "2"]
    assert notes[0].text == (
        "Pêndulo de latão, do modelo que a fábrica deixou de produzir em 1931, "
        "quando o dono morreu."
    )
    assert notes[1].text == "Nota do tradutor: o modelo de 1931."
    assert notes[0].page == 1


def test_chamada_vira_sup(blocks):
    texts = [b.text for b in blocks]
    assert "O relojoeiro guardava o pêndulo<sup>1</sup> numa gaveta." in texts
    assert any("modelo antigo<sup>*</sup> e que" in t and "Lisboa<sup>2</sup> no" in t for t in texts)


def test_paragrafo_atravessa_a_pagina_por_cima_das_notas(blocks):
    texts = [b.text for b in blocks]
    assert (
        "A menina voltou no dia seguinte e perguntou pelo relógio, que ainda "
        "estava desmontado sobre a bancada, esperando a peça que faltava."
    ) in texts
    # A nota da página 1 vem depois do parágrafo em que a página termina.
    order = [b.kind for b in blocks]
    assert order[:4] == ["heading1", "paragraph", "paragraph", "footnote"]


def test_texto_pequeno_sem_marcador_nao_vira_nota(blocks):
    caption = next(b for b in blocks if "Desenho de Teodoro" in b.text)
    assert caption.kind == "paragraph"


def test_nota_e_traduzivel():
    assert "footnote" in TRANSLATABLE


def test_epub_liga_chamada_e_nota_no_fim_do_capitulo():
    call = Block(kind="paragraph", text="Um ordinal 1<sup>o</sup> e uma chamada<sup>1</sup>.")
    second = Block(kind="paragraph", text="Outra página, de novo<sup>1</sup>.")
    note_a = Block(kind="footnote", text="Primeira.", meta={"marker": "1"}, translated="First.")
    note_b = Block(kind="footnote", text="Segunda.", meta={"marker": "1"})
    orphan = Block(kind="footnote", text="Sem chamada.", meta={"marker": "9"})
    chapter = Chapter(title="C", blocks=[call, note_a, second, note_b, orphan])

    xhtml = chapter_to_xhtml(chapter)
    etree.fromstring(xhtml.encode())  # XHTML bem-formado, com o prefixo epub:

    body, notes = xhtml.split('<section class="footnotes"')
    assert "Primeira" not in body and "First" not in body
    # Cada chamada "1" liga à nota "1" da sua vez, não sempre à primeira.
    assert f'href="#n{note_a.id}">1</a></sup>.</p>' in body
    assert f'id="r{note_b.id}" href="#n{note_b.id}"' in body
    assert body.index(f"#n{note_a.id}") < body.index(f"#n{note_b.id}")
    assert "1<sup>o</sup>" in body  # sem nota "o": fica como está
    # As notas, traduzidas, com link de volta só quando há chamada.
    assert f'<aside epub:type="footnote" class="footnote" id="n{note_a.id}">' in notes
    assert f'<a href="#r{note_a.id}">1</a></span> First.' in notes
    assert '<span class="note-marker">9</span> Sem chamada.' in notes


def test_capitulo_sem_notas_nao_ganha_secao():
    chapter = Chapter(title="C", blocks=[Block(kind="paragraph", text="Texto<sup>1</sup>.")])
    xhtml = chapter_to_xhtml(chapter)
    assert "footnotes" not in xhtml
    assert "Texto<sup>1</sup>." in xhtml
