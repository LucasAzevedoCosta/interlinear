"""Itálico, negrito e imagens na extração de PDF."""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from PIL import Image  # noqa: E402  (vem com o reportlab)
from reportlab.lib.pagesizes import A5  # noqa: E402
from reportlab.lib.utils import ImageReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

from app.core.blocks import Document  # noqa: E402
from app.core.epub_write import write_epub  # noqa: E402
from app.core.pdf_extract import _font_flags, _render_styled, extract_blocks  # noqa: E402
from app.core.structure import split_into_chapters  # noqa: E402

FILLER = (
    "O relojoeiro abriu a caixa devagar e espalhou as peças sobre a bancada, "
    "uma a uma, sem pressa nenhuma de terminar o serviço daquela tarde."
)


def _picture(color: tuple[int, int, int], size=(160, 100), fmt="PNG") -> ImageReader:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, fmt, quality=90)
    buffer.seek(0)
    return ImageReader(buffer)


def _styled_line(c: canvas.Canvas, x: float, y: float, runs: list[tuple[str, str]]) -> None:
    text = c.beginText(x, y)
    for font, chunk in runs:
        text.setFont(font, 11)
        text.textOut(chunk)
    c.drawText(text)


@pytest.fixture(scope="module")
def pdf(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("pdf") / "ilustrado.pdf"
    c = canvas.Canvas(str(path), pagesize=A5)
    width, height = A5
    logo = _picture((200, 0, 0), size=(60, 60))

    for page in range(3):
        c.drawImage(logo, width - 70, height - 60, 50, 50)  # logotipo em toda página

        if page == 0:
            c.setFont("Times-Bold", 20)
            c.drawString(40, height - 100, "Capítulo 1")
            _styled_line(c, 40, height - 140, [
                ("Times-Roman", "Ele leu "),
                ("Times-Italic", "Dom Casmurro"),
                ("Times-Roman", " duas vezes e achou "),
                ("Times-Bold", "genial"),
                ("Times-Roman", "."),
            ])
            c.drawImage(_picture((0, 120, 200)), 40, height - 290, 160, 100)  # PNG
            c.drawImage(_picture((90, 90, 90), size=(8, 8)), 40, height - 320, 12, 12)  # ornamento
            c.setFont("Times-Roman", 11)
            c.drawString(40, height - 340, "Depois da figura vem este parágrafo.")
        else:
            c.setFont("Times-Roman", 11)
            for i in range(6):
                c.drawString(40, height - 120 - i * 14, FILLER[:70] if i % 2 else FILLER[70:])
            if page == 1:
                c.drawImage(_picture((30, 160, 60), fmt="JPEG"), 40, height - 330, 160, 100)
        c.showPage()

    c.save()
    return path


@pytest.fixture(scope="module")
def extracted(pdf):
    images: dict[str, bytes] = {}
    blocks, _ = extract_blocks(str(pdf), images=images)
    return blocks, images


# ---------------------------------------------------------------- estilos


def test_italico_e_negrito_viram_tags(extracted):
    blocks, _ = extracted
    paragraph = next(b for b in blocks if "Casmurro" in b.text)
    assert paragraph.text == (
        "Ele leu <em>Dom Casmurro</em> duas vezes e achou <strong>genial</strong>."
    )


def test_titulo_nao_leva_strong(extracted):
    blocks, _ = extracted
    heading = next(b for b in blocks if b.kind == "heading1")
    assert heading.text == "Capítulo 1"


@pytest.mark.parametrize(
    ("font", "expected"),
    [
        ("Times-Roman", (False, False)),
        ("Times-Italic", (False, True)),
        ("Helvetica-BoldOblique", (True, True)),
        ("ABCDEF+MinionPro-It", (False, True)),
        ("Garamond-BoldIt", (True, True)),
        ("Sabon-Bd", (True, False)),
        ("Bitstream-Charter", (False, False)),  # "it" no meio do nome não conta
        ("Literata-Regular", (False, False)),
    ],
)
def test_font_flags(font, expected):
    assert _font_flags(font) == expected


def test_render_styled_tira_espacos_das_tags_e_aninha():
    chars = [(c, "") for c in "um "] + [(c, "i") for c in "dois "] + [(c, "bi") for c in "três"]
    assert _render_styled(chars) == "um <em>dois</em> <strong><em>três</em></strong>"


# ---------------------------------------------------------------- imagens


def test_extrai_figuras_e_ignora_ornamento_e_logotipo(extracted):
    _, images = extracted
    assert sorted(images) == ["pdf-001.png", "pdf-002.jpg"]

    png = Image.open(io.BytesIO(images["pdf-001.png"]))
    assert (png.format, png.size, png.mode) == ("PNG", (160, 100), "RGB")
    assert png.getpixel((10, 10)) == (0, 120, 200)
    assert images["pdf-002.jpg"][:3] == b"\xff\xd8\xff"  # JPEG copiado como está


def test_figura_fica_entre_os_paragrafos_certos(extracted):
    blocks, _ = extracted
    kinds = [(b.kind, b.src or b.text[:12]) for b in blocks]
    figure = kinds.index(("image", "pdf-001.png"))
    assert "Casmurro" in blocks[figure - 1].text
    assert blocks[figure + 1].text.startswith("Depois da figura")
    assert blocks[figure].page == 1


def test_sem_o_parametro_images_nada_muda(pdf):
    blocks, _ = extract_blocks(str(pdf))
    assert not any(b.kind == "image" for b in blocks)


def test_figuras_chegam_ao_epub(extracted, tmp_path):
    blocks, images = extracted
    document = Document(title="Ilustrado", images=images)
    document.chapters = split_into_chapters(blocks)
    output = write_epub(document, tmp_path / "ilustrado.epub", use_translation=False)

    with zipfile.ZipFile(output) as archive:
        assert archive.read("OEBPS/images/pdf-001.png") == images["pdf-001.png"]
        opf = archive.read("OEBPS/content.opf").decode()
        assert 'href="images/pdf-002.jpg" media-type="image/jpeg"' in opf
        chapter = archive.read("OEBPS/text/ch0001.xhtml").decode()
        assert '<img src="../images/pdf-001.png"' in chapter
        assert "<em>Dom Casmurro</em>" in chapter
