"""Validação das propostas do revisor (ai/review_validation.py)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.ai.review_validation import changed_chars, validate  # noqa: E402
from app.ai.reviewer import Proposal  # noqa: E402
from app.core.blocks import Block, Chapter, Document  # noqa: E402

LONG = "Texto " + " ".join(["corrido"] * 60) + "."


@pytest.fixture
def book():
    blocks = {
        "a": Block("paragraph", "O parágrafo partido no meio da"),
        "b": Block("paragraph", "frase pela quebra."),
        "num": Block("paragraph", "23"),
        "img": Block("image", src="f.png"),
        "titulo": Block("heading2", "Um título"),
        "longo": Block("paragraph", LONG),
        "ital": Block("paragraph", "Ele leu <em>Dom Casmurro</em> duas vezes no verao."),
        "tabela": Block("table", "a | b\nc | d"),
    }
    first = Chapter("C1", list(blocks.values()))
    second = Chapter("C2", [Block("heading1", "Capítulo 2"), Block("paragraph", LONG)])
    return Document(chapters=[first, second]), blocks, first, second


def proposal(chapter: Chapter, block: Block, op: str, **payload) -> Proposal:
    return Proposal(chapter.id, block.id, op, 0.9, "", payload=payload)


def problems(document, *proposals) -> list[str | None]:
    """O motivo da rejeição de cada proposta, ou None se passou."""
    valid, rejected = validate(document, proposals)
    reasons = {id(r.proposal): r.reason for r in rejected}
    assert len(valid) + len(rejected) == len(proposals)
    return [reasons.get(id(p)) for p in proposals]


# -- junções ----------------------------------------------------------------


def test_juncao_entre_paragrafos_vizinhos(book):
    document, b, c1, _ = book
    assert problems(document, proposal(c1, b["a"], "merge_next")) == [None]
    assert problems(document, proposal(c1, b["b"], "merge_prev")) == [None]


def test_mesma_juncao_pelos_dois_lados_vale_uma_vez(book):
    document, b, c1, _ = book
    first, second = proposal(c1, b["a"], "merge_next"), proposal(c1, b["b"], "merge_prev")
    result = problems(document, first, second)
    assert result[0] is None and "mesma junção" in result[1]


def test_juncao_com_tipo_diferente_ou_fora_do_capitulo(book):
    document, b, c1, c2 = book
    result = problems(
        document,
        proposal(c1, b["num"], "merge_next"),  # o seguinte é imagem
        proposal(c1, b["titulo"], "merge_prev"),  # o anterior é imagem
        proposal(c1, b["a"], "merge_prev"),  # primeiro do capítulo
        proposal(c1, b["tabela"], "merge_next"),  # último do capítulo
    )
    assert "não junta paragraph com image" in result[0]
    assert "não junta image com heading2" in result[1]
    assert "anterior" in result[2] and "seguinte" in result[3]


def test_juncao_de_dois_paragrafos_completos_e_descartada(book):
    # O caso do teste real: o modelo responde merge_prev no lugar de
    # merge_next, e o anterior é um parágrafo completo.
    document, b, c1, _ = book
    complete = Block("paragraph", "Um parágrafo completo, com ponto final.")
    c1.blocks.insert(0, complete)
    result = problems(document, proposal(c1, b["a"], "merge_prev"))
    assert "termina a frase" in result[0]


def test_juncao_vale_com_fim_aberto_ou_minuscula(book):
    document, _, c1, _ = book
    pairs = [
        ("Termina com vírgula,", "Mas segue com maiúscula."),
        ("Termina com ponto.", "e segue com minúscula."),
        ("Ele disse:", "— Vamos."),  # dois-pontos fecha: não é partido
    ]
    proposals = []
    for first, second in pairs:
        a, z = Block("paragraph", first), Block("paragraph", second)
        c1.blocks += [Block("heading3", "Separador"), a, z]
        proposals.append(proposal(c1, a, "merge_next"))
    result = problems(document, *proposals)
    assert result[:2] == [None, None]
    assert "termina a frase" in result[2]


# -- tipo, apagar, capítulos ------------------------------------------------


def test_trocar_tipo(book):
    document, b, c1, _ = book
    result = problems(
        document,
        proposal(c1, b["titulo"], "set_kind", kind="paragraph"),
        proposal(c1, b["img"], "set_kind", kind="paragraph"),
        proposal(c1, b["tabela"], "set_kind", kind="paragraph"),
    )
    assert result[0] is None
    assert "image" in result[1] and "table" in result[2]


def test_apagar_so_o_que_e_curto(book):
    document, b, c1, _ = book
    result = problems(
        document,
        proposal(c1, b["num"], "delete"),
        proposal(c1, b["longo"], "delete"),
        proposal(c1, b["img"], "delete"),
    )
    assert result[0] is None
    assert "perderia texto" in result[1] and "imagem" in result[2]


def test_capitulos(book):
    document, b, c1, c2 = book
    result = problems(
        document,
        proposal(c1, b["titulo"], "start_chapter"),
        proposal(c1, b["a"], "start_chapter"),  # já abre o capítulo
        proposal(c2, c2.blocks[0], "merge_chapter"),
        proposal(c1, b["a"], "merge_chapter"),  # primeiro capítulo
        proposal(c1, b["num"], "ok"),
    )
    assert result[0] is None and "já abre" in result[1]
    assert result[2] is None and "primeiro capítulo" in result[3]
    assert result[4] is None


def test_bloco_que_sumiu_ou_mudou_de_capitulo(book):
    document, b, c1, c2 = book
    gone = Proposal(c1.id, "naoexiste", "ok", 0.9, "")
    moved = proposal(c2, b["num"], "delete")  # o bloco está no C1
    result = problems(document, gone, moved)
    assert "não existe" in result[0] and "mudou de capítulo" in result[1]


# -- fix_text ---------------------------------------------------------------


def test_correcao_pequena_passa(book):
    document, b, c1, _ = book
    fixes = [
        proposal(c1, b["ital"], "fix_text", text=b["ital"].text.replace("verao", "verão")),
        proposal(c1, b["a"], "fix_text", text="O parágrafo partido no meio dá"),
    ]
    assert problems(document, *fixes) == [None, None]


def test_bloco_curto_tem_piso(book):
    document, _, c1, _ = book
    block = Block("paragraph", "lsso.")
    c1.blocks.append(block)
    assert problems(document, proposal(c1, block, "fix_text", text="Isso.")) == [None]


def test_correcao_que_reescreve_e_descartada(book):
    document, b, c1, _ = book
    rewritten = "Ele releu <em>Dom Casmurro</em> algumas vezes durante as férias."
    result = problems(document, proposal(c1, b["ital"], "fix_text", text=rewritten))
    assert "muda" in result[0]


def test_correcao_que_perde_ou_poe_marcacao_e_descartada(book):
    document, b, c1, _ = book
    lost = b["ital"].text.replace("<em>", "").replace("</em>", "")
    added = b["a"].text.replace("partido", "<strong>partido</strong>")
    result = problems(
        document,
        proposal(c1, b["ital"], "fix_text", text=lost),
        proposal(c1, b["a"], "fix_text", text=added),
    )
    assert all("marcações" in r for r in result)


def test_correcao_em_tabela_ou_imagem(book):
    document, b, c1, _ = book
    result = problems(document, proposal(c1, b["img"], "fix_text", text="algo"))
    assert "não tem texto" in result[0]


def test_changed_chars():
    assert changed_chars("exem- plo", "exemplo") == 2
    assert changed_chars("lsso", "Isso") == 1
    assert changed_chars("igual", "igual") == 0
    assert changed_chars("o gato", "o cachorro") >= 5
