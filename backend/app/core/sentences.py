"""Divide o texto de um bloco em frases, para a tradução frase a frase.

No formato frase a frase, cada frase do original aparece seguida da tradução
dela. Quem divide é este módulo; o modelo só traduz cada pedaço marcado
(ai/translator.py, Fase 3 do plano em docs-local/traducao-bilingue-plano.md).
Por isso um erro de divisão nunca desalinha nada: no pior caso, um par fica
com duas frases ou com meia frase. Regras simples, bem testadas, bastam; não
vale uma dependência (a pysbd, a mais usada, está parada desde 2021 e não tem
regras para português).

O texto do bloco tem tags inline (<em>, <strong>, <sup>, <sub>, <small>,
<br>, <a href>). Uma tag que atravessa o corte é fechada no fim de uma frase
e reaberta na seguinte, para cada frase ser XHTML válido sozinha.
join_sentences desfaz isso.

Regras do corte, sobre o texto sem tags:
  - depois de . ! ? … (e combinações: ?!, ..., ."), incluindo aspas e
    parênteses que fecham logo depois;
  - seguido de espaço e de algo que abre frase: maiúscula, número, aspas,
    travessão, parêntese, ¿ ¡;
  - não corta em abreviação conhecida do idioma (Sr., Dr., Mr., etc.), em
    inicial (J. R. R. Tolkien), em número no começo (1. Item) nem quando o
    que vem depois começa com minúscula (reticências no meio da frase);
  - diálogo com travessão: "— Vamos? — perguntou ela." é uma frase só, porque
    depois do travessão vem minúscula;
  - japonês e chinês cortam em 。！？ sem precisar de espaço;
  - uma "frase" de uma palavra só se junta à seguinte (ou à anterior, no fim).
"""

from __future__ import annotations

import re

# Só as tags que o livro guarda (as do inline_html, em epub_write.py). Um "<"
# que não abre uma delas é texto: "StackNode<T>", "i < n" num livro técnico.
_TAG = re.compile(r"</?(?:em|strong|i|b|sup|sub|small|br|a)(?:\s[^<>]*)?\s*/?>", re.IGNORECASE)
_TAG_NAME = re.compile(r"^<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9]*)")
_VOID = frozenset({"br", "img", "hr", "wbr"})

_TERMINALS = ".!?…"
_CJK_TERMINALS = "。！？"
# Fecham depois do ponto e ficam com a frase: aspas, parênteses, colchetes.
_CLOSERS = "\"'”’»)]」』）"
# Abrem a frase seguinte.
_OPENERS = "\"'“‘«([¿¡—–-「『（"
_DASHES = "—–"

_CJK_LANGUAGES = frozenset({"ja", "zh"})

# Abreviações por idioma, sem o ponto final e em minúsculas. Quem tem ponto
# no meio ("e.g", "z.b") é comparado inteiro.
_COMMON = {"etc", "cf", "vs", "ca", "ibid", "op", "cit", "approx", "aprox", "fig", "vol", "nº"}
_ABBREVIATIONS: dict[str, set[str]] = {
    "pt": {
        "sr",
        "sra",
        "srs",
        "sras",
        "srta",
        "dr",
        "dra",
        "drs",
        "prof",
        "profa",
        "sto",
        "sta",
        "exmo",
        "exma",
        "v",
        "vv",
        "cap",
        "caps",
        "ed",
        "pág",
        "págs",
        "p",
        "pp",
        "séc",
        "av",
        "gen",
        "cel",
        "maj",
        "ten",
        "cia",
        "ltda",
        "obs",
        "tel",
        "núm",
        "num",
        "ex",
        "dª",
        "d",
        "s",
        "pe",
        "fr",
        "ir",
        "obr",
    },
    "en": {
        "mr",
        "mrs",
        "ms",
        "dr",
        "prof",
        "st",
        "jr",
        "sr",
        "e.g",
        "i.e",
        "no",
        "nos",
        "pp",
        "p",
        "ch",
        "dept",
        "gen",
        "col",
        "lt",
        "sgt",
        "capt",
        "rev",
        "inc",
        "ltd",
        "co",
        "mt",
        "ft",
        "a.m",
        "p.m",
        "u.s",
        "u.k",
        "jan",
        "feb",
        "mar",
        "apr",
        "jun",
        "jul",
        "aug",
        "sep",
        "sept",
        "oct",
        "nov",
        "dec",
    },
    "es": {"sr", "sra", "srta", "dr", "dra", "ud", "uds", "pág", "prof", "dña", "d", "p", "núm"},
    "fr": {"m", "mm", "mme", "mlle", "dr", "st", "ste", "p", "cf", "vol", "mgr", "me"},
    "de": {
        "hr",
        "fr",
        "dr",
        "prof",
        "nr",
        "bzw",
        "usw",
        "z.b",
        "d.h",
        "u.a",
        "s.o",
        "ca",
        "bd",
        "st",
    },
    "it": {"sig", "sigg", "dott", "prof", "ecc", "pag", "sig.ra", "dr", "avv", "ing"},
}


