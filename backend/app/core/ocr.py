"""OCR de PDFs escaneados, para quando o PDF não tem texto.

Cada página é renderizada com pypdfium2 (Apache-2.0/BSD) e lida pelo
Tesseract (Apache-2.0), chamado como programa externo. O Tesseract devolve
hOCR: o texto de cada linha com a posição e uma estimativa do tamanho da
letra. Isso vira a mesma lista de Line que pdf_extract lê de um PDF com texto,
então o resto do pipeline (parágrafos, títulos, hífen, colunas, notas,
tabelas) vale igual.

Ficou de fora o ocrmypdf, o caminho mais comum: ele depende do Ghostscript,
que é AGPL. Veja docs/licencas.md.

Itálico e negrito não são detectados: o Tesseract 4+ não informa a fonte.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from pathlib import Path

from lxml import html as lxml_html

from .pdf_extract import Line, _reading_order

log = logging.getLogger(__name__)

# Resolução da renderização. O Tesseract é calibrado para 300 dpi; menos que
# isso perde acentos em corpo 10.
DPI = 300

# Linhas com tamanho até este tanto longe da mediana são do corpo. A
# estimativa do Tesseract oscila de linha para linha, e o extrator parte o
# parágrafo quando o tamanho muda.
BODY_SNAP = 0.12

# Idiomas pedidos ao Tesseract, na ordem de preferência. Só os instalados são
# usados.
DEFAULT_LANGUAGES = "por+eng"

_X_SIZE = re.compile(r"x_size\s+([\d.]+)")
_BBOX = re.compile(r"bbox\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)")


def tesseract_path() -> str | None:
    return shutil.which(os.environ.get("INTERLINEAR_TESSERACT", "tesseract"))


def available() -> bool:
    """Diz se há como fazer OCR nesta máquina (Tesseract e pypdfium2)."""
    if tesseract_path() is None:
        return False
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def _installed_languages() -> frozenset[str]:
    result = subprocess.run(
        [tesseract_path() or "tesseract", "--list-langs"],
        capture_output=True,
        text=True,
        check=False,
    )
    # A primeira linha é um cabeçalho ("List of available languages ...").
    return frozenset(line.strip() for line in result.stdout.splitlines()[1:] if line.strip())


def languages() -> str:
    wanted = os.environ.get("INTERLINEAR_OCR_LANGS", DEFAULT_LANGUAGES).split("+")
    installed = _installed_languages()
    chosen = [lang for lang in wanted if lang in installed]
    return "+".join(chosen) or "eng"


def read_lines(pdf_path: str) -> list[Line]:
    """Faz OCR de todas as páginas e devolve as linhas, como _read_lines.

    As páginas são lidas em paralelo, um processo do Tesseract por núcleo, cada
    um limitado a uma thread (OMP_THREAD_LIMIT): é mais rápido que um processo
    só usando todas.
    """
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(pdf_path)
    try:
        lang = languages()
        log.info("PDF sem texto: OCR de %d páginas (%s)", len(document), lang)
        with tempfile.TemporaryDirectory(prefix="interlinear-ocr-") as tmp:
            jobs = []
            for number in range(len(document)):
                page = document[number]
                width, height = page.get_size()
                image = Path(tmp) / f"{number + 1:05d}.pgm"
                _render(page, image)
                page.close()
                jobs.append((number + 1, image, width, height))

            workers = max(1, min(len(jobs), os.cpu_count() or 1))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                pages = list(pool.map(lambda job: _ocr_page(*job, lang=lang), jobs))
    finally:
        document.close()

    lines = [line for page in pages for line in page]
    _snap_body_size(lines)
    return lines


def _render(page, path: Path) -> None:
    """Grava a página em tons de cinza como PGM, que o Tesseract lê direto.

    PGM é um cabeçalho de texto seguido dos bytes da imagem: dispensa o Pillow.
    """
    bitmap = page.render(scale=DPI / 72, grayscale=True)
    width, height, stride = bitmap.width, bitmap.height, bitmap.stride
    buffer = bytes(bitmap.buffer)
    if stride != width:
        buffer = b"".join(buffer[row * stride : row * stride + width] for row in range(height))
    path.write_bytes(f"P5\n{width} {height}\n255\n".encode() + buffer)
    bitmap.close()


def _ocr_page(number: int, image: Path, width: float, height: float, *, lang: str) -> list[Line]:
    result = subprocess.run(
        [tesseract_path() or "tesseract", str(image), "stdout", "-l", lang, "hocr"],
        capture_output=True,
        env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        check=False,
    )
    if result.returncode != 0:
        log.warning(
            "OCR falhou na página %d: %s", number, result.stderr.decode(errors="replace")[-300:]
        )
        return []
    # Mesma ordem de leitura do PDF com texto: colunas e tabelas.
    return _reading_order(parse_hocr(result.stdout, page=number, page_height=height), [])


def parse_hocr(content: bytes, *, page: int, page_height: float) -> list[Line]:
    """Converte o hOCR de uma página em Line, com medidas em pontos."""
    if not content.strip():
        return []
    tree = lxml_html.fromstring(content)
    scale = 72 / DPI
    lines: list[Line] = []
    for element in tree.xpath(
        "//*[contains(concat(' ', @class, ' '), ' ocr_line ')"
        " or contains(concat(' ', @class, ' '), ' ocr_header ')"
        " or contains(concat(' ', @class, ' '), ' ocr_caption ')"
        " or contains(concat(' ', @class, ' '), ' ocr_textfloat ')]"
    ):
        words = [
            word.text_content().strip()
            for word in element.xpath(".//*[contains(concat(' ', @class, ' '), ' ocrx_word ')]")
        ]
        text = " ".join(word for word in words if word)
        title = element.get("title", "")
        box = _BBOX.search(title)
        if not text or not box:
            continue
        x0, top, x1, _ = (int(value) for value in box.groups())
        size_match = _X_SIZE.search(title)
        size = float(size_match.group(1)) * scale if size_match else 0.0
        lines.append(
            Line(
                text=text,
                size=round(size * 2) / 2,  # meio ponto: a estimativa é ruidosa
                bold=False,
                italic=False,
                x0=x0 * scale,
                x1=x1 * scale,
                top=top * scale,
                page=page,
                page_height=page_height,
                chars=[(char, "") for char in text],
            )
        )
    return lines


def _snap_body_size(lines: list[Line]) -> None:
    """Iguala ao corpo os tamanhos que só diferem dele pelo ruído do OCR."""
    if not lines:
        return
    # O tamanho mais comum, como em pdf_extract._body_size.
    body = Counter(line.size for line in lines).most_common(1)[0][0]
    for line in lines:
        if abs(line.size - body) <= body * BODY_SNAP:
            line.size = body
