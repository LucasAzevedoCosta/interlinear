"""Estilos de parágrafo: validação, valores prontos, XHTML, CSS e rotas."""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

import pytest
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import styles  # noqa: E402
from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.core.epub_write import DEFAULT_CSS, book_css, chapter_to_xhtml, write_epub  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402


def _book() -> Document:
    blocks = [
        Block(kind="heading1", text="Capítulo 1", id="h1"),
        Block(kind="paragraph", text="Primeiro.", id="p1"),
        Block(kind="paragraph", text="Segundo.", id="p2"),
        Block(kind="image", src="fig.png", id="img"),
        Block(kind="paragraph", text="Depois da figura.", id="p3"),
        Block(kind="quote", text="Uma citação.", id="q1"),
        Block(kind="list_item", text="Item.", id="li"),
        Block(kind="footnote", text="Nota.", id="fn", meta={"marker": "1"}),
    ]
    return Document(chapters=[Chapter(title="Um", blocks=blocks, id="c1")])


def _classes(xhtml: str) -> dict[str, str]:
    """id do bloco -> classe, lido do XHTML."""
    return dict(re.findall(r'id="b(\w+)" class="([^"]+)"', xhtml))


# ------------------------------------------------------- livro sem estilos


def test_livro_sem_estilos_sai_como_antes():
    document = _book()
    assert not styles.uses_styles(document)
    assert book_css(document) == DEFAULT_CSS
    chapter = document.chapters[0]
    assert chapter_to_xhtml(chapter) == chapter_to_xhtml(chapter, styled=None)
    assert 'class="st-' not in chapter_to_xhtml(chapter)


def test_um_ajuste_num_bloco_ja_liga_os_estilos():
    document = _book()
    document.chapters[0].blocks[2].meta["overrides"] = {"space_after": 1.5}
    assert styles.uses_styles(document)


def test_marca_em_bloco_sem_estilo_nao_liga():
    """Nota de rodapé não usa estilo; um 'style' perdido nela não muda o livro."""
    document = _book()
    document.chapters[0].blocks[-1].meta["style"] = "quote"
    assert not styles.uses_styles(document)


# ------------------------------------------------------------- validação


def test_propriedade_desconhecida_e_recusada():
    with pytest.raises(styles.StyleError, match="desconhecida"):
        styles.clean_props({"color": "red"})


@pytest.mark.parametrize(
    "props",
    [
        {"indent": 99},
        {"indent": "1.2em"},
        {"font_size": True},
        {"line_height": 0.1},
        {"align": "middle"},
        {"bold": "sim"},
        {"font_family": "Comic Sans"},
    ],
)
def test_valores_invalidos_sao_recusados(props):
    with pytest.raises(styles.StyleError):
        styles.clean_props(props)


def test_numero_e_arredondado_e_none_e_mantido():
    assert styles.clean_props({"indent": 1.23456, "bold": None}) == {"indent": 1.23, "bold": None}


def test_valor_invalido_gravado_nao_derruba_o_css():
    """Um document.json editado à mão: o valor ruim é ignorado, o resto vale."""
    document = _book()
    document.styles["paragraph"] = {"props": {"indent": "<script>", "space_after": 2}}
    css = styles.stylesheet(document)
    assert "<script>" not in css
    assert "margin-bottom: 2em" in css


# -------------------------------------------------- os valores prontos


@pytest.mark.parametrize(
    ("style_id", "expected"),
    [
        ("heading1", ["font-size: 1.6em", "margin: 2em 0 1em", "page-break-before: always"]),
        ("heading2", ["font-size: 1.3em", "margin: 1.6em 0 0.8em"]),
        ("heading3", ["font-size: 1.1em", "margin: 1.4em 0 0.6em"]),
        ("quote", ["margin: 1em 2em", "font-style: italic"]),
        ("paragraph", ["text-indent: 1.2em", "line-height: 1.5", "text-align: justify"]),
        ("list_item", ["margin-bottom: 0.3em"]),
    ],
)
def test_estilos_prontos_reproduzem_o_css_padrao(style_id, expected):
    """O DEFAULT_CSS tem estes valores; os estilos prontos têm de ter os mesmos."""
    for declaration in expected:
        assert declaration in DEFAULT_CSS, declaration
    props = styles.BUILTIN[style_id]["props"]
    checks = {
        "heading1": props.get("font_size") == 1.6
        and (props["space_before"], props["space_after"]) == (2, 1)
        and props["page_break_before"],
        "heading2": props.get("font_size") == 1.3
        and (props["space_before"], props["space_after"]) == (1.6, 0.8),
        "heading3": props.get("font_size") == 1.1
        and (props["space_before"], props["space_after"]) == (1.4, 0.6),
        "quote": props["italic"]
        and (props["space_before"], props["margin_right"]) == (1, 2)
        and props["margin_left"] == 2,
        "paragraph": (props["indent"], props["line_height"], props["align"])
        == (1.2, 1.5, "justify"),
        "list_item": props["space_after"] == 0.3 and props["indent"] == 0,
    }
    assert checks[style_id]


