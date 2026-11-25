"""Espaçamento: separador de cena, linha em branco e quebra de página inseridos."""

from __future__ import annotations

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
from app.core.epub_write import DEFAULT_CSS, chapter_to_xhtml, write_epub  # noqa: E402
from app.core.review import find_suspects  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402


def _chapter() -> Chapter:
    return Chapter(
        title="Um",
        id="c1",
        blocks=[
            Block(kind="heading1", text="Um", id="h1"),
            Block(kind="paragraph", text="Antes da troca de cena.", id="p1"),
            Block(kind="scene_break", text="* * *", id="sb"),
            Block(kind="paragraph", text="Depois da troca.", id="p2"),
            Block(kind="spacer", id="sp"),
            Block(kind="paragraph", text="Depois do respiro.", id="p3"),
            Block(kind="page_break", id="pb"),
            Block(kind="paragraph", text="Página nova.", id="p4"),
        ],
    )


# ------------------------------------------------------------------ XHTML


def test_separador_linha_em_branco_e_quebra_no_xhtml():
    xhtml = chapter_to_xhtml(_chapter())
    assert '<p id="bsb" class="scene-break">* * *</p>' in xhtml
    assert '<p id="bsp" class="spacer">&#160;</p>' in xhtml
    assert '<hr class="page-break"/>' in xhtml
    etree.fromstring(xhtml.encode("utf-8"))


def test_sinal_do_separador_e_escapado_e_tem_padrao():
    chapter = _chapter()
    chapter.blocks[2].text = "<b>~</b>"
    assert "&lt;b&gt;~&lt;/b&gt;" in chapter_to_xhtml(chapter)
    chapter.blocks[2].text = "   "
    assert '<p id="bsb" class="scene-break">* * *</p>' in chapter_to_xhtml(chapter)


def test_css_padrao_centraliza_o_separador_e_tira_o_recuo_depois():
    assert "p.scene-break {" in DEFAULT_CSS
    assert "p.scene-break + p, p.spacer + p { text-indent: 0; }" in DEFAULT_CSS


def test_com_estilos_paragrafo_depois_da_troca_de_cena_e_primeiro_paragrafo():
    document = Document(chapters=[_chapter()])
    document.styles["paragraph"] = {"props": {"indent": 2.0}}
    xhtml = chapter_to_xhtml(document.chapters[0], styled=document)
    assert 'id="bp2" class="st-first-paragraph"' in xhtml  # depois do separador
    assert 'id="bp3" class="st-first-paragraph"' in xhtml  # depois da linha em branco
    assert 'id="bp4" class="st-paragraph"' in xhtml  # quebra de página não muda
    assert styles.default_style("paragraph", "scene_break") == "first_paragraph"


def test_separador_nao_e_traduzido_nem_conta_palavras():
    document = Document(chapters=[_chapter()])
    kinds = {b.kind for b in document.translatable_blocks()}
    assert "scene_break" not in kinds and "spacer" not in kinds
    assert document.word_count() == len(
        "Um Antes da troca de cena. Depois da troca. Depois do respiro. Página nova.".split()
    )


def test_capitulo_curto_nao_conta_os_asteriscos():
    curto = Chapter(
        title="Dois",
        blocks=[Block(kind="paragraph", text="uma duas"), Block(kind="scene_break", text="* * *")],
    )
    document = Document(chapters=[_chapter(), curto])
    detail = next(
        s.detail
        for s in find_suspects(document)
        if s.reason == "short_chapter" and s.chapter_id == curto.id
    )
    assert detail == "2 palavras"


def test_exportacao_com_os_tipos_novos(tmp_path):
    path = write_epub(Document(chapters=[_chapter()]), tmp_path / "x.epub", use_translation=False)
    with zipfile.ZipFile(path) as epub:
        etree.fromstring(epub.read("OEBPS/text/ch0001.xhtml"))


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
    document = Document(
        chapters=[Chapter(title="Um", id="c1", blocks=[Block(kind="paragraph", text="a", id="p1")])]
    )
    return get_store().create(document, source_kind="epub", source_filename="x.epub").id


@pytest.mark.parametrize(
    ("kind", "text"),
    [("scene_break", "* * *"), ("spacer", ""), ("page_break", ""), ("paragraph", "")],
)
def test_rota_insere_cada_tipo(client, book, kind, text):
    response = client.post(
        f"/api/projects/{book}/chapters/c1/blocks", json={"index": 1, "kind": kind}
    )
    assert response.status_code == 200, response.text
    block = response.json()["block"]
    assert (block["kind"], block["text"]) == (kind, text)
    assert block["meta"] == {"inserted": True}
    assert [b.kind for b in get_store().load_document(book).chapters[0].blocks] == [
        "paragraph",
        kind,
    ]


def test_sem_tipo_continua_inserindo_paragrafo(client, book):
    response = client.post(f"/api/projects/{book}/chapters/c1/blocks", json={"index": 0})
    assert response.json()["block"]["kind"] == "paragraph"


def test_tipo_que_nao_se_insere_e_recusado(client, book):
    for kind in ("image", "footnote", "heading1", "lixo"):
        response = client.post(
            f"/api/projects/{book}/chapters/c1/blocks", json={"index": 0, "kind": kind}
        )
        assert response.status_code == 400, kind


def test_sinal_do_separador_muda_pelo_patch(client, book):
    created = client.post(
        f"/api/projects/{book}/chapters/c1/blocks", json={"index": 1, "kind": "scene_break"}
    ).json()["block"]
    client.patch(
        f"/api/projects/{book}/blocks", json={"updates": [{"id": created["id"], "text": "⁂"}]}
    )
    html = client.get(f"/api/projects/{book}/preview/c1?translated=false").text
    assert 'class="scene-break">⁂</p>' in html
