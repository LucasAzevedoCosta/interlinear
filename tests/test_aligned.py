"""Tradução alinhada (formato frase a frase), com um provedor falso."""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402

from app.ai.base import Completion, Provider, ProviderConfig, Usage  # noqa: E402
from app.ai.translator import Aligned, TranslationSettings, Translator  # noqa: E402
from app.config import ProviderProfile  # noqa: E402
from app.core import editing  # noqa: E402
from app.core.blocks import Block, Chapter, Document  # noqa: E402
from app.core.sentences import is_aligned  # noqa: E402
from app.deps import get_store, reset_caches  # noqa: E402
from app.routes import ai as ai_routes  # noqa: E402

_SEGMENT = re.compile(r"⟦(\d+)⟧\n(.*?)(?=\n\n⟦|\n\n\[Par|\Z)", re.S)
REFUSAL = "I can't translate this copyrighted text."


class AlignedFake(Provider):
    """Devolve "[pt] frase" por marcador. Os modos imitam erros do modelo."""

    def __init__(self) -> None:
        super().__init__(ProviderConfig(kind="fake", model="fake"))
        self.calls: list[str] = []
        self.echo_labels = False  # repete as linhas [Parágrafo n]
        self.merge_in_batch = False  # junta duas frases quando há mais de um parágrafo
        self.never_align = False  # nunca devolve o número certo de marcadores
        self.refuse: set[str] = set()

    async def complete(self, messages, *, client) -> Completion:
        body = messages[-1].content.split("Segmentos a traduzir:", 1)[-1]
        self.calls.append(body)
        usage = Usage(input_tokens=1, output_tokens=1)
        if any(text in body for text in self.refuse):
            return Completion(text=REFUSAL, usage=usage)
        if "⟦" not in body:  # plano C: o parágrafo inteiro, sem marcadores
            return Completion(text=f"[pt] {body.strip().splitlines()[-1]}", usage=usage)
        segments = _SEGMENT.findall(body)
        several = body.count("[Parágrafo") > 1
        if self.never_align or (self.merge_in_batch and several):
            segments = segments[:-1]  # "juntou" a última frase na anterior
        lines = []
        for number, text in segments:
            if self.echo_labels:
                lines.append("[Parágrafo 1]")
            lines.append(f"⟦{number}⟧ [pt] {text.strip()}")
        return Completion(text="\n\n".join(lines), usage=usage)

    async def list_models(self, *, client) -> list[str]:
        return ["fake"]


def _translate(blocks: list[Block], fake: AlignedFake, *, aligned: bool = True) -> None:
    settings = TranslationSettings(aligned=aligned, source_code="en", batch_chars=10_000)

    async def run() -> None:
        async with Translator(fake, settings) as translator:
            await translator.translate_blocks(blocks)

    asyncio.run(run())


def _two() -> list[Block]:
    return [
        Block(kind="paragraph", text="The door opened. Nobody came in.", id="a"),
        Block(kind="paragraph", text="It was late. The shop was closed.", id="b"),
    ]


# -------------------------------------------------------------- tradutor


def test_alinha_cada_frase_e_junta_a_traducao():
    fake = AlignedFake()
    blocks = _two()
    _translate(blocks, fake)
    assert len(fake.calls) == 1  # um lote só
    assert blocks[0].translated == "[pt] The door opened. [pt] Nobody came in."
    assert blocks[0].meta["sentences"] == [
        {"src": "The door opened.", "tgt": "[pt] The door opened."},
        {"src": "Nobody came in.", "tgt": "[pt] Nobody came in."},
    ]
    assert all(is_aligned(b.text, b.translated, b.meta["sentences"]) for b in blocks)
    assert "align_failed" not in blocks[0].meta


def test_pedido_tem_paragrafos_e_a_regra_de_fidelidade():
    fake = AlignedFake()
    _translate(_two(), fake)
    body = fake.calls[0]
    assert "[Parágrafo 1]" in body and "[Parágrafo 2]" in body
    assert body.count("⟦") == 4


def test_linhas_de_paragrafo_repetidas_sao_tiradas():
    fake = AlignedFake()
    fake.echo_labels = True
    blocks = _two()
    _translate(blocks, fake)
    assert "[Parágrafo" not in blocks[0].translated
    assert blocks[1].meta["sentences"][0]["tgt"] == "[pt] It was late."


