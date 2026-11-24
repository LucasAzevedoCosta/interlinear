"""Sugestões da revisão por IA: gravar, aceitar, rejeitar.

O revisor (ai/reviewer.py) devolve propostas validadas; aqui elas viram
sugestões no review.json do projeto e esperam o usuário (modo sugestão; ver
docs-local/revisao-ia-modo-sugestao.md).

Formato do review.json:

    {
      "reviewed_at": "...", "provider": "...", "model": "...",
      "suggestions": [
        {"id", "chapter_id", "block_id", "op", "payload", "confidence",
         "reason", "suspects", "fingerprint", "source": "text" | "visual",
         "status": "pending" | "accepted" | "rejected" | "stale",
         "decided_by": "user" | "auto" | "", "decided_at", "problem"}
      ],
      "dismissed": [{"block_id", "text"}]
    }

  * **Aceitar é uma edição comum** (core/editing.py), validada de novo contra o
    livro atual: o usuário pode ter editado entre a revisão e o clique. O que
    não passa vira "stale", com o motivo em "problem".
  * **A impressão digital** junta a operação, o dado extra e o texto do bloco.
    Serve para não sugerir de novo o que foi rejeitado e para não aplicar uma
    sugestão a um bloco cujo texto mudou desde a revisão.
  * **"dismissed"**: blocos cujo "está certo" o usuário aceitou. A marcação
    de suspeito some deles enquanto o texto for o mesmo.
  * **decided_by** existe para o modo automático futuro gravar "auto".
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import datetime, timezone
from typing import Any

from ..core import editing
from ..core.blocks import Block, Chapter, Document, new_id
from ..core.review import Suspect
from ..core.storage import ProjectStore
from .review_validation import validate
from .reviewer import Proposal

STATUSES = ("pending", "accepted", "rejected", "stale")

_PROPOSAL_FIELDS = ("chapter_id", "block_id", "op", "confidence", "reason", "suspects", "payload")


def _hash(text: str) -> str:
    # Identifica um texto, não protege nada.
    return hashlib.sha1(text.encode("utf-8"), usedforsecurity=False).hexdigest()[:16]


def fingerprint(op: str, payload: dict[str, str], text: str) -> str:
    return _hash(f"{op}\x00{json.dumps(payload, sort_keys=True)}\x00{text}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Gravar uma revisão
# --------------------------------------------------------------------------


def record_review(
    review: dict[str, Any],
    document: Document,
    proposals: Sequence[Proposal],
    *,
    provider: str = "",
    model: str = "",
    source: str = "text",
) -> dict[str, int]:
    """Põe as propostas de uma revisão nova no review.json (alterado no lugar).

    As sugestões pendentes de antes, da mesma origem, saem: a revisão nova as
    substitui. Uma revisão visual não apaga as pendentes da de texto, nem o
    contrário. As decididas ficam, como histórico. Uma proposta igual a uma já
    rejeitada (mesma operação, mesmo dado, mesmo texto do bloco) não volta.
    """
    old = review.get("suggestions") or []
    rejected = {s.get("fingerprint") for s in old if s.get("status") == "rejected"}
    index = document.block_index()

    new: list[dict[str, Any]] = []
    skipped = 0
    for proposal in proposals:
        block = index.get(proposal.block_id)
        if block is None:
            continue
        mark = fingerprint(proposal.op, proposal.payload, block.text)
        if mark in rejected:
            skipped += 1
            continue
        suggestion = {field: getattr(proposal, field) for field in _PROPOSAL_FIELDS}
        suggestion.update(
            id=new_id(),
            source=source,
            fingerprint=mark,
            status="pending",
            decided_by="",
            decided_at="",
            problem="",
        )
        new.append(suggestion)

    kept = [s for s in old if s.get("status") != "pending" or s.get("source", "text") != source]
    review["suggestions"] = kept + new
    review["reviewed_at"] = _now()
    review["provider"] = provider
    review["model"] = model
    review.setdefault("dismissed", [])
    return {"new": len(new), "skipped": skipped}


# --------------------------------------------------------------------------
# Leitura
# --------------------------------------------------------------------------


def check(document: Document, suggestion: dict[str, Any]) -> str | None:
    """Por que a sugestão não pode ser aplicada agora, ou None se pode."""
    proposal = _proposal(suggestion)
    try:
        chapter, index = editing.locate_block(document, proposal.block_id)
    except editing.NotFound:
        return "o bloco não existe mais"
    current = fingerprint(proposal.op, proposal.payload, chapter.blocks[index].text)
    if current != suggestion.get("fingerprint"):
        return "o texto do bloco mudou depois da revisão"
    _, rejected = validate(document, [proposal])
    return rejected[0].reason if rejected else None


def view(document: Document, review: dict[str, Any]) -> list[dict[str, Any]]:
    """As sugestões para a interface. As pendentes trazem o "problem" de agora."""
    shown = []
    for suggestion in review.get("suggestions") or []:
        item = dict(suggestion)
        if item.get("status") == "pending":
            item["problem"] = check(document, suggestion) or ""
        # O registro para desfazer traz blocos inteiros; a tela só precisa
        # saber se dá.
        item["undoable"] = item.pop("undo", None) is not None and item.get("status") == "accepted"
        item.pop("seq", None)
        shown.append(item)
    return shown


def dismissed_blocks(document: Document, review: dict[str, Any]) -> set[str]:
    """Blocos que o usuário confirmou como certos, com o mesmo texto de então."""
    index = document.block_index()
    return {
        entry["block_id"]
        for entry in review.get("dismissed") or []
        if entry.get("block_id") in index
        and _hash(index[entry["block_id"]].text) == entry.get("text")
    }


def open_suspects(
    document: Document, review: dict[str, Any], suspects: Iterable[Suspect]
) -> list[Suspect]:
    """Os suspeitos sem os blocos dispensados."""
    dismissed = dismissed_blocks(document, review)
    return [s for s in suspects if s.block_id not in dismissed]


# --------------------------------------------------------------------------
# Decidir
# --------------------------------------------------------------------------


def apply(document: Document, suggestion: dict[str, Any]) -> dict[str, Any]:
    """Aplica a sugestão ao documento e devolve o que é preciso para desfazer.

    Levanta EditError se não for possível.
    """
    problem = check(document, suggestion)
    if problem:
        raise editing.EditError(problem)

    op = suggestion["op"]
    block_id = suggestion["block_id"]
    payload = suggestion.get("payload") or {}
    chapter, index = editing.locate_block(document, block_id)
    block = chapter.blocks[index]

    if op == "ok":
        return {}
    if op in ("merge_next", "merge_prev"):
        # "B continua o A" é o "juntar com o próximo" do A.
        first = index if op == "merge_next" else index - 1
        originals = [chapter.blocks[first].to_dict(), chapter.blocks[first + 1].to_dict()]
        _, merged = editing.merge_with_next(document, chapter.blocks[first].id)
        return {"merged": merged.to_dict(), "originals": originals}
    if op == "set_kind":
        before = block.kind
        editing.set_kind(block, payload["kind"])
        return {"kind": before, "applied_kind": block.kind}
    if op == "delete":
        _, removed, position = editing.delete_block(document, block_id)
        after = chapter.blocks[position - 1].id if position > 0 else None
        return {"chapter_id": chapter.id, "after": after, "block": removed.to_dict()}
    if op == "fix_text":
        before = block.to_dict()
        editing.set_text(block, payload["text"])
        return {"before": before, "applied_text": block.text}
    if op == "start_chapter":
        created = editing.split_chapter(document, chapter.id, block_id)
        return {"chapter_id": created.id, "from_chapter": chapter.id}
    if op == "merge_chapter":
        first_block = chapter.blocks[0].id if chapter.blocks else None
        into = editing.merge_chapter(document, chapter.id)
        return {
            "chapter": {"id": chapter.id, "title": chapter.title},
            "first_block": first_block,
            "into": into.id,
        }
    if op == "insert_after":
        inserted = editing.insert_paragraph_after(document, block_id, payload["text"])
        return {"inserted": inserted.to_dict()}
    raise editing.EditError(f"Operação desconhecida: {op}")


def revert(document: Document, suggestion: dict[str, Any]) -> None:
    """Desfaz uma sugestão aplicada. Levanta EditError se o livro mudou demais.

    Cada caso confere que o trecho está como a sugestão o deixou: desfazer
    por cima de uma edição do usuário apagaria a edição.
    """
    op = suggestion.get("op")
    undo = suggestion.get("undo")
    if undo is None:
        raise editing.EditError("esta sugestão foi aplicada sem o registro para desfazer")
    changed = editing.EditError("o trecho foi editado depois; desfaça à mão")

    if op == "ok":
        return
    if op in ("merge_next", "merge_prev"):
        chapter, index = _find(document, undo["merged"]["id"], changed)
        if chapter.blocks[index].text != undo["merged"]["text"]:
            raise changed
        chapter.blocks[index : index + 1] = [Block.from_dict(b) for b in undo["originals"]]
    elif op == "set_kind":
        chapter, index = _find(document, suggestion["block_id"], changed)
        block = chapter.blocks[index]
        if block.kind != undo["applied_kind"]:
            raise changed
        block.kind = undo["kind"]
    elif op == "delete":
        restored = Block.from_dict(undo["block"])
        if restored.id in document.block_index():
            raise changed
        chapter = next((c for c in document.chapters if c.id == undo["chapter_id"]), None)
        if chapter is None:
            raise editing.EditError("o capítulo do trecho não existe mais")
        if undo["after"] is None:
            chapter.blocks.insert(0, restored)
        else:
            position = next(
                (i for i, b in enumerate(chapter.blocks) if b.id == undo["after"]), None
            )
            if position is None:
                raise changed
            chapter.blocks.insert(position + 1, restored)
    elif op == "fix_text":
        chapter, index = _find(document, suggestion["block_id"], changed)
        block = chapter.blocks[index]
        if block.text != undo["applied_text"]:
            raise changed
        before = undo["before"]
        block.text = before["text"]
        block.translated = before.get("translated")
        block.meta = dict(before.get("meta") or {})
    elif op == "insert_after":
        chapter, index = _find(document, undo["inserted"]["id"], changed)
        if chapter.blocks[index].text != undo["inserted"]["text"]:
            raise changed
        chapter.blocks.pop(index)
    elif op == "start_chapter":
        ids = [c.id for c in document.chapters]
        position = ids.index(undo["chapter_id"]) if undo["chapter_id"] in ids else -1
        if position < 1 or ids[position - 1] != undo["from_chapter"]:
            raise editing.EditError("os capítulos mudaram depois; junte à mão")
        editing.merge_chapter(document, undo["chapter_id"])
    elif op == "merge_chapter":
        into = next((c for c in document.chapters if c.id == undo["into"]), None)
        if into is None:
            raise editing.EditError("o capítulo não existe mais")
        old = undo["chapter"]
        if undo["first_block"] is None:
            restored = Chapter(title=old["title"], id=old["id"])
            document.chapters.insert(document.chapters.index(into) + 1, restored)
        else:
            if not any(b.id == undo["first_block"] for b in into.blocks[1:]):
                raise editing.EditError("os capítulos mudaram depois; divida à mão")
            restored = editing.split_chapter(document, into.id, undo["first_block"])
            restored.id, restored.title = old["id"], old["title"]
    else:
        raise editing.EditError(f"Operação desconhecida: {op}")


def _find(document: Document, block_id: str, error: editing.EditError) -> tuple[Chapter, int]:
    try:
        return editing.locate_block(document, block_id)
    except editing.NotFound:
        raise error from None


def decide(
    store: ProjectStore,
    project_id: str,
    ids: Sequence[str],
    *,
    accept: bool,
    decided_by: str = "user",
) -> dict[str, Any]:
    """Aceita ou rejeita sugestões pendentes. Grava documento e revisão juntos.

    As aceitas são aplicadas na ordem de leitura do livro, uma a uma, cada uma
    validada contra o livro já alterado pelas anteriores. A que não passa vira
    "stale" e não impede as outras.

    Levanta FileNotFoundError se o projeto não existe mais.
    """
    wanted = set(ids)
    outcome: dict[str, Any] = {"accepted": [], "rejected": [], "stale": []}

    with store.lock(project_id):
        review = store.load_review(project_id)
        chosen = [
            s
            for s in review.get("suggestions") or []
            if s.get("id") in wanted and s.get("status") == "pending"
        ]
        if not chosen:
            outcome["document"] = None
            return outcome

        now = _now()
        if not accept:
            for suggestion in chosen:
                suggestion.update(status="rejected", decided_by=decided_by, decided_at=now)
                outcome["rejected"].append(suggestion["id"])
            store.update_review(project_id, lambda current: current.update(review))
            outcome["document"] = None
            return outcome

        def change(document: Document) -> dict[str, Any]:
            order = {block.id: i for i, block in enumerate(document.all_blocks())}
            for suggestion in sorted(chosen, key=lambda s: order.get(s["block_id"], len(order))):
                text = _block_text(document, suggestion["block_id"])
                try:
                    undo = apply(document, suggestion)
                except editing.EditError as exc:
                    suggestion.update(status="stale", problem=exc.message)
                    outcome["stale"].append({"id": suggestion["id"], "problem": exc.message})
                    continue
                # seq: desfazer em grupo volta na ordem contrária à de aplicar.
                review["seq"] = int(review.get("seq", 0)) + 1
                suggestion.update(
                    status="accepted",
                    decided_by=decided_by,
                    decided_at=now,
                    undo=undo,
                    seq=review["seq"],
                )
                outcome["accepted"].append(suggestion["id"])
                if suggestion["op"] == "ok" and text is not None:
                    review.setdefault("dismissed", []).append(
                        {"block_id": suggestion["block_id"], "text": _hash(text)}
                    )
            return document.to_dict()

        outcome["document"] = store.update(project_id, change)
        store.update_review(project_id, lambda current: current.update(review))
    return outcome


def undo(store: ProjectStore, project_id: str, ids: Sequence[str]) -> dict[str, Any]:
    """Desfaz sugestões aceitas (pelo usuário ou pelo modo automático).

    Volta na ordem contrária à de aplicar: a última aplicada é a primeira
    desfeita, para que cada uma encontre o livro como o deixou. A desfeita
    volta a ser pendente, para o usuário decidir de novo. A que o livro não
    permite mais desfazer fica como está, com o motivo em "failed".

    Levanta FileNotFoundError se o projeto não existe mais.
    """
    wanted = set(ids)
    outcome: dict[str, Any] = {"undone": [], "failed": [], "document": None}

    with store.lock(project_id):
        review = store.load_review(project_id)
        chosen = sorted(
            (
                s
                for s in review.get("suggestions") or []
                if s.get("id") in wanted and s.get("status") == "accepted"
            ),
            key=lambda s: int(s.get("seq", 0)),
            reverse=True,
        )
        if not chosen:
            return outcome

        def change(document: Document) -> dict[str, Any]:
            for suggestion in chosen:
                try:
                    revert(document, suggestion)
                except editing.EditError as exc:
                    outcome["failed"].append({"id": suggestion["id"], "problem": exc.message})
                    continue
                for field in ("undo", "seq"):
                    suggestion.pop(field, None)
                suggestion.update(status="pending", decided_by="", decided_at="", problem="")
                outcome["undone"].append(suggestion["id"])
                if suggestion["op"] == "ok":
                    review["dismissed"] = [
                        d
                        for d in review.get("dismissed") or []
                        if d.get("block_id") != suggestion["block_id"]
                    ]
            return document.to_dict()

        outcome["document"] = store.update(project_id, change)
        store.update_review(project_id, lambda current: current.update(review))
    return outcome


# O que o modo automático aplica sozinho: o que não muda a estrutura do livro
# e que a validação consegue conferir. Começar ou juntar capítulo e trocar o
# tipo erraram com confiança alta no teste com o Claude; inserir texto é o
# modelo escrevendo. Esses esperam o usuário, sempre.
AUTO_OPERATIONS = frozenset({"ok", "delete", "fix_text", "merge_next", "merge_prev"})


def auto_apply(
    store: ProjectStore, project_id: str, *, threshold: float, source: str
) -> dict[str, Any]:
    """O modo automático: aceita as pendentes de baixo risco acima do limite."""
    review = store.load_review(project_id)
    confident = set(select(review, min_confidence=threshold))
    ids = [
        s["id"]
        for s in review.get("suggestions") or []
        if s.get("id") in confident
        and s.get("op") in AUTO_OPERATIONS
        and s.get("source", "text") == source
    ]
    return decide(store, project_id, ids, accept=True, decided_by="auto")


def select(
    review: dict[str, Any],
    *,
    op: str | None = None,
    min_confidence: float = 0.0,
) -> list[str]:
    """Ids das pendentes de uma operação (ou de todas) com confiança mínima.

    É o "aceitar todas" da interface e, no futuro, o modo automático.
    """
    return [
        s["id"]
        for s in review.get("suggestions") or []
        if s.get("status") == "pending"
        and (op is None or s.get("op") == op)
        and float(s.get("confidence", 0)) >= min_confidence
    ]


def _block_text(document: Document, block_id: str) -> str | None:
    try:
        chapter, index = editing.locate_block(document, block_id)
    except editing.NotFound:
        return None
    return chapter.blocks[index].text


def _proposal(suggestion: dict[str, Any]) -> Proposal:
    return Proposal(
        chapter_id=str(suggestion.get("chapter_id", "")),
        block_id=str(suggestion.get("block_id", "")),
        op=str(suggestion.get("op", "")),
        confidence=float(suggestion.get("confidence", 0)),
        reason=str(suggestion.get("reason", "")),
        suspects=list(suggestion.get("suspects") or []),
        payload=dict(suggestion.get("payload") or {}),
    )
