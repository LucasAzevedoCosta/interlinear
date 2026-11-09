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
    # Nota de rodapé. O marcador ("1", "*") fica em meta["marker"] e a chamada
    # no texto é um <sup> com o mesmo marcador. No EPUB vai para o fim do
    # capítulo; no editor, fica logo depois do parágrafo da página dela.
    "footnote",
    # Tabela em texto puro: uma linha da tabela por linha do texto, células
    # separadas por " | ". meta["header"] diz se a primeira linha é cabeçalho.
    "table",
    "image",
    "page_break",
]

# Tipos cujo texto faz sentido traduzir.
TRANSLATABLE: frozenset[str] = frozenset(
    {
        "heading1", "heading2", "heading3", "paragraph", "quote", "list_item",
        "footnote", "table",
    }
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


_WORD = re.compile(r"\w+(?:-\w+)*")
_TAGS = re.compile(r"<[^>]+>")
_LAST_FRAGMENT = re.compile(r"(\w+)-$")
_FIRST_FRAGMENT = re.compile(r"^(\w+)")


@dataclass
class Vocabulary:
    """As palavras do próprio livro, para decidir o hífen de fim de linha.

    Um livro de verdade costuma escrever "guarda-chuva" inteiro no meio de
    alguma linha. Essa é a evidência mais confiável para saber se o hífen da
    quebra de linha faz parte da palavra, e vale para qualquer idioma.
    """

    hyphenated: set[str] = field(default_factory=set)
    plain: set[str] = field(default_factory=set)

    @classmethod
    def from_texts(cls, texts) -> "Vocabulary":
        vocabulary = cls()
        for text in texts:
            for word in _WORD.findall(_TAGS.sub("", text).lower()):
                (vocabulary.hyphenated if "-" in word else vocabulary.plain).add(word)
        return vocabulary


# Sem evidência no livro, só regras que não se confundem com divisão silábica.
# Pronomes curtos soltos ("-na", "-se", "-me") ficam de fora de propósito:
# "pe-na" e "pas-se" são quebras de sílaba comuns.
_ACCENTED_END = tuple("áéêíóôú")
_NASAL_END = ("m", "ão", "õe", "ãe")
_ENCLITIC_AFTER_ACCENT = frozenset({"lo", "la", "los", "las"})
_ENCLITIC_AFTER_NASAL = frozenset({"no", "na", "nos", "nas"})
_COMPOUND_SUFFIXES = frozenset({"feira", "feiras"})
# "ex", "sub", "mal" e "sem" ficam de fora: "ex-cesso", "sub-ir", "mal-dade" e
# "sem-pre" são quebras de sílaba, não prefixos.
_COMPOUND_PREFIXES = frozenset({"pré", "pós", "pró", "recém", "além", "aquém", "vice"})


def keeps_hyphen(before: str, after: str, vocabulary: Vocabulary | None = None) -> bool:
    """Diz se o hífen entre `before` e `after` (fim e começo de linha) é da palavra.

    "guarda" + "chuva" → True se o livro escreve "guarda-chuva" em outro lugar;
    "exem" + "plo" → False. Na dúvida, False: o hífen de quebra é muito mais
    comum que o de palavra composta.
    """
    before, after = before.lower(), after.lower()
    if vocabulary is not None:
        if f"{before}-{after}" in vocabulary.hyphenated:
            return True
        if f"{before}{after}" in vocabulary.plain:
            return False

    if after in _ENCLITIC_AFTER_ACCENT and before.endswith(_ACCENTED_END):
        return True  # fazê-lo, pô-la, vendê-los
    if after in _ENCLITIC_AFTER_NASAL and before.endswith(_NASAL_END):
        return True  # deram-na, põe-no, dão-nos
    if after in _COMPOUND_SUFFIXES:
        return True  # segunda-feira
    return before in _COMPOUND_PREFIXES  # pré-história, recém-chegado


def join_hyphenated(previous: str, nxt: str, vocabulary: Vocabulary | None = None) -> str:
    """Junta duas linhas, desfazendo a hifenização de quebra de linha.

    "exem-" + "plo" vira "exemplo". O hífen fica quando a palavra seguinte
    começa com maiúscula (provável nome composto) ou quando keeps_hyphen diz
    que ele faz parte da palavra ("guarda-" + "chuva").
    """
    previous = previous.rstrip()
    nxt = nxt.lstrip()
    before = _LAST_FRAGMENT.search(previous)
    if before and nxt[:1].isupper():
        return previous + nxt  # Jean-Paul
    if before and nxt[:1].islower():
        after = _FIRST_FRAGMENT.search(nxt)
        if after and keeps_hyphen(before.group(1), after.group(1), vocabulary):
            return previous + nxt
        return previous[:-1] + nxt
    if not previous:
        return nxt
    return previous + " " + nxt
