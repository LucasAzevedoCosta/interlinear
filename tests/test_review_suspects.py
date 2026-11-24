"""Marcação de suspeitos (core/review.py) e a rota que a expõe."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.core.review import find_suspects  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402

LONG = "Texto " + " ".join(["corrido"] * 200) + "."


def doc(*chapters: list[Block]) -> Document:
    return Document(chapters=[Chapter(title=f"C{i}", blocks=b) for i, b in enumerate(chapters)])


def reasons(document: Document) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for suspect in find_suspects(document):
        found.setdefault(suspect.block_id, set()).add(suspect.reason)
    return found


def p(text: str, page: int | None = 1, kind: str = "paragraph") -> Block:
    return Block(kind=kind, text=text, page=page)


def test_livro_limpo_nao_tem_suspeitos():
    blocks = [
        p("Capítulo 1", kind="heading1"),
        p("Era uma vez um livro bem convertido."),
        p("— Disse ele, com <em>calma</em>:"),
        p("“Outra fala.”"),
        p(LONG),
    ]
    assert find_suspects(doc(blocks)) == []


def test_minuscula_no_comeco_e_fim_aberto_marcam_o_par():
    first = p("O parágrafo foi partido no meio da", page=1)
    second = p("frase pela quebra de página.", page=2)
    found = reasons(doc([first, second, p(LONG)]))
    assert found[first.id] == {"open_ending"}
    assert found[second.id] == {"lowercase_start"}


def test_minuscula_depois_de_travessao_e_aspas():
    block = p("— e então ele saiu.")
    quoted = p("“mas não voltou.”")
    found = reasons(doc([block, quoted, p(LONG)]))
    assert "lowercase_start" in found[block.id]
    assert "lowercase_start" in found[quoted.id]


def test_fim_aberto_atravessa_imagem_e_nota():
    first = p("Um parágrafo interrompido pela")
    blocks = [first, Block(kind="image", src="a.png"), p("1 Nota.", kind="footnote")]
    found = reasons(doc(blocks + [p("Figura que ficou no meio."), p(LONG)]))
    assert "open_ending" in found[first.id]


def test_fim_aberto_antes_de_titulo_ou_no_fim_nao_marca():
    before_heading = p("Uma epígrafe sem ponto")
    last = p("Último parágrafo sem ponto")
    blocks = [before_heading, p("Seção", kind="heading2"), p(LONG), last]
    found = reasons(doc(blocks))
    assert before_heading.id not in found
    assert last.id not in found


def test_titulo_longo():
    heading = p(" ".join(["muito"] * 19), kind="heading2")
    found = reasons(doc([heading, p(LONG)]))
    assert found[heading.id] == {"long_heading"}


def test_bloco_minusculo():
    tiny = p("7")
    found = reasons(doc([p(LONG), tiny, p(LONG)]))
    assert found[tiny.id] == {"tiny_block"}


def test_cabecalho_repetido_em_varias_paginas():
    heads = [p(f"{n} · O Nome do Livro", page=n) for n in (3, 5, 7)]
    blocks = [p(LONG, page=1)]
    for head in heads:
        blocks += [head, p(LONG, page=head.page)]
    found = reasons(doc(blocks))
    for head in heads:
        # Sem "fim aberto" junto: cabeçalho nunca termina em ponto.
        assert found[head.id] == {"repeated_text"}


def test_repeticao_na_mesma_pagina_ou_em_duas_nao_marca():
    same_page = [p("Nota do editor.", page=4) for _ in range(3)]
    two_pages = [p("Outra repetição.", page=n) for n in (8, 9)]
    found = reasons(doc([p(LONG), *same_page, *two_pages]))
    assert not any("repeated_text" in r for r in found.values())


def test_titulo_que_abre_capitulo_nao_conta_como_repetido():
    chapters = [[p(f"Capítulo {n}", page=n * 10, kind="heading1"), p(LONG)] for n in (1, 2, 3)]
    assert find_suspects(doc(*chapters)) == []


def test_capitulo_curto():
    short = [p("Dedicatória", kind="heading1"), p("Para alguém.")]
    document = doc(short, [p("Capítulo", kind="heading1"), p(LONG)])
    suspects = [s for s in find_suspects(document) if s.reason == "short_chapter"]
    assert [s.chapter_id for s in suspects] == [document.chapters[0].id]
    assert suspects[0].block_id == short[0].id


def test_livro_de_um_capitulo_so_nao_e_curto():
    assert find_suspects(doc([p("Pouco texto.")])) == []


# -- rota -------------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


def test_rota_devolve_os_suspeitos_e_some_depois_de_corrigir(client):
    first = p("Partido no meio da", page=1)
    second = p("frase.", page=2)
    document = doc([first, second, p(LONG)])
    meta = get_store().create(document, source_filename="livro.pdf")

    response = client.get(f"/api/projects/{meta.id}/suspects")
    assert response.status_code == 200
    suspects = response.json()["suspects"]
    assert {(s["block_id"], s["reason"]) for s in suspects} == {
        (first.id, "open_ending"),
        (second.id, "lowercase_start"),
    }
    assert all(s["chapter_id"] == document.chapters[0].id and s["message"] for s in suspects)

    client.post(f"/api/projects/{meta.id}/blocks/{first.id}/merge-next")
    assert client.get(f"/api/projects/{meta.id}/suspects").json()["suspects"] == []


def test_rota_de_projeto_inexistente(client):
    assert client.get("/api/projects/naoexiste/suspects").status_code == 404
