"""Rotas de configuração de IA e de tradução."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

import httpx
from fastapi import APIRouter, Body, HTTPException

from ..ai.base import Message, ProviderConfig, ProviderError
from ..ai.providers import PROVIDER_CATALOG, build_provider
from ..ai.translator import Progress, TranslationSettings, Translator
from ..config import ProviderProfile, Settings
from ..core.blocks import Block, Document
from ..deps import get_settings_store, get_store
from ..jobs import registry

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["ai"])


# --------------------------------------------------------------------------
# Configurações
# --------------------------------------------------------------------------


@router.get("/providers/catalog")
async def provider_catalog() -> dict[str, Any]:
    """Os tipos de provedor suportados, para montar o formulário."""
    return {"catalog": PROVIDER_CATALOG}


@router.get("/settings")
async def read_settings() -> dict[str, Any]:
    """Devolve as preferências sem expor as chaves inteiras."""
    return {"settings": get_settings_store().load().redacted()}


@router.put("/settings")
async def write_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Salva as preferências gerais (idiomas, estilo, concorrência)."""
    store = get_settings_store()
    settings = store.load()

    for name in ("source_language", "target_language", "style_notes"):
        if name in payload:
            setattr(settings, name, str(payload[name]))

    for name in ("batch_chars", "concurrency", "context_blocks"):
        if name in payload:
            try:
                setattr(settings, name, int(payload[name]))
            except (TypeError, ValueError):
                raise HTTPException(400, f"Valor inválido para {name}") from None

    if "glossary" in payload:
        glossary = payload["glossary"]
        if not isinstance(glossary, dict):
            raise HTTPException(400, "Glossário deve ser um objeto termo/tradução")
        settings.glossary = {str(k): str(v) for k, v in glossary.items() if str(k).strip()}

    if "active_provider_id" in payload:
        settings.active_provider_id = str(payload["active_provider_id"])

    settings.concurrency = max(1, min(settings.concurrency, 16))
    settings.batch_chars = max(500, min(settings.batch_chars, 20_000))
    settings.context_blocks = max(0, min(settings.context_blocks, 5))

    store.save(settings)
    return {"settings": settings.redacted()}


@router.post("/providers")
async def upsert_provider(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    """Cria ou atualiza um perfil de provedor.

    Uma chave vazia num perfil que já existe significa "manter a chave atual",
    para que o usuário possa trocar o modelo sem redigitar a chave.
    """
    store = get_settings_store()
    settings = store.load()

    kind = str(payload.get("kind", "")).strip()
    if not kind:
        raise HTTPException(400, "Informe o tipo do provedor")

    profile_id = str(payload.get("id") or uuid.uuid4().hex[:8])
    existing = next((p for p in settings.providers if p.id == profile_id), None)

    api_key = str(payload.get("api_key") or "")
    if not api_key and existing:
        api_key = existing.api_key

    profile = ProviderProfile(
        id=profile_id,
        label=str(payload.get("label") or kind),
        kind=kind,
        model=str(payload.get("model") or ""),
        base_url=str(payload.get("base_url") or ""),
        api_key=api_key,
        temperature=float(payload.get("temperature", 0.2)),
        max_tokens=int(payload.get("max_tokens", 4096)),
    )

    if existing:
        settings.providers[settings.providers.index(existing)] = profile
    else:
        settings.providers.append(profile)

    if not settings.active_provider_id:
        settings.active_provider_id = profile.id

    store.save(settings)
    return {"provider": profile.redacted(), "settings": settings.redacted()}


@router.delete("/providers/{provider_id}")
async def delete_provider(provider_id: str) -> dict[str, Any]:
    store = get_settings_store()
    settings = store.load()
    settings.providers = [p for p in settings.providers if p.id != provider_id]
    if settings.active_provider_id == provider_id:
        settings.active_provider_id = (
            settings.providers[0].id if settings.providers else ""
        )
    store.save(settings)
    return {"settings": settings.redacted()}


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: str) -> dict[str, Any]:
    """Faz uma chamada mínima para confirmar que chave, URL e modelo funcionam."""
    settings = get_settings_store().load()
    provider, _ = _resolve_provider(settings, provider_id)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            completion = await provider.complete(
                [
                    Message(role="system", content="Responda em uma palavra."),
                    Message(role="user", content="Diga: funcionando"),
                ],
                client=client,
            )
        except ProviderError as exc:
            return {"ok": False, "error": str(exc)}
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            return {"ok": False, "error": f"Não foi possível conectar: {exc}"}

    return {
        "ok": True,
        "reply": completion.text.strip()[:120],
        "input_tokens": completion.usage.input_tokens,
        "output_tokens": completion.usage.output_tokens,
    }


