"""Extração de PDFs com texto nativo.

A ideia central: um PDF não guarda parágrafos, só caracteres posicionados numa
página. Para reconstruir a estrutura usamos as pistas tipográficas disponíveis —
tamanho de fonte, peso, recuo e espaçamento vertical — em vez de tentar adivinhar
pelo texto puro.

O pipeline:
    1. ler linhas com metadados de fonte
    2. descartar cabeçalhos, rodapés e números de página repetidos
    3. estabelecer o tamanho de fonte do corpo (mediana ponderada)
    4. agrupar linhas em parágrafos por espaçamento e recuo
    5. classificar cada parágrafo como título ou corpo
    6. encaixar as imagens entre os parágrafos pela posição na página

Itálico e negrito vêm da fonte de cada caractere e viram <em> e <strong> no
texto do bloco, o mesmo formato que o leitor de EPUB produz.

Usamos pdfminer.six (licença MIT) para manter o projeto sob licença permissiva.
Há um backend alternativo com PyMuPDF, mais rápido e preciso, mas AGPL — veja
docs/licencas.md antes de ativá-lo.
"""

from __future__ import annotations

import hashlib
import logging
import re
import statistics
import struct
import zlib
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTFigure, LTImage, LTTextContainer, LTTextLine
from pdfminer.pdftypes import (
    LITERALS_ASCII85_DECODE,
    LITERALS_ASCIIHEX_DECODE,
    LITERALS_DCT_DECODE,
    LITERALS_FLATE_DECODE,
    LITERALS_LZW_DECODE,
    LITERALS_RUNLENGTH_DECODE,
    resolve1,
)
from pdfminer.psparser import PSLiteral

from .blocks import Block, Vocabulary, keeps_hyphen, normalize_spaces

log = logging.getLogger(__name__)

# Uma linha precisa ser este tanto maior que o corpo para virar título.
# Calibrado em livros reais: títulos de capítulo costumam ficar entre 1,4x e
# 1,8x o corpo, e subtítulos entre 1,15x e 1,3x.
HEADING_RATIO_H1 = 1.40
HEADING_RATIO_H2 = 1.20
HEADING_RATIO_H3 = 1.08

# Títulos são curtos. Acima disso é texto grande, não título.
MAX_HEADING_WORDS = 18

# Fração das páginas em que uma linha precisa se repetir para ser cabeçalho.
RUNNING_HEAD_THRESHOLD = 0.45

# Margens (fração da altura da página) onde cabeçalhos e rodapés vivem.
TOP_MARGIN = 0.90
BOTTOM_MARGIN = 0.10

# Imagens menores que isto (em pontos, na página) são ornamentos: vinhetas,
# fios, capitulares desenhadas.
MIN_IMAGE_POINTS = 40
# A mesma imagem em tantas páginas ou mais é decoração (logotipo, moldura).
REPEATED_IMAGE_PAGES = 3

# Filtros sem perda que o pdfminer decodifica sozinho em stream.get_data().
_LOSSLESS_FILTERS = frozenset(
    LITERALS_FLATE_DECODE
    + LITERALS_LZW_DECODE
    + LITERALS_ASCII85_DECODE
    + LITERALS_ASCIIHEX_DECODE
    + LITERALS_RUNLENGTH_DECODE
)

# Um caractere com seu estilo: "", "b", "i" ou "bi".
StyledChar = tuple[str, str]


@dataclass
class Line:
    """Uma linha física do PDF com suas métricas tipográficas."""

    text: str
    size: float
    bold: bool
    italic: bool
    x0: float
    x1: float
    top: float  # distância do topo da página, cresce para baixo
    page: int
    page_height: float
    # O mesmo texto, caractere a caractere, com o estilo da fonte de cada um.
    chars: list[StyledChar] = field(default_factory=list)

    @property
    def word_count(self) -> int:
        return len(self.text.split())


@dataclass
class PageImage:
    """Uma imagem já convertida para um formato que o EPUB aceita."""

    page: int
    top: float
    extension: str  # "jpg" ou "png"
    data: bytes


