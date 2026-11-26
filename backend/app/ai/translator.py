"""Tradução de livros em lote.

Traduzir um livro não é traduzir um texto grande: é traduzir milhares de
parágrafos mantendo coerência entre eles. Os problemas reais são:

  * **Formato.** Mandar vários parágrafos de uma vez é mais barato e dá mais
    contexto ao modelo, mas é preciso recuperar a correspondência um-para-um.
    Modelos baratos juntam, dividem ou numeram errado. Por isso o lote é
    verificado e, se vier torto, cai para tradução individual.
  * **Consistência.** Nomes e termos precisam ser traduzidos igual do começo ao
    fim. Resolvemos com um glossário injetado em todo lote.
  * **Contexto.** Um parágrafo isolado é ambíguo. Mandamos os parágrafos
    vizinhos como contexto somente-leitura.
  * **Interrupção.** O progresso é salvo a cada lote, então fechar o navegador
    ou perder a conexão não joga fora o que já foi feito.
  * **Frase a frase.** No formato bilíngue frase a frase, cada parágrafo é
    dividido em frases (core/sentences.py) e cada frase ganha o seu marcador.
    O modelo vê o parágrafo inteiro, mas devolve uma tradução por frase, e o
    resultado vai para block.meta["sentences"]. Se não vier alinhado nem na
    segunda tentativa, o parágrafo é traduzido inteiro e marcado
    meta["align_failed"]: sai parágrafo a parágrafo.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import httpx

from ..core.blocks import Block
from ..core.sentences import join_sentences, split_sentences
from .base import Message, PlanLimitError, Provider, ProviderError, Usage

log = logging.getLogger(__name__)

# Marcador de segmento. Escolhido por ser improvável no texto de um livro e
# fácil o bastante para modelos pequenos reproduzirem.
_MARKER = "⟦{n}⟧"
_MARKER_RE = re.compile(r"⟦\s*(\d+)\s*⟧")

DEFAULT_SYSTEM_PROMPT = """Você é um tradutor literário profissional traduzindo \
de {source} para {target}.

