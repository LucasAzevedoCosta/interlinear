"""Revisor IA da conversão (ai/reviewer.py), com um provedor falso."""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai.base import (  # noqa: E402
    Completion,
    Message,
    PlanLimitError,
    Provider,
    ProviderConfig,
    ProviderError,
    Usage,
)
from app.ai.reviewer import Reviewer, build_cases, format_case, parse_reply  # noqa: E402
from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.core.review import find_suspects  # noqa: E402

LONG = "Texto " + " ".join(["corrido"] * 200) + "."


class Scripted(Provider):
    """Responde com `answer(textos dos trechos)`; guarda os pedidos."""

    def __init__(self, answer):
        super().__init__(ProviderConfig(kind="fake"))
        self.answer = answer
        self.requests: list[list[Message]] = []

    async def complete(self, messages, *, client) -> Completion:
        self.requests.append(messages)
        content = messages[-1].content
        targets = re.findall(r"^TRECHO \([^)]*\): (.*)$", content, re.MULTILINE)
        result = self.answer(targets)
        if isinstance(result, Exception):
            raise result
        return Completion(text=result, usage=Usage(10, 5))

    async def list_models(self, *, client):
        return []


def book() -> tuple[Document, dict[str, Block]]:
    blocks = {
        "partido": Block("paragraph", "O parágrafo foi partido no meio da", page=1),
        "resto": Block("paragraph", "frase pela quebra de página.", page=2),
        "numero": Block("paragraph", "7", page=2),
    }
    document = Document(
        chapters=[
            Chapter(
                "C1",
                [Block("paragraph", LONG, page=1), *blocks.values(), Block("paragraph", LONG)],
            )
        ]
    )
    return document, blocks


def review(provider: Provider, document: Document, **kwargs):
    async def go():
        async with Reviewer(provider, **kwargs) as reviewer:
            return await reviewer.review(document, find_suspects(document))

    return asyncio.run(go())


def reply_for(targets: list[str]) -> str:
    """Um modelo que acerta: junta o partido, apaga o número."""
    items = []
    for n, text in enumerate(targets, start=1):
        if text == "7":
            items.append({"n": n, "op": "delete", "confidence": 0.95, "reason": "número"})
        elif text.startswith("frase"):
            items.append({"n": n, "op": "merge_prev", "confidence": 0.9, "reason": "continua"})
        else:
            items.append({"n": n, "op": "merge_next", "confidence": 0.9, "reason": "aberto"})
    return json.dumps(items, ensure_ascii=False)


# -- montagem ---------------------------------------------------------------


def test_casos_trazem_vizinhos_e_motivos():
    document, blocks = book()
    cases = build_cases(document, find_suspects(document))
    assert [c.block.id for c in cases] == [b.id for b in blocks.values()]
    resto = cases[1]
    assert resto.reasons == ["lowercase_start"]
    assert resto.before is blocks["partido"] and resto.after is blocks["numero"]

    text = format_case(2, resto)
    assert text.startswith("⟦2⟧")
    assert "começa com minúscula" in text
    assert "TRECHO (parágrafo, página 2): frase pela quebra de página." in text


def test_pedido_leva_titulo_do_livro_e_evidencia():
    # O teste real mostrou o modelo tomando o cabeçalho corrente por título de
    # capítulo quando só sabia "repetido em várias páginas".
    heads = [Block("paragraph", "A CASA DO RIO", page=n) for n in (3, 5, 7)]
    blocks = [Block("paragraph", LONG, page=1)]
    for head in heads:
        blocks += [head, Block("paragraph", LONG, page=head.page)]
    document = Document(title="A casa do rio", chapters=[Chapter("C", blocks)])
    provider = Scripted(lambda targets: "[]")
    review(provider, document)
    content = provider.requests[0][-1].content
    assert content.startswith("Livro: A casa do rio\n")
    assert "Motivo: texto repetido em várias páginas (repetido nas páginas 3, 5 e 7)" in content


def test_contexto_corta_vizinhos_longos():
    document, _ = book()
    first = build_cases(document, find_suspects(document))[0]
    text = format_case(1, first)
    # O anterior aparece pelo fim, cortado.
    assert "Bloco anterior (parágrafo, página 1): …" in text
    assert len(text) < len(LONG)


# -- leitura da resposta ----------------------------------------------------


def test_le_json_com_cerca_e_texto_em_volta():
    document, _ = book()
    cases = build_cases(document, find_suspects(document))
    reply = "Claro!\n```json\n" + reply_for([c.block.text for c in cases]) + "\n```"
    proposals = parse_reply(reply, cases)
    assert [p.op for p in proposals] == ["merge_next", "merge_prev", "delete"]
    assert proposals[2].confidence == 0.95 and proposals[2].suspects == ["tiny_block"]


