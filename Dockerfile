# Titled Tuesday pipeline container.
#
# Two kalshi-core install paths:
#   - Local dev: bind-mount the sibling repo at /opt/kalshi-core; the entrypoint
#     installs it in editable mode on container start.
#   - Railway / CI: pass GH_TOKEN as a build arg → pip installs kalshi-core from
#     a private GitHub repo at build time, so no mount is needed at runtime.
FROM python:3.13-slim

ARG GH_TOKEN=""
ARG KALSHI_CORE_REF="main"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Install kalshi-core at build time if we were given a GH_TOKEN; otherwise
# wait for the entrypoint to find a bind-mount.
RUN if [ -n "$GH_TOKEN" ]; then \
      pip install --no-cache-dir --root-user-action=ignore \
        "git+https://${GH_TOKEN}@github.com/EvanMcCormick37/kalshi-core.git@${KALSHI_CORE_REF}"; \
    fi

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

COPY . /app/

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "scripts/weekly_pipeline.py"]
