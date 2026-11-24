"""Fase 7: desfazer, modo automático e revisão ao terminar a importação."""

from __future__ import annotations

import copy
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
from app.ai.base import Completion, Provider, ProviderConfig, Usage  # noqa: E402
from app.ai.reviewer import Proposal  # noqa: E402
from app.config import Settings  # noqa: E402
from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.deps import get_settings_store, get_store, reset_caches  # noqa: E402

LONG = "Texto " + " ".join(["corrido"] * 60) + "."


def book() -> tuple[Document, dict[str, Block]]:
    first = {
        "titulo": Block("heading1", "Capítulo 1", page=1),
        "partido": Block(
            "paragraph", "O parágrafo foi partido no meio da", page=1, translated="T1"
        ),
        "resto": Block("paragraph", "frase pela quebra de página.", page=2, translated="T2"),
        "numero": Block("paragraph", "23", page=2),
        "ocr": Block("paragraph", "lsso não era normal.", page=2, translated="Old"),
        # Mais de 18 palavras: a Fase 1 o marca como título longo.
        "longo": Block(
            "heading2",
            "Um título que na verdade é uma frase longa, com palavras demais para "
            "ser um título de verdade em qualquer livro.",
            page=3,
        ),
        "meio": Block("paragraph", LONG, page=3),
    }
    second = {"dedicatoria": Block("paragraph", "Para minha mãe.", page=4)}
    document = Document(
        title="Livro",
        chapters=[
            Chapter("C1", list(first.values())),
            Chapter("Dedicatória", list(second.values())),
        ],
    )
    return document, {**first, **second}


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    yield get_store()
    reset_caches()


def setup(store, make):
    document, blocks = book()
    project_id = store.create(document, source_filename="l.pdf").id
    original = store.load_document(project_id).to_dict()
    proposals = make(document, blocks)
    store.update_review(project_id, lambda r: review_apply.record_review(r, document, proposals))
    ids = [s["id"] for s in store.load_review(project_id)["suggestions"]]
    return project_id, blocks, ids, original


def p(document, chapter, block, op, confidence=0.95, **payload) -> Proposal:
    return Proposal(document.chapters[chapter].id, block.id, op, confidence, "", [], payload)


CASES = {
    "merge_next": lambda d, b: [p(d, 0, b["partido"], "merge_next")],
    "merge_prev": lambda d, b: [p(d, 0, b["resto"], "merge_prev")],
    "delete": lambda d, b: [p(d, 0, b["numero"], "delete")],
    "delete_primeiro": lambda d, b: [p(d, 1, b["dedicatoria"], "delete")],
    "fix_text": lambda d, b: [p(d, 0, b["ocr"], "fix_text", text="Isso não era normal.")],
    "set_kind": lambda d, b: [p(d, 0, b["longo"], "set_kind", kind="paragraph")],
    "insert_after": lambda d, b: [
        p(d, 0, b["numero"], "insert_after", text="Uma linha que a extração perdeu.")
    ],
    "start_chapter": lambda d, b: [p(d, 0, b["longo"], "start_chapter")],
    "merge_chapter": lambda d, b: [p(d, 1, b["dedicatoria"], "merge_chapter")],
    "ok": lambda d, b: [p(d, 0, b["numero"], "ok")],
}


@pytest.mark.parametrize("name", CASES)
def test_aplicar_e_desfazer_devolve_o_livro_identico(store, name):
    project_id, _, ids, original = setup(store, CASES[name])
    applied = review_apply.decide(store, project_id, ids, accept=True)
    assert applied["accepted"] == ids, applied
    if name != "ok":
        assert store.load_document(project_id).to_dict() != original

    outcome = review_apply.undo(store, project_id, ids)
    assert outcome["undone"] == ids and outcome["failed"] == []
    assert store.load_document(project_id).to_dict() == original
    suggestion = store.load_review(project_id)["suggestions"][0]
    assert suggestion["status"] == "pending" and "undo" not in suggestion
    assert store.load_review(project_id)["dismissed"] == []
    # Desfeita, volta a valer: dá para aceitar de novo.
    assert review_apply.decide(store, project_id, ids, accept=True)["accepted"] == ids


