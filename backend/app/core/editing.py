"""Edições de estrutura e de texto do documento, sem HTTP.

As rotas do editor e o aceite das sugestões da revisão por IA usam as mesmas
funções: aceitar "juntar estes parágrafos" tem de fazer exatamente o que o
botão "Juntar com o próximo" faz, inclusive com a tradução.

Cada função altera o Document recebido e levanta EditError quando a edição
não é possível. Quem chama decide como gravar (ProjectStore.update).
"""

from __future__ import annotations

from .blocks import TRANSLATABLE, Block, Chapter, Document, Vocabulary, join_hyphenated
from .sentences import is_aligned


class EditError(Exception):
    """A edição não pode ser feita. A mensagem é para o usuário."""

    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(EditError):
    status = 404


def locate_block(document: Document, block_id: str) -> tuple[Chapter, int]:
    for chapter in document.chapters:
        for index, block in enumerate(chapter.blocks):
            if block.id == block_id:
                return chapter, index
    raise NotFound("Bloco não encontrado")


def find_chapter(document: Document, chapter_id: str) -> Chapter:
    chapter = next((c for c in document.chapters if c.id == chapter_id), None)
    if chapter is None:
        raise NotFound("Capítulo não encontrado")
    return chapter


def set_text(block: Block, text: str) -> None:
    """Troca o original. Uma tradução feita sobre o texto antigo fica desatualizada."""
    if text != block.text and block.translated:
        block.meta["stale"] = True
    block.text = text


def set_kind(block: Block, kind: str) -> None:
    # Só entre tipos de texto: virar imagem pediria um arquivo.
    if kind not in TRANSLATABLE or block.kind not in TRANSLATABLE:
        raise EditError(f"Tipo de bloco inválido: {kind}")
    block.kind = kind  # type: ignore[assignment]


def delete_block(document: Document, block_id: str) -> tuple[Chapter, Block, int]:
    """Apaga o bloco. Devolve o capítulo, o bloco e a posição, para desfazer."""
    chapter, index = locate_block(document, block_id)
    return chapter, chapter.blocks.pop(index), index


def merge_with_next(document: Document, block_id: str) -> tuple[Chapter, Block]:
    """Junta um bloco com o seguinte do mesmo capítulo. Devolve o bloco novo.

    Conserta o parágrafo que a extração partiu em dois (por uma figura, uma
    nota de rodapé, uma quebra de página mal detectada).
    """
    chapter, index = locate_block(document, block_id)
    if index + 1 >= len(chapter.blocks):
        raise EditError("Não há bloco seguinte neste capítulo")
    first, second = chapter.blocks[index], chapter.blocks[index + 1]
    if first.kind not in TRANSLATABLE or first.kind != second.kind:
        # Título com parágrafo não é um parágrafo partido; é estrutura.
        raise EditError("Só blocos de texto do mesmo tipo podem ser juntados")

    # A tradução só sobrevive se as duas metades estavam traduzidas; meia
    # tradução colada no texto inteiro passaria por pronta na revisão.
    vocabulary = Vocabulary.from_texts(
        text for block in document.all_blocks() for text in (block.text, block.translated or "")
    )

    def join(a: str, b: str) -> str:
        # Tabela partida na quebra de página: as linhas da segunda continuam
        # a primeira, sem mexer em hífen.
        if first.kind == "table":
            return f"{a.rstrip()}\n{b.lstrip()}"
        return join_hyphenated(a, b, vocabulary)

    translated = None
    if first.translated and second.translated:
        translated = join(first.translated, second.translated)

    merged = Block(
        kind=first.kind,
        text=join(first.text, second.text),
        translated=translated,
        page=first.page,
        meta={
            **second.meta,
            **first.meta,
            "edited": bool(translated)
            and bool(first.meta.get("edited") or second.meta.get("edited")),
        },
    )
    merged.meta.pop("stale", None)
    if translated and (first.meta.get("stale") or second.meta.get("stale")):
        merged.meta["stale"] = True
    # Frase a frase: as frases das duas metades, se as duas estavam
    # alinhadas; senão o bloco junto não está alinhado.
    merged.meta.pop("sentences", None)
    merged.meta.pop("align_failed", None)
    if translated and all(
        is_aligned(b.text, b.translated, b.meta.get("sentences")) for b in (first, second)
    ):
        merged.meta["sentences"] = list(first.meta["sentences"]) + list(second.meta["sentences"])
    # Onde o bloco está no PDF: as duas metades, para o painel Original
    # destacar o parágrafo inteiro (inclusive o que atravessa a página).
    boxes = list(first.meta.get("boxes") or []) + list(second.meta.get("boxes") or [])
    if boxes:
        merged.meta["boxes"] = boxes
    # Id novo de propósito: uma tradução em andamento ainda tem a tradução
    # de cada metade pendente pelo id antigo e não deve gravá-la aqui.
    chapter.blocks[index : index + 2] = [merged]
    return chapter, merged


def insert_paragraph_after(document: Document, block_id: str, text: str) -> Block:
    """Um parágrafo novo depois do bloco, na mesma página. Devolve o bloco.

    Para texto que a extração perdeu. meta["inserted"] é a mesma marca do
    "Inserir abaixo" do editor.
    """
    chapter, index = locate_block(document, block_id)
    block = Block(
        kind="paragraph", text=text, page=chapter.blocks[index].page, meta={"inserted": True}
    )
    chapter.blocks.insert(index + 1, block)
    return block


def split_chapter(document: Document, chapter_id: str, block_id: str) -> Chapter:
    """Divide o capítulo em dois a partir de um bloco. Devolve o capítulo novo."""
    chapter = find_chapter(document, chapter_id)
    position = next((i for i, b in enumerate(chapter.blocks) if b.id == block_id), None)
    if position is None or position == 0:
        raise EditError("Bloco inválido para divisão")

    tail = chapter.blocks[position:]
    chapter.blocks = chapter.blocks[:position]

    new_title = next((b.text for b in tail if b.kind.startswith("heading") and b.text), "")
    new_chapter = Chapter(title=new_title or "Novo capítulo", blocks=tail)
    document.chapters.insert(document.chapters.index(chapter) + 1, new_chapter)
    return new_chapter


def merge_chapter(document: Document, chapter_id: str) -> Chapter:
    """Funde o capítulo no anterior. Devolve o anterior."""
    chapter = find_chapter(document, chapter_id)
    position = document.chapters.index(chapter)
    if position == 0:
        raise EditError("Não há capítulo anterior para fundir")
    previous = document.chapters[position - 1]
    previous.blocks.extend(chapter.blocks)
    document.chapters.pop(position)
    return previous
