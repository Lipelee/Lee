FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY agora ./agora
COPY examples ./examples
# AGORA_ROLE=server (padrão), editor ou motion. A plataforma só sobe em modo privado.
ENV AGORA_DB=/data/agora.db PORT=8080 AGORA_TRUST_PROXY=1 AGORA_REQUIRE_CODE=1 PYTHONUNBUFFERED=1
RUN mkdir -p /data
EXPOSE 8080
CMD ["python", "-m", "agora.run"]
