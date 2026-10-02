"""Trade tab — deliberately deferred to Phase 3.

The plan sequences trading last (after the data layer + visualisation prove out)
and insists on a two-tap dry-run → live confirm UX. Rendering a working but
unlocked trade button here would violate the "no money moves without an
explicit tap" invariant, so we show a stub instead.
"""
import streamlit as st

from app.state import current_status


def render() -> None:
    st.subheader('Trade')
    s = current_status()

    st.info(
        'Trade controls arrive in Phase 3. Until then, use the CLI scripts '
        'from your laptop:\n\n'
        '```\n'
        'python scripts/place_bids.py          # dry run\n'
        'python scripts/place_bids.py --live   # execute\n'
        'python scripts/take_trades.py --live\n'
        'python scripts/cancel_bids.py --live\n'
        '```'
    )

    if s.state != 'completed':
        st.warning(
            f'Pipeline is `{s.state}` — trading will be disabled until the pipeline '
            'reaches `completed` (stale-data guard, per plan §8).'
        )
