"""Testes da tarefa de tradução em segundo plano, com um provedor falso.

O provedor não sai para a rede: devolve "[pt] <texto>" para cada segmento e
pode ser segurado por um portão, para que o teste consiga editar o projeto
enquanto a tradução está no meio do caminho.
"""

from __future__ import annotations

import asyncio
import re
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.ai.base import (  # noqa: E402
    Completion,
    Provider,
    ProviderConfig,
    ProviderError,
    Usage,
)
from app.ai.translator import MAX_CONSECUTIVE_FAILURES  # noqa: E402
from app.config import ProviderProfile  # noqa: E402
from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402
from app.routes import ai as ai_routes  # noqa: E402

_SEGMENT_RE = re.compile(r"⟦(\d+)⟧\n(.*?)(?=\n\n⟦|\Z)", re.S)


class FakeProvider(Provider):
    def __init__(self) -> None:
        super().__init__(ProviderConfig(kind="fake", model="fake"))
        self.gate = threading.Event()
        self.gate.set()
        self.calls = 0
        # A partir desta chamada (contando de 1), lança `error` em vez de responder.
        self.fail_from: int | None = None
        self.error = ProviderError("Incorrect API key", status=401)

    async def complete(self, messages, *, client) -> Completion:
        self.calls += 1
        while not self.gate.is_set():
            await asyncio.sleep(0.01)
        if self.fail_from is not None and self.calls >= self.fail_from:
            raise self.error
        body = messages[-1].content.split("Segmentos a traduzir:", 1)[-1]
        reply = "\n\n".join(
            f"⟦{n}⟧ [pt] {text.strip()}" for n, text in _SEGMENT_RE.findall(body)
        )
        return Completion(text=reply, usage=Usage(input_tokens=1, output_tokens=1))

    async def list_models(self, *, client) -> list[str]:
        return ["fake"]


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider()
    profile = ProviderProfile(id="fake", label="Falso", kind="fake", model="fake")
    monkeypatch.setattr(ai_routes, "_resolve_provider", lambda *_: (fake, profile))
    return fake


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


def _new_project(size: int = 4) -> tuple[str, list[str]]:
    blocks = [Block(kind="paragraph", text=f"Paragraph {i}.") for i in range(1, size + 1)]
    document = Document(title="Original", chapters=[Chapter(title="Um", blocks=blocks)])
    meta = get_store().create(document, source_kind="epub", source_filename="x.epub")
    return meta.id, [b.id for b in blocks]


def _wait_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("A tarefa não terminou a tempo")


def _start(client: TestClient, project_id: str) -> str:
    response = client.post(
        f"/api/projects/{project_id}/translate",
        json={"batch_chars": 1, "concurrency": 1},
    )
    assert response.status_code == 200, response.text
    return response.json()["job"]["id"]


def test_edicoes_durante_a_traducao_sobrevivem(client, provider):
    project_id, ids = _new_project()
    provider.gate.clear()
    job_id = _start(client, project_id)

    # Com a tradução parada no portão, o usuário edita de tudo um pouco.
    client.patch(
        f"/api/projects/{project_id}/blocks",
        json={"updates": [{"id": ids[0], "translated": "Minha versão."}]},
    ).raise_for_status()
    client.patch(f"/api/projects/{project_id}", json={"title": "Renomeado"}).raise_for_status()
    chapter_id = client.get(f"/api/projects/{project_id}").json()["document"]["chapters"][0]["id"]
    client.post(
        f"/api/projects/{project_id}/chapters/{chapter_id}/split",
        json={"block_id": ids[2]},
    ).raise_for_status()

    provider.gate.set()
    assert _wait_job(client, job_id)["status"] == "done"

    document = get_store().load_document(project_id)
    blocks = document.block_index()
    assert document.title == "Renomeado"
    assert len(document.chapters) == 2
    assert blocks[ids[0]].translated == "Minha versão."  # a edição vence
    assert blocks[ids[1]].translated == "[pt] Paragraph 2."
    assert blocks[ids[3]].translated == "[pt] Paragraph 4."


def test_tarefa_so_termina_depois_de_gravar(client, provider, monkeypatch):
    """Quem vê "done" e recarrega o projeto precisa encontrar a tradução no disco.

    A gravação final é atrasada de propósito: antes da correção, o status
    virava "done" antes dela, e o CI pegou essa janela por acaso.
    """
    project_id, ids = _new_project()
    store = get_store()
    original = store.update

    def slow_update(*args, **kwargs):
        time.sleep(0.3)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "update", slow_update)
    job_id = _start(client, project_id)

    assert _wait_job(client, job_id)["status"] == "done"
    blocks = store.load_document(project_id).block_index()
    assert [blocks[i].translated for i in ids] == [f"[pt] Paragraph {n}." for n in range(1, 5)]


