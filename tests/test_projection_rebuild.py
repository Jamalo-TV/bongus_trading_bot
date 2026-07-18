from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest

from bongus.engine.state_store import (
    LifecycleRebuildError,
    StateReader,
    StateWriter,
    Trade,
)


def _position(symbol: str, qty: float) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": "LONG_SPOT_SHORT_PERP",
        "direction": "long",
        "spot_entry": 100.0,
        "perp_entry": 101.0,
        "spot_live": 100.0,
        "perp_live": 101.0,
        "qty": qty,
        "hedge_ratio": 1.0,
        "ann_funding": 0.1,
        "trading_mode": "paper",
    }


def test_corrupt_projections_rebuild_from_hash_checked_journal_and_exchange(
    tmp_path,
) -> None:
    path = str(tmp_path / "state.db")
    writer = StateWriter(path)
    reader = StateReader(path)
    btc = _position("BTCUSDT", 1.0)
    eth = _position("ETHUSDT", 2.0)
    try:
        writer.project_entry_lifecycle(
            event_key="entry-btc",
            intent_id="intent-entry-btc",
            event_time="2026-07-18T10:00:00+00:00",
            position_fields=btc,
            evidence={"exchange_trade_id": "trade-entry-btc"},
        )
        writer.project_exit_lifecycle(
            event_key="exit-btc",
            intent_id="intent-exit-btc",
            event_time="2026-07-18T11:00:00+00:00",
            trade=Trade(
                symbol="BTCUSDT",
                side="LONG_SPOT_SHORT_PERP",
                entry_time="2026-07-18T10:00:00+00:00",
                exit_time="2026-07-18T11:00:00+00:00",
                entry_price=100.0,
                exit_price=102.0,
                qty=1.0,
                net_pnl_usd=2.0,
                cycle_id="cycle-btc",
                entry_intent_id="intent-entry-btc",
                exit_intent_id="intent-exit-btc",
            ),
            evidence={"exchange_trade_id": "trade-exit-btc"},
        )
        writer.project_entry_lifecycle(
            event_key="entry-eth",
            intent_id="intent-entry-eth",
            event_time="2026-07-18T12:00:00+00:00",
            position_fields=eth,
            evidence={"exchange_trade_id": "trade-entry-eth"},
        )

        # Logical projection corruption leaves the append-only evidence intact.
        writer.conn.execute("DELETE FROM positions")
        writer.upsert_position(**_position("BOGUSUSDT", 99.0), commit=False)
        writer.conn.execute("UPDATE trade_history SET net_pnl_usd = -999")
        writer.conn.commit()

        before = reader.get_positions()
        with pytest.raises(LifecycleRebuildError, match="does not match"):
            writer.rebuild_lifecycle_projections(
                authoritative_positions=[_position("ETHUSDT", 3.0)]
            )
        assert reader.get_positions() == before

        proof = writer.rebuild_lifecycle_projections(
            authoritative_positions=[deepcopy(eth)]
        )
        assert proof["exchange_positions_matched"] is True
        assert proof["event_count"] == 3
        assert proof["position_count"] == 1
        assert proof["trade_count"] == 1
        assert len(str(proof["proof_hash"])) == 64
        assert [row["symbol"] for row in reader.get_positions()] == ["ETHUSDT"]
        trades = reader.get_trades(limit=10)
        assert len(trades) == 1
        assert trades[0]["symbol"] == "BTCUSDT"
        assert trades[0]["net_pnl_usd"] == 2.0

        # Journal tampering is rejected before current projections are touched.
        writer.conn.execute(
            "UPDATE lifecycle_events SET payload_json = '{}' WHERE event_key = 'entry-eth'"
        )
        writer.conn.commit()
        stable = reader.get_positions()
        with pytest.raises(LifecycleRebuildError, match="content hash mismatch"):
            writer.rebuild_lifecycle_projections(authoritative_positions=[eth])
        assert reader.get_positions() == stable
    finally:
        reader.close()
        writer.close()
