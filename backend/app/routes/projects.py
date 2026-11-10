"""Rotas de projeto: importação, leitura, edição, preview e exportação."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Callable, TypeVar

from fastapi import APIRouter, Body, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, HTMLResponse, Response

from ..core.blocks import (
    TRANSLATABLE,
    Block,
    Chapter,
    Document,
    Vocabulary,
    join_hyphenated,
)
from ..core.epub_write import DEFAULT_CSS, chapter_to_xhtml, write_epub
from ..core.importer import ImportOptions, import_file
from ..core.storage import ProjectStore
from ..deps import get_store
from ..jobs import registry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects", tags=["projects"])

MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB

T = TypeVar("T")


# --------------------------------------------------------------------------
# Importação
# --------------------------------------------------------------------------


@router.post("")
async def create_project(
    file: UploadFile = File(...),
    split_on: str = Form("heading1"),
    title: str = Form(""),
    ocr_mode: str = Form("auto"),
    ocr_languages: str = Form(""),
) -> dict[str, Any]:
    """Importa um PDF ou EPUB de uma vez e cria um projeto editável.

    A interface usa POST /api/imports, que faz o mesmo em segundo plano e
    mostra o progresso. Esta rota fica para scripts.
    """
    data, suffix, filename = await read_upload(file)
    options = import_options(title, split_on, ocr_mode, ocr_languages)

    # Extrair um PDF leva de segundos a minutos de CPU. Fora do loop de eventos,
    # para que traduções em andamento e o resto da interface continuem respondendo.
    try:
        document, summary = await asyncio.to_thread(
            import_file, data, suffix, filename, options
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - arquivos de terceiros
        log.exception("Falha ao importar %s", filename)
        raise HTTPException(500, f"Não foi possível ler o arquivo: {exc}") from exc

    meta = await asyncio.to_thread(
        get_store().create,
        document,
        source_kind=suffix.lstrip("."),
        source_filename=filename,
        source_bytes=data,
        import_summary=summary,
    )
    return {"project": meta.to_dict()}


async def read_upload(file: UploadFile) -> tuple[bytes, str, str]:
    """Lê o arquivo enviado e confere tipo e tamanho. Devolve (bytes, sufixo, nome)."""
    filename = Path(file.filename or "arquivo").name
    suffix = Path(filename).suffix.lower()

    if suffix not in (".pdf", ".epub"):
        raise HTTPException(400, "Envie um arquivo .pdf ou .epub")

    data = await file.read()
    if not data:
        raise HTTPException(400, "Arquivo vazio")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Arquivo acima do limite de 200 MB")
    return data, suffix, filename


_LANGUAGE_SPEC = re.compile(r"^[A-Za-z_]+(\+[A-Za-z_]+)*$")


def import_options(
    title: str, split_on: str, ocr_mode: str, ocr_languages: str
) -> ImportOptions:
    options = ImportOptions(
        title=title.strip(),
        split_on=split_on,
        ocr_mode=ocr_mode,
        ocr_languages=ocr_languages.strip() or None,
    )
    # Vai para a linha de comando do Tesseract: só letras, "_" e "+".
    if options.ocr_languages and not _LANGUAGE_SPEC.match(options.ocr_languages):
        raise HTTPException(400, "Idiomas do OCR inválidos")
    try:
        options.validate()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return options


# --------------------------------------------------------------------------
# Leitura
# --------------------------------------------------------------------------


@router.get("")
async def list_projects() -> dict[str, Any]:
    projects = await asyncio.to_thread(get_store().list_projects)
    return {"projects": [m.to_dict() for m in projects]}


@router.get("/{project_id}")
async def get_project(project_id: str) -> dict[str, Any]:
    store = get_store()
    document = await _load(store, project_id)
    meta = store.get_meta(project_id)
    return {
        "project": meta.to_dict() if meta else None,
        "document": document.to_dict(),
    }


@router.get("/{project_id}/chapters/{chapter_id}")
async def get_chapter(project_id: str, chapter_id: str) -> dict[str, Any]:
    document = await _load(get_store(), project_id)
    chapter = _find_chapter(document, chapter_id)
    return {"chapter": chapter.to_dict()}


# --------------------------------------------------------------------------
# Edição
# --------------------------------------------------------------------------


@router.patch("/{project_id}/blocks")
async def update_blocks(
    project_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Aplica alterações a um ou mais blocos.

    Formato esperado:
        {"updates": [{"id": "...", "text": "...", "translated": "...",
                      "kind": "paragraph"}]}

    Campos ausentes ficam como estão, o que permite salvar só o que mudou.

    Mudar o original de um bloco já traduzido marca a tradução como
    desatualizada (meta.stale); a próxima tradução refaz o bloco. Gravar uma
    tradução nova, à mão, desfaz a marca.
    """
    store = get_store()

    updates = payload.get("updates") or []
    if not isinstance(updates, list):
        raise HTTPException(400, "Campo 'updates' deve ser uma lista")

    def change(document: Document) -> int:
        index = document.block_index()
        changed = 0
        for update in updates:
            if not isinstance(update, dict):
                continue
            block = index.get(str(update.get("id", "")))
            if block is None:
                continue

            if "text" in update:
                text = str(update["text"])
                if text != block.text and block.translated:
                    block.meta["stale"] = True
                block.text = text
            if "translated" in update:
                value = update["translated"]
                block.translated = None if value is None else str(value)
                block.meta.pop("stale", None)
            if "kind" in update and update["kind"]:
                kind = str(update["kind"])
                # Só entre tipos de texto: virar imagem pediria um arquivo.
                if kind not in TRANSLATABLE or block.kind not in TRANSLATABLE:
                    raise HTTPException(400, f"Tipo de bloco inválido: {kind}")
                block.kind = kind  # type: ignore[assignment]
            if isinstance(update.get("meta"), dict):
                # Mesclado, não substituído: o editor manda só o que mudou.
                block.meta = {**block.meta, **update["meta"]}
                if not block.meta.get("stale"):
                    block.meta.pop("stale", None)
            changed += 1
        return changed

    changed = await _update(store, project_id, change)
    meta = store.get_meta(project_id)
    return {"updated": changed, "project": meta.to_dict() if meta else None}