def test_itens_invalidos_viram_none_sem_derrubar_o_lote():
    document, _ = book()
    cases = build_cases(document, find_suspects(document))
    reply = json.dumps(
        [
            {"n": 1, "op": "rewrite_everything"},  # operação inventada
            {"n": 2, "op": "set_kind", "kind": "image"},  # tipo não permitido
            {"n": 3, "op": "fix_text", "text": "7"},  # "corrige" para o mesmo texto
            {"n": 9, "op": "ok"},  # trecho que não existe
            {"n": True, "op": "ok"},
        ]
    )
    assert parse_reply(reply, cases) == [None, None, None]


def test_primeira_resposta_de_um_trecho_vale_e_confianca_e_limitada():
    document, _ = book()
    cases = build_cases(document, find_suspects(document))[:1]
    reply = json.dumps(
        [
            {"n": 1, "op": "set_kind", "kind": "heading2", "confidence": 7},
            {"n": 1, "op": "delete"},
        ]
    )
    (proposal,) = parse_reply(reply, cases)
    assert proposal.op == "set_kind" and proposal.payload == {"kind": "heading2"}
    assert proposal.confidence == 1.0


def test_resposta_sem_json_devolve_none():
    document, _ = book()
    cases = build_cases(document, find_suspects(document))
    assert parse_reply("Acho que está tudo certo.", cases) is None
    assert parse_reply("[não é json]", cases) is None


# -- revisor ----------------------------------------------------------------


def test_revisa_o_livro_e_nao_altera_o_documento():
    document, blocks = book()
    before = document.to_dict()
    provider = Scripted(reply_for)
    result = review(provider, document)

    assert document.to_dict() == before
    assert len(provider.requests) == 1
    assert (result.total, result.reviewed, result.failed) == (3, 3, 0)
    assert [(p.block_id, p.op) for p in result.proposals] == [
        (blocks["partido"].id, "merge_next"),
        (blocks["numero"].id, "delete"),
    ]
    # As duas metades propuseram a mesma junção; a validação deixa uma.
    assert [(r.proposal.block_id, r.proposal.op) for r in result.rejected] == [
        (blocks["resto"].id, "merge_prev")
    ]
    assert result.usage.input_tokens == 10


def test_lote_fora_do_formato_refaz_um_a_um():
    document, _ = book()

    def answer(targets):
        return "não sei responder em JSON" if len(targets) > 1 else reply_for(targets)

    provider = Scripted(answer)
    result = review(provider, document)
    assert len(provider.requests) == 4  # o lote + um pedido por trecho
    assert result.reviewed == 3
    assert [p.op for p in result.proposals] == ["merge_next", "delete"]


def test_lotes_respeitam_o_orcamento():
    document, _ = book()
    provider = Scripted(reply_for)
    result = review(provider, document, batch_chars=100)
    assert len(provider.requests) == 3
    assert result.reviewed == 3


def test_recusa_nao_vira_proposta():
    document, _ = book()
    refusal = "Desculpe, não posso reproduzir este texto protegido por direitos autorais."
    result = review(Scripted(lambda targets: refusal), document)
    assert result.proposals == [] and result.failed == 3


def test_erro_fatal_interrompe():
    document, _ = book()
    with pytest.raises(ProviderError, match="chave"):
        review(Scripted(lambda t: ProviderError("chave inválida", status=401)), document)


def test_limite_do_plano_interrompe_em_vez_de_esperar():
    document, _ = book()
    with pytest.raises(PlanLimitError):
        review(Scripted(lambda t: PlanLimitError("limite")), document)


def test_lotes_seguidos_sem_resposta_util_interrompem():
    blocks = [Block("paragraph", LONG, page=1)]
    for n in range(8):
        blocks += [Block("paragraph", f"Partido {n} no meio da"), Block("paragraph", LONG)]
    document = Document(chapters=[Chapter("C", blocks)])
    provider = Scripted(lambda targets: "[]")
    with pytest.raises(ProviderError, match="lotes seguidos"):
        review(provider, document, batch_chars=100, concurrency=1)
    assert len(provider.requests) == 5


def test_sem_suspeitos_nao_chama_o_modelo():
    document = Document(chapters=[Chapter("C", [Block("paragraph", LONG)])])
    provider = Scripted(reply_for)
    result = review(provider, document)
    assert provider.requests == [] and result.total == 0
