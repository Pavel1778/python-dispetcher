# NEXUS runtime image. One service, no WSGI object: the app is a stdlib HTTP
# server run as a module, and it is stateful (it runs the dispatch loop), so run
# exactly one instance.
FROM python:3.12-slim

WORKDIR /app

# Only the runtime dependencies; the test/lint tooling is not needed here.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY orchestration ./orchestration
COPY migrations ./migrations
COPY pyproject.toml ./

# A non-root user by default. The copied files must be readable by it; the
# build context's permissions are not preserved for a non-root user otherwise.
RUN useradd --create-home --uid 10001 nexus \
    && chown -R nexus:nexus /app
USER nexus

EXPOSE 8000
# Liveness; readiness (/readyz) also checks the database.
HEALTHCHECK --interval=15s --timeout=5s --retries=5 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=5).status==200 else 1)"

CMD ["python", "-m", "orchestration.web_server", "--host", "0.0.0.0", "--port", "8000"]
