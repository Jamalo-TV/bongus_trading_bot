"""Canonical multi-symbol live trader runtime."""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from datetime import datetime, timezone
from typing import Any

import requests
from dotenv import load_dotenv

from bongus.core.binance_endpoints import get_rest_base_urls
from bongus.core.config import CANONICAL_RUNTIME_NAME, LIVE_CONFIG_PATH
from bongus.core.config_manager import ConfigManager
from bongus.engine.cost_model import quality_score_from_slippage
from bongus.engine.risk_engine import RiskDecision, RiskEngine, RiskState
from bongus.engine.state_store import (
    CandidateSnapshot,
    ExecutionQualitySample,
    FeatureSnapshot,
    PendingIntent,
    ShadowDecision,
    StateReader,
    StateWriter,
    Trade,
)
from bongus.ipc.execution import ExecutionClient
from bongus.ipc.telemetry import TelemetryClient
from bongus.market_data.depth_tracker import DepthTracker
from bongus.market_data.funding_ranker import MarketCandidate, direction_from_funding, rank_candidates
from bongus.portfolio.portfolio_allocator import PortfolioAllocator, RankedCandidate
from bongus.portfolio.regime_filter import compute_regime_health
from bongus.runtime.shadow_exit import ShadowExitModel

load_dotenv()

logger = logging.getLogger(__name__)


