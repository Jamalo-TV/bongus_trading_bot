"""Multi-symbol live trader orchestrator.

Wires together:
  - RustDataSubscriber (depth + fill confirmations from Rust port 9000)
  - FundingRanker (single REST call every 60s)
  - CorrelationBreaker (portfolio-level circuit breaker)
  - PortfolioAllocator (sizing, liquidity filter, rotation)
  - ExecutionClient (ZMQ PUSH to Rust)
  - StateWriter/StateReader (SQLite shared state)

Execution invariant: exits are dispatched first; ENTER for a rotation target
only fires after FILLED confirmation from Rust (or timeout fallback).

The original live_trader.py is preserved as a single-symbol fallback.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import signal
import time
import uuid
from urllib.parse import urlencode
from datetime import datetime, timedelta, timezone
from statistics import fmean, pstdev

import requests
from dotenv import load_dotenv

from bongus.core.config import (
    CAPITAL_PER_SLOT_USD,
    TARGET_LEVERAGE,
    ROTATION_CONFIRM_TIMEOUT_S,
    FUNDING_SNAPSHOT_HOURS,
    DYNAMIC_SYMBOL_MODE,
    ENTRY_PREMIUM_THRESHOLD,
    INVERSE_FUNDING_ENABLED,
    MAX_ALLOWED_GAP_MINUTES,
    MAX_CONCURRENT_POSITIONS,
    MAX_SYMBOL_CONCENTRATION,
    FUNDING_INTERVAL_HOURS,
    FUNDING_PERIODS_PER_YEAR,
    PENDING_INTENT_MAX_AGE_SECONDS,
    ROTATION_MIN_GAP_ANN,
    ADAPTIVE_RULES_PAPER_ONLY,
    ADAPTIVE_THRESHOLDS_ENABLED,
    AI_REPORT_AGENT_ENABLED,
    DAILY_PNL_SUMMARY_HOUR_UTC,
    DAILY_PNL_SUMMARY_MINUTE_UTC,
    DATA_RETENTION_DAYS,
    HEALTH_ALERT_ZSCORE,
    HEALTH_MONITOR_ENABLED,
    HEALTH_SAFE_MODE_ZSCORE,
    HEALTH_SAMPLE_RETENTION_DAYS,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_MISS_THRESHOLD,
    LOSS_STREAK_ENTRY_MULTIPLIER,
    LOSS_STREAK_NOTIONAL_SCALE,
    LOSS_STREAK_TRIGGER,
    MARKET_SAMPLE_RETENTION_DAYS,
    VALIDATION_SNAPSHOT_INTERVAL_MINUTES,
    WIN_STREAK_RESET,
    get_monitored_symbols,
)
from bongus.core.config_manager import ConfigManager
from bongus.engine.cooldown_manager import CooldownManager
from bongus.engine.cost_model import blended_entry_cost, blended_exit_cost
from bongus.engine.risk_engine import RiskDecision, RiskEngine, RiskLimits, RiskState
from bongus.engine.state_store import StateWriter, StateReader, Trade
from bongus.ipc.execution import ExecutionClient
from bongus.market_data.bybit_monitor import BybitFundingMonitor
from bongus.market_data.depth_tracker import DepthTracker
from bongus.market_data.funding_predictor import FundingPredictor, MIN_CONFIDENCE_FOR_ENTRY
from bongus.market_data.funding_ranker import FundingRanker
from bongus.market_data.rust_data_subscriber import RustDataSubscriber
from bongus.market_data.rest_depth_fetcher import RestDepthFetcher
from bongus.monitoring.performance_metrics import calculate_metrics
from bongus.portfolio.correlation_breaker import CorrelationBreaker
from bongus.portfolio.portfolio_allocator import OpenPosition, PortfolioAllocator
from bongus.portfolio.regime_filter import RegimeDecision, RegimeFilter

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_DOTENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
_SENTIMENT_PATH = os.path.join(_PROJECT_ROOT, "current_sentiment.json")

load_dotenv(_DOTENV_PATH)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("live_trader_v2")

# If the circuit breaker stays HALTED for this long, escalate to partial exits
# rather than holding troubled positions indefinitely with no recovery path.
_HALTED_ESCALATION_SECS: int = 1800  # 30 minutes
_SIGNED_RECV_WINDOW_MS: int = 5_000
_POSITION_QTY_TOLERANCE: float = 1e-9
_DEFAULT_COST_DEPTH_USD: float = 500_000.0
_BLOCKED_EXIT_CODE: int = 78
_QUOTE_ASSET_SUFFIXES: tuple[str, ...] = (
    "USDT",
    "USDC",
    "FDUSD",
    "BUSD",
    "BTC",
    "ETH",
    "BNB",
    "TRY",
    "EUR",
)


class StartupBlockedError(RuntimeError):
    pass


def _float_or_zero(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _iso_from_ms(value) -> str:
    try:
        timestamp_ms = int(float(value))
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()
    if timestamp_ms <= 0:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(timestamp_ms / 1000.0, tz=timezone.utc).isoformat()


def _extract_base_asset(symbol: str) -> str:
    upper_symbol = symbol.upper()
    for suffix in _QUOTE_ASSET_SUFFIXES:
        if upper_symbol.endswith(suffix) and len(upper_symbol) > len(suffix):
            return upper_symbol[:-len(suffix)]
    return upper_symbol


class LiveTraderV2:
    def __init__(self) -> None:
        self._trading_mode = os.getenv("TRADING_MODE", "paper").lower()
        logger.info("TRADING_MODE = %s", self._trading_mode)
        logger.info(
            "Runtime config: ACCOUNT_EQUITY_USD=%s MAX_GROSS_EXPOSURE_USD=%s MONITORED_SYMBOLS=%s",
            os.getenv("ACCOUNT_EQUITY_USD", "10000"),
            os.getenv("MAX_GROSS_EXPOSURE_USD", "50000"),
            os.getenv("MONITORED_SYMBOLS", "<default>"),
        )
        self.monitored_symbols = get_monitored_symbols()
        self._monitored_symbol_set = set(self.monitored_symbols)

        self.depth_tracker = DepthTracker()
        tracked_symbols = None if DYNAMIC_SYMBOL_MODE else self.monitored_symbols
        self.funding_ranker = FundingRanker(tracked_symbols)
        self.breaker = CorrelationBreaker()
        self.state_writer = StateWriter()
        self.state_reader = StateReader()
        self._config = ConfigManager(
            on_validation_error=self._on_config_validation_error,
            on_reload=self._on_config_reloaded,
        )
        self.allocator = PortfolioAllocator(self.depth_tracker, self.funding_ranker)
        self.predictor = FundingPredictor()
        self.bybit_monitor = BybitFundingMonitor(tracked_symbols)
        self.regime_filter = RegimeFilter(self.depth_tracker, config_get=self._config.get)
        self.cooldowns = CooldownManager(config_get=self._config.get)
        # REST fallback depth fetcher - used when WebSocket depth is unavailable
        self.rest_depth_fetcher = RestDepthFetcher(self.monitored_symbols)
        self._last_compound_check: float = 0.0
        self._last_xval_check: float = 0.0
        self._xval_last_warn_at: dict[str, float] = {}
        self._xval_mismatch_snapshot: dict[str, tuple[float, float]] = {}
        self._sentiment_score: float = 0.0
        self._last_breaker_state: str = "CLEAR"
        # Tracks when the circuit breaker first entered HALTED state.
        # If HALTED persists beyond _HALTED_ESCALATION_SECS, exit troubled positions
        # rather than holding them indefinitely with no recovery path.
        self._halted_since: float = 0.0
        self.execution = ExecutionClient(endpoint="tcp://127.0.0.1:5555")
        self._config.start_watching()
        self._shutdown_started = False
        self._shutdown_event = asyncio.Event()
        self._background_tasks: list[asyncio.Task] = []
        self._safe_mode_flags: set[str] = set()
        self._blocked_reason: str = ""
        self._runtime_mode: str = "LIVE"
        self._last_runtime_mode_change: str = datetime.now(timezone.utc).isoformat()
        self._last_heartbeat_sent_monotonic: float = 0.0
        self._last_heartbeat_ack_monotonic: float = 0.0
        self._heartbeat_misses: int = 0
        self._last_heartbeat_ack_id: str = ""
        self._last_heartbeat_ack_at: str = ""
        self._latest_volume_bar: dict[str, tuple[str, float]] = {}
        self._last_sampled_minute: str = ""
        self._last_retention_run_date: str = ""
        self._last_validation_snapshot_bucket: int | None = None
        self._preflight_status: str = "idle"
        self._bot_started_at: str = datetime.now(timezone.utc).isoformat()
        self._adaptive_entry_threshold_base: float = float(self._config.get("entry_ann_funding_threshold"))
        self._adaptive_rotation_gap: float = ROTATION_MIN_GAP_ANN
        self._streak_notional_scale: float = 1.0
        self._risk_position_scale: float = 1.0
        self._risk_allow_new_risk: bool = True
        self._risk_derisk_required: bool = False
        self._risk_kill_switch: bool = False
        self._risk_reasons: list[str] = []
        self._risk_last_evaluated_at: str = ""
        self._loss_streak: int = 0
        self._win_streak: int = 0
        self._last_exchange_health_check_monotonic: float = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._futures_api_key = os.getenv("BINANCE_API_KEY", "").strip()
        self._futures_api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
        self._spot_api_key = os.getenv("BINANCE_SPOT_API_KEY", self._futures_api_key).strip()
        self._spot_api_secret = os.getenv("BINANCE_SPOT_API_SECRET", self._futures_api_secret).strip()
        if self._trading_mode == "live":
            self._futures_base_url = "https://fapi.binance.com"
            self._spot_base_url = "https://api.binance.com"
        else:
            self._futures_base_url = "https://testnet.binancefuture.com"
            self._spot_base_url = "https://testnet.binance.vision"
        self._binance_time_offset_ms: int = 0

        # Write trading mode to state DB so dashboard can display it
        self.state_writer.set_risk_snapshot(
            {
                "trading_mode": self._trading_mode,
                "runtime_mode": self._runtime_mode,
                "preflight_status": self._preflight_status,
                "bot_started_at": self._bot_started_at,
                "allow_new_risk": True,
            }
        )
        self._peak_account_equity: float = float(self._config.get("account_equity_usd"))
        self._risk_engine = RiskEngine()

        # Pending exit tracking: symbol → asyncio.Event (set when FILLED received from Rust).
        # Note: spec described this as set[str]; dict[str, Event] enables per-symbol await
        # without a global polling loop — deliberate improvement over the spec.
        self._exit_events: dict[str, asyncio.Event] = {}
        self._pending_exit_intents: dict[str, str] = {}
        self._pending_exit_created_at: dict[str, str] = {}

        # Pending enter tracking: symbol → entry intent data stored at dispatch time.
        # Consumed when ENTER FILLED arrives to write position to SQLite.
        self._pending_enters: dict[str, dict] = {}
        self._stale_pending_enters: dict[str, dict] = {}

        # Entry time cache: populated on ENTER fill, consumed on EXIT fill for trade record.
        self._entry_times: dict[str, str] = {}

        # Estimated entry-side execution cost, captured at dispatch time and used
        # as a fallback when live exchange commissions are unavailable.
        self._estimated_entry_costs: dict[str, float] = {}

        # Mark price cache: populated from perp markPrice WebSocket events.
        # Used by _dispatch_enter to compute base-asset qty from notional.
        self._mark_prices: dict[str, float] = {}
        self._mark_price_updated_monotonic: dict[str, float] = {}

        # Track when we first received mark price for each symbol (for startup readiness check)
        self._mark_price_ready: set[str] = set()

        # LOT_SIZE step sizes per symbol fetched from Binance at startup.
        # Keyed by symbol (e.g. "BTCUSDT" → 0.001). Falls back to 1e-5 if absent.
        self._lot_step: dict[str, float] = {}

        # Direction cache: populated from state DB each loop iteration.
        # "long" = long spot + short perp; "short" = short spot + long perp (inverse funding).
        self._position_directions: dict[str, str] = {}

        self.subscriber = RustDataSubscriber(
            on_depth=self._on_depth_update,
            on_order_update=self._on_order_update,
            on_mark_price=self._on_mark_price,
            on_heartbeat_ack=self._on_heartbeat_ack,
            on_volume_bar=self._on_volume_bar,
        )

    def _maybe_log_cross_validation_gap(
        self,
        symbol: str,
        ranker_rate: float,
        bybit_rate: float,
        *,
        now: float,
    ) -> None:
        gap = abs(bybit_rate - ranker_rate)
        if gap <= 0.01:
            if symbol in self._xval_mismatch_snapshot:
                logger.info(
                    "Cross-validation back within tolerance for %s: ranker=%.4f bybit=%.4f",
                    symbol,
                    ranker_rate,
                    bybit_rate,
                )
                self._xval_mismatch_snapshot.pop(symbol, None)
                self._xval_last_warn_at.pop(symbol, None)
            return

        previous = self._xval_mismatch_snapshot.get(symbol)
        significant_shift = previous is None
        if previous is not None:
            prev_ranker_rate, prev_bybit_rate = previous
            prev_gap = abs(prev_bybit_rate - prev_ranker_rate)
            significant_shift = (
                abs(gap - prev_gap) >= 0.02
                or (ranker_rate > 0.0) != (prev_ranker_rate > 0.0)
                or (bybit_rate > 0.0) != (prev_bybit_rate > 0.0)
            )

        last_warn = self._xval_last_warn_at.get(symbol)
        if last_warn is None or now - last_warn >= 600 or significant_shift:
            logger.warning(
                "Cross-validation mismatch for %s: ranker=%.4f bybit=%.4f",
                symbol,
                ranker_rate,
                bybit_rate,
            )
            self._xval_last_warn_at[symbol] = now

        self._xval_mismatch_snapshot[symbol] = (ranker_rate, bybit_rate)

    def _on_config_validation_error(self, error: str) -> None:
        logger.warning("Rejected live_config.json reload: %s", error)
        self._set_config_reload_status(
            {
                "config_last_error": error,
                "config_last_error_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _on_config_reloaded(self, changed: dict, snapshot: dict) -> None:
        del snapshot
        self._set_config_reload_status(
            {
                "config_last_error": "",
                "config_last_reload_at": datetime.now(timezone.utc).isoformat(),
                "config_last_reloaded_keys": sorted(changed.keys()),
            }
        )

    def _set_config_reload_status(self, payload: dict[str, object]) -> None:
        state_writer = getattr(self, "state_writer", None)
        if state_writer is None:
            return
        state_writer.set_risk_snapshot(payload)

    def _adaptive_controls_enabled(self) -> bool:
        enabled = bool(self._config.get("adaptive_thresholds_enabled"))
        if not enabled:
            return False
        if bool(self._config.get("adaptive_rules_paper_only")) and self._trading_mode == "live":
            return False
        return True

    def _health_monitor_enabled(self) -> bool:
        enabled = bool(self._config.get("health_monitor_enabled"))
        if not enabled:
            return False
        if bool(self._config.get("adaptive_rules_paper_only")) and self._trading_mode == "live":
            return False
        return True

    def _ai_report_agent_enabled(self) -> bool:
        enabled = bool(self._config.get("ai_report_agent_enabled"))
        if not enabled:
            return False
        if bool(self._config.get("adaptive_rules_paper_only")) and self._trading_mode == "live":
            return False
        return True

    def _safe_mode_reason(self) -> str:
        return ", ".join(sorted(self._safe_mode_flags))

    def _persist_runtime_state(self) -> None:
        safe_reason = self._safe_mode_reason()
        funding_status = self.funding_ranker.status_snapshot()
        allow_new_risk = self._runtime_mode == "LIVE" and self._risk_allow_new_risk
        self.state_writer.set_risk_snapshot(
            {
                "runtime_mode": self._runtime_mode,
                "safe_mode_reason": safe_reason,
                "blocked_reason": self._blocked_reason,
                "allow_new_risk": allow_new_risk,
                "preflight_status": self._preflight_status,
                "execution_bridge_healthy": self._heartbeat_misses < int(self._config.get("heartbeat_miss_threshold")),
                "heartbeat_status": (
                    "ok"
                    if self._heartbeat_misses == 0 and self._last_heartbeat_ack_monotonic > 0.0
                    else ("missed" if self._heartbeat_misses > 0 else "unknown")
                ),
                "heartbeat_miss_count": self._heartbeat_misses,
                "heartbeat_last_ack_id": self._last_heartbeat_ack_id,
                "heartbeat_last_ack_at": (
                    self._last_heartbeat_ack_at
                    if self._last_heartbeat_ack_monotonic > 0.0
                    else ""
                ),
                "funding_staleness_status": funding_status["funding_staleness_status"],
                "funding_last_refresh_at": funding_status["funding_last_refresh_at"],
                "funding_last_refresh_age_s": funding_status["funding_last_refresh_age_s"],
                "funding_consecutive_failures": funding_status["funding_consecutive_failures"],
                "funding_last_error": funding_status["funding_last_error"],
                "last_runtime_mode_change": self._last_runtime_mode_change,
                "risk_derisk_required": self._risk_derisk_required,
                "risk_kill_switch": self._risk_kill_switch,
                "risk_position_scale": self._risk_position_scale,
                "risk_reasons": self._risk_reasons,
                "risk_last_evaluated_at": self._risk_last_evaluated_at,
                "pending_enter_count": len(self._pending_enters),
                "stale_pending_enter_count": len(self._stale_pending_enters),
                "pending_exit_count": len(self._pending_exit_intents),
            }
        )

    def _recompute_runtime_mode(self) -> None:
        previous_mode = self._runtime_mode
        if self._blocked_reason:
            self._runtime_mode = "BLOCKED"
        elif self._safe_mode_flags:
            self._runtime_mode = "SAFE_MODE"
        else:
            self._runtime_mode = "LIVE"
        if self._runtime_mode != previous_mode:
            self._last_runtime_mode_change = datetime.now(timezone.utc).isoformat()
            if self._runtime_mode in {"SAFE_MODE", "BLOCKED"}:
                notes = (
                    f"{previous_mode}->{self._runtime_mode}: "
                    f"{self._safe_mode_reason() or self._blocked_reason or 'operator attention required'}"
                )
                self._record_operator_intervention(
                    sample_time=self._last_runtime_mode_change,
                    notes=notes,
                    alert_level="critical" if self._runtime_mode == "BLOCKED" else "warning",
                )
        self._persist_runtime_state()

    def _set_safe_mode_flag(self, reason: str, enabled: bool) -> None:
        if enabled:
            self._safe_mode_flags.add(reason)
        else:
            self._safe_mode_flags.discard(reason)
        self._recompute_runtime_mode()

    def _set_blocked_reason(self, reason: str) -> None:
        self._blocked_reason = reason
        self._recompute_runtime_mode()

    def _install_signal_handlers(self) -> None:
        if self._loop is None:
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self._loop.add_signal_handler(
                    signum,
                    lambda current=signum: asyncio.create_task(
                        self.shutdown(reason=f"signal:{current.name.lower()}")
                    ),
                )
            except (NotImplementedError, RuntimeError, ValueError):
                try:
                    loop = self._loop
                    if loop is None:
                        continue
                    signal.signal(
                        signum,
                        lambda *_args, current=signum, current_loop=loop: current_loop.call_soon_threadsafe(
                            lambda: asyncio.create_task(
                                self.shutdown(reason=f"signal:{current.name.lower()}")
                            )
                        ),
                    )
                except Exception:
                    continue

    async def _sleep_or_shutdown(self, seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=max(0.0, seconds))
            return True
        except asyncio.TimeoutError:
            return False

    def _signed_timestamp_ms(self) -> int:
        return int(time.time() * 1000) + self._binance_time_offset_ms

    async def _sync_binance_time(self) -> None:
        response = await asyncio.to_thread(
            requests.get,
            f"{self._futures_base_url}/fapi/v1/time",
            timeout=10,
        )
        response.raise_for_status()
        server_time = int(response.json()["serverTime"])
        self._binance_time_offset_ms = server_time - int(time.time() * 1000)

    async def _signed_get_json(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict[str, str | int | float] | None = None,
        api_key: str,
        api_secret: str,
    ):
        return await self._signed_request_json(
            method="GET",
            base_url=base_url,
            endpoint=endpoint,
            params=params,
            api_key=api_key,
            api_secret=api_secret,
        )

    async def _signed_delete_json(
        self,
        *,
        base_url: str,
        endpoint: str,
        params: dict[str, str | int | float] | None = None,
        api_key: str,
        api_secret: str,
    ):
        return await self._signed_request_json(
            method="DELETE",
            base_url=base_url,
            endpoint=endpoint,
            params=params,
            api_key=api_key,
            api_secret=api_secret,
        )

    async def _signed_request_json(
        self,
        *,
        method: str,
        base_url: str,
        endpoint: str,
        params: dict[str, str | int | float] | None = None,
        api_key: str,
        api_secret: str,
    ):
        if not api_key or not api_secret:
            raise RuntimeError(f"Missing Binance credentials for signed request {endpoint}")

        query_params: dict[str, str | int | float] = dict(params or {})
        query_params["recvWindow"] = int(query_params.get("recvWindow", _SIGNED_RECV_WINDOW_MS))
        query_params["timestamp"] = self._signed_timestamp_ms()
        query_string = urlencode(query_params)
        signature = hmac.new(
            api_secret.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        url = f"{base_url}{endpoint}?{query_string}&signature={signature}"
        request_fn = requests.get if method.upper() == "GET" else requests.delete
        response = await asyncio.to_thread(
            request_fn,
            url,
            headers={"X-MBX-APIKEY": api_key},
            timeout=10,
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Binance request failed for {endpoint}: HTTP {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError(f"Invalid JSON from Binance for {endpoint}") from exc

    async def _public_get_json(self, url: str):
        response = await asyncio.to_thread(requests.get, url, timeout=10)
        response.raise_for_status()
        return response.json()

    async def _ping_exchange(self) -> None:
        errors: list[str] = []
        for label, url in (
            ("futures_time", f"{self._futures_base_url}/fapi/v1/time"),
            ("spot_ping", f"{self._spot_base_url}/api/v3/ping"),
        ):
            last_exc: Exception | None = None
            for attempt in range(3):
                try:
                    await self._public_get_json(url)
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    await asyncio.sleep(0.5 * (2 ** attempt))
            if last_exc is not None:
                errors.append(f"{label}: {last_exc}")
        if errors:
            raise RuntimeError("; ".join(errors))

    def _validate_required_credentials(self) -> None:
        if self._trading_mode == "paper":
            return
        missing = []
        if not self._futures_api_key:
            missing.append("BINANCE_API_KEY")
        if not self._futures_api_secret:
            missing.append("BINANCE_API_SECRET")
        if not self._spot_api_key:
            missing.append("BINANCE_SPOT_API_KEY")
        if not self._spot_api_secret:
            missing.append("BINANCE_SPOT_API_SECRET")
        if missing:
            raise StartupBlockedError(f"Missing required Binance credentials: {', '.join(missing)}")

    async def _db_write_probe(self) -> None:
        self.state_writer.set_stat("preflight_db_probe", time.time())

    async def _wait_for_heartbeat_ack_once(self, timeout_s: float = 5.0) -> bool:
        heartbeat_id = f"hb_{uuid.uuid4().hex[:12]}"
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", 9000)
        except Exception as exc:
            logger.warning("Preflight heartbeat could not connect to Rust TCP bridge: %s", exc)
            return False

        try:
            if not self.execution.send_heartbeat(heartbeat_id):
                return False
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                line = await asyncio.wait_for(reader.readline(), timeout=remaining)
                if not line:
                    return False
                try:
                    event = json.loads(line.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if event.get("event") == "HeartbeatAck" and event.get("heartbeat_id") == heartbeat_id:
                    self._on_heartbeat_ack(
                        heartbeat_id=event.get("heartbeat_id"),
                        status=event.get("status", ""),
                        ts_ms=event.get("ts_ms"),
                    )
                    return True
        except Exception as exc:
            logger.warning("Preflight heartbeat failed: %s", exc)
            return False
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        return False

    async def _cancel_open_orders(self, orders: list[dict], *, futures: bool) -> list[str]:
        failures: list[str] = []
        for order in orders:
            symbol = str(order.get("symbol", "")).upper()
            if not symbol:
                continue
            params: dict[str, str | int | float] = {"symbol": symbol}
            client_order_id = str(order.get("clientOrderId", "")).strip()
            if client_order_id:
                params["origClientOrderId"] = client_order_id
            elif order.get("orderId") is not None:
                params["orderId"] = int(order["orderId"])
            else:
                failures.append(symbol)
                continue
            try:
                await self._signed_delete_json(
                    base_url=self._futures_base_url if futures else self._spot_base_url,
                    endpoint="/fapi/v1/order" if futures else "/api/v3/order",
                    params=params,
                    api_key=self._futures_api_key if futures else self._spot_api_key,
                    api_secret=self._futures_api_secret if futures else self._spot_api_secret,
                )
            except Exception as exc:
                failures.append(f"{symbol}:{exc}")
        return failures

    async def _resolve_pending_intents_from_exchange(self, snapshot: dict) -> None:
        pending_rows = self.state_reader.get_pending_intents(
            statuses=["DISPATCHING", "PENDING_ACK", "TIMEOUT", "NEW", "FILLED"]
        )
        if not pending_rows:
            return

        futures_open_orders = snapshot.get("futures_open_orders") or []
        spot_open_orders = snapshot.get("spot_open_orders") or []
        open_order_symbols = {
            str(order.get("symbol", "")).upper()
            for order in list(futures_open_orders) + list(spot_open_orders)
            if isinstance(order, dict) and order.get("symbol")
        }
        position_symbols = {
            str(position.get("symbol", "")).upper()
            for position in snapshot.get("position_risk") or []
            if abs(_float_or_zero(position.get("positionAmt"))) > _POSITION_QTY_TOLERANCE
        }

        unresolved: list[str] = []
        for row in pending_rows:
            symbol = str(row.get("symbol", "")).upper()
            intent_type = str(row.get("intent_type", "")).upper()
            intent_id = str(row.get("intent_id", ""))
            if intent_type.startswith("ENTER"):
                if symbol in position_symbols or symbol not in open_order_symbols:
                    self.state_writer.delete_pending_intent(intent_id)
                else:
                    unresolved.append(f"{symbol}:{intent_type}")
            elif intent_type.startswith("EXIT"):
                if symbol not in position_symbols and symbol not in open_order_symbols:
                    self.state_writer.delete_pending_intent(intent_id)
                else:
                    unresolved.append(f"{symbol}:{intent_type}")
        if unresolved:
            raise StartupBlockedError(
                f"Unresolved pending intents after recovery: {', '.join(unresolved)}"
            )

    async def _run_preflight(self) -> None:
        self._preflight_status = "running"
        self._persist_runtime_state()
        try:
            await self._db_write_probe()
            self._validate_required_credentials()

            if self._trading_mode != "paper":
                await self._ping_exchange()
                await self._sync_binance_time()
                await self._signed_get_json(
                    base_url=self._futures_base_url,
                    endpoint="/fapi/v3/account",
                    api_key=self._futures_api_key,
                    api_secret=self._futures_api_secret,
                )
                await self._signed_get_json(
                    base_url=self._spot_base_url,
                    endpoint="/api/v3/account",
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                )

            if not await self._wait_for_heartbeat_ack_once():
                self._preflight_status = "blocked_execution_bridge"
                self._set_blocked_reason("execution bridge preflight failed")
                raise StartupBlockedError("Rust execution bridge preflight failed")

            if self._trading_mode != "paper":
                snapshot = await self._fetch_exchange_startup_snapshot()
                futures_open_orders = snapshot.get("futures_open_orders") or []
                spot_open_orders = snapshot.get("spot_open_orders") or []
                if futures_open_orders or spot_open_orders:
                    order_symbols = sorted(
                        {
                            str(order.get("symbol", "")).upper()
                            for order in list(futures_open_orders) + list(spot_open_orders)
                            if isinstance(order, dict) and order.get("symbol")
                        }
                    )
                    self._preflight_status = "blocked_open_orders"
                    self._set_blocked_reason(
                        "exchange has open orders at startup: " + ", ".join(order_symbols or ["unknown"])
                    )
                    raise StartupBlockedError("Startup blocked: exchange has open orders")
                await self._resolve_pending_intents_from_exchange(snapshot)

            self._preflight_status = "passed"
            self._persist_runtime_state()
        except StartupBlockedError:
            raise
        except Exception as exc:
            self._preflight_status = "blocked_preflight"
            self._set_blocked_reason(str(exc))
            raise StartupBlockedError(str(exc)) from exc

    def _direction_from_futures_position(self, position_amt: float, position_side: str) -> str:
        side = position_side.upper()
        if side == "SHORT":
            return "long"
        if side == "LONG":
            return "short"
        return "long" if position_amt < 0.0 else "short"

    def _build_spot_balance_map(self, spot_account: dict | None) -> dict[str, float]:
        if not isinstance(spot_account, dict):
            return {}
        balances: dict[str, float] = {}
        for balance in spot_account.get("balances", []):
            asset = str(balance.get("asset", "")).upper()
            total = _float_or_zero(balance.get("free")) + _float_or_zero(balance.get("locked"))
            if asset and total > _POSITION_QTY_TOLERANCE:
                balances[asset] = total
        return balances

    async def _fetch_exchange_startup_snapshot(self) -> dict:
        await self._sync_binance_time()
        futures_account, position_risk, futures_open_orders = await asyncio.gather(
            self._signed_get_json(
                base_url=self._futures_base_url,
                endpoint="/fapi/v3/account",
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
            ),
            self._signed_get_json(
                base_url=self._futures_base_url,
                endpoint="/fapi/v3/positionRisk",
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
            ),
            self._signed_get_json(
                base_url=self._futures_base_url,
                endpoint="/fapi/v1/openOrders",
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
            ),
        )

        spot_account = None
        spot_open_orders: list[dict] = []
        try:
            spot_account, spot_open_orders = await asyncio.gather(
                self._signed_get_json(
                    base_url=self._spot_base_url,
                    endpoint="/api/v3/account",
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                ),
                self._signed_get_json(
                    base_url=self._spot_base_url,
                    endpoint="/api/v3/openOrders",
                    api_key=self._spot_api_key,
                    api_secret=self._spot_api_secret,
                ),
            )
        except Exception as exc:
            logger.warning("Spot snapshot unavailable during startup reconciliation: %s", exc)

        funding_income: list[dict] = []
        try:
            funding_income = await self._signed_get_json(
                base_url=self._futures_base_url,
                endpoint="/fapi/v1/income",
                params={"incomeType": "FUNDING_FEE", "limit": 20},
                api_key=self._futures_api_key,
                api_secret=self._futures_api_secret,
            )
        except Exception as exc:
            logger.warning("Funding income snapshot unavailable during startup reconciliation: %s", exc)

        return {
            "futures_account": futures_account,
            "position_risk": position_risk,
            "futures_open_orders": futures_open_orders,
            "spot_account": spot_account,
            "spot_open_orders": spot_open_orders,
            "funding_income": funding_income,
        }

    @staticmethod
    def _percentile(values: list[float], percentile: float) -> float:
        if not values:
            return 0.0
        if len(values) == 1:
            return float(values[0])
        ordered = sorted(float(value) for value in values)
        rank = max(0.0, min(100.0, percentile)) / 100.0 * (len(ordered) - 1)
        lower = int(math.floor(rank))
        upper = int(math.ceil(rank))
        if lower == upper:
            return ordered[lower]
        weight = rank - lower
        return ordered[lower] * (1.0 - weight) + ordered[upper] * weight

    def _refresh_adaptive_state(self) -> None:
        recent_trades = self.state_reader.get_trades(limit=200)
        loss_streak = 0
        win_streak = 0
        for trade in recent_trades:
            pnl = _float_or_zero(trade.get("net_pnl_usd"))
            if pnl < 0.0 and win_streak == 0:
                loss_streak += 1
            elif pnl > 0.0 and loss_streak == 0:
                win_streak += 1
            else:
                break

        self._loss_streak = loss_streak
        self._win_streak = win_streak
        if self._loss_streak >= int(self._config.get("loss_streak_trigger")):
            self._streak_notional_scale = float(self._config.get("loss_streak_notional_scale"))
        else:
            self._streak_notional_scale = 1.0

        adaptive_enabled = self._adaptive_controls_enabled()
        adaptive_entry_base = float(self._config.get("entry_ann_funding_threshold"))
        adaptive_rotation_gap = ROTATION_MIN_GAP_ANN
        if adaptive_enabled:
            since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat()
            market_samples = self.state_reader.get_market_samples(since=since, limit=100_000)
            funding_values = [
                abs(_float_or_zero(sample.get("ann_funding")))
                for sample in market_samples
                if abs(_float_or_zero(sample.get("ann_funding"))) > 0.0
            ]
            if funding_values:
                adaptive_entry_base = self._percentile(funding_values, 75.0)
                if len(funding_values) >= 2:
                    adaptive_rotation_gap = max(
                        ROTATION_MIN_GAP_ANN,
                        pstdev(funding_values) * 1.5,
                    )

        self._adaptive_entry_threshold_base = adaptive_entry_base
        self._adaptive_rotation_gap = adaptive_rotation_gap
        self.state_writer.set_risk_snapshot(
            {
                "adaptive_entry_threshold_base": adaptive_entry_base,
                "adaptive_rotation_gap": adaptive_rotation_gap,
                "adaptive_controls_active": adaptive_enabled,
                "loss_streak": self._loss_streak,
                "win_streak": self._win_streak,
                "streak_notional_scale": self._streak_notional_scale,
            }
        )

    def _record_health_metric(
        self,
        *,
        metric: str,
        value: float,
        symbol: str | None = None,
        expected_value: float = 0.0,
        notes: str = "",
        sample_time: str | None = None,
    ) -> tuple[str, float | None]:
        recent_samples = self.state_reader.get_health_samples(
            metric=metric,
            symbol=symbol,
            since=(datetime.now(timezone.utc) - timedelta(days=14)).isoformat(),
            limit=2_000,
        )
        values = [
            _float_or_zero(sample.get("value"))
            for sample in recent_samples
            if sample.get("value") is not None
        ]
        zscore = None
        if len(values) >= 20:
            mean_value = fmean(values)
            sigma = pstdev(values)
            if sigma > 1e-9:
                zscore = abs(value - mean_value) / sigma

        alert_level = ""
        if zscore is not None and zscore >= float(self._config.get("health_safe_mode_zscore")):
            alert_level = "critical"
        elif zscore is not None and zscore >= float(self._config.get("health_alert_zscore")):
            alert_level = "warning"

        self.state_writer.record_health_sample(
            metric=metric,
            value=value,
            symbol=symbol,
            expected_value=expected_value,
            zscore=zscore,
            alert_level=alert_level,
            runtime_mode=self._runtime_mode,
            notes=notes,
            sample_time=sample_time,
        )
        return alert_level, zscore

    def _record_market_samples_for_minute(self, sample_minute: str) -> None:
        for symbol in self.monitored_symbols:
            minute_key, volume_usd = self._latest_volume_bar.get(symbol, ("", 0.0))
            self.state_writer.record_market_sample(
                symbol=symbol,
                sample_minute=sample_minute,
                ann_funding=self.funding_ranker.get_rate(symbol),
                basis_pct=_float_or_zero(self.depth_tracker.basis_pct(symbol)),
                mark_price=_float_or_zero(
                    self._mark_prices.get(symbol) or self.depth_tracker.perp_mid_price(symbol)
                ),
                minute_notional_volume=volume_usd if minute_key == sample_minute[:16] else 0.0,
            )

    def _record_operator_intervention(self, *, sample_time: str, notes: str, alert_level: str) -> None:
        self.state_writer.record_health_sample(
            metric="operator_intervention_required",
            value=1.0,
            expected_value=0.0,
            alert_level=alert_level,
            runtime_mode=self._runtime_mode,
            notes=notes,
            sample_time=sample_time,
        )

    def _maybe_record_validation_snapshot(self, now: datetime) -> None:
        interval_minutes = max(1, int(VALIDATION_SNAPSHOT_INTERVAL_MINUTES))
        bucket = int(now.timestamp() // (interval_minutes * 60))
        if bucket == self._last_validation_snapshot_bucket:
            return
        self._last_validation_snapshot_bucket = bucket

        metrics = calculate_metrics(self.state_reader)
        snapshot_time = now.replace(second=0, microsecond=0).isoformat()
        self.state_writer.record_validation_snapshot(
            snapshot_time=snapshot_time,
            validation_status=str(metrics.get("validation_status", "UNKNOWN")),
            go_no_go=str(metrics.get("go_no_go", "ADJUST")),
            observation_days=_float_or_zero(metrics.get("observation_days")),
            trade_count=int(metrics.get("trade_count", 0)),
            blockers=list(metrics.get("validation_blockers") or []),
            metrics=metrics,
        )
        self.state_writer.set_risk_snapshot(
            {
                "validation_status": metrics.get("validation_status", "UNKNOWN"),
                "validation_go_no_go": metrics.get("go_no_go", "ADJUST"),
                "validation_observation_days": metrics.get("observation_days", 0.0),
                "validation_intervention_free_days": metrics.get("intervention_free_days", 0.0),
                "validation_blockers": metrics.get("validation_blockers", []),
                "last_validation_snapshot_at": snapshot_time,
            }
        )

    async def _sample_exchange_health(self, sample_time: str) -> bool:
        if self._trading_mode == "paper":
            return False
        try:
            snapshot = await self._fetch_exchange_startup_snapshot()
        except Exception as exc:
            logger.warning("Exchange health sample failed: %s", exc)
            return False

        critical = False
        spot_balances = self._build_spot_balance_map(snapshot.get("spot_account"))
        for raw_position in snapshot.get("position_risk") or []:
            symbol = str(raw_position.get("symbol", "")).upper()
            position_amt = _float_or_zero(raw_position.get("positionAmt"))
            qty = abs(position_amt)
            if not symbol or qty <= _POSITION_QTY_TOLERANCE:
                continue
            direction = self._direction_from_futures_position(
                position_amt,
                str(raw_position.get("positionSide", "BOTH")),
            )
            if direction != "long":
                continue
            base_asset = _extract_base_asset(symbol)
            hedge_ratio = 0.0 if qty <= 0.0 else spot_balances.get(base_asset, 0.0) / qty
            alert_level, _ = self._record_health_metric(
                metric="hedge_ratio",
                value=hedge_ratio,
                symbol=symbol,
                expected_value=1.0,
                notes="exchange health sample",
                sample_time=sample_time,
            )
            if alert_level == "critical":
                critical = True
        return critical

    async def _record_runtime_health(self, sample_time: str) -> None:
        self._record_health_metric(
            metric="loop_alive",
            value=1.0,
            expected_value=1.0,
            notes="trader maintenance heartbeat",
            sample_time=sample_time,
        )

        critical_health_detected = False
        for position in self.state_reader.get_positions():
            symbol = str(position.get("symbol", "")).upper()
            if not symbol:
                continue
            alert_level, _ = self._record_health_metric(
                metric="slot_pnl_usd",
                value=_float_or_zero(position.get("net_pnl_usd")),
                symbol=symbol,
                expected_value=0.0,
                notes="open slot pnl",
                sample_time=sample_time,
            )
            if alert_level == "critical":
                critical_health_detected = True

        if (
            self._health_monitor_enabled()
            and self._trading_mode != "paper"
            and time.monotonic() - self._last_exchange_health_check_monotonic >= 300.0
        ):
            self._last_exchange_health_check_monotonic = time.monotonic()
            critical_health_detected = (
                await self._sample_exchange_health(sample_time) or critical_health_detected
            )

        self._set_safe_mode_flag(
            "health_monitor",
            self._health_monitor_enabled() and critical_health_detected,
        )

    async def _run_maintenance_loop(self) -> None:
        while not self._shutdown_event.is_set():
            funding_status = self.funding_ranker.status_snapshot()
            now = datetime.now(timezone.utc)
            sample_minute = now.replace(second=0, microsecond=0).isoformat()
            self._expire_stale_pending_intents()
            recent_execution_events = self.state_reader.get_execution_events_since(
                (now - timedelta(minutes=15)).isoformat(),
                limit=500,
            )
            recent_rejects = [
                event
                for event in recent_execution_events
                if str(event.get("status", "")).upper() in {"REJECTED", "EXPIRED", "CANCELED", "CANCELLED"}
            ]
            self.state_writer.set_risk_snapshot(
                {
                    **funding_status,
                    "loop_last_alive_at": now.isoformat(),
                    "recent_reject_count_15m": len(recent_rejects),
                    "recent_reject_symbols_15m": sorted(
                        {str(event.get("symbol", "")).upper() for event in recent_rejects if event.get("symbol")}
                    ),
                }
            )
            self._set_safe_mode_flag(
                "funding_stale",
                funding_status.get("funding_staleness_status") != "fresh",
            )
            self._set_safe_mode_flag(
                "rust_subscriber",
                self._preflight_status == "passed" and not self.subscriber.is_connected,
            )

            if sample_minute != self._last_sampled_minute:
                self._last_sampled_minute = sample_minute
                self._record_market_samples_for_minute(sample_minute)
                self._refresh_adaptive_state()
                await self._record_runtime_health(now.isoformat())

            current_date = now.date().isoformat()
            if current_date != self._last_retention_run_date:
                self._last_retention_run_date = current_date
                archive_counts = self.state_writer.archive_old_data(
                    retention_days=int(self._config.get("data_retention_days")),
                    market_retention_days=int(self._config.get("market_sample_retention_days")),
                    health_retention_days=int(self._config.get("health_sample_retention_days")),
                )
                self.state_writer.set_risk_snapshot(
                    {
                        "last_retention_run_at": now.isoformat(),
                        "last_retention_result": archive_counts,
                    }
                )

            self._maybe_record_validation_snapshot(now)
            self._persist_runtime_state()
            if await self._sleep_or_shutdown(5.0):
                break

    async def _run_heartbeat_loop(self) -> None:
        while not self._shutdown_event.is_set():
            heartbeat_id = f"hb_{uuid.uuid4().hex[:12]}"
            self._last_heartbeat_sent_monotonic = time.monotonic()
            sent = self.execution.send_heartbeat(heartbeat_id)
            interval = max(1, int(self._config.get("heartbeat_interval_seconds")))
            if await self._sleep_or_shutdown(interval):
                break

            missed = not sent or self._last_heartbeat_ack_id != heartbeat_id
            if missed:
                self._heartbeat_misses += 1
            else:
                self._heartbeat_misses = 0

            miss_threshold = max(1, int(self._config.get("heartbeat_miss_threshold")))
            if self._heartbeat_misses >= miss_threshold:
                self._set_safe_mode_flag("heartbeat_bridge", True)
                if (
                    not sent
                    and not self.subscriber.is_connected
                    and bool(self.state_reader.get_positions())
                ):
                    self._set_blocked_reason("execution bridge unavailable for exits")
            else:
                self._persist_runtime_state()

    async def shutdown(self, reason: str = "manual") -> None:
        if self._shutdown_started:
            return
        self._shutdown_started = True
        self._shutdown_event.set()
        shutdown_at = datetime.now(timezone.utc).isoformat()
        if reason == "manual" or reason.startswith("signal:"):
            self._record_operator_intervention(
                sample_time=shutdown_at,
                notes=f"shutdown:{reason}",
                alert_level="warning",
            )
        try:
            self.state_writer.set_risk_snapshot(
                {
                    "shutdown_reason": reason,
                    "shutdown_started_at": shutdown_at,
                    "allow_new_risk": False,
                }
            )
        except Exception:
            pass

        if self._trading_mode != "paper" and self._preflight_status == "passed":
            try:
                snapshot = await self._fetch_exchange_startup_snapshot()
                cancel_failures: list[str] = []
                cancel_failures.extend(
                    await self._cancel_open_orders(snapshot.get("futures_open_orders") or [], futures=True)
                )
                cancel_failures.extend(
                    await self._cancel_open_orders(snapshot.get("spot_open_orders") or [], futures=False)
                )
                self.state_writer.set_risk_snapshot(
                    {
                        "shutdown_order_cancelled_at": shutdown_at,
                        "shutdown_order_cancel_failures": cancel_failures,
                    }
                )
            except Exception as exc:
                logger.warning("Graceful shutdown order cancel failed: %s", exc)
                try:
                    self.state_writer.set_risk_snapshot(
                        {"shutdown_order_cancel_error": str(exc)[:300]}
                    )
                except Exception:
                    pass

        current_task = asyncio.current_task()
        tasks = [
            task
            for task in self._background_tasks
            if task is not current_task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._config.stop_watching()
        try:
            self.execution.close()
        except Exception:
            pass
        try:
            self.state_reader.close()
        except Exception:
            pass
        try:
            self.state_writer.close()
        except Exception:
            pass

    async def _reconcile_live_startup_state(self) -> None:
        snapshot = await self._fetch_exchange_startup_snapshot()
        futures_open_orders = snapshot.get("futures_open_orders") or []
        spot_open_orders = snapshot.get("spot_open_orders") or []
        all_open_orders = [
            order for order in list(futures_open_orders) + list(spot_open_orders)
            if isinstance(order, dict)
        ]
        if all_open_orders:
            order_symbols = sorted(
                {
                    str(order.get("symbol", "")).upper()
                    for order in all_open_orders
                    if order.get("symbol")
                }
            )
            self.state_writer.set_risk_snapshot(
                {
                    "startup_reconciliation_status": "blocked_open_orders",
                    "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                    "startup_reconciliation_open_order_symbols": order_symbols,
                    "startup_reconciliation_open_order_count": len(all_open_orders),
                    "allow_new_risk": False,
                    "reasons": [
                        "startup blocked: exchange still has open orders that are not locally tracked"
                    ],
                }
            )
            raise RuntimeError(
                f"Startup reconciliation blocked: exchange reported {len(all_open_orders)} open order(s)"
            )

        futures_account = snapshot["futures_account"]
        position_risk = snapshot.get("position_risk") or []
        spot_balances = self._build_spot_balance_map(snapshot.get("spot_account"))
        funding_income = snapshot.get("funding_income") or []
        local_positions = {row["symbol"]: row for row in self.state_reader.get_positions()}

        reconciled_symbols: set[str] = set()
        mismatched_symbols: list[str] = []
        hedge_gap_symbols: list[str] = []
        unsupported_direction_symbols: list[str] = []
        gross_exposure_usd = 0.0

        for raw_position in position_risk:
            symbol = str(raw_position.get("symbol", "")).upper()
            position_amt = _float_or_zero(raw_position.get("positionAmt"))
            qty = abs(position_amt)
            if not symbol or qty <= _POSITION_QTY_TOLERANCE:
                continue

            direction = self._direction_from_futures_position(
                position_amt,
                str(raw_position.get("positionSide", "BOTH")),
            )
            if direction != "long":
                unsupported_direction_symbols.append(symbol)
            entry_price = _float_or_zero(raw_position.get("breakEvenPrice"))
            if entry_price <= 0.0:
                entry_price = _float_or_zero(raw_position.get("entryPrice"))
            mark_price = _float_or_zero(raw_position.get("markPrice"))
            if entry_price <= 0.0:
                entry_price = mark_price

            side_label = (
                "SHORT_SPOT_LONG_PERP" if direction == "short" else "LONG_SPOT_SHORT_PERP"
            )
            updated_at = _iso_from_ms(raw_position.get("updateTime"))
            local_position = local_positions.get(symbol)
            if local_position is not None:
                local_qty = _float_or_zero(local_position.get("qty"))
                if (
                    local_position.get("direction") != direction
                    or abs(local_qty - qty) > _POSITION_QTY_TOLERANCE
                ):
                    mismatched_symbols.append(symbol)

            if direction == "long":
                base_asset = _extract_base_asset(symbol)
                spot_qty = spot_balances.get(base_asset, 0.0)
                if spot_qty + _POSITION_QTY_TOLERANCE < qty:
                    hedge_gap_symbols.append(symbol)

            current_ann_funding = self.funding_ranker.get_rate(symbol)
            self.state_writer.upsert_position(
                symbol=symbol,
                side=side_label,
                spot_entry=entry_price,
                perp_entry=entry_price,
                spot_live=mark_price,
                perp_live=mark_price,
                qty=qty,
                ann_funding=current_ann_funding,
                entry_ann_funding=current_ann_funding,
                net_pnl_usd=_float_or_zero(raw_position.get("unRealizedProfit")),
                status="OPEN",
                direction=direction,
                updated_at=updated_at,
            )
            self._entry_times[symbol] = updated_at
            self._position_directions[symbol] = direction
            reconciled_symbols.add(symbol)
            gross_exposure_usd += qty * max(mark_price, 0.0) * 2.0

        local_only_symbols = sorted(set(local_positions) - reconciled_symbols)
        for symbol in local_only_symbols:
            self.state_writer.remove_position(symbol)
            self._entry_times.pop(symbol, None)
            self._position_directions.pop(symbol, None)

        account_equity = _float_or_zero(
            futures_account.get("totalMarginBalance")
        ) or _float_or_zero(futures_account.get("totalWalletBalance"))
        available_balance = _float_or_zero(futures_account.get("availableBalance"))
        last_funding_fee = 0.0
        last_funding_fee_time = ""
        if funding_income:
            latest_income = max(
                funding_income,
                key=lambda item: int(_float_or_zero(item.get("time"))),
            )
            last_funding_fee = _float_or_zero(latest_income.get("income"))
            last_funding_fee_time = _iso_from_ms(latest_income.get("time"))

        self.state_writer.set_stat("account_equity", account_equity)
        self.state_writer.set_stat("gross_exposure", gross_exposure_usd)
        self.state_writer.set_stat(
            "max_gross_exposure",
            float(self._config.get("max_gross_exposure_usd")),
        )
        self.state_writer.set_risk_snapshot(
            {
                "account_equity": account_equity,
                "available_balance": available_balance,
                "startup_reconciliation_status": "ok",
                "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                "startup_reconciliation_position_count": len(reconciled_symbols),
                "startup_reconciliation_local_only_symbols": local_only_symbols,
                "startup_reconciliation_mismatched_symbols": sorted(mismatched_symbols),
                "startup_reconciliation_spot_hedge_gaps": sorted(hedge_gap_symbols),
                "startup_reconciliation_unsupported_directions": sorted(unsupported_direction_symbols),
                "startup_reconciliation_spot_assets": sorted(spot_balances),
                "startup_reconciliation_last_funding_fee": last_funding_fee,
                "startup_reconciliation_last_funding_fee_time": last_funding_fee_time,
                "allow_new_risk": not (mismatched_symbols or hedge_gap_symbols or unsupported_direction_symbols),
            }
        )
        logger.info(
            "Live startup reconciliation complete: %d exchange positions, %d stale local rows removed, %d mismatches, %d hedge gaps",
            len(reconciled_symbols),
            len(local_only_symbols),
            len(mismatched_symbols),
            len(hedge_gap_symbols),
        )
        blockers: list[str] = []
        if mismatched_symbols:
            blockers.append(f"symbol mismatch: {', '.join(sorted(mismatched_symbols))}")
        if hedge_gap_symbols:
            blockers.append(f"spot hedge gap: {', '.join(sorted(hedge_gap_symbols))}")
        if unsupported_direction_symbols:
            blockers.append(
                f"unsupported inverse/long-perp position: {', '.join(sorted(unsupported_direction_symbols))}"
            )
        if blockers:
            self.state_writer.set_risk_snapshot(
                {
                    "startup_reconciliation_status": "blocked_mismatch",
                    "startup_reconciliation_blockers": blockers,
                    "allow_new_risk": False,
                }
            )
            raise RuntimeError("Startup reconciliation blocked: " + "; ".join(blockers))

    async def _on_startup(self) -> None:
        """
        Phase 4: Smart startup - handles paper vs live mode correctly.
        
        Paper mode:  Clear all stale positions from local DB (fresh start)
        Live mode:   Sync positions from Binance API (true state from exchange)
        
        This prevents stale "OPEN" positions from previous runs affecting
        paper trading results.
        """
        import requests
        from datetime import datetime, timezone
        
        logger.info("="*50)
        logger.info("STARTUP MODE: %s", self._trading_mode.upper())
        logger.info("="*50)
        
        if self._trading_mode == "paper":
            # Paper mode: Clear all positions for fresh start
            logger.info("PAPER MODE: Clearing stale positions for fresh demo run...")
            self._pending_enters.clear()
            self._stale_pending_enters.clear()
            self._pending_exit_intents.clear()
            self._pending_exit_created_at.clear()
            self._exit_events.clear()
            for pending_intent in self.state_reader.get_pending_intents():
                self.state_writer.delete_pending_intent(str(pending_intent.get("intent_id")))
            
            # Get current positions
            positions = self.state_reader.get_positions()
            if positions:
                logger.info("Found %d stale positions to clear: %s", 
                           len(positions), [p.get('symbol') for p in positions])
                
                for pos in positions:
                    if pos.get('status') == 'OPEN':
                        self.state_writer.remove_position(pos['symbol'])
                        logger.info("  Cleared stale paper position: %s", pos['symbol'])
                
                logger.info("Paper mode startup complete - fresh start!")
            else:
                logger.info("No stale positions found - clean slate!")
            self.state_writer.set_risk_snapshot(
                {
                    "startup_reconciliation_status": "paper_reset",
                    "startup_reconciliation_time": datetime.now(timezone.utc).isoformat(),
                    "startup_reconciliation_position_count": 0,
                    "startup_reconciliation_spot_hedge_gaps": [],
                    "startup_reconciliation_mismatched_symbols": [],
                }
            )
                
        else:
            logger.info("%s MODE: Reconciling startup state against signed Binance account truth...", self._trading_mode.upper())
            await self._reconcile_live_startup_state()
            risk = self.state_reader.get_risk()
            hedge_gaps = list(risk.get("startup_reconciliation_spot_hedge_gaps") or [])
            mismatches = list(risk.get("startup_reconciliation_mismatched_symbols") or [])
            self._set_safe_mode_flag("hedge_gap", bool(hedge_gaps))
            self._set_safe_mode_flag("startup_mismatch", bool(mismatches))
        
        logger.info("="*50)

    async def _fetch_lot_step_sizes(self) -> None:
        """Fetch futures LOT_SIZE stepSize for all monitored symbols at startup.

        Rounds quantities to the exchange-mandated step size, preventing -1111
        (invalid quantity precision) order rejections on symbols like DOGEUSDT
        where stepSize=1.0 and PEPEUSDT where stepSize=1000.0.
        """
        try:
            resp = await asyncio.to_thread(
                requests.get,
                "https://fapi.binance.com/fapi/v1/exchangeInfo",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("Could not fetch exchange info for lot sizes: %s", exc)
            return

        for sym_info in data.get("symbols", []):
            symbol = sym_info.get("symbol", "")
            if not symbol:
                continue
            for f in sym_info.get("filters", []):
                if f.get("filterType") == "LOT_SIZE":
                    try:
                        self._lot_step[symbol] = float(f["stepSize"])
                    except (KeyError, ValueError):
                        pass
                    break

        logger.info("Lot step sizes loaded for %d symbols", len(self._lot_step))

    def _round_to_step(self, qty: float, step: float) -> float:
        """Round qty down to the nearest valid lot step size.

        Uses log10 to derive the correct number of decimal places:
          step=0.001 → 3 dp, step=1.0 → 0 dp, step=1000.0 → 0 dp.
        """
        if step <= 0:
            return qty
        rounded = (qty // step) * step
        decimals = max(0, -int(math.floor(math.log10(step))))
        return round(rounded, decimals)

    def _minutes_since_last_snapshot(self) -> float:
        """Return minutes elapsed since the most recent funding snapshot (0/8/16 UTC)."""
        now = datetime.now(timezone.utc)
        current_minutes = now.hour * 60 + now.minute
        snapshot_minutes = sorted(h * 60 for h in FUNDING_SNAPSHOT_HOURS)
        # Find the most recent snapshot that has already passed today
        elapsed = None
        for snap in reversed(snapshot_minutes):
            if current_minutes >= snap:
                elapsed = current_minutes - snap
                break
        if elapsed is None:
            # Past midnight but before first snapshot: measure from last snapshot of previous day
            elapsed = current_minutes + (24 * 60 - snapshot_minutes[-1])
        return float(elapsed)

    def _cost_depth_or_default(self, depth_usd: float) -> float:
        if depth_usd and depth_usd > 0.0:
            return depth_usd
        return _DEFAULT_COST_DEPTH_USD

    def _leg_mark_prices(self, symbol: str, row: dict | None = None) -> tuple[float, float]:
        row = row or {}

        spot_live = self.depth_tracker.spot_mid_price(symbol)
        if spot_live <= 0.0:
            spot_live = _float_or_zero(row.get("spot_live")) or _float_or_zero(row.get("spot_entry"))

        perp_live = _float_or_zero(self._mark_prices.get(symbol))
        if perp_live <= 0.0:
            perp_live = self.depth_tracker.perp_mid_price(symbol)
        if perp_live <= 0.0:
            perp_live = _float_or_zero(row.get("perp_live")) or _float_or_zero(row.get("perp_entry"))

        return spot_live, perp_live

    def _funding_has_decayed(self, direction: str, ann_funding: float) -> bool:
        exit_threshold = float(self._config.get("exit_ann_funding_threshold"))
        if direction == "short":
            return ann_funding <= 0.0
        return ann_funding < exit_threshold

    def _is_cycle_completion_event(
        self,
        execution_type: str | None,
        spot_fill_price,
        perp_fill_price,
    ) -> bool:
        execution_label = str(execution_type or "").upper()
        if execution_label in {"FILLED_CYCLE", "PAPER_FILL"}:
            return True
        return spot_fill_price is not None and perp_fill_price is not None

    def _next_intent_id(self, symbol: str, intent_type: str) -> str:
        return f"{intent_type.lower()}_{symbol.lower()}_{uuid.uuid4().hex[:12]}"

    def _persist_pending_intent(
        self,
        *,
        intent_id: str,
        symbol: str,
        intent_type: str,
        status: str,
        direction: str,
        quantity: float = 0.0,
        notional_usd: float = 0.0,
        client_order_id: str | None = None,
        retry_count: int = 0,
        last_error: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        self.state_writer.upsert_pending_intent(
            intent_id=intent_id,
            symbol=symbol,
            intent_type=intent_type,
            status=status,
            direction=direction,
            quantity=quantity,
            notional_usd=notional_usd,
            client_order_id=client_order_id,
            retry_count=retry_count,
            last_error=last_error,
            metadata=metadata,
        )

    def _resolve_pending_intent(self, intent_id: str | None) -> None:
        if not intent_id:
            return
        self.state_writer.delete_pending_intent(intent_id)

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _intent_timeout_seconds(self) -> float:
        try:
            return max(30.0, float(self._config.get("pending_intent_max_age_seconds")))
        except (TypeError, ValueError):
            return float(PENDING_INTENT_MAX_AGE_SECONDS)

    def _refresh_stale_pending_flag(self) -> None:
        stale_exit_count = 0
        timeout_s = self._intent_timeout_seconds()
        now = datetime.now(timezone.utc)
        for created_at in self._pending_exit_created_at.values():
            created_dt = self._parse_timestamp(created_at)
            if created_dt is None:
                continue
            if (now - created_dt).total_seconds() >= timeout_s:
                stale_exit_count += 1
        self._set_safe_mode_flag(
            "stale_pending_intent",
            bool(self._stale_pending_enters) or stale_exit_count > 0,
        )

    def _finalize_entry_fill(self, symbol: str, entry: dict, **fill_kwargs) -> None:
        def _float_or_none(value):
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def _pick_price(*candidates):
            for candidate in candidates:
                value = _float_or_none(candidate)
                if value is not None and value > 0.0:
                    return value
            return None

        direction = str(entry.get("direction", "long"))
        side_label = "SHORT_SPOT_LONG_PERP" if direction == "short" else "LONG_SPOT_SHORT_PERP"
        self._entry_times[symbol] = str(entry["entry_time"])
        self._position_directions[symbol] = direction
        self._estimated_entry_costs[symbol] = float(entry.get("estimated_entry_cost_usd", 0.0))
        spot_entry_price = _pick_price(
            fill_kwargs.get("spot_fill_price"),
            fill_kwargs.get("avg_fill_price"),
            fill_kwargs.get("last_fill_price"),
            entry.get("entry_price"),
        ) or float(entry["entry_price"])
        perp_entry_price = _pick_price(
            fill_kwargs.get("perp_fill_price"),
            fill_kwargs.get("avg_fill_price"),
            fill_kwargs.get("last_fill_price"),
            entry.get("entry_price"),
        ) or float(entry["entry_price"])
        self.state_writer.upsert_position(
            symbol=symbol,
            side=side_label,
            spot_entry=spot_entry_price,
            perp_entry=perp_entry_price,
            qty=float(entry["qty"]),
            ann_funding=_float_or_zero(entry.get("ann_funding")),
            entry_ann_funding=_float_or_zero(entry.get("ann_funding")),
            spot_live=spot_entry_price,
            perp_live=perp_entry_price,
            direction=direction,
            status="OPEN",
            updated_at=str(entry["entry_time"]),
        )
        logger.info(
            "Position opened for %s qty=%.5f spot=%.2f perp=%.2f (direction=%s)",
            symbol,
            float(entry["qty"]),
            spot_entry_price,
            perp_entry_price,
            direction,
        )
        self._resolve_pending_intent(str(entry.get("intent_id") or ""))

    def _handle_failed_order_update(self, symbol: str, status: str, **event_kwargs) -> None:
        terminal_status = status.upper()
        if terminal_status not in {"REJECTED", "CANCELED", "CANCELLED", "EXPIRED", "FAILED"}:
            return

        client_order_id = str(event_kwargs.get("client_order_id") or "")
        if symbol in self._pending_enters:
            entry = self._pending_enters.pop(symbol)
            self.state_writer.update_pending_intent(
                str(entry.get("intent_id") or ""),
                status=terminal_status,
                last_error=f"entry_{terminal_status.lower()}",
                client_order_id=client_order_id or None,
            )
            logger.error("Entry for %s failed with status %s", symbol, terminal_status)

        stale_entry = self._stale_pending_enters.pop(symbol, None)
        if stale_entry is not None:
            self.state_writer.update_pending_intent(
                str(stale_entry.get("intent_id") or ""),
                status=terminal_status,
                last_error=f"entry_{terminal_status.lower()}",
                client_order_id=client_order_id or None,
            )

        if symbol in self._pending_exit_intents:
            intent_id = self._pending_exit_intents.pop(symbol, None)
            self._pending_exit_created_at.pop(symbol, None)
            if intent_id:
                self.state_writer.update_pending_intent(
                    intent_id,
                    status=terminal_status,
                    last_error=f"exit_{terminal_status.lower()}",
                    client_order_id=client_order_id or None,
                )
            self._exit_events.pop(symbol, None)
            logger.critical(
                "Exit for %s failed with status %s; blocking new risk until reconciled",
                symbol,
                terminal_status,
            )
            self._set_safe_mode_flag("exit_failure", True)

        self._refresh_stale_pending_flag()

    def _expire_stale_pending_intents(self) -> None:
        timeout_s = self._intent_timeout_seconds()
        now = datetime.now(timezone.utc)
        stale_exit_symbols: list[str] = []

        for symbol, entry in list(self._pending_enters.items()):
            created_dt = self._parse_timestamp(str(entry.get("entry_time") or ""))
            if created_dt is None or (now - created_dt).total_seconds() < timeout_s:
                continue
            stale_entry = self._pending_enters.pop(symbol)
            stale_entry["timed_out_at"] = now.isoformat()
            self._stale_pending_enters[symbol] = stale_entry
            self.state_writer.update_pending_intent(
                str(stale_entry.get("intent_id") or ""),
                status="TIMEOUT",
                last_error="pending_enter_timeout",
            )
            logger.error(
                "Pending ENTER for %s timed out after %.0fs; symbol remains blocked until a terminal update arrives",
                symbol,
                timeout_s,
            )

        for symbol, intent_id in list(self._pending_exit_intents.items()):
            created_dt = self._parse_timestamp(self._pending_exit_created_at.get(symbol))
            if created_dt is None or (now - created_dt).total_seconds() < timeout_s:
                continue
            stale_exit_symbols.append(symbol)
            self.state_writer.update_pending_intent(
                intent_id,
                status="TIMEOUT",
                last_error="pending_exit_timeout",
            )
            logger.critical(
                "Pending EXIT for %s is older than %.0fs; trading remains in safe mode until it resolves",
                symbol,
                timeout_s,
            )

        self.state_writer.set_risk_snapshot(
            {
                "stale_pending_enter_symbols": sorted(self._stale_pending_enters.keys()),
                "stale_pending_exit_symbols": sorted(stale_exit_symbols),
            }
        )
        self._refresh_stale_pending_flag()

    def _current_risk_limits(self) -> RiskLimits:
        return RiskLimits(
            max_gross_exposure_usd=float(self._config.get("max_gross_exposure_usd")),
            max_symbol_concentration=MAX_SYMBOL_CONCENTRATION,
            soft_drawdown_pct=float(self._config.get("soft_drawdown_pct")),
            max_drawdown_pct=float(self._config.get("max_drawdown_pct")),
            max_data_staleness_minutes=MAX_ALLOWED_GAP_MINUTES,
            max_latency_ms=int(self._config.get("max_venue_latency_ms")),
            max_consecutive_losses=max(1, int(self._config.get("loss_streak_trigger"))),
        )

    def _estimate_account_equity(self, rows: list[dict]) -> float:
        if self._trading_mode != "paper":
            exchange_equity = self.state_reader.get_account_equity()
            if exchange_equity is not None and exchange_equity > 0.0:
                return exchange_equity

        starting_equity = float(self._config.get("account_equity_usd"))
        realized_pnl = sum(_float_or_zero(trade.get("net_pnl_usd")) for trade in self.state_reader.get_trades(limit=5_000))
        open_pnl = sum(_float_or_zero(row.get("net_pnl_usd")) for row in rows)
        return starting_equity + realized_pnl + open_pnl

    def _estimate_data_staleness_minutes(self, rows: list[dict]) -> int:
        funding_status = self.funding_ranker.status_snapshot()
        funding_age_minutes = int(max(0.0, _float_or_zero(funding_status.get("funding_last_refresh_age_s"))) // 60)
        tracked_symbols = [str(row.get("symbol", "")) for row in rows if row.get("symbol")] or list(self.monitored_symbols)
        latest_mark_age_s = 0.0
        now_monotonic = time.monotonic()
        for symbol in tracked_symbols:
            updated_at = self._mark_price_updated_monotonic.get(symbol)
            if updated_at is None:
                latest_mark_age_s = max(latest_mark_age_s, float(MAX_ALLOWED_GAP_MINUTES * 60))
                continue
            latest_mark_age_s = max(latest_mark_age_s, max(0.0, now_monotonic - updated_at))
        mark_age_minutes = int(latest_mark_age_s // 60)
        return max(funding_age_minutes, mark_age_minutes)

    def _evaluate_risk_controls(self, rows: list[dict]) -> RiskDecision:
        gross_by_symbol: dict[str, float] = {}
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            qty = _float_or_zero(row.get("qty"))
            if not symbol or qty <= 0.0:
                continue
            spot_live = _float_or_zero(row.get("spot_live")) or _float_or_zero(row.get("spot_entry"))
            perp_live = _float_or_zero(row.get("perp_live")) or _float_or_zero(row.get("perp_entry"))
            leg_price = max(spot_live, perp_live, 0.0)
            gross_by_symbol[symbol] = qty * leg_price * 2.0

        gross_exposure = sum(gross_by_symbol.values())
        symbol_concentration = (
            max(gross_by_symbol.values(), default=0.0) / gross_exposure
            if gross_exposure > 0.0
            else 0.0
        )
        account_equity = self._estimate_account_equity(rows)
        if account_equity > self._peak_account_equity:
            self._peak_account_equity = account_equity
        drawdown_pct = (
            max(0.0, (self._peak_account_equity - account_equity) / self._peak_account_equity)
            if self._peak_account_equity > 0.0
            else 0.0
        )
        venue_latency_ms = int(
            max(0, self._heartbeat_misses) * int(self._config.get("heartbeat_interval_seconds")) * 1000
        )

        self._risk_engine.limits = self._current_risk_limits()
        decision = self._risk_engine.evaluate(
            RiskState(
                gross_exposure_usd=gross_exposure,
                symbol_concentration=symbol_concentration,
                drawdown_pct=drawdown_pct,
                data_staleness_minutes=self._estimate_data_staleness_minutes(rows),
                venue_latency_ms=venue_latency_ms,
                consecutive_losses=self._loss_streak,
            )
        )

        self._risk_allow_new_risk = decision.allow_new_risk
        self._risk_derisk_required = decision.derisk_required
        self._risk_kill_switch = decision.kill_switch
        self._risk_position_scale = decision.position_scale
        self._risk_reasons = list(decision.reasons)
        self._risk_last_evaluated_at = datetime.now(timezone.utc).isoformat()

        self.state_writer.set_stat("account_equity", account_equity)
        self.state_writer.set_stat("gross_exposure", gross_exposure)
        self.state_writer.set_risk_snapshot(
            {
                "account_equity": account_equity,
                "account_equity_high_watermark": self._peak_account_equity,
                "gross_exposure": gross_exposure,
                "symbol_concentration": symbol_concentration,
                "drawdown_pct": drawdown_pct,
                "venue_latency_ms": venue_latency_ms,
                "kill_switch": decision.kill_switch,
                "risk_reasons": decision.reasons,
            }
        )
        self._set_safe_mode_flag("risk_limits", decision.derisk_required or decision.kill_switch)
        return decision

    def _on_depth_update(self, symbol: str, market: str, bids: list, asks: list) -> None:
        """Update depth cache; capture top perp bid as mark price proxy."""
        self.depth_tracker.on_l2depth(symbol, market, bids, asks)
        self.regime_filter.on_depth_update(symbol)
        # Note: mark prices are now primarily set via _on_mark_price from MarkPrice WS events.
        # This depth-based fallback is kept for robustness if MarkPrice stream is delayed.

    def _on_mark_price(self, symbol: str, mark_price: float, next_funding_rate: float) -> None:
        """Update FundingRanker with live WS funding rate (~1s cadence).

        This provides sub-minute rate resolution compared to the 60s REST fallback,
        enabling the post-snapshot decay exit and rotation logic to react immediately
        when funding collapses at settlement rather than waiting for the next REST poll.
        """
        self.funding_ranker.update_rate(symbol, next_funding_rate)
        self.predictor.push_sample(symbol, next_funding_rate * 1095)
        self.regime_filter.on_mark_price(symbol, mark_price, next_funding_rate * 1095.0)
        # Also keep mark price cache fresh for ENTER quantity calculations.
        if mark_price > 0.0:
            self._mark_prices[symbol] = mark_price
            self._mark_price_ready.add(symbol)
            self._mark_price_updated_monotonic[symbol] = time.monotonic()

    def _on_heartbeat_ack(self, heartbeat_id: str | None, status: str, ts_ms=None) -> None:
        if str(status).lower() != "ok":
            return
        self._last_heartbeat_ack_monotonic = time.monotonic()
        self._heartbeat_misses = 0
        self._last_heartbeat_ack_id = str(heartbeat_id or "")
        self._last_heartbeat_ack_at = _iso_from_ms(ts_ms)
        self._set_safe_mode_flag("heartbeat_bridge", False)

    def _on_volume_bar(self, symbol: str, minute_start_ms, notional_usd: float) -> None:
        minute_iso = _iso_from_ms(minute_start_ms)
        self._latest_volume_bar[symbol] = (minute_iso[:16], _float_or_zero(notional_usd))
        self.regime_filter.on_volume_bar(symbol, _float_or_zero(notional_usd))

    def _external_entry_block_reason(self) -> str | None:
        if self._runtime_mode == "BLOCKED":
            return f"blocked: {self._blocked_reason or 'unknown'}"
        if self._runtime_mode == "SAFE_MODE":
            return f"safe mode: {self._safe_mode_reason() or 'operator guard'}"
        if self._risk_kill_switch:
            return "kill switch active"
        if not self._risk_allow_new_risk:
            return "risk engine blocked new exposure"
        risk_state = self.state_reader.get_risk()
        if risk_state.get("kill_switch") or risk_state.get("is_kill_switch"):
            return "kill switch active"
        if risk_state.get("allow_new_risk") is False:
            return "allow_new_risk=false"
        return None

    def _refresh_open_position_metrics(self, rows: list[dict] | None = None) -> list[dict]:
        rows = rows if rows is not None else self.state_reader.get_positions()
        for row in rows:
            symbol = str(row.get("symbol", ""))
            if not symbol:
                continue

            ann_funding = self.funding_ranker.get_rate(symbol)
            spot_live, perp_live = self._leg_mark_prices(symbol, row)

            qty = _float_or_zero(row.get("qty"))
            spot_entry = _float_or_zero(row.get("spot_entry"))
            perp_entry = _float_or_zero(row.get("perp_entry"))
            direction = str(row.get("direction", "long"))
            net_pnl_usd = _float_or_zero(row.get("net_pnl_usd"))

            if qty > 0.0 and spot_live > 0.0 and perp_live > 0.0:
                if direction == "short":
                    spot_pnl = (spot_entry - spot_live) * qty
                    perp_pnl = (perp_live - perp_entry) * qty
                else:
                    spot_pnl = (spot_live - spot_entry) * qty
                    perp_pnl = (perp_entry - perp_live) * qty
                net_pnl_usd = spot_pnl + perp_pnl

            row["ann_funding"] = ann_funding
            row["spot_live"] = spot_live
            row["perp_live"] = perp_live
            row["net_pnl_usd"] = net_pnl_usd

            self.state_writer.update_position_metrics(
                symbol,
                ann_funding=ann_funding,
                spot_live=spot_live,
                perp_live=perp_live,
                net_pnl_usd=net_pnl_usd,
            )

        return rows

    async def _sync_rest_depth_to_tracker(self) -> None:
        """Sync REST fallback depth to the main depth tracker.
        
        This ensures we have depth data even when WebSocket depth isn't flowing.
        """
        updated_count = 0
        for symbol in self.monitored_symbols:
            spot_depth = self.rest_depth_fetcher._spot_depths.get(symbol, 0.0)
            perp_depth = self.rest_depth_fetcher._perp_depths.get(symbol, 0.0)
            # Only update if REST has fresh data
            if self.rest_depth_fetcher.has_fresh_depth(symbol) and (spot_depth > 0 or perp_depth > 0):
                self.depth_tracker.set_rest_depth(symbol, spot_depth, perp_depth)
                updated_count += 1
        if updated_count > 0:
            logger.debug("Synced REST depth for %d symbols to tracker", updated_count)

    def _calculate_trade_pnl(
        self,
        *,
        qty: float,
        direction: str,
        ann_funding: float,
        hold_hours: float,
        execution_cost_usd: float = 0.0,
        entry_price: float | None = None,
        exit_price: float | None = None,
        spot_entry_price: float | None = None,
        perp_entry_price: float | None = None,
        spot_exit_price: float | None = None,
        perp_exit_price: float | None = None,
    ) -> tuple[float, float, float]:
        """Calculate net PnL and funding collected for a funding arbitrage trade.

        For delta-neutral funding arbitrage:
        - The spot and perp positions offset each other, minimizing directional risk
        - Main profit comes from funding payments collected
        - For LONG (long spot + short perp): we receive positive funding
        - For SHORT (short spot + long perp): we receive funding when ann_funding < 0

        Returns: (net_pnl_usd, funding_collected, basis_pnl_usd)
        """
        spot_entry = _float_or_zero(spot_entry_price) or _float_or_zero(entry_price)
        perp_entry = _float_or_zero(perp_entry_price) or _float_or_zero(entry_price)
        spot_exit = _float_or_zero(spot_exit_price) or _float_or_zero(exit_price)
        perp_exit = _float_or_zero(perp_exit_price) or _float_or_zero(exit_price)
        if min(spot_entry, perp_entry, spot_exit, perp_exit) <= 0.0 or qty <= 0:
            return 0.0, 0.0, 0.0

        direction = str(direction or "long").lower()
        if direction == "short":
            basis_pnl = ((spot_entry - spot_exit) + (perp_exit - perp_entry)) * qty
            signed_ann_funding = -ann_funding
        else:
            basis_pnl = ((spot_exit - spot_entry) + (perp_entry - perp_exit)) * qty
            signed_ann_funding = ann_funding

        # Funding collected proportional to time held
        # ann_funding is annualized, so pro-rate it by the fraction of a year held.
        funding_periods = max(hold_hours, 0.0) / FUNDING_INTERVAL_HOURS
        notional_usd = ((spot_entry + perp_entry) / 2.0) * qty
        funding_collected = (
            signed_ann_funding
            * (funding_periods / FUNDING_PERIODS_PER_YEAR)
            * notional_usd
        )

        net_pnl = basis_pnl + funding_collected - execution_cost_usd

        return net_pnl, funding_collected, basis_pnl

    def _on_order_update(self, symbol: str, status: str, filled_qty: float = 0.0, **_kwargs) -> None:
        def _float_or_none(value):
            if value is None:
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                return None

        def _pick_price(*candidates):
            for candidate in candidates:
                value = _float_or_none(candidate)
                if value is not None and value > 0.0:
                    return value
            return None

        event_payload = {
            "symbol": symbol,
            "status": status,
            "filled_qty": filled_qty,
            "client_order_id": _kwargs.get("client_order_id", ""),
            "avg_fill_price": _kwargs.get("avg_fill_price"),
            "last_fill_price": _kwargs.get("last_fill_price"),
            "cumulative_quote_qty": _kwargs.get("cumulative_quote_qty"),
            "commission": _kwargs.get("commission"),
            "commission_asset": _kwargs.get("commission_asset"),
            "realized_pnl": _kwargs.get("realized_pnl"),
            "maker": _kwargs.get("maker"),
            "execution_type": _kwargs.get("execution_type"),
            "spot_fill_price": _kwargs.get("spot_fill_price"),
            "perp_fill_price": _kwargs.get("perp_fill_price"),
        }
        self.state_writer.record_execution_event(event_payload)

        client_order_id = str(_kwargs.get("client_order_id") or "")
        pending_enter = self._pending_enters.get(symbol)
        if pending_enter is not None and client_order_id:
            self.state_writer.update_pending_intent(
                str(pending_enter.get("intent_id") or ""),
                client_order_id=client_order_id,
                status=status,
            )
        stale_pending_enter = self._stale_pending_enters.get(symbol)
        if stale_pending_enter is not None and client_order_id:
            self.state_writer.update_pending_intent(
                str(stale_pending_enter.get("intent_id") or ""),
                client_order_id=client_order_id,
                status=status,
            )
        pending_exit_intent_id = self._pending_exit_intents.get(symbol)
        if pending_exit_intent_id is not None and client_order_id:
            self.state_writer.update_pending_intent(
                pending_exit_intent_id,
                client_order_id=client_order_id,
                status=status,
            )

        self._handle_failed_order_update(symbol, status, **_kwargs)
        if status != "FILLED":
            return

        is_cycle_complete = self._is_cycle_completion_event(
            _kwargs.get("execution_type"),
            _kwargs.get("spot_fill_price"),
            _kwargs.get("perp_fill_price"),
        )
        if (
            symbol in self._exit_events
            or symbol in self._pending_enters
            or symbol in self._stale_pending_enters
        ) and not is_cycle_complete:
            logger.debug(
                "Ignoring leg-level FILLED for %s until hedge cycle completes (execution_type=%s)",
                symbol,
                _kwargs.get("execution_type"),
            )
            return

        # ── Exit fill ──────────────────────────────────────────────────────────
        if symbol in self._exit_events:
            logger.info("Exit FILLED confirmed for %s — releasing capital slot", symbol)
            positions = self.state_reader.get_positions()
            pos = next((p for p in positions if p["symbol"] == symbol), None)
            if pos:
                spot_entry_price = _float_or_zero(pos.get("spot_entry"))
                perp_entry_price = _float_or_zero(pos.get("perp_entry")) or spot_entry_price
                fallback_spot_price, fallback_perp_price = self._leg_mark_prices(symbol, pos)
                spot_exit_price = _pick_price(
                    _kwargs.get("spot_fill_price"),
                    fallback_spot_price,
                    pos.get("spot_live"),
                    spot_entry_price,
                )
                perp_exit_price = _pick_price(
                    _kwargs.get("perp_fill_price"),
                    _kwargs.get("avg_fill_price"),
                    _kwargs.get("last_fill_price"),
                    fallback_perp_price,
                    pos.get("perp_live"),
                    perp_entry_price,
                )
                if spot_exit_price is None:
                    spot_exit_price = spot_entry_price
                    logger.warning("No spot exit price available for %s, using spot entry", symbol)
                if perp_exit_price is None:
                    perp_exit_price = perp_entry_price
                    logger.warning("No perp exit price available for %s, using perp entry", symbol)

                direction = pos.get("direction", "long")
                side_label = "SHORT_SPOT_LONG_PERP" if direction == "short" else "LONG_SPOT_SHORT_PERP"
                entry_time_str = self._entry_times.pop(symbol, pos.get("updated_at", ""))
                exit_time = datetime.now(timezone.utc).isoformat()

                # Calculate hold duration for funding pro-rata
                try:
                    entry_dt = datetime.fromisoformat(entry_time_str.replace("Z", "+00:00"))
                    if entry_dt.tzinfo is None:
                        entry_dt = entry_dt.replace(tzinfo=timezone.utc)
                    hold_hours = (datetime.now(timezone.utc) - entry_dt).total_seconds() / 3600
                except (ValueError, TypeError):
                    hold_hours = 0.0
                    logger.warning("Could not parse entry time for %s, defaulting hold_hours=0", symbol)

                entry_ann_funding = _float_or_zero(pos.get("entry_ann_funding"))
                ann_funding = entry_ann_funding or _float_or_zero(pos.get("ann_funding"))
                qty = pos["qty"]
                entry_notional_usd = ((spot_entry_price + perp_entry_price) / 2.0) * qty
                estimated_entry_cost_usd = self._estimated_entry_costs.pop(symbol, 0.0)
                if estimated_entry_cost_usd <= 0.0 and entry_notional_usd > 0.0:
                    estimated_entry_cost_usd = blended_entry_cost(
                        entry_notional_usd,
                        depth_usd=_DEFAULT_COST_DEPTH_USD,
                    )

                actual_execution_cost_usd = self.state_reader.estimate_trade_execution_cost(
                    symbol,
                    entry_time_str,
                    exit_time,
                )
                execution_cost_usd = actual_execution_cost_usd
                estimated_total_cost_usd = estimated_entry_cost_usd
                if execution_cost_usd <= 0.0:
                    exit_notional_usd = ((spot_exit_price + perp_exit_price) / 2.0) * qty
                    estimated_exit_cost_usd = blended_exit_cost(
                        exit_notional_usd,
                        depth_usd=self._cost_depth_or_default(self.depth_tracker.get_exit_depth(symbol)),
                    )
                    estimated_total_cost_usd += estimated_exit_cost_usd
                    execution_cost_usd = estimated_entry_cost_usd + estimated_exit_cost_usd
                else:
                    exit_notional_usd = ((spot_exit_price + perp_exit_price) / 2.0) * qty
                    estimated_exit_cost_usd = blended_exit_cost(
                        exit_notional_usd,
                        depth_usd=self._cost_depth_or_default(self.depth_tracker.get_exit_depth(symbol)),
                    )
                    estimated_total_cost_usd += estimated_exit_cost_usd

                if estimated_total_cost_usd > 0.0:
                    cost_model_error_pct = (
                        abs(execution_cost_usd - estimated_total_cost_usd)
                        / estimated_total_cost_usd
                    ) * 100.0
                    self._record_health_metric(
                        metric="cost_model_error_pct",
                        value=cost_model_error_pct,
                        symbol=symbol,
                        expected_value=0.0,
                        notes="trade execution cost reconciliation",
                        sample_time=exit_time,
                    )

                # Calculate PnL properly
                net_pnl, funding_collected, basis_pnl_usd = self._calculate_trade_pnl(
                    qty=qty,
                    direction=direction,
                    ann_funding=ann_funding,
                    hold_hours=max(hold_hours, 0.0),
                    execution_cost_usd=execution_cost_usd,
                    spot_entry_price=spot_entry_price,
                    perp_entry_price=perp_entry_price,
                    spot_exit_price=spot_exit_price,
                    perp_exit_price=perp_exit_price,
                )

                trade = Trade(
                    symbol=symbol,
                    side=side_label,
                    entry_time=entry_time_str,
                    exit_time=exit_time,
                    entry_price=(spot_entry_price + perp_entry_price) / 2.0,
                    exit_price=(spot_exit_price + perp_exit_price) / 2.0,
                    qty=qty,
                    net_pnl_usd=net_pnl,
                    funding_collected=funding_collected,
                    execution_cost_usd=execution_cost_usd,
                    basis_pnl_usd=basis_pnl_usd,
                )
                self.state_writer.record_trade(trade)
                self.state_writer.remove_position(symbol)
                self._position_directions.pop(symbol, None)
                logger.info(
                    "Trade recorded for %s pnl=$%.4f funding=$%.4f basis=$%.4f exec_cost=$%.4f hold_h=%.2f",
                    symbol, net_pnl, funding_collected, basis_pnl_usd, execution_cost_usd, hold_hours,
                )
            else:
                logger.warning("Exit FILLED for %s but no position in DB to record", symbol)
                self._entry_times.pop(symbol, None)
                self._estimated_entry_costs.pop(symbol, None)
            event = self._exit_events.pop(symbol, None)
            if event is not None:
                event.set()
            self._pending_exit_created_at.pop(symbol, None)
            self._resolve_pending_intent(self._pending_exit_intents.pop(symbol, None))
            self._set_safe_mode_flag("exit_failure", False)
            self._refresh_stale_pending_flag()

        # ── Entry fill ─────────────────────────────────────────────────────────
        elif symbol in self._pending_enters:
            entry = self._pending_enters.pop(symbol)
            self._finalize_entry_fill(symbol, entry, **_kwargs)
            self._refresh_stale_pending_flag()
        elif symbol in self._stale_pending_enters:
            entry = self._stale_pending_enters.pop(symbol)
            logger.critical(
                "Late FILLED arrived for %s after entry timeout; position will be recorded and trading stays in safe mode",
                symbol,
            )
            self._finalize_entry_fill(symbol, entry, **_kwargs)
            self._set_safe_mode_flag("late_entry_fill", True)
            self._refresh_stale_pending_flag()

    def _get_open_positions(self, rows: list[dict] | None = None) -> list[OpenPosition]:
        rows = rows if rows is not None else self.state_reader.get_positions()
        positions = []
        for r in rows:
            spot_price = r.get("spot_live", 0.0)
            # If spot_live is populated (price > $1), use actual qty × price.
            # Otherwise fall back to configured slot size (e.g., cold start with stale cache).
            if spot_price > 1.0:
                notional_usd = r["qty"] * spot_price
            else:
                notional_usd = CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE
            positions.append(OpenPosition(
                symbol=r["symbol"],
                notional_usd=notional_usd,
                ann_funding=self.funding_ranker.get_rate(r["symbol"]),
            ))
            # Cache direction for use by exit dispatches
            self._position_directions[r["symbol"]] = r.get("direction", "long")
        return positions

    def _dispatch_exit(self, symbol: str, urgency: float = 0.8, direction: str = "long") -> asyncio.Event:
        """Send EXIT instruction and return an Event that fires when FILLED.

        If the ZMQ send fails (Rust engine down), the event is registered but
        will never be set — callers rely on ROTATION_CONFIRM_TIMEOUT_S to unblock.
        The CRITICAL log from ExecutionClient is the alert signal.
        """
        event = asyncio.Event()
        position = next(
            (row for row in self.state_reader.get_positions() if str(row.get("symbol", "")).upper() == symbol.upper()),
            None,
        )
        qty = _float_or_zero(position.get("qty")) if position is not None else 0.0
        if qty <= 0.0:
            logger.critical("Refusing to dispatch EXIT for %s without a known position quantity", symbol)
            self._set_safe_mode_flag("exit_failure", True)
            return event

        self._exit_events[symbol] = event
        intent = "EXIT_SHORT" if direction == "short" else "EXIT_LONG"
        intent_id = self._next_intent_id(symbol, intent)
        created_at = datetime.now(timezone.utc).isoformat()
        self._persist_pending_intent(
            intent_id=intent_id,
            symbol=symbol,
            intent_type=intent,
            status="DISPATCHING",
            direction=direction,
            quantity=qty,
            metadata={"urgency": urgency, "quantity": qty, "created_at": created_at},
        )
        sent = self.execution.send_order_intent({
            "symbol": symbol,
            "intent": intent,
            "quantity": qty,
            "urgency": urgency,
            "max_slippage_bps": 20.0 if urgency >= 1.0 else 5.0,
            "exposure_scale": 1.0,
            "intent_id": intent_id,
        })
        if sent:
            logger.info(
                "EXIT dispatched for %s qty=%.5f (urgency=%.1f, direction=%s)",
                symbol,
                qty,
                urgency,
                direction,
            )
            self._pending_exit_intents[symbol] = intent_id
            self._pending_exit_created_at[symbol] = created_at
            self.state_writer.update_pending_intent(intent_id, status="PENDING_ACK")
        else:
            logger.critical("EXIT for %s NOT sent — ZMQ down. Position unhedged!", symbol)
            self.state_writer.update_pending_intent(
                intent_id,
                status="FAILED",
                retry_count=1,
                last_error="zmq_send_timeout",
            )
            self._set_safe_mode_flag("execution_bridge", True)
            self._exit_events.pop(symbol, None)
        return event

    def _dispatch_enter(
        self,
        symbol: str,
        notional_usd: float,
        direction: str = "long",
        ann_funding: float | None = None,
    ) -> None:
        """Send ENTER instruction. Skips if no mark price has been received yet."""
        if symbol in self._stale_pending_enters:
            logger.warning(
                "Skipping ENTER for %s because a previous entry attempt timed out and has not been reconciled",
                symbol,
            )
            return
        mark_price = self._mark_prices.get(symbol, 0.0)
        if mark_price <= 0.0:
            logger.warning(
                "No mark price for %s yet — skipping ENTER (will retry next cycle)", symbol
            )
            return
        raw_qty = notional_usd / mark_price
        step = self._lot_step.get(symbol, 1e-5)
        qty = self._round_to_step(raw_qty, step)
        if qty <= 0.0:
            logger.warning(
                "Rounded quantity for %s is 0 (raw=%.8f, step=%s) — skipping ENTER",
                symbol,
                raw_qty,
                step,
            )
            return
        intent = "ENTER_SHORT" if direction == "short" else "ENTER_LONG"
        intent_id = self._next_intent_id(symbol, intent)
        entry_depth_usd = self._cost_depth_or_default(self.depth_tracker.get_entry_depth(symbol))
        entry_metadata = {
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "entry_price": mark_price,
            "qty": qty,
            "direction": direction,
            "ann_funding": self.funding_ranker.get_rate(symbol) if ann_funding is None else ann_funding,
            "estimated_entry_cost_usd": blended_entry_cost(notional_usd, depth_usd=entry_depth_usd),
            "intent_id": intent_id,
        }
        self._persist_pending_intent(
            intent_id=intent_id,
            symbol=symbol,
            intent_type=intent,
            status="DISPATCHING",
            direction=direction,
            quantity=qty,
            notional_usd=notional_usd,
            metadata=entry_metadata,
        )
        sent = self.execution.send_order_intent({
            "symbol": symbol,
            "intent": intent,
            "quantity": qty,
            "urgency": 0.8,
            "max_slippage_bps": 5.0,
            "exposure_scale": 1.0,
            "intent_id": intent_id,
        })
        if sent:
            logger.info("ENTER dispatched for %s qty=%.5f (notional=$%.0f, price=$%.2f, direction=%s)",
                        symbol, qty, notional_usd, mark_price, direction)
            self._pending_enters[symbol] = dict(entry_metadata)
            self.state_writer.update_pending_intent(intent_id, status="PENDING_ACK")
        else:
            logger.critical("ENTER for %s NOT sent — ZMQ down.", symbol)
            self.state_writer.update_pending_intent(
                intent_id,
                status="FAILED",
                retry_count=1,
                last_error="zmq_send_timeout",
            )
            self._set_safe_mode_flag("execution_bridge", True)
            return

    async def _await_exit_confirmation(self, symbol: str) -> bool:
        """Wait for FILLED event. Returns True if confirmed, False on timeout."""
        event = self._exit_events.get(symbol)
        if event is None:
            return False
        try:
            await asyncio.wait_for(event.wait(), timeout=ROTATION_CONFIRM_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            logger.warning("Exit confirmation timeout for %s — entry will be deferred", symbol)
            pending_intent_id = self._pending_exit_intents.get(symbol)
            if pending_intent_id:
                self.state_writer.update_pending_intent(
                    pending_intent_id,
                    status="TIMEOUT",
                    last_error="exit_confirmation_timeout",
                )
            return False

    async def _maybe_recompound(self) -> None:
        import time
        if time.time() - self._last_compound_check < 86400:
            return
        self._last_compound_check = time.time()
        equity = self.state_reader.get_account_equity()
        if equity and equity > 0:
            new_capital = equity / MAX_CONCURRENT_POSITIONS
            self.allocator = PortfolioAllocator(
                self.depth_tracker, self.funding_ranker, capital_per_slot_usd=new_capital
            )
            logger.info("Auto-compounding: equity=%.2f, new capital_per_slot=%.2f", equity, new_capital)

    async def _watch_sentiment_file(self) -> None:
        """Read current_sentiment.json every 60s and persist score to SQLite for the dashboard."""
        import math
        while not self._shutdown_event.is_set():
            try:
                if not bool(self._config.get("sentiment_enabled")):
                    self._sentiment_score = 0.0
                    self.state_writer.set_stat("sentiment_score", 0.0)
                    if await self._sleep_or_shutdown(60.0):
                        break
                    continue
                if os.path.exists(_SENTIMENT_PATH):
                    with open(_SENTIMENT_PATH, encoding="utf-8") as f:
                        data = json.load(f)
                    raw = float(data.get("sentiment_score", 0.0))
                    # Guard against NaN/Inf from malformed or LLM-hallucinated AI responses.
                    if math.isnan(raw) or math.isinf(raw):
                        logger.warning("Sentiment score is non-finite (%s) — resetting to neutral", raw)
                        raw = 0.0
                    # Clamp to valid range [-1.0, 1.0] regardless of AI output.
                    self._sentiment_score = max(-1.0, min(1.0, raw))
                    self.state_writer.set_stat("sentiment_score", self._sentiment_score)
            except (json.JSONDecodeError, ValueError, TypeError, KeyError) as e:
                logger.warning("Failed to parse current_sentiment.json, resetting to neutral: %s", e)
                self._sentiment_score = 0.0
            except Exception as e:
                logger.error("Unexpected error reading sentiment file: %s", e)
                self._sentiment_score = 0.0
            if await self._sleep_or_shutdown(60.0):
                break

    def _effective_entry_threshold(self) -> float:
        """Entry threshold with adaptive baseline, sentiment, and streak penalties.

        Adaptive thresholds raise the floor based on recent funding distributions.
        Sentiment then tilts the threshold modestly, and loss streaks can temporarily
        harden the gate further in paper/testnet adaptive mode.
        """
        base = float(self._config.get("entry_ann_funding_threshold"))
        adaptive_base = self._adaptive_entry_threshold_base if self._adaptive_controls_enabled() else base
        threshold = max(base, adaptive_base)
        if bool(self._config.get("sentiment_enabled")):
            scale = max(0.50, min(1.50, 1.0 - 0.20 * self._sentiment_score))
            threshold *= scale
        if self._loss_streak >= int(self._config.get("loss_streak_trigger")):
            threshold *= float(self._config.get("loss_streak_entry_multiplier"))
        return threshold

    def _effective_rotation_gap(self) -> float:
        if not self._adaptive_controls_enabled():
            return ROTATION_MIN_GAP_ANN
        return max(ROTATION_MIN_GAP_ANN, self._adaptive_rotation_gap)

    def _effective_notional_scale(self) -> float:
        adaptive_scale = 1.0
        if self._adaptive_controls_enabled():
            adaptive_scale = max(0.1, min(1.0, self._streak_notional_scale))
        return max(0.1, min(1.0, min(adaptive_scale, self._risk_position_scale)))

    def _cooldown_seconds(self, key: str) -> float:
        try:
            minutes = float(self._config.get(key))
        except (TypeError, ValueError):
            minutes = 0.0
        return max(0.0, minutes * 60.0)

    def _persist_guard_snapshot(
        self,
        regime_blocked: dict[str, RegimeDecision] | None = None,
    ) -> None:
        cooldown_snapshot = self.cooldowns.snapshot()
        payload = {
            "cooldown_global_active": cooldown_snapshot["global_active"],
            "cooldown_global_reason": cooldown_snapshot["global_reason"],
            "cooldown_global_until": cooldown_snapshot["global_until"],
            "cooldown_global_remaining_s": cooldown_snapshot["global_remaining_s"],
            "cooldown_symbols": cooldown_snapshot["symbol_cooldowns"],
        }
        if regime_blocked is not None:
            payload["regime_blocked_symbols"] = sorted(regime_blocked.keys())
            payload["regime_blocked_reasons"] = {
                symbol: decision.reasons for symbol, decision in regime_blocked.items()
            }
        self.state_writer.set_risk_snapshot(payload)

    def _activate_breaker_cooldown(self, state: str, symbols: list[str]) -> None:
        reason = f"breaker {state.lower()}"
        if state == "HALTED":
            self.cooldowns.activate_global(
                self._cooldown_seconds("cooldown_halted_minutes"),
                reason,
            )
        elif state == "PARTIAL_EXIT":
            self.cooldowns.activate_global(
                self._cooldown_seconds("cooldown_partial_exit_minutes"),
                reason,
            )
        elif state == "EMERGENCY":
            self.cooldowns.activate_global(
                self._cooldown_seconds("cooldown_emergency_minutes"),
                reason,
            )
        else:
            return

        symbol_duration_s = self._cooldown_seconds("cooldown_symbol_minutes")
        for symbol in symbols:
            self.cooldowns.activate_symbol(symbol, symbol_duration_s, reason)

        self._persist_guard_snapshot()

    def _predictor_allows_entry(self, symbol: str, effective_threshold: float) -> bool:
        """Return False if the FundingPredictor projects the rate will decay below
        the entry threshold by the next funding snapshot with sufficient confidence.

        Prevents entering a position whose funding rate is about to collapse.
        Returns True when there is insufficient predictor data (allow entry).
        """
        if not self.predictor.has_data(symbol):
            return True
        minutes_since_snap = self._minutes_since_last_snapshot()
        minutes_to_next_snap = max(0.1, FUNDING_INTERVAL_HOURS * 60 - minutes_since_snap)
        projected_rate, confidence = self.predictor.predict_with_confidence(symbol, minutes_to_next_snap)
        projected_edge = projected_rate if not INVERSE_FUNDING_ENABLED else abs(projected_rate)
        if confidence >= MIN_CONFIDENCE_FOR_ENTRY and projected_edge < effective_threshold:
            logger.info(
                "Predictor gate: skipping %s — projected rate %.2f%% < threshold %.2f%% "
                "at next snapshot (confidence=%.0f%%)",
                symbol, projected_rate * 100, effective_threshold * 100, confidence * 100,
            )
            return False
        return True

    def _entry_structure_allows_symbol(self, symbol: str) -> bool:
        basis_pct = self.depth_tracker.basis_pct(symbol)
        threshold = float(self._config.get("entry_premium_threshold"))
        if basis_pct is None:
            logger.info("Skipping %s — no live spot/perp basis yet", symbol)
            return False
        if basis_pct <= threshold:
            logger.debug(
                "Skipping %s — basis %.4f below required premium %.4f",
                symbol,
                basis_pct,
                threshold,
            )
            return False
        minutes_to_next_snapshot = max(0.0, FUNDING_INTERVAL_HOURS * 60 - self._minutes_since_last_snapshot())
        if minutes_to_next_snapshot <= 15.0:
            logger.info(
                "Skipping %s — only %.0f minutes to next funding snapshot",
                symbol,
                minutes_to_next_snapshot,
            )
            return False
        return True

    async def _trading_loop(self) -> None:
        _last_heartbeat = 0.0
        _last_rest_sync = 0.0
        while not self._shutdown_event.is_set():
            try:
                if self._runtime_mode == "BLOCKED":
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue
                if not self.subscriber.is_connected:
                    logger.info("Waiting for Rust subscriber connection before dispatching entries")
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                # Sync REST depth to tracker every ~5 seconds
                import time as _sync_time
                now_sync = _sync_time.monotonic()
                if now_sync - _last_rest_sync >= 5:
                    _last_rest_sync = now_sync
                    await self._sync_rest_depth_to_tracker()

                position_rows = self._refresh_open_position_metrics()
                open_positions = self._get_open_positions(position_rows)
                funding_rates = {p.symbol: p.ann_funding for p in open_positions}
                self._expire_stale_pending_intents()
                risk_decision = self._evaluate_risk_controls(position_rows)
                if risk_decision.kill_switch or risk_decision.derisk_required or not risk_decision.allow_new_risk:
                    if risk_decision.reasons:
                        log_fn = logger.critical if risk_decision.kill_switch else logger.warning
                        log_fn("RISK ENGINE: %s", "; ".join(risk_decision.reasons))
                    for pos in open_positions:
                        if pos.symbol not in self._exit_events:
                            self._dispatch_exit(
                                pos.symbol,
                                urgency=1.0 if risk_decision.kill_switch else 0.9,
                                direction=self._position_directions.get(pos.symbol, "long"),
                            )
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                # ── 0. Post-snapshot funding decay exit ──────────────────────
                # Within 5 minutes after a funding snapshot, funding rates that
                # have decayed below the exit threshold are acted on immediately
                # rather than waiting for the next allocator cycle.
                minutes_since_snap = self._minutes_since_last_snapshot()
                if minutes_since_snap <= 5 and open_positions:
                    for pos in open_positions:
                        if (
                            self._funding_has_decayed(
                                self._position_directions.get(pos.symbol, "long"),
                                pos.ann_funding,
                            )
                            and pos.symbol not in self._exit_events
                        ):
                            logger.info(
                                "Post-snapshot decay: %s funding=%.1f%% crossed exit threshold — exiting",
                                pos.symbol, pos.ann_funding * 100,
                            )
                            self._dispatch_exit(
                                pos.symbol,
                                urgency=1.0,
                                direction=self._position_directions.get(pos.symbol, "long"),
                            )

                # ── 1. Circuit breaker ───────────────────────────────────────
                liquidity_map = {
                    p.symbol: self.depth_tracker.get_exit_depth(p.symbol)
                    for p in open_positions
                }
                breaker_decision = self.breaker.evaluate(
                    funding_rates,
                    liquidity_map=liquidity_map,
                    directions=self._position_directions,
                )
                if breaker_decision.state != self._last_breaker_state:
                    self._activate_breaker_cooldown(
                        breaker_decision.state,
                        breaker_decision.positions_to_exit,
                    )
                    self._last_breaker_state = breaker_decision.state

                if breaker_decision.state == "WARNED":
                    logger.warning("CIRCUIT BREAKER: WARNED — %s", breaker_decision.reason)
                    # Entries still allowed; fall through to allocation logic

                elif breaker_decision.state == "PARTIAL_EXIT":
                    logger.warning("CIRCUIT BREAKER: PARTIAL_EXIT — %s", breaker_decision.reason)
                    for symbol in breaker_decision.positions_to_exit:
                        if symbol not in self._exit_events:
                            self._dispatch_exit(
                                symbol,
                                urgency=0.9,
                                direction=self._position_directions.get(symbol, "long"),
                            )
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                elif breaker_decision.state == "EMERGENCY":
                    logger.warning("CIRCUIT BREAKER: EMERGENCY — exiting all positions")
                    for symbol in breaker_decision.positions_to_exit:
                        if symbol not in self._exit_events:
                            self._dispatch_exit(
                                symbol,
                                urgency=1.0,
                                direction=self._position_directions.get(symbol, "long"),
                            )
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                if not breaker_decision.allow_new_entries:
                    import time as _halt_time
                    now_halt = _halt_time.monotonic()
                    if self._halted_since == 0.0:
                        self._halted_since = now_halt
                        logger.info("CIRCUIT BREAKER: HALTED — blocking new entries")
                    elif now_halt - self._halted_since >= _HALTED_ESCALATION_SECS:
                        logger.warning(
                            "CIRCUIT BREAKER: HALTED for %.0f min — escalating to partial exits",
                            (now_halt - self._halted_since) / 60,
                        )
                        self._halted_since = 0.0  # Reset so next HALTED gets a fresh clock
                        for pos in open_positions:
                            if (
                                self._funding_has_decayed(
                                    self._position_directions.get(pos.symbol, "long"),
                                    pos.ann_funding,
                                )
                                and pos.symbol not in self._exit_events
                            ):
                                self._dispatch_exit(
                                    pos.symbol,
                                    urgency=0.9,
                                    direction=self._position_directions.get(pos.symbol, "long"),
                                )
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                # Clear HALTED timer when breaker returns to non-blocking state
                self._halted_since = 0.0

                # ── 2. Allocation decision ───────────────────────────────────
                await self._maybe_recompound()
                import time as _time
                bybit_rates = self.bybit_monitor.get_rates()
                now = _time.monotonic()
                if bybit_rates and now - self._last_xval_check >= 60:
                    self._last_xval_check = now
                    for sym, bybit_rate in bybit_rates.items():
                        if not self.funding_ranker.has_symbol(sym):
                            continue
                        ranker_rate = self.funding_ranker.get_rate(sym)
                        self._maybe_log_cross_validation_gap(
                            sym,
                            ranker_rate,
                            bybit_rate,
                            now=now,
                        )
                ranked = self.funding_ranker.get_ranked()
                ranked_symbols = [sym for sym, _ in ranked]
                regime_blocked = self.regime_filter.blocked_symbols(ranked_symbols)
                cooldown_snapshot = self.cooldowns.snapshot()

                if cooldown_snapshot["global_active"]:
                    if now - _last_heartbeat >= 60:
                        _last_heartbeat = now
                        top_rate = ranked[0][1] if ranked else 0.0
                        logger.info(
                            "HEARTBEAT: %d positions | top funding=%.2f%% | threshold=%.1f%% | "
                            "global cooldown active (%s, %.0fs left)",
                            len(open_positions),
                            top_rate * 100,
                            self._effective_entry_threshold() * 100,
                            cooldown_snapshot["global_reason"],
                            cooldown_snapshot["global_remaining_s"],
                        )
                        self.state_writer.set_stat("open_positions", float(len(open_positions)))
                        self.state_writer.set_stat("top_funding_rate", top_rate * 100)
                        self.state_writer.set_stat("top_funding_symbol", ranked[0][0] if ranked else "")
                        self._persist_guard_snapshot(regime_blocked)
                    if await self._sleep_or_shutdown(1.0):
                        break
                    continue

                cooldown_blocked = self.cooldowns.blocked_symbols(ranked_symbols)
                blocked_symbols = set(regime_blocked) | set(cooldown_blocked)

                decision = self.allocator.decide(
                    open_positions,
                    blocked_symbols=blocked_symbols,
                    notional_scale=self._effective_notional_scale(),
                    rotation_min_gap_ann=self._effective_rotation_gap(),
                )
                external_entry_block_reason = self._external_entry_block_reason()

                # ── 3. Dispatch exits ────────────────────────────────────────
                for symbol, reason in decision.exit:
                    if symbol not in self._exit_events:
                        logger.info("Rotation: exiting %s (%s)", symbol, reason)
                        self._dispatch_exit(
                            symbol,
                            urgency=0.8,
                            direction=self._position_directions.get(symbol, "long"),
                        )

                # ── 4. Await exit confirmations, dispatch rotation entries ────
                # All rotation exits are awaited concurrently so a single slow fill
                # doesn't hold up others or block the circuit breaker for N×timeout.
                if decision.rotation_targets:
                    confirm_tasks = {
                        exited_symbol: asyncio.ensure_future(
                            self._await_exit_confirmation(exited_symbol)
                        )
                        for exited_symbol in decision.rotation_targets
                    }
                    results = await asyncio.gather(*confirm_tasks.values(), return_exceptions=True)
                    for (exited_symbol, rotation_target), confirmed in zip(
                        decision.rotation_targets.items(), results
                    ):
                        if confirmed is True:
                            if external_entry_block_reason is not None:
                                logger.info(
                                    "Skipping rotation entry for %s — external risk gate active (%s)",
                                    rotation_target,
                                    external_entry_block_reason,
                                )
                                continue
                            allowed, cooldown_reason = self.cooldowns.allow_symbol(rotation_target)
                            if not allowed:
                                logger.info(
                                    "Skipping rotation entry for %s — cooldown active (%s)",
                                    rotation_target, cooldown_reason,
                                )
                                continue
                            regime_decision = self.regime_filter.evaluate(rotation_target)
                            if not regime_decision.allow_entry:
                                logger.info(
                                    "Skipping rotation entry for %s — regime filter blocked (%s)",
                                    rotation_target, ", ".join(regime_decision.reasons),
                                )
                                continue
                            rot_funding = self.funding_ranker.get_rate(rotation_target) or 0.0
                            rot_threshold = self._effective_entry_threshold()
                            if rot_funding < rot_threshold:
                                logger.info(
                                    "Skipping rotation entry for %s — funding %.2f%% below threshold %.1f%%",
                                    rotation_target, rot_funding * 100, rot_threshold * 100,
                                )
                                continue
                            if not self._entry_structure_allows_symbol(rotation_target):
                                continue
                            if not self._predictor_allows_entry(rotation_target, rot_threshold):
                                continue
                            rotation_notional = decision.rotation_notionals.get(
                                exited_symbol,
                                CAPITAL_PER_SLOT_USD * TARGET_LEVERAGE,
                            )
                            self._dispatch_enter(
                                rotation_target,
                                rotation_notional,
                                direction="long",
                                ann_funding=rot_funding,
                            )
                        else:
                            logger.warning(
                                "Skipping rotation entry for %s — exit of %s unconfirmed",
                                rotation_target, exited_symbol,
                            )

                # ── 5. Dispatch entries for empty slots ─────────────────────
                entry_threshold = self._effective_entry_threshold()
                for symbol, notional in decision.enter:
                    if symbol in self._exit_events:
                        continue
                    if symbol in self._pending_enters:
                        logger.debug("Skipping %s — entry already pending confirmation", symbol)
                        continue
                    if external_entry_block_reason is not None:
                        logger.info(
                            "Skipping %s — external risk gate active (%s)",
                            symbol,
                            external_entry_block_reason,
                        )
                        continue
                    allowed, cooldown_reason = self.cooldowns.allow_symbol(symbol)
                    if not allowed:
                        logger.info(
                            "Skipping %s — cooldown active (%s)",
                            symbol, cooldown_reason,
                        )
                        continue
                    regime_decision = self.regime_filter.evaluate(symbol)
                    if not regime_decision.allow_entry:
                        logger.info(
                            "Skipping %s — regime filter blocked (%s)",
                            symbol, ", ".join(regime_decision.reasons),
                        )
                        continue

                    ann_funding = self.funding_ranker.get_rate(symbol) or 0.0
                    # Long-only release candidate: only collect positive funding.
                    if ann_funding < entry_threshold:
                        logger.debug(
                            "Skipping %s — funding %.2f%% below threshold %.1f%%",
                            symbol, ann_funding * 100, entry_threshold * 100,
                        )
                        continue
                    if not self._entry_structure_allows_symbol(symbol):
                        continue
                    # Predictor gate: skip if projected rate decays below threshold at snapshot
                    if not self._predictor_allows_entry(symbol, entry_threshold):
                        continue
                    self._dispatch_enter(symbol, notional, direction="long", ann_funding=ann_funding)

                # ── 6. Heartbeat — periodic status for logs + dashboard ────
                if now - _last_heartbeat >= 60:
                    _last_heartbeat = now
                    top_rate = ranked[0][1] if ranked else 0.0
                    logger.info(
                        "HEARTBEAT: %d positions | top funding=%.2f%% | threshold=%.1f%% | "
                        "%d pending enters | %d pending exits | %d regime/cooldown blocks",
                        len(open_positions),
                        top_rate * 100,
                        self._effective_entry_threshold() * 100,
                        len(self._pending_enters),
                        len(self._exit_events),
                        len(blocked_symbols),
                    )
                    self.state_writer.set_stat("open_positions", float(len(open_positions)))
                    self.state_writer.set_stat("top_funding_rate", top_rate * 100)
                    self.state_writer.set_stat("top_funding_symbol", ranked[0][0] if ranked else "")
                    self._persist_guard_snapshot(regime_blocked)

            except Exception as exc:
                logger.error("Error in trading loop: %s", exc, exc_info=True)

            if await self._sleep_or_shutdown(1.0):
                break

    async def _fetch_mark_prices_via_rest(self) -> None:
        """Fetch current mark prices for all monitored symbols via Binance REST API.

        This populates _mark_prices cache before the trading loop starts,
        preventing "No mark price yet" warnings during startup.
        """
        try:
            # Fetch all prices from Binance futures ticker
            resp = await asyncio.to_thread(
                requests.get,
                "https://fapi.binance.com/fapi/v1/ticker/price",
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()

            count = 0
            for item in data:
                sym = item.get("symbol", "")
                if sym in self._monitored_symbol_set:
                    try:
                        price = float(item.get("price", 0.0))
                        if price > 0.0:
                            self._mark_prices[sym] = price
                            self._mark_price_ready.add(sym)
                            self._mark_price_updated_monotonic[sym] = time.monotonic()
                            count += 1
                    except (ValueError, TypeError):
                        pass

            logger.info("REST mark prices fetched for %d/%d symbols", count, len(self.monitored_symbols))
        except Exception as exc:
            logger.warning("Could not fetch REST mark prices: %s", exc)

    async def run(self) -> None:
        logger.info("Starting LiveTraderV2 — monitoring %d symbols", len(self.monitored_symbols))
        self._loop = asyncio.get_running_loop()
        self._install_signal_handlers()
        try:
            await self._run_preflight()
            await self._on_startup()
            self._refresh_adaptive_state()

            await self._fetch_lot_step_sizes()
            await asyncio.gather(
                self.funding_ranker.refresh(),
                self.bybit_monitor.refresh(),
                self.rest_depth_fetcher.refresh_all(),
                self._fetch_mark_prices_via_rest(),
            )
            await self._sync_rest_depth_to_tracker()
            ready_count = len(self._mark_price_ready)
            logger.info(
                "Startup primed: %d/%d symbols with mark prices ready",
                ready_count, len(self.monitored_symbols),
            )
            self._background_tasks = [
                asyncio.create_task(self.subscriber.run(), name="rust_subscriber"),
                asyncio.create_task(
                    self.funding_ranker.run_forever(interval_s=60),
                    name="funding_ranker",
                ),
                asyncio.create_task(self.bybit_monitor.run_forever(), name="bybit_monitor"),
                asyncio.create_task(
                    self.rest_depth_fetcher.run_forever(interval_s=30),
                    name="rest_depth_fetcher",
                ),
                asyncio.create_task(self._watch_sentiment_file(), name="sentiment_watch"),
                asyncio.create_task(self._run_heartbeat_loop(), name="heartbeat_loop"),
                asyncio.create_task(self._run_maintenance_loop(), name="maintenance_loop"),
                asyncio.create_task(self._trading_loop(), name="trading_loop"),
            ]
            await asyncio.gather(*self._background_tasks)
        except asyncio.CancelledError:
            if not self._shutdown_started:
                raise
        except StartupBlockedError:
            await self.shutdown(reason="startup_blocked")
            raise
        finally:
            await self.shutdown(reason="run_exit")


async def main() -> None:
    trader = LiveTraderV2()
    try:
        await trader.run()
    except StartupBlockedError as exc:
        logger.error("Trader startup blocked: %s", exc)
        raise SystemExit(_BLOCKED_EXIT_CODE) from exc


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("LiveTraderV2 stopped.")