def _language(code: str) -> str:
    return (code or "").split("-")[0].split("_")[0].lower()


def _abbreviations(language: str) -> set[str]:
    return _COMMON | _ABBREVIATIONS.get(language, set())


# --------------------------------------------------------------------------
# Onde cortar, no texto sem tags
# --------------------------------------------------------------------------


def _word_before(plain: str, end: int) -> str:
    """A palavra que termina em `end` (o índice do ponto), sem aspas na frente."""
    start = end
    while start > 0 and not plain[start - 1].isspace():
        start -= 1
    return plain[start:end].lstrip(_OPENERS)


def _is_abbreviation(word: str, abbreviations: set[str], at_start: bool) -> bool:
    lowered = word.lower()
    if not lowered:
        return False
    if lowered in abbreviations:
        return True
    # Inicial: J. R. R. Tolkien.
    if len(word) == 1 and word.isalpha() and word.isupper():
        return True
    # Número no começo do bloco: "1. Primeiro item".
    if at_start and word.isdigit():
        return True
    # Sigla com pontos no meio, partes curtas: "U.S", "a.C", "e.V".
    parts = lowered.split(".")
    return len(parts) > 1 and all(0 < len(part) <= 2 for part in parts)


def _opens_sentence(plain: str, index: int) -> bool:
    """O que começa em `index` (já depois do espaço) pode abrir uma frase?"""
    if index >= len(plain):
        return False
    char = plain[index]
    if char in _DASHES:
        # Travessão de diálogo: com minúscula depois, é a fala continuando
        # ("— Vamos? — perguntou ela."); com maiúscula, é fala nova.
        rest = plain[index + 1 :].lstrip()
        return bool(rest) and not rest[0].islower()
    if char in _OPENERS:
        return True
    return char.isupper() or char.isdigit()


def _cut_points(plain: str, language: str) -> list[int]:
    """Índices (no texto sem tags) onde termina cada frase, menos a última."""
    cuts: list[int] = []
    abbreviations = _abbreviations(language)
    cjk = language in _CJK_LANGUAGES
    index = 0
    length = len(plain)
    while index < length:
        char = plain[index]
        if cjk and char in _CJK_TERMINALS:
            end = index + 1
            while end < length and plain[end] in _CLOSERS + _CJK_TERMINALS:
                end += 1
            if end < length:
                cuts.append(end)
            index = end
            continue
        if char not in _TERMINALS:
            index += 1
            continue
        terminal = index
        end = index + 1
        while end < length and plain[end] in _TERMINALS + _CLOSERS:
            end += 1
        index = end
        if end >= length or not plain[end].isspace():
            continue  # 3.5, "a.b", fim do texto
        following = end
        while following < length and plain[following].isspace():
            following += 1
        if not _opens_sentence(plain, following):
            continue
        if plain[terminal] == "." and terminal > 0 and plain[terminal - 1] != ".":
            word = _word_before(plain, terminal)
            at_start = not plain[: terminal - len(word)].strip()
            if _is_abbreviation(word, abbreviations, at_start):
                continue
        cuts.append(end)
    return cuts


# --------------------------------------------------------------------------
# Tags: o texto sem tags e o caminho de volta
# --------------------------------------------------------------------------


def _strip_tags(text: str) -> tuple[str, list[int]]:
    """O texto sem tags e, para cada caractere dele, a posição no original."""
    plain: list[str] = []
    positions: list[int] = []
    index = 0
    for match in _TAG.finditer(text):
        for offset, char in enumerate(text[index : match.start()]):
            plain.append(char)
            positions.append(index + offset)
        index = match.end()
    for offset, char in enumerate(text[index:]):
        plain.append(char)
        positions.append(index + offset)
    return "".join(plain), positions