@lru_cache(maxsize=256)
def _font_flags(name: str) -> tuple[bool, bool]:
    """Deduz negrito e itálico pelo nome da fonte embutida.

    Além de "Italic" e "Bold" por extenso, reconhece as abreviações comuns em
    fontes comerciais: "MinionPro-It", "Garamond-BoldIt", "Sabon-Bd".
    """
    lowered = name.lower().split("+", 1)[-1]  # "ABCDEF+Times-Italic"
    bold = bool(re.search(r"bold|black|heavy|semibold|[-,]bd\b|[-,]bdit\b", lowered))
    italic = bool(re.search(r"italic|oblique|[-,](?:bold|bd|semibold)?it\b", lowered))
    return bold, italic


def _char_style(fontname: str) -> str:
    bold, italic = _font_flags(fontname)
    return ("b" if bold else "") + ("i" if italic else "")


def _styled_line(text_line: LTTextLine) -> list[StyledChar]:
    """Os caracteres da linha com estilo, já normalizados como normalize_spaces.

    Espaços e hífens invisíveis são tratados aqui, e não depois, para que o
    texto e os estilos continuem alinhados caractere a caractere.
    """
    chars: list[StyledChar] = []
    style = ""
    for item in text_line:
        text = item.get_text()
        if isinstance(item, LTChar):
            style = _char_style(item.fontname)
        for char in text:
            if char == "­":  # hífen invisível de quebra
                continue
            if char.isspace():
                # Espaço herda o estilo anterior para não partir um trecho em
                # itálico em vários <em>.
                if chars and chars[-1][0] != " ":
                    chars.append((" ", style))
                continue
            chars.append((char, style))

    while chars and chars[-1][0] == " ":
        chars.pop()
    return chars


def _join_styled(
    previous: list[StyledChar],
    nxt: list[StyledChar],
    vocabulary: Vocabulary | None = None,
) -> list[StyledChar]:
    """join_hyphenated sobre caracteres com estilo. Mesmas regras."""
    while previous and previous[-1][0] == " ":
        previous = previous[:-1]
    while nxt and nxt[0][0] == " ":
        nxt = nxt[1:]
    before = _last_word(previous[:-1]) if previous and previous[-1][0] == "-" else ""
    if before and nxt[:1] and nxt[0][0].isupper():
        return previous + nxt  # Jean-Paul
    if before and nxt[:1] and nxt[0][0].islower():
        after = _first_word(nxt)
        if after and keeps_hyphen(before, after, vocabulary):
            return previous + nxt
        return previous[:-1] + nxt
    if not previous:
        return list(nxt)
    if not nxt:
        return previous
    joint = previous[-1][1] if previous[-1][1] == nxt[0][1] else ""
    return previous + [(" ", joint)] + nxt


def _last_word(chars: list[StyledChar]) -> str:
    word: list[str] = []
    for char, _ in reversed(chars):
        if not char.isalnum():
            break
        word.append(char)
    return "".join(reversed(word))


def _first_word(chars: list[StyledChar]) -> str:
    word: list[str] = []
    for char, _ in chars:
        if not char.isalnum():
            break
        word.append(char)
    return "".join(word)


_OPEN = {"b": "<strong>", "i": "<em>"}
_CLOSE = {"b": "</strong>", "i": "</em>"}


def _render_styled(chars: list[StyledChar]) -> str:
    """Texto com <em> e <strong> nos trechos em itálico e negrito."""
    out: list[str] = []
    current = ""

    def switch(style: str) -> None:
        nonlocal current
        # Fecha na ordem inversa da abertura: <strong><em>…</em></strong>.
        out.extend(_CLOSE[flag] for flag in reversed(current))
        out.extend(_OPEN[flag] for flag in style)
        current = style

    for char, style in chars:
        if char != " " and style != current:
            switch(style)
        out.append(char)
    switch("")

    text = "".join(out)
    # "<em>palavra </em>seguinte" fica "<em>palavra</em> seguinte".
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r" ((?:</em>|</strong>)+)", r"\1 ", text)
    return text


