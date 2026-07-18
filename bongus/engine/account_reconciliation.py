"""Fail-closed account ownership and exposure reconciliation.

The reconciler is deliberately exchange-adapter agnostic: it consumes the raw
Binance account snapshot already fetched by the live trader and produces a
stable, serialisable report.  It never mutates exchange or local state.

An order's client ID is the only authority for cancellation ownership.  A
matching symbol, side, or quantity is *not* sufficient because those fields can
also belong to an operator or another strategy sharing the account.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence


BOT_CLIENT_ORDER_PREFIX = "bngs_"
BOT_CLIENT_ORDER_ID_MAX_LENGTH = 36
_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,36}$")
_DEFAULT_QUOTE_ASSETS = frozenset({"USDT", "USDC", "FDUSD", "BUSD", "USDS"})
_DEFAULT_TREASURY_ASSETS = frozenset({*_DEFAULT_QUOTE_ASSETS, "BNB"})
_QUOTE_SUFFIXES = (
    "FDUSD",
    "USDT",
    "USDC",
    "BUSD",
    "USDS",
    "BTC",
    "ETH",
    "BNB",
    "EUR",
    "TRY",
)


class OrderOwnership(str, Enum):
    BOT = "bot_owned"
    BOT_ORPHAN = "bot_owned_orphan"
    EXTERNAL = "external"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ReconciliationIssue:
    code: str
    scope: str
    message: str
    venue: str = ""
    symbol: str = ""
    asset: str = ""
    client_order_id: str = ""
    blocking: bool = True

    @property
    def incident_id(self) -> str:
        identity = "|".join(
            (
                self.code,
                self.scope,
                self.venue,
                self.symbol,
                self.asset,
                self.client_order_id,
            )
        )
        return f"recon_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]}"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["incident_id"] = self.incident_id
        return payload


@dataclass(frozen=True, slots=True)
class ClassifiedOrder:
    venue: str
    symbol: str
    client_order_id: str
    exchange_order_id: str
    status: str
    ownership: OrderOwnership
    linked_intent_ids: tuple[str, ...] = ()

    @property
    def cancellable_by_bot(self) -> bool:
        return self.ownership in {OrderOwnership.BOT, OrderOwnership.BOT_ORPHAN}

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["ownership"] = self.ownership.value
        payload["cancellable_by_bot"] = self.cancellable_by_bot
        return payload


@dataclass(frozen=True, slots=True)
class ClassifiedPosition:
    symbol: str
    direction: str
    exchange_quantity: str
    local_quantity: str
    classification: str
    hedge_asset: str
    hedge_quantity: str
    liability_quantity: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ClassifiedLiability:
    asset: str
    quantity: str
    allocated_quantity: str
    residual_quantity: str
    classification: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class AccountReconciliationReport:
    generated_at: str
    ready: bool
    snapshot_complete: bool
    orders: tuple[ClassifiedOrder, ...]
    positions: tuple[ClassifiedPosition, ...]
    liabilities: tuple[ClassifiedLiability, ...]
    issues: tuple[ReconciliationIssue, ...]
    exchange_only_symbols: tuple[str, ...] = ()
    local_only_symbols: tuple[str, ...] = ()
    mismatched_symbols: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def blocking_issues(self) -> tuple[ReconciliationIssue, ...]:
        return tuple(issue for issue in self.issues if issue.blocking)

    @property
    def bot_owned_orders(self) -> tuple[ClassifiedOrder, ...]:
        return tuple(order for order in self.orders if order.cancellable_by_bot)

    @property
    def unrelated_orders(self) -> tuple[ClassifiedOrder, ...]:
        return tuple(order for order in self.orders if not order.cancellable_by_bot)

    @property
    def fingerprint(self) -> str:
        payload = self.to_dict(include_generated_at=False, include_fingerprint=False)
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def to_dict(
        self,
        *,
        include_generated_at: bool = True,
        include_fingerprint: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ready": self.ready,
            "snapshot_complete": self.snapshot_complete,
            "orders": [order.to_dict() for order in self.orders],
            "positions": [position.to_dict() for position in self.positions],
            "liabilities": [liability.to_dict() for liability in self.liabilities],
            "issues": [issue.to_dict() for issue in self.issues],
            "blocking_issue_count": len(self.blocking_issues),
            "exchange_only_symbols": list(self.exchange_only_symbols),
            "local_only_symbols": list(self.local_only_symbols),
            "mismatched_symbols": list(self.mismatched_symbols),
            "bot_owned_open_order_count": len(self.bot_owned_orders),
            "unrelated_open_order_count": len(self.unrelated_orders),
            "metadata": dict(self.metadata),
        }
        if include_generated_at:
            payload["generated_at"] = self.generated_at
        if include_fingerprint:
            payload["fingerprint"] = self.fingerprint
        return payload

    def risk_snapshot(self, *, prefix: str = "account_reconciliation") -> dict[str, Any]:
        report = self.to_dict()
        return {
            f"{prefix}_status": "ready" if self.ready else "blocked",
            f"{prefix}_ready": self.ready,
            f"{prefix}_snapshot_complete": self.snapshot_complete,
            f"{prefix}_time": self.generated_at,
            f"{prefix}_fingerprint": self.fingerprint,
            f"{prefix}_blocking_issue_count": len(self.blocking_issues),
            f"{prefix}_issues": report["issues"],
            f"{prefix}_orders": report["orders"],
            f"{prefix}_positions": report["positions"],
            f"{prefix}_liabilities": report["liabilities"],
            f"{prefix}_exchange_only_symbols": list(self.exchange_only_symbols),
            f"{prefix}_local_only_symbols": list(self.local_only_symbols),
            f"{prefix}_mismatched_symbols": list(self.mismatched_symbols),
            f"{prefix}_bot_owned_open_order_count": len(self.bot_owned_orders),
            f"{prefix}_unrelated_open_order_count": len(self.unrelated_orders),
        }


def is_bot_client_order_id(
    client_order_id: object,
    *,
    prefix: str = BOT_CLIENT_ORDER_PREFIX,
) -> bool:
    value = str(client_order_id or "").strip()
    return bool(value and value.startswith(prefix) and _CLIENT_ORDER_ID_RE.fullmatch(value))


def make_bot_client_order_id(
    *,
    leg: str,
    intent_id: str,
    nonce: str,
    prefix: str = BOT_CLIENT_ORDER_PREFIX,
) -> str:
    """Create a valid, deterministic namespace ID without leaking full intent IDs."""

    normalized_leg = re.sub(r"[^a-z0-9]", "", leg.lower())[:4] or "leg"
    digest = hashlib.sha256(f"{intent_id}|{nonce}|{normalized_leg}".encode("utf-8")).hexdigest()
    value = f"{prefix}{normalized_leg}_{digest[:20]}"
    if len(value) > BOT_CLIENT_ORDER_ID_MAX_LENGTH or not _CLIENT_ORDER_ID_RE.fullmatch(value):
        raise ValueError("bot client-order namespace cannot produce a valid exchange ID")
    return value


def order_client_id(order: Mapping[str, Any]) -> str:
    for key in ("clientOrderId", "client_order_id", "origClientOrderId"):
        value = str(order.get(key) or "").strip()
        if value:
            return value
    return ""


def bot_owned_orders(
    orders: Iterable[Mapping[str, Any]],
    *,
    prefix: str = BOT_CLIENT_ORDER_PREFIX,
) -> list[Mapping[str, Any]]:
    return [order for order in orders if is_bot_client_order_id(order_client_id(order), prefix=prefix)]


def unrelated_orders(
    orders: Iterable[Mapping[str, Any]],
    *,
    prefix: str = BOT_CLIENT_ORDER_PREFIX,
) -> list[Mapping[str, Any]]:
    return [order for order in orders if not is_bot_client_order_id(order_client_id(order), prefix=prefix)]


def _decimal(value: Any) -> Decimal:
    if value in (None, ""):
        return Decimal("0")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return result if result.is_finite() else Decimal("0")


def _base_asset(symbol: str) -> str:
    value = symbol.upper()
    for suffix in _QUOTE_SUFFIXES:
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[: -len(suffix)]
    return value


def _direction(position_amount: Decimal, position_side: object) -> str:
    side = str(position_side or "BOTH").upper()
    if side == "SHORT":
        return "long"
    if side == "LONG":
        return "short"
    return "long" if position_amount < 0 else "short"


def _position_map(rows: object, *, tolerance: Decimal) -> tuple[dict[str, dict[str, Any]], list[str]]:
    result: dict[str, dict[str, Any]] = {}
    duplicates: list[str] = []
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray)):
        return result, duplicates
    for raw in rows:
        if not isinstance(raw, Mapping):
            continue
        symbol = str(raw.get("symbol") or "").upper()
        amount = _decimal(raw.get("positionAmt"))
        if not symbol or abs(amount) <= tolerance:
            continue
        if symbol in result:
            duplicates.append(symbol)
            continue
        result[symbol] = dict(raw)
    return result, duplicates


def _local_position_map(rows: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    return {
        str(row.get("symbol") or "").upper(): row
        for row in rows
        if str(row.get("symbol") or "").strip()
        and str(row.get("status") or "OPEN").upper() != "CLOSED"
    }


def _spot_balance_map(account: object, *, tolerance: Decimal) -> dict[str, Decimal]:
    if not isinstance(account, Mapping):
        return {}
    balances: dict[str, Decimal] = {}
    raw_balances = account.get("balances")
    if not isinstance(raw_balances, Sequence):
        return balances
    for raw in raw_balances:
        if not isinstance(raw, Mapping):
            continue
        asset = str(raw.get("asset") or "").upper()
        quantity = _decimal(raw.get("free")) + _decimal(raw.get("locked"))
        if asset and quantity > tolerance:
            balances[asset] = balances.get(asset, Decimal("0")) + quantity
    return balances


def _margin_assets(account: object) -> dict[str, Mapping[str, Any]]:
    if not isinstance(account, Mapping):
        return {}
    raw_assets = account.get("userAssets")
    if not isinstance(raw_assets, Sequence):
        return {}
    return {
        str(raw.get("asset") or "").upper(): raw
        for raw in raw_assets
        if isinstance(raw, Mapping) and str(raw.get("asset") or "").strip()
    }


def _asset_value_usd(
    asset: str,
    quantity: Decimal,
    prices: Mapping[str, Decimal],
    quote_assets: frozenset[str],
) -> Decimal | None:
    if asset in quote_assets:
        return abs(quantity)
    price = prices.get(asset)
    if price is None or price <= 0:
        return None
    return abs(quantity * price)


def _classify_orders(
    *,
    snapshot: Mapping[str, Any],
    local_positions: Mapping[str, Mapping[str, Any]],
    pending_intents: Iterable[Mapping[str, Any]],
    prefix: str,
) -> tuple[list[ClassifiedOrder], list[ReconciliationIssue]]:
    pending_by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    pending_by_client_id: dict[str, list[Mapping[str, Any]]] = {}
    for intent in pending_intents:
        symbol = str(intent.get("symbol") or "").upper()
        if symbol:
            pending_by_symbol.setdefault(symbol, []).append(intent)
        client_id = str(intent.get("client_order_id") or "").strip()
        if client_id:
            pending_by_client_id.setdefault(client_id, []).append(intent)

    classified: list[ClassifiedOrder] = []
    issues: list[ReconciliationIssue] = []
    for venue, key in (
        ("usdt_futures", "futures_open_orders"),
        ("spot", "spot_open_orders"),
        ("cross_margin", "margin_open_orders"),
    ):
        raw_orders = snapshot.get(key)
        if not isinstance(raw_orders, Sequence) or isinstance(raw_orders, (str, bytes, bytearray)):
            continue
        for raw in raw_orders:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or "").upper()
            client_id = order_client_id(raw)
            exchange_order_id = str(raw.get("orderId") or raw.get("order_id") or "")
            status = str(raw.get("status") or "UNKNOWN").upper()
            linked = pending_by_client_id.get(client_id, []) or pending_by_symbol.get(symbol, [])
            linked_ids = tuple(sorted(str(row.get("intent_id") or "") for row in linked if row.get("intent_id")))
            if is_bot_client_order_id(client_id, prefix=prefix):
                ownership = (
                    OrderOwnership.BOT
                    if linked or symbol in local_positions
                    else OrderOwnership.BOT_ORPHAN
                )
                if ownership is OrderOwnership.BOT_ORPHAN:
                    issues.append(
                        ReconciliationIssue(
                            code="orphan_bot_order",
                            scope="order",
                            venue=venue,
                            symbol=symbol,
                            client_order_id=client_id,
                            message="Bot-namespaced open order has no durable intent or managed position.",
                        )
                    )
            elif client_id:
                ownership = OrderOwnership.EXTERNAL
                issues.append(
                    ReconciliationIssue(
                        code="unrelated_open_order",
                        scope="order",
                        venue=venue,
                        symbol=symbol,
                        client_order_id=client_id,
                        message="Open order is outside the bot namespace and must not be cancelled.",
                    )
                )
            else:
                ownership = OrderOwnership.UNKNOWN
                issues.append(
                    ReconciliationIssue(
                        code="order_ownership_unknown",
                        scope="order",
                        venue=venue,
                        symbol=symbol,
                        message="Open order has no usable client-order identity.",
                    )
                )
            classified.append(
                ClassifiedOrder(
                    venue=venue,
                    symbol=symbol,
                    client_order_id=client_id,
                    exchange_order_id=exchange_order_id,
                    status=status,
                    ownership=ownership,
                    linked_intent_ids=linked_ids,
                )
            )
    return classified, issues


def reconcile_account_snapshot(
    snapshot: Mapping[str, Any],
    *,
    local_positions: Iterable[Mapping[str, Any]],
    pending_intents: Iterable[Mapping[str, Any]] = (),
    asset_prices_usd: Mapping[str, Any] | None = None,
    bot_prefix: str = BOT_CLIENT_ORDER_PREFIX,
    quantity_tolerance: Decimal | str = Decimal("1e-9"),
    cash_tolerance_usd: Decimal | str = Decimal("0.01"),
    hedge_shortfall_tolerance: Decimal | str = Decimal("0.0025"),
    quote_assets: frozenset[str] = _DEFAULT_QUOTE_ASSETS,
    treasury_assets: frozenset[str] = _DEFAULT_TREASURY_ASSETS,
    expected_account_uid: str | None = None,
    require_account_uid: bool = False,
    generated_at: str | None = None,
) -> AccountReconciliationReport:
    """Classify every visible order, perp position, spot hedge and liability.

    ``ready`` is false whenever an endpoint is incomplete or any amount/order is
    unexplained.  Exchange-only positions remain visible in the returned report
    so callers can adopt them into a manual-review projection without erasing the
    incident that caused readiness to fail.
    """

    qty_tolerance = _decimal(quantity_tolerance)
    cash_tolerance = _decimal(cash_tolerance_usd)
    hedge_tolerance = _decimal(hedge_shortfall_tolerance)
    prices = {
        str(asset).upper(): _decimal(price)
        for asset, price in (asset_prices_usd or {}).items()
        if _decimal(price) > 0
    }
    local_map = _local_position_map(local_positions)
    issues: list[ReconciliationIssue] = []

    expected_uid = str(expected_account_uid or "").strip()
    spot_account = snapshot.get("spot_account")
    observed_uid = (
        str(spot_account.get("uid") or "").strip()
        if isinstance(spot_account, Mapping)
        else ""
    )
    if require_account_uid and not expected_uid:
        issues.append(
            ReconciliationIssue(
                code="dedicated_account_identity_unconfigured",
                scope="account",
                venue="binance",
                message="BONGUS_EXPECTED_ACCOUNT_UID must identify the dedicated bot account.",
            )
        )
    elif expected_uid and observed_uid != expected_uid:
        issues.append(
            ReconciliationIssue(
                code="dedicated_account_identity_mismatch",
                scope="account",
                venue="binance",
                message="Observed account UID does not match the configured dedicated bot account.",
            )
        )

    required_snapshot_fields = (
        "futures_account",
        "position_risk",
        "futures_open_orders",
        "spot_account",
        "spot_open_orders",
    )
    for key in required_snapshot_fields:
        if key not in snapshot or snapshot.get(key) is None:
            issues.append(
                ReconciliationIssue(
                    code="account_endpoint_incomplete",
                    scope="account",
                    venue="binance",
                    message=f"Required account snapshot field {key!r} is unavailable.",
                )
            )
    futures_account = snapshot.get("futures_account")
    if isinstance(futures_account, Mapping) and "positions" not in futures_account:
        issues.append(
            ReconciliationIssue(
                code="account_endpoint_incomplete",
                scope="account",
                venue="usdt_futures",
                message="Futures account response omitted its positions projection.",
            )
        )

    margin_status = str(snapshot.get("margin_account_status") or "unknown").lower()
    if margin_status not in {"available", "disabled"}:
        issues.append(
            ReconciliationIssue(
                code="margin_liability_endpoint_unverified",
                scope="account",
                venue="cross_margin",
                message="Cross-margin liabilities could not be proven absent or enumerated.",
            )
        )
    margin_orders_status = str(snapshot.get("margin_open_orders_status") or "unknown").lower()
    if margin_orders_status not in {"available", "disabled"}:
        issues.append(
            ReconciliationIssue(
                code="margin_order_endpoint_unverified",
                scope="account",
                venue="cross_margin",
                message="Cross-margin open orders could not be proven absent or enumerated.",
            )
        )
    for endpoint, detail in sorted((snapshot.get("snapshot_errors") or {}).items()):
        if endpoint == "funding_income":
            continue
        issues.append(
            ReconciliationIssue(
                code="account_snapshot_error",
                scope="account",
                venue=str(endpoint),
                message=f"Account snapshot error: {str(detail)[:240]}",
            )
        )

    risk_map, risk_duplicates = _position_map(snapshot.get("position_risk"), tolerance=qty_tolerance)
    account_positions = (
        snapshot.get("futures_account", {}).get("positions")
        if isinstance(snapshot.get("futures_account"), Mapping)
        else None
    )
    account_map, account_duplicates = _position_map(account_positions, tolerance=qty_tolerance)
    for symbol in sorted(set(risk_duplicates + account_duplicates)):
        issues.append(
            ReconciliationIssue(
                code="duplicate_or_hedge_mode_position",
                scope="position",
                venue="usdt_futures",
                symbol=symbol,
                message="Multiple open futures rows for one symbol are unsupported and ambiguous.",
            )
        )

    if isinstance(snapshot.get("futures_account"), Mapping) and "positions" in snapshot.get("futures_account", {}):
        for symbol in sorted(set(risk_map) | set(account_map)):
            risk_row = risk_map.get(symbol)
            account_row = account_map.get(symbol)
            if risk_row is None or account_row is None:
                issues.append(
                    ReconciliationIssue(
                        code="futures_position_endpoint_mismatch",
                        scope="position",
                        venue="usdt_futures",
                        symbol=symbol,
                        message="Futures account and position-risk endpoints disagree on open exposure.",
                    )
                )
                continue
            risk_amount = _decimal(risk_row.get("positionAmt"))
            account_amount = _decimal(account_row.get("positionAmt"))
            if abs(risk_amount - account_amount) > qty_tolerance:
                issues.append(
                    ReconciliationIssue(
                        code="futures_position_endpoint_mismatch",
                        scope="position",
                        venue="usdt_futures",
                        symbol=symbol,
                        message="Futures account and position-risk quantities disagree.",
                    )
                )

    exchange_map = dict(account_map)
    exchange_map.update(risk_map)
    spot_balances = _spot_balance_map(snapshot.get("spot_account"), tolerance=qty_tolerance)
    required_spot: dict[str, Decimal] = {}
    required_liability: dict[str, Decimal] = {}
    classified_positions: list[ClassifiedPosition] = []
    exchange_only: list[str] = []
    mismatched: list[str] = []

    for symbol, row in sorted(exchange_map.items()):
        amount = _decimal(row.get("positionAmt"))
        exchange_qty = abs(amount)
        direction = _direction(amount, row.get("positionSide"))
        asset = _base_asset(symbol)
        local = local_map.get(symbol)
        local_qty = _decimal(local.get("qty")) if local else Decimal("0")
        local_direction = str(local.get("direction") or "long").lower() if local else ""
        if local is None:
            classification = "exchange_only"
            exchange_only.append(symbol)
            issues.append(
                ReconciliationIssue(
                    code="exchange_only_position",
                    scope="position",
                    venue="usdt_futures",
                    symbol=symbol,
                    message="Exchange exposure has no durable local ownership lineage; adopt for manual review only.",
                )
            )
        elif local_direction != direction or abs(local_qty - exchange_qty) > qty_tolerance:
            classification = "mismatched"
            mismatched.append(symbol)
            issues.append(
                ReconciliationIssue(
                    code="managed_position_mismatch",
                    scope="position",
                    venue="usdt_futures",
                    symbol=symbol,
                    message="Exchange direction or quantity differs from the local managed projection.",
                )
            )
        elif str(local.get("recovery_state") or "").strip().lower() == "manual_review":
            classification = "manual_review"
            issues.append(
                ReconciliationIssue(
                    code="managed_position_manual_review",
                    scope="position",
                    venue="usdt_futures",
                    symbol=symbol,
                    message="Managed exposure remains in manual review and cannot establish readiness.",
                )
            )
        else:
            classification = "matched"

        if direction == "long":
            required_spot[asset] = required_spot.get(asset, Decimal("0")) + exchange_qty
        else:
            required_liability[asset] = required_liability.get(asset, Decimal("0")) + exchange_qty
        classified_positions.append(
            ClassifiedPosition(
                symbol=symbol,
                direction=direction,
                exchange_quantity=str(exchange_qty),
                local_quantity=str(local_qty),
                classification=classification,
                hedge_asset=asset,
                hedge_quantity=str(spot_balances.get(asset, Decimal("0"))),
                liability_quantity="0",
            )
        )

    local_only = sorted(set(local_map) - set(exchange_map))
    for symbol in local_only:
        local = local_map[symbol]
        issues.append(
            ReconciliationIssue(
                code="local_only_position",
                scope="position",
                venue="local_state",
                symbol=symbol,
                message="Local managed position is absent from both futures account endpoints.",
            )
        )
        classified_positions.append(
            ClassifiedPosition(
                symbol=symbol,
                direction=str(local.get("direction") or "long").lower(),
                exchange_quantity="0",
                local_quantity=str(_decimal(local.get("qty"))),
                classification="local_only",
                hedge_asset=_base_asset(symbol),
                hedge_quantity=str(spot_balances.get(_base_asset(symbol), Decimal("0"))),
                liability_quantity="0",
            )
        )

    minimum_hedge_factor = Decimal("1") - hedge_tolerance
    for asset, required in sorted(required_spot.items()):
        actual = spot_balances.get(asset, Decimal("0"))
        if actual + qty_tolerance < required * minimum_hedge_factor:
            issues.append(
                ReconciliationIssue(
                    code="spot_hedge_shortfall",
                    scope="position",
                    venue="spot",
                    asset=asset,
                    message=f"Spot inventory {actual} does not cover required hedge {required}.",
                )
            )

    for asset, actual in sorted(spot_balances.items()):
        if asset in treasury_assets:
            continue
        residual = max(Decimal("0"), actual - required_spot.get(asset, Decimal("0")))
        if residual <= qty_tolerance:
            continue
        usd_value = _asset_value_usd(asset, residual, prices, quote_assets)
        if usd_value is None:
            issues.append(
                ReconciliationIssue(
                    code="unvalued_spot_inventory",
                    scope="balance",
                    venue="spot",
                    asset=asset,
                    message=f"Residual spot inventory {residual} cannot be valued or assigned.",
                )
            )
        elif usd_value > cash_tolerance:
            issues.append(
                ReconciliationIssue(
                    code="unassigned_spot_inventory",
                    scope="balance",
                    venue="spot",
                    asset=asset,
                    message=f"Residual spot inventory {residual} (${usd_value}) is outside managed hedges.",
                )
            )

    margin_assets = _margin_assets(snapshot.get("margin_account"))
    classified_liabilities: list[ClassifiedLiability] = []
    for asset in sorted(set(margin_assets) | set(required_liability)):
        row = margin_assets.get(asset, {})
        liability = _decimal(row.get("borrowed")) + _decimal(row.get("interest"))
        allocated = min(liability, required_liability.get(asset, Decimal("0")))
        residual = max(Decimal("0"), liability - required_liability.get(asset, Decimal("0")))
        required = required_liability.get(asset, Decimal("0"))
        classification = "matched"
        if required > 0 and liability + qty_tolerance < required * minimum_hedge_factor:
            classification = "shortfall"
            issues.append(
                ReconciliationIssue(
                    code="margin_liability_shortfall",
                    scope="liability",
                    venue="cross_margin",
                    asset=asset,
                    message=f"Inverse hedge requires liability {required}, but exchange reports {liability}.",
                )
            )
        if residual > qty_tolerance:
            residual_value = _asset_value_usd(asset, residual, prices, quote_assets)
            if residual_value is None or residual_value > cash_tolerance:
                classification = "unassigned"
                issues.append(
                    ReconciliationIssue(
                        code="unassigned_margin_liability",
                        scope="liability",
                        venue="cross_margin",
                        asset=asset,
                        message=f"Margin liability {liability} has unassigned residual {residual}.",
                    )
                )
        if liability > qty_tolerance or required > qty_tolerance:
            classified_liabilities.append(
                ClassifiedLiability(
                    asset=asset,
                    quantity=str(liability),
                    allocated_quantity=str(allocated),
                    residual_quantity=str(residual),
                    classification=classification,
                )
            )

    orders, order_issues = _classify_orders(
        snapshot=snapshot,
        local_positions=local_map,
        pending_intents=pending_intents,
        prefix=bot_prefix,
    )
    issues.extend(order_issues)
    snapshot_complete = not any(
        issue.code
        in {
            "account_endpoint_incomplete",
            "account_snapshot_error",
            "margin_liability_endpoint_unverified",
            "margin_order_endpoint_unverified",
        }
        for issue in issues
    )
    issues = sorted(
        issues,
        key=lambda item: (
            item.code,
            item.scope,
            item.venue,
            item.symbol,
            item.asset,
            item.client_order_id,
        ),
    )
    blocking = any(issue.blocking for issue in issues)
    return AccountReconciliationReport(
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        ready=snapshot_complete and not blocking,
        snapshot_complete=snapshot_complete,
        orders=tuple(orders),
        positions=tuple(classified_positions),
        liabilities=tuple(classified_liabilities),
        issues=tuple(issues),
        exchange_only_symbols=tuple(sorted(exchange_only)),
        local_only_symbols=tuple(local_only),
        mismatched_symbols=tuple(sorted(mismatched)),
        metadata={
            "bot_client_order_prefix": bot_prefix,
            "cash_tolerance_usd": str(cash_tolerance),
            "quantity_tolerance": str(qty_tolerance),
            "margin_account_status": margin_status,
            "margin_open_orders_status": margin_orders_status,
            "dedicated_account_identity_configured": bool(expected_uid),
            "dedicated_account_identity_matched": bool(expected_uid and observed_uid == expected_uid),
        },
    )
