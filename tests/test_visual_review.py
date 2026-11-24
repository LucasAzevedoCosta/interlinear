"""Revisão visual: imagem da página, imagem em cada provedor, insert_after, rota."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.ai import review_apply  # noqa: E402
from app.ai.base import Completion, Message, Provider, ProviderConfig, Usage  # noqa: E402
from app.ai.providers import build_provider  # noqa: E402
from app.ai.review_validation import validate  # noqa: E402
from app.ai.reviewer import Proposal  # noqa: E402
from app.ai.visual_reviewer import (  # noqa: E402
    PageCase,
    VisualReviewer,
    choose_pages,
    format_page,
    page_blocks,
    parse_page,
)
from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.core.importer import ImportOptions, import_file  # noqa: E402
from app.core.page_image import render_page  # noqa: E402
from app.core.review import Suspect  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\nfalso"
B64 = base64.b64encode(PNG).decode()


# -- imagem da página -------------------------------------------------------


def test_render_page_devolve_png_da_pagina(livro_pdf):
    png = render_page(livro_pdf, 1)
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    width, height = int.from_bytes(png[16:20], "big"), int.from_bytes(png[20:24], "big")
    assert height > width > 300  # página em pé, a 100 dpi
    with pytest.raises(ValueError, match="página 99"):
        render_page(livro_pdf, 99)


def test_source_pdf(tmp_path, monkeypatch, livro_pdf):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    store = get_store()
    with_pdf = store.create(
        Document(), source_filename="l.pdf", source_bytes=livro_pdf.read_bytes()
    )
    epub = store.create(Document(), source_kind="epub", source_filename="l.epub", source_bytes=b"x")
    assert store.source_pdf(with_pdf.id).name == "l.pdf"
    assert store.source_pdf(epub.id) is None
    reset_caches()


# -- a imagem em cada provedor ----------------------------------------------


MESSAGES = [Message("system", "Confira."), Message("user", "Página 1", images=[PNG])]


def _sent(kind: str, reply: dict, **config) -> dict:
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=reply)

    provider = build_provider(ProviderConfig(kind=kind, model="m", api_key="k", **config))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await provider.complete(MESSAGES, client=client)

    asyncio.run(go())
    return sent[0]


def test_openai_manda_imagem_como_data_uri():
    body = _sent("openai", {"choices": [{"message": {"content": "[]"}}]})
    assert body["messages"][0] == {"role": "system", "content": "Confira."}
    content = body["messages"][1]["content"]
    assert content[0] == {"type": "text", "text": "Página 1"}
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{B64}"


def test_anthropic_manda_bloco_de_imagem_antes_do_texto():
    body = _sent("anthropic", {"content": [{"type": "text", "text": "[]"}]})
    content = body["messages"][0]["content"]
    assert content[0]["source"] == {"type": "base64", "media_type": "image/png", "data": B64}
    assert content[1] == {"type": "text", "text": "Página 1"}


def test_google_manda_inline_data():
    body = _sent("google", {"candidates": [{"content": {"parts": [{"text": "[]"}]}}]})
    parts = body["contents"][0]["parts"]
    assert parts[0] == {"inline_data": {"mime_type": "image/png", "data": B64}}
    assert parts[1] == {"text": "Página 1"}


def test_sem_imagem_o_pedido_continua_texto_puro():
    plain = [Message("user", "Olá")]
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = build_provider(ProviderConfig(kind="openai", model="m", api_key="k"))

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await provider.complete(plain, client=client)

    asyncio.run(go())
    assert sent[0]["messages"] == [{"role": "user", "content": "Olá"}]


FAKE_CLI = r"""#!{python}
import json, os, sys
stdin = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "a") as out:
    out.write(json.dumps({{"args": sys.argv[1:], "stdin": stdin}}) + "\n")
if sys.argv[1:3] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}}))
    sys.exit(0)
print(json.dumps({{"type": "system", "subtype": "init"}}))
print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "[]",
                  "usage": {{"input_tokens": 3, "output_tokens": 2}}}}))
