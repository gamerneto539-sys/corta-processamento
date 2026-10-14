FROM denoland/deno:bin-2.9.7 AS deno
FROM python:3.13.16-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CORTA_DATA_DIR=/tmp/corta PORT=8080
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno
WORKDIR /app
COPY cloud/requirements.txt cloud/requirements.txt
RUN pip install --no-cache-dir -r cloud/requirements.txt && useradd --create-home --uid 10001 corta
COPY desktop/engine.py desktop/engine.py
COPY cloud/server.py cloud/server.py
USER corta
EXPOSE 8080
CMD ["python", "cloud/server.py"]
