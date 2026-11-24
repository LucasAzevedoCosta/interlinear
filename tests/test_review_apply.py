"""Sugestões da revisão por IA: gravar, aceitar, rejeitar (ai/review_apply.py e rotas)."""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.ai import review_apply  # noqa: E402
from app.ai.base import Completion, Provider, ProviderConfig, ProviderError, Usage  # noqa: E402
from app.ai.reviewer import Proposal  # noqa: E402
from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402

LONG = "Texto " + " ".join(["corrido"] * 200) + "."


def book() -> tuple[Document, dict[str, Block]]:
    blocks = {
        "inicio": Block("paragraph", LONG, page=1),
        "partido": Block("paragraph", "O parágrafo foi partido no meio da", page=1),
        "resto": Block("paragraph", "frase pela quebra de página.", page=2),
        "numero": Block("paragraph", "23", page=2),
        "ocr": Block("paragraph", "lsso não era normal, pensou ela.", page=2, translated="Old"),
        "titulo": Block("heading2", " ".join(["Esta"] + ["frase"] * 20) + ".", page=3),
        "fim": Block("paragraph", LONG, page=3),
    }
    return Document(title="Livro", chapters=[Chapter("C1", list(blocks.values()))]), blocks


def proposal(document: Document, block: Block, op: str, confidence=0.9, **payload) -> Proposal:
    return Proposal(document.chapters[0].id, block.id, op, confidence, "motivo", [], payload)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    yield get_store()
    reset_caches()


def project(store, proposals_for) -> tuple[str, dict[str, Block], dict[str, str]]:
    """Cria o projeto e grava uma revisão. Devolve o id, os blocos e id da sugestão por nome."""
    document, blocks = book()
    meta = store.create(document, source_filename="l.pdf")
    proposals = proposals_for(document, blocks)
    store.update_review(meta.id, lambda r: review_apply.record_review(r, document, proposals))
    by_block = {s["block_id"]: s["id"] for s in store.load_review(meta.id)["suggestions"]}
    names = {name: by_block[b.id] for name, b in blocks.items() if b.id in by_block}
    return meta.id, blocks, names


def everything(document, b):
    return [
        proposal(document, b["partido"], "merge_next"),
        proposal(document, b["numero"], "delete", confidence=0.95),
        proposal(document, b["ocr"], "fix_text", text="Isso não era normal, pensou ela."),
        proposal(document, b["titulo"], "set_kind", confidence=0.6, kind="paragraph"),
    ]


# -- gravar -----------------------------------------------------------------


def test_record_grava_pendentes_com_impressao_digital(store):
    project_id, blocks, ids = project(store, everything)
    review = store.load_review(project_id)
    assert [s["status"] for s in review["suggestions"]] == ["pending"] * 4
    first = review["suggestions"][0]
    assert first["block_id"] == blocks["partido"].id and first["op"] == "merge_next"
    assert first["fingerprint"] and first["decided_by"] == ""
    assert review["reviewed_at"]


def test_revisao_nova_troca_pendentes_e_nao_repete_rejeitadas(store):
    project_id, blocks, ids = project(store, everything)
    review_apply.decide(store, project_id, [ids["numero"]], accept=False)

    document = store.load_document(project_id)
    again = everything(document, blocks)
    counts = store.update_review(
        project_id, lambda r: review_apply.record_review(r, document, again)
    )
    assert counts == {"new": 3, "skipped": 1}
    statuses = [(s["block_id"], s["status"]) for s in store.load_review(project_id)["suggestions"]]
    assert statuses.count((blocks["numero"].id, "rejected")) == 1
    assert len(statuses) == 4  # 1 rejeitada (histórico) + 3 pendentes novas


# -- aceitar ----------------------------------------------------------------


def test_aceitar_aplica_cada_operacao(store):
    project_id, blocks, ids = project(store, everything)
    outcome = review_apply.decide(store, project_id, list(ids.values()), accept=True)
    assert sorted(outcome["accepted"]) == sorted(ids.values()) and outcome["stale"] == []

    document = store.load_document(project_id)
    texts = [b.text for b in document.all_blocks()]
    assert "O parágrafo foi partido no meio da frase pela quebra de página." in texts
    assert "23" not in texts
    fixed = next(b for b in document.all_blocks() if b.id == blocks["ocr"].id)
    assert fixed.text.startswith("Isso") and fixed.meta.get("stale") is True  # tradução antiga
    titulo = next(b for b in document.all_blocks() if b.id == blocks["titulo"].id)
    assert titulo.kind == "paragraph"
    review = store.load_review(project_id)
    assert {s["status"] for s in review["suggestions"]} == {"accepted"}
    assert {s["decided_by"] for s in review["suggestions"]} == {"user"}


