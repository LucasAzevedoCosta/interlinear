"""Estruturas de dados compartilhadas entre extração, estruturação e escrita de EPUB.

Um documento em processamento é sempre uma lista plana de Block. A conversão
PDF -> EPUB e a leitura de um EPUB existente produzem a mesma estrutura, de modo
que o editor e o tradutor não precisam saber qual foi a origem do arquivo.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

BlockKind = Literal[
    "heading1",
    "heading2",
    "heading3",
    "paragraph",
    "quote",
    "list_item",
    "image",
    "page_break",
]

# Tipos cujo texto faz sentido traduzir.
TRANSLATABLE: frozenset[str] = frozenset(
    {"heading1", "heading2", "heading3", "paragraph", "quote", "list_item"}
)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass
class Block:
    """Um bloco de conteúdo: um parágrafo, um título, uma citação, uma imagem."""

    kind: BlockKind
    text: str = ""
    id: str = field(default_factory=new_id)
    # Texto traduzido, quando houver. O original nunca é sobrescrito, o que
    # permite revisar lado a lado e retraduzir sem perder a fonte.
    translated: str | None = None
    # Página do PDF de origem, usada para ancorar o preview na página certa.
    page: int | None = None
    # Para kind == "image": nome do arquivo dentro do EPUB.
    src: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def display_text(self) -> str:
        """O texto que deve aparecer no EPUB exportado."""
        return self.translated if self.translated is not None else self.text

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Block":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Chapter:
    """Um capítulo vira um arquivo XHTML dentro do EPUB."""

    title: str
    blocks: list[Block] = field(default_factory=list)
    id: str = field(default_factory=new_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "blocks": [b.to_dict() for b in self.blocks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Chapter":
        return cls(
            id=data.get("id", new_id()),
            title=data.get("title", ""),
            blocks=[Block.from_dict(b) for b in data.get("blocks", [])],
        )


@dataclass
class Document:
    """O livro inteiro, com metadados e capítulos."""

    title: str = "Sem título"
    author: str = ""
    language: str = "pt-BR"
    source_language: str = "en"
    identifier: str = field(default_factory=lambda: f"urn:uuid:{uuid.uuid4()}")
    chapters: list[Chapter] = field(default_factory=list)
    # Imagens extraídas: nome do arquivo -> bytes. Não entra no JSON do projeto,
    # é gravado em disco separadamente.
    images: dict[str, bytes] = field(default_factory=dict, repr=False)

    def all_blocks(self) -> list[Block]:
        return [b for ch in self.chapters for b in ch.blocks]

    def translatable_blocks(self) -> list[Block]:
        return [
            b
            for b in self.all_blocks()
            if b.kind in TRANSLATABLE and b.text.strip()
        ]

    def block_index(self) -> dict[str, Block]:
        return {b.id: b for b in self.all_blocks()}

    def word_count(self) -> int:
        return sum(len(b.text.split()) for b in self.translatable_blocks())

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "author": self.author,
            "language": self.language,
            "source_language": self.source_language,
            "identifier": self.identifier,
            "chapters": [c.to_dict() for c in self.chapters],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Document":
        doc = cls(
            title=data.get("title", "Sem título"),
            author=data.get("author", ""),
            language=data.get("language", "pt-BR"),
            source_language=data.get("source_language", "en"),
            identifier=data.get("identifier", f"urn:uuid:{uuid.uuid4()}"),
            chapters=[Chapter.from_dict(c) for c in data.get("chapters", [])],
        )
        return doc


# --------------------------------------------------------------------------
# Utilidades de texto usadas por mais de um módulo
# --------------------------------------------------------------------------

_WS = re.compile(r"[ \t ]+")
_SOFT_HYPHEN = "­"


def normalize_spaces(text: str) -> str:
    """Colapsa espaços e remove hífens de quebra invisíveis."""
    return _WS.sub(" ", text.replace(_SOFT_HYPHEN, "")).strip()


def join_hyphenated(previous: str, nxt: str) -> str:
    """Junta duas linhas, desfazendo a hifenização de quebra de linha.

    "exem-" + "plo" vira "exemplo", mas "bem-" + "vindo" preserva o hífen
    quando a palavra seguinte começa com maiúscula (provável nome composto).
    """
    previous = previous.rstrip()
    if previous.endswith("-") and nxt[:1].islower():
        return previous[:-1] + nxt.lstrip()
    if not previous:
        return nxt.lstrip()
    return previous + " " + nxt.lstrip()
