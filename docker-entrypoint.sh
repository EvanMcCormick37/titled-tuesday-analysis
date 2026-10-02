#!/bin/sh
# kalshi-core install strategy:
#   - If the image already has kalshi-core (built via GH_TOKEN build arg in
#     Phase 1 / Railway), skip — we're already good.
#   - Else if /opt/kalshi-core is bind-mounted (local dev), install editable.
#   - Else error out — no viable path to the shared package.
set -e

if ! python -c "import kalshi_core" 2>/dev/null; then
    if [ -d "/opt/kalshi-core" ] && [ -f "/opt/kalshi-core/pyproject.toml" ]; then
        pip install --no-cache-dir --quiet --root-user-action=ignore -e /opt/kalshi-core
    else
        echo "ERROR: kalshi-core is not installed and no bind mount at /opt/kalshi-core." >&2
        echo "       Rebuild the image with --build-arg GH_TOKEN=<pat> or run with" >&2
        echo "       -v <path-to-kalshi-core>:/opt/kalshi-core" >&2
        exit 1
    fi
fi

exec "$@"
