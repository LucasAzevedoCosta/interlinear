"""Rotas da revisão da conversão por IA (modo sugestão).

    POST /api/projects/{id}/review                  inicia a revisão em segundo plano;
                                                    {"mode": "visual", "pages"?: [3, 7]}
                                                    compara com as páginas do PDF
    GET  /api/projects/{id}/review                  sugestões e a tarefa atual
    POST /api/projects/{id}/review/accept           {"ids": [...]}
    POST /api/projects/{id}/review/reject           {"ids": [...]}
    POST /api/projects/{id}/review/accept-all       {"op"?: "...", "min_confidence"?: 0.9}
    POST /api/projects/{id}/review/undo             {"ids": [...]} desfaz aceitas
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
from ..ai.visual_reviewer import PageCase, VisualReviewer, choose_pages, page_blocks
from ..core.page_image import render_page
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

    Usa o provedor ativo, ou o "provider_id" do pedido. Com "mode": "visual",
    compara a imagem das páginas do PDF com o texto extraído delas: as
    páginas de "pages", ou as com mais trechos duvidosos.
    """
    return await launch_review(
        project_id,
        mode=payload.get("mode") or "text",
        pages=_pages(payload.get("pages")),
        provider_id=payload.get("provider_id"),
    )


async def launch_review(
    project_id: str,
    *,
    mode: str = "text",
    pages: list[int] | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    """Começa a revisão em segundo plano. Levanta HTTPException se não der.

    Também chamada pela importação, quando "revisar ao importar" está ligado.
    No modo automático (Settings.review_mode), as sugestões de baixo risco
    acima do limite são aplicadas no fim da tarefa (review_apply.auto_apply).
    """
    store = get_store()
    try:
        document = await asyncio.to_thread(store.load_document, project_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Projeto não encontrado") from exc

    if mode not in ("text", "visual"):
        raise HTTPException(400, f"Modo de revisão desconhecido: {mode}")

    review = await asyncio.to_thread(store.load_review, project_id)
    suspects = review_apply.open_suspects(document, review, find_suspects(document))

    if mode == "visual":
        pdf = store.source_pdf(project_id)
        if pdf is None:
            raise HTTPException(
                400, "A revisão visual precisa do PDF original, e este livro não veio de um."
            )
        pages = pages or choose_pages(document, suspects)
        if not pages:
            return {
                "job": None,
                "message": "Nenhuma página com trechos duvidosos. Escolha as páginas a conferir.",
            }
        total = len(pages)
    else:
        blocks = {s.block_id for s in suspects if s.block_id}
        if not blocks:
            return {"job": None, "message": "Nenhum trecho duvidoso para revisar."}
        total = len(blocks)

    settings = get_settings_store().load()
    provider, profile = _resolve_provider(settings, provider_id)
    auto = settings.review_mode == "auto"
    threshold = settings.review_threshold

    try:
        job = review_registry.create(uuid.uuid4().hex[:12], project_id, total=total)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    def on_progress(result: ReviewResult) -> None:
        job.progress.total = result.total
        job.progress.done = result.reviewed
        job.progress.failed = result.failed
        job.progress.usage = result.usage

    def render() -> list[PageCase]:
        # Fora do loop de eventos: renderizar é trabalho de CPU, sob trava.
        return [
            PageCase(page, render_page(pdf, page), page_blocks(document, page)) for page in pages
        ]

    def persist(result: ReviewResult) -> dict[str, int]:
        # Valida de novo contra o livro de agora: o usuário pode ter editado
        # enquanto o modelo pensava.
        with store.lock(project_id):
            current = store.load_document(project_id)
            valid, _ = validate(current, result.proposals)
            return store.update_review(
                project_id,
                lambda data: review_apply.record_review(
                    data, current, valid, provider=profile.label, model=profile.model, source=mode
                ),
            )

    async def run() -> None:
        status = "error"
        try:
            if mode == "visual":
                page_cases = await asyncio.to_thread(render)
                async with VisualReviewer(provider) as reviewer:
                    result = await reviewer.review_pages(
                        document,
                        page_cases,
                        on_progress=on_progress,
                        should_stop=lambda: job.should_stop,
                    )
            else:
                async with Reviewer(provider) as reviewer:
                    result = await reviewer.review(
                        document,
                        suspects,
                        on_progress=on_progress,
                        should_stop=lambda: job.should_stop,
                    )
            counts = await asyncio.to_thread(persist, result)
            if auto:
                applied = await asyncio.to_thread(
                    review_apply.auto_apply, store, project_id, threshold=threshold, source=mode
                )
                accepted = set(applied["accepted"])
                ops = [
                    item["op"]
                    for item in store.load_review(project_id).get("suggestions") or []
                    if item.get("id") in accepted
                ]
                # "Está certo" não muda o livro: conta à parte, como confirmação.
                counts["auto"] = sum(op != "ok" for op in ops)
                counts["confirmed"] = sum(op == "ok" for op in ops)
            job.progress.current_note = _note(counts)
            status = "stopped" if job.should_stop else "done"
        except ProviderError as exc:
            job.error = str(exc)
            log.error("Revisão falhou no projeto %s: %s", project_id, exc)
        except FileNotFoundError:
            log.info("Projeto %s apagado durante a revisão", project_id)
            status = "stopped"
        except ValueError as exc:  # página que o PDF não tem
            job.error = str(exc)
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


@router.post("/undo")
async def undo(project_id: str, payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Desfaz sugestões aceitas; elas voltam a ser pendentes."""
    store = get_store()
    try:
        outcome = await asyncio.to_thread(review_apply.undo, store, project_id, _ids(payload))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Projeto não encontrado") from exc
    meta = store.get_meta(project_id)
    outcome["project"] = meta.to_dict() if meta else None
    return outcome


@router.post("/stop")
async def stop(project_id: str) -> dict[str, Any]:
    job = review_registry.for_project(project_id)
    if job is None or job.status != "running":
        raise HTTPException(404, "Nenhuma revisão em andamento")
    job.request_stop()
    return {"job": job.to_dict()}


# --------------------------------------------------------------------------


# Uma revisão visual pedida à mão olha no máximo isto: cada página é uma imagem.
MAX_VISUAL_PAGES = 30


def _pages(value: Any) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(
        isinstance(p, int) and not isinstance(p, bool) and p >= 1 for p in value
    ):
        raise HTTPException(400, "Campo 'pages' deve ser uma lista de números de página")
    pages = sorted(set(value))
    if len(pages) > MAX_VISUAL_PAGES:
        raise HTTPException(400, f"No máximo {MAX_VISUAL_PAGES} páginas por revisão visual")
    return pages


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
    if counts.get("auto"):
        auto = counts["auto"]
        text += f", {auto} {'aplicada' if auto == 1 else 'aplicadas'} automaticamente"
    if counts.get("confirmed"):
        confirmed = counts["confirmed"]
        text += (
            f", {confirmed} {'trecho confirmado' if confirmed == 1 else 'trechos confirmados'}"
            " como certo"
        )
    if counts.get("skipped"):
        text += f" ({counts['skipped']} já recusadas antes ficaram de fora)"
    return text
