"""Titled Tuesday — mobile dashboard.

Entry point for the tt-app Streamlit service. Guards access via auth.py,
renders the persistent status banner, then routes to one of six tabs.

Run locally:
    streamlit run app/dashboard.py

Env vars:
    DASHBOARD_PASSWORD  (optional) — password gate; unset = open (dev mode)
    DATABASE_URL        — Postgres (prod) or empty for local SQLite
    KALSHI_*            — for the Portfolio / Trade tabs (Phases 2b/3)
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import streamlit as st

st.set_page_config(
    page_title='Titled Tuesday',
    page_icon=':chess_pawn:',
    layout='wide',
    initial_sidebar_state='collapsed',
)

from app.auth import require_auth
from app import banner
from app.tabs import odds, adjustments, pipeline as pipeline_tab, portfolio, trade, jobs

require_auth()

banner.render()

tab_odds, tab_adj, tab_pipe, tab_port, tab_trade, tab_jobs = st.tabs([
    'Odds', 'Attendance', 'Pipeline', 'Portfolio', 'Trade', 'Jobs',
])
with tab_odds:    odds.render()
with tab_adj:     adjustments.render()
with tab_pipe:    pipeline_tab.render()
with tab_port:    portfolio.render()
with tab_trade:   trade.render()
with tab_jobs:    jobs.render()
