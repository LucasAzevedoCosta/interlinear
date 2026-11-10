"""Importação em segundo plano, com progresso e cancelamento.

O arquivo chega inteiro no POST, que devolve na hora o id da tarefa. A
extração roda numa thread e avisa cada página lida; a interface consulta
GET /api/imports/{id} até o projeto ficar pronto.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from ..core import ocr
from ..core.importer import import_file
from ..deps import get_store
from ..jobs import ImportCancelled, imports
from .projects import MAX_UPLOAD_BYTES, import_options, read_upload

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/imports", tags=["imports"])

# O loop de eventos só guarda referência fraca às tarefas: sem esta, uma
# importação poderia ser coletada no meio do caminho.
_tasks: set[asyncio.Task[None]] = set()


@router.get("/options")
async def import_choices() -> dict[str, Any]:
    """O que a tela de opções oferece: OCR disponível e idiomas instalados."""
    available = await asyncio.to_thread(ocr.available)
    return {
        "ocr": {
            "available": available,
            "languages": await asyncio.to_thread(ocr.installed_languages) if available else [],
            "default": await asyncio.to_thread(ocr.languages) if available else "",
        },
        "max_upload_bytes": MAX_UPLOAD_BYTES,
    }


@router.post("")
async def start_import(
    file: UploadFile = File(...),
    split_on: str = Form("heading1"),
    title: str = Form(""),
    ocr_mode: str = Form("auto"),
    ocr_languages: str = Form(""),
) -> dict[str, Any]:
    data, suffix, filename = await read_upload(file)
    options = import_options(title, split_on, ocr_mode, ocr_languages)
    job = imports.create(uuid.uuid4().hex[:12], filename)
    store = get_store()

    async def run() -> None:
        status = "error"
        try:
            document, summary = await asyncio.to_thread(
                import_file, data, suffix, filename, options, job.report
            )
            # Cancelado depois da última página: não cria o projeto.
            job.report("save", 0, 1)
            meta = await asyncio.to_thread(
                store.create,
                document,
                source_kind=suffix.lstrip("."),
                source_filename=filename,
                source_bytes=data,
                import_summary=summary,
            )
            job.project_id = meta.id
            status = "done"
        except ImportCancelled:
            status = "stopped"
        except ValueError as exc:
            job.error = str(exc)
        except Exception as exc:  # noqa: BLE001 - arquivos de terceiros
            log.exception("Falha ao importar %s", filename)
            job.error = f"Não foi possível ler o arquivo: {exc}"
        finally:
            job.finished_at = time.time()
            job.status = status

    task = asyncio.create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return {"import": job.to_dict()}


@router.get("")
async def running_imports() -> dict[str, Any]:
    """As importações em andamento, para a interface se reconectar depois de recarregar."""
    return {"imports": [job.to_dict() for job in imports.running()]}


@router.get("/{job_id}")
async def import_status(job_id: str) -> dict[str, Any]:
    job = imports.get(job_id)
    if job is None:
        raise HTTPException(404, "Importação não encontrada")
    return {"import": job.to_dict()}


@router.post("/{job_id}/cancel")
async def cancel_import(job_id: str) -> dict[str, Any]:
    """Pede o cancelamento. Ele vale na próxima página lida, em menos de um segundo."""
    job = imports.get(job_id)
    if job is None:
        raise HTTPException(404, "Importação não encontrada")
    job.request_stop()
    return {"import": job.to_dict()}
