"""Revisão visual da conversão: a página do PDF ao lado do texto extraído dela.

A revisão de texto (ai/reviewer.py) só vê o que a extração produziu; não tem
como saber que uma linha sumiu, que o OCR leu "rn" onde a página diz "m", ou
que um título virou parágrafo porque a fonte enganou o extrator. Aqui o modelo
recebe a imagem da página e os blocos extraídos dela, e aponta o que não
confere.

Mesmas regras do revisor de texto: operações de uma lista fechada, blocos
referidos por número, propostas validadas antes de virar sugestão. A única
operação nova é "insert_after", para texto que está na página e não no livro;
é também a mais vigiada (ai/review_validation.py).

Um pedido por página. O modelo responde só o que está errado: uma página
certa volta como [], e custa só a leitura da imagem.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..core.blocks import Document
from ..core.review import Suspect
from .base import Message
from .reviewer import (
    BatchOutcome,
    Case,
    ProgressCallback,
    Reviewer,
    ReviewResult,
    _describe,
    _load_array,
    _proposal,
)

log = logging.getLogger(__name__)

# Quantas páginas uma revisão visual olha quando ninguém diz quais: as com
# mais trechos duvidosos primeiro. Uma imagem custa bem mais que um trecho.
DEFAULT_MAX_PAGES = 10

VISUAL_REASON = "visual"

VISUAL_PROMPT = """Você confere a conversão automática de um livro de PDF para \
EPUB. Recebe a IMAGEM de uma página do PDF e os blocos de texto que a \
conversão extraiu dessa página, numerados ⟦n⟧, com o tipo de cada um. Compare \
os dois e aponte SÓ o que está errado no texto extraído.

Operações (uma por bloco):
- "fix_text": o texto do bloco difere da página por erro de extração (letra \
trocada pelo OCR, palavra partida, espaço faltando). Informe "text" com o \
bloco inteiro corrigido, igual à página. Não reescreva, não traduza, não \
mude a pontuação do autor e mantenha <em>, <strong> e <sup>.
- "set_kind": o tipo está errado (um título na página extraído como \
parágrafo, por exemplo). Informe "kind": heading1, heading2, heading3, \
paragraph, quote, list_item ou footnote.
- "delete": o bloco não é texto do livro: número de página, cabeçalho ou \
rodapé corrente.
- "merge_next" / "merge_prev": o bloco é um pedaço de parágrafo que a \
conversão partiu; na página ele continua no seguinte / no anterior.
- "insert_after": há texto na página que NÃO aparece em nenhum bloco (uma \
linha ou parágrafo que a extração perdeu). Informe "text" com o texto que \
falta, transcrito exatamente, e "n" do bloco depois do qual ele entra. Não \
use para cabeçalho, número de página ou legenda de imagem.

Diferenças de hifenização no fim da linha, de quebra de linha e de espaço \
entre parágrafos NÃO são erro: a conversão junta as linhas de propósito.

Responda SOMENTE com um array JSON, com um objeto por problema, assim:
[{"n": 2, "op": "fix_text", "text": "...", "confidence": 0.9, "reason": "..."}]
"confidence" vai de 0 a 1 e "reason" é uma frase curta em português. Página \
sem problema: []. Sem texto antes ou depois do JSON."""


@dataclass
class PageCase:
    """Uma página: o número, a imagem e os blocos extraídos dela, em ordem."""

    page: int
    image: bytes
    cases: list[Case]


def choose_pages(
    document: Document, suspects: Sequence[Suspect], *, limit: int = DEFAULT_MAX_PAGES
) -> list[int]:
    """As páginas que mais valem uma conferência visual: as com mais suspeitos.

    Em ordem de página, para a revisão andar como o livro.
    """
    pages = {b.id: b.page for b in document.all_blocks() if b.page is not None}
    count = Counter(pages[s.block_id] for s in suspects if s.block_id in pages)
    chosen = sorted(count, key=lambda page: (-count[page], page))[:limit]
    return sorted(chosen)


def page_blocks(document: Document, page: int) -> list[Case]:
    """Os blocos da página, de qualquer capítulo, como casos sem motivo de suspeita."""
    found: list[Case] = []
    for chapter in document.chapters:
        for index, block in enumerate(chapter.blocks):
            if block.page == page and block.kind not in ("image", "page_break"):
                found.append(Case(chapter.id, block, [VISUAL_REASON], opens_chapter=index == 0))
    return found


def format_page(case: PageCase) -> str:
    lines = [f"Página {case.page}. Blocos extraídos dela:"]
    for n, item in enumerate(case.cases, start=1):
        lines.append(f"⟦{n}⟧ ({_describe(item.block)}): {item.block.text}")
    return "\n\n".join(lines)


def parse_page(reply: str, cases: Sequence[Case]) -> list | None:
    """As propostas da página, ou None se a resposta não é um array JSON.

    Ao contrário da revisão de texto, o modelo só fala dos blocos com problema;
    um bloco que não aparece está certo. Um bloco só aceita uma operação.
    """
    items = _load_array(reply)
    if items is None:
        return None
    proposals = []
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        n = item.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(cases):
            continue
        if n in seen or item.get("op") == "ok":
            continue
        proposal = _proposal(item, cases[n - 1])
        if proposal is not None:
            seen.add(n)
            proposals.append(proposal)
    return proposals


class VisualReviewer(Reviewer):
    """Revisor que manda a imagem da página junto com o texto extraído dela."""

    def __init__(self, provider, **kwargs) -> None:
        kwargs.setdefault("concurrency", 2)
        super().__init__(provider, **kwargs)

    async def review_page(self, case: PageCase) -> BatchOutcome:
        book = f"Livro: {self.book_title}\n" if self.book_title.strip() else ""
        messages = [
            Message(role="system", content=VISUAL_PROMPT),
            Message(role="user", content=book + format_page(case), images=[case.image]),
        ]
        completion = await self.provider.complete_with_retry(messages, client=self.client)
        proposals = parse_page(completion.text, case.cases)
        if proposals is None:
            log.warning("A página %d voltou fora do formato", case.page)
            return BatchOutcome([], 0, 1, completion.usage)
        return BatchOutcome(proposals, 1, 0, completion.usage)

    async def review_pages(
        self,
        document: Document,
        pages: Sequence[PageCase],
        *,
        on_progress: ProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ReviewResult:
        """Revisa as páginas. `total`, `reviewed` e `failed` contam páginas."""
        self.book_title = document.title
        order = {b.id: i for i, b in enumerate(document.all_blocks())}
        with_text = [p for p in pages if p.cases]  # página só de imagem: nada a conferir
        return await self.run_batches(
            document,
            with_text,
            self.review_page,
            total=len(with_text),
            size=lambda _page: 1,
            order=order,
            on_progress=on_progress,
            should_stop=should_stop,
        )
