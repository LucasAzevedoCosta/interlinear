"""OCR das páginas escaneadas de um PDF.

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
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import Callable

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


def installed_languages() -> list[str]:
    """Os idiomas do Tesseract instalados, sem o "osd" (detecção de rotação)."""
    if tesseract_path() is None:
        return []
    return sorted(lang for lang in _installed_languages() if lang != "osd")


def languages(wanted: str | None = None) -> str:
    """Os idiomas pedidos (ou os do ambiente) que estão instalados, como "por+eng"."""
    requested = (wanted or os.environ.get("INTERLINEAR_OCR_LANGS", DEFAULT_LANGUAGES)).split("+")
    installed = _installed_languages()
    chosen = [lang for lang in requested if lang in installed]
    return "+".join(chosen) or "eng"


def read_lines(
    pdf_path: str,
    pages: list[int] | None = None,
    *,
    langs: str | None = None,
    body_size: float | None = None,
    on_page: Callable[[int, int], None] | None = None,
) -> list[Line]:
    """Faz OCR das páginas pedidas (todas, sem `pages`) e devolve as linhas.

    As páginas são lidas em paralelo, um processo do Tesseract por núcleo, cada
    um limitado a uma thread (OMP_THREAD_LIMIT): é mais rápido que um processo
    só usando todas. O pdfium não aceita duas renderizações ao mesmo tempo, então
    cada página é renderizada sob uma trava, já dentro do seu processo de OCR:
    a renderização de uma corre junto com o OCR das outras.

    on_page(feitas, total) é chamada a cada página lida. Uma exceção levantada
    nela cancela as páginas que ainda não começaram e sobe daqui.

    body_size, quando informado, vira o tamanho do corpo (ver _snap_body_size).
    """
    import pypdfium2 as pdfium

    lang = languages(langs)
    document = pdfium.PdfDocument(pdf_path)
    numbers = pages or list(range(1, len(document) + 1))
    render_lock = threading.Lock()
    log.info("OCR de %d páginas (%s)", len(numbers), lang)

    try:
        with tempfile.TemporaryDirectory(prefix="interlinear-ocr-") as tmp:

            def work(number: int) -> list[Line]:
                image = Path(tmp) / f"{number:05d}.pgm"
                with render_lock:
                    page = document[number - 1]
                    width, height = page.get_size()
                    _render(page, image)
                    page.close()
                try:
                    return _ocr_page(number, image, width, height, lang=lang)
                finally:
                    image.unlink(missing_ok=True)

            workers = max(1, min(len(numbers), os.cpu_count() or 1))
            pool = ThreadPoolExecutor(max_workers=workers)
            try:
                futures = {pool.submit(work, number): number for number in numbers}
                by_page: dict[int, list[Line]] = {}
                for future in as_completed(futures):
                    by_page[futures[future]] = future.result()
                    if on_page:
                        on_page(len(by_page), len(numbers))
            finally:
                # Num cancelamento, as páginas na fila nem começam; as que estão
                # no Tesseract terminam antes de a pasta temporária sumir.
                pool.shutdown(wait=True, cancel_futures=True)
    finally:
        document.close()

    lines = [line for number in sorted(by_page) for line in by_page[number]]
    _snap_body_size(lines, body_size)
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


def _snap_body_size(lines: list[Line], target: float | None = None) -> None:
    """Iguala ao corpo os tamanhos que só diferem dele pelo ruído do OCR.

    Com `target`, o corpo passa a medir `target` e os outros tamanhos mudam na
    mesma proporção: um título continua tão maior que o corpo quanto era.
    """
    if not lines:
        return
    # O tamanho mais comum, como em pdf_extract._body_size.
    body = Counter(line.size for line in lines).most_common(1)[0][0]
    scale = target / body if target and body else 1.0
    for line in lines:
        if abs(line.size - body) <= body * BODY_SNAP:
            line.size = target or body
        elif scale != 1.0:
            line.size = round(line.size * scale * 2) / 2