# ------------------------------------------------------------------ XHTML


def test_classe_de_cada_bloco_e_primeiro_paragrafo_depois_do_titulo():
    document = _book()
    document.styles["paragraph"] = {"props": {"indent": 2.0}}
    xhtml = chapter_to_xhtml(document.chapters[0], styled=document)
    classes = _classes(xhtml)
    assert classes["h1"] == "st-heading1"
    assert classes["p1"] == "st-first-paragraph"
    assert classes["p2"] == "st-paragraph"
    # Depois da figura continua "Parágrafo"; o DEFAULT_CSS tira o recuo (p.image + p).
    assert classes["p3"] == "st-paragraph"
    assert classes["q1"] == "st-quote"
    assert classes["li"] == "st-list-item"
    assert 'class="image"' in xhtml  # a figura não muda
    etree.fromstring(xhtml.encode("utf-8"))  # XHTML bem formado


def test_estilo_escolhido_e_ajuste_do_bloco():
    document = _book()
    blocks = document.chapters[0].blocks
    blocks[2].meta["style"] = "quote"
    blocks[2].meta["overrides"] = {"space_after": 1.5, "align": "center"}
    xhtml = chapter_to_xhtml(document.chapters[0], styled=document)
    assert re.search(
        r'<p id="bp2" class="st-quote" style="text-align: center; margin-bottom: 1.5em">', xhtml
    )


def test_estilo_que_nao_existe_volta_ao_do_tipo():
    document = _book()
    document.chapters[0].blocks[2].meta["style"] = "u-sumiu"
    xhtml = chapter_to_xhtml(document.chapters[0], styled=document)
    assert _classes(xhtml)["p2"] == "st-paragraph"


def test_css_tem_uma_regra_por_estilo_e_a_citacao_herda_o_recuo():
    document = _book()
    document.styles["quote"] = {"props": {"indent": 0.0}}
    css = book_css(document)
    assert css.startswith(DEFAULT_CSS)
    for style_id in styles.BUILTIN:
        assert f".{styles.class_name(style_id)} {{" in css
    assert "blockquote.st-quote > p { text-indent: inherit; }" in css
    quote_rule = css.split(".st-quote {")[1].split("}")[0]
    assert "text-indent: 0em" in quote_rule


def test_exportacao_leva_o_css_e_as_classes(tmp_path):
    document = _book()
    document.styles["heading1"] = {"props": {"align": "center"}}
    path = write_epub(document, tmp_path / "livro.epub", use_translation=False)
    with zipfile.ZipFile(path) as epub:
        css = epub.read("OEBPS/style.css").decode()
        xhtml = epub.read("OEBPS/text/ch0001.xhtml").decode()
    assert ".st-heading1 {" in css and "text-align: center" in css
    assert 'class="st-heading1"' in xhtml
    etree.fromstring(xhtml.encode("utf-8"))


# --------------------------------------------------------------- edições


def test_estilo_pronto_guarda_so_a_diferenca():
    document = _book()
    styles.update_style(document, "paragraph", {"indent": 2.0, "line_height": 1.5})
    assert document.styles == {"paragraph": {"props": {"indent": 2.0}}}
    # Voltar ao valor pronto tira a entrada.
    styles.update_style(document, "paragraph", {"indent": 1.2})
    assert document.styles == {}


def test_none_apaga_a_propriedade():
    document = _book()
    styles.update_style(document, "quote", {"italic": False})
    styles.update_style(document, "quote", {"italic": None})
    assert "quote" not in document.styles


def test_estilo_pronto_nao_muda_de_nome():
    with pytest.raises(styles.StyleError):
        styles.update_style(_book(), "paragraph", name="Outro")


def test_criar_e_apagar_estilo_do_usuario():
    document = _book()
    style_id = styles.create_style(document, "  Epígrafe  ", "quote", "abc123")
    assert style_id == "u-abc123"
    styles.update_style(document, style_id, {"align": "right"}, name="Epígrafe longa")
    props = styles.effective(document, style_id)
    assert props["italic"] is True  # vem da Citação
    assert props["align"] == "right"
    assert styles.style_name(document, style_id) == "Epígrafe longa"

    block = document.chapters[0].blocks[2]
    styles.set_block_style(document, block, style_id)
    assert styles.delete_style(document, style_id) == 1
    assert "style" not in block.meta
    assert style_id not in document.styles


