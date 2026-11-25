"""Estilos de parágrafo do livro, como os do InDesign.

Um estilo é um conjunto nomeado de propriedades tipográficas ("Parágrafo",
"Citação", "Título 1"…). Cada bloco usa um estilo e pode ter um ajuste próprio
por cima ("Parágrafo +", na linguagem do InDesign).

Onde cada coisa fica:
  - os estilos prontos e seus valores estão aqui, em BUILTIN;
  - Document.styles guarda só o que o livro mudou: um estilo pronto alterado
    ({"props": {...só o que mudou}}) ou um estilo criado pelo usuário
    ({"name", "based_on", "props"});
  - Block.meta["style"] escolhe outro estilo que não o do tipo do bloco;
  - Block.meta["overrides"] é o ajuste só daquele bloco.

Tudo que vira CSS passa por clean_props: lista fechada de propriedades, cada
uma com tipo e faixa. Nada do que o cliente manda vai para o CSS sem passar
por aqui, pela mesma razão de inline_html (epub_write.py): o arquivo final
tem de ser válido e não pode carregar marcação que ninguém pediu.

As medidas são em em, relativas ao tamanho de letra que o leitor escolher, e
a entrelinha não tem unidade. Os leitores de EPUB deixam o usuário trocar
fonte e tamanho; medidas absolutas brigariam com isso.

A cor ficou de fora de propósito: o modo noturno dos leitores inverte o
fundo, e um texto com cor fixa some ou fica ilegível.
"""

from __future__ import annotations

import re
from typing import Any

from .blocks import Block, Document


class StyleError(ValueError):
    """Estilo ou valor inválido. A mensagem é para o usuário."""


# --------------------------------------------------------------------------
# As propriedades
# --------------------------------------------------------------------------

FONT_FAMILIES = ("serif", "sans-serif", "monospace")
ALIGNMENTS = ("left", "center", "right", "justify")

# nome -> (tipo, mínimo/opções, máximo, rótulo). Números em em, salvo a entrelinha.
PROPS: dict[str, dict[str, Any]] = {
    "font_family": {"type": "choice", "options": FONT_FAMILIES, "label": "Fonte"},
    "font_size": {"type": "number", "min": 0.6, "max": 4.0, "unit": "em", "label": "Tamanho"},
    "bold": {"type": "bool", "label": "Negrito"},
    "italic": {"type": "bool", "label": "Itálico"},
    "small_caps": {"type": "bool", "label": "Versalete"},
    "align": {"type": "choice", "options": ALIGNMENTS, "label": "Alinhamento"},
    "indent": {
        "type": "number",
        "min": -3.0,
        "max": 6.0,
        "unit": "em",
        "label": "Recuo da 1ª linha",
    },
    "margin_left": {
        "type": "number",
        "min": 0.0,
        "max": 8.0,
        "unit": "em",
        "label": "Margem esquerda",
    },
    "margin_right": {
        "type": "number",
        "min": 0.0,
        "max": 8.0,
        "unit": "em",
        "label": "Margem direita",
    },
    "space_before": {
        "type": "number",
        "min": 0.0,
        "max": 8.0,
        "unit": "em",
        "label": "Espaço antes",
    },
    "space_after": {
        "type": "number",
        "min": 0.0,
        "max": 8.0,
        "unit": "em",
        "label": "Espaço depois",
    },
    "line_height": {"type": "number", "min": 0.8, "max": 3.0, "unit": "", "label": "Entrelinha"},
    "page_break_before": {"type": "bool", "label": "Começar em página nova"},
}