class CanonicalMultiSymbolTrader:
    def __init__(
        self,
        *,
        writer: StateWriter | None = None,
        reader: StateReader | None = None,
        config_manager: ConfigManager | None = None,
        execution_client: ExecutionClient | None = None,
        telemetry_client: TelemetryClient | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.writer = writer or StateWriter()
        self.reader = reader or StateReader()
        self.config_manager = config_manager or ConfigManager(LIVE_CONFIG_PATH)
        self.execution_client = execution_client or ExecutionClient()
        self.telemetry_client = telemetry_client or TelemetryClient()
        self.session = session or requests.Session()
        self.depth_tracker = DepthTracker()
        self.risk_engine = RiskEngine()
        self.trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        self.futures_base_url, self.spot_base_url = get_rest_base_urls(self.trading_mode)
        self.runtime_mode = "PAPER" if self.trading_mode == "paper" else "LIVE"
        self.shadow_model = ShadowExitModel(self.config_manager.get("shadow_exit_model_path", ""))
        self.last_telemetry_ts = 0.0
        self.universe: dict[str, dict[str, Any]] = {}
        # EWMA state for volatility/stability — O(1) update, replaces O(n) deque recalc.
        # alpha ≈ 2/(90+1) gives decay equivalent to a 90-sample rolling window.
        self._ewma_alpha = 2.0 / 91.0
        self._rv_last_price: dict[str, float] = {}
        self._rv_ema_mean: dict[str, float] = {}
        self._rv_ema_var: dict[str, float] = {}
        self._bs_ema_mean: dict[str, float] = {}
        self._bs_ema_var: dict[str, float] = {}

    async def run(self) -> None:
        self.config_manager.start_watching()
        await self.preflight()
        await asyncio.gather(self._telemetry_loop(), self._cycle_loop(), self._prune_loop())

    async def preflight(self) -> None:
        checks: list[str] = []
        try:
            # Run synchronous HTTP calls in the thread pool so we don't block
            # the event loop during startup.
            await asyncio.to_thread(self.refresh_universe)
            checks.append("universe_ready")
        except Exception as exc:  # pragma: no cover - network dependent
            logger.exception("Universe refresh failed during preflight: %s", exc)

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.telemetry_client.host, self.telemetry_client.port),
                timeout=2,
            )
            writer.close()
            await writer.wait_closed()
            self.last_telemetry_ts = time.time()
            checks.append("telemetry_ready")
        except Exception:
            checks.append("telemetry_deferred")

        checks.append("execution_ready")
        checks.append("state_store_ready")

        self.writer.set_risk_snapshot(
            {
                "runtime_name": CANONICAL_RUNTIME_NAME,
                "runtime_mode": self.runtime_mode,
                "trading_mode": self.trading_mode,
                "preflight_status": "passed",
                "preflight_checks": checks,
                "runtime_ready": True,
                "telemetry_connected": self.last_telemetry_ts > 0,
                "bot_started_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def refresh_universe(self) -> None:
        futures_info = self.session.get(f"{self.futures_base_url}/fapi/v1/exchangeInfo", timeout=10).json()
        spot_info = self.session.get(f"{self.spot_base_url}/api/v3/exchangeInfo", timeout=10).json()
        spot_symbols = {
            item["symbol"]
            for item in spot_info.get("symbols", [])
            if item.get("status") == "TRADING" and item.get("quoteAsset") == "USDT"
        }
        universe: dict[str, dict[str, Any]] = {}
        now_ms = time.time() * 1000
        cfg = self.config_manager.snapshot()
        allowlist_value = cfg.get("scanner_allowlist", [])
        blocklist_value = cfg.get("scanner_blocklist", [])
        allowlist = set(allowlist_value if isinstance(allowlist_value, list) else [])
        blocklist = set(blocklist_value if isinstance(blocklist_value, list) else [])
        cluster_map = cfg.get("portfolio_cluster_map", {})
        if not isinstance(cluster_map, dict):
            cluster_map = {}
        default_cluster = str(cfg.get("default_cluster", "OTHER"))
        for item in futures_info.get("symbols", []):
            symbol = item.get("symbol")
            if not symbol or symbol in blocklist:
                continue
            if allowlist and symbol not in allowlist:
                continue
            if item.get("contractType") != "PERPETUAL" or item.get("quoteAsset") != "USDT":
                continue
            onboard = float(item.get("onboardDate", now_ms))
            listing_age_days = max(0.0, (now_ms - onboard) / 86_400_000.0)
            universe[symbol] = {
                "has_spot": symbol in spot_symbols,
                "listing_age_days": listing_age_days,
                "cluster": cluster_map.get(symbol, default_cluster),
                "status": item.get("status", "UNKNOWN"),
            }
        self.universe = universe
        self._cleanup_stale_symbols(set(universe))

    def _cleanup_stale_symbols(self, active: set[str]) -> None:
        """Remove EWMA state for symbols no longer in the active universe."""
        stale = {sym for sym in self._rv_last_price if sym not in active}
        for sym in stale:
            self._rv_last_price.pop(sym, None)
            self._rv_ema_mean.pop(sym, None)
            self._rv_ema_var.pop(sym, None)
            self._bs_ema_mean.pop(sym, None)
            self._bs_ema_var.pop(sym, None)

    async def _telemetry_loop(self) -> None:
        async for event in self.telemetry_client.stream_events():
            if event is None:
                continue
            self.last_telemetry_ts = time.time()
            event_name = str(event.get("event", ""))
            symbol = str(event.get("symbol", ""))
            if event_name == "BookTicker":
                self.depth_tracker.on_book_ticker(symbol, float(event.get("bid_price", 0.0)), float(event.get("ask_price", 0.0)))
            elif event_name == "L2Depth":
                self.depth_tracker.on_l2_depth(symbol, list(event.get("bids", [])), list(event.get("asks", [])))
            elif event_name == "OrderUpdate":
                self.writer.record_execution_event(event)

    async def _cycle_loop(self) -> None:
        while True:
            started = time.perf_counter()
            await asyncio.to_thread(self.run_cycle)
            elapsed = time.perf_counter() - started
            interval = float(self.config_manager.get("trader_cycle_interval_seconds", 15))
            await asyncio.sleep(max(0.0, interval - elapsed))

    def _funding_snapshot(self) -> list[dict[str, Any]]:
        payload = self.session.get(f"{self.futures_base_url}/fapi/v1/premiumIndex", timeout=10).json()
        return payload if isinstance(payload, list) else []

    def _realized_volatility(self, symbol: str, mark_price: float) -> float:
        """EWMA realized volatility — O(1) update, no deque allocation."""
        if mark_price <= 0.0:
            return 0.0
        last = self._rv_last_price.get(symbol)
        self._rv_last_price[symbol] = mark_price
        if last is None or last <= 0.0:
            return 0.0
        r = mark_price / last - 1.0
        alpha = self._ewma_alpha
        mean = self._rv_ema_mean.get(symbol, r)
        var = self._rv_ema_var.get(symbol, 0.0)
        delta = r - mean
        self._rv_ema_mean[symbol] = mean + alpha * delta
        self._rv_ema_var[symbol] = (1.0 - alpha) * (var + alpha * delta * delta)
        return self._rv_ema_var[symbol] ** 0.5

    def _basis_stability(self, symbol: str, basis_pct: float) -> float:
        """EWMA basis stability — O(1) update, no deque allocation."""
        alpha = self._ewma_alpha
        mean = self._bs_ema_mean.get(symbol, basis_pct)
        var = self._bs_ema_var.get(symbol, 0.0)
        delta = basis_pct - mean
        self._bs_ema_mean[symbol] = mean + alpha * delta
        self._bs_ema_var[symbol] = (1.0 - alpha) * (var + alpha * delta * delta)
        return 1.0 / (1.0 + (self._bs_ema_var[symbol] ** 0.5) * 10_000.0)

    def build_candidates(self, funding_rows: list[dict[str, Any]], cfg: dict[str, Any]) -> list[MarketCandidate]:
        now = time.time()
        candidates: list[MarketCandidate] = []
        for row in funding_rows:
            symbol = str(row.get("symbol", ""))
            meta = self.universe.get(symbol)
            if meta is None:
                continue
            depth = self.depth_tracker.get(symbol)
            depth_usd = depth.depth_usd if depth is not None else 0.0
            spread_bps = depth.spread_bps if depth is not None else 10_000.0
            mark_price = float(row.get("markPrice", 0.0) or 0.0)
            index_price = float(row.get("indexPrice", 0.0) or 0.0)
            annualized_funding = float(row.get("lastFundingRate", 0.0) or 0.0) * 1095.0
            basis_pct = 0.0 if index_price <= 0 else (mark_price - index_price) / index_price
            updated_at = 0.0
            if depth is not None and depth.updated_at:
                updated_at = datetime.fromisoformat(depth.updated_at).timestamp()
            staleness = math.inf if updated_at == 0 else max(0.0, now - updated_at)
            realized_volatility = self._realized_volatility(symbol, mark_price or index_price)
            basis_stability = self._basis_stability(symbol, basis_pct)
            regime_health = compute_regime_health(spread_bps, realized_volatility, staleness if staleness != math.inf else 9999.0)
            candidates.append(
                MarketCandidate(
                    symbol=symbol,
                    annualized_funding=annualized_funding,
                    basis_pct=basis_pct,
                    spread_bps=spread_bps,
                    depth_usd=depth_usd,
                    realized_volatility=realized_volatility,
                    basis_stability=basis_stability,
                    regime_health=regime_health,
                    listing_age_days=float(meta.get("listing_age_days", 0.0)),
                    data_staleness_seconds=staleness if staleness != math.inf else 9999.0,
                    has_spot=bool(meta.get("has_spot", False)),
                    is_delist_risk=str(meta.get("status", "")).upper() != "TRADING",
                    toxicity_bps=spread_bps,
                    cluster=str(meta.get("cluster", "OTHER")),
                    imbalance=depth.imbalance if depth is not None else 0.0,
                    mark_price=mark_price or index_price,
                    direction=direction_from_funding(annualized_funding),
                )
            )
        return candidates

    def run_cycle(self) -> dict[str, Any]:
        cfg = self.config_manager.snapshot()
        cycle_id = datetime.now(timezone.utc).isoformat()
        if not self.universe:
            self.refresh_universe()
        candidates = self.build_candidates(self._funding_snapshot(), cfg)
        snapshots, scores = rank_candidates(cycle_id, candidates, cfg)
        snapshot_by_symbol = {snapshot.symbol: snapshot for snapshot in snapshots}

        ranked_candidates = [
            RankedCandidate(
                symbol=score.symbol,
                cluster=snapshot_by_symbol[score.symbol].cluster,
                rank=score.rank,
                total_score=score.total_score,
                predicted_net_edge_bps=score.predicted_net_edge_bps,
                depth_usd=float(snapshot_by_symbol[score.symbol].metrics.get("depth_usd", 0.0)),
                realized_volatility=float(snapshot_by_symbol[score.symbol].metrics.get("realized_volatility", 0.0)),
            )
            for score in scores
        ]
        allocator = PortfolioAllocator(cfg)
        # Fetch positions once per cycle; pass the list to all callees so we
        # avoid 3+ redundant SQLite round-trips on every iteration.
        open_positions = self.reader.get_positions()
        current_positions = {row["symbol"]: float(row["qty"]) for row in open_positions}
        decision = allocator.decide(ranked_candidates, current_positions)
        selected_symbols = {row["symbol"] for row in decision.selected}
        for score in scores:
            score.selected = score.symbol in selected_symbols

        # Batch all observability writes into a single SQLite commit.
        try:
            self.writer.record_candidate_snapshots(snapshots)
            self.writer.record_opportunity_scores(scores)
            sample_minute = cycle_id[:16] + ":00+00:00"
            for snapshot in snapshots:
                metrics = snapshot.metrics
                self.writer.upsert_market_sample(
                    sample_minute=sample_minute,
                    symbol=snapshot.symbol,
                    ann_funding=float(metrics.get("annualized_funding", 0.0)),
                    basis_pct=float(metrics.get("basis_pct", 0.0)),
                    mark_price=float(metrics.get("mark_price", 0.0)),
                    minute_notional_volume=float(metrics.get("depth_usd", 0.0)),
                )
            risk_decision = self._write_runtime_state(cfg, snapshots, decision, open_positions)
        finally:
            # Single commit for all non-critical observability writes above.
            self.writer.flush()

        if risk_decision.allow_new_risk:
            self._apply_decision(decision, snapshot_by_symbol, open_positions)
        self._record_shadow_observations(current_positions, snapshot_by_symbol, decision)
        # Shadow snapshots/decisions are appended after the observability batch and
        # need their own commit before readers on a separate connection can see them.
        self.writer.flush()
        return {"cycle_id": cycle_id, "selected": decision.selected, "exits": decision.exits}

    def _write_runtime_state(
        self,
        cfg: dict[str, Any],
        snapshots: list[CandidateSnapshot],
        decision: Any,
        open_positions: list[dict[str, Any]],
    ) -> RiskDecision:
        telemetry_staleness = max(0.0, time.time() - self.last_telemetry_ts) if self.last_telemetry_ts else 9_999.0
        gross_exposure = sum(
            abs(float(row["qty"])) * max(float(row["spot_live"]), float(row["spot_entry"]))
            for row in open_positions
        )

        # Concentration is relative to total allowable capacity, not just active exposure.
        # Without this, a single open position appears as 100% concentration and triggers
        # the risk engine's symbol concentration limit, blocking all new trades.
        max_exposure_limit = float(cfg.get("max_gross_exposure_usd", 50000.0))
        concentration_denominator = max(gross_exposure, max_exposure_limit)

        risk_state = RiskState(
            gross_exposure_usd=gross_exposure,
            symbol_concentration=0.0 if concentration_denominator <= 0 else max(
                (
                    abs(float(row["qty"])) * max(float(row["spot_live"]), float(row["spot_entry"])) / concentration_denominator
                    for row in open_positions
                ),
                default=0.0,
            ),
            drawdown_pct=0.0,
            data_staleness_minutes=int(telemetry_staleness / 60.0),
            venue_latency_ms=0,
        )
        decision_risk = self.risk_engine.evaluate(risk_state)
        accepted = sum(1 for snapshot in snapshots if snapshot.accepted)
        self.writer.set_stats(
            {
                "scanner_breadth": float(len(snapshots)),
                "accepted_candidates": float(accepted),
                "rejected_candidates": float(len(snapshots) - accepted),
                "open_positions": float(len(open_positions)),
                "gross_exposure": gross_exposure,
            }
        )
        self.writer.set_risk_snapshot(
            {
                "telemetry_staleness_seconds": telemetry_staleness,
                "telemetry_connected": telemetry_staleness <= cfg.get("max_runtime_staleness_seconds", 45.0),
                "gross_exposure_convention": "one_sided",
                "allow_new_risk": decision_risk.allow_new_risk,
                "kill_switch": decision_risk.kill_switch,
                "reasons": decision_risk.reasons,
                "runtime_mode": self.runtime_mode,
                "preflight_status": "running",
                "loop_last_alive_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.writer.record_health_sample(
            metric="telemetry_staleness_seconds",
            value=telemetry_staleness,
            expected_value=cfg.get("max_runtime_staleness_seconds", 45.0),
            runtime_mode=self.runtime_mode,
            alert_level="WARN" if telemetry_staleness > cfg.get("max_runtime_staleness_seconds", 45.0) else "OK",
        )
        return decision_risk

    def _apply_decision(
        self,
        decision: Any,
        snapshot_by_symbol: dict[str, CandidateSnapshot],
        open_positions: list[dict[str, Any]],
    ) -> None:
        for symbol in decision.exits:
            self._dispatch_order(symbol, "EXIT", snapshot_by_symbol.get(symbol), open_positions=open_positions)
        if decision.exits:
            return
        open_symbols = {row["symbol"] for row in open_positions}
        for selected in decision.selected:
            symbol = selected["symbol"]
            if symbol in open_symbols:
                continue
            self._dispatch_order(symbol, "ENTER", snapshot_by_symbol[symbol], target_notional_usd=selected["target_notional_usd"], open_positions=open_positions)

    def _dispatch_order(
        self,
        symbol: str,
        action: str,
        snapshot: CandidateSnapshot | None,
        target_notional_usd: float | None = None,
        open_positions: list[dict[str, Any]] | None = None,
    ) -> None:
        direction = snapshot.direction if snapshot is not None else "LONG_SPOT_SHORT_PERP"
        intent = f"{action}_{'LONG' if direction == 'LONG_SPOT_SHORT_PERP' else 'SHORT'}"
        price = float(snapshot.metrics.get("mark_price", 1.0)) if snapshot is not None else 1.0
        notional = float(target_notional_usd or self.config_manager.get("notional_per_trade", 0.0))
        quantity = round(notional / max(price, 1.0), 6)
        payload = {
            "symbol": symbol,
            "intent": intent,
            "quantity": quantity,
            "urgency": 0.4 if action == "ENTER" else 0.8,
            "max_slippage_bps": float(self.config_manager.get("execution_default_max_slippage_bps", 10.0)),
            "exposure_scale": 1.0,
        }
        intent_id = f"{symbol}:{action}:{int(time.time() * 1000)}"
        self.writer.upsert_pending_intent(
            PendingIntent(
                intent_id=intent_id,
                symbol=symbol,
                intent_type=action,
                direction=direction,
                status="submitted",
                quantity=quantity,
                notional_usd=notional,
                metadata=payload,
            )
        )
        if self.trading_mode == "paper":
            self._simulate_fill(symbol, action, direction, price, quantity, notional, open_positions=open_positions or [])
        else:  # pragma: no cover - live I/O
            self.execution_client.send_order_intent(payload)

    def _simulate_fill(
        self,
        symbol: str,
        action: str,
        direction: str,
        price: float,
        quantity: float,
        notional: float,
        open_positions: list[dict[str, Any]] | None = None,
    ) -> None:
        if action == "ENTER":
            self.writer.upsert_position(
                symbol=symbol,
                side=direction,
                spot_entry=price,
                perp_entry=price,
                qty=quantity,
                ann_funding=0.0,
                basis_pct=0.0,
                spot_live=price,
                perp_live=price,
                status="OPEN",
            )
        else:
            # Use the cycle-cached positions to avoid an extra SQLite round-trip.
            rows = open_positions if open_positions is not None else self.reader.get_positions()
            for row in rows:
                if row["symbol"] != symbol:
                    continue
                self.writer.record_trade(
                    Trade(
                        symbol=symbol,
                        side=row["side"],
                        entry_time=row["updated_at"],
                        exit_time=datetime.now(timezone.utc).isoformat(),
                        entry_price=float(row["spot_entry"]),
                        exit_price=price,
                        qty=float(row["qty"]),
                        net_pnl_usd=0.0,
                    )
                )
                self.writer.remove_position(symbol)

    def _record_shadow_observations(
        self,
        current_positions: dict[str, float],
        snapshot_by_symbol: dict[str, CandidateSnapshot],
        decision: Any,
    ) -> None:
        if not self.config_manager.get_bool("shadow_exit_enabled"):
            return
        for symbol in current_positions:
            snapshot = snapshot_by_symbol.get(symbol)
            if snapshot is None:
                continue
            features = {
                "annualized_funding": snapshot.metrics.get("annualized_funding", 0.0),
                "basis_pct": snapshot.metrics.get("basis_pct", 0.0),
                "spread_bps": snapshot.metrics.get("spread_bps", 0.0),
                "depth_usd": snapshot.metrics.get("depth_usd", 0.0),
                "realized_volatility": snapshot.metrics.get("realized_volatility", 0.0),
                "basis_stability": snapshot.metrics.get("basis_stability", 0.0),
                "regime_health": snapshot.metrics.get("regime_health", 0.0),
            }
            trade_id = symbol
            self.writer.record_feature_snapshot(FeatureSnapshot(trade_id=trade_id, symbol=symbol, features=features))
            hold_score, exit_score = self.shadow_model.score(features)
            incremental_value = hold_score - exit_score
            self.writer.record_shadow_decision(
                ShadowDecision(
                    trade_id=trade_id,
                    symbol=symbol,
                    action="hold" if hold_score >= exit_score else "exit",
                    hold_score=hold_score,
                    exit_score=exit_score,
                    incremental_value_usd=incremental_value,
                    recommended=exit_score > hold_score,
                    metadata={"selected_symbols": [row["symbol"] for row in decision.selected]},
                )
            )
            sample = ExecutionQualitySample(
                symbol=symbol,
                client_order_id=f"shadow:{symbol}",
                side=snapshot.direction,
                order_type="shadow",
                urgency=0.0,
                expected_cost_bps=float(snapshot.metrics.get("spread_bps", 0.0)),
                realized_slippage_bps=float(snapshot.metrics.get("spread_bps", 0.0)) / 2.0,
                spread_bps=float(snapshot.metrics.get("spread_bps", 0.0)),
                depth_usd=float(snapshot.metrics.get("depth_usd", 0.0)),
                maker=False,
                quality_score=quality_score_from_slippage(float(snapshot.metrics.get("spread_bps", 0.0)) / 2.0),
            )
            self.writer.record_execution_quality(sample)

    async def _prune_loop(self) -> None:
        """Archive old SQLite rows once per day to keep the DB compact."""
        while True:
            await asyncio.sleep(86_400)
            try:
                cfg = self.config_manager.snapshot()
                result = await asyncio.to_thread(
                    self.writer.archive_old_data,
                    retention_days=int(cfg.get("data_retention_days", 30)),
                    market_retention_days=int(cfg.get("market_sample_retention_days", 7)),
                    health_retention_days=int(cfg.get("health_sample_retention_days", 7)),
                )
                logger.info("Daily pruning complete: %s", result)
            except Exception as exc:
                logger.exception("Daily pruning failed: %s", exc)

    def close(self) -> None:
        self.config_manager.stop_watching()
        self.writer.close()
        self.reader.close()
        self.execution_client.close()
        self.session.close()


async def main() -> None:
    trader = CanonicalMultiSymbolTrader()
    try:
        await trader.run()
    finally:
        trader.close()
