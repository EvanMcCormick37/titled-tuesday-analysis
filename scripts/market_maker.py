#!/usr/bin/env python3
"""tt-mm service: the reactive two-tier market maker for every registered market.

Shadow mode by default: it subscribes to live books and computes every quote,
but sends nothing. Environment:

    MM_LIVE=1          send orders (otherwise shadow)
    MM_SCOUTS=0        anchors only (scouts also have a per-market switch on the dashboard)
    MM_MARKETS=a,b     limit to these market slugs (default: all registered)
    MM_JOURNAL_DIR     where the JSONL journal goes (default data/mm_journal)

Run exactly one replica. Parameters, pause/resume and player reposts are
changed from the dashboard's Market maker page and picked up within seconds.
"""
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kalshi_core import KalshiClient, KalshiWS

from src.mm.engine import Engine, from_env_flags
from src.mm.gateway import LiveGateway, ShadowGateway
from src.mm.journal import Journal
from src.strategies import MARKETS


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    live, scouts = from_env_flags()
    wanted = [s for s in os.environ.get('MM_MARKETS', '').split(',') if s]
    specs = [m.spec for slug, m in MARKETS.items() if not wanted or slug in wanted]
    client = KalshiClient.from_env()

    budget = (100.0, 100.0)
    try:
        limits = client.get_account_limits()['write']
        budget = (float(limits['bucket_capacity']), float(limits['refill_rate']))
    except Exception as e:  # noqa: BLE001
        logging.warning('could not read account limits (%s); assuming Basic tier', e)

    engine = Engine(specs, client, LiveGateway(client) if live else ShadowGateway(),
                    ws_factory=lambda: KalshiWS(client), scouts=scouts, journal=Journal(),
                    write_budget=budget)
    logging.info('tt-mm starting: %s, scouts %s, markets %s, write budget %s',
                 'LIVE' if live else 'SHADOW', 'on' if scouts else 'off',
                 [s.slug for s in specs], budget)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, engine.stop)
            except NotImplementedError:          # Windows
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(engine.stop))
        await engine.run()

    asyncio.run(_run())


if __name__ == '__main__':
    main()
