# Incident Commander: the reference product built on agent-harness-sdk.
FROM python:3.15-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY agent_harness_sdk ./agent_harness_sdk
RUN pip install ".[server,mcp]"
COPY examples/incident_commander ./incident_commander
RUN useradd -r -u 10001 app && mkdir /data && chown app /data
USER 10001
ENV IC_DB=/data/incidents.db LOG_FORMAT=json LOG_LEVEL=INFO PORT=8080
EXPOSE 8080
HEALTHCHECK CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')"
CMD ["python", "-m", "incident_commander", "serve"]