@router.get("/providers/{provider_id}/models")
async def list_provider_models(provider_id: str) -> dict[str, Any]:
    settings = get_settings_store().load()
    provider, _ = _resolve_provider(settings, provider_id)

    async with httpx.AsyncClient(follow_redirects=True) as client:
        try:
            models = await provider.list_models(client=client)
        except ProviderError as exc:
            raise HTTPException(502, str(exc)) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise HTTPException(502, f"Não foi possível conectar: {exc}") from exc

    return {"models": models}


# --------------------------------------------------------------------------
# Tradução
# --------------------------------------------------------------------------


@router.post("/projects/{project_id}/translate")
async def start_translation(
    project_id: str, payload: dict[str, Any] = Body(default={})
) -> dict[str, Any]:
    """Dispara a tradução em segundo plano e devolve o id da tarefa.

    Por padrão traduz só o que ainda não foi traduzido, para que retomar um
    trabalho interrompido não gaste tokens de novo. retranslate=true refaz tudo.
    """
    project_store = get_store()
    settings = get_settings_store().load()

    try:
        document = project_store.load_document(project_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(404, "Projeto não encontrado") from exc

    provider, profile = _resolve_provider(settings, payload.get("provider_id"))

    retranslate = bool(payload.get("retranslate"))
    chapter_ids = payload.get("chapter_ids") or []

    candidates = []
    for chapter in document.chapters:
        if chapter_ids and chapter.id not in chapter_ids:
            continue
        for block in chapter.blocks:
            if block.kind not in {
                "heading1", "heading2", "heading3", "paragraph", "quote", "list_item",
            }:
                continue
            if not block.text.strip():
                continue
            if not retranslate and block.translated:
                continue
            candidates.append(block)

    if not candidates:
        return {"job": None, "message": "Nada a traduzir: tudo já está traduzido."}

    # O glossário do pedido substitui o salvo; sem ele, usa o das preferências.
    glossary = payload.get("glossary")
    if glossary is None:
        glossary = settings.glossary
    if not isinstance(glossary, dict):
        raise HTTPException(400, "Glossário deve ser um objeto de pares termo/tradução")

    translation_settings = TranslationSettings(
        source_language=str(
            payload.get("source_language") or settings.source_language
        ),
        target_language=str(
            payload.get("target_language") or settings.target_language
        ),
        glossary={str(k): str(v) for k, v in glossary.items()},
        style_notes=str(payload.get("style_notes") or settings.style_notes),
        batch_chars=int(payload.get("batch_chars") or settings.batch_chars),
        context_blocks=int(
            payload.get("context_blocks")
            if payload.get("context_blocks") is not None
            else settings.context_blocks
        ),
        concurrency=int(payload.get("concurrency") or settings.concurrency),
    )

    job_id = uuid.uuid4().hex[:12]
    try:
        job = registry.create(job_id, project_id, total=len(candidates))
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc

    # A cópia em `document` só serve para a tradução escrever. Ela nunca é
    # gravada inteira: o usuário pode editar, dividir capítulos ou renomear o
    # livro enquanto a tarefa roda, e isso está só no disco.
    initial = {block.id: block.translated for block in candidates}
    written: dict[str, str | None] = {}

    def persist() -> None:
        project_store.update(
            project_id,
            lambda current: _apply_translations(current, candidates, initial, written),
        )

    async def run() -> None:
        saved_counter = {"last": 0}

        async def on_progress(progress: Progress) -> None:
            job.progress = progress
            # Grava a cada ~20 blocos para não perder trabalho sem martelar o disco.
            if progress.done - saved_counter["last"] >= 20:
                saved_counter["last"] = progress.done
                try:
                    await asyncio.to_thread(persist)
                except FileNotFoundError:
                    log.info("Projeto %s apagado durante a tradução", project_id)
                    job.request_stop()

        try:
            async with Translator(provider, translation_settings) as translator:
                await translator.translate_blocks(
                    candidates,
                    on_progress=on_progress,
                    should_stop=lambda: job.should_stop,
                )
            job.status = "stopped" if job.should_stop else "done"
        except ProviderError as exc:
            job.status = "error"
            job.error = str(exc)
            log.error("Tradução falhou no projeto %s: %s", project_id, exc)
        except asyncio.CancelledError:
            job.status = "stopped"
            raise
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = f"Erro inesperado: {exc}"
            log.exception("Tradução falhou no projeto %s", project_id)
        finally:
            import time

            job.finished_at = time.time()
            try:
                await asyncio.to_thread(persist)
            except FileNotFoundError:
                log.info("Projeto %s apagado durante a tradução", project_id)

    job._task = asyncio.create_task(run())

    return {
        "job": job.to_dict(),
        "provider": profile.label,
        "model": profile.model,
    }


@router.get("/jobs/{job_id}")
async def job_status(job_id: str) -> dict[str, Any]:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "Tarefa não encontrada")
    return {"job": job.to_dict()}