def _image_payload(image: LTImage) -> tuple[str, bytes] | None:
    """Converte uma imagem do PDF para JPEG ou PNG, ou None se não der.

    JPEG (DCTDecode) é copiado como está. Bitmaps de 8 bits em RGB ou tons de
    cinza viram PNG, montado aqui para não depender do Pillow. O resto (CMYK,
    paleta, 1 bit, JPEG 2000, JBIG2) é ignorado: são raros em livros e pedem
    conversões que o EPUB não precisa para o caso comum.
    """
    stream = image.stream
    if image.imagemask:
        return None

    filters = [f for f, _ in stream.get_filters()]
    channels = _color_channels(stream.get_any(("CS", "ColorSpace")))

    try:
        if filters and filters[-1] in LITERALS_DCT_DECODE:
            if channels == 4:  # JPEG CMYK aparece invertido em vários leitores
                return None
            return "jpg", stream.get_data()

        if any(f not in _LOSSLESS_FILTERS for f in filters):
            return None
        width, height = (int(v) for v in image.srcsize)
        if image.bits != 8 or channels not in (1, 3):
            return None
        raw = stream.get_data()
        if len(raw) != width * height * channels:
            return None
        return "png", _encode_png(width, height, channels, raw)
    except Exception:  # noqa: BLE001 - fluxos de terceiros, às vezes malformados
        log.debug("Imagem %s ignorada: não foi possível decodificar", image.name)
        return None


def _color_channels(spec) -> int | None:
    spec = resolve1(spec)
    if isinstance(spec, list) and spec:
        name = getattr(resolve1(spec[0]), "name", "")
        if name == "ICCBased" and len(spec) > 1:
            return int(resolve1(spec[1]).get("N", 0)) or None
        return None  # Indexed, Separation, DeviceN...
    name = spec.name if isinstance(spec, PSLiteral) else ""
    return {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}.get(name)


def _encode_png(width: int, height: int, channels: int, raw: bytes) -> bytes:
    row = width * channels
    scanlines = b"".join(
        b"\x00" + raw[y * row : (y + 1) * row] for y in range(height)
    )

    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    color_type = 2 if channels == 3 else 0
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(scanlines, 6))
        + chunk(b"IEND", b"")
    )


def _walk_images(element):
    if isinstance(element, LTImage):
        yield element
    elif isinstance(element, LTFigure):
        for child in element:
            yield from _walk_images(child)


def _read_lines(
    pdf_path: str, laparams: LAParams, *, with_images: bool = False
) -> tuple[list[Line], list[PageImage]]:
    """Percorre o PDF e devolve as linhas com metadados e, se pedido, as imagens."""
    lines: list[Line] = []
    images: list[PageImage] = []

    for page_number, page_layout in enumerate(
        extract_pages(pdf_path, laparams=laparams), start=1
    ):
        page_height = float(page_layout.height) or 1.0

        for element in page_layout:
            if with_images and isinstance(element, LTFigure):
                for image in _walk_images(element):
                    if min(image.width, image.height) < MIN_IMAGE_POINTS:
                        continue
                    payload = _image_payload(image)
                    if payload:
                        images.append(
                            PageImage(
                                page=page_number,
                                top=page_height - float(image.y1),
                                extension=payload[0],
                                data=payload[1],
                            )
                        )
                continue

            if not isinstance(element, LTTextContainer):
                continue

            for text_line in element:
                if not isinstance(text_line, LTTextLine):
                    continue

                chars = _styled_line(text_line)
                text = "".join(char for char, _ in chars)
                if not text:
                    continue

                sizes: list[float] = []
                fonts: Counter[str] = Counter()
                for char in text_line:
                    if isinstance(char, LTChar):
                        sizes.append(round(char.size, 1))
                        fonts[char.fontname] += 1

                if not sizes:
                    continue

                # A moda representa melhor a linha que a média quando há um
                # caractere isolado em tamanho diferente (nota de rodapé, etc).
                size = statistics.mode(sizes)
                dominant_font = fonts.most_common(1)[0][0] if fonts else ""
                bold, italic = _font_flags(dominant_font)

                lines.append(
                    Line(
                        text=text,
                        size=size,
                        bold=bold,
                        italic=italic,
                        x0=float(text_line.x0),
                        x1=float(text_line.x1),
                        top=page_height - float(text_line.y1),
                        page=page_number,
                        page_height=page_height,
                        chars=chars,
                    )
                )

    return lines, images