def test_merge_prev_vira_juntar_o_anterior(store):
    project_id, blocks, ids = project(store, lambda d, b: [proposal(d, b["resto"], "merge_prev")])
    review_apply.decide(store, project_id, [ids["resto"]], accept=True)
    texts = [b.text for b in store.load_document(project_id).all_blocks()]
    assert "O parágrafo foi partido no meio da frase pela quebra de página." in texts


def test_capitulos(store):
    def proposals(document, b):
        return [proposal(document, b["titulo"], "start_chapter")]

    project_id, blocks, ids = project(store, proposals)
    review_apply.decide(store, project_id, [ids["titulo"]], accept=True)
    document = store.load_document(project_id)
    assert len(document.chapters) == 2 and document.chapters[1].blocks[0].id == blocks["titulo"].id

    # E o caminho de volta: fundir o capítulo novo no anterior.
    later = Proposal(document.chapters[1].id, blocks["titulo"].id, "merge_chapter", 0.9, "")
    store.update_review(project_id, lambda r: review_apply.record_review(r, document, [later]))
    pending = review_apply.select(store.load_review(project_id))
    review_apply.decide(store, project_id, pending, accept=True)
    assert len(store.load_document(project_id).chapters) == 1


def test_texto_editado_depois_da_revisao_fica_desatualizada(store):
    project_id, blocks, ids = project(store, everything)

    def edit(document):
        target = next(b for b in document.all_blocks() if b.id == blocks["ocr"].id)
        target.text = "Isso não era normal, pensou ela, já na varanda."

    store.update(project_id, edit)
    outcome = review_apply.decide(store, project_id, [ids["ocr"]], accept=True)
    assert outcome["accepted"] == []
    assert outcome["stale"] == [
        {"id": ids["ocr"], "problem": "o texto do bloco mudou depois da revisão"}
    ]
    # A edição do usuário venceu.
    text = next(b for b in store.load_document(project_id).all_blocks() if b.id == blocks["ocr"].id)
    assert text.text.endswith("já na varanda.")


def test_conflito_entre_aceitas_e_resolvido_na_ordem_do_livro(store):
    # "Apagar o resto" e "juntar o partido com o resto", aceitas juntas. A
    # ordem é a do livro: a junção (no partido, que vem antes) é aplicada
    # primeiro, e o "resto" deixa de existir; o apagar fica desatualizado em
    # vez de apagar outra coisa.
    def proposals(document, b):
        return [
            proposal(document, b["resto"], "delete"),
            proposal(document, b["partido"], "merge_next"),
        ]

    project_id, blocks, ids = project(store, proposals)
    outcome = review_apply.decide(store, project_id, [ids["resto"], ids["partido"]], accept=True)
    assert outcome["accepted"] == [ids["partido"]]
    assert outcome["stale"][0]["id"] == ids["resto"]
    assert "não existe" in outcome["stale"][0]["problem"]


def test_ok_dispensa_o_suspeito_enquanto_o_texto_for_o_mesmo(store):
    project_id, blocks, ids = project(store, lambda d, b: [proposal(d, b["numero"], "ok")])
    review_apply.decide(store, project_id, [ids["numero"]], accept=True)
    document = store.load_document(project_id)
    review = store.load_review(project_id)
    assert review_apply.dismissed_blocks(document, review) == {blocks["numero"].id}

    def edit(document):
        next(b for b in document.all_blocks() if b.id == blocks["numero"].id).text = "24"

    store.update(project_id, edit)
    assert review_apply.dismissed_blocks(store.load_document(project_id), review) == set()


def test_decidir_so_vale_para_pendentes(store):
    project_id, _, ids = project(store, everything)
    review_apply.decide(store, project_id, [ids["numero"]], accept=False)
    outcome = review_apply.decide(store, project_id, [ids["numero"], "inventado"], accept=True)
    assert outcome == {"accepted": [], "rejected": [], "stale": [], "document": None}


def test_select_por_operacao_e_confianca(store):
    project_id, _, ids = project(store, everything)
    review = store.load_review(project_id)
    assert review_apply.select(review, op="delete") == [ids["numero"]]
    assert set(review_apply.select(review, min_confidence=0.9)) == {
        ids["partido"],
        ids["numero"],
        ids["ocr"],
    }


def test_view_mostra_o_problema_das_pendentes(store):
    project_id, blocks, ids = project(store, everything)
    store.update(
        project_id,
        lambda d: d.chapters[0].blocks.remove(
            next(b for b in d.chapters[0].blocks if b.id == blocks["numero"].id)
        ),
    )
    view = review_apply.view(store.load_document(project_id), store.load_review(project_id))
    problems = {s["id"]: s["problem"] for s in view}
    assert problems[ids["numero"]] == "o bloco não existe mais"
    assert problems[ids["partido"]] == ""


# -- rotas ------------------------------------------------------------------