Regras:
- Traduza o sentido e o tom, não palavra por palavra. O resultado deve soar \
natural para um leitor nativo de {target}.
- Preserve o registro do original: formal continua formal, coloquial continua \
coloquial, arcaico continua arcaico.
- Mantenha intactas as marcações inline como <em>, <strong> e <a href="...">, \
nas mesmas palavras correspondentes.
- Segmentos com várias linhas e células separadas por " | " são tabelas: \
traduza cada célula e mantenha as mesmas linhas e os mesmos separadores.
- Não acrescente, não remova e não resuma nada. Não explique suas escolhas.
- Nomes próprios de pessoas e lugares permanecem como estão, salvo indicação \
no glossário.
- Devolva exatamente a mesma quantidade de segmentos que recebeu, cada um \
precedido de seu marcador original."""


@dataclass
class TranslationSettings:
    source_language: str = "inglês"
    target_language: str = "português brasileiro"
    glossary: dict[str, str] = field(default_factory=dict)
    style_notes: str = ""
    batch_chars: int = 3500
    context_blocks: int = 1
    concurrency: int = 3
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Formato frase a frase: traduz alinhado, uma tradução por frase.
    aligned: bool = False
    # Código do idioma do original ("en", "pt-BR"), para dividir as frases.
    source_code: str = ""


@dataclass
class Progress:
    done: int = 0
    total: int = 0
    failed: int = 0
    refused: int = 0  # o modelo se recusou a traduzir
    usage: Usage = field(default_factory=Usage)
    current_note: str = ""


ProgressCallback = Callable[[Progress], Awaitable[None] | None]

# Respostas que valem para o livro inteiro: chave inválida, sem permissão,
# modelo inexistente. Insistir só repete o mesmo erro em cada lote.
FATAL_STATUSES = frozenset({401, 403, 404})

# Lotes seguidos falhando por qualquer motivo (servidor fora do ar, 400 por
# parâmetro inválido...) também indicam problema de configuração, não de texto.
MAX_CONSECUTIVE_FAILURES = 5

# Limite do plano sem hora de renovação informada: tenta de novo depois disto.
PLAN_LIMIT_WAIT = 10 * 60.0
# Folga depois da hora de renovação, para não bater no limite ainda fechado.
PLAN_LIMIT_MARGIN = 30.0


def is_fatal(exc: ProviderError) -> bool:
    return exc.fatal or (not exc.retryable and exc.status in FATAL_STATUSES)


# --------------------------------------------------------------------------
# Recusas
# --------------------------------------------------------------------------


class Refusal(str):
    """Resposta em que o modelo se recusou a traduzir o trecho.

    Não é tradução e não vai para block.translated: fica em
    block.meta["refused"], para o usuário ver e decidir.
    """


# O começo de uma recusa, em inglês, português e espanhol...
_REFUSAL_OPENING = re.compile(
    r"\bi\s*(?:can[’']?t|cannot|can not|won[’']?t|will not|am not able|[’']m not able"
    r"|am unable|[’']m unable)\b"
    r"|\bi[’']m sorry\b|\bsorry, but\b|\bunfortunately,? i\b"
    r"|\bnão (?:posso|consigo|é possível)\b|\bdesculpe\b|\blamento\b|\binfelizmente,? não\b"
    r"|\bno (?:puedo|es posible)\b|\blo siento\b|\blamentablemente\b",
    re.IGNORECASE,
)
# ...e o assunto dela: direitos autorais, reproduzir ou traduzir o texto.
_REFUSAL_SUBJECT = re.compile(
    r"copyright|direitos autorais|derechos de autor"
    r"|(?:material|texto|obra|conteúdo) protegid|protected (?:text|material|work|content)"
    r"|reprodu|translat|tradu",
    re.IGNORECASE,
)
# A recusa diz a que veio logo no começo.
_REFUSAL_WINDOW = 300


def looks_like_refusal(reply: str, source: str) -> bool:
    """Diz se a resposta é uma recusa em vez da tradução de `source`.

    Pede as duas coisas no começo da resposta: uma frase de recusa ("não
    posso", "I can't") e o assunto dela (direitos autorais, traduzir). Um
    original que já começa assim ("I can't," she said) é diálogo, não recusa.
    A regra pode errar; por isso a recusa é guardada e o usuário pode
    aceitá-la como tradução.
    """
    head = reply[:_REFUSAL_WINDOW]
    if not (_REFUSAL_OPENING.search(head) and _REFUSAL_SUBJECT.search(head)):
        return False
    return not _REFUSAL_OPENING.search(source[:_REFUSAL_WINDOW])


# --------------------------------------------------------------------------
# Montagem dos lotes
# --------------------------------------------------------------------------


def build_batches(
    blocks: Sequence[Block], *, max_chars: int, max_items: int = 25
) -> list[list[Block]]:
    """Agrupa blocos em lotes respeitando um orçamento de caracteres.

    Um bloco maior que o orçamento vira um lote sozinho; a divisão de
    parágrafos gigantes acontece na hora do envio.
    """
    batches: list[list[Block]] = []
    current: list[Block] = []
    size = 0

    for block in blocks:
        length = len(block.text)

        if current and (size + length > max_chars or len(current) >= max_items):
            batches.append(current)
            current = []
            size = 0

        current.append(block)
        size += length

    if current:
        batches.append(current)

    return batches


def _format_segments(blocks: Sequence[Block]) -> str:
    return "\n\n".join(
        f"{_MARKER.format(n=i)}\n{block.text}" for i, block in enumerate(blocks, start=1)
    )


def _parse_segments(reply: str, expected: int) -> list[str] | None:
    """Recupera os segmentos traduzidos da resposta do modelo.

    Devolve None se a contagem não bater, sinal de que o lote precisa ser
    refeito de outro jeito.
    """
    matches = list(_MARKER_RE.finditer(reply))
    if len(matches) != expected:
        return None

    segments: list[str] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(reply)
        segments.append(reply[start:end].strip())

    # Numeração fora de ordem indica que o modelo reorganizou o conteúdo.
    numbers = [int(m.group(1)) for m in matches]
    if numbers != list(range(1, expected + 1)):
        return None

    if any(not segment for segment in segments):
        return None

    return segments


def _glossary_text(glossary: dict[str, str]) -> str:
    if not glossary:
        return ""
    entries = "\n".join(f"- {source} → {target}" for source, target in glossary.items())
    return (
        "\n\nGlossário obrigatório. Use exatamente estas traduções sempre que "
        f"os termos aparecerem:\n{entries}"
    )


def _context_text(before: Sequence[Block], after: Sequence[Block]) -> str:
    parts = []
    if before:
        parts.append(
            "Trecho anterior (apenas para contexto, NÃO traduza):\n"
            + "\n".join(b.text for b in before)
        )
    if after:
        parts.append(
            "Trecho seguinte (apenas para contexto, NÃO traduza):\n"
            + "\n".join(b.text for b in after)
        )
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


# --------------------------------------------------------------------------
# Frase a frase
# --------------------------------------------------------------------------


class Aligned(str):
    """Tradução de um parágrafo feita frase a frase.

    O valor é a tradução inteira (as frases juntadas), que vai para
    block.translated como qualquer outra; `pairs` são os pares (frase do
    original, tradução dela), que vão para block.meta["sentences"].
    """

    pairs: list[tuple[str, str]]

    def __new__(cls, pairs: list[tuple[str, str]]) -> "Aligned":
        value = str.__new__(cls, join_sentences([target for _, target in pairs]))
        value.pairs = pairs
        return value


# Decisão do usuário (2026-11-26): natural, não ao pé da letra, mas sempre o
# mais fiel possível; e o conteúdo de cada frase fica no par dela, porque a
# tradução é mostrada frase a frase embaixo do original.
_ALIGNED_RULES = (
    "Estas são frases, agrupadas pelo parágrafo de onde vêm ([Parágrafo n]). A "
    "tradução vai ser mostrada frase a frase: cada frase do original seguida da "
    "tradução dela.\n"
    "- Leia o parágrafo inteiro para entender o contexto.\n"
    "- Traduza cada frase de forma natural em {target}, sem ser palavra por "
    "palavra, mas sempre o mais fiel possível ao sentido e ao tom do original.\n"
    "- Devolva exatamente uma tradução por marcador, na mesma ordem. O que está "
    "numa frase do original fica na tradução dela: não passe parte de uma frase "
    "para a vizinha, não junte nem divida frases.\n"
    "- Não repita as linhas [Parágrafo n]."
)

_PARAGRAPH_LABEL = re.compile(r"^\s*\[\s*Par[áa]grafo\s+\d+\s*\]\s*$", re.MULTILINE | re.IGNORECASE)


def _format_aligned(groups: Sequence[Sequence[str]]) -> str:
    """As frases numeradas em sequência, com o parágrafo de cada uma."""
    parts: list[str] = []
    number = 0
    for index, sentences in enumerate(groups, start=1):
        lines = [f"[Parágrafo {index}]"]
        for sentence in sentences:
            number += 1
            lines.append(f"{_MARKER.format(n=number)}\n{sentence}")
        parts.append("\n\n".join(lines))
    return "\n\n".join(parts)


def _clean_aligned(segment: str) -> str:
    """Tira de um segmento as linhas [Parágrafo n] que o modelo repetir."""
    return _PARAGRAPH_LABEL.sub("", segment).strip()


# --------------------------------------------------------------------------
# Tradutor
# --------------------------------------------------------------------------


class Translator:
    def __init__(
        self,
        provider: Provider,
        settings: TranslationSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.provider = provider
        self.settings = settings
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> "Translator":
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
            raise RuntimeError("Use o Translator como context manager (async with)")
        return self._client

    def _system_message(self) -> Message:
        prompt = self.settings.system_prompt.format(
            source=self.settings.source_language,
            target=self.settings.target_language,
        )
        if self.settings.style_notes.strip():
            prompt += f"\n\nInstruções adicionais do tradutor:\n{self.settings.style_notes.strip()}"
        prompt += _glossary_text(self.settings.glossary)
        return Message(role="system", content=prompt)

    async def translate_batch(
        self,
        batch: Sequence[Block],
        *,
        before: Sequence[Block] = (),
        after: Sequence[Block] = (),
    ) -> tuple[list[str], Usage]:
        """Traduz um lote, com verificação de formato e plano B individual.

        Cada item devolvido é a tradução, "" (falhou) ou uma Refusal.
        """
        if not batch:
            return [], Usage()

        user_content = (
            f"Traduza os {len(batch)} segmentos abaixo. Devolva cada um precedido "
            "do seu marcador ⟦n⟧, na mesma ordem, sem nenhum texto adicional."
            f"{_context_text(before, after)}\n\n"
            f"Segmentos a traduzir:\n\n{_format_segments(batch)}"
        )

        messages = [self._system_message(), Message(role="user", content=user_content)]

        completion = await self.provider.complete_with_retry(messages, client=self.client)
        segments = _parse_segments(completion.text, len(batch))

        if segments is not None:
            refused = [
                block
                for block, segment in zip(batch, segments)
                if looks_like_refusal(segment, block.text)
            ]
            if not refused:
                return segments, completion.usage
            # Recusa num segmento só: ele tenta de novo sozinho, fora do lote.
            retried, usage = await self._translate_individually(
                refused, base_usage=completion.usage
            )
            again = dict(zip(map(id, refused), retried))
            return [again.get(id(b), s) for b, s in zip(batch, segments)], usage

        log.warning(
            "Lote de %d segmentos voltou fora do formato; traduzindo individualmente",
            len(batch),
        )
        return await self._translate_individually(batch, base_usage=completion.usage)

    async def _translate_individually(
        self, batch: Sequence[Block], *, base_usage: Usage
    ) -> tuple[list[str], Usage]:
        """Plano B: um pedido por bloco, sem marcadores para errar."""
        usage = base_usage
        results: list[str] = []

        for block in batch:
            messages = [
                self._system_message(),
                Message(
                    role="user",
                    content=(
                        "Traduza o texto a seguir. Responda apenas com a tradução, "
                        "sem comentários, sem aspas ao redor e sem marcadores.\n\n" + block.text
                    ),
                ),
            ]
            try:
                completion = await self.provider.complete_with_retry(messages, client=self.client)
                usage = usage + completion.usage
                text = _clean_single(completion.text)
                if looks_like_refusal(text, block.text):
                    log.warning("O modelo recusou o bloco %s", block.id)
                    results.append(Refusal(text))
                else:
                    results.append(text)
            except ProviderError as exc:
                # O limite do plano sobe para o lote inteiro esperar e ser refeito.
                if is_fatal(exc) or isinstance(exc, PlanLimitError):
                    raise
                log.error("Falha ao traduzir bloco %s: %s", block.id, exc)
                results.append("")

        return results, usage

    async def translate_batch_aligned(
        self,
        batch: Sequence[Block],
        *,
        before: Sequence[Block] = (),
        after: Sequence[Block] = (),
    ) -> tuple[list[str], Usage]:
        """Traduz um lote frase a frase.

        Cada item devolvido é um Aligned (alinhou), um str (não alinhou nem
        sozinho: tradução do parágrafo inteiro), "" (falhou) ou uma Refusal.
        """
        if not batch:
            return [], Usage()
        groups = [self._sentences_of(block) for block in batch]
        total = sum(len(group) for group in groups)
        completion = await self.provider.complete_with_retry(
            [self._system_message(), self._aligned_message(groups, total, before, after)],
            client=self.client,
        )
        segments = _parse_segments(completion.text, total)
        if segments is None:
            log.warning(
                "Lote alinhado de %d frases voltou fora do formato; um parágrafo por vez", total
            )
            return await self._translate_aligned_individually(
                batch, groups, base_usage=completion.usage
            )

        results: list[str] = []
        redo: list[int] = []
        cursor = 0
        for index, (block, group) in enumerate(zip(batch, groups)):
            targets = [_clean_aligned(s) for s in segments[cursor : cursor + len(group)]]
            cursor += len(group)
            aligned = Aligned(list(zip(group, targets)))
            if not all(targets) or looks_like_refusal(aligned, block.text):
                redo.append(index)
                results.append("")
            else:
                results.append(aligned)
        usage = completion.usage
        if redo:
            # Frase vazia ou recusa: o parágrafo tenta de novo sozinho.
            again, usage = await self._translate_aligned_individually(
                [batch[i] for i in redo], [groups[i] for i in redo], base_usage=usage
            )
            for index, result in zip(redo, again):
                results[index] = result
        return results, usage

    def _sentences_of(self, block: Block) -> list[str]:
        return split_sentences(block.text, self.settings.source_code) or [block.text]

    def _aligned_message(
        self,
        groups: Sequence[Sequence[str]],
        total: int,
        before: Sequence[Block] = (),
        after: Sequence[Block] = (),
    ) -> Message:
        rules = _ALIGNED_RULES.format(target=self.settings.target_language)
        return Message(
            role="user",
            content=(
                f"Traduza as {total} frases abaixo. Devolva cada uma precedida do "
                "seu marcador ⟦n⟧, na mesma ordem, sem nenhum texto adicional.\n\n"
                f"{rules}"
                f"{_context_text(before, after)}\n\n"
                f"Segmentos a traduzir:\n\n{_format_aligned(groups)}"
            ),
        )

    async def _translate_aligned_individually(
        self, batch: Sequence[Block], groups: Sequence[Sequence[str]], *, base_usage: Usage
    ) -> tuple[list[str], Usage]:
        """Plano B: um parágrafo por pedido, ainda frase a frase.

        Se nem assim alinhar, o parágrafo é traduzido inteiro (plano C, o
        mesmo do modo normal): sai parágrafo a parágrafo, com align_failed.
        """
        usage = base_usage
        results: list[str] = []
        for block, group in zip(batch, groups):
            try:
                completion = await self.provider.complete_with_retry(
                    [self._system_message(), self._aligned_message([group], len(group))],
                    client=self.client,
                )
            except ProviderError as exc:
                if is_fatal(exc) or isinstance(exc, PlanLimitError):
                    raise
                log.error("Falha ao traduzir alinhado o bloco %s: %s", block.id, exc)
                results.append("")
                continue
            usage = usage + completion.usage
            segments = _parse_segments(completion.text, len(group))
            if segments is not None:
                targets = [_clean_aligned(s) for s in segments]
                aligned = Aligned(list(zip(group, targets)))
                if all(targets) and not looks_like_refusal(aligned, block.text):
                    results.append(aligned)
                    continue
            log.warning("Bloco %s não alinhou; traduzindo o parágrafo inteiro", block.id)
            whole, usage = await self._translate_individually([block], base_usage=usage)
            results.append(whole[0])
        return results, usage

    async def translate_blocks(
        self,
        blocks: Sequence[Block],
        *,
        on_progress: ProgressCallback | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> Progress:
        """Traduz uma lista de blocos, gravando o resultado em block.translated.

        Lotes rodam em paralelo até o limite de concorrência. O progresso é
        reportado conforme cada lote termina, não no fim de tudo.

        Um erro que vale para o livro inteiro (chave, permissão, modelo) ou uma
        sequência de lotes falhando interrompe tudo: os lotes na fila não são
        enviados e o ProviderError sobe para quem chamou. O que já foi traduzido
        fica em block.translated.

        Quando o limite do plano da assinatura acaba (PlanLimitError), todos os
        lotes esperam a janela renovar e continuam; current_note diz até quando.

        Um trecho que o modelo se recusou a traduzir fica sem tradução, com a
        resposta em block.meta["refused"].
        """
        pending = [b for b in blocks if b.text.strip()]
        batches = build_batches(pending, max_chars=self.settings.batch_chars)

        progress = Progress(total=len(pending))
        semaphore = asyncio.Semaphore(max(1, self.settings.concurrency))
        lock = asyncio.Lock()
        abort: list[ProviderError] = []
        consecutive_failures = 0

        # Mapa para localizar vizinhos de cada lote e montar o contexto.
        position = {id(block): i for i, block in enumerate(pending)}
        window = self.settings.context_blocks

        def halted() -> bool:
            return bool(abort) or bool(should_stop and should_stop())

        # Limite do plano da assinatura: todos os lotes esperam até a mesma hora.
        paused_until = 0.0

        async def pause_for_plan(exc: PlanLimitError) -> None:
            nonlocal paused_until
            async with lock:
                resume = (
                    exc.resets_at + PLAN_LIMIT_MARGIN
                    if exc.resets_at
                    else time.time() + PLAN_LIMIT_WAIT
                )
                paused_until = max(paused_until, resume)
                at = time.strftime("%H:%M", time.localtime(paused_until))
                progress.current_note = f"Limite do plano atingido. Continua às {at}."
                await _notify(on_progress, progress)

        async def wait_while_paused() -> None:
            while time.time() < paused_until and not halted():
                await asyncio.sleep(min(5.0, max(0.0, paused_until - time.time())))

        async def run_batch(batch: list[Block]) -> None:
            nonlocal consecutive_failures
            if halted():
                return

            start = position[id(batch[0])]
            end = position[id(batch[-1])]
            before = pending[max(0, start - window) : start]
            after = pending[end + 1 : end + 1 + window]

            async with semaphore:
                while True:
                    await wait_while_paused()
                    if halted():
                        return
                    try:
                        translate = (
                            self.translate_batch_aligned
                            if self.settings.aligned
                            else self.translate_batch
                        )
                        segments, usage = await translate(batch, before=before, after=after)
                        break
                    except PlanLimitError as exc:
                        # Não é falha do lote: espera a janela renovar e refaz.
                        log.info("Limite do plano: %s", exc)
                        await pause_for_plan(exc)
                    except ProviderError as exc:
                        log.error("Lote falhou definitivamente: %s", exc)
                        async with lock:
                            progress.failed += len(batch)
                            progress.current_note = f"Erro: {exc}"
                            consecutive_failures += 1
                            if not abort and (
                                is_fatal(exc) or consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                            ):
                                abort.append(exc)
                            await _notify(on_progress, progress)
                        return

            async with lock:
                for block, translation in zip(batch, segments):
                    if isinstance(translation, Refusal):
                        block.meta["refused"] = str(translation)
                        progress.refused += 1
                    elif translation:
                        block.translated = str(translation)
                        block.meta.pop("refused", None)
                        _record_alignment(block, translation, aligned=self.settings.aligned)
                        progress.done += 1
                    else:
                        progress.failed += 1
                progress.usage = progress.usage + usage
                progress.current_note = ""
                # Um modelo que recusa tudo gastaria o plano inteiro recusando.
                if all(isinstance(t, Refusal) for t in segments):
                    consecutive_failures += 1
                    if not abort and consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        abort.append(ProviderError("O modelo recusou traduzir o livro."))
                else:
                    consecutive_failures = 0
                await _notify(on_progress, progress)

        await asyncio.gather(*(run_batch(batch) for batch in batches))

        if abort:
            exc = abort[0]
            if is_fatal(exc):
                raise exc
            raise ProviderError(
                f"{MAX_CONSECUTIVE_FAILURES} lotes seguidos falharam. Último erro: {exc}",
                status=exc.status,
            ) from exc
        return progress


def _record_alignment(block: Block, translation: str, *, aligned: bool) -> None:
    """Guarda (ou tira) as frases da tradução nova em block.meta.

    - frase a frase e alinhou: meta["sentences"] com os pares;
    - frase a frase e não alinhou: meta["align_failed"], sem frases;
    - formato normal: nada (as frases de uma tradução anterior não valem mais).
    """
    block.meta.pop("sentences", None)
    block.meta.pop("align_failed", None)
    if isinstance(translation, Aligned):
        block.meta["sentences"] = [{"src": src, "tgt": tgt} for src, tgt in translation.pairs]
    elif aligned:
        block.meta["align_failed"] = True


def _clean_single(text: str) -> str:
    """Limpa respostas individuais que vierem com enfeites."""
    cleaned = _MARKER_RE.sub("", text).strip()
    # Modelos às vezes embrulham a resposta inteira em aspas.
    if len(cleaned) > 2 and cleaned[0] in '"“' and cleaned[-1] in '"”':
        cleaned = cleaned[1:-1].strip()
    return cleaned


async def _notify(callback: ProgressCallback | None, progress: Progress) -> None:
    if callback is None:
        return
    result = callback(progress)
    if asyncio.iscoroutine(result):
        await result
