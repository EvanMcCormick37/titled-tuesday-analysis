"""Portfolio tab — current Kalshi TT positions.

Phase 2 stub: shows the raw positions DataFrame from Kalshi API. The full
P&L simulation + Kelly sizing lands in a later phase; for now this is the
minimum useful read-only view of money at risk.
"""
from __future__ import annotations

import streamlit as st


def _try_load_positions():
    """Returns (df, err). df is None on error; err is None on success."""
    try:
        from kalshi_core import KalshiClient
        from src.kalshi_tt import get_tt_positions_df
    except ImportError as e:
        return None, f'Kalshi client not available: {e}'

    try:
        client = KalshiClient.from_env()
    except Exception as e:  # noqa: BLE001
        return None, f'Could not construct KalshiClient.from_env(): {e}'
    if client._private_key is None:  # noqa: SLF001
        return None, ('No Kalshi credentials loaded. Set KALSHI_API_KEY_ID + '
                      'KALSHI_PRIVATE_KEY_B64 env vars.')

    try:
        df = get_tt_positions_df(client)
    except Exception as e:  # noqa: BLE001
        return None, f'get_tt_positions_df failed: {e}'
    return df, None


def render() -> None:
    st.subheader('Portfolio')

    if st.button('Refresh positions', key='port_refresh'):
        st.rerun()

    df, err = _try_load_positions()
    if err:
        st.warning(err)
        st.caption('P&L simulation + Kelly sizing arrive in a later phase.')
        return

    if df is None or df.empty:
        st.info('No open TT positions.')
        return

    # Sync fills first so the central kalshi.db has the latest fee data; without
    # this, newly-executed trades show $0 fees until the next sync.
    if st.button('Sync fills from Kalshi', key='port_sync_fills',
                 help='Fetch latest fills from Kalshi API into the central DB '
                      '(needed for accurate fee calculation).'):
        from kalshi_core import KalshiClient, sync_fills
        with st.spinner('Syncing fills...'):
            try:
                client = KalshiClient.from_env()
                n = sync_fills(client)
                st.success(f'Synced {n} new fills.')
            except Exception as e:  # noqa: BLE001
                st.exception(e)
        st.rerun()

    st.dataframe(df, width='stretch', hide_index=True)

    try:
        cost        = float(df['cost'].abs().sum())
        fees        = float(df['fees'].sum())
        total       = float(df['cost_incl_fees'].abs().sum())
        pct_fees    = (fees / cost * 100) if cost else 0.0
        cols = st.columns(3)
        cols[0].metric('Exposure (excl. fees)', f'${cost:,.2f}')
        cols[1].metric('Fees paid', f'${fees:,.2f}', f'{pct_fees:.1f}% of exposure')
        cols[2].metric('Out-of-pocket', f'${total:,.2f}')
        if fees == 0.0:
            st.caption('⚠ Fees show $0 — tap "Sync fills from Kalshi" to pull the '
                       'latest fee data into the central DB.')
    except Exception:  # noqa: BLE001
        pass
