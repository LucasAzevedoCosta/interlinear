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
from ..core.blocks import Document, new_id
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


def apply(document: Document, suggestion: dict[str, Any]) -> None:
    """Aplica a sugestão ao documento. Levanta EditError se não for possível."""
    problem = check(document, suggestion)
    if problem:
        raise editing.EditError(problem)

    op = suggestion["op"]
    block_id = suggestion["block_id"]
    payload = suggestion.get("payload") or {}
    chapter, index = editing.locate_block(document, block_id)
    block = chapter.blocks[index]

    if op == "ok":
        return
    if op == "merge_next":
        editing.merge_with_next(document, block_id)
    elif op == "merge_prev":
        # "B continua o A" é o "juntar com o próximo" do A.
        editing.merge_with_next(document, chapter.blocks[index - 1].id)
    elif op == "set_kind":
        editing.set_kind(block, payload["kind"])
    elif op == "delete":
        editing.delete_block(document, block_id)
    elif op == "fix_text":
        editing.set_text(block, payload["text"])
    elif op == "start_chapter":
        editing.split_chapter(document, chapter.id, block_id)
    elif op == "merge_chapter":
        editing.merge_chapter(document, chapter.id)
    elif op == "insert_after":
        editing.insert_paragraph_after(document, block_id, payload["text"])
    else:
        raise editing.EditError(f"Operação desconhecida: {op}")


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
                    apply(document, suggestion)
                except editing.EditError as exc:
                    suggestion.update(status="stale", problem=exc.message)
                    outcome["stale"].append({"id": suggestion["id"], "problem": exc.message})
                    continue
                suggestion.update(status="accepted", decided_by=decided_by, decided_at=now)
                outcome["accepted"].append(suggestion["id"])
                if suggestion["op"] == "ok" and text is not None:
                    review.setdefault("dismissed", []).append(
                        {"block_id": suggestion["block_id"], "text": _hash(text)}
                    )
            return document.to_dict()

        outcome["document"] = store.update(project_id, change)
        store.update_review(project_id, lambda current: current.update(review))
    return outcome


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
