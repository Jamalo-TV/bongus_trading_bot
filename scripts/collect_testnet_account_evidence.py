"""Collect immutable, strictly read-only Binance testnet reconciliation evidence."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bongus.core.binance_endpoints import (
    get_rest_base_urls,
    normalize_trading_mode,
    resolve_binance_credentials,
)
from bongus.core.config_manager import ConfigManager
from bongus.engine.account_reconciliation import reconcile_account_snapshot
from bongus.engine.exchange_statements import normalize_binance_futures_income
from bongus.engine.state_store import StateReader
from bongus.exchanges.binance_account_snapshot import BinanceAccountSnapshotClient

TERMINAL_INTENT_STATES = {"FILLED", "CANCELLED", "REJECTED", "EXPIRED", "TERMINAL"}
STABLE_ASSETS = {"USD", "USDT", "USDC", "FDUSD", "BUSD"}


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=str,
    ).encode("utf-8")


def _positive_rows(rows: object, quantity_fields: tuple[str, ...]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not isinstance(rows, list):
        return result
    for row in rows:
        if not isinstance(row, dict):
            continue
        total = 0.0
        for field in quantity_fields:
            try:
                total += abs(float(row.get(field) or 0.0))
            except (TypeError, ValueError):
                pass
        if total <= 1e-9:
            continue
        result.append(
            {
                key: str(row.get(key) or "")
                for key in (
                    "asset",
                    "symbol",
                    "positionAmt",
                    "positionSide",
                    "free",
                    "locked",
                    "borrowed",
                    "interest",
                )
                if key in row
            }
        )
    return result


def _reconciliation_snapshot(
    snapshot: dict[str, Any],
    *,
    prices: dict[str, str],
    generated_at: str,
    snapshot_complete: bool,
    account_identity_verified: bool,
) -> dict[str, Any]:
    balances: dict[str, Decimal] = {}

    def add(asset: object, *values: object) -> None:
        name = str(asset or "").strip().upper()
        if not name:
            return
        total = Decimal("0")
        for value in values:
            try:
                parsed = Decimal(str(value or "0"))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if parsed.is_finite():
                total += parsed
        balances[name] = balances.get(name, Decimal("0")) + total

    futures = snapshot.get("futures_account")
    futures = futures if isinstance(futures, dict) else {}
    futures_assets = futures.get("assets")
    if isinstance(futures_assets, list) and futures_assets:
        for row in futures_assets:
            if isinstance(row, dict):
                add(row.get("asset"), row.get("walletBalance"))
    elif futures:
        add("USDT", futures.get("totalWalletBalance"))

    spot = snapshot.get("spot_account")
    spot = spot if isinstance(spot, dict) else {}
    for row in spot.get("balances") or []:
        if isinstance(row, dict):
            add(row.get("asset"), row.get("free"), row.get("locked"))

    margin = snapshot.get("margin_account")
    margin = margin if isinstance(margin, dict) else {}
    for row in margin.get("userAssets") or []:
        if isinstance(row, dict):
            add(row.get("asset"), row.get("netAsset"))

    positions: dict[str, Decimal] = {}
    for row in snapshot.get("position_risk") or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").strip().upper()
        side = str(row.get("positionSide") or "BOTH").strip().upper()
        try:
            quantity = Decimal(str(row.get("positionAmt") or "0"))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if symbol and quantity.is_finite() and quantity != 0:
            positions[f"{symbol}:{side}"] = quantity

    valued_assets = set(balances) | set(prices) | STABLE_ASSETS
    normalized_prices = {
        asset: ("1" if asset in STABLE_ASSETS else str(prices.get(asset) or ""))
        for asset in sorted(valued_assets)
        if asset in STABLE_ASSETS or str(prices.get(asset) or "")
    }
    return {
        "observed_at": generated_at,
        "snapshot_complete": snapshot_complete,
        "account_identity_verified": account_identity_verified,
        "combined_balances": {
            asset: format(value, "f")
            for asset, value in sorted(balances.items())
            if value != 0
        },
        "balance_tolerances": {
            asset: ("0.01" if asset in STABLE_ASSETS else "0.00000001")
            for asset, value in sorted(balances.items())
            if value != 0
        },
        "asset_prices_usd": normalized_prices,
        "perpetual_positions": {
            key: format(value, "f") for key, value in sorted(positions.items())
        },
        "position_tolerance": "0.00000001",
    }


def build_artifact(
    *,
    snapshot: dict[str, Any],
    prices: dict[str, str],
    endpoint_statuses: dict[str, str],
    local_positions: list[dict[str, Any]],
    pending_intents: list[dict[str, Any]],
    expected_uid: str,
    account_id: str,
    generated_at: str,
) -> dict[str, Any]:
    report = reconcile_account_snapshot(
        snapshot,
        local_positions=local_positions,
        pending_intents=pending_intents,
        asset_prices_usd=prices,
        expected_account_uid=expected_uid,
        require_account_uid=True,
        generated_at=generated_at,
    )
    report_payload = report.to_dict()
    unexplained_position_count = sum(
        position.classification
        in {"exchange_only", "local_only", "mismatched", "manual_review"}
        for position in report.positions
    )
    unexplained_order_count = sum(
        order.ownership.value != "bot_owned" for order in report.orders
    )
    futures_positions = _positive_rows(
        snapshot.get("position_risk"), ("positionAmt",)
    )
    spot_balances = _positive_rows(
        (snapshot.get("spot_account") or {}).get("balances")
        if isinstance(snapshot.get("spot_account"), dict)
        else [],
        ("free", "locked"),
    )
    margin_assets = _positive_rows(
        (snapshot.get("margin_account") or {}).get("userAssets")
        if isinstance(snapshot.get("margin_account"), dict)
        else [],
        ("borrowed", "interest"),
    )
    funding_statements = []
    for row in snapshot.get("funding_income") or []:
        statement = normalize_binance_futures_income(
            row,
            account_id=account_id,
            trading_mode="testnet",
            strategy_id="funding-arbitrage-v2",
        )
        funding_statements.append(
            {
                "statement_key": statement.statement_key,
                "content_hash": statement.content_hash,
                "exchange_transaction_id": statement.exchange_transaction_id,
                "event_time": statement.event_time,
                "symbol": statement.symbol,
                "asset": statement.asset,
                "amount": statement.amount,
                "reconciliation_status": statement.reconciliation_status,
            }
        )
    raw_snapshot_sha256 = hashlib.sha256(_canonical_bytes(snapshot)).hexdigest()
    return {
        "schema_version": 1,
        "evidence_kind": "account_reconciliation",
        "environment": "testnet",
        "generated_at": generated_at,
        "account_id": account_id,
        "collection_policy": {
            "read_only": True,
            "http_methods": ["GET"],
            "orders_cancelled": 0,
            "orders_submitted": 0,
            "transfers_submitted": 0,
        },
        "endpoint_statuses": endpoint_statuses,
        "raw_snapshot_sha256": raw_snapshot_sha256,
        "exchange_reconciliation_snapshot": _reconciliation_snapshot(
            snapshot,
            prices=prices,
            generated_at=generated_at,
            snapshot_complete=report.snapshot_complete,
            account_identity_verified=bool(
                report.metadata.get("dedicated_account_identity_matched")
            ),
        ),
        "exchange_facts": {
            "open_futures_positions": futures_positions,
            "positive_spot_balances": spot_balances,
            "margin_liabilities": margin_assets,
            "funding_statements": funding_statements,
            "futures_open_order_count": len(snapshot.get("futures_open_orders") or []),
            "spot_open_order_count": len(snapshot.get("spot_open_orders") or []),
            "margin_open_order_count": len(snapshot.get("margin_open_orders") or []),
        },
        "local_facts": {
            "open_position_count": len(local_positions),
            "nonterminal_pending_intent_count": len(pending_intents),
        },
        "reconciliation": report_payload,
        "gate_metrics": {
            "unclassified_open_orders_positions": (
                unexplained_position_count + unexplained_order_count
            ),
            "ready_under_mismatch": bool(report.ready and report.mismatched_symbols),
            "actual_funding_settlements_observed": len(funding_statements),
        },
        "machine_attestation": {
            "attested": True,
            "basis": "direct signed Binance testnet GET readback plus local SQLite projection",
            "dedicated_account_uid_configured": bool(expected_uid),
            "reconciliation_fingerprint": report.fingerprint,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect signed GET-only Binance testnet account evidence."
    )
    parser.add_argument("--db", type=Path, default=ROOT / "state.db")
    parser.add_argument("--config", type=Path, default=ROOT / "live_config.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    mode = normalize_trading_mode()
    if mode != "testnet":
        parser.error("evidence collection is restricted to TRADING_MODE=testnet")
    config = ConfigManager(args.config)
    if not config.get_bool("pause_new_entries"):
        parser.error("pause_new_entries must remain true during evidence collection")
    if config.get_float("per_symbol_notional_cap_usd") > 2_500.0:
        parser.error("per-symbol cap exceeds the protected ceiling")
    if config.get_float("max_gross_exposure_usd") > 10_000.0:
        parser.error("gross cap exceeds the protected ceiling")

    credentials = resolve_binance_credentials()
    futures_base, spot_base = get_rest_base_urls(mode)
    client = BinanceAccountSnapshotClient(
        futures_base_url=futures_base,
        spot_base_url=spot_base,
        futures_api_key=credentials["futures_api_key"],
        futures_api_secret=credentials["futures_api_secret"],
        spot_api_key=credentials["spot_api_key"],
        spot_api_secret=credentials["spot_api_secret"],
    )
    snapshot, prices, statuses = client.collect()
    reader = StateReader(str(args.db))
    try:
        local_positions = reader.get_positions_for_current_mode()
        pending_intents = [
            row
            for row in reader.get_pending_intents(limit=10_000)
            if str(row.get("status") or "").upper() not in TERMINAL_INTENT_STATES
        ]
    finally:
        reader.close()

    generated_at = datetime.now(timezone.utc).isoformat()
    artifact = build_artifact(
        snapshot=snapshot,
        prices=prices,
        endpoint_statuses=statuses,
        local_positions=local_positions,
        pending_intents=pending_intents,
        expected_uid=os.getenv("BONGUS_EXPECTED_ACCOUNT_UID", "").strip(),
        account_id=os.getenv("BINANCE_ACCOUNT_ID", "binance-default").strip(),
        generated_at=generated_at,
    )
    payload = _canonical_bytes(artifact) + b"\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(args.output)
    digest = hashlib.sha256(payload).hexdigest()
    print(
        json.dumps(
            {
                "status": "collected",
                "output": str(args.output.resolve()),
                "sha256": digest,
                "reconciliation_ready": artifact["reconciliation"]["ready"],
                "blocking_issue_count": artifact["reconciliation"][
                    "blocking_issue_count"
                ],
                "read_only": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
