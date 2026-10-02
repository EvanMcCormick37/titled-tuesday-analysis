#!/bin/sh
# Install the bind-mounted kalshi-core as an editable package on container start.
# In Phase 1 (Railway), kalshi-core is baked in at build time via git+https and
# this install becomes a no-op (the directory isn't mounted).
set -e

if [ -d "/opt/kalshi-core" ] && [ -f "/opt/kalshi-core/pyproject.toml" ]; then
    pip install --no-cache-dir --quiet --root-user-action=ignore -e /opt/kalshi-core
fi

exec "$@"
