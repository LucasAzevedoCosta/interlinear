"""Tarefas em segundo plano: tradução e importação.

Traduzir um livro leva de minutos a horas. A requisição HTTP não pode ficar
aberta esse tempo todo, então quem dispara a tradução recebe um id de tarefa e
consulta o progresso. O resultado é gravado no projeto conforme avança, de modo
que fechar a aba não perde nada.

A importação segue o mesmo modelo: um PDF escaneado leva cerca de um minuto
no OCR, e a interface mostra a página em que ele está.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from .ai.translator import Progress

log = logging.getLogger(__name__)


@dataclass
class Job:
    id: str
    project_id: str
    status: str = "running"  # running | done | error | stopped
    progress: Progress = field(default_factory=Progress)
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    _stop_requested: bool = False
    _task: asyncio.Task[Any] | None = field(default=None, repr=False)

    def request_stop(self) -> None:
        self._stop_requested = True

    def cancel(self) -> None:
        """Para já, sem esperar os lotes em andamento. Para quando o projeto some."""
        self.request_stop()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @property
    def should_stop(self) -> bool:
        return self._stop_requested

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def eta_seconds(self) -> float | None:
        """Estimativa simples: ritmo médio aplicado ao que falta."""
        completed = self.progress.done + self.progress.failed
        if completed < 3 or self.progress.total <= completed:
            return None
        rate = completed / max(self.elapsed, 0.001)
        return (self.progress.total - completed) / rate if rate > 0 else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "status": self.status,
            "done": self.progress.done,
            "failed": self.progress.failed,
            "total": self.progress.total,
            "note": self.progress.current_note,
            "input_tokens": self.progress.usage.input_tokens,
            "output_tokens": self.progress.usage.output_tokens,
            "elapsed_seconds": round(self.elapsed, 1),
            "eta_seconds": (
                round(eta, 1) if (eta := self.eta_seconds()) is not None else None
            ),
            "error": self.error,
        }


class JobRegistry:
    """Guarda as tarefas em memória. Uma tarefa ativa por projeto."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._by_project: dict[str, str] = {}

    def create(self, job_id: str, project_id: str, total: int) -> Job:
        existing = self.for_project(project_id)
        if existing and existing.status == "running":
            raise ValueError("Já existe uma tradução em andamento neste projeto")

        job = Job(id=job_id, project_id=project_id)
        job.progress.total = total
        self._jobs[job_id] = job
        self._by_project[project_id] = job_id
        self._prune()
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def for_project(self, project_id: str) -> Job | None:
        job_id = self._by_project.get(project_id)
        return self._jobs.get(job_id) if job_id else None

    def all(self) -> list[Job]:
        return list(self._jobs.values())

    def _prune(self, keep: int = 50) -> None:
        """Descarta tarefas antigas já concluídas."""
        finished = sorted(
            (j for j in self._jobs.values() if j.status != "running"),
            key=lambda j: j.finished_at or 0,
        )
        while len(self._jobs) > keep and finished:
            victim = finished.pop(0)
            self._jobs.pop(victim.id, None)
            if self._by_project.get(victim.project_id) == victim.id:
                self._by_project.pop(victim.project_id, None)


registry = JobRegistry()


class ImportCancelled(Exception):
    """Levantada dentro da extração quando o usuário cancela a importação."""


@dataclass
class ImportJob:
    id: str
    filename: str
    status: str = "running"  # running | done | error | stopped
    # read (lendo as páginas) | ocr | build (montando capítulos) | save
    stage: str = "read"
    done: int = 0
    total: int = 0
    error: str = ""
    project_id: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    stage_started_at: float = field(default_factory=time.time)
    _stop_requested: bool = False

    def request_stop(self) -> None:
        self._stop_requested = True

    def report(self, stage: str, done: int, total: int) -> None:
        """Recebe o andamento da extração (pdf_extract.OnProgress), na thread dela."""
        if self._stop_requested:
            raise ImportCancelled
        if stage != self.stage:
            self.stage_started_at = time.time()
        self.stage, self.done, self.total = stage, done, total

    def eta_seconds(self) -> float | None:
        """O ritmo da etapa atual aplicado ao que falta dela."""
        if self.done < 3 or self.total <= self.done:
            return None
        rate = self.done / max(time.time() - self.stage_started_at, 0.001)
        return (self.total - self.done) / rate

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "filename": self.filename,
            "status": self.status,
            "stage": self.stage,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "project_id": self.project_id,
            "elapsed_seconds": round((self.finished_at or time.time()) - self.started_at, 1),
            "eta_seconds": (
                round(eta, 1) if (eta := self.eta_seconds()) is not None else None
            ),
        }


class ImportRegistry:
    """As importações em memória. Várias podem rodar ao mesmo tempo."""

    def __init__(self) -> None:
        self._jobs: dict[str, ImportJob] = {}

    def create(self, job_id: str, filename: str) -> ImportJob:
        job = ImportJob(id=job_id, filename=filename)
        self._jobs[job_id] = job
        self._prune()
        return job

    def get(self, job_id: str) -> ImportJob | None:
        return self._jobs.get(job_id)

    def running(self) -> list[ImportJob]:
        return sorted(
            (job for job in self._jobs.values() if job.status == "running"),
            key=lambda job: job.started_at,
        )

    def _prune(self, keep: int = 20) -> None:
        finished = sorted(
            (j for j in self._jobs.values() if j.status != "running"),
            key=lambda j: j.finished_at or 0,
        )
        while len(self._jobs) > keep and finished:
            self._jobs.pop(finished.pop(0).id, None)


imports = ImportRegistry()
