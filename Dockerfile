# Titled Tuesday pipeline container.
#
# Phase 0 (local): kalshi-core is bind-mounted at /opt/kalshi-core and the
# entrypoint installs it in editable mode on container start.
# Phase 1 (Railway): swap the entrypoint install for `pip install git+https://...`
# at build time; drop the kalshi-core mount from the run command.
FROM python:3.13-slim

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

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

COPY . /app/

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["python", "scripts/weekly_pipeline.py"]
