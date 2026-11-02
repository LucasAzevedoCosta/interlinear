"""Edição de estrutura: apagar, juntar e inserir blocos."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402


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
    """Um capítulo com um parágrafo partido ao meio por uma figura."""
    blocks = [
        Block(kind="heading1", text="Um", id="h1"),
        Block(kind="paragraph", text="Era uma vez um exem-", translated="Once upon a", id="p1"),
        Block(kind="paragraph", text="plo partido.", translated="time.", id="p2"),
        Block(kind="image", src="fig.png", id="img"),
        Block(kind="paragraph", text="Fim.", id="p3"),
    ]
    document = Document(chapters=[Chapter(title="Um", blocks=blocks, id="c1")])
    meta = get_store().create(document, source_kind="epub", source_filename="x.epub")
    return meta.id


def _ids(project_id: str) -> list[str]:
    return [b.id for b in get_store().load_document(project_id).chapters[0].blocks]


def test_apagar_e_desfazer(client, book):
    response = client.delete(f"/api/projects/{book}/blocks/img")
    assert response.status_code == 200
    data = response.json()
    assert [b["id"] for b in data["chapter"]["blocks"]] == ["h1", "p1", "p2", "p3"]
    assert data["removed"]["index"] == 3
    assert data["project"]["id"] == book

    # Desfazer: o que a rota devolveu volta para o mesmo lugar, com o mesmo id.
    restored = client.post(f"/api/projects/{book}/chapters/c1/blocks", json=data["removed"])
    assert restored.status_code == 200
    assert _ids(book) == ["h1", "p1", "p2", "img", "p3"]


def test_restaurar_id_repetido_e_recusado(client, book):
    response = client.post(
        f"/api/projects/{book}/chapters/c1/blocks",
        json={"index": 0, "block": {"kind": "paragraph", "id": "p1"}},
    )
    assert response.status_code == 409


def test_juntar_desfaz_hifenizacao_e_mantem_traducao_completa(client, book):
    response = client.post(f"/api/projects/{book}/blocks/p1/merge-next")
    assert response.status_code == 200
    merged = response.json()["block"]

    assert merged["text"] == "Era uma vez um exemplo partido."
    assert merged["translated"] == "Once upon a time."
    assert merged["id"] not in {"p1", "p2"}  # id novo, ver o comentário na rota
    assert _ids(book) == ["h1", merged["id"], "img", "p3"]


def test_juntar_com_metade_sem_traducao_descarta_a_traducao(client, book):
    client.patch(
        f"/api/projects/{book}/blocks", json={"updates": [{"id": "p2", "translated": None}]}
    ).raise_for_status()

    merged = client.post(f"/api/projects/{book}/blocks/p1/merge-next").json()["block"]
    assert merged["translated"] is None


@pytest.mark.parametrize(
    ("block_id", "status"), [("h1", 400), ("p2", 400), ("p3", 400), ("nada", 404)]
)
def test_juntar_recusa_tipos_diferentes_fim_de_capitulo_e_bloco_inexistente(
    client, book, block_id, status
):
    # h1 → próximo é parágrafo; p2 → próximo é imagem; p3 → último do capítulo.
    response = client.post(f"/api/projects/{book}/blocks/{block_id}/merge-next")
    assert response.status_code == status
    assert _ids(book) == ["h1", "p1", "p2", "img", "p3"]  # nada mudou


def test_inserir_paragrafo_vazio_e_escrever_o_original(client, book):
    response = client.post(f"/api/projects/{book}/chapters/c1/blocks", json={"index": 4})
    block = response.json()["block"]
    assert block["kind"] == "paragraph"
    assert block["meta"] == {"inserted": True}
    assert _ids(book)[4] == block["id"]

    client.patch(
        f"/api/projects/{book}/blocks",
        json={"updates": [{"id": block["id"], "text": "Trecho que faltou."}]},
    ).raise_for_status()
    saved = get_store().load_document(book).block_index()[block["id"]]
    assert saved.text == "Trecho que faltou."


def test_inserir_fora_dos_limites_vai_para_a_ponta(client, book):
    block = client.post(
        f"/api/projects/{book}/chapters/c1/blocks", json={"index": 99}
    ).json()["block"]
    assert _ids(book)[-1] == block["id"]
