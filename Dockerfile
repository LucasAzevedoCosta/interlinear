# Imagem do Interlinear. Todas as dependências têm wheels prontos para
# linux/amd64 e linux/arm64, então não é preciso compilador.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    INTERLINEAR_DATA_DIR=/data \
    INTERLINEAR_OPEN_BROWSER=0

WORKDIR /app

# Dependências primeiro: a camada só é refeita quando requirements.txt muda.
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY backend/ backend/
COPY frontend/ frontend/

# Usuário sem privilégios, dono da pasta de dados (projetos e settings.json).
RUN useradd --create-home --uid 1000 interlinear \
    && mkdir -p /data \
    && chown interlinear:interlinear /data
USER interlinear

VOLUME ["/data"]
EXPOSE 8000

WORKDIR /app/backend

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)"

# Dentro do container precisa escutar em 0.0.0.0 para a porta publicada
# funcionar. Quem limita o acesso à máquina local é o compose (127.0.0.1:8000).
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
