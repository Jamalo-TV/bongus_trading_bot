from __future__ import annotations

import ast
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import cast

import pytest

from bongus.research.cross_venue.schema import (
    CanonicalAsset,
    ContractMetadata,
    ExactDecimalInput,
    FundingPriceKind,
    FundingSettlement,
    Venue,
    decimal_text,
    deterministic_event_id,
    exact_decimal,
    exact_wire,
)

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_FILES = (
    ROOT / "bongus" / "exchanges" / "hyperliquid_read_only.py",
    ROOT / "bongus" / "research" / "cross_venue" / "__init__.py",
    ROOT / "bongus" / "research" / "cross_venue" / "schema.py",
    ROOT / "bongus" / "research" / "cross_venue" / "feeds.py",
    ROOT / "bongus" / "research" / "cross_venue" / "normalization.py",
    ROOT / "bongus" / "research" / "cross_venue" / "kernel.py",
)


def test_exact_decimal_contract_rejects_binary_float_and_nonfinite_values() -> None:
    assert exact_decimal("0.00000001", "rate") == Decimal("0.00000001")
    assert exact_decimal(2, "quantity") == Decimal("2")
    with pytest.raises(TypeError, match="Decimal"):
        exact_decimal(cast(ExactDecimalInput, 0.1), "rate")
    with pytest.raises(TypeError, match="Decimal"):
        exact_decimal(True, "rate")
    with pytest.raises(ValueError, match="finite"):
        exact_decimal("NaN", "rate")
    assert decimal_text(Decimal("1E-8")) == "0.00000001"
    assert decimal_text(Decimal("-0")) == "0"


def test_wire_contract_serializes_every_decimal_as_plain_text() -> None:
    contract = ContractMetadata(
        venue=Venue.HYPERLIQUID,
        canonical_asset=CanonicalAsset.BTC,
        venue_symbol="BTC",
        contract_id="core:BTC",
        base_asset="BTC",
        quote_asset="USDT",
        settlement_asset="USDC",
        contract_multiplier=Decimal("1"),
        quantity_step=Decimal("0.00001"),
        price_tick=None,
        funding_interval_hours=Decimal("1"),
        status="TRADING",
    )
    wire = exact_wire(contract)
    assert isinstance(wire, dict)
    assert wire["quantity_step"] == "0.00001"
    assert wire["funding_interval_hours"] == "1"
    assert wire["venue"] == "hyperliquid"
    assert all(not isinstance(value, float) for value in wire.values())
    with pytest.raises(ValueError, match="fixed public v1 envelope"):
        replace(contract, environment="live-trading")


def test_event_identity_is_stable_and_settlement_is_causal() -> None:
    first = deterministic_event_id("hyperliquid", "core:BTC", "funding", "100")
    assert first == deterministic_event_id("hyperliquid", "core:BTC", "funding", "100")
    assert first != deterministic_event_id("binance", "core:BTC", "funding", "100")
    with pytest.raises(ValueError, match="before settlement"):
        FundingSettlement(
            event_id=first,
            venue=Venue.HYPERLIQUID,
            canonical_asset=CanonicalAsset.BTC,
            contract_id="core:BTC",
            settlement_time_ns=101,
            available_time_ns=100,
            rate=Decimal("0.0001"),
            settlement_price=Decimal("50000"),
            price_kind=FundingPriceKind.ORACLE,
        )


def test_cross_venue_core_has_no_trading_or_secret_dependency_surface() -> None:
    forbidden_import_prefixes = (
        "bongus.engine",
        "bongus.ipc",
        "bongus.monitoring",
        "bongus.portfolio",
        "bongus.runtime",
        "bongus.strategies",
        "dotenv",
        "eth_account",
        "hmac",
        "hyperliquid",
        "web3",
        "zmq",
    )
    forbidden_source_fragments = (
        "state.db",
        "live_config",
        "127.0.0.1:5555",
        "127.0.0.1:9000",
    )
    for path in PRODUCTION_FILES:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        assert not any(
            module == prefix or module.startswith(prefix + ".")
            for module in imported
            for prefix in forbidden_import_prefixes
        ), (path, imported)
        string_literals = (
            node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
        )
        assert not any(literal.casefold().rstrip("/").endswith("/exchange") for literal in string_literals), path
        lowered = source.casefold()
        assert not any(value.casefold() in lowered for value in forbidden_source_fragments), path
