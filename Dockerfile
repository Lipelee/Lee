FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fontconfig \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY agora ./agora
COPY examples ./examples
ENV AGORA_DB=/data/agora.db PORT=8080
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "agora.server"]