@router.patch("/{project_id}/chapters")
async def update_chapter_titles(
    project_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Renomeia capítulos. Espera {"chapters": [{"id": "...", "title": "..."}]}."""
    def change(document: Document) -> int:
        by_id = {c.id: c for c in document.chapters}
        changed = 0
        for item in payload.get("chapters") or []:
            if not isinstance(item, dict):
                continue
            chapter = by_id.get(str(item.get("id", "")))
            if chapter is not None and "title" in item:
                chapter.title = str(item["title"])
                changed += 1
        return changed

    return {"updated": await _update(get_store(), project_id, change)}


@router.patch("/{project_id}")
async def update_metadata(
    project_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Atualiza título, autor e idiomas do livro."""
    store = get_store()

    def change(document: Document) -> None:
        for field_name in ("title", "author", "language", "source_language"):
            if field_name in payload and payload[field_name] is not None:
                setattr(document, field_name, str(payload[field_name]))

    await _update(store, project_id, change)
    meta = store.get_meta(project_id)
    return {"project": meta.to_dict() if meta else None}


@router.post("/{project_id}/chapters/{chapter_id}/split")
async def split_chapter(
    project_id: str, chapter_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Divide um capítulo em dois a partir de um bloco.

    Útil quando a detecção automática juntou dois capítulos do livro.
    """
    block_id = str(payload.get("block_id", ""))

    def change(document: Document) -> dict[str, Any]:
        chapter = _find_chapter(document, chapter_id)
        position = next(
            (i for i, b in enumerate(chapter.blocks) if b.id == block_id), None
        )
        if position is None or position == 0:
            raise HTTPException(400, "Bloco inválido para divisão")

        tail = chapter.blocks[position:]
        chapter.blocks = chapter.blocks[:position]

        new_title = next(
            (b.text for b in tail if b.kind.startswith("heading") and b.text), ""
        )
        new_chapter = Chapter(title=new_title or "Novo capítulo", blocks=tail)

        chapter_position = document.chapters.index(chapter)
        document.chapters.insert(chapter_position + 1, new_chapter)
        return document.to_dict()

    return {"document": await _update(get_store(), project_id, change)}


@router.post("/{project_id}/chapters/{chapter_id}/merge")
async def merge_chapter(project_id: str, chapter_id: str) -> dict[str, Any]:
    """Funde um capítulo com o anterior."""
    def change(document: Document) -> dict[str, Any]:
        chapter = _find_chapter(document, chapter_id)
        position = document.chapters.index(chapter)
        if position == 0:
            raise HTTPException(400, "Não há capítulo anterior para fundir")

        document.chapters[position - 1].blocks.extend(chapter.blocks)
        document.chapters.pop(position)
        return document.to_dict()

    return {"document": await _update(get_store(), project_id, change)}


# Blocos: os consertos que a extração mais pede. Cada rota devolve o capítulo
# inteiro para a interface substituir o que tem, sem recarregar o livro.


@router.delete("/{project_id}/blocks/{block_id}")
async def delete_block(project_id: str, block_id: str) -> dict[str, Any]:
    """Apaga um bloco. Devolve o bloco e a posição, para a interface desfazer."""

    def change(document: Document) -> dict[str, Any]:
        chapter, index = _locate_block(document, block_id)
        removed = chapter.blocks.pop(index)
        return {
            "chapter": chapter.to_dict(),
            "removed": {"block": removed.to_dict(), "index": index},
        }

    return await _update_with_meta(get_store(), project_id, change)


@router.post("/{project_id}/blocks/{block_id}/merge-next")
async def merge_block_with_next(project_id: str, block_id: str) -> dict[str, Any]:
    """Junta um bloco com o seguinte do mesmo capítulo.

    Conserta o parágrafo que a extração partiu em dois (por uma figura, uma
    nota de rodapé, uma quebra de página mal detectada).
    """

    def change(document: Document) -> dict[str, Any]:
        chapter, index = _locate_block(document, block_id)
        if index + 1 >= len(chapter.blocks):
            raise HTTPException(400, "Não há bloco seguinte neste capítulo")
        first, second = chapter.blocks[index], chapter.blocks[index + 1]
        if first.kind not in TRANSLATABLE or first.kind != second.kind:
            # Título com parágrafo não é um parágrafo partido; é estrutura.
            raise HTTPException(400, "Só blocos de texto do mesmo tipo podem ser juntados")

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
        # Id novo de propósito: uma tradução em andamento ainda tem a tradução
        # de cada metade pendente pelo id antigo e não deve gravá-la aqui.
        chapter.blocks[index : index + 2] = [merged]
        return {"chapter": chapter.to_dict(), "block": merged.to_dict()}

    return await _update_with_meta(get_store(), project_id, change)


@router.post("/{project_id}/chapters/{chapter_id}/blocks")
async def insert_block(
    project_id: str, chapter_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Insere um bloco na posição `index` do capítulo.

    Sem `block`, cria um parágrafo vazio para o usuário escrever o original. Com
    `block` (o que delete_block devolveu), restaura um bloco apagado, mesmo id.
    """
    try:
        index = int(payload.get("index", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "Posição inválida") from None
    data = payload.get("block") or {}
    if not isinstance(data, dict):
        raise HTTPException(400, "Campo 'block' deve ser um objeto")

    def change(document: Document) -> dict[str, Any]:
        chapter = _find_chapter(document, chapter_id)
        if data:
            block = Block.from_dict(data)
            if block.id in document.block_index():
                raise HTTPException(409, "Já existe um bloco com esse id")
        else:
            block = Block(kind="paragraph", meta={"inserted": True})
        position = max(0, min(index, len(chapter.blocks)))
        chapter.blocks.insert(position, block)
        return {"chapter": chapter.to_dict(), "block": block.to_dict()}

    return await _update_with_meta(get_store(), project_id, change)


@router.delete("/{project_id}")
async def delete_project(project_id: str) -> dict[str, Any]:
    # Uma tradução em andamento continuaria pagando por lotes que não têm mais
    # onde ser gravados.
    job = registry.for_project(project_id)
    if job is not None and job.status == "running":
        job.cancel()

    if not await asyncio.to_thread(get_store().delete, project_id):
        raise HTTPException(404, "Projeto não encontrado")
    return {"deleted": True}


# --------------------------------------------------------------------------
# Preview e exportação
# --------------------------------------------------------------------------


@router.get("/{project_id}/preview/{chapter_id}", response_class=HTMLResponse)
async def preview_chapter(
    project_id: str,
    chapter_id: str,
    translated: bool = Query(True, description="Mostrar a tradução ou o original"),
) -> HTMLResponse:
    """Renderiza um capítulo exatamente como ele sairá no EPUB.

    O preview usa o mesmo gerador de XHTML da exportação, então o que se vê
    aqui é o que vai para o arquivo final — não uma aproximação.
    """
    document = await _load(get_store(), project_id)
    chapter = _find_chapter(document, chapter_id)

    language = document.language if translated else document.source_language
    xhtml = chapter_to_xhtml(
        chapter,
        language=language,
        use_translation=translated,
        css_href=f"/api/projects/{project_id}/preview.css",
    )
    # Imagens do preview apontam para a rota de imagens do projeto.
    xhtml = xhtml.replace('src="../images/', f'src="/api/projects/{project_id}/images/')
    return HTMLResponse(xhtml)


@router.get("/{project_id}/preview.css")
async def preview_css(project_id: str) -> Response:
    return Response(DEFAULT_CSS, media_type="text/css")


@router.get("/{project_id}/images/{name}")
async def project_image(project_id: str, name: str) -> FileResponse:
    store = get_store()
    path = store.images_dir(project_id) / Path(name).name
    if not path.exists():
        raise HTTPException(404, "Imagem não encontrada")
    return FileResponse(path)


@router.get("/{project_id}/export")
async def export_epub(
    project_id: str,
    translated: bool = Query(True),
) -> FileResponse:
    """Gera e devolve o EPUB final."""
    store = get_store()
    document = await _load(store, project_id, with_images=True)

    suffix = "traduzido" if translated else "original"
    safe_title = "".join(
        c for c in document.title if c.isalnum() or c in " -_"
    ).strip() or "livro"
    output = store.project_dir(project_id) / f"{safe_title} ({suffix}).epub"

    await asyncio.to_thread(write_epub, document, output, use_translation=translated)

    return FileResponse(
        output,
        media_type="application/epub+zip",
        filename=output.name,
    )


# --------------------------------------------------------------------------


# Ler e gravar o JSON de um livro grande leva tempo; as duas funções rodam numa
# thread para não segurar o loop de eventos.


async def _load(
    store: ProjectStore, project_id: str, *, with_images: bool = False
) -> Document:
    try:
        return await asyncio.to_thread(
            store.load_document, project_id, with_images=with_images
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Projeto não encontrado") from exc


async def _update(
    store: ProjectStore, project_id: str, change: Callable[[Document], T]
) -> T:
    """store.update com o 404 das rotas. Um HTTPException em `change` cancela a gravação."""
    try:
        return await asyncio.to_thread(store.update, project_id, change)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Projeto não encontrado") from exc


async def _update_with_meta(
    store: ProjectStore,
    project_id: str,
    change: Callable[[Document], dict[str, Any]],
) -> dict[str, Any]:
    """_update que acrescenta os metadados (contagem traduzida) à resposta."""
    result = await _update(store, project_id, change)
    meta = store.get_meta(project_id)
    result["project"] = meta.to_dict() if meta else None
    return result


def _locate_block(document: Document, block_id: str) -> tuple[Chapter, int]:
    for chapter in document.chapters:
        for index, block in enumerate(chapter.blocks):
            if block.id == block_id:
                return chapter, index
    raise HTTPException(404, "Bloco não encontrado")


def _find_chapter(document: Document, chapter_id: str) -> Chapter:
    chapter = next((c for c in document.chapters if c.id == chapter_id), None)
    if chapter is None:
        raise HTTPException(404, "Capítulo não encontrado")
    return chapter
