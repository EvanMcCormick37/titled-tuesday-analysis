"""Reactive two-tier market maker (scout + anchor) for weekly top-N markets.

    config      parameters (MMParams) and client_order_id prefixes
    book        order books from the WebSocket feed, with an ex-own view
    quoting     pure scout/anchor pricing and sizing
    fair_value  validated fair values from latest_model_predictions
    risk        position / player / event limits
    oms         order manager: target diffing, token bucket, order states
    gateway     LiveGateway (Kalshi REST) and ShadowGateway (sends nothing)
    engine      the asyncio process run by scripts/market_maker.py (tt-mm)
    watchdog    cutoff + dead-man's switch, run by tt-worker
    store       mm_* tables
    journal     JSONL record of every message and action
"""
