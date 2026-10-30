"""Ponto de entrada da aplicação.

O servidor serve a API e os arquivos da interface no mesmo endereço, o que
evita CORS e faz o app abrir com um comando só.

Por padrão escuta apenas em 127.0.0.1: as chaves de API ficam nesta máquina e
nada deve responder na rede local sem o usuário pedir.
"""

from __future__ import annotations

import logging
import os
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import data_root
from .routes import ai, projects

logging.basicConfig(
    level=os.environ.get("INTERLINEAR_LOG_LEVEL", "INFO"),
    format="%(asctime)s  %(levelname)-7s %(name)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("interlinear")

FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    root = data_root()
    root.mkdir(parents=True, exist_ok=True)
    log.info("Dados em %s", root)
    yield
    log.info("Encerrando")


app = FastAPI(
    title="Interlinear",
    description="Converte PDF em EPUB, edita e traduz com a IA de sua escolha.",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(projects.router)
app.include_router(ai.router)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "data_dir": str(data_root())}


@app.exception_handler(ValueError)
async def value_error_handler(_request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})


# A interface é servida por último para não capturar as rotas da API.
if FRONTEND_DIR.exists():
    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIR / "assets")
        if (FRONTEND_DIR / "assets").exists()
        else StaticFiles(directory=FRONTEND_DIR),
        name="assets",
    )

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(FRONTEND_DIR / "index.html")

    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


def run() -> None:
    """Sobe o servidor e abre o navegador. Chamado por `interlinear` ou `python -m`."""
    import uvicorn

    host = os.environ.get("INTERLINEAR_HOST", "127.0.0.1")
    port = int(os.environ.get("INTERLINEAR_PORT", "8000"))

    if os.environ.get("INTERLINEAR_OPEN_BROWSER", "1") == "1":
        webbrowser.open(f"http://{host}:{port}")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
