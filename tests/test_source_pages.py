"""O arquivo original na bancada: posição dos blocos no PDF e imagem da página."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.core import editing  # noqa: E402
from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.core.page_image import page_sizes, render_page  # noqa: E402
from app.core.pdf_extract import extract_blocks  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


@pytest.fixture(scope="module")
def extracted(livro_pdf: Path):
    blocks, _ = extract_blocks(str(livro_pdf))
    return blocks


@pytest.fixture
def project_id(client, livro_pdf: Path) -> str:
    response = client.post(
        "/api/projects",
        files={"file": ("livro.pdf", livro_pdf.read_bytes(), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return response.json()["project"]["id"]


# --------------------------------------------------------- meta["boxes"]


def test_todo_bloco_de_texto_sabe_onde_esta_no_pdf(extracted, livro_pdf):
    sizes = page_sizes(livro_pdf)
    texto = [b for b in extracted if b.kind != "image"]
    assert texto
    for block in texto:
        boxes = block.meta.get("boxes")
        assert boxes, f"sem posição: {block.text[:40]!r}"
        for page, x0, top, x1, bottom in boxes:
            width, height = sizes[page - 1]
            assert 0 <= x0 < x1 <= width + 1, (block.text[:40], x0, x1, width)
            assert 0 <= top < bottom <= height + 1, (block.text[:40], top, bottom, height)


def test_primeiro_retangulo_e_da_pagina_do_bloco(extracted):
    for block in extracted:
        assert block.meta["boxes"][0][0] == block.page


def test_titulo_fica_acima_do_paragrafo_seguinte(extracted):
    titulo = next(b for b in extracted if b.kind == "heading1")
    seguinte = extracted[extracted.index(titulo) + 1]
    assert seguinte.meta["boxes"][0][0] == titulo.meta["boxes"][0][0]
    assert titulo.meta["boxes"][0][4] <= seguinte.meta["boxes"][0][2]


def test_juntar_blocos_soma_os_retangulos():
    first = Block(kind="paragraph", text="Uma frase par-", meta={"boxes": [[1, 50, 700, 300, 712]]})
    second = Block(kind="paragraph", text="tida.", meta={"boxes": [[2, 50, 60, 120, 72]]})
    document = Document(chapters=[Chapter(title="Um", blocks=[first, second])])
    _, merged = editing.merge_with_next(document, first.id)
    assert merged.meta["boxes"] == [[1, 50, 700, 300, 712], [2, 50, 60, 120, 72]]


def test_juntar_blocos_sem_posicao_nao_cria_a_chave():
    document = Document(
        chapters=[
            Chapter(
                title="Um",
                blocks=[Block(kind="paragraph", text="a"), Block(kind="paragraph", text="b")],
            )
        ]
    )
    _, merged = editing.merge_with_next(document, document.chapters[0].blocks[0].id)
    assert "boxes" not in merged.meta


# ------------------------------------------------------------ a imagem


def test_render_colorido_e_rgb(livro_pdf):
    png = render_page(livro_pdf, 1, dpi=72, grayscale=False)
    assert png.startswith(b"\x89PNG")
    assert png[25] == 2  # tipo de cor 2: RGB


def test_render_padrao_continua_cinza(livro_pdf):
    """A revisão visual por IA usa o padrão e não deve mudar."""
    png = render_page(livro_pdf, 1, dpi=72)
    assert png[25] == 0  # tipo de cor 0: tons de cinza


# -------------------------------------------------------------- as rotas


def test_source_devolve_o_tamanho_das_paginas(client, project_id, livro_pdf):
    response = client.get(f"/api/projects/{project_id}/source")
    assert response.status_code == 200
    data = response.json()
    assert data["kind"] == "pdf"
    assert data["pages"] == [list(size) for size in page_sizes(livro_pdf)]


def test_pagina_em_png_e_guardada_em_cache(client, project_id, tmp_path):
    response = client.get(f"/api/projects/{project_id}/source/pages/1?dpi=72")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG")
    cache = tmp_path / "projects" / project_id / "cache" / "pages" / "1-72.png"
    assert cache.read_bytes() == response.content
    assert not list(cache.parent.glob("*.tmp"))

    again = client.get(f"/api/projects/{project_id}/source/pages/1?dpi=72")
    assert again.content == response.content


def test_pagina_inexistente_e_404(client, project_id):
    assert client.get(f"/api/projects/{project_id}/source/pages/99").status_code == 404
    assert client.get(f"/api/projects/{project_id}/source/pages/0").status_code == 404


def test_resolucao_fora_da_lista_e_recusada(client, project_id, tmp_path):
    response = client.get(f"/api/projects/{project_id}/source/pages/1?dpi=600")
    assert response.status_code == 400
    assert not (tmp_path / "projects" / project_id / "cache").exists()


def test_projeto_de_epub_nao_tem_paginas(client):
    document = Document(chapters=[Chapter(title="Um", blocks=[Block(kind="paragraph", text="a")])])
    meta = get_store().create(document, source_kind="epub", source_filename="x.epub")
    assert client.get(f"/api/projects/{meta.id}/source").json() == {"kind": "epub", "pages": []}
    assert client.get(f"/api/projects/{meta.id}/source/pages/1").status_code == 404


def test_projeto_inexistente_e_404(client):
    assert client.get("/api/projects/naoexiste/source").status_code == 404
    assert client.get("/api/projects/naoexiste/source/pages/1").status_code == 404
