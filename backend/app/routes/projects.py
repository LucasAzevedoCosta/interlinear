"""Rotas de projeto: importação, leitura, edição, preview e exportação."""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Callable, TypeVar

from fastapi import APIRouter, Body, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import FileResponse, HTMLResponse, Response

from ..ai.review_apply import open_suspects
from ..core import editing
from ..core import styles as book_styles
from ..core.blocks import Block, Chapter, Document, new_id
from ..core.epub_write import LAYOUTS, book_css, chapter_to_xhtml, write_epub
from ..core.importer import ImportOptions, import_file
from ..core.page_image import page_sizes, render_page
from ..core.review import find_suspects
from ..core.storage import ProjectStore
from ..deps import get_store
from ..jobs import registry, review_registry

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
    purpose: str = Form(""),
    source_language: str = Form(""),
    target_language: str = Form(""),
    provider_id: str = Form(""),
) -> dict[str, Any]:
    """Importa um PDF ou EPUB de uma vez e cria um projeto editável.

    A interface usa POST /api/imports, que faz o mesmo em segundo plano e
    mostra o progresso. Esta rota fica para scripts.
    """
    data, suffix, filename = await read_upload(file)
    options = import_options(
        suffix,
        title=title,
        split_on=split_on,
        ocr_mode=ocr_mode,
        ocr_languages=ocr_languages,
        purpose=purpose,
        source_language=source_language,
        target_language=target_language,
        provider_id=provider_id,
    )

    # Extrair um PDF leva de segundos a minutos de CPU. Fora do loop de eventos,
    # para que traduções em andamento e o resto da interface continuem respondendo.
    try:
        document, summary = await asyncio.to_thread(import_file, data, suffix, filename, options)
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
        purpose=options.purpose,
        translation=options.translation(),
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
    suffix: str,
    *,
    title: str,
    split_on: str,
    ocr_mode: str,
    ocr_languages: str,
    purpose: str,
    source_language: str,
    target_language: str,
    provider_id: str,
) -> ImportOptions:
    """Os campos do formulário de importação, conferidos. Erros viram 400.

    Sem `purpose`, um PDF é importado para conversão e um EPUB para tradução,
    o único objetivo que um EPUB aceita.
    """
    options = ImportOptions(
        title=title.strip(),
        split_on=split_on,
        ocr_mode=ocr_mode,
        ocr_languages=ocr_languages.strip() or None,
        purpose=purpose or ("convert" if suffix == ".pdf" else "translate"),
        source_language=source_language.strip(),
        target_language=target_language.strip(),
        provider_id=provider_id.strip(),
    )
    # Vai para a linha de comando do Tesseract: só letras, "_" e "+".
    if options.ocr_languages and not _LANGUAGE_SPEC.match(options.ocr_languages):
        raise HTTPException(400, "Idiomas do OCR inválidos")
    try:
        options.validate(suffix)
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


@router.get("/{project_id}/suspects")
async def list_suspects(project_id: str) -> dict[str, Any]:
    """Os blocos que a conversão provavelmente errou (core/review.py).

    Sem os que o usuário confirmou como certos na revisão por IA, enquanto o
    texto deles não mudar.
    """
    store = get_store()
    document = await _load(store, project_id)
    review = await asyncio.to_thread(store.load_review, project_id)
    suspects = await asyncio.to_thread(find_suspects, document)
    return {"suspects": [s.to_dict() for s in open_suspects(document, review, suspects)]}


# --------------------------------------------------------------------------
# Edição
# --------------------------------------------------------------------------


