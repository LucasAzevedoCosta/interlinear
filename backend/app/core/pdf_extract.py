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

Usamos pdfminer.six (licença MIT) para manter o projeto sob licença permissiva.
Há um backend alternativo com PyMuPDF, mais rápido e preciso, mas AGPL — veja
docs/licencas.md antes de ativá-lo.
"""

from __future__ import annotations

import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass

from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTTextContainer, LTTextLine

from .blocks import Block, join_hyphenated, normalize_spaces

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

    @property
    def word_count(self) -> int:
        return len(self.text.split())


def _font_flags(name: str) -> tuple[bool, bool]:
    """Deduz negrito e itálico pelo nome da fonte embutida."""
    lowered = name.lower()
    bold = "bold" in lowered or "black" in lowered or "heavy" in lowered
    italic = "italic" in lowered or "oblique" in lowered
    return bold, italic


def _read_lines(pdf_path: str, laparams: LAParams) -> list[Line]:
    """Percorre o PDF e devolve as linhas com metadados."""
    lines: list[Line] = []

    for page_number, page_layout in enumerate(
        extract_pages(pdf_path, laparams=laparams), start=1
    ):
        page_height = float(page_layout.height) or 1.0

        for element in page_layout:
            if not isinstance(element, LTTextContainer):
                continue

            for text_line in element:
                if not isinstance(text_line, LTTextLine):
                    continue

                raw = text_line.get_text()
                text = normalize_spaces(raw)
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
                    )
                )

    return lines


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
    pdf_path: str, *, source_name: str | None = None
) -> tuple[list[Block], str]:
    """Converte um PDF com texto nativo numa lista plana de blocos.

    Devolve os blocos e um palpite de título. A divisão em capítulos é
    responsabilidade de structure.split_into_chapters, para manter este módulo
    focado só em extração.

    source_name é o nome original do arquivo enviado pelo usuário. Importa
    porque o upload chega num arquivo temporário de nome aleatório, e é dele
    que sai o palpite de título quando o PDF não traz folha de rosto.

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

    lines = _read_lines(pdf_path, laparams)
    if not lines:
        raise ValueError(
            "Nenhum texto extraível encontrado. O PDF provavelmente é "
            "escaneado e precisa passar por OCR antes da conversão."
        )

    lines = _strip_running_heads(lines)
    body_size = _body_size(lines)
    log.info("Tamanho de fonte do corpo detectado: %.1f", body_size)

    groups = _group_into_paragraphs(lines, body_size)

    blocks: list[Block] = []
    for group in groups:
        kind = _classify(group, body_size)

        text = ""
        for line in group:
            text = join_hyphenated(text, line.text)
        text = normalize_spaces(text)

        if not text:
            continue

        if kind == "paragraph" and _looks_like_list_item(text):
            kind = "list_item"
            text = _strip_bullet(text)

        blocks.append(
            Block(
                kind=kind,  # type: ignore[arg-type]
                text=text,
                page=group[0].page,
                meta={"size": group[0].size},
            )
        )

    return blocks, _guess_title(blocks, source_name or pdf_path)


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