def _strip_running_heads(lines: list[Line]) -> list[Line]:
    """Remove cabeçalhos, rodapés e números de página.

    Critério: a linha fica na margem superior ou inferior E o seu texto
    normalizado se repete numa boa parte das páginas. Números de página puros
    são removidos sem exigir repetição, já que cada um é diferente.
    """
    if not lines:
        return lines

    pages = {line.page for line in lines}
    total_pages = len(pages)
    if total_pages < 3:
        # Com poucas páginas a repetição não é evidência confiável.
        return [line for line in lines if not _is_bare_page_number(line)]

    margin_texts: Counter[str] = Counter()
    for line in lines:
        if _in_margin(line):
            margin_texts[_signature(line.text)] += 1

    repeated = {
        text
        for text, count in margin_texts.items()
        if count >= max(2, total_pages * RUNNING_HEAD_THRESHOLD)
    }

    kept: list[Line] = []
    for line in lines:
        if _is_bare_page_number(line):
            continue
        if _in_margin(line) and _signature(line.text) in repeated:
            continue
        kept.append(line)

    removed = len(lines) - len(kept)
    if removed:
        log.info("Removidas %d linhas de cabeçalho/rodapé", removed)
    return kept


def _in_margin(line: Line) -> bool:
    relative = line.top / line.page_height
    return relative < (1 - TOP_MARGIN) or relative > (1 - BOTTOM_MARGIN)


def _signature(text: str) -> str:
    """Normaliza a linha para comparação, trocando dígitos por um curinga.

    Assim "Capítulo 3 — 47" e "Capítulo 3 — 48" são reconhecidos como o mesmo
    cabeçalho corrente.
    """
    return "".join("#" if c.isdigit() else c.lower() for c in text).strip()


def _is_bare_page_number(line: Line) -> bool:
    stripped = line.text.strip(" .-—–[]()")
    if not stripped or len(stripped) > 6:
        return False
    if not _in_margin(line):
        return False
    return stripped.isdigit() or _is_roman(stripped)


def _is_roman(text: str) -> bool:
    return bool(text) and all(c in "ivxlcdmIVXLCDM" for c in text)


def _body_size(lines: list[Line]) -> float:
    """Tamanho de fonte do corpo do texto, ponderado por quantidade de texto.

    Ponderamos por número de caracteres porque o corpo do livro domina em
    volume, mesmo que haja muitas linhas curtas de título.
    """
    if not lines:
        return 10.0

    weights: Counter[float] = Counter()
    for line in lines:
        weights[line.size] += len(line.text)

    return weights.most_common(1)[0][0]


def _classify(
    group: list[Line], body_size: float
) -> str:
    """Decide se um grupo de linhas é título e de qual nível."""
    if not group:
        return "paragraph"

    size = max(line.size for line in group)
    words = sum(line.word_count for line in group)
    all_bold = all(line.bold for line in group)
    ratio = size / body_size if body_size else 1.0

    if words > MAX_HEADING_WORDS:
        return "paragraph"

    # Texto que termina em pontuação de frase raramente é título.
    joined = " ".join(line.text for line in group).rstrip()
    if joined.endswith((".", ";", ",", ":")) and ratio < HEADING_RATIO_H1:
        return "paragraph"

    if ratio >= HEADING_RATIO_H1:
        return "heading1"
    if ratio >= HEADING_RATIO_H2:
        return "heading2"
    if ratio >= HEADING_RATIO_H3 or (all_bold and words <= 10):
        return "heading3"

    # Linha isolada, em negrito e curta, no meio do corpo: subtítulo.
    if all_bold and words <= MAX_HEADING_WORDS:
        return "heading3"

    return "paragraph"


# Travessões (— e –) ficam de fora de propósito: em romances em português,
# espanhol e francês eles abrem falas, não listas. Tratá-los como marcadores
# transformaria todo diálogo do livro numa lista com marcadores.
_BULLETS = ("•", "·", "‣", "▪", "-", "*")