@router.patch("/{project_id}/blocks")
async def update_blocks(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Aplica alterações a um ou mais blocos.

    Formato esperado:
        {"updates": [{"id": "...", "text": "...", "translated": "...",
                      "kind": "paragraph", "style": "quote",
                      "overrides": {"space_after": 1.5}}]}

    Campos ausentes ficam como estão, o que permite salvar só o que mudou.

    Mudar o original de um bloco já traduzido marca a tradução como
    desatualizada (meta.stale); a próxima tradução refaz o bloco. Gravar uma
    tradução nova, à mão, desfaz a marca, e também a recusa do modelo
    (meta.refused).
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
                editing.set_text(block, str(update["text"]))
            if "translated" in update:
                value = update["translated"]
                block.translated = None if value is None else str(value)
                block.meta.pop("stale", None)
                block.meta.pop("refused", None)
            if "kind" in update and update["kind"]:
                editing.set_kind(block, str(update["kind"]))
            # Estilo e ajuste do bloco têm campo próprio e são validados
            # (core/styles.py): viram CSS no EPUB.
            if "style" in update:
                book_styles.set_block_style(document, block, update["style"])
            if "overrides" in update:
                book_styles.set_block_overrides(block, update["overrides"])
            if isinstance(update.get("meta"), dict):
                # Mesclado, não substituído: o editor manda só o que mudou.
                # Estilo e ajuste não entram por aqui, sem validação.
                extra = {k: v for k, v in update["meta"].items() if k not in ("style", "overrides")}
                block.meta = {**block.meta, **extra}
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
async def update_metadata(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
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
        editing.split_chapter(document, chapter_id, block_id)
        return document.to_dict()

    return {"document": await _update(get_store(), project_id, change)}


@router.post("/{project_id}/chapters/{chapter_id}/merge")
async def merge_chapter(project_id: str, chapter_id: str) -> dict[str, Any]:
    """Funde um capítulo com o anterior."""

    def change(document: Document) -> dict[str, Any]:
        editing.merge_chapter(document, chapter_id)
        return document.to_dict()

    return {"document": await _update(get_store(), project_id, change)}


# Blocos: os consertos que a extração mais pede. Cada rota devolve o capítulo
# inteiro para a interface substituir o que tem, sem recarregar o livro.


@router.delete("/{project_id}/blocks/{block_id}")
async def delete_block(project_id: str, block_id: str) -> dict[str, Any]:
    """Apaga um bloco. Devolve o bloco e a posição, para a interface desfazer."""

    def change(document: Document) -> dict[str, Any]:
        chapter, removed, index = editing.delete_block(document, block_id)
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
        chapter, merged = editing.merge_with_next(document, block_id)
        return {"chapter": chapter.to_dict(), "block": merged.to_dict()}

    return await _update_with_meta(get_store(), project_id, change)


# O que o usuário pode inserir entre os blocos, e o texto com que nasce.
INSERTABLE_KINDS = {"paragraph": "", "scene_break": "* * *", "spacer": "", "page_break": ""}


@router.post("/{project_id}/chapters/{chapter_id}/blocks")
async def insert_block(
    project_id: str, chapter_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Insere um bloco na posição `index` do capítulo.

    Sem `block`, cria um bloco novo do tipo `kind`: um parágrafo vazio para o
    usuário escrever o original (o padrão), um separador de cena, uma linha
    em branco ou uma quebra de página. Com `block` (o que delete_block
    devolveu), restaura um bloco apagado, mesmo id.
    """
    try:
        index = int(payload.get("index", 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "Posição inválida") from None
    data = payload.get("block") or {}
    if not isinstance(data, dict):
        raise HTTPException(400, "Campo 'block' deve ser um objeto")
    kind = payload.get("kind") or "paragraph"
    if kind not in INSERTABLE_KINDS:
        raise HTTPException(400, f"Tipo de bloco que não se insere: {kind}")

    def change(document: Document) -> dict[str, Any]:
        chapter = editing.find_chapter(document, chapter_id)
        if data:
            block = Block.from_dict(data)
            if block.id in document.block_index():
                raise HTTPException(409, "Já existe um bloco com esse id")
        else:
            block = Block(kind=kind, text=INSERTABLE_KINDS[kind], meta={"inserted": True})
        position = max(0, min(index, len(chapter.blocks)))
        chapter.blocks.insert(position, block)
        return {"chapter": chapter.to_dict(), "block": block.to_dict()}

    return await _update_with_meta(get_store(), project_id, change)


@router.delete("/{project_id}")
async def delete_project(project_id: str) -> dict[str, Any]:
    # Uma tradução ou revisão em andamento continuaria pagando por lotes que
    # não têm mais onde ser gravados.
    for jobs in (registry, review_registry):
        job = jobs.for_project(project_id)
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
    layout: str | None = Query(None, description="original, translated ou paragraph"),
) -> HTMLResponse:
    """Renderiza um capítulo exatamente como ele sairá no EPUB.

    O preview usa o mesmo gerador de XHTML da exportação, então o que se vê
    aqui é o que vai para o arquivo final — não uma aproximação.
    """
    document = await _load(get_store(), project_id)
    chapter = _find_chapter(document, chapter_id)

    chosen = _layout_param(layout, translated)
    language = document.source_language if chosen == "original" else document.language
    xhtml = chapter_to_xhtml(
        chapter,
        language=language,
        css_href=f"/api/projects/{project_id}/preview.css",
        styled=document if book_styles.uses_styles(document) else None,
        layout=chosen,
        source_language=document.source_language,
    )
    # Imagens do preview apontam para a rota de imagens do projeto.
    xhtml = xhtml.replace('src="../images/', f'src="/api/projects/{project_id}/images/')
    return HTMLResponse(xhtml)


@router.get("/{project_id}/preview.css")
async def preview_css(project_id: str) -> Response:
    """O mesmo style.css da exportação, com os estilos de parágrafo do livro."""
    document = await _load(get_store(), project_id)
    # Muda a cada edição de estilo: o navegador não pode guardar.
    return Response(
        book_css(document), media_type="text/css", headers={"Cache-Control": "no-store"}
    )


# --------------------------------------------------------------------------
# Estilos de parágrafo (core/styles.py)
# --------------------------------------------------------------------------


@router.get("/{project_id}/styles")
async def list_styles(project_id: str) -> dict[str, Any]:
    """Os estilos do livro com os valores finais, e a descrição das propriedades."""
    document = await _load(get_store(), project_id)
    return {"styles": book_styles.describe(document), "schema": book_styles.schema()}


@router.post("/{project_id}/styles")
async def create_style(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Um estilo novo a partir de um pronto: {"name": "Epígrafe", "based_on": "quote"}."""

    def change(document: Document) -> dict[str, Any]:
        style_id = book_styles.create_style(
            document, payload.get("name"), payload.get("based_on"), new_id()
        )
        if "props" in payload:
            book_styles.update_style(document, style_id, payload["props"])
        return {"id": style_id, "styles": book_styles.describe(document)}

    return await _update(get_store(), project_id, change)


@router.patch("/{project_id}/styles/{style_id}")
async def update_style(
    project_id: str, style_id: str, payload: dict[str, Any] = Body(...)
) -> dict[str, Any]:
    """Muda propriedades ({"props": {"indent": 2}}; null apaga) e, nos do usuário, o nome."""

    def change(document: Document) -> dict[str, Any]:
        book_styles.update_style(document, style_id, payload.get("props"), payload.get("name"))
        return {"styles": book_styles.describe(document)}

    return await _update(get_store(), project_id, change)


@router.delete("/{project_id}/styles/{style_id}")
async def delete_style(project_id: str, style_id: str) -> dict[str, Any]:
    """Estilo pronto volta ao padrão; o do usuário some e os blocos dele voltam ao automático."""

    def change(document: Document) -> dict[str, Any]:
        moved = book_styles.delete_style(document, style_id)
        return {"moved": moved, "styles": book_styles.describe(document)}

    return await _update(get_store(), project_id, change)


@router.get("/{project_id}/images/{name}")
async def project_image(project_id: str, name: str) -> FileResponse:
    store = get_store()
    path = store.images_dir(project_id) / Path(name).name
    if not path.exists():
        raise HTTPException(404, "Imagem não encontrada")
    return FileResponse(path)


# --------------------------------------------------------------------------
# O arquivo original (painel Original · PDF da bancada)
# --------------------------------------------------------------------------

# Resoluções aceitas para a imagem da página. Poucas e fixas: cada uma vira um
# arquivo no cache, e um valor livre deixaria qualquer cliente encher o disco.
PAGE_DPI = (72, 110, 150, 200)


@router.get("/{project_id}/source")
async def project_source(project_id: str) -> dict[str, Any]:
    """O tipo do arquivo original e, se for PDF, o tamanho de cada página.

    `pages` vem em pontos, [largura, altura], na mesma medida de
    meta["boxes"] dos blocos. Lista vazia quando o original é um EPUB ou o
    PDF não está mais na pasta do projeto.
    """
    store = get_store()
    meta = store.get_meta(project_id)
    if meta is None:
        raise HTTPException(404, "Projeto não encontrado")
    pdf = store.source_pdf(project_id)
    if pdf is None:
        return {"kind": meta.source_kind, "pages": []}
    sizes = await asyncio.to_thread(page_sizes, pdf)
    return {"kind": "pdf", "pages": [list(size) for size in sizes]}


@router.get("/{project_id}/source/pages/{page}")
async def project_source_page(project_id: str, page: int, dpi: int = Query(110)) -> FileResponse:
    """A página `page` do PDF original em PNG colorido, guardada em cache."""
    if dpi not in PAGE_DPI:
        raise HTTPException(400, f"Resolução inválida; use uma destas: {PAGE_DPI}")
    store = get_store()
    pdf = store.source_pdf(project_id)
    if pdf is None:
        raise HTTPException(404, "Este projeto não tem o PDF original")

    cache = store.project_dir(project_id) / "cache" / "pages" / f"{page}-{dpi}.png"
    if not cache.exists():
        try:
            png = await asyncio.to_thread(render_page, pdf, page, dpi=dpi, grayscale=False)
        except ValueError as error:
            raise HTTPException(404, str(error)) from None
        cache.parent.mkdir(parents=True, exist_ok=True)
        # Grava ao lado e troca: dois pedidos da mesma página ao mesmo tempo
        # não deixam um PNG pela metade.
        temporary = cache.with_suffix(f".{id(png)}.tmp")
        temporary.write_bytes(png)
        temporary.replace(cache)
    # O PDF original não muda: o navegador pode guardar a imagem.
    return FileResponse(
        cache, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"}
    )


# Como cada formato aparece no nome do arquivo exportado.
_LAYOUT_SUFFIX = {"original": "original", "translated": "traduzido", "paragraph": "bilíngue"}


def _layout_param(layout: str | None, translated: bool) -> str:
    """O formato pedido: `layout`, ou o `translated` de antes (só os dois de sempre)."""
    if layout is None:
        return "translated" if translated else "original"
    if layout not in LAYOUTS:
        raise HTTPException(400, f"Formato desconhecido: {layout}")
    return layout


@router.get("/{project_id}/export")
async def export_epub(
    project_id: str,
    translated: bool = Query(True),
    layout: str | None = Query(None, description="original, translated ou paragraph"),
) -> FileResponse:
    """Gera e devolve o EPUB final, no formato pedido."""
    chosen = _layout_param(layout, translated)
    store = get_store()
    document = await _load(store, project_id, with_images=True)

    suffix = _LAYOUT_SUFFIX[chosen]
    safe_title = "".join(c for c in document.title if c.isalnum() or c in " -_").strip() or "livro"
    output = store.project_dir(project_id) / f"{safe_title} ({suffix}).epub"

    await asyncio.to_thread(write_epub, document, output, layout=chosen)

    return FileResponse(
        output,
        media_type="application/epub+zip",
        filename=output.name,
    )


# --------------------------------------------------------------------------


# Ler e gravar o JSON de um livro grande leva tempo; as duas funções rodam numa
# thread para não segurar o loop de eventos.


async def _load(store: ProjectStore, project_id: str, *, with_images: bool = False) -> Document:
    try:
        return await asyncio.to_thread(store.load_document, project_id, with_images=with_images)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Projeto não encontrado") from exc


async def _update(store: ProjectStore, project_id: str, change: Callable[[Document], T]) -> T:
    """store.update com o 404 das rotas. Um HTTPException em `change` cancela a gravação."""
    try:
        return await asyncio.to_thread(store.update, project_id, change)
    except editing.EditError as exc:
        raise HTTPException(exc.status, exc.message) from exc
    except book_styles.StyleError as exc:
        # Antes do ValueError: StyleError é um, e não é "projeto não encontrado".
        raise HTTPException(400, str(exc)) from exc
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


def _find_chapter(document: Document, chapter_id: str) -> Chapter:
    """Para as rotas de leitura, fora do _update que converte o EditError."""
    try:
        return editing.find_chapter(document, chapter_id)
    except editing.NotFound as exc:
        raise HTTPException(404, exc.message) from exc
