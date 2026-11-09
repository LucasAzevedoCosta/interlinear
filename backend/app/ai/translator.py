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
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import httpx

from ..core.blocks import Block
from .base import Message, Provider, ProviderError, Usage

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


@dataclass
class Progress:
    done: int = 0
    total: int = 0
    failed: int = 0
    usage: Usage = field(default_factory=Usage)
    current_note: str = ""


ProgressCallback = Callable[[Progress], Awaitable[None] | None]

# Respostas que valem para o livro inteiro: chave inválida, sem permissão,
# modelo inexistente. Insistir só repete o mesmo erro em cada lote.
FATAL_STATUSES = frozenset({401, 403, 404})

# Lotes seguidos falhando por qualquer motivo (servidor fora do ar, 400 por
# parâmetro inválido...) também indicam problema de configuração, não de texto.
MAX_CONSECUTIVE_FAILURES = 5


def is_fatal(exc: ProviderError) -> bool:
    return not exc.retryable and exc.status in FATAL_STATUSES


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
        """Traduz um lote, com verificação de formato e plano B individual."""
        if not batch:
            return [], Usage()

        user_content = (
            f"Traduza os {len(batch)} segmentos abaixo. Devolva cada um precedido "
            "do seu marcador ⟦n⟧, na mesma ordem, sem nenhum texto adicional."
            f"{_context_text(before, after)}\n\n"
            f"Segmentos a traduzir:\n\n{_format_segments(batch)}"
        )

        messages = [self._system_message(), Message(role="user", content=user_content)]

        completion = await self.provider.complete_with_retry(
            messages, client=self.client
        )
        segments = _parse_segments(completion.text, len(batch))

        if segments is not None:
            return segments, completion.usage

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
                        "sem comentários, sem aspas ao redor e sem marcadores.\n\n"
                        + block.text
                    ),
                ),
            ]
            try:
                completion = await self.provider.complete_with_retry(
                    messages, client=self.client
                )
                usage = usage + completion.usage
                results.append(_clean_single(completion.text))
            except ProviderError as exc:
                if is_fatal(exc):
                    raise
                log.error("Falha ao traduzir bloco %s: %s", block.id, exc)
                results.append("")

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

        async def run_batch(batch: list[Block]) -> None:
            nonlocal consecutive_failures
            if halted():
                return

            start = position[id(batch[0])]
            end = position[id(batch[-1])]
            before = pending[max(0, start - window) : start]
            after = pending[end + 1 : end + 1 + window]

            async with semaphore:
                if halted():
                    return
                try:
                    segments, usage = await self.translate_batch(
                        batch, before=before, after=after
                    )
                except ProviderError as exc:
                    log.error("Lote falhou definitivamente: %s", exc)
                    async with lock:
                        progress.failed += len(batch)
                        progress.current_note = f"Erro: {exc}"
                        consecutive_failures += 1
                        if not abort and (
                            is_fatal(exc)
                            or consecutive_failures >= MAX_CONSECUTIVE_FAILURES
                        ):
                            abort.append(exc)
                        await _notify(on_progress, progress)
                    return

            async with lock:
                for block, translation in zip(batch, segments):
                    if translation:
                        block.translated = translation
                        progress.done += 1
                    else:
                        progress.failed += 1
                consecutive_failures = 0
                progress.usage = progress.usage + usage
                progress.current_note = ""
                await _notify(on_progress, progress)

        await asyncio.gather(*(run_batch(batch) for batch in batches))

        if abort:
            exc = abort[0]
            if is_fatal(exc):
                raise exc
            raise ProviderError(
                f"{MAX_CONSECUTIVE_FAILURES} lotes seguidos falharam. "
                f"Último erro: {exc}",
                status=exc.status,
            ) from exc
        return progress


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