def _balance(pieces: list[str]) -> list[str]:
    """Fecha no fim de cada pedaço as tags abertas, e reabre no seguinte."""
    balanced: list[str] = []
    open_tags: list[tuple[str, str]] = []  # (nome, tag de abertura inteira)
    for piece in pieces:
        # O espaço do corte fica fora das tags reabertas: "<em>Depois", não
        # "<em> Depois".
        piece = piece.strip()
        prefix = "".join(tag for _, tag in open_tags)
        for match in _TAG.finditer(piece):
            parsed = _TAG_NAME.match(match.group(0))
            if not parsed:
                continue
            closing, name = parsed.group(1) == "/", parsed.group(2).lower()
            if name in _VOID or match.group(0).rstrip(">").endswith("/"):
                continue
            if closing:
                for position in range(len(open_tags) - 1, -1, -1):
                    if open_tags[position][0] == name:
                        del open_tags[position]
                        break
            else:
                open_tags.append((name, match.group(0)))
        suffix = "".join(f"</{name}>" for name, _ in reversed(open_tags))
        balanced.append(_drop_empty(prefix + piece + suffix))
    return balanced


_EMPTY_PAIR = re.compile(r"<([a-zA-Z][a-zA-Z0-9]*)(?:\s[^>]*)?>(\s*)</\1>")


def _drop_empty(text: str) -> str:
    previous = None
    while previous != text:
        previous = text
        text = _EMPTY_PAIR.sub(r"\2", text)
    return text.strip()


# --------------------------------------------------------------------------
# Interface
# --------------------------------------------------------------------------


def split_sentences(text: str, language: str = "") -> list[str]:
    """As frases do texto, cada uma com as tags fechadas.

    `language` é o código do idioma do livro ("pt-BR", "en"…); só muda a
    lista de abreviações e o corte sem espaço do japonês e do chinês.
    Texto vazio dá lista vazia.
    """
    if not text or not text.strip():
        return []
    language = _language(language)
    plain, positions = _strip_tags(text)
    cuts = _cut_points(plain, language)

    pieces: list[str] = []
    start = 0
    for cut in cuts:
        # O corte é no espaço depois do ponto: tags que fecham logo depois do
        # ponto ficam com esta frase, as que abrem a próxima ficam com ela.
        raw = positions[cut] if cut < len(positions) else len(text)
        pieces.append(text[start:raw])
        start = raw
    pieces.append(text[start:])

    sentences = [piece for piece in _balance(pieces) if _TAG.sub("", piece).strip()]
    if not sentences:
        # Só tags, sem texto ("<strong>•</strong>" vira "•"... ou nada): o
        # bloco inteiro é uma frase, para ter o que traduzir.
        return [text.strip()]
    if language in _CJK_LANGUAGES:
        # Sem espaço entre as palavras, "uma palavra só" não quer dizer nada.
        return sentences
    return _merge_single_words(sentences)


def _merge_single_words(sentences: list[str]) -> list[str]:
    """Uma "frase" de uma palavra só vai junto da seguinte (ou da anterior)."""
    merged: list[str] = []
    carry = ""
    for sentence in sentences:
        current = f"{carry} {sentence}".strip() if carry else sentence
        carry = ""
        if len(_TAG.sub("", current).split()) <= 1:
            carry = current
            continue
        merged.append(current)
    if carry:
        if merged:
            merged[-1] = f"{merged[-1]} {carry}"
        else:
            merged.append(carry)
    return [_join_tags(sentence) for sentence in merged]


# Uma sequência de tags que fecham, espaço, e uma sequência de tags que abrem.
_SPLIT_TAGS = re.compile(r"((?:</[a-zA-Z][a-zA-Z0-9]*>)+)(\s+)((?:<[a-zA-Z][^>]*>)+)")
_CLOSE_NAME = re.compile(r"</([a-zA-Z][a-zA-Z0-9]*)>")


def _join_tags(text: str) -> str:
    """Desfaz o fecha-e-reabre que o corte criou.

    "</strong></a> <a href="#x"><strong>" vira um espaço quando as tags que
    reabrem são, na ordem inversa, as mesmas que fecharam.
    """

    def undo(match: re.Match[str]) -> str:
        closed = _CLOSE_NAME.findall(match.group(1))
        reopened = [
            parsed.group(2).lower()
            for tag in _TAG.findall(match.group(3))
            if (parsed := _TAG_NAME.match(tag))
        ]
        if [name.lower() for name in reversed(closed)] == reopened:
            return match.group(2)
        return match.group(0)

    return _SPLIT_TAGS.sub(undo, text)


def join_sentences(sentences: list[str]) -> str:
    """O texto de volta, a partir das frases (inverso de split_sentences)."""
    return _join_tags(" ".join(sentence.strip() for sentence in sentences if sentence.strip()))
