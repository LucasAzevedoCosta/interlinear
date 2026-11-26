"""Formatos do livro traduzido: só a tradução e bilíngue parágrafo a parágrafo."""

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

from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.core.epub_write import DEFAULT_CSS, chapter_to_xhtml, write_epub  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402

SRC = ' lang="en" xml:lang="en"'


def _chapter() -> Chapter:
    return Chapter(
        title="One",
        id="c1",
        blocks=[
            Block(kind="heading1", text="Chapter One", translated="Capítulo Um", id="h1"),
            Block(
                kind="paragraph",
                text="First <em>day</em>.",
                translated="Primeiro <em>dia</em>.",
                id="p1",
            ),
            Block(
                kind="paragraph",
                text="See note<sup>1</sup>.",
                translated="Veja a nota<sup>1</sup>.",
                id="p2",
            ),
            Block(kind="paragraph", text="Not translated yet.", id="p3"),
            Block(kind="quote", text="A quote.", translated="Uma citação.", id="q1"),
            Block(kind="list_item", text="Item.", translated="Item traduzido.", id="li"),
            Block(kind="table", text="a | b\nc | d", translated="A | B\nC | D", id="t1"),
            Block(kind="scene_break", text="* * *", id="sb"),
            Block(kind="image", src="fig.png", id="img"),
            Block(
                kind="paragraph", text="After the figure.", translated="Depois da figura.", id="p4"
            ),
            Block(
                kind="footnote",
                text="The note.",
                translated="A nota.",
                id="fn",
                meta={"marker": "1"},
            ),
        ],
    )


def _bilingual(chapter: Chapter | None = None, **extra) -> str:
    return chapter_to_xhtml(
        chapter or _chapter(), language="pt-BR", layout="paragraph", source_language="en", **extra
    )


# ------------------------------------------------- os formatos de sempre


def test_sem_layout_continua_igual():
    chapter = _chapter()
    assert chapter_to_xhtml(chapter, use_translation=True) == chapter_to_xhtml(
        chapter, layout="translated"
    )
    assert chapter_to_xhtml(chapter, use_translation=False) == chapter_to_xhtml(
        chapter, layout="original"
    )
    assert "il-src" not in chapter_to_xhtml(chapter, layout="translated")


# -------------------------------------------------- parágrafo a parágrafo


def test_original_antes_da_traducao_com_o_idioma():
    xhtml = _bilingual()
    assert (
        f'<p class="il-src"{SRC}>First <em>day</em>.</p><p id="bp1" class="first">Primeiro <em>dia</em>.</p>'
        in xhtml
    )


def test_bloco_nao_traduzido_sai_uma_vez():
    xhtml = _bilingual()
    assert xhtml.count("Not translated yet.") == 1
    assert '<p id="bp3">Not translated yet.</p>' in xhtml


def test_titulo_item_e_citacao_com_o_original_dentro():
    xhtml = _bilingual()
    assert f'<h1 id="bh1"><span class="il-src"{SRC}>Chapter One</span> Capítulo Um</h1>' in xhtml
    assert f'<li id="bli"><span class="il-src"{SRC}>Item.</span> Item traduzido.</li>' in xhtml
    assert (
        f'<blockquote id="bq1"><p class="il-src"{SRC}>A quote.</p><p>Uma citação.</p></blockquote>'
        in xhtml
    )


def test_tabela_sai_nas_duas_linguas():
    xhtml = _bilingual()
    assert f'<table class="table il-src-table"{SRC}><tr><td>a</td><td>b</td></tr>' in xhtml
    assert '<table class="table" id="bt1"><tr><td>A</td><td>B</td></tr>' in xhtml


def test_espacamento_e_imagem_saem_uma_vez():
    xhtml = _bilingual()
    assert xhtml.count('class="scene-break"') == 1
    assert xhtml.count('<img src="../images/fig.png"') == 1


def test_primeiro_paragrafo_depois_de_titulo_e_figura_sem_recuo():
    """O original fica entre o título e a tradução: o "h1 + p" do CSS não pega mais."""
    xhtml = _bilingual()
    assert '<p id="bp1" class="first">' in xhtml
    assert '<p id="bp2">' in xhtml  # depois de parágrafo: com recuo
    assert '<p id="bp4" class="first">' in xhtml  # depois da figura
    assert "p.first" in DEFAULT_CSS


def test_estilo_escolhido_a_mao_nao_ganha_first():
    chapter = _chapter()
    chapter.blocks[1].meta["style"] = "paragraph"
    assert '<p id="bp1">' in _bilingual(chapter)