def _looks_like_list_item(text: str) -> bool:
    stripped = text.lstrip()
    if stripped[:1] in _BULLETS and stripped[1:2] in (" ", "\t"):
        return True
    # "1. ", "a) ", "iv. "
    head = stripped.split(" ", 1)[0] if " " in stripped else ""
    return bool(head) and head[:-1].isalnum() and head[-1:] in (".", ")") and len(head) <= 4


def _group_into_paragraphs(lines: list[Line], body_size: float) -> list[list[Line]]:
    """Agrupa linhas consecutivas em parágrafos.

    Quebramos o parágrafo quando: muda de página com espaço, o espaçamento
    vertical cresce muito, o tamanho da fonte muda, ou a linha seguinte tem
    recuo de primeira linha.
    """
    if not lines:
        return []

    # Espaçamento típico entre linhas do mesmo parágrafo.
    gaps = [
        b.top - a.top
        for a, b in zip(lines, lines[1:])
        if a.page == b.page and 0 < b.top - a.top < body_size * 4
    ]
    normal_gap = statistics.median(gaps) if gaps else body_size * 1.2
    gap_threshold = normal_gap * 1.45

    # Margem esquerda do corpo: o recuo mais comum.
    left_margins = Counter(round(line.x0) for line in lines)
    body_left = left_margins.most_common(1)[0][0] if left_margins else 0

    groups: list[list[Line]] = [[lines[0]]]

    for previous, current in zip(lines, lines[1:]):
        current_group = groups[-1]
        split = False

        if current.page != previous.page:
            # Páginas diferentes só quebram o parágrafo se a linha anterior
            # terminou uma frase; livros quebram parágrafos entre páginas.
            split = previous.text.rstrip().endswith((".", "!", "?", '"', "”", "»"))
        else:
            vertical_gap = current.top - previous.top
            if vertical_gap > gap_threshold:
                split = True
            elif abs(current.size - previous.size) > 0.6:
                split = True
            elif current.x0 > body_left + body_size * 0.8:
                # Recuo de primeira linha indica parágrafo novo.
                split = True
            elif _looks_like_list_item(current.text):
                split = True

        if split:
            groups.append([current])
        else:
            current_group.append(current)

    return groups


def extract_blocks(
    pdf_path: str,
    *,
    source_name: str | None = None,
    images: dict[str, bytes] | None = None,
) -> tuple[list[Block], str]:
    """Converte um PDF com texto nativo numa lista plana de blocos.

    Devolve os blocos e um palpite de título. A divisão em capítulos é
    responsabilidade de structure.split_into_chapters, para manter este módulo
    focado só em extração.

    source_name é o nome original do arquivo enviado pelo usuário. Importa
    porque o upload chega num arquivo temporário de nome aleatório, e é dele
    que sai o palpite de título quando o PDF não traz folha de rosto.

    images, quando informado, recebe as imagens extraídas (nome do arquivo ->
    bytes, pronto para Document.images) e os blocos ganham os kind="image"
    correspondentes. Sem ele, as imagens são ignoradas, como antes.

    Levanta ValueError quando o PDF não tem texto extraível, o que normalmente
    significa que é um documento escaneado e precisaria de OCR.
    """
    laparams = LAParams(
        line_margin=0.4,
        char_margin=2.0,
        word_margin=0.1,
        boxes_flow=0.5,
        detect_vertical=False,
    )

    lines, page_images = _read_lines(pdf_path, laparams, with_images=images is not None)
    if not lines:
        raise ValueError(
            "Nenhum texto extraível encontrado. O PDF provavelmente é "
            "escaneado e precisa passar por OCR antes da conversão."
        )

    lines = _strip_running_heads(lines)
    body_size = _body_size(lines)
    log.info("Tamanho de fonte do corpo detectado: %.1f", body_size)

    groups = _group_into_paragraphs(lines, body_size)
    # Palavras como o livro as escreve no meio das linhas: decide se o hífen
    # de "guarda-" no fim de uma linha é de quebra ou da palavra.
    vocabulary = Vocabulary.from_texts(line.text for line in lines)

    # (página, topo, bloco): a posição serve para encaixar as imagens depois.
    placed: list[tuple[int, float, Block]] = []
    for group in groups:
        kind = _classify(group, body_size)

        chars: list[StyledChar] = []
        for line in group:
            chars = _join_styled(chars, line.chars, vocabulary)
        plain = normalize_spaces("".join(char for char, _ in chars))

        if not plain:
            continue

        if kind == "paragraph" and _looks_like_list_item(plain):
            kind = "list_item"
            stripped = _strip_bullet(plain)
            if len(chars) == len(plain):
                chars = chars[len(chars) - len(stripped) :]
            else:  # desalinhado por algum espaço exótico: perde o estilo, não o texto
                chars = [(char, "") for char in stripped]
            plain = stripped

        # Títulos já têm destaque próprio; marcar o negrito de novo só suja o texto.
        text = plain if kind.startswith("heading") else normalize_spaces(_render_styled(chars))

        placed.append(
            (
                group[0].page,
                group[0].top,
                Block(
                    kind=kind,  # type: ignore[arg-type]
                    text=text,
                    page=group[0].page,
                    meta={"size": group[0].size},
                ),
            )
        )

    blocks = [block for _, _, block in placed]
    title = _guess_title(blocks, source_name or pdf_path)

    if images is not None and page_images:
        blocks = _place_images(placed, page_images, images)

    return blocks, title