"""


def test_claude_code_manda_imagem_por_stream_json(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(FAKE_CLI.format(python=sys.executable))
    script.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_LOG", str(log))

    provider = build_provider(ProviderConfig(kind="claude_cli", model="haiku"))
    completion = asyncio.run(provider.complete(MESSAGES, client=None))  # type: ignore[arg-type]

    assert completion.text == "[]" and completion.usage.output_tokens == 2
    call = [json.loads(line) for line in log.read_text().splitlines()][-1]
    args = call["args"]
    assert args[:6] == [
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    assert args[args.index("--tools") + 1] == ""  # continua sem ferramentas
    message = json.loads(call["stdin"])
    content = message["message"]["content"]
    assert content[0]["source"]["data"] == B64
    assert content[1] == {"type": "text", "text": "Página 1"}


# -- o revisor visual -------------------------------------------------------


def book() -> tuple[Document, dict[str, Block]]:
    blocks = {
        "titulo": Block("paragraph", "Capítulo 1", page=1),
        "ocr": Block("paragraph", "O rio estava rnais cheio do que nunca.", page=1),
        "fim": Block("paragraph", "A casa resistiu.", page=1),
        "outra": Block("paragraph", "Outra página.", page=2),
        "figura": Block("image", src="f.png", page=1),
    }
    return Document(title="Livro", chapters=[Chapter("C1", list(blocks.values()))]), blocks


def test_choose_pages_prefere_as_com_mais_suspeitos():
    document, b = book()
    suspects = [
        Suspect(document.chapters[0].id, b["ocr"].id, "x", ""),
        Suspect(document.chapters[0].id, b["fim"].id, "x", ""),
        Suspect(document.chapters[0].id, b["outra"].id, "x", ""),
    ]
    assert choose_pages(document, suspects) == [1, 2]
    assert choose_pages(document, suspects, limit=1) == [1]


def test_page_blocks_e_formato_pulam_imagem():
    document, b = book()
    cases = page_blocks(document, 1)
    assert [c.block.id for c in cases] == [b["titulo"].id, b["ocr"].id, b["fim"].id]
    text = format_page(PageCase(1, PNG, cases))
    assert text.startswith("Página 1.")
    assert "⟦2⟧ (parágrafo, página 1): O rio estava rnais cheio" in text


def test_parse_page_so_traz_problemas():
    document, b = book()
    cases = page_blocks(document, 1)
    reply = json.dumps(
        [
            {"n": 1, "op": "set_kind", "kind": "heading1", "confidence": 0.9},
            {"n": 2, "op": "fix_text", "text": "O rio estava mais cheio do que nunca."},
            {"n": 2, "op": "delete"},  # segunda operação no mesmo bloco: fica a primeira
            {"n": 3, "op": "ok"},
            {"n": 3, "op": "insert_after", "text": "A água subiu até a porta."},
            {"n": 9, "op": "delete"},
        ]
    )
    proposals = parse_page(reply, cases)
    assert [(p.block_id, p.op) for p in proposals] == [
        (b["titulo"].id, "set_kind"),
        (b["ocr"].id, "fix_text"),
        (b["fim"].id, "insert_after"),
    ]
    assert proposals[2].payload == {"text": "A água subiu até a porta."}
    assert proposals[0].suspects == ["visual"]
    assert parse_page("[]", cases) == []
    assert parse_page("A página está certa.", cases) is None


class Scripted(Provider):
    def __init__(self, reply):
        super().__init__(ProviderConfig(kind="fake"))
        self.reply = reply
        self.requests: list[list[Message]] = []

    async def complete(self, messages, *, client) -> Completion:
        self.requests.append(messages)
        text = self.reply(messages) if callable(self.reply) else self.reply
        return Completion(text=text, usage=Usage(5, 1))

    async def list_models(self, *, client):
        return []


def test_revisor_visual_manda_a_imagem_e_valida():
    document, b = book()
    reply = json.dumps(
        [
            {
                "n": 2,
                "op": "fix_text",
                "text": "O rio estava mais cheio do que nunca.",
                "confidence": 0.9,
            },
            # "Já está no livro": o modelo achou faltando o que está no bloco 3.
            {"n": 1, "op": "insert_after", "text": "A casa resistiu.", "confidence": 0.8},
        ]
    )
    provider = Scripted(lambda messages: reply if "Página 1" in messages[-1].content else "[]")
    pages = [PageCase(1, PNG, page_blocks(document, 1)), PageCase(2, PNG, page_blocks(document, 2))]

    async def go():
        async with VisualReviewer(provider) as reviewer:
            return await reviewer.review_pages(document, pages)

    result = asyncio.run(go())
    assert len(provider.requests) == 2
    assert provider.requests[0][-1].images == [PNG]
    assert provider.requests[0][-1].content.startswith("Livro: Livro\nPágina 1.")
    assert (result.total, result.reviewed, result.failed) == (2, 2, 0)
    assert [p.op for p in result.proposals] == ["fix_text"]
    assert [r.reason for r in result.rejected] == ["o texto a inserir já está no livro"]


def test_pagina_fora_do_formato_conta_como_falha():
    document, _ = book()

    async def go():
        async with VisualReviewer(Scripted("não sei")) as reviewer:
            return await reviewer.review_pages(
                document, [PageCase(1, PNG, page_blocks(document, 1))]
            )

    result = asyncio.run(go())
    assert (result.reviewed, result.failed, result.proposals) == (0, 1, [])


# -- validação e aplicação do insert_after ------------------------------------


def insert(document, block, text) -> Proposal:
    return Proposal(
        document.chapters[0].id, block.id, "insert_after", 0.9, "", ["visual"], {"text": text}
    )


def test_validacao_do_insert_after():
    document, b = book()
    cases = [
        insert(document, b["ocr"], "Ninguém lembrava de uma cheia assim."),
        insert(document, b["ocr"], "12"),
        insert(document, b["ocr"], "Texto com <a href='x'>link</a> inventado."),
        insert(document, b["titulo"], "a casa   resistiu."),
        insert(document, b["fim"], "x" * 1600),
    ]
    valid, rejected = validate(document, cases)
    assert valid == [cases[0]]
    reasons = [r.reason for r in rejected]
    assert "curto demais" in reasons[0]
    assert "marcações" in reasons[1]
    assert "já está no livro" in reasons[2]
    assert "1500" in reasons[3]


def test_aceitar_insert_after_cria_paragrafo_na_mesma_pagina(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    store = get_store()
    document, b = book()
    project_id = store.create(document, source_filename="l.pdf").id
    proposal = insert(document, b["ocr"], "Ninguém lembrava de uma cheia assim.")
    store.update_review(
        project_id, lambda r: review_apply.record_review(r, document, [proposal], source="visual")
    )
    suggestion = store.load_review(project_id)["suggestions"][0]
    assert suggestion["source"] == "visual"

    review_apply.decide(store, project_id, [suggestion["id"]], accept=True)
    blocks = store.load_document(project_id).chapters[0].blocks
    index = next(i for i, x in enumerate(blocks) if x.id == b["ocr"].id)
    new = blocks[index + 1]
    assert new.text == "Ninguém lembrava de uma cheia assim."
    assert (new.kind, new.page, new.meta.get("inserted")) == ("paragraph", 1, True)
    reset_caches()


def test_revisao_de_uma_origem_nao_apaga_pendentes_da_outra():
    document, b = book()
    review: dict = {}
    text = Proposal(document.chapters[0].id, b["ocr"].id, "ok", 0.9, "")
    visual = insert(document, b["ocr"], "Ninguém lembrava de uma cheia assim.")
    review_apply.record_review(review, document, [text], source="text")
    review_apply.record_review(review, document, [visual], source="visual")
    assert sorted(s["source"] for s in review["suggestions"]) == ["text", "visual"]
    review_apply.record_review(review, document, [], source="visual")  # visual nova, vazia
    assert [s["source"] for s in review["suggestions"]] == ["text"]


# -- rota -------------------------------------------------------------------


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

    holder = SimpleNamespace(provider=Scripted("[]"))
    profile = SimpleNamespace(label="Falso", model="visao-1")
    monkeypatch.setattr(review_routes, "_resolve_provider", lambda *_: (holder.provider, profile))
    return holder


def wait(client, project_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/projects/{project_id}/review").json()["job"]
        if job and job["status"] != "running":
            return job
        time.sleep(0.02)
    raise AssertionError("A revisão não terminou")


def test_rota_visual_com_o_pdf_de_verdade(client, fake, livro_pdf):
    data = livro_pdf.read_bytes()
    document, _ = import_file(data, ".pdf", "livro.pdf", ImportOptions())
    project_id = get_store().create(document, source_filename="livro.pdf", source_bytes=data).id

    def reply(messages):
        # Corrige o primeiro bloco da página, seja ele qual for.
        first = messages[-1].content.split("⟦1⟧ (", 1)[1].split("): ", 1)[1].split("\n\n")[0]
        assert messages[-1].images[0].startswith(b"\x89PNG")
        return json.dumps([{"n": 1, "op": "fix_text", "text": first + "!", "confidence": 0.9}])

    fake.provider = Scripted(reply)
    started = client.post(
        f"/api/projects/{project_id}/review", json={"mode": "visual", "pages": [1, 2]}
    )
    assert started.status_code == 200 and started.json()["job"]["total"] == 2
    job = wait(client, project_id)
    assert job["status"] == "done", job
    assert job["done"] == 2
    suggestions = client.get(f"/api/projects/{project_id}/review").json()["suggestions"]
    assert suggestions and {s["source"] for s in suggestions} == {"visual"}


def test_rota_visual_recusa_o_que_nao_da(client, fake):
    document, _ = book()
    project_id = get_store().create(document, source_kind="epub", source_filename="l.epub").id
    base = f"/api/projects/{project_id}/review"
    response = client.post(base, json={"mode": "visual", "pages": [1]})
    assert response.status_code == 400 and "PDF original" in response.json()["detail"]
    assert client.post(base, json={"mode": "outro"}).status_code == 400


def test_rota_visual_valida_paginas_e_pagina_inexistente(client, fake, livro_pdf):
    data = livro_pdf.read_bytes()
    document, _ = import_file(data, ".pdf", "livro.pdf", ImportOptions())
    project_id = get_store().create(document, source_filename="livro.pdf", source_bytes=data).id
    base = f"/api/projects/{project_id}/review"
    assert client.post(base, json={"mode": "visual", "pages": ["1"]}).status_code == 400
    assert (
        client.post(base, json={"mode": "visual", "pages": list(range(1, 40))}).status_code == 400
    )
    client.post(base, json={"mode": "visual", "pages": [999]})
    job = wait(client, project_id)
    assert job["status"] == "error" and "999" in job["error"]