def test_desfazer_varias_volta_na_ordem_contraria(store):
    def make(d, b):
        return [
            p(d, 0, b["partido"], "merge_next"),
            p(d, 0, b["numero"], "delete"),
            p(d, 0, b["ocr"], "fix_text", text="Isso não era normal."),
            p(d, 0, b["longo"], "start_chapter"),
        ]

    project_id, _, ids, original = setup(store, make)
    assert len(review_apply.decide(store, project_id, ids, accept=True)["accepted"]) == 4
    outcome = review_apply.undo(store, project_id, ids)
    assert sorted(outcome["undone"]) == sorted(ids)
    assert store.load_document(project_id).to_dict() == original


def test_nao_desfaz_por_cima_de_edicao_do_usuario(store):
    project_id, blocks, ids, _ = setup(store, CASES["fix_text"])
    review_apply.decide(store, project_id, ids, accept=True)

    def edit(document):
        target = next(x for x in document.all_blocks() if x.id == blocks["ocr"].id)
        target.text = "Isso não era normal, pensou ela."

    store.update(project_id, edit)
    outcome = review_apply.undo(store, project_id, ids)
    assert outcome["undone"] == [] and "editado" in outcome["failed"][0]["problem"]
    text = next(x for x in store.load_document(project_id).all_blocks() if x.id == blocks["ocr"].id)
    assert text.text == "Isso não era normal, pensou ela."
    assert store.load_review(project_id)["suggestions"][0]["status"] == "accepted"


def test_sugestao_aceita_sem_registro_nao_desfaz(store):
    project_id, _, ids, _ = setup(store, CASES["delete"])
    review_apply.decide(store, project_id, ids, accept=True)
    store.update_review(project_id, lambda r: r["suggestions"][0].pop("undo"))
    outcome = review_apply.undo(store, project_id, ids)
    assert "sem o registro" in outcome["failed"][0]["problem"]


def test_view_diz_o_que_da_para_desfazer_sem_mandar_o_registro(store):
    project_id, _, ids, _ = setup(store, CASES["delete"])
    review_apply.decide(store, project_id, ids, accept=True)
    (item,) = review_apply.view(store.load_document(project_id), store.load_review(project_id))
    assert item["undoable"] is True and "undo" not in item and "seq" not in item


# -- modo automático --------------------------------------------------------


def test_auto_apply_so_o_que_e_de_baixo_risco_e_acima_do_limite(store):
    def make(d, b):
        return [
            p(d, 0, b["partido"], "merge_next", confidence=0.95),
            p(d, 0, b["numero"], "delete", confidence=0.95),
            p(d, 0, b["ocr"], "fix_text", confidence=0.85, text="Isso não era normal."),
            p(d, 0, b["longo"], "set_kind", confidence=0.99, kind="paragraph"),
            p(d, 1, b["dedicatoria"], "merge_chapter", confidence=0.99),
        ]

    project_id, blocks, ids, _ = setup(store, make)
    outcome = review_apply.auto_apply(store, project_id, threshold=0.9, source="text")
    review = {s["block_id"]: s for s in store.load_review(project_id)["suggestions"]}
    assert len(outcome["accepted"]) == 2
    assert review[blocks["partido"].id]["decided_by"] == "auto"
    assert review[blocks["numero"].id]["status"] == "accepted"
    # Abaixo do limite, e operações que mudam a estrutura: esperam o usuário.
    for name in ("ocr", "longo", "dedicatoria"):
        assert review[blocks[name].id]["status"] == "pending"


def test_auto_apply_nao_mexe_nas_pendentes_da_outra_revisao(store):
    project_id, _, _, _ = setup(store, CASES["delete"])
    outcome = review_apply.auto_apply(store, project_id, threshold=0.9, source="visual")
    assert outcome["accepted"] == []


def test_preferencias_do_modo(tmp_path, monkeypatch):
    assert Settings.from_dict({}).review_mode == "suggest"
    loaded = Settings.from_dict({"review_mode": "x", "review_threshold": 3})
    assert (loaded.review_mode, loaded.review_threshold) == ("suggest", 1.0)
    assert Settings.from_dict({"review_threshold": 0.1}).review_threshold == 0.5
    assert Settings.from_dict({"review_threshold": "abc"}).review_threshold == 0.9


# -- rotas ------------------------------------------------------------------