def test_apagar_projeto_durante_a_traducao_nao_recria_a_pasta(client, provider):
    project_id, _ = _new_project()
    provider.gate.clear()
    job_id = _start(client, project_id)

    client.delete(f"/api/projects/{project_id}").raise_for_status()
    provider.gate.set()
    job = _wait_job(client, job_id)

    assert not get_store().project_dir(project_id).exists()
    # A tarefa foi cancelada no DELETE: nenhum lote além do que estava no portão.
    assert job["status"] == "stopped"
    time.sleep(0.1)
    assert provider.calls == 1


def test_chave_invalida_interrompe_e_marca_erro(client, provider):
    project_id, ids = _new_project(size=10)
    provider.fail_from = 3  # dois lotes passam, o terceiro recebe 401

    job = _wait_job(client, _start(client, project_id))

    assert job["status"] == "error"
    assert "Incorrect API key" in job["error"]
    assert provider.calls == 3  # nenhum lote enviado depois do 401
    blocks = get_store().load_document(project_id).block_index()
    assert blocks[ids[0]].translated == "[pt] Paragraph 1."  # o já feito fica salvo
    assert blocks[ids[1]].translated == "[pt] Paragraph 2."
    assert blocks[ids[2]].translated is None


def test_lotes_seguidos_falhando_interrompem(client, provider):
    project_id, _ = _new_project(size=12)
    provider.fail_from = 1
    # 400 não é fatal sozinho (pode ser um trecho problemático), mas em sequência é.
    provider.error = ProviderError("max_tokens inválido", status=400)

    job = _wait_job(client, _start(client, project_id))

    assert job["status"] == "error"
    assert "lotes seguidos falharam" in job["error"]
    assert "max_tokens inválido" in job["error"]
    assert provider.calls == MAX_CONSECUTIVE_FAILURES


def test_apply_translations_respeita_edicao_feita_depois_de_gravar():
    block = Block(kind="paragraph", text="Hello.", id="b1")
    initial = {"b1": None}
    written: dict[str, str | None] = {}

    on_disk = Document(chapters=[Chapter(title="", blocks=[Block(kind="paragraph", text="Hello.", id="b1")])])
    block.translated = "Olá."
    ai_routes._apply_translations(on_disk, [block], initial, written)
    assert on_disk.block_index()["b1"].translated == "Olá."

    # O usuário revisa a tradução já gravada; a próxima gravação não pode desfazer.
    on_disk.block_index()["b1"].translated = "Olá!"
    ai_routes._apply_translations(on_disk, [block], initial, written)
    assert on_disk.block_index()["b1"].translated == "Olá!"


def test_traduzir_refaz_so_o_que_ficou_desatualizado(client, provider):
    project_id, ids = _new_project()
    assert _wait_job(client, _start(client, project_id))["status"] == "done"
    calls = provider.calls

    client.patch(
        f"/api/projects/{project_id}/blocks",
        json={"updates": [{"id": ids[1], "text": "Paragraph 2, revised."}]},
    ).raise_for_status()
    assert _wait_job(client, _start(client, project_id))["status"] == "done"

    assert provider.calls == calls + 1  # um lote: só o bloco desatualizado
    blocks = get_store().load_document(project_id).block_index()
    assert blocks[ids[1]].translated == "[pt] Paragraph 2, revised."
    assert "stale" not in blocks[ids[1]].meta


def test_traducao_igual_a_anterior_tambem_tira_a_marca(client, provider):
    project_id, ids = _new_project(size=1)
    store = get_store()
    store.update(project_id, lambda d: setattr(d.all_blocks()[0], "translated", "[pt] Paragraph 1."))
    store.update(project_id, lambda d: d.all_blocks()[0].meta.update(stale=True))

    assert _wait_job(client, _start(client, project_id))["status"] == "done"
    block = store.load_document(project_id).block_index()[ids[0]]
    assert block.translated == "[pt] Paragraph 1."
    assert "stale" not in block.meta


def test_original_mudado_durante_a_traducao_continua_desatualizado(client, provider):
    project_id, ids = _new_project(size=1)
    provider.gate.clear()
    job_id = _start(client, project_id)

    client.patch(
        f"/api/projects/{project_id}/blocks",
        json={"updates": [{"id": ids[0], "text": "Paragraph 1, changed."}]},
    ).raise_for_status()
    provider.gate.set()
    assert _wait_job(client, job_id)["status"] == "done"

    block = get_store().load_document(project_id).block_index()[ids[0]]
    assert block.translated == "[pt] Paragraph 1."  # traduziu o texto antigo
    assert block.meta["stale"] is True
