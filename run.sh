#!/usr/bin/env bash
# Sobe o Interlinear. Cria o ambiente virtual e instala as dependências na primeira vez.
# No Windows, use run.ps1.
set -euo pipefail
cd "$(dirname "$0")"

# O projeto exige Python 3.11+. O python3 do macOS ainda é 3.9, então procura
# uma versão nova antes de cair no python3. PYTHON=... escolhe outro.
find_python() {
  for candidate in "${PYTHON:-}" python3.13 python3.12 python3.11 python3; do
    [ -n "$candidate" ] || continue
    if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

if [ ! -d .venv ]; then
  if ! python=$(find_python); then
    echo "O Interlinear precisa do Python 3.11 ou mais recente." >&2
    echo "Instale por https://www.python.org/downloads/ ou pelo gerenciador do sistema" >&2
    echo "(brew install python@3.12, apt install python3.12, ...) e rode de novo." >&2
    exit 1
  fi
  echo "Criando ambiente virtual com $("$python" --version)…"
  "$python" -m venv .venv
  ./.venv/bin/pip install --quiet --upgrade pip
  ./.venv/bin/pip install --quiet -r requirements.txt
fi

cd backend
exec ../.venv/bin/python -m uvicorn app.main:app \
  --host "${INTERLINEAR_HOST:-127.0.0.1}" --port "${INTERLINEAR_PORT:-8000}"
