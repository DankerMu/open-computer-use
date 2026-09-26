# SPDX-License-Identifier: FSL-1.1-Apache-2.0
# Copyright (c) 2025 Open Computer Use Contributors
# Computer Use Orchestrator Dockerfile - Production
FROM python:3.12-slim

LABEL maintainer="Computer Use Orchestrator"
LABEL description="Orchestrator for Computer Use containers"
LABEL version="1.0.0"

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY ./requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY ./app.py .
COPY ./mcp_tools.py .
COPY ./mcp_resources.py .
COPY ./uploads.py .
COPY ./system_prompt.py .
COPY ./skill_manager.py .
COPY ./docker_manager.py .
COPY ./sandbox_dns.py .
COPY ./outputs_broker.py .
COPY ./context_vars.py .
COPY ./docs_html.py .
COPY ./security.py .
COPY ./auth_guard.py .
COPY ./ws_recheck.py .
COPY ./cli_runtime.py .
COPY ./cli_adapters/ ./cli_adapters/
COPY ./static/ ./static/

# MCP API Key for authorization
ENV MCP_API_KEY=""

# Expose port
EXPOSE 8081

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8081/health')"

# Preflight runs in this process. A worker-only lifespan check is not enough:
# uvicorn --workers respawns a worker that exits during startup.
CMD ["sh", "-c", "python -c 'import auth_guard,sys; sys.exit(auth_guard.startup_preflight())' && exec python -m uvicorn app:app --host 0.0.0.0 --port 8081 --workers 2 --no-proxy-headers"]
