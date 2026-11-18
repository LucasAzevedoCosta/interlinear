# Imagem do Interlinear. Todas as dependências têm wheels prontos para
# linux/amd64 e linux/arm64, então não é preciso compilador.
FROM python:3.12-slim

# 1 instala o Claude Code, para traduzir pela assinatura do Claude
# (experimental, ~350 MB a mais). Veja docs/provedores.md.
ARG INTERLINEAR_WITH_CLAUDE=0

# O login do Claude Code fica todo em /claude, um volume: sobrevive a rebuild.
# A versão do Claude Code é a da imagem; reconstruir atualiza.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INTERLINEAR_DATA_DIR=/data \
    INTERLINEAR_OPEN_BROWSER=0 \
    INTERLINEAR_DOCKER=1 \
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

COPY backend/ backend/
COPY frontend/ frontend/

USER interlinear

VOLUME ["/data"]
EXPOSE 8000

WORKDIR /app/backend

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# Dentro do container precisa escutar em 0.0.0.0 para a porta publicada
# funcionar. Quem limita o acesso à máquina local é o compose (127.0.0.1:8000).
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
