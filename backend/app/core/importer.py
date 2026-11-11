"""Importação: do arquivo enviado ao Document, com progresso e resumo.

As rotas usam isto de dois jeitos: de uma vez (POST /api/projects, útil para
scripts) e como tarefa em segundo plano com barra de progresso (POST
/api/imports, o que a interface usa).
"""

from __future__ import annotations

import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any

from .blocks import Document
from .epub_read import read_epub
from .pdf_extract import ExtractionReport, OnProgress, extract_blocks
from .structure import split_into_chapters

SPLIT_LEVELS = ("heading1", "heading2", "none")
OCR_MODES = ("auto", "force", "off")

# Para que o livro foi importado: só converter o PDF em EPUB, ou traduzir.
# As duas etapas continuam abertas a qualquer livro; o objetivo decide por
# onde ele começa e como aparece na lista.
PURPOSES = ("convert", "translate")

# Idiomas oferecidos na importação para tradução: o código vai para o EPUB
# (lang, dc:language), o nome para o pedido ao modelo.
LANGUAGES: dict[str, str] = {
    "en": "inglês",
    "pt-BR": "português brasileiro",
    "pt-PT": "português europeu",
    "es": "espanhol",
    "fr": "francês",
    "de": "alemão",
    "it": "italiano",
    "nl": "holandês",
    "ru": "russo",
    "ja": "japonês",
    "zh": "chinês",
    "la": "latim",
}

# Acima disto, um livro inteiro num capítulo só é suspeito.
SINGLE_CHAPTER_WORDS = 5_000


@dataclass
class ImportOptions:
    title: str = ""
    # Que títulos começam um capítulo (ver structure.split_into_chapters).
    split_on: str = "heading1"
    # Ver pdf_extract.extract_blocks.
    ocr_mode: str = "auto"
    ocr_languages: str | None = None
    purpose: str = "convert"
    # Só na importação para tradução: códigos de LANGUAGES e o provedor.
    source_language: str = ""
    target_language: str = ""
    provider_id: str = ""

    def validate(self, suffix: str = ".pdf") -> None:
        if self.split_on not in SPLIT_LEVELS:
            raise ValueError(f"Nível de capítulo desconhecido: {self.split_on}")
        if self.ocr_mode not in OCR_MODES:
            raise ValueError(f"Modo de OCR desconhecido: {self.ocr_mode}")
        if self.purpose not in PURPOSES:
            raise ValueError(f"Objetivo desconhecido: {self.purpose}")
        if self.purpose == "convert" and suffix != ".pdf":
            raise ValueError(
                "A conversão recebe só PDF. Para traduzir um EPUB, use a importação para tradução."
            )
        for code in (self.source_language, self.target_language):
            if code and code not in LANGUAGES:
                raise ValueError(f"Idioma desconhecido: {code}")

    def translation(self) -> dict[str, str]:
        """As preferências de tradução do livro, como o projeto as guarda."""
        if self.purpose != "translate":
            return {}
        chosen = {
            "source_language": LANGUAGES.get(self.source_language, ""),
            "target_language": LANGUAGES.get(self.target_language, ""),
            "provider_id": self.provider_id,
        }
        return {key: value for key, value in chosen.items() if value}


def import_file(
    data: bytes,
    suffix: str,
    filename: str,
    options: ImportOptions,
    on_progress: OnProgress | None = None,
) -> tuple[Document, dict[str, Any]]:
    """Lê um PDF ou EPUB. Devolve o documento e o resumo da importação.

    Levanta ValueError quando o arquivo não tem como ser lido; o texto da
    exceção é para o usuário.
    """
    options.validate(suffix)
    started = time.monotonic()
    report: ExtractionReport | None = None

    # Os extratores trabalham com caminho em disco, não com bytes.
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as temporary:
        temporary.write(data)
        temporary.flush()

        if suffix == ".pdf":
            report = ExtractionReport()
            images: dict[str, bytes] = {}
            blocks, guessed_title = extract_blocks(
                temporary.name,
                source_name=filename,
                images=images,
                ocr_mode=options.ocr_mode,
                ocr_languages=options.ocr_languages,
                on_progress=on_progress,
                report=report,
            )
            document = Document(title=options.title or guessed_title, images=images)
            document.chapters = split_into_chapters(blocks, split_on=options.split_on)
        else:
            if on_progress:
                on_progress("read", 0, 1)
            document = read_epub(temporary.name)
            if options.title:
                document.title = options.title
            if on_progress:
                on_progress("read", 1, 1)

    if options.source_language:
        document.source_language = options.source_language
    if options.target_language:
        document.language = options.target_language

    summary = build_summary(document, report, options, time.monotonic() - started)
    return document, summary