@router.get("/projects/{project_id}/job")
async def project_job(project_id: str) -> dict[str, Any]:
    """A tarefa mais recente do projeto, para a interface se reconectar."""
    job = registry.for_project(project_id)
    return {"job": job.to_dict() if job else None}


@router.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str) -> dict[str, Any]:
    job = registry.get(job_id)
    if job is None:
        raise HTTPException(404, "Tarefa não encontrada")
    job.request_stop()
    return {"job": job.to_dict()}


# --------------------------------------------------------------------------


def _apply_translations(
    document: Document,
    translated_blocks: list[Block],
    initial: dict[str, str | None],
    written: dict[str, str | None],
) -> None:
    """Copia para `document` as traduções novas da tarefa, bloco a bloco.

    Um bloco só é tocado se, no disco, ele ainda tem o valor de quando a tarefa
    começou ou o que a própria tarefa gravou antes. Qualquer outro valor é uma
    edição do usuário feita durante a tradução, e a edição vence. Blocos que
    sumiram (capítulo apagado, por exemplo) são ignorados.
    """
    on_disk = document.block_index()
    for block in translated_blocks:
        if block.translated == initial[block.id]:
            continue  # a tarefa ainda não traduziu este bloco
        target = on_disk.get(block.id)
        if target is None:
            continue
        if target.translated not in (initial[block.id], written.get(block.id)):
            continue
        target.translated = block.translated
        written[block.id] = block.translated


def _resolve_provider(settings: Settings, provider_id: str | None):
    profile = settings.get_provider(provider_id)
    if profile is None:
        raise HTTPException(
            400, "Nenhum provedor de IA configurado. Adicione um nas configurações."
        )
    if not profile.model:
        raise HTTPException(400, f"O provedor '{profile.label}' está sem modelo definido")

    config = ProviderConfig(
        kind=profile.kind,
        api_key=settings.resolved_key(profile),
        base_url=profile.base_url,
        model=profile.model,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
    )
    try:
        return build_provider(config), profile
    except ProviderError as exc:
        raise HTTPException(400, str(exc)) from exc