def _place_images(
    placed: list[tuple[int, float, Block]],
    page_images: list[PageImage],
    images: dict[str, bytes],
) -> list[Block]:
    """Intercala blocos de imagem entre os de texto, pela posição na página.

    Cada imagem entra antes do primeiro parágrafo que começa abaixo dela.
    Imagens repetidas em muitas páginas (logotipos, molduras) são descartadas.
    """
    pages_by_hash: dict[str, set[int]] = {}
    for image in page_images:
        digest = hashlib.sha1(image.data).hexdigest()
        pages_by_hash.setdefault(digest, set()).add(image.page)

    kept: list[tuple[int, float, Block]] = []
    names_by_hash: dict[str, str] = {}
    for image in sorted(page_images, key=lambda i: (i.page, i.top)):
        digest = hashlib.sha1(image.data).hexdigest()
        if len(pages_by_hash[digest]) >= REPEATED_IMAGE_PAGES:
            continue
        # A mesma figura repetida poucas vezes vira um arquivo só.
        name = names_by_hash.get(digest)
        if name is None:
            name = f"pdf-{len(names_by_hash) + 1:03d}.{image.extension}"
            names_by_hash[digest] = name
            images[name] = image.data
        kept.append((image.page, image.top, Block(kind="image", src=name, page=image.page)))

    merged: list[Block] = []
    pending = iter(kept)
    next_image = next(pending, None)
    for page, top, block in placed:
        while next_image is not None and (next_image[0], next_image[1]) < (page, top):
            merged.append(next_image[2])
            next_image = next(pending, None)
        merged.append(block)
    while next_image is not None:
        merged.append(next_image[2])
        next_image = next(pending, None)

    if kept:
        log.info("Extraídas %d imagens do PDF", len(kept))
    return merged


def _strip_bullet(text: str) -> str:
    stripped = text.lstrip()
    if stripped[:1] in _BULLETS:
        return stripped[1:].lstrip()
    head, _, rest = stripped.partition(" ")
    if head and head[-1:] in (".", ")") and head[:-1].isalnum() and len(head) <= 4:
        return rest.lstrip()
    return stripped


_CHAPTER_OPENER = re.compile(
    r"^\s*(cap[íi]tulo|chapter|parte|part|se[çc][ãa]o|book|livro)\b"
    r"|^\s*(\d{1,3}|[ivxlcdm]{1,7})\s*[.\-—–:]",
    re.IGNORECASE,
)


def _guess_title(blocks: list[Block], pdf_path: str) -> str:
    """Palpite do título do livro, a partir da folha de rosto.

    Um PDF que começa direto em "Capítulo 1" não traz o título do livro em
    lugar nenhum, então o nome do arquivo é o melhor palpite disponível — bem
    melhor que batizar o livro de "Capítulo 1".
    """
    import os

    fallback = os.path.splitext(os.path.basename(pdf_path))[0].replace("_", " ")

    for block in blocks[:12]:
        text = block.text.strip()
        if block.kind == "heading1" and text and not _CHAPTER_OPENER.match(text):
            return text

    return fallback or "Sem título"