class Scripted(Provider):
    def __init__(self, answer):
        super().__init__(ProviderConfig(kind="fake"))
        self.answer = answer
        self.calls = 0

    async def complete(self, messages, *, client) -> Completion:
        self.calls += 1
        targets = re.findall(r"^TRECHO \([^)]*\): (.*)$", messages[-1].content, re.MULTILINE)
        result = self.answer(targets)
        if isinstance(result, Exception):
            raise result
        return Completion(text=result, usage=Usage(1, 1))

    async def list_models(self, *, client):
        return []


def good_reply(targets):
    ops = {"23": "delete", "frase pela quebra de página.": "merge_prev"}
    items = []
    for n, text in enumerate(targets, start=1):
        if text.startswith("lsso"):
            items.append({"n": n, "op": "fix_text", "text": "I" + text[1:], "confidence": 0.9})
        elif text.startswith("Esta frase"):
            items.append({"n": n, "op": "set_kind", "kind": "paragraph", "confidence": 0.6})
        else:
            items.append({"n": n, "op": ops.get(text, "merge_next"), "confidence": 0.95})
    return json.dumps(items)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


@pytest.fixture
def fake(monkeypatch):
    from app.routes import review as review_routes

    holder = SimpleNamespace(provider=Scripted(good_reply))
    profile = SimpleNamespace(label="Falso", model="falso-1")
    monkeypatch.setattr(review_routes, "_resolve_provider", lambda *_: (holder.provider, profile))
    return holder


def wait(client, project_id, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/projects/{project_id}/review").json()["job"]
        if job and job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("A revisão não terminou")


def test_rota_revisa_aceita_em_grupo_e_some_dos_suspeitos(client, fake):
    document, blocks = book()
    project_id = get_store().create(document, source_filename="l.pdf").id

    started = client.post(f"/api/projects/{project_id}/review").json()
    assert started["job"]["status"] == "running" and started["model"] == "falso-1"
    job = wait(client, project_id)
    assert job["status"] == "done" and job["note"] == "4 sugestões"

    data = client.get(f"/api/projects/{project_id}/review").json()
    assert data["provider"] == "Falso"
    ops = sorted(s["op"] for s in data["suggestions"])
    # A junção veio pelos dois lados; ficou uma.
    assert ops == ["delete", "fix_text", "merge_next", "set_kind"]

    outcome = client.post(
        f"/api/projects/{project_id}/review/accept-all", json={"min_confidence": 0.9}
    ).json()
    assert len(outcome["accepted"]) == 3 and outcome["project"]["word_count"] > 0
    texts = [b["text"] for c in outcome["document"]["chapters"] for b in c["blocks"]]
    assert "23" not in texts

    # O título longo (0,6) continua pendente; rejeitar.
    pending = [
        s
        for s in client.get(f"/api/projects/{project_id}/review").json()["suggestions"]
        if s["status"] == "pending"
    ]
    assert [s["op"] for s in pending] == ["set_kind"]
    client.post(f"/api/projects/{project_id}/review/reject", json={"ids": [pending[0]["id"]]})

    # Os suspeitos corrigidos sumiram; sobra o título rejeitado.
    suspects = client.get(f"/api/projects/{project_id}/suspects").json()["suspects"]
    assert [s["reason"] for s in suspects] == ["long_heading"]


def test_rota_ok_aceito_tira_o_suspeito(client, fake):
    fake.provider = Scripted(
        lambda targets: json.dumps([{"n": n, "op": "ok"} for n in range(1, len(targets) + 1)])
    )
    document, _ = book()
    project_id = get_store().create(document, source_filename="l.pdf").id
    client.post(f"/api/projects/{project_id}/review")
    wait(client, project_id)
    client.post(f"/api/projects/{project_id}/review/accept-all", json={"op": "ok"})
    assert client.get(f"/api/projects/{project_id}/suspects").json()["suspects"] == []
    # Nada mais a revisar: não chama o modelo.
    calls = fake.provider.calls
    response = client.post(f"/api/projects/{project_id}/review").json()
    assert response["job"] is None and fake.provider.calls == calls


def test_rota_erro_do_provedor_vira_erro_da_tarefa(client, fake):
    fake.provider = Scripted(lambda t: ProviderError("chave inválida", status=401))
    document, _ = book()
    project_id = get_store().create(document, source_filename="l.pdf").id
    client.post(f"/api/projects/{project_id}/review")
    job = wait(client, project_id)
    assert job["status"] == "error" and "chave" in job["error"]
    assert client.get(f"/api/projects/{project_id}/review").json()["suggestions"] == []


def test_rota_validacoes(client, fake):
    document, _ = book()
    project_id = get_store().create(document, source_filename="l.pdf").id
    base = f"/api/projects/{project_id}/review"
    assert client.post(f"{base}/accept", json={"ids": "x"}).status_code == 400
    assert client.post(f"{base}/accept-all", json={"op": "rewrite"}).status_code == 400
    assert client.post(f"{base}/stop").status_code == 404
    assert client.get("/api/projects/naoexiste/review").status_code == 404
    assert client.post("/api/projects/naoexiste/review").status_code == 404
