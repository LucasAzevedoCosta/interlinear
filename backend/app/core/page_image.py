"""Imagem de uma página do PDF: para a revisão visual e para o painel Original.

A revisão visual manda ao modelo a página como ela é e o texto que a
extração tirou dela, para ele apontar o que sumiu ou saiu errado. O painel
Original da bancada mostra a mesma página ao usuário, em cores.

Tons de cinza a 100 dpi: o modelo lê bem, e uma página de livro fica com
dezenas de KB. O OCR usa 300 dpi porque o Tesseract é calibrado para isso; um
modelo de visão reduz a imagem de qualquer jeito.
"""

from __future__ import annotations

import threading
from pathlib import Path

from .pdf_extract import _encode_png

DPI = 100

# O pdfium não aceita duas renderizações ao mesmo tempo no mesmo processo. Uma
# trava só para todos: o OCR de uma importação (core/ocr.py) e a revisão
# visual podem rodar juntos.
PDFIUM_LOCK = threading.Lock()


def render_page(
    pdf_path: str | Path, page: int, *, dpi: int = DPI, grayscale: bool = True
) -> bytes:
    """A página `page` (contando de 1, como Block.page) em PNG.

    Em tons de cinza por padrão (revisão visual); `grayscale=False` dá RGB,
    para o usuário ver a página como ela é. Levanta ValueError se a página não
    existe no PDF.
    """
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            if not 1 <= page <= len(document):
                raise ValueError(f"O PDF não tem a página {page}")
            # rev_byteorder: RGB em vez do BGR nativo do pdfium.
            bitmap = document[page - 1].render(
                scale=dpi / 72, grayscale=grayscale, rev_byteorder=not grayscale
            )
            width, height, stride = bitmap.width, bitmap.height, bitmap.stride
            channels = bitmap.n_channels
            buffer = bytes(bitmap.buffer)
            bitmap.close()
        finally:
            document.close()

    row_bytes = width * channels
    if stride != row_bytes:
        buffer = b"".join(buffer[row * stride : row * stride + row_bytes] for row in range(height))
    return _encode_png(width, height, channels, buffer)


def page_sizes(pdf_path: str | Path) -> list[tuple[float, float]]:
    """Largura e altura de cada página, em pontos (1/72 de polegada).

    São as mesmas unidades de meta["boxes"] dos blocos: o frontend divide uma
    pela outra para desenhar o destaque por cima da imagem em qualquer zoom.
    """
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            return [
                tuple(round(v, 2) for v in document[i].get_size()) for i in range(len(document))
            ]
        finally:
            document.close()
