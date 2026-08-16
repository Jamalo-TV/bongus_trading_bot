"""Strictly read-only Binance account snapshot collection.

This client intentionally exposes only GET-backed evidence endpoints.  It is
used by promotion-gate collection so gathering account truth cannot cancel an
order, transfer assets, or submit a repair.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Sequence
from urllib.parse import urlencode

import requests

GetCallable = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class ReadOnlyCallError(RuntimeError):
    endpoint: str
    http_status: int
    exchange_code: int | None
    exchange_message: str

    def __str__(self) -> str:
        return (
            f"read-only GET {self.endpoint} failed: HTTP {self.http_status} "
            f"code={self.exchange_code} message={self.exchange_message[:160]}"
        )


class BinanceAccountSnapshotClient:
    """Fetch the account surfaces required by the ownership reconciler."""

    def __init__(
        self,
        *,
        futures_base_url: str,
        spot_base_url: str,
        futures_api_key: str,
        futures_api_secret: str,
        spot_api_key: str,
        spot_api_secret: str,
        request_get: GetCallable = requests.get,
        timeout_seconds: float = 15.0,
        spot_trade_symbols: Sequence[str] = (),
    ) -> None:
        credentials = (
            futures_api_key,
            futures_api_secret,
            spot_api_key,
            spot_api_secret,
        )
        if any(not value.strip() for value in credentials):
            raise ValueError("complete futures and spot read-only credentials are required")
        self.futures_base_url = futures_base_url.rstrip("/")
        self.spot_base_url = spot_base_url.rstrip("/")
        self._futures_api_key = futures_api_key
        self._futures_api_secret = futures_api_secret
        self._spot_api_key = spot_api_key
        self._spot_api_secret = spot_api_secret
        self._get = request_get
        self._timeout = float(timeout_seconds)
        self._spot_trade_symbols = tuple(
            sorted({str(symbol).strip().upper() for symbol in spot_trade_symbols if str(symbol).strip()})
        )

    @staticmethod
    def _payload(response: Any, endpoint: str) -> Any:
        try:
            payload = response.json()
        except Exception as exc:
            raise ReadOnlyCallError(
                endpoint=endpoint,
                http_status=int(getattr(response, "status_code", 0)),
                exchange_code=None,
                exchange_message="non-JSON response",
            ) from exc
        status = int(getattr(response, "status_code", 0))
        code = payload.get("code") if isinstance(payload, dict) else None
        if status >= 400 or (isinstance(code, int) and code < 0):
            raise ReadOnlyCallError(
                endpoint=endpoint,
                http_status=status,
                exchange_code=code if isinstance(code, int) else None,
                exchange_message=(
                    str(payload.get("msg") or "exchange request rejected")
                    if isinstance(payload, dict)
                    else "exchange request rejected"
                ),
            )
        return payload

    def _public_get(self, base_url: str, endpoint: str) -> Any:
        response = self._get(
            f"{base_url}{endpoint}",
            timeout=self._timeout,
        )
        return self._payload(response, endpoint)

    def _server_time(self, base_url: str, endpoint: str) -> int:
        payload = self._public_get(base_url, endpoint)
        if not isinstance(payload, dict) or not isinstance(payload.get("serverTime"), int):
            raise ReadOnlyCallError(endpoint, 200, None, "serverTime missing")
        return int(payload["serverTime"])

    def _signed_get(
        self,
        *,
        base_url: str,
        endpoint: str,
        api_key: str,
        api_secret: str,
        server_time_ms: int,
        params: dict[str, str | int] | None = None,
    ) -> Any:
        query: dict[str, str | int] = dict(params or {})
        query["recvWindow"] = 60_000
        query["timestamp"] = server_time_ms
        encoded = urlencode(query)
        signature = hmac.new(
            api_secret.encode("utf-8"),
            encoded.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        response = self._get(
            f"{base_url}{endpoint}?{encoded}&signature={signature}",
            headers={"X-MBX-APIKEY": api_key},
            timeout=self._timeout,
        )
        return self._payload(response, endpoint)

    def _signed_get_fallback(
        self,
        *,
        base_url: str,
        endpoints: tuple[str, ...],
        api_key: str,
        api_secret: str,
        server_time_ms: int,
    ) -> Any:
        last_error: ReadOnlyCallError | None = None
        for endpoint in endpoints:
            try:
                return self._signed_get(
                    base_url=base_url,
                    endpoint=endpoint,
                    api_key=api_key,
                    api_secret=api_secret,
                    server_time_ms=server_time_ms,
                )
            except ReadOnlyCallError as exc:
                last_error = exc
                if exc.http_status not in {400, 404}:
                    raise
        assert last_error is not None
        raise last_error

    def collect(self) -> tuple[dict[str, Any], dict[str, str], dict[str, str]]:
        """Return raw reconciliation snapshot, asset prices, endpoint statuses."""

        captured_at = datetime.now(timezone.utc).isoformat()
        futures_time = self._server_time(
            self.futures_base_url, "/fapi/v1/time"
        )
        spot_time = self._server_time(self.spot_base_url, "/api/v3/time")
        snapshot: dict[str, Any] = {}
        statuses: dict[str, str] = {}
        errors: dict[str, str] = {}

        required = (
            (
                "futures_account",
                self.futures_base_url,
                ("/fapi/v3/account", "/fapi/v2/account"),
                self._futures_api_key,
                self._futures_api_secret,
                futures_time,
            ),
            (
                "position_risk",
                self.futures_base_url,
                ("/fapi/v3/positionRisk", "/fapi/v2/positionRisk"),
                self._futures_api_key,
                self._futures_api_secret,
                futures_time,
            ),
            (
                "futures_open_orders",
                self.futures_base_url,
                ("/fapi/v1/openOrders",),
                self._futures_api_key,
                self._futures_api_secret,
                futures_time,
            ),
            (
                "spot_account",
                self.spot_base_url,
                ("/api/v3/account",),
                self._spot_api_key,
                self._spot_api_secret,
                spot_time,
            ),
            (
                "spot_open_orders",
                self.spot_base_url,
                ("/api/v3/openOrders",),
                self._spot_api_key,
                self._spot_api_secret,
                spot_time,
            ),
        )
        for name, base_url, endpoints, key, secret, timestamp in required:
            snapshot[name] = self._signed_get_fallback(
                base_url=base_url,
                endpoints=endpoints,
                api_key=key,
                api_secret=secret,
                server_time_ms=timestamp,
            )
            statuses[name] = "available"

        try:
            snapshot["futures_position_mode"] = self._signed_get(
                base_url=self.futures_base_url,
                endpoint="/fapi/v1/positionSide/dual",
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
                server_time_ms=futures_time,
            )
            statuses["futures_position_mode"] = "available"
        except ReadOnlyCallError as exc:
            snapshot["futures_position_mode"] = None
            statuses["futures_position_mode"] = "unknown"
            errors["futures_position_mode"] = str(exc)
        except Exception as exc:
            snapshot["futures_position_mode"] = None
            statuses["futures_position_mode"] = "unknown"
            errors["futures_position_mode"] = str(exc)[:240]
        snapshot["futures_position_mode_status"] = statuses["futures_position_mode"]

        margin_specs = (
            ("margin_account", "/sapi/v1/margin/account"),
            ("margin_open_orders", "/sapi/v1/margin/openOrders"),
        )
        for name, endpoint in margin_specs:
            try:
                snapshot[name] = self._signed_get(
                    base_url=self.spot_base_url,
                    endpoint=endpoint,
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                    server_time_ms=spot_time,
                )
                statuses[name] = "available"
            except ReadOnlyCallError as exc:
                if exc.exchange_code == -3003:
                    snapshot[name] = None if name == "margin_account" else []
                    statuses[name] = "disabled"
                else:
                    snapshot[name] = None if name == "margin_account" else []
                    statuses[name] = "unknown"
                    errors[name] = str(exc)

        snapshot["margin_account_status"] = statuses["margin_account"]
        snapshot["margin_open_orders_status"] = statuses["margin_open_orders"]
        snapshot["snapshot_errors"] = errors

        trade_symbols = set(self._spot_trade_symbols)
        for key in ("spot_open_orders", "futures_open_orders"):
            for row in snapshot.get(key) or []:
                if isinstance(row, dict) and str(row.get("symbol") or "").strip():
                    trade_symbols.add(str(row["symbol"]).upper())
        for row in snapshot.get("position_risk") or []:
            if not isinstance(row, dict):
                continue
            try:
                amount = Decimal(str(row.get("positionAmt")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if amount != 0 and str(row.get("symbol") or "").strip():
                trade_symbols.add(str(row["symbol"]).upper())
        for row in snapshot.get("spot_account", {}).get("balances", []):
            if not isinstance(row, dict):
                continue
            asset = str(row.get("asset") or "").upper()
            if not asset or asset in {"USDT", "USDC", "FDUSD", "BUSD", "USDS"}:
                continue
            try:
                quantity = Decimal(str(row.get("free"))) + Decimal(str(row.get("locked")))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if quantity != 0:
                trade_symbols.add(f"{asset}USDT")

        spot_trades: list[dict[str, Any]] = []
        spot_trades_status = "available"
        for symbol in sorted(trade_symbols):
            try:
                payload = self._signed_get(
                    base_url=self.spot_base_url,
                    endpoint="/api/v3/myTrades",
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                    server_time_ms=spot_time,
                    params={"symbol": symbol, "limit": 1000},
                )
            except ReadOnlyCallError as exc:
                spot_trades_status = "unknown"
                errors[f"spot_trades:{symbol}"] = str(exc)
                continue
            except Exception as exc:
                spot_trades_status = "unknown"
                errors[f"spot_trades:{symbol}"] = str(exc)[:240]
                continue
            if not isinstance(payload, list):
                spot_trades_status = "unknown"
                errors[f"spot_trades:{symbol}"] = "trade response is not a list"
                continue
            for row in payload:
                if isinstance(row, dict):
                    spot_trades.append({"symbol": symbol, **row})
        snapshot["spot_trade_symbols"] = sorted(trade_symbols)
        snapshot["spot_trades"] = spot_trades
        snapshot["spot_trades_status"] = spot_trades_status
        snapshot["spot_trade_scope"] = "ACTIVE_ACCOUNT_UNIVERSE_LATEST_1000_PER_SYMBOL"
        statuses["spot_trades"] = spot_trades_status

        funding_rows: list[dict[str, Any]] = []
        funding_cursor = futures_time - 90 * 24 * 60 * 60 * 1000
        for _page in range(100):
            payload = self._signed_get(
                base_url=self.futures_base_url,
                endpoint="/fapi/v1/income",
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
                server_time_ms=futures_time,
                params={
                    "incomeType": "FUNDING_FEE",
                    "startTime": funding_cursor,
                    "endTime": futures_time,
                    "limit": 1000,
                },
            )
            if not isinstance(payload, list):
                raise ReadOnlyCallError(
                    "/fapi/v1/income", 200, None, "income response is not a list"
                )
            page_rows = [dict(row) for row in payload if isinstance(row, dict)]
            funding_rows.extend(page_rows)
            if len(page_rows) < 1000:
                break
            next_cursor = max(int(row.get("time") or 0) for row in page_rows) + 1
            if next_cursor <= funding_cursor:
                raise ReadOnlyCallError(
                    "/fapi/v1/income", 200, None, "income cursor did not advance"
                )
            funding_cursor = next_cursor
        snapshot["funding_income"] = funding_rows
        statuses["funding_income"] = "available"

        ticker_rows = self._public_get(self.spot_base_url, "/api/v3/ticker/price")
        prices: dict[str, str] = {}
        if isinstance(ticker_rows, list):
            for row in ticker_rows:
                if not isinstance(row, dict):
                    continue
                symbol = str(row.get("symbol") or "").upper()
                if symbol.endswith("USDT") and len(symbol) > 4:
                    prices[symbol[:-4]] = str(row.get("price") or "")
        statuses["spot_ticker_prices"] = "available"
        snapshot["captured_at"] = captured_at
        snapshot["availability_time"] = datetime.now(timezone.utc).isoformat()
        statuses["collected_at_unix_ms"] = str(int(time.time() * 1000))
        return snapshot, prices, statuses