def test_lote_torto_tenta_um_paragrafo_por_vez():
    fake = AlignedFake()
    fake.merge_in_batch = True  # erra com dois parágrafos, acerta com um
    blocks = _two()
    _translate(blocks, fake)
    assert len(fake.calls) == 3  # o lote e um pedido por parágrafo
    assert all(len(b.meta["sentences"]) == 2 for b in blocks)


def test_sem_alinhar_traduz_o_paragrafo_inteiro_e_marca():
    fake = AlignedFake()
    fake.never_align = True
    blocks = _two()
    _translate(blocks, fake)
    for block in blocks:
        assert block.translated.startswith("[pt]")
        assert block.meta["align_failed"] is True
        assert "sentences" not in block.meta


def test_recusa_num_paragrafo_fica_como_recusa():
    fake = AlignedFake()
    fake.refuse = {"It was late."}
    blocks = _two()
    _translate(blocks, fake)
    assert blocks[0].meta["sentences"]  # o outro alinhou
    assert blocks[1].translated is None
    assert blocks[1].meta["refused"] == REFUSAL


def test_paragrafo_de_uma_frase():
    fake = AlignedFake()
    blocks = [Block(kind="paragraph", text="Only one sentence here.", id="c")]
    _translate(blocks, fake)
    assert blocks[0].meta["sentences"] == [
        {"src": "Only one sentence here.", "tgt": "[pt] Only one sentence here."}
    ]


def test_formato_normal_tira_as_frases_antigas():
    block = Block(
        kind="paragraph",
        text="Old one. Old two.",
        meta={"sentences": [{"src": "x", "tgt": "y"}], "align_failed": True},
    )

    class Plain(AlignedFake):
        async def complete(self, messages, *, client):
            body = messages[-1].content.split("Segmentos a traduzir:", 1)[-1]
            numbers = re.findall(r"⟦(\d+)⟧", body)
            return Completion(text="\n\n".join(f"⟦{n}⟧ tradução" for n in numbers), usage=Usage())

    _translate([block], Plain(), aligned=False)
    assert block.translated == "tradução"
    assert "sentences" not in block.meta and "align_failed" not in block.meta


def test_aligned_e_um_str_com_os_pares():
    value = Aligned([("A.", "Á."), ("B <em>c</em>.", "B <em>ç</em>.")])
    assert value == "Á. B <em>ç</em>."
    assert value.pairs[1] == ("B <em>c</em>.", "B <em>ç</em>.")


# ------------------------------------------------------------ is_aligned


def test_is_aligned():
    pairs = [{"src": "A b.", "tgt": "X."}, {"src": "C d.", "tgt": "Y."}]
    assert is_aligned("A b. C d.", "X. Y.", pairs)
    assert not is_aligned("A b. C mudou.", "X. Y.", pairs)  # original mudou
    assert not is_aligned("A b. C d.", None, pairs)
    assert not is_aligned("A b. C d.", "X.", [{"src": "A b. C d.", "tgt": " "}])
    assert not is_aligned("A b.", "X.", "lixo")


# -------------------------------------------------------- juntar blocos


def _aligned_block(text: str, target: str, id: str) -> Block:
    return Block(
        kind="paragraph",
        text=text,
        translated=target,
        id=id,
        meta={"sentences": [{"src": text, "tgt": target}]},
    )


def test_juntar_dois_alinhados_soma_as_frases():
    first = _aligned_block("Uma frase.", "One sentence.", "x")
    second = _aligned_block("Outra frase.", "Another one.", "y")
    document = Document(chapters=[Chapter(title="C", blocks=[first, second])])
    _, merged = editing.merge_with_next(document, "x")
    assert [p["src"] for p in merged.meta["sentences"]] == ["Uma frase.", "Outra frase."]
    assert is_aligned(merged.text, merged.translated, merged.meta["sentences"])


def test_juntar_com_um_desalinhado_tira_as_frases():
    first = _aligned_block("Uma frase.", "One sentence.", "x")
    second = Block(kind="paragraph", text="Outra.", translated="Other.", id="y")
    document = Document(chapters=[Chapter(title="C", blocks=[first, second])])
    _, merged = editing.merge_with_next(document, "x")
    assert "sentences" not in merged.meta


# ------------------------------------------------------------------ rotas


@pytest.fixture
def fake(monkeypatch) -> AlignedFake:
    provider = AlignedFake()
    profile = ProviderProfile(id="fake", label="Falso", kind="fake", model="fake")
    monkeypatch.setattr(ai_routes, "_resolve_provider", lambda *_: (provider, profile))
    return provider


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("INTERLINEAR_DATA_DIR", str(tmp_path))
    reset_caches()
    from app.main import app

    with TestClient(app, base_url="http://127.0.0.1") as test_client:
        yield test_client
    reset_caches()


