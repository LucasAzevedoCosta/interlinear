"""Divisão de uma lista plana de blocos em capítulos.

Cada capítulo vira um arquivo XHTML dentro do EPUB. Leitores de e-book carregam
um arquivo por vez, então capítulos muito grandes deixam a navegação lenta e
capítulos minúsculos poluem o sumário. Buscamos um meio-termo.
"""

from __future__ import annotations

import re

from .blocks import Block, Chapter

# Acima disso, o capítulo é quebrado mesmo sem título, para não gerar um
# XHTML gigante que trava o leitor.
MAX_WORDS_PER_CHAPTER = 12_000

# Abaixo disso, um "capítulo" é quase certamente uma folha de rosto ou um
# título órfão, e é fundido com o seguinte.
MIN_WORDS_PER_CHAPTER = 40

# Palavras que costumam abrir um capítulo de verdade, em vários idiomas.
_CHAPTER_WORDS = (
    "capítulo", "capitulo", "chapter", "capítulo", "capitolo", "kapitel",
    "chapitre", "parte", "part", "livro", "book", "seção", "secao",
    "prólogo", "prologo", "prologue", "epílogo", "epilogo", "epilogue",
    "introdução", "introducao", "introduction", "prefácio", "prefacio",
    "preface", "apêndice", "apendice", "appendix", "conclusão", "conclusao",
    "conclusion", "posfácio", "posfacio", "afterword",
)

_NUMERIC_HEAD = re.compile(r"^\s*(\d{1,3}|[ivxlcdm]{1,7})\s*[.\-—–:]?\s*$", re.I)


def _is_chapter_opener(block: Block) -> bool:
    """Um título que provavelmente abre um capítulo, não uma subseção."""
    if block.kind != "heading1":
        return True

    text = block.text.strip().lower()
    if not text:
        return False

    first_word = text.split()[0].strip(".:—–-")
    if first_word in _CHAPTER_WORDS:
        return True
    if _NUMERIC_HEAD.match(text):
        return True
    # Um heading1 isolado quase sempre abre capítulo.
    return True


def _word_count(blocks: list[Block]) -> int:
    return sum(len(b.text.split()) for b in blocks)


def split_into_chapters(
    blocks: list[Block],
    *,
    split_on: str = "heading1",
) -> list[Chapter]:
    """Agrupa blocos em capítulos, quebrando nos títulos do nível escolhido.

    split_on aceita "heading1", "heading2" ou "none" (um capítulo só, quebrado
    apenas por tamanho).
    """
    if not blocks:
        return [Chapter(title="Capítulo 1")]

    levels = {
        "heading1": {"heading1"},
        "heading2": {"heading1", "heading2"},
        "none": set(),
    }.get(split_on, {"heading1"})

    # Nem todo livro usa o nível pedido. Se o documento não tem nenhum título
    # desse nível, descemos um degrau em vez de devolver o livro inteiro como
    # um capítulo só — que é o pior resultado possível para o leitor.
    if levels and not any(b.kind in levels for b in blocks):
        for fallback in ({"heading1", "heading2"}, {"heading1", "heading2", "heading3"}):
            if any(b.kind in fallback for b in blocks):
                levels = fallback
                break

    chapters: list[Chapter] = []
    current: list[Block] = []
    current_title = ""

    def flush() -> None:
        nonlocal current, current_title
        if current:
            title = current_title or _derive_title(current, len(chapters) + 1)
            chapters.append(Chapter(title=title, blocks=current))
        current = []
        current_title = ""

    for block in blocks:
        starts_chapter = (
            block.kind in levels
            and _is_chapter_opener(block)
            and _word_count(current) >= MIN_WORDS_PER_CHAPTER
        )

        if starts_chapter:
            flush()
            current_title = block.text.strip()
        elif block.kind in levels and not current_title and not current:
            current_title = block.text.strip()

        current.append(block)

        if _word_count(current) > MAX_WORDS_PER_CHAPTER:
            # Quebra forçada: procura o último fim de parágrafo para não
            # cortar no meio de uma ideia.
            flush()

    flush()

    if not chapters:
        chapters = [Chapter(title="Capítulo 1", blocks=blocks)]

    return _merge_tiny(chapters)


def _merge_tiny(chapters: list[Chapter]) -> list[Chapter]:
    """Funde capítulos curtos demais no seguinte."""
    if len(chapters) <= 1:
        return chapters

    merged: list[Chapter] = []
    pending: list[Block] = []
    pending_title = ""

    for chapter in chapters:
        blocks = pending + chapter.blocks
        title = pending_title or chapter.title

        if _word_count(blocks) < MIN_WORDS_PER_CHAPTER:
            pending = blocks
            pending_title = title
            continue

        merged.append(Chapter(title=title, blocks=blocks))
        pending = []
        pending_title = ""

    if pending:
        if merged:
            merged[-1].blocks.extend(pending)
        else:
            merged.append(Chapter(title=pending_title or "Capítulo 1", blocks=pending))

    return merged


def _derive_title(blocks: list[Block], index: int) -> str:
    """Quando não há título, usa o começo do primeiro parágrafo."""
    for block in blocks:
        if block.kind.startswith("heading") and block.text.strip():
            return block.text.strip()
    for block in blocks:
        # Sem <em>/<strong>: o título vai para o sumário e a lista de capítulos.
        plain = re.sub(r"<[^>]+>", "", block.text)
        if plain.strip():
            snippet = " ".join(plain.split()[:7])
            return f"{snippet}…"
    return f"Capítulo {index}"
