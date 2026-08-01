"""Kalshi REST API client for Titled Tuesday market data and order management."""
from __future__ import annotations

import base64
import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional, Union

import pandas as pd
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import find_dotenv, load_dotenv

_dotenv_path = find_dotenv()
load_dotenv(_dotenv_path)
_dotenv_dir = Path(_dotenv_path).parent if _dotenv_path else Path.cwd()

_PROD_BASE = "https://api.elections.kalshi.com/trade-api/v2"
_DEMO_BASE = "https://demo-api.kalshi.co/trade-api/v2"

# Event ticker templates for a given Kalshi date string (YYMONDD)
_TT_EVENT_TEMPLATES = [
    "KXTITLEDTUESDAY-{date}",    # winner
    "KXTITLEDTUESTOP-{date}T3",  # top 3
    "KXTITLEDTUESTOP-{date}T5",  # top 5
    "KXTITLEDTUESTOP-{date}T8",  # top 8
]


def _next_tuesday(from_date: Optional[date] = None) -> date:
    d = from_date or date.today()
    days_until = (1 - d.weekday()) % 7  # 0 if today is Tuesday
    return d + timedelta(days=days_until)


def _to_kalshi_date(d: Union[date, str, None]) -> str:
    """Convert a date (or ISO string) to Kalshi's YYMONDD ticker format, e.g. 26JUL28."""
    if d is None:
        d = _next_tuesday()
    elif isinstance(d, str):
        d = datetime.strptime(d, "%Y-%m-%d").date()
    return d.strftime("%y%b%d").upper()


class KalshiClient:
    def __init__(
        self,
        api_key_id: str = "",
        private_key_path: str = "",
        env: str = "prod",
    ):
        self.api_key_id = api_key_id or os.environ.get("KALSHI_API_KEY_ID", "")
        self.base = _PROD_BASE if env == "prod" else _DEMO_BASE
        self._session = requests.Session()

        _key_path = private_key_path or os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        if _key_path:
            resolved = Path(_key_path)
            if not resolved.is_absolute():
                resolved = _dotenv_dir / resolved
            self._private_key = serialization.load_pem_private_key(
                resolved.read_bytes(), password=None
            )
        else:
            self._private_key = None

    # ── Auth ─────────────────────────────────────────────────────────────────────

    def _sign_request(self, method: str, path: str, body: Optional[str] = None) -> dict:
        """Return RSA-PSS signed auth headers. body param is accepted but not used in signature per Kalshi spec."""
        if not self._private_key or not self.api_key_id:
            raise RuntimeError("API key ID and private key are required for authenticated requests.")
        ts_ms = str(int(time.time() * 1000))
        msg = (ts_ms + method.upper() + path).encode("utf-8")
        sig = self._private_key.sign(
            msg,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY":       self.api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode("utf-8"),
        }

    # ── HTTP helpers ─────────────────────────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None, auth: bool = False) -> dict:
        headers = self._sign_request("GET", path) if auth else {}
        resp = self._session.get(self.base + path, params=params, headers=headers)
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, body: dict, auth: bool = True) -> dict:
        body_str = json.dumps(body)
        headers = {"Content-Type": "application/json"}
        if auth:
            headers.update(self._sign_request("POST", path, body_str))
        resp = self._session.post(self.base + path, data=body_str, headers=headers)
        resp.raise_for_status()
        return resp.json()

    # ── Market data (public) ──────────────────────────────────────────────────────

    def get_market(self, ticker: str) -> dict:
        return self._get(f"/markets/{ticker}")

    def get_markets_by_event(self, event_ticker: str) -> list[dict]:
        markets = []
        cursor = None
        while True:
            params = {"event_ticker": event_ticker, "status": "open", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/markets", params=params)
            markets.extend(data.get("markets", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return markets

    def get_orderbook(self, ticker: str) -> dict:
        """Return orderbook for a market. Keys: 'yes_dollars', 'no_dollars'."""
        return self._get(f"/markets/{ticker}/orderbook").get("orderbook_fp") or {}

    def get_titled_tuesday_markets(self, event_date: Union[date, str, None] = None) -> list[dict]:
        """Return all open markets for a Titled Tuesday date (defaults to soonest upcoming Tuesday)."""
        date_str = _to_kalshi_date(event_date)
        markets = []
        for tmpl in _TT_EVENT_TEMPLATES:
            markets.extend(self.get_markets_by_event(tmpl.format(date=date_str)))
        return markets

    def get_titled_tuesday_asks(
        self, event_date: Union[date, str, None] = None, sleep: float = 0.05
    ) -> list[dict]:
        """Fetch all resting ask levels for a Titled Tuesday event date.

        Each entry represents one price level on one side of one market:
            Market Ticker, Market Title, Player Name,
            Ask Price ($), Quantity Available, Side ('YES' | 'NO')
        """
        markets = self.get_titled_tuesday_markets(event_date)
        all_asks = []
        for market in markets:
            ticker   = market["ticker"]
            title    = market.get("title", "")
            subtitle = market.get("yes_sub_title", "")
            try:
                ob = self.get_orderbook(ticker)
                for price_str, qty_str in ob.get("yes_dollars") or []:
                    all_asks.append({
                        "Market Ticker": ticker, "Market Title": title, "Player Name": subtitle,
                        "Ask Price ($)": 1.0 - float(price_str),
                        "Quantity Available": float(qty_str), "Side": "NO",
                    })
                for price_str, qty_str in ob.get("no_dollars") or []:
                    all_asks.append({
                        "Market Ticker": ticker, "Market Title": title, "Player Name": subtitle,
                        "Ask Price ($)": 1.0 - float(price_str),
                        "Quantity Available": float(qty_str), "Side": "YES",
                    })
            except Exception as e:
                print(f"Could not retrieve orderbook for {ticker}: {e}")
            if sleep:
                time.sleep(sleep)
        return all_asks

    # ── Portfolio (authenticated) ─────────────────────────────────────────────────

    def get_positions(self) -> pd.DataFrame:
        positions = []
        cursor = None
        while True:
            params = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = self._get("/portfolio/positions", params=params, auth=True)
            positions.extend(data.get("market_positions", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return pd.DataFrame(positions)

    def get_fills(self, ticker: str) -> list[dict]:
        data = self._get("/portfolio/fills", params={"ticker": ticker}, auth=True)
        return data.get("fills", [])

    # ── Order placement ───────────────────────────────────────────────────────────

    def place_order(
        self,
        ticker: str,
        side: str,
        count: int,
        price: int,
        dry_run: bool = True,
    ) -> dict:
        """Place a limit order. dry_run=True (default) only prints — does not submit."""
        if side not in ("yes", "no"):
            raise ValueError(f"side must be 'yes' or 'no', got {side!r}")
        if not (1 <= price <= 99):
            raise ValueError(f"price must be 1–99 cents, got {price}")
        if count < 1:
            raise ValueError(f"count must be >= 1, got {count}")

        body = {
            "action": "buy",
            "count":  count,
            "ticker": ticker,
            "type":   "limit",
            ("yes_price" if side == "yes" else "no_price"): price,
        }

        if dry_run:
            print(f"[DRY RUN] place_order: ticker={ticker!r}, side={side!r}, count={count}, price={price}¢")
            print(f"          Would POST to {self.base}/portfolio/orders")
            return {"dry_run": True, **body}

        return self._post("/portfolio/orders", body)
