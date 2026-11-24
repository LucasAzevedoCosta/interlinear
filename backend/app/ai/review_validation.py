"""Validação das propostas do revisor: o que o modelo sugeriu é seguro de aplicar?

O revisor (ai/reviewer.py) já confere a forma de cada resposta: operação da
lista, tipo permitido, trecho que existe no lote. Aqui se confere o
conteúdo contra o livro:

  * **Possível.** Juntar só blocos de texto vizinhos do mesmo tipo, como a
    rota merge-next; começar capítulo só no meio de um; fundir capítulo só
    depois do primeiro.
  * **Seguro.** "Corrigir o texto" que muda mais que um erro de extração
    mudaria é alucinação, ou o modelo reescrevendo o autor. Apagar um bloco
    longo apagaria texto do livro, não um cabeçalho que escapou.
  * **Sem duplicata.** "O B continua o A" e "o A continua no B" são a mesma
    junção; aplicar as duas juntaria três blocos.

Serve duas vezes: no fim da revisão, e de novo na hora de aplicar (Fase 4),
porque o livro pode ter mudado entre uma e outra.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..core.blocks import TRANSLATABLE, Block, Chapter, Document
from ..core.review import looks_continued

if TYPE_CHECKING:  # o revisor importa este módulo; aqui só a anotação
    from .reviewer import Proposal

# Quanto um fix_text pode mudar: 5% dos caracteres, com um piso para blocos
# curtos (em "lsso" → "Isso", 1 de 4 já é 25%).
FIX_MAX_SHARE = 0.05
FIX_MIN_CHANGES = 4

# Cabeçalho, rodapé e número de página são curtos. Acima disto, "apagar"
# perderia texto do livro. O mesmo limite da regra de texto repetido.
DELETE_MAX_WORDS = 12

_TAG = re.compile(r"</?[a-zA-Z][^>]*>")
_TEXT = re.compile(r"<[^>]+>")


@dataclass
class Rejection:
    proposal: Proposal
    # Para o log e para a documentação; o usuário não vê o que foi descartado.
    reason: str


def validate(
    document: Document, proposals: Sequence[Proposal]
) -> tuple[list[Proposal], list[Rejection]]:
    """Separa as propostas aplicáveis das que precisam ser descartadas.

    A ordem das válidas é a recebida.
    """
    where = _positions(document)
    valid: list[Proposal] = []
    rejected: list[Rejection] = []
    merges: set[tuple[str, str]] = set()

    for proposal in proposals:
        problem = _check(proposal, where)
        if problem is None and proposal.op in ("merge_next", "merge_prev"):
            pair = _merge_pair(proposal, where)
            if pair in merges:
                problem = "mesma junção já proposta pelo outro bloco"
            merges.add(pair)
        if problem is None:
            valid.append(proposal)
        else:
            rejected.append(Rejection(proposal, problem))
    return valid, rejected


def _positions(document: Document) -> dict[str, tuple[int, Chapter, int]]:
    """Id do bloco → (índice do capítulo, capítulo, índice no capítulo)."""
    return {
        block.id: (c, chapter, i)
        for c, chapter in enumerate(document.chapters)
        for i, block in enumerate(chapter.blocks)
    }


def _check(proposal: Proposal, where: dict[str, tuple[int, Chapter, int]]) -> str | None:
    """O problema da proposta, ou None se ela pode ser aplicada."""
    if proposal.block_id not in where:
        return "o bloco não existe mais"
    chapter_index, chapter, index = where[proposal.block_id]
    if chapter.id != proposal.chapter_id:
        return "o bloco mudou de capítulo"
    block = chapter.blocks[index]
    op = proposal.op

    if op == "ok":
        return None
    if op == "merge_next":
        if index + 1 >= len(chapter.blocks):
            return "não há bloco seguinte no capítulo"
        return _mergeable(block, chapter.blocks[index + 1])
    if op == "merge_prev":
        if index == 0:
            return "não há bloco anterior no capítulo"
        return _mergeable(chapter.blocks[index - 1], block)
    if op == "set_kind":
        if block.kind not in TRANSLATABLE or block.kind == "table":
            return f"um bloco do tipo {block.kind} não troca de tipo"
        if proposal.payload.get("kind") == block.kind:
            return "o bloco já é desse tipo"
        return None
    if op == "delete":
        if block.kind in ("image", "page_break"):
            return "a revisão não apaga imagem nem quebra"
        if len(_TEXT.sub("", block.text).split()) > DELETE_MAX_WORDS:
            return f"apagar mais de {DELETE_MAX_WORDS} palavras perderia texto do livro"
        return None
    if op == "fix_text":
        return _check_fix(block, proposal.payload.get("text", ""))
    if op == "start_chapter":
        return "o bloco já abre o capítulo" if index == 0 else None
    if op == "merge_chapter":
        return "o primeiro capítulo não tem anterior" if chapter_index == 0 else None
    return f"operação desconhecida: {op}"


def _mergeable(first: Block, second: Block) -> str | None:
    """A regra da rota merge-next (dois blocos de texto do mesmo tipo), mais o texto.

    O texto tem de parecer partido. Sem isso, um "merge_next" trocado por
    "merge_prev" (o Haiku fez isso no teste real, com o motivo certo e a
    operação errada) colaria o parágrafo partido no parágrafo completo de
    antes dele.
    """
    if first.kind not in TRANSLATABLE or first.kind != second.kind:
        return f"não junta {first.kind} com {second.kind}"
    if first.kind != "table" and not looks_continued(first.text, second.text):
        return "o primeiro termina a frase e o segundo começa com maiúscula"
    return None


def _merge_pair(proposal: Proposal, where: dict[str, tuple[int, Chapter, int]]) -> tuple[str, str]:
    _, chapter, index = where[proposal.block_id]
    if proposal.op == "merge_next":
        return chapter.blocks[index].id, chapter.blocks[index + 1].id
    return chapter.blocks[index - 1].id, chapter.blocks[index].id


def _check_fix(block: Block, text: str) -> str | None:
    if block.kind not in TRANSLATABLE:
        return f"um bloco do tipo {block.kind} não tem texto para corrigir"
    if not text or text == block.text:
        return "a correção não muda nada"
    if Counter(_TAG.findall(text)) != Counter(_TAG.findall(block.text)):
        return "a correção mexe nas marcações (<em>, <strong>, <sup>)"
    changed = changed_chars(block.text, text)
    allowed = max(FIX_MIN_CHANGES, int(len(block.text) * FIX_MAX_SHARE))
    if changed > allowed:
        return f"a correção muda {changed} caracteres; um erro de extração muda até {allowed}"
    return None


def changed_chars(before: str, after: str) -> int:
    """Quantos caracteres mudaram: o lado maior de cada trecho diferente.

    "exem- plo" → "exemplo" dá 2; trocar uma palavra de 6 letras por outra de
    8 dá 8. Não é a distância de edição exata, mas é próxima e roda em tempo
    razoável num parágrafo de milhares de caracteres.
    """
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    return sum(
        max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in matcher.get_opcodes() if tag != "equal"
    )
