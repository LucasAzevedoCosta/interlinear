"""Leitura de arquivos EPUB existentes para dentro da estrutura interna.

Abrir um EPUB que já existe é o caminho para editar ou traduzir um livro que
não veio de um PDF. Lemos o manifesto para descobrir a ordem de leitura e
convertemos cada XHTML de volta em blocos.
"""

from __future__ import annotations

import logging
import posixpath
import zipfile
from pathlib import Path

from lxml import etree, html as lxml_html

from .blocks import Block, Chapter, Document, normalize_spaces

log = logging.getLogger(__name__)

OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}

# Tags inline preservadas ao converter XHTML de volta para texto de bloco.
_KEEP_INLINE = {"em", "strong", "i", "b", "sup", "sub", "small", "br", "a"}

_BLOCK_MAP = {
    "h1": "heading1",
    "h2": "heading2",
    "h3": "heading3",
    "h4": "heading3",
    "h5": "heading3",
    "h6": "heading3",
    "p": "paragraph",
    "blockquote": "quote",
    "li": "list_item",
}


def _find_opf_path(archive: zipfile.ZipFile) -> str:
    try:
        container = etree.fromstring(archive.read("META-INF/container.xml"))
    except KeyError as exc:
        raise ValueError("EPUB inválido: META-INF/container.xml não encontrado") from exc

    rootfile = container.find(f".//{{{CONTAINER_NS}}}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise ValueError("EPUB inválido: o container não aponta para um .opf")
    return rootfile.get("full-path")


def _text_of(element) -> str:
    """Serializa o conteúdo de um elemento preservando só as tags inline."""
    parts: list[str] = []

    if element.text:
        parts.append(element.text)

    for child in element:
        tag = etree.QName(child).localname.lower() if child.tag is not etree.Comment else ""

        if tag in _KEEP_INLINE:
            inner = _text_of(child)
            if tag == "br":
                parts.append("<br/>")
            elif tag == "a":
                href = child.get("href", "")
                if href and inner:
                    parts.append(f'<a href="{href}">{inner}</a>')
                else:
                    parts.append(inner)
            elif inner:
                normalized = {"i": "em", "b": "strong"}.get(tag, tag)
                parts.append(f"<{normalized}>{inner}</{normalized}>")
        else:
            parts.append(_text_of(child))

        if child.tail:
            parts.append(child.tail)

    return normalize_spaces("".join(parts))


def _blocks_from_xhtml(content: bytes, image_names: set[str]) -> list[Block]:
    """Converte um documento XHTML numa lista de blocos."""
    try:
        tree = lxml_html.fromstring(content)
    except Exception as exc:  # noqa: BLE001 - XHTML de terceiros é imprevisível
        log.warning("Falha ao ler um capítulo do EPUB: %s", exc)
        return []

    body = tree.find(".//body")
    target = body if body is not None else tree

    blocks: list[Block] = []

    for element in target.iter():
        if not isinstance(element.tag, str):
            continue

        tag = etree.QName(element).localname.lower()

        if tag == "img":
            src = element.get("src", "")
            name = posixpath.basename(src)
            if name:
                blocks.append(
                    Block(kind="image", src=name, text=element.get("alt", "") or "")
                )
            continue

        kind = _BLOCK_MAP.get(tag)
        if not kind:
            continue

        # Um <p> dentro de <blockquote> já foi coberto pelo blockquote.
        parent = element.getparent()
        if parent is not None:
            parent_tag = etree.QName(parent).localname.lower()
            if tag == "p" and parent_tag == "blockquote":
                continue

        text = _text_of(element)
        if not text:
            continue

        blocks.append(Block(kind=kind, text=text))  # type: ignore[arg-type]

    return blocks


def read_epub(epub_path: str | Path) -> Document:
    """Carrega um EPUB para a estrutura interna, preservando capítulos.

    Levanta ValueError se o arquivo não for um EPUB legível.
    """
    epub_path = Path(epub_path)

    try:
        archive = zipfile.ZipFile(epub_path)
    except zipfile.BadZipFile as exc:
        raise ValueError("Arquivo não é um EPUB válido (ZIP corrompido)") from exc

    with archive:
        opf_path = _find_opf_path(archive)
        opf_dir = posixpath.dirname(opf_path)
        opf = etree.fromstring(archive.read(opf_path))

        metadata = opf.find(f"{{{OPF_NS}}}metadata")
        manifest = opf.find(f"{{{OPF_NS}}}manifest")
        spine = opf.find(f"{{{OPF_NS}}}spine")

        if manifest is None or spine is None:
            raise ValueError("EPUB inválido: manifesto ou spine ausente")

        def meta_text(name: str, default: str = "") -> str:
            if metadata is None:
                return default
            node = metadata.find(f"{{{DC_NS}}}{name}")
            return (node.text or default).strip() if node is not None else default

        document = Document(
            title=meta_text("title", epub_path.stem),
            author=meta_text("creator"),
            source_language=meta_text("language", "en"),
        )

        # id -> href
        items: dict[str, str] = {}
        for item in manifest.findall(f"{{{OPF_NS}}}item"):
            item_id = item.get("id")
            href = item.get("href")
            if item_id and href:
                items[item_id] = href

        # Imagens vão junto para que o preview e a reexportação as mantenham.
        image_names: set[str] = set()
        for href in items.values():
            full = posixpath.normpath(posixpath.join(opf_dir, href))
            if Path(href).suffix.lower() in _IMAGE_SUFFIXES:
                try:
                    data = archive.read(full)
                except KeyError:
                    continue
                name = posixpath.basename(href)
                document.images[name] = data
                image_names.add(name)

        for itemref in spine.findall(f"{{{OPF_NS}}}itemref"):
            idref = itemref.get("idref")
            if not idref or idref not in items:
                continue

            href = items[idref]
            full = posixpath.normpath(posixpath.join(opf_dir, href))
            try:
                content = archive.read(full)
            except KeyError:
                log.warning("Item do spine ausente no arquivo: %s", full)
                continue

            blocks = _blocks_from_xhtml(content, image_names)
            if not blocks:
                continue

            title = next(
                (b.text for b in blocks if b.kind.startswith("heading") and b.text),
                "",
            )
            document.chapters.append(
                Chapter(
                    title=title or f"Capítulo {len(document.chapters) + 1}",
                    blocks=blocks,
                )
            )

    if not document.chapters:
        raise ValueError("Nenhum conteúdo de texto encontrado no EPUB")

    return document
