# Imagem do Interlinear. O Interlinear só roda em container: este arquivo é a
# única forma de executar o app, os testes e as ferramentas de qualidade.
#
# Três estágios:
#   base     sistema, Tesseract, usuário sem privilégios e dependências
#   dev      base + dependências de desenvolvimento e o código com os testes
#            (docker compose run --rm dev pytest; é o que a CI roda)
#   runtime  base + só o código da aplicação. É o último, então é o padrão do
#            `docker build` e do compose.
#
# Todas as dependências têm wheels prontos para linux/amd64 e linux/arm64,
# então não é preciso compilador.

FROM python:3.12-slim AS base

# 1 instala o Claude Code, para traduzir pela assinatura do Claude
# (experimental, ~350 MB a mais). Veja docs/provedores.md.
ARG INTERLINEAR_WITH_CLAUDE=0

# O login do Claude Code fica todo em /claude, um volume: sobrevive a rebuild.
# A versão do Claude Code é a da imagem; reconstruir atualiza.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    INTERLINEAR_DATA_DIR=/data \
    CLAUDE_CONFIG_DIR=/claude \
    DISABLE_AUTOUPDATER=1 \
    PATH=/home/interlinear/.local/bin:$PATH

WORKDIR /app

# Tesseract para o OCR de PDFs escaneados (Apache-2.0), com português; o
# inglês vem junto. Outros idiomas: tesseract-ocr-<código> e
# INTERLINEAR_OCR_LANGS (docs/conversao.md). curl só para instalar o Claude Code.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-por \
       $([ "$INTERLINEAR_WITH_CLAUDE" = "1" ] && echo curl ca-certificates) \
    && rm -rf /var/lib/apt/lists/*

# Usuário sem privilégios, dono da pasta de dados (projetos e settings.json) e
# da do login do Claude Code.
RUN useradd --create-home --uid 1000 interlinear \
    && mkdir -p /data /claude \
    && chown interlinear:interlinear /data /claude

# Antes do código: mudar o Interlinear não baixa o Claude Code de novo.
USER interlinear
RUN if [ "$INTERLINEAR_WITH_CLAUDE" = "1" ]; then \
        curl -fsSL https://claude.ai/install.sh -o /tmp/claude-install.sh \
        && bash /tmp/claude-install.sh \
        && rm /tmp/claude-install.sh \
        && claude --version; \
    fi
USER root

# Dependências primeiro: a camada só é refeita quando requirements.txt muda.
COPY requirements.txt .
RUN pip install -r requirements.txt

VOLUME ["/data"]


# --------------------------------------------------------------------------
FROM base AS dev

# Ferramentas de teste e de qualidade (pytest, ruff, pip-audit, bandit).
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt

# O código vai junto para a imagem funcionar sozinha na CI. No dia a dia, o
# compose monta a pasta do projeto por cima, só para leitura: nada que os
# testes ou o servidor gerem vai parar no seu computador.
COPY pyproject.toml ./
COPY backend/ backend/
COPY frontend/ frontend/
COPY tests/ tests/

ENV RUFF_CACHE_DIR=/tmp/ruff-cache

USER interlinear
CMD ["pytest"]


# --------------------------------------------------------------------------
FROM base AS runtime

COPY backend/ backend/
COPY frontend/ frontend/

USER interlinear

EXPOSE 8000

WORKDIR /app/backend

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# Dentro do container precisa escutar em 0.0.0.0 para a porta publicada
# funcionar. Quem limita o acesso à máquina local é o compose (127.0.0.1:8000).
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
