"""Odds tab — Plotly port of the three main model-viz charts, plus data tables.

Mobile-friendly: one chart per screen with a radio for N and raw/adjusted toggle.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from app.state import load_predictions, username_to_player_map

_N_VALUES = (1, 3, 5, 8, 10)
_N_LABELS = {1: 'Winner', 3: 'Top 3', 5: 'Top 5', 8: 'Top 8', 10: 'Top 10'}


def _name_col(df: pd.DataFrame) -> pd.Series:
    m = username_to_player_map()
    return df.index.to_series().map(m).fillna(df.index.to_series())


def _topn_df(preds: pd.DataFrame, n: int, limit: int = 30) -> pd.DataFrame:
    """Return the top `limit` players by P_top{n}, sorted descending."""
    col = f'P_top{n}'
    if col not in preds.columns:
        return pd.DataFrame()
    out = preds.nlargest(limit, col).copy()
    out['player'] = _name_col(out)
    return out.reset_index()


def _render_chances_chart(preds: pd.DataFrame, n: int) -> None:
    df = _topn_df(preds, n, limit=30)
    if df.empty:
        st.info('No predictions available yet.')
        return

    col_p = f'P_top{n}'
    col_ip = f'P_top{n}_given_play'
    df['label_pct'] = (df[col_p] * 100).round(1).astype(str) + '%'
    df['ip_pct']    = (df[col_ip] * 100).round(1).astype(str) + 'ip%'

    fig = px.bar(
        df, x=col_p, y='player', orientation='h',
        color=col_ip, color_continuous_scale='Plasma',
        labels={col_p: 'P(Top-N)', col_ip: 'P | plays', 'player': ''},
        hover_data={col_p: ':.3%', col_ip: ':.3%', 'p_participate': ':.3%',
                    'player': False, 'label_pct': False, 'ip_pct': False},
        height=720,
    )
    fig.update_layout(
        title=f'Top players by chance to make {_N_LABELS.get(n, f"Top {n}")}',
        yaxis={'autorange': 'reversed'},
        margin={'l': 10, 'r': 10, 't': 50, 'b': 30},
    )
    fig.update_xaxes(tickformat='.0%')
    st.plotly_chart(fig, use_container_width=True)


def _render_prices_chart(preds: pd.DataFrame, n: int) -> None:
    df = _topn_df(preds, n, limit=30)
    if df.empty:
        return

    col_p = f'P_top{n}'
    # 33 % house margin fair-price convention from the existing viz.
    df['yes_price'] = (df[col_p] * (2 / 3) * 100).round(1)
    df['no_price']  = ((1 - df[col_p] * (3 / 2)) * 100).clip(lower=0).round(1)

    fig = go.Figure()
    fig.add_bar(y=df['player'], x=df['yes_price'], orientation='h',
                marker_color='#2ca02c', name='YES price (¢)',
                text=df['yes_price'], textposition='outside')
    fig.add_bar(y=df['player'], x=-df['no_price'], orientation='h',
                marker_color='#d62728', name='NO price (¢)',
                text=df['no_price'], textposition='outside')
    fig.update_layout(
        barmode='relative',
        title=f'Estimated fair prices (33% margin) — {_N_LABELS.get(n, f"Top {n}")}',
        yaxis={'autorange': 'reversed'},
        margin={'l': 10, 'r': 10, 't': 50, 'b': 30},
        height=720,
        xaxis={'title': 'cents'},
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_ip_adv_chart(preds: pd.DataFrame, n: int) -> None:
    col_p = f'P_top{n}'
    col_adv = f'ip_adv_top{n}'
    if col_adv not in preds.columns:
        st.info('IP-advantage not available.')
        return

    df = preds[preds[col_p] > 0.01].nlargest(25, col_adv).copy()
    df['player'] = _name_col(df)
    df = df.reset_index()
    if df.empty:
        st.info('No players with P(Top-N) > 1%.')
        return

    fig = px.bar(
        df, x=col_adv, y='player', orientation='h',
        color=col_p, color_continuous_scale='Plasma',
        labels={col_adv: 'IP advantage', col_p: 'P(Top-N)', 'player': ''},
        hover_data={col_adv: ':.2f', col_p: ':.3%',
                    f'P_top{n}_given_play': ':.3%', 'player': False},
        height=620,
    )
    fig.update_layout(
        title=f'IP-advantage — {_N_LABELS.get(n, f"Top {n}")} (P[play] ≥ 1%)',
        yaxis={'autorange': 'reversed'},
        margin={'l': 10, 'r': 10, 't': 50, 'b': 30},
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_table(preds: pd.DataFrame, n: int) -> None:
    df = _topn_df(preds, n, limit=50)
    if df.empty:
        return
    show = df[['player', 'p_participate', f'P_top{n}_given_play', f'P_top{n}']].copy()
    show.columns = ['Player', 'P(play)', 'P(top | play)', 'P(top)']
    for col in show.columns[1:]:
        show[col] = (show[col] * 100).round(2).astype(str) + '%'
    st.dataframe(show, width='stretch', hide_index=True)


def render() -> None:
    st.subheader('Odds')

    cols = st.columns([2, 2, 2])
    with cols[0]:
        source = st.radio('Source', ('Adjusted', 'Raw'), horizontal=True,
                          label_visibility='collapsed')
    with cols[1]:
        n = st.selectbox('N', _N_VALUES, format_func=lambda v: _N_LABELS[v])
    with cols[2]:
        view = st.selectbox('View', ('Chances', 'Fair prices', 'IP advantage', 'Table'))

    preds = load_predictions(adjusted=(source == 'Adjusted'))
    if preds.empty:
        st.info('No predictions in DB yet. Run the pipeline + Save & recompute.')
        return

    st.caption(
        f'{len(preds):,} players · showing {_N_LABELS[n]} · {source.lower()} predictions · '
        f'tourn_date={preds["tourn_date"].iloc[0] if "tourn_date" in preds.columns else "—"}'
    )

    if view == 'Chances':      _render_chances_chart(preds, n)
    elif view == 'Fair prices':_render_prices_chart(preds, n)
    elif view == 'IP advantage':_render_ip_adv_chart(preds, n)
    else:                      _render_table(preds, n)