class Scripted(Provider):
    def __init__(self):
        super().__init__(ProviderConfig(kind="fake"))
        self.calls = 0

    async def complete(self, messages, *, client) -> Completion:
        self.calls += 1
        targets = re.findall(r"^TRECHO \([^)]*\): (.*)$", messages[-1].content, re.MULTILINE)
        items = []
        for n, text in enumerate(targets, start=1):
            if text == "23":
                items.append({"n": n, "op": "delete", "confidence": 0.95})
            elif text.endswith("meio da"):
                items.append({"n": n, "op": "merge_next", "confidence": 0.95})
            elif text.startswith("Um título"):
                items.append({"n": n, "op": "set_kind", "kind": "paragraph", "confidence": 0.99})
            else:
                items.append({"n": n, "op": "ok", "confidence": 0.7})
        return Completion(text=json.dumps(items), usage=Usage(1, 1))

    async def list_models(self, *, client):
        return []


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app
    from app.routes import review as review_routes

    fake = Scripted()
    profile = SimpleNamespace(label="Falso", model="falso-1")
    monkeypatch.setattr(review_routes, "_resolve_provider", lambda *_: (fake, profile))
    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        test_client.fake = fake
        yield test_client
    reset_caches()


def wait(client, project_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/projects/{project_id}/review").json()["job"]
        if job and job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("A revisão não terminou")


def test_rota_de_preferencias(client):
    response = client.put("/api/settings", json={"review_mode": "auto", "review_threshold": 0.8})
    settings = response.json()["settings"]
    assert (settings["review_mode"], settings["review_threshold"]) == ("auto", 0.8)
    assert client.put("/api/settings", json={"review_mode": "sempre"}).status_code == 400


def test_modo_automatico_aplica_no_fim_e_desfaz_pela_rota(client):
    client.put("/api/settings", json={"review_mode": "auto", "review_threshold": 0.9})
    document, blocks = book()
    original = copy.deepcopy(document.to_dict())
    project_id = get_store().create(document, source_filename="l.pdf").id

    client.post(f"/api/projects/{project_id}/review")
    job = wait(client, project_id)
    assert job["status"] == "done"
    assert "2 aplicadas automaticamente" in job["note"], job["note"]

    suggestions = client.get(f"/api/projects/{project_id}/review").json()["suggestions"]
    auto = [s for s in suggestions if s["decided_by"] == "auto"]
    assert sorted(s["op"] for s in auto) == ["delete", "merge_next"]
    assert all(s["undoable"] for s in auto)
    # O título longo (0,99) ficou como sugestão: trocar o tipo não é automático.
    assert [s["op"] for s in suggestions if s["status"] == "pending" and s["op"] != "ok"] == [
        "set_kind"
    ]

    outcome = client.post(
        f"/api/projects/{project_id}/review/undo", json={"ids": [s["id"] for s in auto]}
    ).json()
    assert len(outcome["undone"]) == 2 and outcome["project"]
    assert get_store().load_document(project_id).to_dict() == original


def test_modo_sugestao_nao_aplica_nada(client):
    document, _ = book()
    project_id = get_store().create(document, source_filename="l.pdf").id
    client.post(f"/api/projects/{project_id}/review")
    job = wait(client, project_id)
    assert "automaticamente" not in job["note"]
    statuses = {
        s["status"] for s in client.get(f"/api/projects/{project_id}/review").json()["suggestions"]
    }
    assert statuses == {"pending"}


def test_revisa_sozinho_ao_terminar_a_importacao(client, livro_pdf):
    client.put("/api/settings", json={"review_after_import": True})
    started = client.post(
        "/api/imports", files={"file": ("livro.pdf", livro_pdf.read_bytes(), "application/pdf")}
    ).json()["import"]
    deadline = time.time() + 20
    while time.time() < deadline:
        job = client.get(f"/api/imports/{started['id']}").json()["import"]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done", job
    review_job = wait(client, job["project_id"])
    assert review_job["status"] == "done" and client.fake.calls >= 1


def test_sem_a_preferencia_a_importacao_nao_revisa(client, livro_pdf):
    started = client.post(
        "/api/imports", files={"file": ("livro.pdf", livro_pdf.read_bytes(), "application/pdf")}
    ).json()["import"]
    deadline = time.time() + 20
    while time.time() < deadline:
        job = client.get(f"/api/imports/{started['id']}").json()["import"]
        if job["status"] != "running":
            break
        time.sleep(0.05)
    assert job["status"] == "done"
    assert client.get(f"/api/projects/{job['project_id']}/review").json()["job"] is None
    assert client.fake.calls == 0
    assert get_settings_store().load().review_after_import is False
