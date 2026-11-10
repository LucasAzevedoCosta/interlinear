"""OCR de PDF escaneado: hOCR, tamanhos e o caminho completo com Tesseract."""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from reportlab.lib.pagesizes import A5  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.core import ocr  # noqa: E402
from app.core.pdf_extract import ExtractionReport, Line, extract_blocks  # noqa: E402

WIDTH, HEIGHT = A5
X = 50

PARAGRAPHS = [
    [
        "O relojoeiro abriu a caixa devagar e espalhou",
        "as peças sobre a bancada, uma a uma, sem pres-",
        "sa nenhuma de terminar o serviço daquela tarde.",
    ],
    [
        "A menina voltou no dia seguinte e perguntou",
        "pelo pêndulo, que ainda estava desmontado.",
    ],
]


def _draw_page(c: canvas.Canvas, *, title: bool) -> None:
    y = HEIGHT - 80
    if title:
        c.setFont("Times-Bold", 20)
        c.drawString(X, y, "Capítulo 1")
        y -= 50
    c.setFont("Times-Roman", 12)
    for lines in PARAGRAPHS:
        for i, line in enumerate(lines):
            c.drawString(X + (16 if i == 0 else 0), y, line)
            y -= 16


def _scanned(path: Path, pages: int = 2, *, native: tuple[int, ...] = ()) -> None:
    """Um PDF com imagens das páginas: desenha o texto, rasteriza e cola.

    As páginas em `native` (contando de 1) ficam com o texto do PDF, sem
    rasterizar: um PDF misto.
    """
    import pypdfium2 as pdfium

    source = io.BytesIO()
    c = canvas.Canvas(source, pagesize=A5)
    for page in range(pages):
        _draw_page(c, title=page == 0)
        c.showPage()
    c.save()

    document = pdfium.PdfDocument(source.getvalue())
    out = canvas.Canvas(str(path), pagesize=A5)
    for index in range(len(document)):
        if index + 1 in native:
            _draw_page(out, title=index == 0)
        else:
            image = document[index].render(scale=300 / 72).to_pil()
            buffer = io.BytesIO()
            image.convert("L").save(buffer, "PNG")
            buffer.seek(0)
            out.drawImage(ImageReader(buffer), 0, 0, WIDTH, HEIGHT)
        out.showPage()
    out.save()
    document.close()


HOCR = b"""<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><body>
<div class="ocr_page" title="bbox 0 0 1748 2480">
 <span class="ocr_header" title="bbox 208 300 640 380; baseline 0 -10; x_size 83.3">
  <span class="ocrx_word" title="bbox 208 300 500 380">Cap\xc3\xadtulo</span>
  <span class="ocrx_word" title="bbox 520 300 640 380">1</span>
 </span>
 <span class="ocr_line" title="bbox 275 500 1500 550; baseline 0 -12; x_size 50">
  <span class="ocrx_word" title="bbox 275 500 400 550">O</span>
  <span class="ocrx_word" title="bbox 410 500 800 550">rel\xc3\xb3gio</span>
 </span>
 <span class="ocr_line" title="bbox 208 560 1500 610; x_size 47.5">
  <span class="ocrx_word" title="bbox 208 560 500 610"> </span>
 </span>
</div></body></html>"""


def test_parse_hocr_converte_para_pontos():
    lines = ocr.parse_hocr(HOCR, page=3, page_height=595.0)

    assert [line.text for line in lines] == ["Capítulo 1", "O relógio"]  # linha vazia some
    title, body = lines
    assert title.page == 3 and title.page_height == 595.0
    # 300 dpi: 1 pixel = 0,24 ponto.
    assert title.x0 == pytest.approx(208 * 0.24)
    assert title.top == pytest.approx(300 * 0.24)
    assert title.size == 20.0  # 83,3 px = 19,99 pt, arredondado ao meio ponto
    assert body.size == 12.0
    assert body.chars[0] == ("O", "")


def test_tamanho_do_corpo_absorve_o_ruido_do_ocr():
    def line(size: float) -> Line:
        return Line("x", size, False, False, 0, 10, 0, 1, 100)

    lines = [line(s) for s in (12.0, 11.5, 12.5, 12.0, 13.0, 20.0)]
    ocr._snap_body_size(lines)
    assert [l.size for l in lines] == [12.0, 12.0, 12.0, 12.0, 12.0, 20.0]


def test_margem_com_ruido_nao_perde_para_o_recuo():
    """Margem em 50,4 e 50,6 se dividia entre 50 e 51 e empatava com o recuo."""
    from app.core.pdf_extract import _group_into_paragraphs

    def line(x0: float, top: float, text: str) -> Line:
        return Line(text, 11.0, False, False, x0, 300, top, 1, 595)

    lines = [
        line(51.1, 100, "a"), line(66.5, 116, "b"), line(50.4, 132, "c"),
        line(50.6, 148, "d"), line(66.2, 164, "e"), line(50.2, 180, "f"),
        line(66.5, 196, "g"), line(50.4, 212, "h"), line(50.6, 228, "i"),
        line(66.2, 244, "j"), line(50.2, 260, "k"),
    ]
    groups = _group_into_paragraphs(lines, 11.0)
    assert [[l.text for l in g] for g in groups] == [
        ["a"], ["b", "c", "d"], ["e", "f"], ["g", "h", "i"], ["j", "k"],
    ]