def build_summary(
    document: Document,
    report: ExtractionReport | None,
    options: ImportOptions,
    seconds: float,
) -> dict[str, Any]:
    """O que a importação encontrou e o que merece uma conferida."""
    counts = Counter(block.kind for block in document.all_blocks())
    headings = sum(counts[kind] for kind in ("heading1", "heading2", "heading3"))
    words = document.word_count()

    warnings: list[str] = []
    if report is not None:
        if report.scanned_without_ocr:
            reason = (
                "o OCR estava desligado"
                if options.ocr_mode == "off"
                else "o Tesseract não está instalado"
            )
            warnings.append(
                f"{_pages_label(report.scanned_without_ocr).capitalize()} "
                f"{_plural(len(report.scanned_without_ocr), 'parece escaneada', 'parecem escaneadas')} "
                f"e {_plural(len(report.scanned_without_ocr), 'ficou', 'ficaram')} sem texto: {reason}."
            )
        if report.ocr_empty:
            warnings.append(f"O OCR não achou texto {_in_pages(report.ocr_empty)}.")
        if report.skipped_images:
            total = len(report.skipped_images)
            warnings.append(
                f"{_plural(total, 'Uma imagem ficou', f'{total} imagens ficaram')} de fora, "
                f"num formato que o conversor não leva para o EPUB "
                f"({_pages_label(sorted(set(report.skipped_images)))})."
            )
    if len(document.chapters) == 1 and words >= SINGLE_CHAPTER_WORDS and options.split_on != "none":
        warnings.append(
            "O livro inteiro virou um capítulo só. Divida-o na etapa de conversão "
            "com \"Começar capítulo aqui\", ou importe de novo separando os "
            "capítulos por outro nível de título."
        )

    return {
        "source_kind": "pdf" if report is not None else "epub",
        "pages": report.pages if report is not None else None,
        "ocr_pages": len(report.ocr_pages) if report is not None else 0,
        "ocr_languages": report.ocr_languages if report is not None else "",
        "chapters": len(document.chapters),
        "words": words,
        "headings": headings,
        "paragraphs": counts["paragraph"] + counts["list_item"] + counts["quote"],
        "footnotes": counts["footnote"],
        "tables": counts["table"],
        "images": counts["image"],
        "seconds": round(seconds, 1),
        "warnings": warnings,
    }


def _plural(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


def _pages_label(pages: list[int]) -> str:
    return f"{_plural(len(pages), 'página', 'páginas')} {page_ranges(pages)}"


def _in_pages(pages: list[int]) -> str:
    return f"{_plural(len(pages), 'na página', 'nas páginas')} {page_ranges(pages)}"


def page_ranges(pages: list[int], limit: int = 8) -> str:
    """[1, 2, 3, 7, 9, 10] -> "1–3, 7, 9–10". Corta em `limit` trechos."""
    spans: list[tuple[int, int]] = []
    for page in sorted(set(pages)):
        if spans and page == spans[-1][1] + 1:
            spans[-1] = (spans[-1][0], page)
        else:
            spans.append((page, page))
    parts = [str(a) if a == b else f"{a}–{b}" for a, b in spans[:limit]]
    if len(spans) > limit:
        parts.append("…")
    return ", ".join(parts)
