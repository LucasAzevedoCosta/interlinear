"""Ordem de leitura em páginas de duas colunas."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from PIL import Image  # noqa: E402  (vem com o reportlab)
from reportlab.lib.pagesizes import A4, A5  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.core.pdf_extract import extract_blocks  # noqa: E402

WIDTH, HEIGHT = A4
LEFT, RIGHT = 50, 310  # x de cada coluna
LEADING = 13


def _column(
    c: canvas.Canvas,
    x: float,
    top: float,
    paragraphs: list[list[str]],
    *,
    continued: bool = False,
) -> float:
    """Escreve parágrafos numa coluna, com recuo na primeira linha.

    continued: o primeiro parágrafo vem da coluna anterior e não tem recuo.
    """
    y = top
    for number, lines in enumerate(paragraphs):
        for i, line in enumerate(lines):
            indent = 12 if i == 0 and not (continued and number == 0) else 0
            c.drawString(x + indent, y, line)
            y -= LEADING
    return y


def _lines(tag: str, count: int, *, end: str = ".") -> list[str]:
    body = [f"{tag} linha {i} segue o texto da coluna sem pressa" for i in range(count)]
    body[-1] += end
    return body


@pytest.fixture(scope="module")
def pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "revista.pdf"
    c = canvas.Canvas(str(path), pagesize=A4)

    # Página 1: título que atravessa a página, duas colunas e um parágrafo
    # que começa embaixo da coluna esquerda e termina no topo da direita.
    c.setFont("Times-Bold", 20)
    c.drawString(WIDTH / 2 - 60, HEIGHT - 70, "Capítulo 1")
    c.setFont("Times-Roman", 10)
    top = HEIGHT - 110
    _column(c, LEFT, top, [_lines("A1", 6), _lines("A2", 5), _lines("PONTE início", 3, end=",")])
    _column(
        c, RIGHT, top, [_lines("PONTE fim", 3), _lines("B2", 6), _lines("B3", 5)], continued=True
    )
    c.showPage()

    # Página 2: duas faixas separadas por um subtítulo largo no meio, e uma
    # figura na coluna direita da faixa de cima.
    c.setFont("Times-Roman", 10)
    top = HEIGHT - 70
    _column(c, LEFT, top, [_lines("C1", 5), _lines("C2", 5)])
    end = _column(c, RIGHT, top, [_lines("D1", 4)])
    picture = io.BytesIO()
    Image.new("RGB", (160, 90), (20, 90, 160)).save(picture, "PNG")
    picture.seek(0)
    c.drawImage(ImageReader(picture), RIGHT, end - 90, 160, 80)
    c.setFont("Times-Roman", 10)
    _column(c, RIGHT, end - 100, [_lines("D2", 2)])

    c.setFont("Times-Bold", 14)
    c.drawString(WIDTH / 2 - 110, HEIGHT - 250, "Uma seção que atravessa as colunas")
    c.setFont("Times-Roman", 10)
    _column(c, LEFT, HEIGHT - 290, [_lines("E1", 5)])
    _column(c, RIGHT, HEIGHT - 290, [_lines("F1", 5)])
    c.showPage()
    c.save()
    return path


@pytest.fixture(scope="module")
def blocks(pdf):
    images: dict[str, bytes] = {}
    found, _ = extract_blocks(str(pdf), images=images)
    return found


def _tags(blocks) -> list[str]:
    return [b.text.split(" linha")[0] if b.kind != "image" else "IMG" for b in blocks]


def test_le_coluna_esquerda_inteira_antes_da_direita(blocks):
    assert _tags(blocks[:7]) == ["Capítulo 1", "A1", "A2", "PONTE início", "B2", "B3", "C1"]


def test_paragrafo_continua_da_coluna_esquerda_para_a_direita(blocks):
    ponte = next(b for b in blocks if b.text.startswith("PONTE início"))
    assert "PONTE fim linha 2" in ponte.text
    # Começa na esquerda e termina na direita, nessa ordem.
    assert ponte.text.index("PONTE início linha 2") < ponte.text.index("PONTE fim linha 0")


def test_titulo_largo_divide_a_pagina_em_faixas(blocks):
    page_two = [b for b in blocks if b.page == 2]
    assert _tags(page_two) == [
        "C1", "C2", "D1", "IMG", "D2",
        "Uma seção que atravessa as colunas",
        "E1", "F1",
    ]
    assert page_two[5].kind.startswith("heading")


def test_coluna_unica_com_linhas_curtas_nao_vira_duas_colunas(tmp_path):
    """Diálogo curto só tem linhas do lado esquerdo: não há calha."""
    path = tmp_path / "dialogo.pdf"
    width, height = A5
    c = canvas.Canvas(str(path), pagesize=A5)
    c.setFont("Times-Roman", 11)
    falas = [
        "— Você viu o relógio?",
        "— Vi.",
        "— E então?",
        "— Parado às três, como sempre esteve desde que o avô morreu naquela casa.",
        "— Às três?",
        "— Às três em ponto.",
        "Ela se calou e ficou olhando para a parede onde o relógio costumava ficar,",
        "como se esperasse que ele voltasse sozinho para o prego de sempre.",
        "— Vamos.",
        "— Vamos.",
    ]
    for i, fala in enumerate(falas):
        c.drawString(40, height - 80 - i * 15, fala)
    c.save()

    found, _ = extract_blocks(str(path))
    texts = " | ".join(b.text for b in found)
    assert texts.index("Você viu") < texts.index("Parado às três") < texts.index("Vamos")
