"""Exact, venue-separated Binance account truth normalization.

The normalizer deliberately keeps Standard Spot (including separately labelled
cross-margin borrow evidence) apart from USD-M Futures.  Decimal evidence is
stored as canonical strings; an absent or malformed field becomes ``None`` and
is listed in ``missing_fields`` instead of being interpreted as zero.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

ACCOUNT_TRUTH_SCHEMA_VERSION = 1
DEFAULT_ACCOUNT_TRUTH_MAX_AGE_SECONDS = 120
_SEQUENCE_TYPES = (str, bytes, bytearray)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _decimal_string(
    value: object,
    path: str,
    missing: list[str],
) -> str | None:
    if value is None or value == "" or isinstance(value, (bool, float)):
        missing.append(path)
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        missing.append(path)
        return None
    if not parsed.is_finite():
        missing.append(path)
        return None
    if parsed == 0:
        return "0"
    return format(parsed.normalize(), "f")


def _text(value: object, path: str, missing: list[str]) -> str | None:
    if value is None or not str(value).strip():
        missing.append(path)
        return None
    return str(value).strip()


def _integer(value: object, path: str, missing: list[str]) -> int | None:
    if value is None or isinstance(value, bool):
        missing.append(path)
        return None
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        missing.append(path)
        return None
    return parsed


def _rows(value: object, path: str, missing: list[str]) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, _SEQUENCE_TYPES):
        missing.append(path)
        return []
    result: list[Mapping[str, Any]] = []
    for index, row in enumerate(value):
        if not isinstance(row, Mapping):
            missing.append(f"{path}[{index}]")
            continue
        result.append(row)
    return result


def _position_key(row: Mapping[str, Any], path: str, missing: list[str]) -> str | None:
    symbol = _text(row.get("symbol"), f"{path}.symbol", missing)
    side = _text(row.get("positionSide", "BOTH"), f"{path}.positionSide", missing)
    if symbol is None or side is None:
        return None
    return f"{symbol.upper()}|{side.upper()}"


def _nonzero_position_rows(
    rows: list[Mapping[str, Any]],
    path: str,
    missing: list[str],
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for index, row in enumerate(rows):
        row_path = f"{path}[{index}]"
        amount = _decimal_string(row.get("positionAmt"), f"{row_path}.positionAmt", missing)
        key = _position_key(row, row_path, missing)
        if amount is None or key is None or Decimal(amount) == 0:
            continue
        if key in result:
            missing.append(f"{path}.duplicate:{key}")
            continue
        result[key] = row
    return result


def _normalize_spot(snapshot: Mapping[str, Any], missing: list[str]) -> dict[str, Any]:
    prefix = "standard_spot"
    account = snapshot.get("spot_account")
    if not isinstance(account, Mapping):
        missing.append(f"{prefix}.account")
        account = {}
    uid = _text(account.get("uid"), f"{prefix}.uid", missing)
    can_trade = account.get("canTrade")
    if not isinstance(can_trade, bool):
        missing.append(f"{prefix}.can_trade")
        can_trade = None

    balances: list[dict[str, Any]] = []
    for index, row in enumerate(_rows(account.get("balances"), f"{prefix}.balances", missing)):
        path = f"{prefix}.balances[{index}]"
        balances.append(
            {
                "asset": _text(row.get("asset"), f"{path}.asset", missing),
                "free": _decimal_string(row.get("free"), f"{path}.free", missing),
                "locked": _decimal_string(row.get("locked"), f"{path}.locked", missing),
            }
        )

    open_orders: list[dict[str, Any]] = []
    for index, row in enumerate(
        _rows(snapshot.get("spot_open_orders"), f"{prefix}.open_orders", missing)
    ):
        path = f"{prefix}.open_orders[{index}]"
        open_orders.append(
            {
                "symbol": _text(row.get("symbol"), f"{path}.symbol", missing),
                "order_id": _integer(row.get("orderId"), f"{path}.order_id", missing),
                "client_order_id": _text(
                    row.get("clientOrderId"), f"{path}.client_order_id", missing
                ),
                "side": _text(row.get("side"), f"{path}.side", missing),
                "type": _text(row.get("type"), f"{path}.type", missing),
                "status": _text(row.get("status"), f"{path}.status", missing),
                "price": _decimal_string(row.get("price"), f"{path}.price", missing),
                "orig_qty": _decimal_string(row.get("origQty"), f"{path}.orig_qty", missing),
                "executed_qty": _decimal_string(
                    row.get("executedQty"), f"{path}.executed_qty", missing
                ),
                "cumulative_quote_qty": _decimal_string(
                    row.get("cummulativeQuoteQty"),
                    f"{path}.cumulative_quote_qty",
                    missing,
                ),
            }
        )

    trades_status = str(snapshot.get("spot_trades_status") or "unknown").lower()
    if trades_status != "available":
        missing.append(f"{prefix}.trades.availability")
    trade_scope = _text(
        snapshot.get("spot_trade_scope"), f"{prefix}.trades.scope", missing
    )
    trades: list[dict[str, Any]] = []
    for index, row in enumerate(
        _rows(snapshot.get("spot_trades"), f"{prefix}.trades", missing)
    ):
        path = f"{prefix}.trades[{index}]"
        trades.append(
            {
                "symbol": _text(row.get("symbol"), f"{path}.symbol", missing),
                "trade_id": _integer(row.get("id"), f"{path}.trade_id", missing),
                "order_id": _integer(row.get("orderId"), f"{path}.order_id", missing),
                "price": _decimal_string(row.get("price"), f"{path}.price", missing),
                "qty": _decimal_string(row.get("qty"), f"{path}.qty", missing),
                "quote_qty": _decimal_string(
                    row.get("quoteQty"), f"{path}.quote_qty", missing
                ),
                "commission": _decimal_string(
                    row.get("commission"), f"{path}.commission", missing
                ),
                "commission_asset": _text(
                    row.get("commissionAsset"), f"{path}.commission_asset", missing
                ),
                "time_ms": _integer(row.get("time"), f"{path}.time_ms", missing),
                "is_buyer": row.get("isBuyer") if isinstance(row.get("isBuyer"), bool) else None,
                "is_maker": row.get("isMaker") if isinstance(row.get("isMaker"), bool) else None,
            }
        )
        if not isinstance(row.get("isBuyer"), bool):
            missing.append(f"{path}.is_buyer")
        if not isinstance(row.get("isMaker"), bool):
            missing.append(f"{path}.is_maker")

    margin_status = str(snapshot.get("margin_account_status") or "unknown").lower()
    borrow_interest: list[dict[str, Any]] = []
    if margin_status == "available":
        margin_account = snapshot.get("margin_account")
        if not isinstance(margin_account, Mapping):
            missing.append(f"{prefix}.borrow_interest.account")
            margin_account = {}
        for index, row in enumerate(
            _rows(
                margin_account.get("userAssets"),
                f"{prefix}.borrow_interest.rows",
                missing,
            )
        ):
            path = f"{prefix}.borrow_interest.rows[{index}]"
            borrow_interest.append(
                {
                    "scope": "CROSS_MARGIN",
                    "asset": _text(row.get("asset"), f"{path}.asset", missing),
                    "borrowed": _decimal_string(
                        row.get("borrowed"), f"{path}.borrowed", missing
                    ),
                    "interest": _decimal_string(
                        row.get("interest"), f"{path}.interest", missing
                    ),
                }
            )
    elif margin_status != "disabled":
        missing.append(f"{prefix}.borrow_interest.availability")

    interest_status = str(snapshot.get("margin_interest_status") or "unknown").lower()
    if interest_status not in {"available", "disabled"}:
        missing.append(f"{prefix}.interest_events.availability")
    interest_events: list[dict[str, Any]] = []
    for index, row in enumerate(
        _rows(snapshot.get("margin_interest"), f"{prefix}.interest_events", missing)
    ):
        path = f"{prefix}.interest_events[{index}]"
        interest_events.append(
            {
                "scope": "CROSS_MARGIN",
                "transaction_id": _text(row.get("txId"), f"{path}.transaction_id", missing),
                "asset": _text(row.get("asset"), f"{path}.asset", missing),
                "interest": _decimal_string(
                    row.get("interest"), f"{path}.interest", missing
                ),
                "principal": _decimal_string(
                    row.get("principal"), f"{path}.principal", missing
                ),
                "time_ms": _integer(
                    row.get("interestAccuredTime") or row.get("interestAccruedTime"),
                    f"{path}.time_ms",
                    missing,
                ),
            }
        )

    raw_trade_symbols = snapshot.get("spot_trade_symbols")
    trade_symbols = (
        sorted({str(item).upper() for item in raw_trade_symbols if str(item)})
        if isinstance(raw_trade_symbols, Sequence)
        and not isinstance(raw_trade_symbols, _SEQUENCE_TYPES)
        else []
    )
    if not isinstance(raw_trade_symbols, Sequence) or isinstance(
        raw_trade_symbols, _SEQUENCE_TYPES
    ):
        missing.append(f"{prefix}.trade_symbols")

    return {
        "venue": "BINANCE_STANDARD_SPOT",
        "uid": uid,
        "can_trade": can_trade,
        "balances": balances,
        "borrow_interest_status": margin_status.upper(),
        "borrow_interest": borrow_interest,
        "interest_events_status": interest_status.upper(),
        "interest_events": interest_events,
        "trades_status": trades_status.upper(),
        "trade_scope": trade_scope,
        "trade_symbols": trade_symbols,
        "trades": trades,
        "open_orders": open_orders,
    }


def _normalize_futures(snapshot: Mapping[str, Any], missing: list[str]) -> dict[str, Any]:
    prefix = "usd_m_futures"
    account = snapshot.get("futures_account")
    if not isinstance(account, Mapping):
        missing.append(f"{prefix}.account")
        account = {}

    wallet_balance = _decimal_string(
        account.get("totalWalletBalance"), f"{prefix}.wallet_balance", missing
    )
    available_balance = _decimal_string(
        account.get("availableBalance"), f"{prefix}.available_balance", missing
    )
    total_margin_balance = _decimal_string(
        account.get("totalMarginBalance"), f"{prefix}.total_margin_balance", missing
    )
    total_maint_margin = _decimal_string(
        account.get("totalMaintMargin"), f"{prefix}.total_maint_margin", missing
    )
    margin_ratio: str | None = None
    if total_margin_balance is not None and total_maint_margin is not None:
        denominator = Decimal(total_margin_balance)
        if denominator > 0:
            margin_ratio = _decimal_string(
                Decimal(total_maint_margin) / denominator,
                f"{prefix}.margin_ratio",
                missing,
            )
        else:
            missing.append(f"{prefix}.margin_ratio")

    assets: list[dict[str, Any]] = []
    for index, row in enumerate(_rows(account.get("assets"), f"{prefix}.assets", missing)):
        path = f"{prefix}.assets[{index}]"
        assets.append(
            {
                "asset": _text(row.get("asset"), f"{path}.asset", missing),
                "wallet_balance": _decimal_string(
                    row.get("walletBalance"), f"{path}.wallet_balance", missing
                ),
                "available_balance": _decimal_string(
                    row.get("availableBalance"), f"{path}.available_balance", missing
                ),
                "margin_balance": _decimal_string(
                    row.get("marginBalance"), f"{path}.margin_balance", missing
                ),
                "unrealized_profit": _decimal_string(
                    row.get("unrealizedProfit"), f"{path}.unrealized_profit", missing
                ),
            }
        )

    account_positions = _nonzero_position_rows(
        _rows(account.get("positions"), f"{prefix}.account_positions", missing),
        f"{prefix}.account_positions",
        missing,
    )
    risk_positions = _nonzero_position_rows(
        _rows(snapshot.get("position_risk"), f"{prefix}.position_risk", missing),
        f"{prefix}.position_risk",
        missing,
    )
    positions: list[dict[str, Any]] = []
    for key in sorted(set(account_positions) | set(risk_positions)):
        account_row = account_positions.get(key)
        risk_row = risk_positions.get(key)
        if account_row is None:
            missing.append(f"{prefix}.positions[{key}].account_projection")
            account_row = {}
        if risk_row is None:
            missing.append(f"{prefix}.positions[{key}].risk_projection")
            risk_row = {}
        path = f"{prefix}.positions[{key}]"
        symbol, position_side = key.split("|", 1)
        positions.append(
            {
                "symbol": symbol,
                "position_side": position_side,
                "position_amount": _decimal_string(
                    risk_row.get("positionAmt", account_row.get("positionAmt")),
                    f"{path}.position_amount",
                    missing,
                ),
                "entry_price": _decimal_string(
                    risk_row.get("entryPrice", account_row.get("entryPrice")),
                    f"{path}.entry_price",
                    missing,
                ),
                "mark_price": _decimal_string(
                    risk_row.get("markPrice"), f"{path}.mark_price", missing
                ),
                "unrealized_profit": _decimal_string(
                    risk_row.get("unRealizedProfit", account_row.get("unrealizedProfit")),
                    f"{path}.unrealized_profit",
                    missing,
                ),
                "leverage": _decimal_string(
                    risk_row.get("leverage", account_row.get("leverage")),
                    f"{path}.leverage",
                    missing,
                ),
                "maintenance_margin": _decimal_string(
                    account_row.get("maintMargin"),
                    f"{path}.maintenance_margin",
                    missing,
                ),
                "margin_type": _text(
                    risk_row.get("marginType"), f"{path}.margin_type", missing
                ),
                "liquidation_price": _decimal_string(
                    risk_row.get("liquidationPrice"),
                    f"{path}.liquidation_price",
                    missing,
                ),
            }
        )

    position_mode_payload = snapshot.get("futures_position_mode")
    position_mode: str | None = None
    if str(snapshot.get("futures_position_mode_status") or "").lower() != "available":
        missing.append(f"{prefix}.position_mode.availability")
    if isinstance(position_mode_payload, Mapping) and isinstance(
        position_mode_payload.get("dualSidePosition"), bool
    ):
        position_mode = (
            "HEDGE" if position_mode_payload["dualSidePosition"] else "ONE_WAY"
        )
    else:
        missing.append(f"{prefix}.position_mode")

    open_orders: list[dict[str, Any]] = []
    for index, row in enumerate(
        _rows(snapshot.get("futures_open_orders"), f"{prefix}.open_orders", missing)
    ):
        path = f"{prefix}.open_orders[{index}]"
        open_orders.append(
            {
                "symbol": _text(row.get("symbol"), f"{path}.symbol", missing),
                "order_id": _integer(row.get("orderId"), f"{path}.order_id", missing),
                "client_order_id": _text(
                    row.get("clientOrderId"), f"{path}.client_order_id", missing
                ),
                "position_side": _text(
                    row.get("positionSide", "BOTH"), f"{path}.position_side", missing
                ),
                "side": _text(row.get("side"), f"{path}.side", missing),
                "type": _text(row.get("type"), f"{path}.type", missing),
                "status": _text(row.get("status"), f"{path}.status", missing),
                "price": _decimal_string(row.get("price"), f"{path}.price", missing),
                "orig_qty": _decimal_string(row.get("origQty"), f"{path}.orig_qty", missing),
                "executed_qty": _decimal_string(
                    row.get("executedQty"), f"{path}.executed_qty", missing
                ),
            }
        )

    return {
        "venue": "BINANCE_USD_M_FUTURES",
        "wallet_balance": wallet_balance,
        "available_balance": available_balance,
        "total_margin_balance": total_margin_balance,
        "total_maintenance_margin": total_maint_margin,
        "margin_ratio": margin_ratio,
        "position_mode": position_mode,
        "assets": assets,
        "positions": positions,
        "open_orders": open_orders,
    }


@dataclass(frozen=True, slots=True)
class NormalizedAccountTruth:
    snapshot_id: str
    account_id: str
    environment: str
    captured_at: str | None
    availability_time: str | None
    expires_at: str | None
    status: str
    standard_spot_status: str
    usd_m_futures_status: str
    missing_fields: tuple[str, ...]
    standard_spot: Mapping[str, Any]
    usd_m_futures: Mapping[str, Any]
    raw_snapshot: Mapping[str, Any]
    content_hash: str
    schema_version: int = ACCOUNT_TRUTH_SCHEMA_VERSION

    @property
    def ready(self) -> bool:
        return self.status == "COMPLETE"

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "snapshot_id": self.snapshot_id,
            "account_id": self.account_id,
            "environment": self.environment,
            "captured_at": self.captured_at,
            "availability_time": self.availability_time,
            "expires_at": self.expires_at,
            "status": self.status,
            "standard_spot_status": self.standard_spot_status,
            "usd_m_futures_status": self.usd_m_futures_status,
            "missing_fields": list(self.missing_fields),
            "standard_spot": dict(self.standard_spot),
            "usd_m_futures": dict(self.usd_m_futures),
            "content_hash": self.content_hash,
        }
        if include_raw:
            payload["raw_snapshot"] = dict(self.raw_snapshot)
        return payload


def normalize_binance_account_truth(
    snapshot: Mapping[str, Any],
    *,
    account_id: str,
    environment: str,
    now: datetime | None = None,
    max_age_seconds: int = DEFAULT_ACCOUNT_TRUTH_MAX_AGE_SECONDS,
) -> NormalizedAccountTruth:
    """Normalize one signed snapshot without inventing absent account values."""

    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    missing: list[str] = []
    availability = _parse_time(snapshot.get("availability_time"))
    if availability is None:
        missing.append("availability_time")
    captured = _parse_time(snapshot.get("captured_at"))
    if snapshot.get("captured_at") is not None and captured is None:
        missing.append("captured_at")

    spot_missing: list[str] = []
    futures_missing: list[str] = []
    standard_spot = _normalize_spot(snapshot, spot_missing)
    usd_m_futures = _normalize_futures(snapshot, futures_missing)
    missing.extend(spot_missing)
    missing.extend(futures_missing)

    max_age = max(1, int(max_age_seconds))
    stale = False
    expires_at: str | None = None
    if availability is not None:
        expires = availability + timedelta(seconds=max_age)
        expires_at = expires.isoformat()
        stale = observed_now > expires or availability > observed_now + timedelta(seconds=5)

    standard_spot_status = (
        "UNKNOWN"
        if spot_missing or availability is None
        else ("STALE" if stale else "COMPLETE")
    )
    usd_m_futures_status = (
        "UNKNOWN"
        if futures_missing or availability is None
        else ("STALE" if stale else "COMPLETE")
    )
    if missing:
        status = "UNKNOWN"
    elif stale:
        status = "STALE"
    else:
        status = "COMPLETE"

    normalized_missing = tuple(sorted(set(missing)))
    raw_snapshot = dict(snapshot)
    hash_payload = {
        "schema_version": ACCOUNT_TRUTH_SCHEMA_VERSION,
        "account_id": str(account_id),
        "environment": str(environment),
        "captured_at": captured.isoformat() if captured else None,
        "availability_time": availability.isoformat() if availability else None,
        "expires_at": expires_at,
        "status": status,
        "standard_spot_status": standard_spot_status,
        "usd_m_futures_status": usd_m_futures_status,
        "missing_fields": normalized_missing,
        "standard_spot": standard_spot,
        "usd_m_futures": usd_m_futures,
        "raw_snapshot": raw_snapshot,
    }
    content_hash = hashlib.sha256(_canonical_json(hash_payload).encode("utf-8")).hexdigest()
    identity = hashlib.sha256(
        f"{account_id}|{environment}|{availability}|{content_hash}".encode("utf-8")
    ).hexdigest()
    return NormalizedAccountTruth(
        snapshot_id=f"account_truth_{identity[:32]}",
        account_id=str(account_id),
        environment=str(environment),
        captured_at=captured.isoformat() if captured else None,
        availability_time=availability.isoformat() if availability else None,
        expires_at=expires_at,
        status=status,
        standard_spot_status=standard_spot_status,
        usd_m_futures_status=usd_m_futures_status,
        missing_fields=normalized_missing,
        standard_spot=standard_spot,
        usd_m_futures=usd_m_futures,
        raw_snapshot=raw_snapshot,
        content_hash=content_hash,
    )
