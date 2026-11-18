"""Recusas do modelo: não entram no livro como tradução."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai.base import Completion, Provider, ProviderConfig, ProviderError, Usage  # noqa: E402
from app.ai.translator import (  # noqa: E402
    MAX_CONSECUTIVE_FAILURES,
    TranslationSettings,
    Translator,
    looks_like_refusal,
)
from app.core.blocks import Block  # noqa: E402

REFUSAL = (
    "I can't translate this text: it appears to be a copyrighted work, and "
    "reproducing it in full would not be appropriate."
)


@pytest.mark.parametrize(
    "reply",
    [
        REFUSAL,
        "I’m sorry, but I can’t help reproduce copyrighted material.",
        "Desculpe, mas não posso traduzir este texto, pois ele é protegido por direitos autorais.",
        "Não posso fornecer a tradução completa de uma obra protegida.",
        "Lo siento, no puedo traducir este texto protegido por derechos de autor.",
    ],
)
def test_reconhece_recusa(reply):
    assert looks_like_refusal(reply, "It was a dark and stormy night.")


@pytest.mark.parametrize(
    ("reply", "source"),
    [
        # Diálogo que começa como recusa, e o original também.
        ("— Não posso traduzir isso — disse ela.", "“I can't translate that,” she said."),
        # Recusa sem o assunto: é só a fala de um personagem.
        ("Não posso ficar aqui esta noite.", "I can't stay here tonight."),
        ("Desculpe, cheguei tarde.", "Sorry I'm late."),
        # O assunto sem a recusa: um livro sobre tradução.
        ("A tradução literária exige escolhas.", "Literary translation requires choices."),
        ("Copyright © 2020. Todos os direitos reservados.", "Copyright © 2020. All rights reserved."),
    ],
)
def test_traducao_normal_nao_e_recusa(reply, source):
    assert not looks_like_refusal(reply, source)


class _Refuser(Provider):
    """Traduz tudo, menos o que contém uma das palavras de `refuse`.

    Lote com palavra recusada: a recusa vem inteira, sem marcadores. Com
    `inline`, vem só no segmento, com o formato certo.
    """

    def __init__(self, refuse: set[str], *, inline: bool = False, once: bool = False):
        super().__init__(ProviderConfig(kind="fake"))
        self.refuse = refuse
        self.inline = inline
        self.once = once  # recusa só no lote; sozinho, traduz
        self.calls = 0

    def _refused(self, text: str) -> bool:
        return any(word in text for word in self.refuse)

    async def complete(self, messages, *, client) -> Completion:
        self.calls += 1
        content = messages[-1].content
        if "Segmentos a traduzir:" not in content:
            text = content.split("\n\n", 1)[1]
            reply = REFUSAL if self._refused(text) and not self.once else f"[pt] {text}"
            return Completion(text=reply, usage=Usage(1, 1))
        segments = content.split("Segmentos a traduzir:\n\n", 1)[1].split("\n\n")
        texts = [segment.split("\n", 1)[1] for segment in segments]
        if not self.inline and any(self._refused(t) for t in texts):
            return Completion(text=REFUSAL, usage=Usage(1, 1))
        reply = "\n\n".join(
            f"⟦{n}⟧\n{REFUSAL if self._refused(t) else f'[pt] {t}'}"
            for n, t in enumerate(texts, start=1)
        )
        return Completion(text=reply, usage=Usage(1, 1))

    async def list_models(self, *, client) -> list[str]:
        return []


def _translate(provider: Provider, blocks: list[Block], **settings):
    async def run():
        async with Translator(provider, TranslationSettings(**settings)) as translator:
            return await translator.translate_blocks(blocks)

    return asyncio.run(run())


def _blocks(*texts: str) -> list[Block]:
    return [Block(kind="paragraph", text=t) for t in texts]


def test_lote_recusado_traduz_o_resto_e_guarda_a_recusa():
    blocks = _blocks("Hello.", "Secret passage.", "Bye.")

    progress = _translate(_Refuser({"Secret"}), blocks)

    assert [b.translated for b in blocks] == ["[pt] Hello.", None, "[pt] Bye."]
    assert blocks[1].meta["refused"] == REFUSAL
    assert (progress.done, progress.refused, progress.failed) == (2, 1, 0)


def test_segmento_recusado_no_lote_tenta_de_novo_sozinho():
    blocks = _blocks("Hello.", "Secret passage.")
    provider = _Refuser({"Secret"}, inline=True, once=True)

    progress = _translate(provider, blocks)

    assert [b.translated for b in blocks] == ["[pt] Hello.", "[pt] Secret passage."]
    assert "refused" not in blocks[1].meta
    assert (progress.done, progress.refused) == (2, 0)
    assert provider.calls == 2  # o lote, e o segmento recusado sozinho


def test_modelo_que_recusa_tudo_interrompe():
    blocks = _blocks(*(f"Secret {n}." for n in range(12)))
    provider = _Refuser({"Secret"})

    with pytest.raises(ProviderError, match="recusou"):
        _translate(provider, blocks, batch_chars=1, concurrency=1)

    # Cada lote: o pedido em lote e o plano B individual.
    assert provider.calls == 2 * MAX_CONSECUTIVE_FAILURES
    assert all(b.translated is None for b in blocks)
