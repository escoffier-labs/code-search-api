FROM python:3.12-slim AS builder

WORKDIR /build
COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install --no-cache-dir build && python -m build --wheel


FROM python:3.12-slim

LABEL org.opencontainers.image.source="https://github.com/escoffier-labs/code-search-api"
LABEL org.opencontainers.image.description="Local semantic code search with Ollama embeddings and SQLite"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    CODE_SEARCH_DB=/data/code_index.db \
    CODE_SEARCH_WORKSPACE=/workspace \
    OLLAMA_URL=http://host.docker.internal:11434

WORKDIR /app
COPY --from=builder /build/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

RUN useradd --system --create-home --uid 10001 codesearch \
    && mkdir -p /data /workspace \
    && chown -R codesearch:codesearch /data /workspace
USER codesearch

VOLUME ["/data", "/workspace"]
EXPOSE 5204

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import httpx, sys; sys.exit(0 if httpx.get('http://localhost:5204/health').status_code == 200 else 1)"

ENTRYPOINT ["code-search-api"]
CMD ["serve", "--host", "0.0.0.0", "--port", "5204"]
