"""Escrita de arquivos EPUB 3.

Um EPUB é um ZIP com regras: o primeiro item precisa ser o arquivo `mimetype`,
armazenado sem compressão, e existe um manifesto XML descrevendo tudo. Montamos
o pacote à mão com `zipfile` em vez de usar uma biblioteca pronta, por dois
motivos: as alternativas populares são AGPL, e controlar o XHTML gerado importa
porque é exatamente ele que o usuário vai editar depois.

O resultado passa no epubcheck para os recursos usados aqui.
"""

from __future__ import annotations

import html
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .blocks import Block, Chapter, Document

CONTAINER_XML = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

DEFAULT_CSS = """/* Estilos base. O leitor do usuário pode sobrescrever quase tudo,
   então mantemos o mínimo necessário para o texto ficar legível. */

body {
  margin: 0 5%;
  line-height: 1.5;
  text-align: justify;
  hyphens: auto;
}

h1, h2, h3 {
  text-align: left;
  line-height: 1.25;
  page-break-after: avoid;
  break-after: avoid;
}

h1 {
  font-size: 1.6em;
  margin: 2em 0 1em;
  page-break-before: always;
  break-before: page;
}

h2 { font-size: 1.3em; margin: 1.6em 0 0.8em; }
h3 { font-size: 1.1em; margin: 1.4em 0 0.6em; }

p {
  margin: 0;
  text-indent: 1.2em;
}

/* O primeiro parágrafo depois de um título não leva recuo. */
h1 + p, h2 + p, h3 + p, blockquote + p.first, p.first {
  text-indent: 0;
}

blockquote {
  margin: 1em 2em;
  font-style: italic;
}

ul { margin: 1em 0; }
li { text-indent: 0; margin-bottom: 0.3em; }

img {
  max-width: 100%;
  height: auto;
}

/* Figura centralizada e sem o recuo de parágrafo; o texto depois dela
   recomeça sem recuo, como depois de um título. */
p.image {
  text-indent: 0;
  text-align: center;
  margin: 1em 0;
}

p.image + p { text-indent: 0; }

hr.page-break {
  border: 0;
  page-break-after: always;
  break-after: page;
}
"""

# Tags inline que o editor pode produzir e que preservamos no XHTML.
_ALLOWED_INLINE = ("em", "strong", "i", "b", "sup", "sub", "small", "br")

_ESCAPED_TAG = re.compile(
    r"&lt;(/?)(" + "|".join(_ALLOWED_INLINE) + r")\s*(/?)&gt;",
    re.IGNORECASE,
)
_ESCAPED_LINK = re.compile(
    r"&lt;a\s+href=(?:&quot;|&#x27;)([^&<>\"']{1,500})(?:&quot;|&#x27;)&gt;",
    re.IGNORECASE,
)
_ESCAPED_LINK_CLOSE = re.compile(r"&lt;/a&gt;", re.IGNORECASE)


def inline_html(text: str) -> str:
    """Escapa o texto e devolve apenas as tags inline permitidas.

    Escapar tudo e reabilitar uma lista curta é mais seguro que tentar remover
    o que é perigoso: qualquer construção não prevista continua escapada e
    aparece como texto literal, nunca como marcação ativa.
    """
    escaped = html.escape(text, quote=True)
    escaped = _ESCAPED_TAG.sub(
        lambda m: f"<{m.group(1)}{m.group(2).lower()}{'/' if m.group(3) else ''}>",
        escaped,
    )
    escaped = _ESCAPED_LINK.sub(
        lambda m: f'<a href="{html.escape(m.group(1), quote=True)}">', escaped
    )
    escaped = _ESCAPED_LINK_CLOSE.sub("</a>", escaped)
    # <br> precisa ser fechado em XHTML.
    escaped = re.sub(r"<br\s*/?>", "<br/>", escaped, flags=re.IGNORECASE)
    return escaped


def _block_to_xhtml(block: Block, *, use_translation: bool) -> str:
    """Converte um bloco em um fragmento XHTML."""
    text = block.display_text if use_translation else block.text

    if block.kind == "page_break":
        return '<hr class="page-break"/>'

    if block.kind == "image":
        if not block.src:
            return ""
        alt = html.escape(text or "", quote=True)
        return f'<p class="image"><img src="../images/{html.escape(block.src)}" alt="{alt}"/></p>'

    if not text.strip():
        return ""

    body = inline_html(text)
    anchor = f' id="b{block.id}"'

    if block.kind == "heading1":
        return f"<h1{anchor}>{body}</h1>"
    if block.kind == "heading2":
        return f"<h2{anchor}>{body}</h2>"
    if block.kind == "heading3":
        return f"<h3{anchor}>{body}</h3>"
    if block.kind == "quote":
        return f"<blockquote{anchor}><p>{body}</p></blockquote>"
    if block.kind == "list_item":
        return f"<li{anchor}>{body}</li>"

    return f"<p{anchor}>{body}</p>"


