"""Revisão da conversão por IA: o modelo decide o que fazer com cada suspeito.

A marcação de suspeitos (core/review.py) aponta os trechos duvidosos; aqui um
modelo olha cada um com os vizinhos e responde o que fazer. Ver
docs/conversao.md, seção 5, e docs-local/revisao-ia-conversao-plano.md.

O ponto central: é **classificação, não geração**. O modelo não reescreve o
livro; escolhe uma operação de uma lista fechada (juntar, trocar o tipo,
apagar, corrigir uma letra…). Isso o impede de resumir, "melhorar" o estilo
ou traduzir sem querer, e deixa cada decisão conferível.

Nada é aplicado aqui. O resultado são propostas; a validação do conteúdo
(Fase 3) e a aplicação com aceite do usuário (Fase 4) vêm depois.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx

from ..core.blocks import Block, Document
from ..core.review import Suspect
from .base import Message, PlanLimitError, Provider, ProviderError, Usage
from .translator import MAX_CONSECUTIVE_FAILURES, is_fatal, looks_like_refusal

log = logging.getLogger(__name__)

# As operações que o modelo pode propor. Qualquer outra é descartada.
OPERATIONS = frozenset(
    {
        "ok",  # o trecho está certo
        "merge_prev",  # o bloco continua o anterior (parágrafo partido)
        "merge_next",  # o bloco continua no seguinte
        "set_kind",  # o tipo está errado: payload "kind"
        "delete",  # sobra da página: número, cabeçalho, rodapé
        "fix_text",  # erro de extração no texto: payload "text"
        "start_chapter",  # um capítulo começa neste bloco
        "merge_chapter",  # o capítulo do bloco devia fazer parte do anterior
    }
)

# Tipos que set_kind pode escolher. Imagem e quebra não saem de texto.
KINDS = frozenset(
    {"heading1", "heading2", "heading3", "paragraph", "quote", "list_item", "footnote"}
)

# Quanto texto de cada vizinho vai como contexto: o fim do anterior e o
# começo do seguinte bastam para decidir uma junção.
CONTEXT_CHARS = 300

_MARKER = "⟦{n}⟧"

SYSTEM_PROMPT = """Você revisa a conversão automática de um livro de PDF para \
EPUB. A conversão reconstrói parágrafos, títulos e capítulos a partir da \
posição do texto na página, e às vezes erra. Você recebe trechos que uma \
regra marcou como duvidosos, cada um com o motivo, o tipo do bloco, a página \
e os blocos vizinhos.

Para cada trecho, escolha UMA operação:
- "ok": o trecho está certo. Na dúvida, é esta.
- "merge_prev": o trecho é a continuação do bloco anterior (um parágrafo \
partido pela quebra de página ou por uma figura).
- "merge_next": o trecho continua no bloco seguinte.
- "set_kind": o tipo está errado. Informe "kind": heading1, heading2, \
heading3, paragraph, quote, list_item ou footnote.
- "delete": o trecho não é do livro, é sobra da página: número de página, \
cabeçalho ou rodapé corrente.
- "fix_text": o texto tem erro de extração (palavra partida por hífen, \
espaço sobrando ou faltando, letra trocada pelo OCR). Informe "text" com o \
bloco inteiro corrigido. Corrija SÓ o erro de extração: não reescreva, não \
traduza, não mude estilo nem pontuação do autor, e mantenha as marcações \
<em>, <strong> e <sup> como estão.
- "start_chapter": um capítulo novo começa neste bloco.
- "merge_chapter": o capítulo deste bloco foi separado por engano e devia \
fazer parte do anterior. Dedicatória, epígrafe, folha de rosto e prefácio \
curtos são partes legítimas do livro, mesmo com poucas palavras: para eles, \
"ok".

