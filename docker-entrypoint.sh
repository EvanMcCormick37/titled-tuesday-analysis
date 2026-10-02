#!/bin/sh
# kalshi-core install strategy (resolved on container start):
#   1. Already baked in via GH_TOKEN build arg?  Use it.
#   2. /opt/kalshi-core bind-mounted (local dev)?  pip install -e.
#   3. $GH_TOKEN set as a runtime env var (Railway prod)?  pip install git+https.
#   4. None of the above — bail with a clear error.
set -e

if ! python -c "import kalshi_core" 2>/dev/null; then
    if [ -d "/opt/kalshi-core" ] && [ -f "/opt/kalshi-core/pyproject.toml" ]; then
        echo "[entrypoint] Installing kalshi-core from bind mount..."
        pip install --no-cache-dir --quiet --root-user-action=ignore -e /opt/kalshi-core
    elif [ -n "$GH_TOKEN" ]; then
        REF="${KALSHI_CORE_REF:-main}"
        echo "[entrypoint] Installing kalshi-core from GitHub (ref=$REF)..."
        pip install --no-cache-dir --quiet --root-user-action=ignore \
            "git+https://${GH_TOKEN}@github.com/EvanMcCormick37/kalshi-core.git@${REF}"
    else
        echo "ERROR: kalshi-core is not installed. Provide one of:" >&2
        echo "  - GH_TOKEN env var (production: GitHub PAT with repo scope)" >&2
        echo "  - -v <path-to-kalshi-core>:/opt/kalshi-core (local dev bind mount)" >&2
        echo "  - --build-arg GH_TOKEN=<pat> at build time" >&2
        exit 1
    fi
fi

exec "$@"