def test_sem_tesseract_explica_o_que_fazer(tmp_path, monkeypatch):
    path = tmp_path / "branco.pdf"
    c = canvas.Canvas(str(path), pagesize=A5)
    c.rect(50, 50, 100, 100)
    c.save()
    monkeypatch.setattr(ocr, "available", lambda: False)

    with pytest.raises(ValueError, match="Tesseract"):
        extract_blocks(str(path))


@pytest.mark.skipif(not ocr.available(), reason="Tesseract não instalado")
def test_pdf_escaneado_passa_pelo_ocr(tmp_path):
    path = tmp_path / "escaneado.pdf"
    _scanned(path)
    images: dict[str, bytes] = {}

    blocks, _ = extract_blocks(str(path), images=images)

    assert blocks[0].kind == "heading1"
    assert blocks[0].text == "Capítulo 1"
    texts = [b.text for b in blocks if b.kind == "paragraph"]
    assert texts[0] == (
        "O relojoeiro abriu a caixa devagar e espalhou as peças sobre a bancada, "
        "uma a uma, sem pressa nenhuma de terminar o serviço daquela tarde."
    )
    assert texts[1] == (
        "A menina voltou no dia seguinte e perguntou pelo pêndulo, que ainda "
        "estava desmontado."
    )
    assert len(texts) == 4
    # A imagem da página escaneada não vira figura do livro.
    assert not images and all(b.kind != "image" for b in blocks)


def test_corpo_do_ocr_vira_o_corpo_do_pdf():
    """Num PDF misto, o corpo do OCR se ajusta ao do texto do PDF, e o título acompanha."""
    def line(size: float) -> Line:
        return Line("x", size, False, False, 0, 10, 0, 1, 100)

    lines = [line(s) for s in (12.0, 11.5, 12.0, 18.0)]
    ocr._snap_body_size(lines, 10.0)
    assert [l.size for l in lines] == [10.0, 10.0, 10.0, 15.0]


def test_pdf_misto_sem_tesseract_fica_com_o_texto_e_avisa(tmp_path, monkeypatch):
    path = tmp_path / "misto.pdf"
    _scanned(path, pages=3, native=(1, 3))
    monkeypatch.setattr(ocr, "available", lambda: False)
    report = ExtractionReport()

    blocks, _ = extract_blocks(str(path), report=report)

    assert sum(b.kind == "paragraph" for b in blocks) == 4  # 2 por página com texto
    assert report.pages == 3
    assert report.scanned_without_ocr == [2]
    assert report.ocr_pages == []


def test_ocr_desligado_em_pdf_sem_texto_explica(tmp_path):
    path = tmp_path / "escaneado.pdf"
    _scanned(path, pages=1)
    with pytest.raises(ValueError, match="OCR está desligado"):
        extract_blocks(str(path), ocr_mode="off")


def test_progresso_da_extracao(tmp_path):
    path = tmp_path / "nativo.pdf"
    _scanned(path, pages=2, native=(1, 2))
    seen: list[tuple[str, int, int]] = []

    extract_blocks(str(path), on_progress=lambda *step: seen.append(step))

    assert seen == [("read", 1, 2), ("read", 2, 2), ("build", 0, 1), ("build", 1, 1)]


def test_excecao_no_progresso_cancela_a_extracao(tmp_path):
    path = tmp_path / "nativo.pdf"
    _scanned(path, pages=3, native=(1, 2, 3))
    seen: list[int] = []

    class Cancelled(Exception):
        pass

    def stop_on_second(stage: str, done: int, total: int) -> None:
        seen.append(done)
        if done == 2:
            raise Cancelled

    with pytest.raises(Cancelled):
        extract_blocks(str(path), on_progress=stop_on_second)
    assert seen == [1, 2]  # a terceira página nem é lida


@pytest.mark.skipif(not ocr.available(), reason="Tesseract não instalado")
def test_pdf_misto_faz_ocr_so_das_paginas_escaneadas(tmp_path):
    path = tmp_path / "misto.pdf"
    _scanned(path, pages=3, native=(1, 3))
    images: dict[str, bytes] = {}
    report = ExtractionReport()
    stages: list[tuple[str, int, int]] = []

    blocks, _ = extract_blocks(
        str(path), images=images, report=report, on_progress=lambda *s: stages.append(s)
    )

    assert report.ocr_pages == [2]
    assert report.scanned_without_ocr == [] and report.ocr_empty == []
    assert ("ocr", 1, 1) in stages
    # A página 2 vem do OCR, no lugar certo, com os mesmos parágrafos.
    pages = [b.page for b in blocks if b.kind == "paragraph"]
    assert pages == [1, 1, 2, 2, 3, 3]
    texts = [b.text for b in blocks if b.kind == "paragraph"]
    assert texts[2] == texts[0] and texts[3] == texts[1]
    # O corpo do OCR é o do PDF: o parágrafo escaneado não virou título nem nota.
    assert [b.kind for b in blocks if b.page == 2] == ["paragraph", "paragraph"]
    assert not images


@pytest.mark.skipif(not ocr.available(), reason="Tesseract não instalado")
def test_ocr_forcado_ignora_o_texto_do_pdf(tmp_path):
    path = tmp_path / "nativo.pdf"
    _scanned(path, pages=1, native=(1,))
    report = ExtractionReport()

    blocks, _ = extract_blocks(str(path), ocr_mode="force", report=report)

    assert report.ocr_pages == [1]
    assert blocks[0].text == "Capítulo 1"
    assert sum(b.kind == "paragraph" for b in blocks) == 2