Responda SOMENTE com um array JSON, um objeto por trecho, assim:
[{"n": 1, "op": "merge_prev", "confidence": 0.9, "reason": "frase continua"}]
"n" é o número do trecho, "confidence" vai de 0 a 1 e "reason" é uma frase \
curta em português. Sem texto antes ou depois do JSON."""

_KIND_NAMES = {
    "heading1": "título 1",
    "heading2": "título 2",
    "heading3": "título 3",
    "paragraph": "parágrafo",
    "quote": "citação",
    "list_item": "item de lista",
    "footnote": "nota de rodapé",
    "table": "tabela",
    "image": "imagem",
    "page_break": "quebra de página",
}

_REASON_NAMES = {
    "lowercase_start": "começa com minúscula",
    "open_ending": "termina sem pontuação antes de outro parágrafo",
    "long_heading": "título longo demais",
    "tiny_block": "bloco de um ou dois caracteres",
    "repeated_text": "texto repetido em várias páginas",
    "short_chapter": "capítulo muito curto (este é o primeiro bloco dele)",
}

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)


@dataclass
class Case:
    """Um bloco suspeito, com o que o modelo precisa para decidir."""

    chapter_id: str
    block: Block
    reasons: list[str]
    before: Block | None = None
    after: Block | None = None


@dataclass
class Proposal:
    """O que o modelo propõe para um bloco. Ainda não validado nem aplicado."""

    chapter_id: str
    block_id: str
    op: str
    confidence: float
    reason: str
    # Motivos da marcação que levaram o bloco ao modelo.
    suspects: list[str] = field(default_factory=list)
    # "kind" para set_kind, "text" para fix_text.
    payload: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewResult:
    proposals: list[Proposal] = field(default_factory=list)
    reviewed: int = 0
    # Trechos sem resposta aproveitável (formato errado, erro, recusa).
    failed: int = 0
    total: int = 0
    usage: Usage = field(default_factory=Usage)


ProgressCallback = Callable[[ReviewResult], None]


# --------------------------------------------------------------------------
# Montagem
# --------------------------------------------------------------------------


def build_cases(document: Document, suspects: Sequence[Suspect]) -> list[Case]:
    """Um caso por bloco suspeito, na ordem de leitura, com os vizinhos."""
    reasons: dict[str, list[str]] = {}
    for suspect in suspects:
        if suspect.block_id:
            reasons.setdefault(suspect.block_id, []).append(suspect.reason)

    cases: list[Case] = []
    for chapter in document.chapters:
        for index, block in enumerate(chapter.blocks):
            if block.id not in reasons:
                continue
            cases.append(
                Case(
                    chapter_id=chapter.id,
                    block=block,
                    reasons=reasons[block.id],
                    before=chapter.blocks[index - 1] if index > 0 else None,
                    after=chapter.blocks[index + 1] if index + 1 < len(chapter.blocks) else None,
                )
            )
    return cases


def build_batches(
    cases: Sequence[Case], *, max_chars: int, max_items: int = 20
) -> list[list[Case]]:
    """Lotes por orçamento de caracteres; um caso grande vai sozinho."""
    batches: list[list[Case]] = []
    current: list[Case] = []
    size = 0
    for case in cases:
        length = len(case.block.text) + 2 * CONTEXT_CHARS
        if current and (size + length > max_chars or len(current) >= max_items):
            batches.append(current)
            current, size = [], 0
        current.append(case)
        size += length
    if current:
        batches.append(current)
    return batches


def _describe(block: Block) -> str:
    kind = _KIND_NAMES.get(block.kind, block.kind)
    return f"{kind}, página {block.page}" if block.page is not None else kind


def _neighbor(label: str, block: Block | None, *, tail: bool) -> str:
    """O vizinho como contexto: o fim do anterior (`tail`) ou o começo do seguinte."""
    if block is None:
        edge = "começo" if tail else "fim"
        return f"{label}: (nenhum, {edge} do capítulo)"
    text = block.text if block.kind != "image" else f"[imagem {block.src or ''}]"
    if len(text) > CONTEXT_CHARS:
        text = "…" + text[-CONTEXT_CHARS:] if tail else text[:CONTEXT_CHARS] + "…"
    return f"{label} ({_describe(block)}): {text}"


def format_case(n: int, case: Case) -> str:
    reasons = "; ".join(_REASON_NAMES.get(r, r) for r in case.reasons)
    return "\n".join(
        [
            _MARKER.format(n=n),
            f"Motivo: {reasons}",
            _neighbor("Bloco anterior", case.before, tail=True),
            f"TRECHO ({_describe(case.block)}): {case.block.text}",
            _neighbor("Bloco seguinte", case.after, tail=False),
        ]
    )


# --------------------------------------------------------------------------
# Leitura da resposta
# --------------------------------------------------------------------------


def parse_reply(reply: str, cases: Sequence[Case]) -> list[Proposal | None] | None:
    """Uma proposta por caso, na ordem dos casos; None onde não veio nada útil.

    Devolve None (sem lista) quando a resposta inteira não é um array JSON:
    sinal de que o lote precisa ser refeito de outro jeito. A forma de cada
    item é conferida aqui; o conteúdo (o texto corrigido mudou demais? a
    junção é possível?) fica para a validação da Fase 3.
    """
    items = _load_array(reply)
    if items is None:
        return None

    proposals: list[Proposal | None] = [None] * len(cases)
    for item in items:
        if not isinstance(item, dict):
            continue
        n = item.get("n")
        # bool é int em Python; "n": true não é um número de trecho.
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= len(cases):
            continue
        if proposals[n - 1] is not None:
            continue  # o primeiro vale; repetição é ruído
        proposals[n - 1] = _proposal(item, cases[n - 1])
    return proposals


def _load_array(reply: str) -> list[Any] | None:
    text = reply.strip()
    match = _JSON_ARRAY.search(text)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    return data if isinstance(data, list) else None


def _proposal(item: dict[str, Any], case: Case) -> Proposal | None:
    op = item.get("op")
    if op not in OPERATIONS:
        return None

    payload: dict[str, str] = {}
    if op == "set_kind":
        kind = item.get("kind")
        if kind not in KINDS or kind == case.block.kind:
            return None
        payload["kind"] = kind
    elif op == "fix_text":
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or text == case.block.text:
            return None
        payload["text"] = text.strip()

    try:
        confidence = float(item.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    if confidence != confidence:  # NaN
        confidence = 0.5

    reason = item.get("reason")
    return Proposal(
        chapter_id=case.chapter_id,
        block_id=case.block.id,
        op=op,
        confidence=min(1.0, max(0.0, confidence)),
        reason=reason.strip()[:300] if isinstance(reason, str) else "",
        suspects=list(case.reasons),
        payload=payload,
    )


# --------------------------------------------------------------------------
# Revisor
# --------------------------------------------------------------------------


class Reviewer:
    """Manda os suspeitos ao modelo em lotes e devolve as propostas.

    O volume é pequeno (um livro inteiro costuma dar poucos lotes), então o
    controle é mais simples que o da tradução: o limite do plano interrompe a
    revisão em vez de esperar a janela renovar, porque refazer custa quase
    nada.
    """

    def __init__(
        self,
        provider: Provider,
        *,
        client: httpx.AsyncClient | None = None,
        batch_chars: int = 4000,
        concurrency: int = 2,
    ) -> None:
        self.provider = provider
        self.batch_chars = batch_chars
        self.concurrency = concurrency
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "Reviewer":
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=True)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("Use o Reviewer como context manager (async with)")
        return self._client

    async def _ask(self, cases: Sequence[Case]) -> tuple[str, Usage]:
        listing = "\n\n".join(format_case(n, c) for n, c in enumerate(cases, start=1))
        messages = [
            Message(role="system", content=SYSTEM_PROMPT),
            Message(
                role="user",
                content=f"Revise os {len(cases)} trechos abaixo.\n\n{listing}",
            ),
        ]
        completion = await self.provider.complete_with_retry(messages, client=self.client)
        return completion.text, completion.usage

    async def review_batch(self, cases: Sequence[Case]) -> tuple[list[Proposal | None], Usage]:
        """Revisa um lote. Resposta fora do formato: refaz caso a caso."""
        reply, usage = await self._ask(cases)
        proposals = parse_reply(reply, cases)
        if proposals is not None:
            return proposals, usage
        if looks_like_refusal(reply, cases[0].block.text):
            # Recusa vale para o lote todo; refazer um a um só repete o pedido.
            log.warning("O modelo recusou revisar um lote de %d trechos", len(cases))
            return [None] * len(cases), usage
        if len(cases) == 1:
            return [None], usage

        log.warning("Lote de %d trechos voltou fora do formato; refazendo um a um", len(cases))
        results: list[Proposal | None] = []
        for case in cases:
            try:
                reply, extra = await self._ask([case])
            except ProviderError as exc:
                if is_fatal(exc) or isinstance(exc, PlanLimitError):
                    raise
                log.error("Falha ao revisar o bloco %s: %s", case.block.id, exc)
                results.append(None)
                continue
            usage = usage + extra
            single = parse_reply(reply, [case])
            results.append(single[0] if single else None)
        return results, usage

    async def review(
        self,
        document: Document,
        suspects: Sequence[Suspect],
        *,
        on_progress: ProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> ReviewResult:
        """Revisa os suspeitos do documento. Não altera o documento.

        Um erro que vale para o livro inteiro (chave, permissão, modelo, limite
        do plano) ou lotes seguidos falhando interrompem tudo e sobem como
        ProviderError; o que já foi revisado se perde, porque refazer é barato.
        """
        cases = build_cases(document, suspects)
        batches = build_batches(cases, max_chars=self.batch_chars)
        result = ReviewResult(total=len(cases))
        semaphore = asyncio.Semaphore(max(1, self.concurrency))
        lock = asyncio.Lock()
        abort: list[ProviderError] = []
        consecutive_failures = 0

        def halted() -> bool:
            return bool(abort) or bool(should_stop and should_stop())

        async def run(batch: list[Case]) -> None:
            nonlocal consecutive_failures
            async with semaphore:
                if halted():
                    return
                try:
                    proposals, usage = await self.review_batch(batch)
                except ProviderError as exc:
                    log.error("Lote da revisão falhou: %s", exc)
                    async with lock:
                        result.failed += len(batch)
                        consecutive_failures += 1
                        if not abort and (
                            is_fatal(exc)
                            or isinstance(exc, PlanLimitError)
                            or consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                        ):
                            abort.append(exc)
                    return

            async with lock:
                result.usage = result.usage + usage
                for proposal in proposals:
                    if proposal is None:
                        result.failed += 1
                    else:
                        result.reviewed += 1
                        result.proposals.append(proposal)
                consecutive_failures = 0 if any(proposals) else consecutive_failures + 1
                if not abort and consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    abort.append(ProviderError("O modelo não devolveu revisões aproveitáveis."))
                if on_progress:
                    on_progress(result)

        await asyncio.gather(*(run(batch) for batch in batches))

        if abort:
            exc = abort[0]
            if is_fatal(exc) or isinstance(exc, PlanLimitError):
                raise exc
            raise ProviderError(
                f"A revisão parou depois de {MAX_CONSECUTIVE_FAILURES} lotes seguidos "
                f"sem resposta aproveitável. Último erro: {exc}",
                status=exc.status,
            ) from exc
        order = {case.block.id: i for i, case in enumerate(cases)}
        result.proposals.sort(key=lambda p: order[p.block_id])
        return result
