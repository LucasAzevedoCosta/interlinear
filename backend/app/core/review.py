"""Marcação de suspeitos: os blocos que a conversão provavelmente errou.

A extração acerta quase tudo, mas um livro de 3.000 blocos não se confere
lendo. Estas regras baratas apontam os poucos lugares duvidosos, para o olho
(e, depois, um modelo de IA) ir direto a eles. Ver docs/conversao.md, seção 4.

Nenhuma regra altera o documento: elas só apontam. Um suspeito pode estar
certo; corrigir o bloco o tira da lista na próxima conferência.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import asdict, dataclass

from .blocks import Block, Document

# O mesmo limite do extrator (pdf_extract.MAX_HEADING_WORDS). Título mais
# longo que isto veio de um EPUB, de uma edição ou é parágrafo mal classificado.
MAX_HEADING_WORDS = 18

# Abaixo disto, um capítulo é provavelmente uma divisão indevida: folha de
# rosto, título órfão, "Começar capítulo aqui" no lugar errado. Maior que o
# structure.MIN_WORDS_PER_CHAPTER, que já funde os menores na importação.
SHORT_CHAPTER_WORDS = 150

# Cabeçalho corrente que escapou do filtro: texto curto repetido em páginas
# diferentes.
REPEATED_MAX_WORDS = 12
REPEATED_MIN_PAGES = 3

# Fim de frase, incluindo o que fecha uma citação ou anuncia a seguinte.
_CLOSING = (".", "!", "?", "…", '"', "”", "»", "'", "’", ")", "]", ":")
# O que pode vir antes da primeira letra sem tirar dela o papel de começo.
_LEADING = "\"'“”‘’«»—–-([… \t"

_TAGS = re.compile(r"<[^>]+>")
_DIGITS = re.compile(r"\d+")
_NON_WORD = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")

# Tipos que continuam um parágrafo: uma imagem ou nota no meio não o encerra.
_INTERRUPTIONS = frozenset({"image", "page_break", "footnote"})
_PROSE = frozenset({"paragraph", "quote", "list_item"})
_HEADINGS = frozenset({"heading1", "heading2", "heading3"})

MESSAGES = {
    "lowercase_start": "Começa com minúscula: pode ser o resto do parágrafo anterior.",
    "open_ending": "Termina sem pontuação antes de outro parágrafo: pode ter sido partido.",
    "long_heading": "Título longo demais: pode ser um parágrafo.",
    "tiny_block": "Bloco de um ou dois caracteres: capitular ou número de página?",
    "repeated_text": "Texto repetido em várias páginas: cabeçalho ou rodapé que escapou?",
    "short_chapter": "Capítulo muito curto: a divisão pode estar no lugar errado.",
}


@dataclass
class Suspect:
    chapter_id: str
    # None só para um capítulo vazio marcado como curto.
    block_id: str | None
    reason: str
    message: str

    def to_dict(self) -> dict[str, str | None]:
        return asdict(self)


def find_suspects(document: Document) -> list[Suspect]:
    """Os suspeitos do livro, na ordem de leitura.

    Um bloco pode aparecer mais de uma vez, com motivos diferentes.
    """
    repeated = _repeated_blocks(document)
    suspects: list[Suspect] = []

    def mark(chapter_id: str, block_id: str | None, reason: str) -> None:
        suspects.append(Suspect(chapter_id, block_id, reason, MESSAGES[reason]))

    for chapter in document.chapters:
        if len(document.chapters) > 1:
            words = sum(len(_plain(b.text).split()) for b in chapter.blocks)
            if words < SHORT_CHAPTER_WORDS:
                first = chapter.blocks[0].id if chapter.blocks else None
                mark(chapter.id, first, "short_chapter")

        for index, block in enumerate(chapter.blocks):
            text = _plain(block.text)
            if not text:
                continue
            if block.kind == "paragraph" and _starts_lowercase(text):
                mark(chapter.id, block.id, "lowercase_start")
            # Número de página e cabeçalho nunca terminam em ponto; o motivo
            # deles já é outro, e "parágrafo partido" só confundiria.
            stray = len(text) <= 2 or block.id in repeated
            if block.kind == "paragraph" and not stray and not text.endswith(_CLOSING):
                following = _next_text_block(chapter.blocks, index)
                if following is not None and following.kind == "paragraph":
                    mark(chapter.id, block.id, "open_ending")
            if block.kind in _HEADINGS and len(text.split()) > MAX_HEADING_WORDS:
                mark(chapter.id, block.id, "long_heading")
            if block.kind in _PROSE and len(text) <= 2:
                mark(chapter.id, block.id, "tiny_block")
            if block.id in repeated:
                mark(chapter.id, block.id, "repeated_text")

    return suspects


def _plain(text: str) -> str:
    return _TAGS.sub("", text).strip()


def _starts_lowercase(text: str) -> bool:
    first = text.lstrip(_LEADING)[:1]
    return first.isalpha() and first.islower()


def _next_text_block(blocks: list[Block], index: int) -> Block | None:
    """O próximo bloco do capítulo, pulando imagem, quebra e nota de rodapé."""
    for block in blocks[index + 1 :]:
        if block.kind not in _INTERRUPTIONS:
            return block
    return None


def _repeat_key(text: str) -> str:
    """O texto sem números nem pontuação: "12 · O NOME" e "O nome 13" empatam."""
    text = _NON_WORD.sub(" ", _DIGITS.sub(" ", text.lower()))
    return _SPACES.sub(" ", text).strip()


def _repeated_blocks(document: Document) -> set[str]:
    """Ids dos blocos curtos cujo texto se repete em páginas diferentes.

    O título que abre um capítulo fica de fora: "Capítulo 1", "Capítulo 2"…
    empatam sem os números, e estão certos.
    """
    pages: dict[str, set[int | str]] = defaultdict(set)
    ids: dict[str, list[str]] = defaultdict(list)

    for chapter in document.chapters:
        for index, block in enumerate(chapter.blocks):
            if block.kind in _INTERRUPTIONS or block.kind == "table":
                continue
            if index == 0 and block.kind in _HEADINGS:
                continue
            text = _plain(block.text)
            if not text or len(text.split()) > REPEATED_MAX_WORDS:
                continue
            key = _repeat_key(text)
            if len(key.replace(" ", "")) < 3:
                continue
            # Sem página (livro vindo de EPUB), cada bloco conta como uma.
            pages[key].add(block.page if block.page is not None else block.id)
            ids[key].append(block.id)

    return {
        block_id
        for key, found in pages.items()
        if len(found) >= REPEATED_MIN_PAGES
        for block_id in ids[key]
    }