def chapter_to_xhtml(
    chapter: Chapter,
    *,
    language: str = "pt-BR",
    use_translation: bool = True,
    css_href: str = "../style.css",
) -> str:
    """Gera o XHTML completo de um capítulo.

    Itens de lista consecutivos são embrulhados num <ul> para que o documento
    seja XHTML válido.
    """
    parts: list[str] = []
    in_list = False

    for block in chapter.blocks:
        fragment = _block_to_xhtml(block, use_translation=use_translation)
        if not fragment:
            continue

        is_item = block.kind == "list_item"
        if is_item and not in_list:
            parts.append("<ul>")
            in_list = True
        elif not is_item and in_list:
            parts.append("</ul>")
            in_list = False

        parts.append(fragment)

    if in_list:
        parts.append("</ul>")

    body = "\n".join(parts)
    title = html.escape(chapter.title or "", quote=True)

    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{html.escape(language)}" xml:lang="{html.escape(language)}">
<head>
  <meta charset="utf-8"/>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="{css_href}"/>
</head>
<body>
{body}
</body>
</html>
"""


def _nav_xhtml(document: Document, filenames: list[str], language: str) -> str:
    items = "\n".join(
        f'      <li><a href="text/{name}">{html.escape(chapter.title or f"Capítulo {i}", quote=True)}</a></li>'
        for i, (chapter, name) in enumerate(zip(document.chapters, filenames), start=1)
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="{html.escape(language)}" xml:lang="{html.escape(language)}">
<head>
  <meta charset="utf-8"/>
  <title>Sumário</title>
</head>
<body>
  <nav epub:type="toc" id="toc">
    <h1>Sumário</h1>
    <ol>
{items}
    </ol>
  </nav>
</body>
</html>
"""


def _toc_ncx(document: Document, filenames: list[str]) -> str:
    """NCX para compatibilidade com leitores antigos (Kindle, ADE)."""
    points = "\n".join(
        f"""    <navPoint id="np{i}" playOrder="{i}">
      <navLabel><text>{html.escape(chapter.title or f"Capítulo {i}")}</text></navLabel>
      <content src="text/{name}"/>
    </navPoint>"""
        for i, (chapter, name) in enumerate(zip(document.chapters, filenames), start=1)
    )
    return f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head>
    <meta name="dtb:uid" content="{html.escape(document.identifier)}"/>
    <meta name="dtb:depth" content="1"/>
    <meta name="dtb:totalPageCount" content="0"/>
    <meta name="dtb:maxPageNumber" content="0"/>
  </head>
  <docTitle><text>{html.escape(document.title)}</text></docTitle>
  <navMap>
{points}
  </navMap>
</ncx>
"""


_IMAGE_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
}


def _content_opf(document: Document, filenames: list[str], language: str) -> str:
    manifest = [
        '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>',
        '    <item id="css" href="style.css" media-type="text/css"/>',
    ]
    spine = []

    for i, name in enumerate(filenames, start=1):
        manifest.append(
            f'    <item id="ch{i}" href="text/{name}" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'    <itemref idref="ch{i}"/>')

    for i, image_name in enumerate(sorted(document.images), start=1):
        media = _IMAGE_TYPES.get(Path(image_name).suffix.lower(), "image/png")
        manifest.append(
            f'    <item id="img{i}" href="images/{image_name}" media-type="{media}"/>'
        )

    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    author = html.escape(document.author or "Desconhecido")

    return f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pub-id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pub-id">{html.escape(document.identifier)}</dc:identifier>
    <dc:title>{html.escape(document.title)}</dc:title>
    <dc:language>{html.escape(language)}</dc:language>
    <dc:creator id="creator">{author}</dc:creator>
    <meta refines="#creator" property="role" scheme="marc:relators">aut</meta>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
{chr(10).join(manifest)}
  </manifest>
  <spine toc="ncx">
{chr(10).join(spine)}
  </spine>
</package>
"""


def write_epub(
    document: Document,
    output_path: str | Path,
    *,
    use_translation: bool = True,
) -> Path:
    """Grava o Document como um arquivo EPUB 3 válido.

    use_translation=False exporta o texto original, útil para comparar a
    tradução com a fonte no mesmo leitor.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    language = document.language if use_translation else document.source_language
    filenames = [f"ch{i:04d}.xhtml" for i in range(1, len(document.chapters) + 1)]

    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as epub:
        # O mimetype precisa ser o primeiro arquivo e não pode ser comprimido.
        epub.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        epub.writestr("META-INF/container.xml", CONTAINER_XML)
        epub.writestr("OEBPS/style.css", DEFAULT_CSS)
        epub.writestr("OEBPS/content.opf", _content_opf(document, filenames, language))
        epub.writestr("OEBPS/nav.xhtml", _nav_xhtml(document, filenames, language))
        epub.writestr("OEBPS/toc.ncx", _toc_ncx(document, filenames))

        for chapter, name in zip(document.chapters, filenames):
            epub.writestr(
                f"OEBPS/text/{name}",
                chapter_to_xhtml(
                    chapter, language=language, use_translation=use_translation
                ),
            )

        for image_name, data in document.images.items():
            epub.writestr(f"OEBPS/images/{image_name}", data)

    return output_path