def _book(layout: str = "sentence") -> str:
    blocks = [
        Block(kind="paragraph", text="The door opened. Nobody came in.", id="a"),
        Block(kind="paragraph", text="It was late. The shop was closed.", id="b"),
        Block(
            kind="paragraph",
            text="He waited. Then he left.",
            translated="Ele esperou e foi embora.",
            id="c",
            meta={"edited": True},
        ),
        Block(
            kind="paragraph",
            text="Morning came. Light filled the room.",
            translated="Amanheceu.",
            id="d",
        ),
    ]
    document = Document(source_language="en", chapters=[Chapter(title="C", blocks=blocks, id="c1")])
    translation = {"source_language": "inglês", "target_language": "português brasileiro"}
    if layout != "translated":
        translation["layout"] = layout
    return (
        get_store()
        .create(document, source_kind="epub", source_filename="x.epub", translation=translation)
        .id
    )


def _run(client, project, **body) -> dict:
    response = client.post(
        f"/api/projects/{project}/translate", json={"batch_chars": 10_000, **body}
    )
    assert response.status_code == 200, response.text
    job = response.json()["job"]
    if job is None:
        return {}
    deadline = time.time() + 5
    while time.time() < deadline:
        state = client.get(f"/api/jobs/{job['id']}").json()["job"]
        if state["status"] != "running":
            return state
        time.sleep(0.02)
    raise AssertionError("tarefa não terminou")


def _blocks(project: str) -> dict[str, Block]:
    return get_store().load_document(project).block_index()


def test_livro_frase_a_frase_traduz_alinhado(client, fake):
    project = _book()
    assert _run(client, project)["status"] == "done"
    blocks = _blocks(project)
    assert blocks["a"].meta["sentences"][1]["tgt"] == "[pt] Nobody came in."
    # Os já traduzidos não são tocados por um "Traduzir" comum.
    assert "sentences" not in blocks["d"].meta
    alignment = client.get(f"/api/projects/{project}/alignment").json()
    assert alignment == {"translated": 4, "aligned": 2, "unaligned": 2, "edited": 1, "failed": 0}


def test_traduzir_alinhado_refaz_os_desalinhados_menos_os_feitos_a_mao(client, fake):
    project = _book()
    _run(client, project)
    _run(client, project, align=True)
    blocks = _blocks(project)
    assert blocks["d"].meta["sentences"]  # refeito alinhado
    assert blocks["c"].translated == "Ele esperou e foi embora."  # feito à mão: intocado
    alignment = client.get(f"/api/projects/{project}/alignment").json()
    assert (alignment["aligned"], alignment["edited"]) == (3, 1)


def test_traduzir_alinhado_com_os_feitos_a_mao(client, fake):
    project = _book()
    _run(client, project, align=True, include_edited=True)
    assert client.get(f"/api/projects/{project}/alignment").json()["unaligned"] == 0


def test_traduzir_alinhado_sem_o_formato_e_400(client, fake):
    project = _book(layout="paragraph")
    response = client.post(f"/api/projects/{project}/translate", json={"align": True})
    assert response.status_code == 400


def test_livro_paragrafo_a_paragrafo_traduz_sem_frases(client, fake):
    project = _book(layout="paragraph")
    _run(client, project)
    blocks = _blocks(project)
    assert blocks["a"].translated and "sentences" not in blocks["a"].meta
    assert "[Parágrafo" not in fake.calls[0]


def test_editar_a_traducao_inteira_desfaz_o_alinhamento(client, fake):
    project = _book()
    _run(client, project)
    client.patch(
        f"/api/projects/{project}/blocks", json={"updates": [{"id": "a", "translated": "Outra."}]}
    )
    assert "sentences" not in _blocks(project)["a"].meta


def test_formato_frase_a_frase_no_livro_e_na_exportacao(client, fake):
    project = _book(layout="translated")
    body = {"source_language": "en", "target_language": "pt-BR", "layout": "sentence"}
    assert (
        client.put(f"/api/projects/{project}/translation", json=body).json()["project"][
            "translation"
        ]["layout"]
        == "sentence"
    )
    exported = client.get(f"/api/projects/{project}/export?layout=sentence")
    assert exported.status_code == 200
    assert (
        "frase a frase" in exported.headers["content-disposition"]
        or "frase%20a%20frase" in exported.headers["content-disposition"]
    )
