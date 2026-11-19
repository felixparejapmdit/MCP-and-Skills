FROM python:3.11.18-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39

LABEL org.opencontainers.image.source="https://github.com/felixparejapmdit/MCP-and-Skills"
LABEL org.opencontainers.image.description="Isolated MCP and agent skills developer sandbox"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MCP_SKILLS_CONTAINER=1 \
    MCP_SKILLS_SANDBOX=1

RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --create-home app \
    && mkdir -p /app /data/agent-home /data/state && chown -R app:app /app /data
WORKDIR /app

# .dockerignore is an allowlist: no profiles, exports, snapshots, tests, caches,
# or credentials enter the build context.
COPY --chown=app:app *.py catalog.json suggestions.json /app/
COPY --chown=app:app web/ /app/web/

USER app
VOLUME ["/data"]
EXPOSE 8766
HEALTHCHECK --interval=20s --timeout=3s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8766/health', timeout=2).read()"]
CMD ["python", "server.py", "--serve", "--bind", "0.0.0.0", "--home", "/data/agent-home", "--state-dir", "/data/state", "--public-origin", "http://127.0.0.1:8877"]
