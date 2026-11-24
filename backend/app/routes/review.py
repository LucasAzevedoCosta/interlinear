"""Rotas da revisão da conversão por IA (modo sugestão).

    POST /api/projects/{id}/review                  inicia a revisão em segundo plano
    GET  /api/projects/{id}/review                  sugestões e a tarefa atual
    POST /api/projects/{id}/review/accept           {"ids": [...]}
    POST /api/projects/{id}/review/reject           {"ids": [...]}
    POST /api/projects/{id}/review/accept-all       {"op"?: "...", "min_confidence"?: 0.9}
    POST /api/projects/{id}/review/stop             para a revisão em andamento

Ver ai/review_apply.py e docs-local/revisao-ia-modo-sugestao.md.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..ai import review_apply
from ..ai.base import ProviderError
from ..ai.review_validation import validate
from ..ai.reviewer import OPERATIONS, Reviewer, ReviewResult
from ..core.review import find_suspects
from ..deps import get_settings_store, get_store
from ..jobs import review_registry
from .ai import _resolve_provider

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/projects/{project_id}/review", tags=["review"])


@router.post("")
async def start_review(
    project_id: str, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Manda os trechos suspeitos ao modelo. As sugestões vão para o review.json.

    Usa o provedor ativo, ou o "provider_id" do pedido.
    """
    store = get_store()
    try:
        document = await asyncio.to_thread(store.load_document, project_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Projeto não encontrado") from exc

    review = await asyncio.to_thread(store.load_review, project_id)
    suspects = review_apply.open_suspects(document, review, find_suspects(document))
    blocks = {s.block_id for s in suspects if s.block_id}
    if not blocks:
        return {"job": None, "message": "Nenhum trecho duvidoso para revisar."}

    settings = get_settings_store().load()
    provider, profile = _resolve_provider(settings, payload.get("provider_id"))

    try:
        job = review_registry.create(uuid.uuid4().hex[:12], project_id, total=len(blocks))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    def on_progress(result: ReviewResult) -> None:
        job.progress.done = result.reviewed
        job.progress.failed = result.failed
        job.progress.usage = result.usage

    def persist(result: ReviewResult) -> dict[str, int]:
        # Valida de novo contra o livro de agora: o usuário pode ter editado
        # enquanto o modelo pensava.
        with store.lock(project_id):
            current = store.load_document(project_id)
            valid, _ = validate(current, result.proposals)
            return store.update_review(
                project_id,
                lambda data: review_apply.record_review(
                    data, current, valid, provider=profile.label, model=profile.model
                ),
            )

    async def run() -> None:
        status = "error"
        try:
            async with Reviewer(provider) as reviewer:
                result = await reviewer.review(
                    document, suspects, on_progress=on_progress, should_stop=lambda: job.should_stop
                )
            counts = await asyncio.to_thread(persist, result)
            job.progress.current_note = _note(counts)
            status = "stopped" if job.should_stop else "done"
        except ProviderError as exc:
            job.error = str(exc)
            log.error("Revisão falhou no projeto %s: %s", project_id, exc)
        except FileNotFoundError:
            log.info("Projeto %s apagado durante a revisão", project_id)
            status = "stopped"
        except asyncio.CancelledError:
            status = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001
            job.error = f"Erro inesperado: {exc}"
            log.exception("Revisão falhou no projeto %s", project_id)
        finally:
            # Só depois de gravar: quem vê a tarefa terminada recarrega as sugestões.
            job.finished_at = time.time()
            job.status = status

    job._task = asyncio.create_task(run())
    return {"job": job.to_dict(), "provider": profile.label, "model": profile.model}


@router.get("")
async def read_review(project_id: str) -> dict[str, Any]:
    store = get_store()
    try:
        document = await asyncio.to_thread(store.load_document, project_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Projeto não encontrado") from exc
    review = await asyncio.to_thread(store.load_review, project_id)
    job = review_registry.for_project(project_id)
    return {
        "suggestions": review_apply.view(document, review),
        "reviewed_at": review.get("reviewed_at", ""),
        "provider": review.get("provider", ""),
        "model": review.get("model", ""),
        "job": job.to_dict() if job else None,
    }


@router.post("/accept")
async def accept(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return await _decide(project_id, _ids(payload), accept=True)


@router.post("/reject")
async def reject(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    return await _decide(project_id, _ids(payload), accept=False)


@router.post("/accept-all")
async def accept_all(project_id: str, payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
    """Aceita as pendentes de uma operação (ou de todas) acima de uma confiança."""
    op = payload.get("op")
    if op is not None and op not in OPERATIONS:
        raise HTTPException(400, f"Operação desconhecida: {op}")
    try:
        min_confidence = float(payload.get("min_confidence") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "Confiança mínima inválida") from None

    store = get_store()
    review = await asyncio.to_thread(store.load_review, project_id)
    ids = review_apply.select(review, op=op, min_confidence=min_confidence)
    return await _decide(project_id, ids, accept=True)


@router.post("/stop")
async def stop(project_id: str) -> dict[str, Any]:
    job = review_registry.for_project(project_id)
    if job is None or job.status != "running":
        raise HTTPException(404, "Nenhuma revisão em andamento")
    job.request_stop()
    return {"job": job.to_dict()}


# --------------------------------------------------------------------------


def _ids(payload: dict[str, Any]) -> list[str]:
    ids = payload.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise HTTPException(400, "Campo 'ids' deve ser uma lista de ids")
    return ids


async def _decide(project_id: str, ids: list[str], *, accept: bool) -> dict[str, Any]:
    store = get_store()
    try:
        outcome = await asyncio.to_thread(
            review_apply.decide, store, project_id, ids, accept=accept
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Projeto não encontrado") from exc
    meta = store.get_meta(project_id)
    outcome["project"] = meta.to_dict() if meta else None
    return outcome


def _note(counts: dict[str, int]) -> str:
    new = counts.get("new", 0)
    text = f"{new} {'sugestão' if new == 1 else 'sugestões'}"
    if counts.get("skipped"):
        text += f" ({counts['skipped']} já recusadas antes ficaram de fora)"
    return text
