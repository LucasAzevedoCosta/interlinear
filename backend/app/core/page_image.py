"""Imagem de uma página do PDF, para a revisão visual da conversão.

A revisão visual manda ao modelo a página como ela é e o texto que a
extração tirou dela, para ele apontar o que sumiu ou saiu errado.

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


def render_page(pdf_path: str | Path, page: int, *, dpi: int = DPI) -> bytes:
    """A página `page` (contando de 1, como Block.page) em PNG.

    Levanta ValueError se a página não existe no PDF.
    """
    import pypdfium2 as pdfium

    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf_path))
        try:
            if not 1 <= page <= len(document):
                raise ValueError(f"O PDF não tem a página {page}")
            bitmap = document[page - 1].render(scale=dpi / 72, grayscale=True)
            width, height, stride = bitmap.width, bitmap.height, bitmap.stride
            buffer = bytes(bitmap.buffer)
            bitmap.close()
        finally:
            document.close()

    if stride != width:
        buffer = b"".join(buffer[row * stride : row * stride + width] for row in range(height))
    return _encode_png(width, height, 1, buffer)
