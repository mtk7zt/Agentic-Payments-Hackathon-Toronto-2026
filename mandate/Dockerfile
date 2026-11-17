# One image for both Mandate services: the gateway (default command) and the seller endpoint.
#   docker build -t mandate .
#   docker run --env-file .env -p 8000:8000 mandate
# Secrets come from the environment at run time; none are baked into the image.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN grep -v '^pytest' backend/requirements.txt > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt \
 && useradd --create-home --uid 10001 mandate

COPY backend backend
COPY scripts scripts
RUN rm -rf backend/tests && mkdir -p /home/mandate/.qatom && chown -R mandate:mandate /home/mandate

USER mandate
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/live', timeout=4).status == 200 else 1)"

# One worker: the rate limiter is per process (docs/OPERATIONS.md). Scale with more containers
# behind the proxy only after moving the limiter to a shared store.
CMD ["uvicorn", "backend.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
