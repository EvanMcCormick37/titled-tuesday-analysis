"""Edges tab — live Kalshi orderbook enriched with model EV/ROI/Expected Return.

Mirrors the notebook's "Section 6 — Best trades" cell. Default sort is
**Expected Return (dollars)** so the top of the table is where the actual
money is, not the thinnest-but-highest-ROI spreads.

Data source is `src.trading.compute_edges()`, which is the same helper
`take_trades()` uses — so what you see here is exactly what the taker
would consider.
"""
from __future__ import annotations

import streamlit as st

from app.state import current_status, next_tourn_date_str


def _client():
    from kalshi_core import KalshiClient
    return KalshiClient.from_env()


@st.cache_data(ttl=15, show_spinner='Fetching live orderbook...')
def _fetch_edges(tourn_date: str):
    """Returns (df, unmatched). TTL is short — press Refresh for a hard reload."""
    from src.trading import compute_edges
    df, unmatched = compute_edges(_client(), tourn_date)
    return df, unmatched


def render() -> None:
    st.subheader('Edges')
    tourn_date = next_tourn_date_str()
    if not tourn_date:
        st.info('No pipeline run yet — edges need model predictions in the DB.')
        return

    status = current_status()
    if status.state != 'completed':
        st.warning(
            f'Pipeline state is `{status.state}` — the model predictions used '
            f'to compute EV may be stale.'
        )

    st.caption(f'Target tournament: **{tourn_date}**')

    cols = st.columns(5)
    with cols[0]:
        min_roi = st.number_input('min ROI', 0.5, 5.0, 1.1, 0.05, key='edges_min_roi')
    with cols[1]:
        min_qty = st.number_input('min qty', 1, 1000, 10, 5, key='edges_min_qty',
                                  help='Hide thin levels (likely iceberg decoys).')
    with cols[2]:
        side = st.selectbox('side', ('All', 'YES', 'NO'), key='edges_side')
    with cols[3]:
        n_filter = st.multiselect('N', [1, 3, 5, 8], default=[1, 3, 5, 8],
                                  key='edges_n', help='Top-N categories to include.')
    with cols[4]:
        sort_by = st.selectbox(
            'sort by',
            ('Expected Return', 'ROI', 'Total Cost', 'Quantity Available', 'EV'),
            key='edges_sort',
        )

    if st.button('Refresh', key='edges_refresh'):
        _fetch_edges.clear()
        st.rerun()

    df, unmatched = _fetch_edges(tourn_date)

    if df.empty:
        st.caption('No asks returned from Kalshi (or no markets for this date yet).')
        return

    mask = (df['ROI'] > min_roi) & (df['Quantity Available'] >= min_qty)
    if side != 'All':
        mask &= (df['Side'] == side)
    if n_filter:
        mask &= df['n'].isin(n_filter)
    edges = df[mask].copy()

    if edges.empty:
        st.info(f'No edges match the filter (ROI > {min_roi}, qty ≥ {min_qty}).')
        if unmatched:
            with st.expander(f'{len(unmatched)} unmatched player name(s)'):
                st.text('\n'.join(unmatched))
        return

    edges = edges.sort_values(sort_by, ascending=False)

    exposure   = float(edges['Total Cost'].sum())
    exp_return = float(edges['Expected Return'].sum())
    port_roi   = (exposure + exp_return) / exposure if exposure > 0 else 0.0
    c = st.columns(4)
    c[0].metric('Edges', f'{len(edges)}')
    c[1].metric('Exposure ($)', f'{exposure:.2f}')
    c[2].metric('Expected $ return', f'{exp_return:.2f}')
    c[3].metric('Portfolio ROI', f'{port_roi:.3f}')

    display_cols = ['Market Title', 'n', 'Side', 'Expected Return', 'ROI',
                    'EV', 'Order Price', 'Quantity Available', 'Total Cost']
    st.dataframe(
        edges[display_cols].style.format({
            'EV':              '{:.3f}',
            'Order Price':     '{:.3f}',
            'ROI':             '{:.3f}',
            'Total Cost':      '${:.2f}',
            'Expected Return': '${:.2f}',
        }),
        use_container_width=True,
        hide_index=True,
    )

    if unmatched:
        with st.expander(f'{len(unmatched)} unmatched player name(s)'):
            st.text('\n'.join(unmatched))
