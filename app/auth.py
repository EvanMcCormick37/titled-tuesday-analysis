"""Dashboard password gate.

Reads $DASHBOARD_PASSWORD. If unset, dashboard is wide open (dev mode).
Otherwise the gate blocks every tab until the user enters the correct password.
Session is remembered via st.session_state until the browser tab closes.
"""
from __future__ import annotations

import os

import streamlit as st


def require_auth() -> bool:
    """Return True once the user is authenticated; else render the gate and halt.

    Call at the top of dashboard.py before any tab-rendering code.
    """
    expected = os.environ.get('DASHBOARD_PASSWORD', '').strip()
    if not expected:
        # No password configured → dev mode, open access.
        return True

    if st.session_state.get('_authed'):
        return True

    st.title('Titled Tuesday dashboard')
    with st.form('auth'):
        pw = st.text_input('Password', type='password')
        submitted = st.form_submit_button('Enter')
    if submitted:
        if pw == expected:
            st.session_state['_authed'] = True
            st.rerun()
        else:
            st.error('Incorrect password.')
    st.stop()
    return False  # unreachable (st.stop raises)
