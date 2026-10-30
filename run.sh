#!/usr/bin/env bash
# Sobe o Interlinear. Cria o ambiente virtual e instala as dependências na primeira vez.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "Criando ambiente virtual…"
  python3 -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

cd backend
exec ../.venv/bin/python -m uvicorn app.main:app \
  --host "${INTERLINEAR_HOST:-127.0.0.1}" --port "${INTERLINEAR_PORT:-8000}"
