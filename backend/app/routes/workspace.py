"""Áreas de trabalho da bancada: a disposição dos painéis.

O servidor só guarda e devolve o JSON; quem entende o formato é o frontend
(frontend/layout.js), que também descarta o que não souber montar. Fica num
arquivo próprio, e não no settings.json, porque aquele guarda chaves de API e
este é gravado a cada arraste de painel.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException

from ..config import data_root

router = APIRouter(prefix="/api/ui", tags=["ui"])

# Uma disposição real tem poucos KB. O limite só impede que um cliente com
# defeito encha o disco gravando a cada arraste.
MAX_BYTES = 256 * 1024


def workspace_path() -> Path:
    return data_root() / "workspace.json"


@router.get("/workspace")
async def read_workspace() -> dict[str, Any]:
    """Devolve o que foi salvo, ou null quando não há nada (ou está corrompido)."""
    path = workspace_path()
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"workspace": None}
    return {"workspace": data if isinstance(data, dict) else None}


@router.put("/workspace")
async def write_workspace(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    text = json.dumps(payload, ensure_ascii=False)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise HTTPException(413, "Disposição dos painéis grande demais")

    path = workspace_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Grava ao lado e troca: um arraste interrompido não deixa o arquivo pela metade.
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
    return {"ok": True}