def test_nota_e_chamada_ligada_na_traducao():
    xhtml = _bilingual()
    # A chamada da tradução leva o link; a do original fica simples.
    assert '<p class="il-src" lang="en" xml:lang="en">See note<sup>1</sup>.</p>' in xhtml
    assert 'Veja a nota<sup><a epub:type="noteref" id="rfn" href="#nfn">1</a></sup>' in xhtml
    assert xhtml.count('id="rfn"') == 1
    note = re.search(r'<aside[^>]*id="nfn">(.*?)</aside>', xhtml).group(1)
    assert note == (
        f'<p class="il-src"{SRC}><span class="note-marker"><a href="#rfn">1</a></span> The note.</p>'
        "<p>A nota.</p>"
    )


def test_com_estilos_a_traducao_leva_a_classe_e_o_ajuste():
    document = Document(chapters=[_chapter()])
    document.styles["paragraph"] = {"props": {"indent": 2.0}}
    document.chapters[0].blocks[2].meta["overrides"] = {"space_after": 1.5}
    xhtml = _bilingual(document.chapters[0], styled=document)
    assert '<p id="bp1" class="st-first-paragraph first">' in xhtml
    assert f'<p class="il-src"{SRC}>See note' in xhtml  # o original sem classe de estilo
    assert '<p id="bp2" class="st-paragraph" style="margin-bottom: 1.5em">' in xhtml


def test_xhtml_bilingue_bem_formado_e_sem_marcador_sobrando():
    xhtml = _bilingual()
    assert "\x00" not in xhtml
    etree.fromstring(xhtml.encode("utf-8"))


def test_css_do_original():
    assert ".il-src {" in DEFAULT_CSS
    assert "table.il-src-table" in DEFAULT_CSS


# ---------------------------------------------------------------- EPUB


def test_epub_bilingue_declara_os_dois_idiomas(tmp_path):
    document = Document(language="pt-BR", source_language="en", chapters=[_chapter()])
    path = write_epub(document, tmp_path / "b.epub", layout="paragraph")
    with zipfile.ZipFile(path) as epub:
        opf = epub.read("OEBPS/content.opf").decode()
        xhtml = epub.read("OEBPS/text/ch0001.xhtml")
    assert re.findall(r"<dc:language>([^<]+)</dc:language>", opf) == ["pt-BR", "en"]
    root = etree.fromstring(xhtml)
    assert root.get("lang") == "pt-BR"
    assert b'class="il-src"' in xhtml


def test_epub_so_traducao_tem_um_idioma(tmp_path):
    document = Document(language="pt-BR", source_language="en", chapters=[_chapter()])
    path = write_epub(document, tmp_path / "t.epub", layout="translated")
    with zipfile.ZipFile(path) as epub:
        opf = epub.read("OEBPS/content.opf").decode()
    assert re.findall(r"<dc:language>([^<]+)</dc:language>", opf) == ["pt-BR"]


def test_formato_desconhecido_e_recusado(tmp_path):
    with pytest.raises(ValueError):
        write_epub(Document(chapters=[_chapter()]), tmp_path / "x.epub", layout="lixo")


# ---------------------------------------------------------------- rotas


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
    document = Document(language="pt-BR", source_language="en", chapters=[_chapter()])
    return get_store().create(document, source_kind="epub", source_filename="x.epub").id


def _put_translation(client, book, **extra):
    body = {"source_language": "en", "target_language": "pt-BR", **extra}
    return client.put(f"/api/projects/{book}/translation", json=body)


def test_formato_do_livro_e_gravado_e_mantido(client, book):
    assert (
        _put_translation(client, book, layout="paragraph").json()["project"]["translation"][
            "layout"
        ]
        == "paragraph"
    )
    # Sem "layout" no pedido, fica o que estava.
    assert _put_translation(client, book).json()["project"]["translation"]["layout"] == "paragraph"
    # Voltar a "só a tradução" tira a chave (é o padrão).
    assert (
        "layout"
        not in _put_translation(client, book, layout="translated").json()["project"]["translation"]
    )


def test_formato_invalido_no_livro_e_400(client, book):
    assert _put_translation(client, book, layout="lixo").status_code == 400


def test_exportacao_bilingue(client, book):
    response = client.get(f"/api/projects/{book}/export?layout=paragraph")
    assert response.status_code == 200
    assert (
        "bil%C3%ADngue" in response.headers["content-disposition"]
        or "bilíngue" in response.headers["content-disposition"]
    )


def test_exportacao_antiga_continua(client, book):
    response = client.get(f"/api/projects/{book}/export?translated=false")
    assert response.status_code == 200
    assert "original" in response.headers["content-disposition"]


def test_exportacao_com_formato_invalido_e_400(client, book):
    assert client.get(f"/api/projects/{book}/export?layout=lixo").status_code == 400


def test_previa_bilingue(client, book):
    html = client.get(f"/api/projects/{book}/preview/c1?layout=paragraph").text
    assert 'class="il-src"' in html
    assert (
        '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" lang="pt-BR"'
        in html
    )
    original = client.get(f"/api/projects/{book}/preview/c1?layout=original").text
    assert 'lang="en"' in original and "Capítulo Um" not in original
