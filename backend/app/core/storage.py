"""Persistência de projetos em disco.

Um projeto é uma pasta com um JSON do documento, o arquivo original e as
imagens extraídas. Sem banco de dados: o conteúdo é de um usuário só, roda
localmente, e um JSON legível facilita inspecionar ou versionar o trabalho.

A gravação é atômica (escreve em arquivo temporário e renomeia) para que uma
queda no meio de um salvamento não corrompa horas de revisão.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar

from .blocks import Document, new_id

T = TypeVar("T")

# Trava por projeto: o tradutor grava progresso enquanto o editor pode salvar.
# Reentrante porque update() segura a trava e chama save_document(), que também
# a pede.
_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def _lock_for(project_id: str) -> threading.RLock:
    with _locks_guard:
        return _locks.setdefault(project_id, threading.RLock())


@dataclass
class ProjectMeta:
    id: str
    title: str
    created_at: str
    updated_at: str
    source_kind: str = "pdf"  # "pdf" ou "epub"
    source_filename: str = ""
    word_count: int = 0
    translated_count: int = 0
    total_translatable: int = 0
    # O que a importação encontrou (ver core/importer.build_summary). Vazio em
    # projetos importados antes dele existir.
    import_summary: dict[str, Any] = field(default_factory=dict)
    # "convert" ou "translate" (ver core/importer.PURPOSES); vazio nos projetos
    # importados antes da escolha existir.
    purpose: str = ""
    # Idiomas e provedor escolhidos na importação para tradução. Valem antes
    # das preferências gerais.
    translation: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


class ProjectStore:
    """Gerencia a pasta de dados onde os projetos vivem."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    # -- caminhos ---------------------------------------------------------

    def project_dir(self, project_id: str) -> Path:
        safe = _safe_id(project_id)
        return self.root / safe

    def _doc_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "document.json"

    def _meta_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "meta.json"

    def images_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "images"

    def source_dir(self, project_id: str) -> Path:
        return self.project_dir(project_id) / "source"

    # -- escrita ----------------------------------------------------------

    def create(
        self,
        document: Document,
        *,
        source_kind: str = "pdf",
        source_filename: str = "",
        source_bytes: bytes | None = None,
        import_summary: dict[str, Any] | None = None,
        purpose: str = "",
        translation: dict[str, str] | None = None,
    ) -> ProjectMeta:
        project_id = new_id()
        directory = self.project_dir(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        self.images_dir(project_id).mkdir(exist_ok=True)
        self.source_dir(project_id).mkdir(exist_ok=True)

        for name, data in document.images.items():
            (self.images_dir(project_id) / _safe_name(name)).write_bytes(data)

        if source_bytes is not None and source_filename:
            (self.source_dir(project_id) / _safe_name(source_filename)).write_bytes(
                source_bytes
            )

        now = _now()
        meta = ProjectMeta(
            id=project_id,
            title=document.title,
            created_at=now,
            updated_at=now,
            source_kind=source_kind,
            source_filename=source_filename,
            word_count=document.word_count(),
            total_translatable=len(document.translatable_blocks()),
            translated_count=_count_translated(document),
            import_summary=import_summary or {},
            purpose=purpose,
            translation=translation or {},
        )

        self.save_document(project_id, document)
        self._write_meta(meta)
        return meta

    def save_document(self, project_id: str, document: Document) -> None:
        with _lock_for(project_id):
            _atomic_write_json(self._doc_path(project_id), document.to_dict())

            meta = self.get_meta(project_id)
            if meta:
                meta.updated_at = _now()
                meta.title = document.title
                meta.word_count = document.word_count()
                meta.total_translatable = len(document.translatable_blocks())
                meta.translated_count = _count_translated(document)
                _atomic_write_json(self._meta_path(project_id), meta.to_dict())

    def update(self, project_id: str, change: Callable[[Document], T]) -> T:
        """Lê, aplica `change` e grava, tudo sob a trava do projeto.

        Quem altera o documento deve passar por aqui. Carregar, mexer e chamar
        save_document() em separado deixa uma janela em que outra gravação (a
        tradução em segundo plano, por exemplo) é sobrescrita pela cópia velha.
        Levanta FileNotFoundError se o projeto não existe mais.
        """
        with _lock_for(project_id):
            document = self.load_document(project_id)
            result = change(document)
            self.save_document(project_id, document)
            return result

    def update_meta(self, project_id: str, change: Callable[[ProjectMeta], None]) -> ProjectMeta:
        """Lê os metadados, aplica `change` e grava, sob a trava do projeto.

        Levanta FileNotFoundError se o projeto não existe mais.
        """
        with _lock_for(project_id):
            meta = self.get_meta(project_id)
            if meta is None:
                raise FileNotFoundError(f"Projeto não encontrado: {project_id}")
            change(meta)
            self._write_meta(meta)
            return meta

    def _write_meta(self, meta: ProjectMeta) -> None:
        _atomic_write_json(self._meta_path(meta.id), meta.to_dict())

    # -- leitura ----------------------------------------------------------

    def load_document(self, project_id: str, *, with_images: bool = False) -> Document:
        """Carrega o documento. As imagens só vêm com with_images=True.

        Só a exportação precisa dos bytes das imagens; a prévia as serve direto
        da pasta. Ler tudo a cada salvamento automático pesava em livros
        ilustrados.
        """
        path = self._doc_path(project_id)
        if not path.exists():
            raise FileNotFoundError(f"Projeto não encontrado: {project_id}")

        document = Document.from_dict(json.loads(path.read_text("utf-8")))

        images_dir = self.images_dir(project_id)
        if with_images and images_dir.exists():
            for image in images_dir.iterdir():
                if image.is_file():
                    document.images[image.name] = image.read_bytes()

        return document

    def get_meta(self, project_id: str) -> ProjectMeta | None:
        path = self._meta_path(project_id)
        if not path.exists():
            return None
        try:
            return ProjectMeta(**json.loads(path.read_text("utf-8")))
        except (json.JSONDecodeError, TypeError):
            return None

    def list_projects(self) -> list[ProjectMeta]:
        projects = [
            meta
            for entry in self.root.iterdir()
            if entry.is_dir()
            for meta in [self.get_meta(entry.name)]
            if meta is not None
        ]
        return sorted(projects, key=lambda m: m.updated_at, reverse=True)

    def delete(self, project_id: str) -> bool:
        # Sob a trava: uma gravação em andamento termina antes, e a seguinte
        # encontra o projeto já apagado (FileNotFoundError em update).
        with _lock_for(project_id):
            directory = self.project_dir(project_id)
            if not directory.exists():
                return False
            shutil.rmtree(directory)
            return True


# --------------------------------------------------------------------------


def _count_translated(document: Document) -> int:
    return sum(
        1
        for b in document.translatable_blocks()
        if b.translated is not None and b.translated.strip()
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False, suffix=".tmp"
    )
    try:
        with handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except Exception:
        Path(handle.name).unlink(missing_ok=True)
        raise


def _safe_id(value: str) -> str:
    """Impede que um id vindo da URL escape da pasta de dados."""
    cleaned = "".join(c for c in value if c.isalnum() or c in "-_")
    if not cleaned:
        raise ValueError("Identificador de projeto inválido")
    return cleaned


def _safe_name(value: str) -> str:
    return Path(value).name.replace("..", "_") or "arquivo"