def _clean_value(name: str, value: Any) -> Any:
    spec = PROPS[name]
    kind = spec["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            raise StyleError(f"{spec['label']}: use verdadeiro ou falso")
        return value
    if kind == "choice":
        if value not in spec["options"]:
            raise StyleError(f"{spec['label']}: valor inválido")
        return value
    # number: bool é int em Python, e True não é um tamanho.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StyleError(f"{spec['label']}: use um número")
    number = float(value)
    if not spec["min"] <= number <= spec["max"]:
        unit = f" {spec['unit']}" if spec["unit"] else ""
        raise StyleError(f"{spec['label']}: entre {spec['min']:g} e {spec['max']:g}{unit}")
    return round(number, 2)


def clean_props(raw: Any) -> dict[str, Any]:
    """Valida um conjunto de propriedades. Levanta StyleError no primeiro erro.

    None num valor quer dizer "tirar esta propriedade" e é mantido, para quem
    chama decidir (ver merge_props).
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise StyleError("As propriedades devem ser um objeto")
    clean: dict[str, Any] = {}
    for name, value in raw.items():
        if name not in PROPS:
            raise StyleError(f"Propriedade desconhecida: {name}")
        clean[name] = None if value is None else _clean_value(name, value)
    return clean


def merge_props(base: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """Aplica `changes` (já limpo) sobre `base`; None apaga a propriedade."""
    merged = dict(base)
    for name, value in changes.items():
        if value is None:
            merged.pop(name, None)
        else:
            merged[name] = value
    return merged


def _safe_props(raw: Any) -> dict[str, Any]:
    """Como clean_props, mas descarta o que for inválido em vez de falhar.

    Para a hora de gerar CSS a partir do que está gravado: um document.json
    editado à mão não pode derrubar a prévia nem a exportação.
    """
    if not isinstance(raw, dict):
        return {}
    safe: dict[str, Any] = {}
    for name, value in raw.items():
        if name in PROPS and value is not None:
            try:
                safe[name] = _clean_value(name, value)
            except StyleError:
                continue
    return safe


# --------------------------------------------------------------------------
# Os estilos prontos
# --------------------------------------------------------------------------

# Os valores reproduzem DEFAULT_CSS (epub_write.py): um livro que passa a usar
# estilos fica igual ao que era, até alguém mudar alguma coisa.
_TEXT = {
    "font_size": 1.0,
    "bold": False,
    "italic": False,
    "small_caps": False,
    "align": "justify",
    "indent": 1.2,
    "margin_left": 0.0,
    "margin_right": 0.0,
    "space_before": 0.0,
    "space_after": 0.0,
    "line_height": 1.5,
    "page_break_before": False,
}

_HEADING = {**_TEXT, "bold": True, "align": "left", "indent": 0.0, "line_height": 1.25}

BUILTIN: dict[str, dict[str, Any]] = {
    "paragraph": {"name": "Parágrafo", "props": dict(_TEXT)},
    # O parágrafo logo depois de um título, sem recuo (h1 + p no DEFAULT_CSS).
    "first_paragraph": {"name": "Primeiro parágrafo", "props": {**_TEXT, "indent": 0.0}},
    "heading1": {
        "name": "Título 1",
        "props": {
            **_HEADING,
            "font_size": 1.6,
            "space_before": 2.0,
            "space_after": 1.0,
            "page_break_before": True,
        },
    },
    "heading2": {
        "name": "Título 2",
        "props": {**_HEADING, "font_size": 1.3, "space_before": 1.6, "space_after": 0.8},
    },
    "heading3": {
        "name": "Título 3",
        "props": {**_HEADING, "font_size": 1.1, "space_before": 1.4, "space_after": 0.6},
    },
    "quote": {
        "name": "Citação",
        "props": {
            **_TEXT,
            "italic": True,
            "margin_left": 2.0,
            "margin_right": 2.0,
            "space_before": 1.0,
            "space_after": 1.0,
        },
    },
    "list_item": {
        "name": "Item de lista",
        "props": {**_TEXT, "indent": 0.0, "space_after": 0.3},
    },
}

# Tipos de bloco que usam estilo. Nota de rodapé, tabela, imagem e quebra de
# página têm formatação própria e ficam de fora por enquanto.
STYLED_KINDS = frozenset({"paragraph", "heading1", "heading2", "heading3", "quote", "list_item"})

MAX_CUSTOM_STYLES = 60
_CUSTOM_ID = re.compile(r"^u-[a-z0-9]{1,24}$")


def is_custom_id(style_id: str) -> bool:
    return bool(_CUSTOM_ID.match(style_id))


# --------------------------------------------------------------------------
# Resolver: qual estilo, com que valores
# --------------------------------------------------------------------------


def exists(document: Document, style_id: str) -> bool:
    if style_id in BUILTIN:
        return True
    entry = document.styles.get(style_id)
    return is_custom_id(style_id) and isinstance(entry, dict)


def effective(document: Document, style_id: str) -> dict[str, Any]:
    """Os valores completos do estilo, com o que o livro mudou por cima."""
    if style_id in BUILTIN:
        entry = document.styles.get(style_id)
        changes = _safe_props(entry.get("props")) if isinstance(entry, dict) else {}
        return {**BUILTIN[style_id]["props"], **changes}
    entry = document.styles.get(style_id)
    if not is_custom_id(style_id) or not isinstance(entry, dict):
        return dict(BUILTIN["paragraph"]["props"])
    base = entry.get("based_on")
    base_props = effective(document, base) if base in BUILTIN else BUILTIN["paragraph"]["props"]
    return {**base_props, **_safe_props(entry.get("props"))}


def style_name(document: Document, style_id: str) -> str:
    if style_id in BUILTIN:
        return BUILTIN[style_id]["name"]
    entry = document.styles.get(style_id)
    return str(entry.get("name") or "Sem nome") if isinstance(entry, dict) else style_id


# Depois destes, o parágrafo começa sem recuo: títulos, e a troca de cena
# (separador ou linha em branco), como manda a tipografia de livro.
_OPENS_SECTION = frozenset({"heading1", "heading2", "heading3", "scene_break", "spacer"})


def default_style(kind: str, previous_kind: str | None) -> str:
    """O estilo de um bloco que não escolheu nenhum.

    O parágrafo logo depois de um título é "Primeiro parágrafo". O DEFAULT_CSS
    faz isso com h1 + p, mas a classe do estilo ganharia daquela regra; aqui a
    decisão é tomada antes de gerar o XHTML.
    """
    if kind == "paragraph" and previous_kind in _OPENS_SECTION:
        return "first_paragraph"
    return kind


def block_style(document: Document, block: Block, previous_kind: str | None) -> str:
    chosen = block.meta.get("style")
    if isinstance(chosen, str) and exists(document, chosen):
        return chosen
    return default_style(block.kind, previous_kind)


def uses_styles(document: Document) -> bool:
    """O livro mexeu em estilos? Se não, o XHTML e o CSS saem como sempre saíram."""
    if document.styles:
        return True
    return any(
        b.meta.get("style") or b.meta.get("overrides")
        for b in document.all_blocks()
        if b.kind in STYLED_KINDS
    )


# --------------------------------------------------------------------------
# CSS
# --------------------------------------------------------------------------


def _number(value: float) -> str:
    return f"{value:g}"


def declarations(props: dict[str, Any]) -> list[str]:
    """As declarações CSS de um conjunto de propriedades já validado."""
    out: list[str] = []
    if "font_family" in props:
        out.append(f"font-family: {props['font_family']}")
    if "font_size" in props:
        out.append(f"font-size: {_number(props['font_size'])}em")
    if "bold" in props:
        out.append(f"font-weight: {'bold' if props['bold'] else 'normal'}")
    if "italic" in props:
        out.append(f"font-style: {'italic' if props['italic'] else 'normal'}")
    if "small_caps" in props:
        out.append(f"font-variant: {'small-caps' if props['small_caps'] else 'normal'}")
    if "align" in props:
        out.append(f"text-align: {props['align']}")
    if "indent" in props:
        out.append(f"text-indent: {_number(props['indent'])}em")
    for name, css in (
        ("margin_left", "margin-left"),
        ("margin_right", "margin-right"),
        ("space_before", "margin-top"),
        ("space_after", "margin-bottom"),
    ):
        if name in props:
            value = props[name]
            out.append(f"{css}: {_number(value)}em" if value else f"{css}: 0")
    if "line_height" in props:
        out.append(f"line-height: {_number(props['line_height'])}")
    if "page_break_before" in props:
        value = "always" if props["page_break_before"] else "auto"
        out.append(f"page-break-before: {value}")
        out.append(f"break-before: {'page' if props['page_break_before'] else 'auto'}")
    return out


def class_name(style_id: str) -> str:
    return f"st-{style_id.replace('_', '-')}"


def stylesheet(document: Document) -> str:
    """As regras de todos os estilos do livro, para ir depois do DEFAULT_CSS.

    Cada estilo vira uma classe com os valores completos. Classe ganha das
    regras por elemento do DEFAULT_CSS (h1, p, blockquote), então o que não
    está aqui continua vindo de lá.
    """
    ids = list(BUILTIN) + sorted(k for k in document.styles if is_custom_id(k))
    rules: list[str] = ["/* Estilos de parágrafo do livro (core/styles.py). */"]
    for style_id in ids:
        css = class_name(style_id)
        body = ";\n  ".join(declarations(effective(document, style_id)))
        rules.append(f".{css} {{\n  {body};\n}}")
        # A citação é <blockquote><p>: o recuo do <p> de dentro vem do estilo
        # do blockquote, e não da regra geral de parágrafo.
        rules.append(f"blockquote.{css} > p {{ text-indent: inherit; }}")
    return "\n".join(rules) + "\n"


def inline_style(block: Block) -> str:
    """O ajuste só deste bloco, como atributo style. Vazio se não houver."""
    props = _safe_props(block.meta.get("overrides"))
    return "; ".join(declarations(props))


# --------------------------------------------------------------------------
# Para a interface
# --------------------------------------------------------------------------


def schema() -> list[dict[str, Any]]:
    """As propriedades com tipo, faixa e rótulo, para montar os controles."""
    out = []
    for name, spec in PROPS.items():
        item = {"name": name, **spec}
        if "options" in item:
            item["options"] = list(item["options"])
        out.append(item)
    return out


def describe(document: Document) -> list[dict[str, Any]]:
    """Todos os estilos do livro, prontos e do usuário, com os valores finais."""
    out = []
    for style_id in list(BUILTIN) + sorted(k for k in document.styles if is_custom_id(k)):
        entry = document.styles.get(style_id)
        custom = style_id not in BUILTIN
        out.append(
            {
                "id": style_id,
                "name": style_name(document, style_id),
                "builtin": not custom,
                "based_on": entry.get("based_on") if custom and isinstance(entry, dict) else None,
                # O que o livro mudou em relação ao estilo pronto (ou à base).
                "changes": _safe_props(entry.get("props")) if isinstance(entry, dict) else {},
                "props": effective(document, style_id),
                "class": class_name(style_id),
            }
        )
    return out


# --------------------------------------------------------------------------
# Edições (as rotas chamam estas; cada uma altera o Document recebido)
# --------------------------------------------------------------------------


def _clean_name(name: Any) -> str:
    text = " ".join(str(name or "").split())[:60]
    if not text:
        raise StyleError("Dê um nome ao estilo")
    return text


def update_style(document: Document, style_id: str, props: Any = None, name: Any = None) -> None:
    """Muda propriedades (só as enviadas; None apaga) e, num estilo do usuário, o nome.

    Num estilo pronto, o que fica gravado é só a diferença para o padrão:
    voltar um valor ao do estilo pronto tira a entrada do livro.
    """
    if not exists(document, style_id):
        raise StyleError("Estilo não encontrado")
    changes = clean_props(props)
    entry = dict(document.styles.get(style_id) or {})
    current = _safe_props(entry.get("props"))
    merged = merge_props(current, changes)

    if style_id in BUILTIN:
        if name is not None:
            raise StyleError("Os estilos prontos não mudam de nome")
        defaults = BUILTIN[style_id]["props"]
        merged = {k: v for k, v in merged.items() if defaults.get(k) != v}
        if merged:
            document.styles[style_id] = {"props": merged}
        else:
            document.styles.pop(style_id, None)
        return

    if name is not None:
        entry["name"] = _clean_name(name)
    entry["props"] = merged
    document.styles[style_id] = entry


def create_style(document: Document, name: Any, based_on: Any, new_id: str) -> str:
    """Um estilo novo do usuário, a partir de um pronto. Devolve o id."""
    if based_on not in BUILTIN:
        raise StyleError("Um estilo novo parte de um dos estilos prontos")
    custom = [k for k in document.styles if is_custom_id(k)]
    if len(custom) >= MAX_CUSTOM_STYLES:
        raise StyleError(f"Limite de {MAX_CUSTOM_STYLES} estilos por livro")
    style_id = f"u-{new_id}"
    if not is_custom_id(style_id) or style_id in document.styles:
        raise StyleError("Identificador de estilo inválido")
    document.styles[style_id] = {"name": _clean_name(name), "based_on": based_on, "props": {}}
    return style_id


def delete_style(document: Document, style_id: str) -> int:
    """Estilo pronto: volta ao padrão. Do usuário: some, e os blocos que o
    usavam voltam ao estilo do tipo deles. Devolve quantos blocos mudaram.
    """
    if style_id in BUILTIN:
        document.styles.pop(style_id, None)
        return 0
    if not exists(document, style_id):
        raise StyleError("Estilo não encontrado")
    document.styles.pop(style_id)
    moved = 0
    for block in document.all_blocks():
        if block.meta.get("style") == style_id:
            block.meta.pop("style")
            moved += 1
    return moved


def set_block_style(document: Document, block: Block, style_id: Any) -> None:
    """Escolhe o estilo do bloco; None (ou o do tipo dele) volta ao automático."""
    if block.kind not in STYLED_KINDS:
        raise StyleError("Este tipo de bloco não usa estilo de parágrafo")
    if style_id is None or style_id == "":
        block.meta.pop("style", None)
        return
    if not isinstance(style_id, str) or not exists(document, style_id):
        raise StyleError("Estilo não encontrado")
    block.meta["style"] = style_id


def set_block_overrides(block: Block, props: Any) -> None:
    """Ajuste do bloco: só o enviado muda; None numa propriedade a tira;
    None no lugar do objeto limpa o ajuste inteiro ("Limpar ajuste")."""
    if block.kind not in STYLED_KINDS:
        raise StyleError("Este tipo de bloco não usa estilo de parágrafo")
    if props is None:
        block.meta.pop("overrides", None)
        return
    merged = merge_props(_safe_props(block.meta.get("overrides")), clean_props(props))
    if merged:
        block.meta["overrides"] = merged
    else:
        block.meta.pop("overrides", None)