def test_estilo_novo_precisa_de_base_pronta_e_nome():
    document = _book()
    with pytest.raises(styles.StyleError):
        styles.create_style(document, "X", "u-outro", "abc")
    with pytest.raises(styles.StyleError):
        styles.create_style(document, "   ", "paragraph", "abc")


def test_bloco_sem_estilo_de_paragrafo_recusa():
    document = _book()
    image = document.chapters[0].blocks[3]
    with pytest.raises(styles.StyleError):
        styles.set_block_style(document, image, "paragraph")
    with pytest.raises(styles.StyleError):
        styles.set_block_overrides(image, {"indent": 1})


def test_ajuste_mescla_apaga_e_limpa():
    block = Block(kind="paragraph", text="x")
    styles.set_block_overrides(block, {"indent": 0, "space_after": 1})
    styles.set_block_overrides(block, {"indent": None, "align": "center"})
    assert block.meta["overrides"] == {"space_after": 1.0, "align": "center"}
    styles.set_block_overrides(block, None)
    assert "overrides" not in block.meta


def test_estilos_sobrevivem_a_gravacao():
    document = _book()
    styles.update_style(document, "paragraph", {"indent": 2.0})
    again = Document.from_dict(document.to_dict())
    assert again.styles == document.styles
    assert Document.from_dict({**document.to_dict(), "styles": ["lixo"]}).styles == {}


# ------------------------------------------------------------------ rotas


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


@pytest.fixture
def book(client):
    return get_store().create(_book(), source_kind="epub", source_filename="x.epub").id


def test_rota_lista_estilos_e_propriedades(client, book):
    data = client.get(f"/api/projects/{book}/styles").json()
    assert [s["id"] for s in data["styles"]] == list(styles.BUILTIN)
    assert {p["name"] for p in data["schema"]} == set(styles.PROPS)


def test_rota_muda_estilo_e_a_previa_acompanha(client, book):
    response = client.patch(f"/api/projects/{book}/styles/paragraph", json={"props": {"indent": 3}})
    assert response.status_code == 200, response.text
    css = client.get(f"/api/projects/{book}/preview.css")
    assert css.headers["cache-control"] == "no-store"
    assert "text-indent: 3em" in css.text
    html = client.get(f"/api/projects/{book}/preview/c1?translated=false").text
    assert 'class="st-paragraph"' in html


def test_valor_invalido_e_400_e_nao_404(client, book):
    response = client.patch(
        f"/api/projects/{book}/styles/paragraph", json={"props": {"indent": 50}}
    )
    assert response.status_code == 400
    assert "Recuo" in response.json()["detail"]
    assert client.patch(f"/api/projects/{book}/styles/nada", json={"props": {}}).status_code == 400


def test_rota_cria_e_apaga_estilo(client, book):
    created = client.post(
        f"/api/projects/{book}/styles", json={"name": "Epígrafe", "based_on": "quote"}
    ).json()
    style_id = created["id"]
    assert style_id.startswith("u-")
    assert any(s["id"] == style_id for s in created["styles"])
    deleted = client.delete(f"/api/projects/{book}/styles/{style_id}")
    assert deleted.status_code == 200
    assert all(s["id"] != style_id for s in deleted.json()["styles"])


def test_bloco_ganha_estilo_e_ajuste_pela_rota(client, book):
    response = client.patch(
        f"/api/projects/{book}/blocks",
        json={"updates": [{"id": "p2", "style": "quote", "overrides": {"space_after": 1.5}}]},
    )
    assert response.status_code == 200, response.text
    block = get_store().load_document(book).block_index()["p2"]
    assert block.meta["style"] == "quote"
    assert block.meta["overrides"] == {"space_after": 1.5}


def test_ajuste_invalido_no_bloco_nao_grava_nada(client, book):
    response = client.patch(
        f"/api/projects/{book}/blocks",
        json={"updates": [{"id": "p2", "text": "Mudou.", "overrides": {"indent": "x"}}]},
    )
    assert response.status_code == 400
    block = get_store().load_document(book).block_index()["p2"]
    assert block.text == "Segundo."


def test_meta_generico_nao_contorna_a_validacao(client, book):
    response = client.patch(
        f"/api/projects/{book}/blocks",
        json={"updates": [{"id": "p2", "meta": {"style": "u-x", "overrides": {"indent": "<b>"}}}]},
    )
    assert response.status_code == 200
    block = get_store().load_document(book).block_index()["p2"]
    assert "style" not in block.meta and "overrides" not in block.meta
