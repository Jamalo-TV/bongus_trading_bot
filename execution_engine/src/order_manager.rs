use rand::Rng;
use serde_json::Value;
use std::collections::{HashMap, HashSet, VecDeque};
use std::fs::OpenOptions;
use std::future::Future;
use std::io::Write;
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::broadcast;
use tokio::sync::mpsc::{Receiver, Sender};
use tokio::time::sleep;
use tracing::{debug, error, info, warn};

use crate::binance_rest::{BinanceRest, LegVenue, ReconciledSubmission, TradeSide};
use crate::collateral_engine::UnifiedPortfolioMarginCalculator;
use crate::ipc::{
    CONFIG_SYNC_INTENT, ConfigAck, ConfigConsensus, IntentJournal, IntentReceipt, ReceiptDecision,
};

#[derive(Debug, PartialEq, Eq, Clone)]
pub enum SystemState {
    Disconnected,
    Reconciling,
    Trading,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AlphaIntent {
    Heartbeat,
    RestorePosition,
    EnterLong,
    EnterShort,
    ExitLong,
    ExitShort,
}

impl AlphaIntent {
    fn parse(value: &str) -> Option<Self> {
        match value {
            "HEARTBEAT" => Some(Self::Heartbeat),
            "RESTORE_POSITION" => Some(Self::RestorePosition),
            "ENTER_LONG" => Some(Self::EnterLong),
            "ENTER_SHORT" => Some(Self::EnterShort),
            "EXIT_LONG" => Some(Self::ExitLong),
            "EXIT_SHORT" => Some(Self::ExitShort),
            _ => None,
        }
    }

    fn is_exit(self) -> bool {
        matches!(self, Self::ExitLong | Self::ExitShort)
    }

    fn is_buy(self) -> bool {
        matches!(self, Self::EnterLong | Self::ExitShort)
    }
}

#[derive(Debug, Clone, Copy, serde::Serialize, serde::Deserialize, PartialEq, Eq, Hash)]
#[serde(rename_all = "lowercase")]
pub enum MarketType {
    Spot,
    Perp,
}

#[derive(Debug, Clone, Copy, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum WsStreamType {
    UserData,
    MarketData,
}

#[derive(Debug, Clone, serde::Serialize)]
#[serde(tag = "event")]
pub enum WsEvent {
    Connected {
        symbol: String,
        stream_type: WsStreamType,
    },
    Disconnected {
        symbol: String,
        stream_type: WsStreamType,
    },
    PrivateStreamStatus {
        market: MarketType,
        status: String,
        start_time_ms: Option<i64>,
        end_time_ms: Option<i64>,
        orders_replayed: u64,
        trades_replayed: u64,
        error: Option<String>,
    },
    ExecutionReadiness {
        status: String,
        reason: String,
        event_time_ms: i64,
    },
    TelemetryGap {
        skipped_messages: u64,
        reason: String,
        event_time_ms: i64,
    },
    BookTicker {
        symbol: String,
        bid_price: f64,
        ask_price: f64,
    },
    L2Depth {
        symbol: String,
        market: MarketType,
        bids: Vec<[f64; 2]>,
        asks: Vec<[f64; 2]>,
        first_update_id: Option<u64>,
        final_update_id: Option<u64>,
        previous_final_update_id: Option<u64>,
        is_snapshot: bool,
    },
    /// Emitted on every markPriceUpdate from Binance perp streams (~1s cadence).
    /// `next_funding_rate` is the predicted rate for the upcoming settlement —
    /// more actionable than lastFundingRate for entry/exit decisions.
    MarkPrice {
        symbol: String,
        mark_price: f64,
        next_funding_rate: f64,
        next_funding_time_ms: i64,
    },
    VolumeBar {
        symbol: String,
        minute_start_ms: i64,
        notional_usd: f64,
    },
    OrderUpdate {
        client_order_id: String,
        symbol: String,
        status: String,
        filled_qty: f64,
        /// Exchange cumulative executed quantity for this concrete order.
        /// `filled_qty` remains the last-fill delta for economic attribution.
        cumulative_filled_qty: Option<f64>,
        avg_fill_price: Option<f64>,
        last_fill_price: Option<f64>,
        cumulative_quote_qty: Option<f64>,
        commission: Option<f64>,
        commission_asset: Option<String>,
        realized_pnl: Option<f64>,
        maker: Option<bool>,
        execution_type: Option<String>,
        event_time_ms: Option<i64>,
        maker_fills: Option<u64>,
        taker_fills: Option<u64>,
        market: Option<MarketType>,
        side: Option<String>,
        order_id: Option<i64>,
        trade_id: Option<i64>,
        account_id: Option<String>,
        environment: Option<String>,
        strategy_id: Option<String>,
        cycle_id: Option<String>,
        intent_id: Option<String>,
        leg_id: Option<String>,
        config_version_hash: Option<String>,
    },
    AccountUpdate {
        balances: HashMap<String, f64>,
        source: String,
    },
    PositionDivergence {
        symbol: String,
        divergence_type: String,
        local_qty: f64,
        exchange_qty: f64,
    },
}

pub enum EngineEvent {
    Ws(WsEvent),
    Alpha(crate::ipc::AlphaInstruction),
    LeggingTimeout(String),
    StrategyTick,
    PositionAuditTick,
    ExchangeInfoRefreshResult(
        Result<HashMap<String, crate::binance_rest::ExchangeSymbolInfo>, String>,
    ),
}

#[derive(Debug, Clone)]
struct PrivateStreamStatusSnapshot {
    market: MarketType,
    status: String,
    start_time_ms: Option<i64>,
    end_time_ms: Option<i64>,
    orders_replayed: u64,
    trades_replayed: u64,
    error: Option<String>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct InternalOrder {
    pub client_order_id: String,
    pub symbol: String,
    pub status: String,
    pub limit_price: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct TrackedLegPosition {
    pub side: String,
    pub entry_price: f64,
    pub quantity: f64,
    pub unrealized_pnl: f64,
    pub last_mark_price: f64,
}

#[allow(dead_code)]
#[derive(Debug, Clone, Default)]
pub struct TrackedPosition {
    pub symbol: String,
    pub spot: Option<TrackedLegPosition>,
    pub perp: Option<TrackedLegPosition>,
}

#[derive(Debug, Clone, Copy)]
pub struct TopOfBook {
    pub bid_price: f64,
    pub ask_price: f64,
    pub bid_qty: f64,
    pub ask_qty: f64,
}

#[derive(Debug, Clone, Copy)]
struct ExecutableDepth {
    bid_notional_usd: f64,
    ask_notional_usd: f64,
    observed_at: Instant,
}

const ENTRY_DEPTH_MULTIPLIER: f64 = 5.0;
const EXECUTABLE_DEPTH_MAX_AGE: Duration = Duration::from_secs(3);
const EXCHANGE_METADATA_TTL: Duration = Duration::from_secs(15 * 60);

const TOXIC_SPREAD_THRESHOLD_BPS: f64 = 50.0;
const TOXIC_LOG_REFRESH_SECS: u64 = 30;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ToxicityLogAction {
    None,
    Enter,
    Refresh,
    Exit,
}

fn toxicity_log_action(
    spread_bps: f64,
    was_toxic: bool,
    last_logged_at: Option<Instant>,
    now: Instant,
) -> ToxicityLogAction {
    if spread_bps > TOXIC_SPREAD_THRESHOLD_BPS {
        if !was_toxic {
            return ToxicityLogAction::Enter;
        }
        let should_refresh = last_logged_at
            .map(|last| now.duration_since(last) >= Duration::from_secs(TOXIC_LOG_REFRESH_SECS))
            .unwrap_or(true);
        return if should_refresh {
            ToxicityLogAction::Refresh
        } else {
            ToxicityLogAction::None
        };
    }
    if was_toxic {
        ToxicityLogAction::Exit
    } else {
        ToxicityLogAction::None
    }
}

pub struct OrderManager {
    pub state: SystemState,
    pub internal_orders: HashMap<String, InternalOrder>,
    pub obi_cache: HashMap<String, f64>,
    pub obi_alert_at: HashMap<String, Instant>,
    pub exchange_info: HashMap<String, crate::binance_rest::ExchangeSymbolInfo>,
    exchange_info_updated_at: Option<Instant>,
    pub event_receiver: Receiver<EngineEvent>,
    deferred_actor_events: VecDeque<EngineEvent>,
    pub engine_tx: tokio::sync::mpsc::Sender<EngineEvent>,
    pub subscription_tx: Sender<String>,
    pub binance_rest: BinanceRest,
    chase_states: HashMap<String, ChaseState>, // key: symbol (uppercase)
    pub dash_tx: broadcast::Sender<Vec<u8>>,
    pub is_toxic: bool,
    toxic_symbols: HashMap<String, Instant>,
    pub last_brain_ping: Instant,
    pub current_gross_exposure_usd: f64,
    pub max_gross_exposure_usd: f64,
    compiled_max_gross_exposure_usd: f64,
    compiled_max_per_symbol_notional_usd: f64,
    pub account_equity_usd: f64,
    pub tracked_positions: HashMap<String, TrackedPosition>,
    #[allow(dead_code)]
    pub collateral_calc: UnifiedPortfolioMarginCalculator,
    pub basis_deviation_stop_bps: f64,
    pub maker_fills: u64,
    pub taker_fills: u64,
    pub mid_price_history: HashMap<String, VecDeque<f64>>,
    spot_mid_cache: HashMap<String, f64>,
    perp_mid_cache: HashMap<String, f64>,
    pub spot_top_cache: HashMap<String, TopOfBook>,
    pub perp_top_cache: HashMap<String, TopOfBook>,
    spot_depth_capacity: HashMap<String, ExecutableDepth>,
    perp_depth_capacity: HashMap<String, ExecutableDepth>,
    pub trading_mode: String,
    pub balances: HashMap<String, f64>,
    pub spot_balances: HashMap<String, f64>,
    pub ranking_engine: crate::ranking::RankingEngine,
    pub strategy_engine: crate::strategy::StrategyEngine,
    intent_journal: Option<IntentJournal>,
    intent_journal_error: Option<String>,
    config_consensus: ConfigConsensus,
    chase_intent_ids: HashMap<String, String>,
    order_cumulative_fills: HashMap<String, f64>,
    order_lineage: HashMap<String, OrderLineage>,
    depth_sequences: HashMap<String, u64>,
    private_stream_ready_markets: HashSet<MarketType>,
    private_stream_status_snapshots: HashMap<MarketType, PrivateStreamStatusSnapshot>,
    execution_state_journal_path: PathBuf,
    execution_state_journal_error: Option<String>,
    chase_unhedged_budgets: HashMap<String, f64>,
    chase_unhedged_started_at_ms: HashMap<String, i64>,
}

#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
struct OrderLineage {
    account_id: Option<String>,
    environment: Option<String>,
    strategy_id: Option<String>,
    cycle_id: Option<String>,
    intent_id: Option<String>,
    leg_id: Option<String>,
    config_version_hash: Option<String>,
    market: Option<MarketType>,
    side: Option<String>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
enum Leg {
    Spot,
    Futures,
}

#[derive(Debug, Clone, Copy, PartialEq, serde::Serialize, serde::Deserialize)]
enum ChasePhase {
    Idle,
    DualMakerPlaced,
    LegFilledWaiting(Leg),
    LeggingDefenseTakerPlaced,
    ReconciliationRequired,
    Completed,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct ChaseState {
    symbol: String,
    quantity: f64,
    spot_quantity: f64,
    perp_quantity: f64,
    spot_client_order_id: String,
    futures_client_order_id: String,
    skip_spot_leg: bool,
    skip_perp_leg: bool,
    spot_side: TradeSide,
    futures_side: TradeSide,
    is_exit: bool,
    max_slippage_bps: f64,
    phase: ChasePhase,
    #[allow(dead_code)]
    #[serde(skip, default = "instant_now")]
    start_time: Instant,
    expected_spot_price: f64,
    expected_fut_price: f64,
    spot_fill_price: Option<f64>,
    futures_fill_price: Option<f64>,
    spot_cumulative_filled: f64,
    futures_cumulative_filled: f64,
    spot_terminal: bool,
    futures_terminal: bool,
}

fn instant_now() -> Instant {
    Instant::now()
}

const EXECUTION_STATE_SCHEMA_VERSION: u32 = 1;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct ExecutionStateSnapshot {
    schema_version: u32,
    recorded_at_ms: i64,
    #[serde(default)]
    chase_states: HashMap<String, ChaseState>,
    #[serde(default)]
    internal_orders: HashMap<String, InternalOrder>,
    #[serde(default)]
    chase_intent_ids: HashMap<String, String>,
    #[serde(default)]
    order_cumulative_fills: HashMap<String, f64>,
    #[serde(default)]
    order_lineage: HashMap<String, OrderLineage>,
    #[serde(default)]
    chase_unhedged_budgets: HashMap<String, f64>,
    #[serde(default)]
    chase_unhedged_started_at_ms: HashMap<String, i64>,
}

impl ChaseState {
    fn has_spot_leg(&self) -> bool {
        !self.skip_spot_leg
    }

    fn has_futures_leg(&self) -> bool {
        !self.skip_perp_leg
    }

    fn is_single_leg(&self) -> bool {
        self.has_spot_leg() ^ self.has_futures_leg()
    }

    fn active_leg(&self) -> Option<Leg> {
        match (self.has_spot_leg(), self.has_futures_leg()) {
            (true, false) => Some(Leg::Spot),
            (false, true) => Some(Leg::Futures),
            _ => None,
        }
    }

    fn cycle_client_order_id(&self) -> &str {
        if self.has_spot_leg() && !self.spot_client_order_id.is_empty() {
            &self.spot_client_order_id
        } else {
            &self.futures_client_order_id
        }
    }

    fn cycle_fill_price(&self) -> f64 {
        if self.has_spot_leg() {
            self.spot_fill_price.unwrap_or(self.expected_spot_price)
        } else {
            self.futures_fill_price.unwrap_or(self.expected_fut_price)
        }
    }

    fn target_for(&self, leg: Leg) -> f64 {
        match leg {
            Leg::Spot => self.spot_quantity,
            Leg::Futures => self.perp_quantity,
        }
    }

    fn cumulative_for(&self, leg: Leg) -> f64 {
        match leg {
            Leg::Spot => self.spot_cumulative_filled,
            Leg::Futures => self.futures_cumulative_filled,
        }
    }

    fn terminal_for(&self, leg: Leg) -> bool {
        match leg {
            Leg::Spot => self.spot_terminal,
            Leg::Futures => self.futures_terminal,
        }
    }

    fn set_progress(&mut self, leg: Leg, cumulative: f64, terminal: bool) {
        match leg {
            Leg::Spot => {
                self.spot_cumulative_filled = cumulative;
                self.spot_terminal = terminal;
            }
            Leg::Futures => {
                self.futures_cumulative_filled = cumulative;
                self.futures_terminal = terminal;
            }
        }
    }

    fn both_legs_terminal(&self) -> bool {
        (!self.has_spot_leg() || self.spot_terminal)
            && (!self.has_futures_leg() || self.futures_terminal)
    }
}

impl OrderManager {
    pub fn new(
        event_receiver: Receiver<EngineEvent>,
        engine_tx: tokio::sync::mpsc::Sender<EngineEvent>,
        subscription_tx: Sender<String>,
        api_key: String,
        secret_key: String,
        dash_tx: broadcast::Sender<Vec<u8>>,
        trading_mode: String,
    ) -> Self {
        const COMPILED_MAX_GROSS_EXPOSURE_USD: f64 = 10_000.0;
        const COMPILED_MAX_PER_SYMBOL_NOTIONAL_USD: f64 = 2_500.0;
        let max_gross_exposure = std::env::var("MAX_GROSS_EXPOSURE_USD")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .filter(|value| value.is_finite() && *value > 0.0)
            .unwrap_or(COMPILED_MAX_GROSS_EXPOSURE_USD)
            .min(COMPILED_MAX_GROSS_EXPOSURE_USD);

        let account_equity = std::env::var("ACCOUNT_EQUITY_USD")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(10_000.0);

        let basis_deviation_stop_bps = std::env::var("BASIS_DEVIATION_STOP_BPS")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(30.0);

        info!(
            "OrderManager config: max_gross_exposure=${}, account_equity=${}, basis_stop={:.0}bps, trading_mode={}",
            max_gross_exposure, account_equity, basis_deviation_stop_bps, trading_mode
        );

        let collateral_calc = UnifiedPortfolioMarginCalculator::new(
            account_equity,
            0.004, // 0.4% maintenance margin rate
            0.8,   // 80% danger threshold
        );

        let binance_rest = BinanceRest::new(api_key, secret_key, trading_mode.clone());
        let shared_rest = std::sync::Arc::new(binance_rest);

        #[cfg(not(test))]
        let journal_result = IntentJournal::from_env();
        #[cfg(test)]
        let journal_result = {
            use std::sync::atomic::{AtomicU64, Ordering};
            static TEST_JOURNAL_ID: AtomicU64 = AtomicU64::new(1);
            let id = TEST_JOURNAL_ID.fetch_add(1, Ordering::Relaxed);
            IntentJournal::load(std::env::temp_dir().join(format!(
                "bongus-intent-test-{}-{id}.jsonl",
                std::process::id()
            )))
        };
        let (intent_journal, intent_journal_error) = match journal_result {
            Ok(journal) => (Some(journal), None),
            Err(err) => {
                error!("Execution intent journal unavailable; new risk will fail closed: {err}");
                (None, Some(err))
            }
        };

        #[cfg(not(test))]
        let execution_state_journal_path = std::env::var("EXECUTION_STATE_JOURNAL_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("execution_state.jsonl"));
        #[cfg(test)]
        let execution_state_journal_path = std::env::temp_dir().join(format!(
            "bongus-execution-state-test-{}-{}.jsonl",
            std::process::id(),
            rand::random::<u64>()
        ));

        let mut manager = Self {
            state: SystemState::Disconnected,
            internal_orders: HashMap::new(),
            obi_cache: HashMap::new(),
            obi_alert_at: HashMap::new(),
            exchange_info: HashMap::new(),
            exchange_info_updated_at: None,
            event_receiver,
            deferred_actor_events: VecDeque::new(),
            engine_tx,
            subscription_tx,
            binance_rest: (*shared_rest).clone(),
            chase_states: HashMap::new(),
            dash_tx,
            is_toxic: false,
            toxic_symbols: HashMap::new(),
            last_brain_ping: Instant::now(),
            current_gross_exposure_usd: 0.0,
            max_gross_exposure_usd: max_gross_exposure,
            compiled_max_gross_exposure_usd: max_gross_exposure,
            compiled_max_per_symbol_notional_usd: COMPILED_MAX_PER_SYMBOL_NOTIONAL_USD,
            account_equity_usd: account_equity,
            tracked_positions: HashMap::new(),
            collateral_calc,
            basis_deviation_stop_bps,
            maker_fills: 0,
            taker_fills: 0,
            mid_price_history: HashMap::new(),
            spot_mid_cache: HashMap::new(),
            perp_mid_cache: HashMap::new(),
            spot_top_cache: HashMap::new(),
            perp_top_cache: HashMap::new(),
            spot_depth_capacity: HashMap::new(),
            perp_depth_capacity: HashMap::new(),
            trading_mode,
            balances: HashMap::new(),
            spot_balances: HashMap::new(),
            ranking_engine: crate::ranking::RankingEngine::new(shared_rest),
            strategy_engine: crate::strategy::StrategyEngine::new(),
            intent_journal,
            intent_journal_error,
            config_consensus: ConfigConsensus::default(),
            chase_intent_ids: HashMap::new(),
            order_cumulative_fills: HashMap::new(),
            order_lineage: HashMap::new(),
            depth_sequences: HashMap::new(),
            private_stream_ready_markets: HashSet::new(),
            private_stream_status_snapshots: HashMap::new(),
            execution_state_journal_path,
            execution_state_journal_error: None,
            chase_unhedged_budgets: HashMap::new(),
            chase_unhedged_started_at_ms: HashMap::new(),
        };
        if let Err(err) = manager.load_execution_state() {
            error!("Execution state journal unavailable; startup will fail closed: {err}");
            manager.execution_state_journal_error = Some(err);
        } else if manager.has_unresolved_execution_effects() {
            warn!(
                "Recovered unresolved execution state for {} symbol(s); startup will remain Reconciling until explicitly repaired",
                manager.chase_states.len()
            );
        }
        manager
    }

    fn validate_execution_snapshot(snapshot: &ExecutionStateSnapshot) -> Result<(), String> {
        if snapshot.schema_version != EXECUTION_STATE_SCHEMA_VERSION {
            return Err(format!(
                "unsupported execution state schema {}",
                snapshot.schema_version
            ));
        }
        for (symbol, chase) in &snapshot.chase_states {
            if symbol.trim().is_empty() || chase.symbol.to_uppercase() != symbol.to_uppercase() {
                return Err(format!("invalid chase symbol identity for {symbol:?}"));
            }
            for (label, value) in [
                ("quantity", chase.quantity),
                ("spot_quantity", chase.spot_quantity),
                ("perp_quantity", chase.perp_quantity),
                ("spot_cumulative_filled", chase.spot_cumulative_filled),
                ("futures_cumulative_filled", chase.futures_cumulative_filled),
            ] {
                if !value.is_finite() || value < 0.0 {
                    return Err(format!("invalid {label} for recovered chase {symbol}"));
                }
            }
            if (chase.has_spot_leg() && chase.spot_client_order_id.trim().is_empty())
                || (chase.has_futures_leg() && chase.futures_client_order_id.trim().is_empty())
            {
                return Err(format!(
                    "missing client order id for recovered chase {symbol}"
                ));
            }
        }
        if snapshot
            .order_cumulative_fills
            .iter()
            .any(|(client_id, value)| {
                client_id.trim().is_empty() || !value.is_finite() || *value < 0.0
            })
        {
            return Err("invalid recovered cumulative-fill map".to_string());
        }
        if snapshot
            .chase_unhedged_budgets
            .iter()
            .any(|(symbol, value)| symbol.trim().is_empty() || !value.is_finite() || *value <= 0.0)
        {
            return Err("invalid recovered max-unhedged budget".to_string());
        }
        Ok(())
    }

    fn apply_execution_snapshot(&mut self, snapshot: ExecutionStateSnapshot) {
        self.chase_states = snapshot.chase_states;
        self.internal_orders = snapshot.internal_orders;
        self.chase_intent_ids = snapshot.chase_intent_ids;
        self.order_cumulative_fills = snapshot.order_cumulative_fills;
        self.order_lineage = snapshot.order_lineage;
        self.chase_unhedged_budgets = snapshot.chase_unhedged_budgets;
        self.chase_unhedged_started_at_ms = snapshot.chase_unhedged_started_at_ms;
    }

    fn load_execution_state(&mut self) -> Result<(), String> {
        if !self.execution_state_journal_path.exists() {
            return Ok(());
        }
        let content = std::fs::read_to_string(&self.execution_state_journal_path)
            .map_err(|err| format!("read execution state journal: {err}"))?;
        let mut latest: Option<ExecutionStateSnapshot> = None;
        for (line_no, line) in content.lines().enumerate() {
            if line.trim().is_empty() {
                continue;
            }
            let snapshot: ExecutionStateSnapshot = match serde_json::from_str(line) {
                Ok(snapshot) => snapshot,
                Err(err) => {
                    if let Some(valid) = latest.take() {
                        self.apply_execution_snapshot(valid);
                    }
                    return Err(format!(
                        "invalid execution state journal line {}: {err}",
                        line_no + 1
                    ));
                }
            };
            if let Err(err) = Self::validate_execution_snapshot(&snapshot) {
                if let Some(valid) = latest.take() {
                    self.apply_execution_snapshot(valid);
                }
                return Err(format!(
                    "invalid execution state journal line {}: {err}",
                    line_no + 1
                ));
            }
            latest = Some(snapshot);
        }
        if let Some(snapshot) = latest {
            self.apply_execution_snapshot(snapshot);
        }
        Ok(())
    }

    #[cfg(test)]
    fn load_execution_state_from_path(&mut self, path: PathBuf) -> Result<(), String> {
        self.execution_state_journal_path = path;
        self.load_execution_state()
    }

    fn execution_snapshot(&self) -> ExecutionStateSnapshot {
        ExecutionStateSnapshot {
            schema_version: EXECUTION_STATE_SCHEMA_VERSION,
            recorded_at_ms: Self::current_time_ms(),
            chase_states: self.chase_states.clone(),
            internal_orders: self.internal_orders.clone(),
            chase_intent_ids: self.chase_intent_ids.clone(),
            order_cumulative_fills: self.order_cumulative_fills.clone(),
            order_lineage: self.order_lineage.clone(),
            chase_unhedged_budgets: self.chase_unhedged_budgets.clone(),
            chase_unhedged_started_at_ms: self.chase_unhedged_started_at_ms.clone(),
        }
    }

    fn append_execution_snapshot(&self) -> Result<(), String> {
        if let Some(parent) = self
            .execution_state_journal_path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            std::fs::create_dir_all(parent)
                .map_err(|err| format!("create execution state journal directory: {err}"))?;
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.execution_state_journal_path)
            .map_err(|err| format!("append execution state journal: {err}"))?;
        serde_json::to_writer(&mut file, &self.execution_snapshot())
            .map_err(|err| format!("encode execution state snapshot: {err}"))?;
        file.write_all(b"\n")
            .map_err(|err| format!("write execution state snapshot: {err}"))?;
        file.sync_data()
            .map_err(|err| format!("sync execution state snapshot: {err}"))
    }

    fn persist_execution_state(&mut self, context: &str) -> bool {
        match self.append_execution_snapshot() {
            Ok(()) => true,
            Err(err) => {
                error!("Execution state persistence failed during {context}: {err}");
                self.execution_state_journal_error = Some(err);
                self.state = SystemState::Reconciling;
                false
            }
        }
    }

    fn store_chase_state(&mut self, symbol: String, chase: ChaseState, context: &str) -> bool {
        self.chase_states.insert(symbol, chase);
        self.persist_execution_state(context)
    }

    fn remove_chase_state(&mut self, symbol: &str, context: &str) -> Option<ChaseState> {
        let removed = self.chase_states.remove(symbol);
        self.chase_unhedged_budgets.remove(symbol);
        self.chase_unhedged_started_at_ms.remove(symbol);
        if removed.is_some() {
            let _ = self.persist_execution_state(context);
        }
        removed
    }

    fn has_unresolved_execution_effects(&self) -> bool {
        !self.chase_states.is_empty()
            || self.internal_orders.values().any(|order| {
                !matches!(
                    order.status.as_str(),
                    "FILLED" | "CANCELED" | "CANCELLED" | "EXPIRED" | "REJECTED"
                )
            })
    }

    fn private_stream_quorum_ready(&self) -> bool {
        self.trading_mode == "paper"
            || (self
                .private_stream_ready_markets
                .contains(&MarketType::Spot)
                && self
                    .private_stream_ready_markets
                    .contains(&MarketType::Perp))
    }

    fn exchange_metadata_fresh(&self) -> bool {
        self.exchange_info_updated_at
            .is_some_and(|updated_at| updated_at.elapsed() <= EXCHANGE_METADATA_TTL)
    }

    fn apply_exchange_info_refresh(
        &mut self,
        result: Result<HashMap<String, crate::binance_rest::ExchangeSymbolInfo>, String>,
    ) {
        let next = match result {
            Ok(value) if !value.is_empty() => value,
            Ok(_) => {
                warn!("Exchange metadata refresh returned no paired tradable symbols");
                if !self.exchange_metadata_fresh() {
                    self.state = SystemState::Reconciling;
                    self.emit_execution_readiness("BLOCKED", "exchange_metadata_empty_or_stale");
                }
                return;
            }
            Err(err) => {
                warn!("Exchange metadata refresh failed: {}", err);
                if !self.exchange_metadata_fresh() {
                    self.state = SystemState::Reconciling;
                    self.emit_execution_readiness("BLOCKED", "exchange_metadata_refresh_failed");
                }
                return;
            }
        };

        let changed_active: Vec<String> = self
            .chase_states
            .keys()
            .filter(|symbol| self.exchange_info.get(*symbol) != next.get(*symbol))
            .cloned()
            .collect();
        self.exchange_info = next;
        self.exchange_info_updated_at = Some(Instant::now());
        if changed_active.is_empty() {
            return;
        }

        warn!(
            "Exchange metadata changed during active execution for {:?}; preserving chase evidence and requiring reconciliation",
            changed_active
        );
        self.state = SystemState::Reconciling;
        let _ = self.persist_execution_state("active-cycle exchange metadata changed");
        let event = serde_json::json!({
            "event": "ExchangeMetadataChanged",
            "symbols": changed_active,
            "reason": "active_cycle_filters_or_status_changed",
            "event_time_ms": Self::current_time_ms(),
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&event) {
            let _ = self.dash_tx.send(payload);
        }
        self.emit_execution_readiness("BLOCKED", "active_cycle_exchange_metadata_changed");
        self.emit_execution_recovery_required("active_cycle_exchange_metadata_changed");
    }

    fn record_private_stream_status(&mut self, market: MarketType, status: &str) -> bool {
        if status.eq_ignore_ascii_case("READY") {
            self.private_stream_ready_markets.insert(market);
        } else {
            self.private_stream_ready_markets.remove(&market);
            if self.trading_mode != "paper" {
                self.state = SystemState::Disconnected;
            }
        }
        self.private_stream_quorum_ready()
    }

    fn market_processing_allowed(&self, symbol: &str) -> bool {
        self.state == SystemState::Trading
            || self
                .chase_states
                .get(&symbol.to_uppercase())
                .map(|chase| chase.is_exit)
                .unwrap_or(false)
    }

    fn emit_execution_recovery_required(&self, reason: &str) {
        let event = serde_json::json!({
            "event": "ExecutionRecoveryRequired",
            "reason": reason,
            "active_chases": self.chase_states.len(),
            "unresolved_orders": self.internal_orders.values().filter(|order| {
                !matches!(order.status.as_str(), "FILLED" | "CANCELED" | "CANCELLED" | "EXPIRED" | "REJECTED")
            }).count(),
            "event_time_ms": Self::current_time_ms(),
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&event) {
            let _ = self.dash_tx.send(payload);
        }
    }

    fn emit_execution_readiness(&self, status: &str, reason: &str) {
        let event = WsEvent::ExecutionReadiness {
            status: status.to_string(),
            reason: reason.to_string(),
            event_time_ms: Self::current_time_ms(),
        };
        if let Ok(payload) = rmp_serde::to_vec_named(&event) {
            let _ = self.dash_tx.send(payload);
        }
    }

    fn emit_current_execution_state_snapshot(&self) {
        // Readiness is state, not a one-shot notification. Replaying the latest
        // private-stream evidence on every heartbeat lets an orchestrator that
        // connected after startup recover the same fail-closed view without
        // pretending that a fresh exchange reconciliation occurred.
        for market in [MarketType::Spot, MarketType::Perp] {
            let Some(snapshot) = self.private_stream_status_snapshots.get(&market) else {
                continue;
            };
            let event = WsEvent::PrivateStreamStatus {
                market: snapshot.market,
                status: snapshot.status.clone(),
                start_time_ms: snapshot.start_time_ms,
                end_time_ms: snapshot.end_time_ms,
                orders_replayed: snapshot.orders_replayed,
                trades_replayed: snapshot.trades_replayed,
                error: snapshot.error.clone(),
            };
            if let Ok(payload) = rmp_serde::to_vec_named(&event) {
                let _ = self.dash_tx.send(payload);
            }
        }

        let (status, reason) = if self.execution_state_journal_error.is_some() {
            ("BLOCKED", "execution_state_journal_unavailable")
        } else {
            match self.state {
                SystemState::Trading
                    if self.trading_mode == "paper" || self.private_stream_quorum_ready() =>
                {
                    ("READY", "current execution state reconciled")
                }
                SystemState::Trading => ("BLOCKED", "private stream quorum is not ready"),
                SystemState::Reconciling => {
                    ("RECONCILING", "exchange reconciliation is in progress")
                }
                SystemState::Disconnected => {
                    ("DISCONNECTED", "private user-data stream is disconnected")
                }
            }
        };
        self.emit_execution_readiness(status, reason);
    }

    async fn check_circuit_breakers(&mut self) -> bool {
        // Circuit breaker 1: Python brain disconnected (staleness)
        if self.last_brain_ping.elapsed() > Duration::from_secs(12 * 60) {
            warn!(
                "CRITICAL: Python brain has not sent instructions in > 12 mins. Halting trading."
            );
            return true;
        }

        // Circuit breaker 2: gross exposure (from env)
        if self.current_gross_exposure_usd > self.max_gross_exposure_usd {
            warn!(
                "CRITICAL: Gross exposure ${:.0} exceeds limit ${:.0}! Halting new risk.",
                self.current_gross_exposure_usd, self.max_gross_exposure_usd
            );
            return true;
        }

        // Circuit breaker 3: collateral engine margin check
        if !self.tracked_positions.is_empty() {
            let total_spot_notional: f64 = self
                .tracked_positions
                .values()
                .filter_map(|position| position.spot.as_ref())
                .filter(|leg| leg.side == "LONG")
                .map(|leg| leg.last_mark_price * leg.quantity)
                .sum();
            let total_perp_notional: f64 = self
                .tracked_positions
                .values()
                .filter_map(|position| position.perp.as_ref())
                .filter(|leg| leg.side == "SHORT")
                .map(|leg| leg.last_mark_price * leg.quantity)
                .sum();
            let total_upnl: f64 = self
                .tracked_positions
                .values()
                .map(|position| {
                    position
                        .spot
                        .as_ref()
                        .map(|leg| leg.unrealized_pnl)
                        .unwrap_or(0.0)
                        + position
                            .perp
                            .as_ref()
                            .map(|leg| leg.unrealized_pnl)
                            .unwrap_or(0.0)
                })
                .sum();

            let unified_equity = self.account_equity_usd + total_upnl;
            if unified_equity <= 0.0 {
                warn!("CRITICAL: Unified equity is zero or negative! Kill switch.");
                return true;
            }

            let directional_risk = (total_spot_notional - total_perp_notional).abs();
            let uni_mmr = directional_risk * 0.004 / unified_equity;
            if uni_mmr >= 0.8 {
                warn!(
                    "CRITICAL: uniMMR {:.4} exceeds danger threshold 0.8! Halting.",
                    uni_mmr
                );
                return true;
            }
        }

        // Circuit breaker 4: direction-aware adverse basis deviation stop.
        // Long-spot/short-perp is hurt only by widening; short-spot/long-perp
        // is hurt only by contraction. Favorable convergence must never trip
        // an emergency flatten.
        for (symbol, position) in &self.tracked_positions {
            if let (Some(spot_pos), Some(perp_pos)) =
                (position.spot.as_ref(), position.perp.as_ref())
            {
                let Some(adverse_deviation_bps) =
                    Self::adverse_basis_deviation_bps(spot_pos, perp_pos)
                else {
                    continue;
                };

                if adverse_deviation_bps > self.basis_deviation_stop_bps {
                    warn!(
                        "CRITICAL: Adverse basis move {:.1}bps exceeds stop {:.0}bps for {}! Emergency flatten.",
                        adverse_deviation_bps, self.basis_deviation_stop_bps, symbol
                    );
                    return true;
                }
            }
        }

        false
    }

    fn side_is_long(side: &str) -> Option<bool> {
        match side.to_ascii_uppercase().as_str() {
            "LONG" | "BUY" => Some(true),
            "SHORT" | "SELL" => Some(false),
            _ => None,
        }
    }

    fn adverse_basis_deviation_bps(
        spot: &TrackedLegPosition,
        perp: &TrackedLegPosition,
    ) -> Option<f64> {
        if spot.entry_price <= 0.0
            || perp.entry_price <= 0.0
            || spot.last_mark_price <= 0.0
            || perp.last_mark_price <= 0.0
        {
            return None;
        }
        let entry_basis = (perp.entry_price - spot.entry_price) / spot.entry_price;
        let current_basis = (perp.last_mark_price - spot.last_mark_price) / spot.last_mark_price;
        let raw_move = current_basis - entry_basis;
        match (
            Self::side_is_long(&spot.side),
            Self::side_is_long(&perp.side),
        ) {
            (Some(true), Some(false)) => Some(raw_move.max(0.0) * 10_000.0),
            (Some(false), Some(true)) => Some((-raw_move).max(0.0) * 10_000.0),
            // A same-direction pair is not delta neutral. Preserve the old
            // conservative breaker behavior for malformed/restored state.
            (Some(_), Some(_)) => Some(raw_move.abs() * 10_000.0),
            _ => None,
        }
    }

    pub fn maker_fill_rate(&self) -> f64 {
        let total = self.maker_fills + self.taker_fills;
        if total == 0 {
            return 0.0;
        }
        self.maker_fills as f64 / total as f64
    }

    fn update_mid_price(&mut self, symbol: &str, mid_price: f64) {
        let sym_upper = symbol.to_uppercase();
        let history = self
            .mid_price_history
            .entry(sym_upper)
            .or_insert_with(|| VecDeque::with_capacity(64));
        if history.len() >= 64 {
            history.pop_front();
        }
        history.push_back(mid_price);
    }

    fn recent_volatility_bps(&self, symbol: &str) -> f64 {
        let sym_upper = symbol.to_uppercase();
        let Some(history) = self.mid_price_history.get(&sym_upper) else {
            return 0.0;
        };
        if history.len() < 2 {
            return 0.0;
        }
        let returns: Vec<f64> = history
            .iter()
            .zip(history.iter().skip(1))
            .map(|(prev, curr)| ((curr - prev) / prev) * 10_000.0)
            .collect();
        let n = returns.len() as f64;
        let mean = returns.iter().sum::<f64>() / n;
        let variance = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / n;
        variance.sqrt()
    }

    fn adaptive_legging_timeout_ms(&self, symbol: &str) -> u64 {
        let vol = self.recent_volatility_bps(symbol);
        let raw = 300.0 - vol * 20.0;
        raw.clamp(50.0, 500.0) as u64
    }

    fn unhedged_notional(&self, chase: &ChaseState) -> f64 {
        let spot_price = chase
            .spot_fill_price
            .unwrap_or(chase.expected_spot_price)
            .max(0.0);
        let perp_price = chase
            .futures_fill_price
            .unwrap_or(chase.expected_fut_price)
            .max(0.0);
        (chase.spot_cumulative_filled * spot_price - chase.futures_cumulative_filled * perp_price)
            .abs()
    }

    fn unhedged_budget_remaining_ms(&self, chase: &ChaseState) -> Option<u64> {
        let symbol = chase.symbol.to_uppercase();
        let budget = self
            .chase_unhedged_budgets
            .get(&symbol)
            .copied()
            .unwrap_or(crate::ipc::DEFAULT_MAX_UNHEDGED_NOTIONAL_MS);
        let unhedged_notional = self.unhedged_notional(chase);
        if !budget.is_finite() || budget <= 0.0 || unhedged_notional <= 1e-9 {
            return None;
        }
        let budget_window_ms = (budget / unhedged_notional).floor().max(1.0) as u64;
        let elapsed_ms = self
            .chase_unhedged_started_at_ms
            .get(&symbol)
            .map(|started| Self::current_time_ms().saturating_sub(*started).max(0) as u64)
            .unwrap_or(0);
        Some(budget_window_ms.saturating_sub(elapsed_ms))
    }

    fn bounded_legging_timeout_ms(&self, chase: &ChaseState) -> u64 {
        let adaptive_ms = self.adaptive_legging_timeout_ms(&chase.symbol);
        self.unhedged_budget_remaining_ms(chase)
            .map(|remaining| adaptive_ms.min(remaining.max(1)))
            .unwrap_or(adaptive_ms)
    }

    fn schedule_legging_timeout(&self, client_order_id: String, timeout_ms: u64) {
        let tx = self.engine_tx.clone();
        tokio::spawn(async move {
            sleep(Duration::from_millis(timeout_ms.max(1))).await;
            let _ = tx.send(EngineEvent::LeggingTimeout(client_order_id)).await;
        });
    }

    fn current_time_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }

    fn recovered_submission_is_resting_new(body: &str) -> bool {
        serde_json::from_str::<Value>(body)
            .ok()
            .and_then(|value| {
                let status = value.get("status")?.as_str()?;
                let executed_qty = value.get("executedQty").and_then(|node| {
                    node.as_f64()
                        .or_else(|| node.as_str().and_then(|raw| raw.parse::<f64>().ok()))
                })?;
                Some(status == "NEW" && executed_qty.is_finite() && executed_qty.abs() <= 1e-12)
            })
            .unwrap_or(false)
    }

    fn paper_limit_crossed(side: TradeSide, limit_price: f64, top: TopOfBook) -> bool {
        match side {
            TradeSide::Buy => top.ask_price > 0.0 && top.ask_price <= limit_price,
            TradeSide::Sell => top.bid_price > 0.0 && top.bid_price >= limit_price,
        }
    }

    fn paper_market_fill_price(
        &self,
        symbol: &str,
        market: MarketType,
        side: TradeSide,
        fallback: f64,
    ) -> f64 {
        let sym_upper = symbol.to_uppercase();
        let top = match market {
            MarketType::Spot => self.spot_top_cache.get(&sym_upper).copied(),
            MarketType::Perp => self.perp_top_cache.get(&sym_upper).copied(),
        };
        match (top, side) {
            (Some(top), TradeSide::Buy) if top.ask_price > 0.0 => top.ask_price,
            (Some(top), TradeSide::Sell) if top.bid_price > 0.0 => top.bid_price,
            _ => fallback,
        }
    }

    fn market_order_slippage_bps(
        &self,
        symbol: &str,
        market: MarketType,
        side: TradeSide,
        expected_price: f64,
    ) -> Option<f64> {
        if expected_price <= 0.0 {
            return None;
        }
        let fill_price = self.paper_market_fill_price(symbol, market, side, 0.0);
        if fill_price <= 0.0 {
            return None;
        }
        let adverse_slippage = match side {
            TradeSide::Buy => (fill_price - expected_price) / expected_price,
            TradeSide::Sell => (expected_price - fill_price) / expected_price,
        };
        Some(adverse_slippage.max(0.0) * 10_000.0)
    }

    async fn emit_paper_order_fill(
        &mut self,
        client_order_id: String,
        symbol: String,
        filled_qty: f64,
        fill_price: f64,
        maker: bool,
        execution_type: &str,
    ) {
        if fill_price <= 0.0 {
            warn!(
                "Skipping synthetic paper fill for {} {} because fill_price is invalid ({})",
                symbol, client_order_id, fill_price
            );
            return;
        }

        let _ = self
            .engine_tx
            .send(EngineEvent::Ws(WsEvent::OrderUpdate {
                client_order_id,
                symbol,
                status: "FILLED".to_string(),
                filled_qty,
                cumulative_filled_qty: Some(filled_qty),
                avg_fill_price: Some(fill_price),
                last_fill_price: Some(fill_price),
                cumulative_quote_qty: Some(fill_price * filled_qty),
                commission: None,
                commission_asset: None,
                realized_pnl: None,
                maker: Some(maker),
                execution_type: Some(execution_type.to_string()),
                event_time_ms: Some(Self::current_time_ms()),
                maker_fills: None,
                taker_fills: None,
                market: None,
                side: None,
                order_id: None,
                trade_id: None,
                account_id: None,
                environment: None,
                strategy_id: None,
                cycle_id: None,
                intent_id: None,
                leg_id: None,
                config_version_hash: None,
            }))
            .await;
    }

    async fn maybe_fill_paper_resting_leg(&mut self, symbol: &str, market: MarketType) {
        if self.trading_mode != "paper" {
            return;
        }

        let sym_upper = symbol.to_uppercase();
        let Some(chase) = self.chase_states.get(&sym_upper).cloned() else {
            return;
        };
        if !matches!(
            chase.phase,
            ChasePhase::DualMakerPlaced | ChasePhase::LegFilledWaiting(_)
        ) {
            return;
        }

        let top = match market {
            MarketType::Spot => self.spot_top_cache.get(&sym_upper).copied(),
            MarketType::Perp => self.perp_top_cache.get(&sym_upper).copied(),
        };
        let Some(top) = top else {
            return;
        };

        let (client_order_id, side, fallback_price) = match market {
            MarketType::Spot => (
                chase.spot_client_order_id.clone(),
                chase.spot_side,
                chase.expected_spot_price,
            ),
            MarketType::Perp => (
                chase.futures_client_order_id.clone(),
                chase.futures_side,
                chase.expected_fut_price,
            ),
        };

        let maybe_fill = {
            let Some(order) = self.internal_orders.get_mut(&client_order_id) else {
                return;
            };
            if order.status != "NEW" {
                return;
            }

            let limit_price = order.limit_price.unwrap_or(fallback_price);
            if limit_price <= 0.0 || !Self::paper_limit_crossed(side, limit_price, top) {
                return;
            }

            order.status = "FILLED_PENDING".to_string();
            Some((
                client_order_id.clone(),
                chase.symbol.clone(),
                chase.quantity,
                limit_price,
            ))
        };

        let Some((client_order_id, symbol, quantity, fill_price)) = maybe_fill else {
            return;
        };

        self.emit_paper_order_fill(
            client_order_id,
            symbol,
            quantity,
            fill_price,
            true,
            "PAPER_RESTING_CROSS_FILL",
        )
        .await;
    }

    fn trade_side_label(side: TradeSide) -> String {
        match side {
            TradeSide::Buy => "LONG".to_string(),
            TradeSide::Sell => "SHORT".to_string(),
        }
    }

    fn precision_for_increment(increment: f64) -> usize {
        let rendered = format!("{:.16}", increment.abs());
        rendered
            .trim_end_matches('0')
            .split('.')
            .nth(1)
            .map(|fractional| fractional.len())
            .unwrap_or(0)
    }

    fn round_down_to_step(value: f64, step: f64) -> f64 {
        if value <= 0.0 || step <= 0.0 {
            return value.max(0.0);
        }

        let rounded = (value / step).floor() * step;
        let precision = Self::precision_for_increment(step);
        let scale = 10f64.powi(precision as i32);
        (rounded * scale).round() / scale
    }

    fn quantize_price(value: f64, tick_size: f64, side: TradeSide) -> f64 {
        if value <= 0.0 || tick_size <= 0.0 {
            return value.max(0.0);
        }

        let scaled = value / tick_size;
        let rounded = match side {
            TradeSide::Buy => scaled.floor(),
            TradeSide::Sell => scaled.ceil(),
        } * tick_size;
        let precision = Self::precision_for_increment(tick_size);
        let scale = 10f64.powi(precision as i32);
        (rounded * scale).round() / scale
    }

    fn format_with_increment(value: f64, increment: f64) -> String {
        let precision = Self::precision_for_increment(increment);
        format!("{:.*}", precision, value)
    }

    fn symbol_info(&self, symbol: &str) -> crate::binance_rest::ExchangeSymbolInfo {
        self.exchange_info
            .get(symbol)
            .cloned()
            .unwrap_or(crate::binance_rest::ExchangeSymbolInfo {
                symbol: symbol.to_string(),
                spot_tick_size: 0.1,
                spot_step_size: 0.1,
                spot_max_qty: f64::MAX,
                spot_min_notional: 5.0,
                futures_tick_size: 0.1,
                futures_step_size: 0.1,
                futures_max_qty: f64::MAX,
                futures_min_notional: 5.0,
            })
    }

    fn normalize_quantity_for_market(
        &self,
        symbol: &str,
        market: MarketType,
        requested_quantity: f64,
    ) -> Option<f64> {
        let info = self.symbol_info(symbol);
        let step_size = match market {
            MarketType::Spot => info.spot_step_size,
            MarketType::Perp => info.futures_step_size,
        };
        let normalized = Self::round_down_to_step(requested_quantity, step_size.max(0.00000001));
        if normalized > 0.0 {
            Some(normalized)
        } else {
            None
        }
    }

    fn format_quantity_for_market(
        &self,
        symbol: &str,
        market: MarketType,
        quantity: f64,
    ) -> String {
        let info = self.symbol_info(symbol);
        let step_size = match market {
            MarketType::Spot => info.spot_step_size,
            MarketType::Perp => info.futures_step_size,
        };
        let normalized = Self::round_down_to_step(quantity, step_size.max(0.00000001));
        Self::format_with_increment(normalized, step_size.max(0.00000001))
    }

    fn refresh_leg_pnl(leg: &mut TrackedLegPosition) {
        leg.unrealized_pnl = match leg.side.as_str() {
            "LONG" => (leg.last_mark_price - leg.entry_price) * leg.quantity,
            "SHORT" => (leg.entry_price - leg.last_mark_price) * leg.quantity,
            _ => 0.0,
        };
    }

    fn recompute_gross_exposure(&mut self) {
        self.current_gross_exposure_usd = self
            .tracked_positions
            .values()
            .map(|position| {
                let spot_notional = position
                    .spot
                    .as_ref()
                    .map(|leg| leg.last_mark_price * leg.quantity)
                    .unwrap_or(0.0);
                let perp_notional = position
                    .perp
                    .as_ref()
                    .map(|leg| leg.last_mark_price * leg.quantity)
                    .unwrap_or(0.0);
                spot_notional + perp_notional
            })
            .sum();
    }

    fn emit_position_snapshot(&self, symbol: &str) {
        let Some(position) = self.tracked_positions.get(symbol) else {
            return;
        };

        let unrealized_pnl = position
            .spot
            .as_ref()
            .map(|leg| leg.unrealized_pnl)
            .unwrap_or(0.0)
            + position
                .perp
                .as_ref()
                .map(|leg| leg.unrealized_pnl)
                .unwrap_or(0.0);
        let basis_bps = match (position.spot.as_ref(), position.perp.as_ref()) {
            (Some(spot), Some(perp)) if spot.last_mark_price > 0.0 => Some(
                ((perp.last_mark_price - spot.last_mark_price) / spot.last_mark_price) * 10_000.0,
            ),
            _ => None,
        };

        let pnl_event = serde_json::json!({
            "event": "PositionPnL",
            "symbol": symbol,
            "unrealized_pnl": unrealized_pnl,
            "spot_mark_price": position.spot.as_ref().map(|leg| leg.last_mark_price),
            "perp_mark_price": position.perp.as_ref().map(|leg| leg.last_mark_price),
            "basis_bps": basis_bps,
        });
        let _ = self
            .dash_tx
            .send(rmp_serde::to_vec_named(&pnl_event).unwrap());
    }

    fn restore_tracked_position(
        &mut self,
        symbol: &str,
        instruction: &crate::ipc::AlphaInstruction,
    ) {
        let direction = instruction
            .direction
            .clone()
            .unwrap_or_else(|| "long".to_string())
            .to_lowercase();
        let perp_qty = instruction
            .perp_quantity
            .unwrap_or(instruction.quantity)
            .max(0.0);
        let spot_qty = instruction
            .spot_quantity
            .unwrap_or_else(|| if direction == "long" { perp_qty } else { 0.0 })
            .max(0.0);

        if spot_qty <= 0.0 && perp_qty <= 0.0 {
            warn!(
                "Skipping RESTORE_POSITION for {} because both leg quantities are non-positive",
                symbol
            );
            return;
        }

        let spot_entry = instruction
            .spot_entry_price
            .or(instruction.perp_entry_price)
            .unwrap_or(0.0);
        let perp_entry = instruction
            .perp_entry_price
            .or(instruction.spot_entry_price)
            .unwrap_or(0.0);
        let spot_mark = instruction.spot_mark_price.unwrap_or(spot_entry).max(0.0);
        let perp_mark = instruction.perp_mark_price.unwrap_or(perp_entry).max(0.0);

        if spot_mark > 0.0 {
            self.spot_mid_cache.insert(symbol.to_string(), spot_mark);
        }
        if perp_mark > 0.0 {
            self.perp_mid_cache.insert(symbol.to_string(), perp_mark);
        }

        let mut restored = TrackedPosition {
            symbol: symbol.to_string(),
            ..TrackedPosition::default()
        };

        if spot_qty > 0.0 {
            let mut spot_leg = TrackedLegPosition {
                side: if direction == "short" {
                    "SHORT".to_string()
                } else {
                    "LONG".to_string()
                },
                entry_price: spot_entry.max(0.0),
                quantity: spot_qty,
                unrealized_pnl: 0.0,
                last_mark_price: if spot_mark > 0.0 {
                    spot_mark
                } else {
                    spot_entry.max(0.0)
                },
            };
            Self::refresh_leg_pnl(&mut spot_leg);
            restored.spot = Some(spot_leg);
        }

        if perp_qty > 0.0 {
            let mut perp_leg = TrackedLegPosition {
                side: if direction == "short" {
                    "LONG".to_string()
                } else {
                    "SHORT".to_string()
                },
                entry_price: perp_entry.max(0.0),
                quantity: perp_qty,
                unrealized_pnl: 0.0,
                last_mark_price: if perp_mark > 0.0 {
                    perp_mark
                } else {
                    perp_entry.max(0.0)
                },
            };
            Self::refresh_leg_pnl(&mut perp_leg);
            restored.perp = Some(perp_leg);
        }

        self.tracked_positions.insert(symbol.to_string(), restored);
        self.recompute_gross_exposure();
        self.emit_position_snapshot(symbol);
        info!(
            "Restored tracked position for {} (direction={}, spot_qty={:.8}, perp_qty={:.8})",
            symbol, direction, spot_qty, perp_qty
        );
    }

    fn apply_mark_price(&mut self, symbol: &str, market: MarketType, mark_price: f64) {
        let sym_upper = symbol.to_uppercase();
        match market {
            MarketType::Spot => {
                self.spot_mid_cache.insert(sym_upper.clone(), mark_price);
            }
            MarketType::Perp => {
                self.perp_mid_cache.insert(sym_upper.clone(), mark_price);
            }
        }

        if let Some(position) = self.tracked_positions.get_mut(&sym_upper) {
            match market {
                MarketType::Spot => {
                    if let Some(leg) = position.spot.as_mut() {
                        leg.last_mark_price = mark_price;
                        Self::refresh_leg_pnl(leg);
                    }
                }
                MarketType::Perp => {
                    if let Some(leg) = position.perp.as_mut() {
                        leg.last_mark_price = mark_price;
                        Self::refresh_leg_pnl(leg);
                    }
                }
            }
        }

        self.recompute_gross_exposure();
        self.emit_position_snapshot(&sym_upper);
    }

    fn apply_fill_to_position(
        &mut self,
        symbol: &str,
        market: MarketType,
        side: TradeSide,
        filled_qty: f64,
        fill_price: f64,
        is_exit: bool,
    ) {
        if filled_qty <= 0.0 {
            return;
        }

        let sym_upper = symbol.to_uppercase();
        let last_mark_price = match market {
            MarketType::Spot => self
                .spot_mid_cache
                .get(&sym_upper)
                .copied()
                .unwrap_or(fill_price),
            MarketType::Perp => self
                .perp_mid_cache
                .get(&sym_upper)
                .copied()
                .unwrap_or(fill_price),
        };
        let side_label = Self::trade_side_label(side);
        let remove_symbol = {
            let position = self
                .tracked_positions
                .entry(sym_upper.clone())
                .or_insert_with(|| TrackedPosition {
                    symbol: sym_upper.clone(),
                    ..TrackedPosition::default()
                });
            let leg_slot = match market {
                MarketType::Spot => &mut position.spot,
                MarketType::Perp => &mut position.perp,
            };

            if is_exit {
                if let Some(existing) = leg_slot.as_mut() {
                    if filled_qty >= existing.quantity - 0.00000001 {
                        *leg_slot = None;
                    } else {
                        existing.quantity -= filled_qty;
                        existing.last_mark_price = last_mark_price;
                        Self::refresh_leg_pnl(existing);
                    }
                } else {
                    warn!(
                        "Received exit fill for {} {:?} with no tracked leg to reduce",
                        sym_upper, market
                    );
                }
            } else if let Some(existing) = leg_slot.as_mut() {
                let new_total_qty = existing.quantity + filled_qty;
                if new_total_qty > 0.0 {
                    existing.entry_price = ((existing.entry_price * existing.quantity)
                        + (fill_price * filled_qty))
                        / new_total_qty;
                    existing.quantity = new_total_qty;
                } else {
                    existing.entry_price = fill_price;
                    existing.quantity = filled_qty;
                }
                existing.side = side_label;
                existing.last_mark_price = last_mark_price;
                Self::refresh_leg_pnl(existing);
            } else {
                *leg_slot = Some(TrackedLegPosition {
                    side: side_label,
                    entry_price: fill_price,
                    quantity: filled_qty,
                    unrealized_pnl: 0.0,
                    last_mark_price,
                });
                if let Some(new_leg) = leg_slot.as_mut() {
                    Self::refresh_leg_pnl(new_leg);
                }
            }

            position.spot.is_none() && position.perp.is_none()
        };

        if remove_symbol {
            self.tracked_positions.remove(&sym_upper);
        }

        self.recompute_gross_exposure();
        self.emit_position_snapshot(&sym_upper);
    }

    fn emit_maker_fill_rate(&self) {
        let fill_event = serde_json::json!({
            "event": "MakerFillRate",
            "maker_fills": self.maker_fills,
            "taker_fills": self.taker_fills,
            "rate": self.maker_fill_rate(),
        });
        let _ = self
            .dash_tx
            .send(rmp_serde::to_vec_named(&fill_event).unwrap());
    }

    fn emit_intent_ack(&self, receipt: &IntentReceipt, replay: bool) {
        let event = serde_json::json!({
            "event": "IntentAck",
            "schema_version": crate::ipc::EXECUTION_PROTOCOL_VERSION,
            "intent_id": receipt.intent_id,
            "producer_id": receipt.producer_id,
            "sequence": receipt.sequence,
            "account_id": receipt.account_id,
            "environment": receipt.environment,
            "strategy_id": receipt.strategy_id,
            "cycle_id": receipt.cycle_id,
            "config_version_hash": receipt.config_version_hash,
            "spot_leg_id": receipt.spot_leg_id,
            "perp_leg_id": receipt.perp_leg_id,
            "spot_client_order_id": receipt.spot_client_order_id,
            "perp_client_order_id": receipt.perp_client_order_id,
            "command_hash": receipt.command_hash,
            "ack_status": receipt.ack_status,
            "reason": receipt.reason,
            "event_time_ms": receipt.updated_at_ms,
            "replay": replay,
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&event) {
            let _ = self.dash_tx.send(payload);
        }
    }

    fn emit_raw_intent_ack(
        &self,
        instruction: &crate::ipc::AlphaInstruction,
        ack_status: &str,
        reason: &str,
    ) {
        let event = serde_json::json!({
            "event": "IntentAck",
            "schema_version": crate::ipc::EXECUTION_PROTOCOL_VERSION,
            "intent_id": instruction.intent_id,
            "producer_id": instruction.producer_id,
            "sequence": instruction.sequence,
            "account_id": instruction.account_id,
            "environment": instruction.environment,
            "strategy_id": instruction.strategy_id,
            "cycle_id": instruction.cycle_id,
            "config_version_hash": instruction.config_version_hash,
            "spot_leg_id": instruction.spot_leg_id,
            "perp_leg_id": instruction.perp_leg_id,
            "spot_client_order_id": instruction.spot_client_order_id,
            "perp_client_order_id": instruction.perp_client_order_id,
            "command_hash": instruction.command_hash,
            "ack_status": ack_status,
            "reason": reason,
            "event_time_ms": Self::current_time_ms(),
            "replay": false,
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&event) {
            let _ = self.dash_tx.send(payload);
        }
    }

    fn emit_config_ack(&self, ack: &ConfigAck) {
        if let Ok(payload) = rmp_serde::to_vec_named(ack) {
            let _ = self.dash_tx.send(payload);
        }
    }

    fn active_per_symbol_notional_cap_usd(&self) -> f64 {
        self.config_consensus
            .active()
            .and_then(|snapshot| snapshot.per_symbol_notional_cap_usd.parse::<f64>().ok())
            .filter(|value| value.is_finite() && *value > 0.0)
            .unwrap_or(self.compiled_max_per_symbol_notional_usd)
            .min(self.compiled_max_per_symbol_notional_usd)
    }

    /// Apply one canonical Python config snapshot through the same durable
    /// receive/ACK journal as risk-changing order commands. A replay after a
    /// Rust restart deliberately re-applies the snapshot because consensus is
    /// in-memory and starts fail-closed in every new process.
    fn handle_config_sync_instruction(&mut self, instruction: crate::ipc::AlphaInstruction) {
        let now_ms = Self::current_time_ms();
        let active_hash = self.config_consensus.applied_hash().map(str::to_string);
        if let Some(reason) = instruction.protocol_error(now_ms) {
            self.emit_config_ack(&ConfigAck::rejected(
                &instruction,
                active_hash.as_deref(),
                reason,
                now_ms,
                false,
            ));
            return;
        }

        let replay = match self.intent_journal.as_mut() {
            Some(journal) => match journal.receive(&instruction, now_ms) {
                Ok(ReceiptDecision::New(receipt)) => {
                    self.emit_intent_ack(&receipt, false);
                    false
                }
                Ok(ReceiptDecision::Replay(receipt)) => {
                    self.emit_intent_ack(&receipt, true);
                    if receipt.ack_status == "REJECTED" {
                        self.emit_config_ack(&ConfigAck::rejected(
                            &instruction,
                            active_hash.as_deref(),
                            &receipt.reason,
                            now_ms,
                            true,
                        ));
                        return;
                    }
                    true
                }
                Ok(ReceiptDecision::Conflict) => {
                    self.emit_config_ack(&ConfigAck::rejected(
                        &instruction,
                        active_hash.as_deref(),
                        "duplicate_intent_conflict",
                        now_ms,
                        false,
                    ));
                    return;
                }
                Ok(ReceiptDecision::NonMonotonicSequence) => {
                    self.emit_config_ack(&ConfigAck::rejected(
                        &instruction,
                        active_hash.as_deref(),
                        "non_monotonic_sequence",
                        now_ms,
                        false,
                    ));
                    return;
                }
                Err(err) => {
                    error!("Execution intent journal receive failed for CONFIG_SYNC: {err}");
                    self.intent_journal_error = Some(err);
                    self.intent_journal = None;
                    self.emit_config_ack(&ConfigAck::rejected(
                        &instruction,
                        active_hash.as_deref(),
                        "intent_journal_unavailable",
                        now_ms,
                        false,
                    ));
                    return;
                }
            },
            None => {
                self.emit_config_ack(&ConfigAck::rejected(
                    &instruction,
                    active_hash.as_deref(),
                    "intent_journal_unavailable",
                    now_ms,
                    false,
                ));
                return;
            }
        };

        let intent_id = instruction.intent_id.clone().unwrap_or_default();
        if !self.transition_intent_ack(&intent_id, "VALIDATED", "") {
            self.emit_config_ack(&ConfigAck::rejected(
                &instruction,
                active_hash.as_deref(),
                "intent_journal_unavailable",
                now_ms,
                replay,
            ));
            return;
        }

        let previous_consensus = self.config_consensus.clone();
        let previous_max_gross = self.max_gross_exposure_usd;
        let snapshot = match self.config_consensus.apply(&instruction) {
            Ok(snapshot) => snapshot,
            Err(err) => {
                let reason = err.code();
                let _ = self.transition_intent_ack(&intent_id, "REJECTED", reason);
                self.emit_config_ack(&ConfigAck::rejected(
                    &instruction,
                    active_hash.as_deref(),
                    reason,
                    now_ms,
                    replay,
                ));
                return;
            }
        };
        let Some(synced_max_gross) = snapshot
            .max_gross_exposure_usd
            .parse::<f64>()
            .ok()
            .filter(|value| value.is_finite() && *value > 0.0)
        else {
            self.config_consensus = previous_consensus;
            let reason = "invalid_risk_limit";
            let _ = self.transition_intent_ack(&intent_id, "REJECTED", reason);
            self.emit_config_ack(&ConfigAck::rejected(
                &instruction,
                active_hash.as_deref(),
                reason,
                now_ms,
                replay,
            ));
            return;
        };
        self.max_gross_exposure_usd = self.compiled_max_gross_exposure_usd.min(synced_max_gross);

        if !self.transition_intent_ack(&intent_id, "TERMINAL", "config_applied") {
            self.config_consensus = previous_consensus;
            self.max_gross_exposure_usd = previous_max_gross;
            self.emit_config_ack(&ConfigAck::rejected(
                &instruction,
                active_hash.as_deref(),
                "intent_journal_unavailable",
                now_ms,
                replay,
            ));
            return;
        }
        info!(
            "Applied CONFIG_SYNC {} (pause_new_entries={}, per_symbol_cap=${}, max_gross=${})",
            snapshot.config_hash,
            snapshot.pause_new_entries,
            snapshot.per_symbol_notional_cap_usd,
            self.max_gross_exposure_usd,
        );
        self.emit_config_ack(&ConfigAck::applied(
            &instruction,
            &snapshot,
            Self::current_time_ms(),
            replay,
        ));
    }

    fn transition_intent_ack(&mut self, intent_id: &str, status: &str, reason: &str) -> bool {
        let Some(journal) = self.intent_journal.as_mut() else {
            error!("Cannot transition intent {intent_id} to {status}: journal unavailable");
            return false;
        };
        match journal.transition(intent_id, status, reason, Self::current_time_ms()) {
            Ok(Some(receipt)) => {
                self.emit_intent_ack(&receipt, false);
                true
            }
            Ok(None) => {
                error!("Cannot transition unknown intent {intent_id} to {status}");
                false
            }
            Err(err) => {
                error!("Could not durably transition intent {intent_id} to {status}: {err}");
                self.intent_journal_error = Some(err);
                self.intent_journal = None;
                false
            }
        }
    }

    fn reject_instruction(
        &mut self,
        instruction: &crate::ipc::AlphaInstruction,
        symbol: Option<&str>,
        reason: &str,
        has_receipt: bool,
    ) {
        let intent_id = instruction.intent_id.clone().unwrap_or_default();
        if has_receipt && !intent_id.is_empty() {
            if !self.transition_intent_ack(&intent_id, "REJECTED", reason) {
                self.emit_raw_intent_ack(instruction, "REJECTED", reason);
            }
        } else if instruction.schema_version.is_some() {
            self.emit_raw_intent_ack(instruction, "REJECTED", reason);
        }
        let rejected_event = serde_json::json!({
            "event": "OrderRejected",
            "symbol": symbol.map(str::to_uppercase),
            "intent": instruction.intent,
            "intent_id": instruction.intent_id,
            "reason": reason,
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&rejected_event) {
            let _ = self.dash_tx.send(payload);
        }
    }

    fn emit_cycle_order_update(
        &mut self,
        chase: &ChaseState,
        status: &str,
        client_order_id: &str,
        filled_qty: f64,
        maker: bool,
        execution_type: &str,
    ) -> bool {
        let resolved_client_order_id = if client_order_id.is_empty() {
            chase.cycle_client_order_id()
        } else {
            client_order_id
        };
        let cycle_fill_price = chase.cycle_fill_price();
        let lineage = self.order_lineage.get(resolved_client_order_id);
        let cycle_event = serde_json::json!({
            "event": "OrderUpdate",
            "symbol": &chase.symbol,
            "status": status,
            "filled_qty": filled_qty,
            "client_order_id": resolved_client_order_id,
            "avg_fill_price": cycle_fill_price,
            "last_fill_price": cycle_fill_price,
            "cumulative_quote_qty": serde_json::Value::Null,
            "commission": serde_json::Value::Null,
            "commission_asset": serde_json::Value::Null,
            "realized_pnl": serde_json::Value::Null,
            "maker": maker,
            "execution_type": execution_type,
            "event_time_ms": SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0),
            "spot_fill_price": chase.spot_fill_price.unwrap_or(chase.expected_spot_price),
            "perp_fill_price": chase.futures_fill_price.unwrap_or(chase.expected_fut_price),
            "account_id": lineage.and_then(|v| v.account_id.clone()),
            "environment": lineage.and_then(|v| v.environment.clone()),
            "strategy_id": lineage.and_then(|v| v.strategy_id.clone()),
            "cycle_id": lineage.and_then(|v| v.cycle_id.clone()),
            "intent_id": lineage.and_then(|v| v.intent_id.clone()),
            "leg_id": lineage.and_then(|v| v.leg_id.clone()),
            "config_version_hash": lineage.and_then(|v| v.config_version_hash.clone()),
        });
        if let Ok(vec) = rmp_serde::to_vec_named(&cycle_event) {
            let _ = self.dash_tx.send(vec);
        }
        let terminal_status = if status == "FILLED" && execution_type == "FILLED_CYCLE" {
            Some(("TERMINAL", "filled_cycle"))
        } else if matches!(status, "REJECTED" | "CANCELED" | "CANCELLED" | "EXPIRED") {
            Some(("REJECTED", execution_type))
        } else {
            None
        };
        if let Some((ack_status, reason)) = terminal_status {
            if let Some(intent_id) = self.chase_intent_ids.get(&chase.symbol).cloned() {
                if !self.transition_intent_ack(&intent_id, ack_status, reason) {
                    error!(
                        "Refusing to forget completed chase {} because terminal intent ACK was not durable",
                        chase.symbol
                    );
                    return false;
                }
                self.chase_intent_ids.remove(&chase.symbol);
            }
        }
        true
    }

    fn require_chase_reconciliation(
        &mut self,
        symbol: &str,
        mut chase: ChaseState,
        client_order_id: &str,
        reason: &str,
    ) {
        error!(
            "Execution reconciliation required for {} {}: {} (spot={:.12}/{:.12}, perp={:.12}/{:.12})",
            symbol,
            client_order_id,
            reason,
            chase.spot_cumulative_filled,
            chase.spot_quantity,
            chase.futures_cumulative_filled,
            chase.perp_quantity,
        );
        chase.phase = ChasePhase::ReconciliationRequired;
        let _ = self.store_chase_state(
            symbol.to_uppercase(),
            chase.clone(),
            "chase marked reconciliation-required",
        );
        if self.trading_mode != "paper" {
            self.state = SystemState::Reconciling;
        }
        self.emit_cycle_order_update(
            &chase,
            "RECONCILIATION_REQUIRED",
            client_order_id,
            0.0,
            false,
            reason,
        );
    }

    pub async fn run(&mut self) {
        info!(
            "OrderManager task started. Max exposure: ${:.0}, Account equity: ${:.0}",
            self.max_gross_exposure_usd, self.account_equity_usd
        );

        info!("Fetching exchange info to populate tick sizes...");
        match self.binance_rest.get_exchange_info().await {
            Ok(info) => {
                self.exchange_info = info;
                self.exchange_info_updated_at = Some(Instant::now());
                info!(
                    "Fetched exchange info for {} symbols.",
                    self.exchange_info.len()
                );
            }
            Err(e) => {
                error!(
                    "Failed to fetch exchange info on startup: {}. Falling back to 0.1 tick sizes.",
                    e
                );
            }
        }

        let engine_tx_for_audit = self.engine_tx.clone();
        let audit_interval_s = std::env::var("RUST_POSITION_AUDIT_INTERVAL_S")
            .ok()
            .and_then(|v| v.parse::<u64>().ok())
            .unwrap_or(120);
        tokio::spawn(async move {
            info!(
                "Position Audit Timer started ({}s interval)",
                audit_interval_s
            );
            loop {
                tokio::time::sleep(std::time::Duration::from_secs(audit_interval_s)).await;
                let _ = engine_tx_for_audit
                    .send(EngineEvent::PositionAuditTick)
                    .await;
            }
        });

        // Exchange filters/status can change while the engine is running. The
        // HTTP work stays outside the actor so private fills keep entering the
        // event queue; only the small immutable result is applied here.
        let engine_tx_for_metadata = self.engine_tx.clone();
        let metadata_rest = self.binance_rest.clone();
        let metadata_interval_s = std::env::var("EXCHANGE_INFO_REFRESH_INTERVAL_S")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(300)
            .max(30);
        tokio::spawn(async move {
            loop {
                tokio::time::sleep(Duration::from_secs(metadata_interval_s)).await;
                let result = metadata_rest.get_exchange_info().await;
                if engine_tx_for_metadata
                    .send(EngineEvent::ExchangeInfoRefreshResult(result))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        });

        loop {
            let event = match self.deferred_actor_events.pop_front() {
                Some(event) => Some(event),
                None => self.event_receiver.recv().await,
            };
            let Some(event) = event else { break };
            match event {
                EngineEvent::Ws(ws_event) => self.process_ws_event(ws_event).await,
                EngineEvent::Alpha(alpha_instruction) => {
                    self.handle_alpha_instruction(alpha_instruction).await;
                }
                EngineEvent::LeggingTimeout(client_id) => {
                    self.handle_legging_timeout(client_id).await;
                }
                EngineEvent::StrategyTick => {
                    self.tick_strategy().await;
                }
                EngineEvent::PositionAuditTick => {
                    if self.state != SystemState::Trading && self.private_stream_quorum_ready() {
                        self.execute_reconciliation_sequence().await;
                    } else {
                        self.runtime_position_audit().await;
                    }
                }
                EngineEvent::ExchangeInfoRefreshResult(result) => {
                    self.apply_exchange_info_refresh(result);
                }
            }
        }
    }

    async fn process_ws_event(&mut self, mut ws_event: WsEvent) {
        if let WsEvent::OrderUpdate {
            ref client_order_id,
            ref mut maker_fills,
            ref mut taker_fills,
            ref mut market,
            ref mut side,
            ref mut account_id,
            ref mut environment,
            ref mut strategy_id,
            ref mut cycle_id,
            ref mut intent_id,
            ref mut leg_id,
            ref mut config_version_hash,
            ..
        } = ws_event
        {
            *maker_fills = Some(self.maker_fills);
            *taker_fills = Some(self.taker_fills);
            if let Some(lineage) = self.order_lineage.get(client_order_id) {
                if market.is_none() {
                    *market = lineage.market;
                }
                if side.is_none() {
                    *side = lineage.side.clone();
                }
                if account_id.is_none() {
                    *account_id = lineage.account_id.clone();
                }
                if environment.is_none() {
                    *environment = lineage.environment.clone();
                }
                if strategy_id.is_none() {
                    *strategy_id = lineage.strategy_id.clone();
                }
                if cycle_id.is_none() {
                    *cycle_id = lineage.cycle_id.clone();
                }
                if intent_id.is_none() {
                    *intent_id = lineage.intent_id.clone();
                }
                if leg_id.is_none() {
                    *leg_id = lineage.leg_id.clone();
                }
                if config_version_hash.is_none() {
                    *config_version_hash = lineage.config_version_hash.clone();
                }
            }
        }
        if let Ok(vec) = rmp_serde::to_vec_named(&ws_event) {
            let _ = self.dash_tx.send(vec);
        }
        // REST waits can recursively pump another websocket event. Box the
        // state transition future so that recursion has an explicit boundary.
        Box::pin(self.handle_ws_event(ws_event)).await;
    }

    async fn await_rest_while_processing_ws<T, F>(&mut self, future: F) -> T
    where
        F: Future<Output = T>,
    {
        tokio::pin!(future);
        loop {
            tokio::select! {
                result = &mut future => return result,
                event = self.event_receiver.recv() => {
                    match event {
                        Some(EngineEvent::Ws(ws_event)) => {
                            Box::pin(self.process_ws_event(ws_event)).await;
                        }
                        Some(EngineEvent::LeggingTimeout(client_order_id)) => {
                            Box::pin(self.handle_legging_timeout(client_order_id)).await;
                        }
                        Some(other) => self.deferred_actor_events.push_back(other),
                        None => return future.await,
                    }
                }
            }
        }
    }

    async fn cancel_order_pumped(
        &mut self,
        venue: LegVenue,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<String, String> {
        let rest = self.binance_rest.clone();
        let symbol = symbol.to_string();
        let client_order_id = client_order_id.to_string();
        self.await_rest_while_processing_ws(async move {
            match venue {
                LegVenue::Spot => rest.cancel_order(&symbol, &client_order_id).await,
                LegVenue::UsdtFutures => rest.cancel_futures_order(&symbol, &client_order_id).await,
            }
        })
        .await
    }

    async fn place_limit_order_pumped(
        &mut self,
        venue: LegVenue,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<ReconciledSubmission, String> {
        let rest = self.binance_rest.clone();
        let symbol = symbol.to_string();
        let quantity = quantity.to_string();
        let price = price.to_string();
        let client_order_id = client_order_id.to_string();
        self.await_rest_while_processing_ws(async move {
            match venue {
                LegVenue::Spot => {
                    rest.place_spot_limit_order_read_before_retry(
                        &symbol,
                        side,
                        &quantity,
                        &price,
                        &client_order_id,
                    )
                    .await
                }
                LegVenue::UsdtFutures => {
                    rest.place_futures_limit_order_read_before_retry(
                        &symbol,
                        side,
                        &quantity,
                        &price,
                        &client_order_id,
                        reduce_only,
                    )
                    .await
                }
            }
        })
        .await
    }

    async fn place_market_order_pumped(
        &mut self,
        venue: LegVenue,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<ReconciledSubmission, String> {
        let rest = self.binance_rest.clone();
        let symbol = symbol.to_string();
        let quantity = quantity.to_string();
        let client_order_id = client_order_id.to_string();
        self.await_rest_while_processing_ws(async move {
            match venue {
                LegVenue::Spot => {
                    rest.place_spot_market_order_read_before_retry(
                        &symbol,
                        side,
                        &quantity,
                        &client_order_id,
                    )
                    .await
                }
                LegVenue::UsdtFutures => {
                    rest.place_futures_market_order_read_before_retry(
                        &symbol,
                        side,
                        &quantity,
                        &client_order_id,
                        reduce_only,
                    )
                    .await
                }
            }
        })
        .await
    }

    async fn get_open_orders_pumped(&mut self, venue: LegVenue) -> Result<String, String> {
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(
            async move { rest.get_open_orders_for_venue(venue).await },
        )
        .await
    }

    async fn get_spot_account_pumped(&mut self) -> Result<String, String> {
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(async move { rest.get_account().await })
            .await
    }

    async fn get_futures_account_pumped(&mut self) -> Result<String, String> {
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(async move { rest.get_fapi_account().await })
            .await
    }

    async fn get_futures_positions_pumped(&mut self) -> Result<String, String> {
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(async move { rest.get_fapi_position_risk().await })
            .await
    }

    async fn sync_time_pumped(&mut self) -> Result<(), String> {
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(async move { rest.sync_time().await })
            .await
    }

    async fn get_order_pumped(
        &mut self,
        venue: LegVenue,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<String, String> {
        let rest = self.binance_rest.clone();
        let symbol = symbol.to_string();
        let client_order_id = client_order_id.to_string();
        self.await_rest_while_processing_ws(async move {
            rest.get_order_by_client_id(venue, &symbol, &client_order_id)
                .await
        })
        .await
    }

    async fn tick_strategy(&mut self) {
        info!("Strategy Engine Tick: Refreshing funding rates...");
        if let Err(e) = self.ranking_engine.refresh().await {
            error!("Strategy Engine: Failed to refresh funding: {}", e);
            return;
        }

        let instructions = self.strategy_engine.generate_instructions(
            &self.ranking_engine,
            &self.tracked_positions,
            self.account_equity_usd,
        );

        for instruction in instructions {
            self.handle_alpha_instruction(instruction).await;
        }
    }

    async fn handle_legging_timeout(&mut self, trigger_client_id: String) {
        let symbol = self
            .chase_states
            .iter()
            .find(|(_, s)| {
                s.spot_client_order_id == trigger_client_id
                    || s.futures_client_order_id == trigger_client_id
            })
            .map(|(k, _)| k.clone());
        let Some(symbol) = symbol else { return };
        let Some(mut chase) = self.chase_states.get(&symbol).cloned() else {
            return;
        };

        let first_filled_leg = match chase.phase {
            ChasePhase::LegFilledWaiting(leg) => leg,
            _ => return,
        };

        info!(
            "Legging timeout reached for: {:?}. Cancelling unfilled maker and converting to taker...",
            first_filled_leg
        );

        let (unfilled_sym, unfilled_cid, unfilled_side, unfilled_leg) = match first_filled_leg {
            Leg::Spot => (
                chase.symbol.clone(),
                chase.futures_client_order_id.clone(),
                chase.futures_side,
                Leg::Futures,
            ),
            Leg::Futures => (
                chase.symbol.clone(),
                chase.spot_client_order_id.clone(),
                chase.spot_side,
                Leg::Spot,
            ),
        };

        let unfilled_market = match unfilled_leg {
            Leg::Spot => MarketType::Spot,
            Leg::Futures => MarketType::Perp,
        };
        let expected_price = match unfilled_leg {
            Leg::Spot => chase.expected_spot_price,
            Leg::Futures => chase.expected_fut_price,
        };
        let remaining_quantity =
            (chase.target_for(unfilled_leg) - chase.cumulative_for(unfilled_leg)).max(0.0);
        let quantity_tolerance = chase.target_for(unfilled_leg).abs().mul_add(1e-9, 1e-12);
        if remaining_quantity <= quantity_tolerance {
            self.require_chase_reconciliation(
                &symbol,
                chase,
                &unfilled_cid,
                "TARGET_REACHED_WITHOUT_TERMINAL_ORDER_STATE",
            );
            return;
        }
        if chase.max_slippage_bps > 0.0 {
            let budget_exhausted = self
                .unhedged_budget_remaining_ms(&chase)
                .map(|remaining| remaining == 0)
                .unwrap_or(false);
            match self.market_order_slippage_bps(
                &unfilled_sym,
                unfilled_market,
                unfilled_side,
                expected_price,
            ) {
                Some(slippage_bps) if slippage_bps <= chase.max_slippage_bps => {}
                Some(slippage_bps) => {
                    if !budget_exhausted {
                        error!(
                            "Legging defense market fallback deferred for {} {:?}: estimated slippage {:.2}bps exceeds cap {:.2}bps",
                            unfilled_sym, unfilled_leg, slippage_bps, chase.max_slippage_bps
                        );
                        self.schedule_legging_timeout(
                            trigger_client_id,
                            self.bounded_legging_timeout_ms(&chase),
                        );
                        return;
                    }
                    warn!(
                        "Max unhedged-notional-ms budget exhausted for {} {:?}; overriding routine slippage cap for emergency hedge",
                        unfilled_sym, unfilled_leg
                    );
                }
                None => {
                    if !budget_exhausted {
                        error!(
                            "Legging defense market fallback deferred for {} {:?}: missing top-of-book or expected price for slippage check",
                            unfilled_sym, unfilled_leg
                        );
                        self.schedule_legging_timeout(
                            trigger_client_id,
                            self.bounded_legging_timeout_ms(&chase),
                        );
                        return;
                    }
                    warn!(
                        "Max unhedged-notional-ms budget exhausted for {} {:?}; submitting emergency hedge without a fresh slippage estimate",
                        unfilled_sym, unfilled_leg
                    );
                }
            }
        }

        let submission_not_started = self
            .internal_orders
            .get(&unfilled_cid)
            .is_some_and(|order| order.status == "PENDING_SUBMIT");
        let already_terminal = self
            .internal_orders
            .get(&unfilled_cid)
            .map(|order| {
                matches!(
                    order.status.as_str(),
                    "CANCELED" | "CANCELLED" | "EXPIRED" | "REJECTED"
                )
            })
            .unwrap_or(false);
        let cancel_result = if already_terminal || submission_not_started {
            Ok(String::new())
        } else {
            match unfilled_leg {
                Leg::Spot => {
                    self.cancel_order_pumped(LegVenue::Spot, &unfilled_sym, &unfilled_cid)
                        .await
                }
                Leg::Futures => {
                    self.cancel_order_pumped(LegVenue::UsdtFutures, &unfilled_sym, &unfilled_cid)
                        .await
                }
            }
        };
        if let Err(err) = cancel_result {
            error!(
                "Cannot replace hedge order {} for {} because cancel was not confirmed: {}",
                unfilled_cid, unfilled_sym, err
            );
            self.require_chase_reconciliation(
                &symbol,
                chase,
                &unfilled_cid,
                "HEDGE_CANCEL_UNCONFIRMED",
            );
            return;
        }
        if let Some(order) = self.internal_orders.get_mut(&unfilled_cid) {
            order.status = if submission_not_started {
                "NOT_SUBMITTED".to_string()
            } else {
                "CANCELED".to_string()
            };
        }
        let _ = self.persist_execution_state("legging maker cancel confirmed");

        let new_taker_cid = Self::generate_client_order_id("legging");
        info!(
            "Placing legging defense MARKET order for {:?} cid={}",
            unfilled_leg, new_taker_cid
        );

        let market = match unfilled_leg {
            Leg::Spot => MarketType::Spot,
            Leg::Futures => MarketType::Perp,
        };
        let fallback_fill_price = match unfilled_leg {
            Leg::Spot => chase.expected_spot_price,
            Leg::Futures => chase.expected_fut_price,
        };
        let expected_fill_price =
            self.paper_market_fill_price(&unfilled_sym, market, unfilled_side, fallback_fill_price);
        self.internal_orders.insert(
            new_taker_cid.clone(),
            InternalOrder {
                client_order_id: new_taker_cid.clone(),
                symbol: unfilled_sym.clone(),
                status: "PENDING_SUBMIT".to_string(),
                limit_price: Some(expected_fill_price),
            },
        );
        match unfilled_leg {
            Leg::Spot => chase.spot_client_order_id = new_taker_cid.clone(),
            Leg::Futures => chase.futures_client_order_id = new_taker_cid.clone(),
        }
        if let Some(lineage) = self.order_lineage.get(&unfilled_cid).cloned() {
            self.order_lineage.insert(new_taker_cid.clone(), lineage);
        }
        self.order_cumulative_fills
            .insert(new_taker_cid.clone(), 0.0);
        chase.phase = ChasePhase::LeggingDefenseTakerPlaced;
        // This write-ahead snapshot closes the crash window between exchange
        // acceptance and recording the replacement client order id.
        let _ = self.store_chase_state(
            symbol.clone(),
            chase.clone(),
            "legging defense before exchange submission",
        );
        if let Some(order) = self.internal_orders.get_mut(&new_taker_cid) {
            order.status = "SUBMITTING".to_string();
        }
        let _ = self.persist_execution_state("legging defense submission started");

        let market_res = match unfilled_leg {
            Leg::Spot => {
                let quantity = self.format_quantity_for_market(
                    &unfilled_sym,
                    MarketType::Spot,
                    remaining_quantity,
                );
                self.place_market_order_pumped(
                    LegVenue::Spot,
                    &unfilled_sym,
                    unfilled_side,
                    &quantity,
                    &new_taker_cid,
                    false,
                )
                .await
            }
            Leg::Futures => {
                let quantity = self.format_quantity_for_market(
                    &unfilled_sym,
                    MarketType::Perp,
                    remaining_quantity,
                );
                self.place_market_order_pumped(
                    LegVenue::UsdtFutures,
                    &unfilled_sym,
                    unfilled_side,
                    &quantity,
                    &new_taker_cid,
                    chase.is_exit,
                )
                .await
            }
        };

        if let Ok(receipt) = market_res {
            info!("Taker hedge submission response: {}", receipt.body);
            if receipt.recovered_after_ambiguous_submit
                && !Self::recovered_submission_is_resting_new(&receipt.body)
            {
                self.require_chase_reconciliation(
                    &symbol,
                    chase,
                    &new_taker_cid,
                    "LEGGING_DEFENSE_SUBMISSION_RECOVERED_WITH_EXECUTION",
                );
                return;
            }
            if let Some(order) = self.internal_orders.get_mut(&new_taker_cid) {
                order.status = if self.trading_mode == "paper" {
                    "FILLED_PENDING".to_string()
                } else {
                    "NEW".to_string()
                };
            }
            let _ = self.persist_execution_state("legging defense submission acknowledged");
            if self.trading_mode == "paper" {
                self.emit_paper_order_fill(
                    new_taker_cid,
                    unfilled_sym,
                    match unfilled_leg {
                        Leg::Spot | Leg::Futures => remaining_quantity,
                    },
                    expected_fill_price,
                    false,
                    "PAPER_TAKER_FILL",
                )
                .await;
            }
        } else {
            error!(
                "Failed to submit legging defense taker order: {:?}",
                market_res.err()
            );
            self.require_chase_reconciliation(
                &symbol,
                chase,
                &new_taker_cid,
                "LEGGING_DEFENSE_SUBMISSION_FAILED",
            );
        }
    }

    async fn handle_alpha_instruction(&mut self, instruction: crate::ipc::AlphaInstruction) {
        if instruction.intent != "HEARTBEAT" {
            info!("Handling Alpha Instruction: {:?}", instruction);
        } else {
            debug!("Handling Alpha Instruction: {:?}", instruction);
        }

        if instruction
            .intent
            .trim()
            .eq_ignore_ascii_case(CONFIG_SYNC_INTENT)
        {
            self.last_brain_ping = Instant::now();
            self.handle_config_sync_instruction(instruction);
            return;
        }

        let intent = match AlphaIntent::parse(&instruction.intent) {
            Some(intent) => intent,
            None => {
                warn!(
                    "Rejecting unsupported alpha intent {:?} for symbol {:?}",
                    instruction.intent, instruction.symbol
                );
                let symbol = instruction.symbol.clone();
                self.reject_instruction(
                    &instruction,
                    symbol.as_deref(),
                    "unsupported_intent",
                    false,
                );
                return;
            }
        };
        self.last_brain_ping = Instant::now();

        if intent == AlphaIntent::Heartbeat {
            self.emit_current_execution_state_snapshot();
            let ack_event = serde_json::json!({
                "event": "HeartbeatAck",
                "heartbeat_id": instruction.heartbeat_id,
                "status": "ok",
                "ts_ms": SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_millis())
                    .unwrap_or(0),
            });
            let _ = self
                .dash_tx
                .send(rmp_serde::to_vec_named(&ack_event).unwrap());
            return;
        }

        if intent == AlphaIntent::RestorePosition {
            let sym_upper = match instruction.symbol.as_deref() {
                Some(s) => s.to_uppercase(),
                None => {
                    warn!("Received RESTORE_POSITION with no symbol; ignoring.");
                    return;
                }
            };
            if let Err(err) = self.subscription_tx.send(sym_upper.clone()).await {
                warn!(
                    "Could not request dynamic market-data subscription for restored position {}: {}",
                    sym_upper, err
                );
            }
            self.restore_tracked_position(&sym_upper, &instruction);
            return;
        }

        let now_ms = Self::current_time_ms();
        if let Some(reason) = instruction.protocol_error(now_ms) {
            let symbol = instruction.symbol.clone();
            self.reject_instruction(&instruction, symbol.as_deref(), reason, false);
            return;
        }

        let receive_decision = match self.intent_journal.as_mut() {
            Some(journal) => journal.receive(&instruction, now_ms),
            None => Err(self
                .intent_journal_error
                .clone()
                .unwrap_or_else(|| "intent journal unavailable".to_string())),
        };
        match receive_decision {
            Ok(ReceiptDecision::New(receipt)) => self.emit_intent_ack(&receipt, false),
            Ok(ReceiptDecision::Replay(receipt)) => {
                self.emit_intent_ack(&receipt, true);
                return;
            }
            Ok(ReceiptDecision::Conflict) => {
                let symbol = instruction.symbol.clone();
                self.reject_instruction(
                    &instruction,
                    symbol.as_deref(),
                    "duplicate_intent_conflict",
                    false,
                );
                return;
            }
            Ok(ReceiptDecision::NonMonotonicSequence) => {
                let symbol = instruction.symbol.clone();
                self.reject_instruction(
                    &instruction,
                    symbol.as_deref(),
                    "non_monotonic_sequence",
                    false,
                );
                return;
            }
            Err(err) => {
                error!("Execution intent journal receive failed: {err}");
                self.intent_journal_error = Some(err);
                self.intent_journal = None;
                let symbol = instruction.symbol.clone();
                self.reject_instruction(
                    &instruction,
                    symbol.as_deref(),
                    "intent_journal_unavailable",
                    false,
                );
                return;
            }
        }

        let intent_id = instruction.intent_id.clone().unwrap_or_default();
        if !self.transition_intent_ack(&intent_id, "VALIDATED", "") {
            self.emit_raw_intent_ack(&instruction, "REJECTED", "intent_journal_unavailable");
            return;
        }

        let sym_upper = match instruction.symbol.as_deref() {
            Some(s) => s.to_uppercase(),
            None => {
                warn!("Received non-heartbeat instruction with no symbol; rejecting.");
                self.reject_instruction(&instruction, None, "missing_symbol", true);
                return;
            }
        };

        let is_exit_intent = intent.is_exit();
        if !is_exit_intent && self.trading_mode != "paper" {
            let command_config_hash = instruction
                .config_version_hash
                .as_deref()
                .unwrap_or_default();
            if let Some(reason) = self
                .config_consensus
                .entry_block_reason(command_config_hash)
            {
                warn!(
                    "Rejecting {} entry for {} because Rust/Python config consensus is not safe: {}",
                    instruction.intent, sym_upper, reason
                );
                self.reject_instruction(&instruction, Some(&sym_upper), reason, true);
                return;
            }
        }
        if self.state != SystemState::Trading && !is_exit_intent {
            warn!(
                "System not currently ready for new risk; rejecting entry instruction for {}.",
                sym_upper
            );
            self.reject_instruction(&instruction, Some(&sym_upper), "system_not_trading", true);
            return;
        }
        if self.state != SystemState::Trading {
            warn!(
                "Executing verified reduce-only exit/repair for {} while system state is {:?}",
                sym_upper, self.state
            );
        }

        if !is_exit_intent && self.execution_state_journal_error.is_some() {
            self.reject_instruction(
                &instruction,
                Some(&sym_upper),
                "execution_state_journal_unavailable",
                true,
            );
            return;
        }
        if !is_exit_intent
            && self.trading_mode != "paper"
            && (!self.exchange_info.contains_key(&sym_upper) || !self.exchange_metadata_fresh())
        {
            let reason = if self.exchange_info.contains_key(&sym_upper) {
                "exchange_metadata_stale"
            } else {
                "exchange_metadata_unavailable"
            };
            self.reject_instruction(&instruction, Some(&sym_upper), reason, true);
            return;
        }
        if !is_exit_intent && self.check_circuit_breakers().await {
            self.reject_instruction(&instruction, Some(&sym_upper), "circuit_breaker", true);
            return;
        }

        if let Err(err) = self.subscription_tx.send(sym_upper.clone()).await {
            warn!(
                "Could not request dynamic market-data subscription for {}: {}",
                sym_upper, err
            );
        }
        if self.chase_states.contains_key(&sym_upper) {
            let can_replace = self
                .chase_states
                .get(&sym_upper)
                .map(|c| {
                    c.phase == ChasePhase::Idle || matches!(c.phase, ChasePhase::DualMakerPlaced)
                })
                .unwrap_or(false);

            if !is_exit_intent && !can_replace {
                warn!(
                    "Currently executing a Chase for {}, skipping new alpha instruction.",
                    sym_upper
                );
                self.reject_instruction(&instruction, Some(&sym_upper), "chase_active", true);
                return;
            }
            let existing = self
                .chase_states
                .get(&sym_upper)
                .cloned()
                .expect("contains_key checked above");
            if is_exit_intent
                && !matches!(
                    existing.phase,
                    ChasePhase::Idle | ChasePhase::DualMakerPlaced
                )
            {
                warn!(
                    "Cannot safely preempt unresolved {:?} chase for {}; explicit reconciliation is required",
                    existing.phase, sym_upper
                );
                self.reject_instruction(
                    &instruction,
                    Some(&sym_upper),
                    "active_execution_requires_reconciliation",
                    true,
                );
                return;
            }

            if matches!(existing.phase, ChasePhase::DualMakerPlaced) {
                warn!(
                    "Cancelling maker orders before preempting active chase on {}",
                    sym_upper
                );
                let spot_cancel = if existing.has_spot_leg() {
                    self.cancel_order_pumped(
                        LegVenue::Spot,
                        &sym_upper,
                        &existing.spot_client_order_id,
                    )
                    .await
                    .map(|_| ())
                } else {
                    Ok(())
                };
                let perp_cancel = if existing.has_futures_leg() {
                    self.cancel_order_pumped(
                        LegVenue::UsdtFutures,
                        &sym_upper,
                        &existing.futures_client_order_id,
                    )
                    .await
                    .map(|_| ())
                } else {
                    Ok(())
                };
                if spot_cancel.is_err() || perp_cancel.is_err() {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        existing,
                        "",
                        "PREEMPT_CANCEL_UNCONFIRMED",
                    );
                    self.reject_instruction(
                        &instruction,
                        Some(&sym_upper),
                        "preempt_cancel_unconfirmed",
                        true,
                    );
                    return;
                }
                for client_id in [
                    &existing.spot_client_order_id,
                    &existing.futures_client_order_id,
                ] {
                    if let Some(order) = self.internal_orders.get_mut(client_id) {
                        order.status = "CANCELED".to_string();
                    }
                }
                let _ = self.persist_execution_state("confirmed preemption cancels");
            }

            // Only an Idle chase or a maker chase with both cancels confirmed
            // is safe to replace.
            if let Some(preempted_intent_id) = self.chase_intent_ids.get(&sym_upper).cloned() {
                if !self.transition_intent_ack(
                    &preempted_intent_id,
                    "REJECTED",
                    "preempted_by_new_intent",
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        existing,
                        "",
                        "PREEMPT_ACK_NOT_DURABLE",
                    );
                    self.reject_instruction(
                        &instruction,
                        Some(&sym_upper),
                        "preempt_ack_not_durable",
                        true,
                    );
                    return;
                }
                self.chase_intent_ids.remove(&sym_upper);
            }
            if let Some(existing) = self.remove_chase_state(&sym_upper, "preempted chase removed") {
                let _ = self.persist_execution_state("preempted intent lineage removed");
                if is_exit_intent {
                    warn!(
                        "EXIT received for {} while chase active (phase: {:?}) — preempting chase",
                        sym_upper, existing.phase
                    );
                } else {
                    warn!(
                        "Replacing Idle/DualMakerPlaced chase state for {}",
                        sym_upper
                    );
                }
            }
        }

        let is_buy = intent.is_buy();
        let is_exit = intent.is_exit();
        let skip_spot_leg = is_exit && instruction.skip_spot_leg;
        let skip_perp_leg = is_exit && instruction.skip_perp_leg;
        if intent == AlphaIntent::ExitShort && skip_perp_leg && !skip_spot_leg {
            warn!(
                "Refusing EXIT_SHORT with skip_perp_leg=true for {}: spot BUY cannot close a non-existent short leg",
                sym_upper
            );
            self.reject_instruction(
                &instruction,
                Some(&sym_upper),
                "invalid_exit_short_skip_flags",
                true,
            );
            return;
        }
        if skip_spot_leg && skip_perp_leg {
            warn!(
                "Received {} for {} with both legs skipped; ignoring.",
                instruction.intent, sym_upper
            );
            self.reject_instruction(&instruction, Some(&sym_upper), "invalid_skip_flags", true);
            return;
        }
        let (mut resolved_spot_qty, mut resolved_perp_qty) = if is_exit {
            let pos = self.tracked_positions.get(&sym_upper);
            let spot_tracked = pos
                .and_then(|p| p.spot.as_ref())
                .map(|l| l.quantity)
                .unwrap_or(0.0);
            let perp_tracked = pos
                .and_then(|p| p.perp.as_ref())
                .map(|l| l.quantity)
                .unwrap_or(0.0);

            let spot_q = if instruction.spot_quantity.unwrap_or(0.0) > 0.0 {
                instruction.spot_quantity.unwrap()
            } else if instruction.quantity > 0.0 {
                instruction.quantity
            } else {
                spot_tracked
            };

            let perp_q = if instruction.perp_quantity.unwrap_or(0.0) > 0.0 {
                instruction.perp_quantity.unwrap()
            } else if instruction.quantity > 0.0 {
                instruction.quantity
            } else {
                perp_tracked
            };

            (spot_q.min(spot_tracked), perp_q.min(perp_tracked))
        } else {
            let q = instruction.quantity * instruction.exposure_scale;
            (q, q)
        };

        let sym_info = self.symbol_info(&sym_upper);
        let max_allowed = sym_info.spot_max_qty.min(sym_info.futures_max_qty);
        resolved_spot_qty = resolved_spot_qty.min(max_allowed);
        resolved_perp_qty = resolved_perp_qty.min(max_allowed);

        let normalized_spot_qty = if skip_spot_leg {
            0.0
        } else {
            match self.normalize_quantity_for_market(
                &sym_upper,
                MarketType::Spot,
                resolved_spot_qty,
            ) {
                Some(q) => q,
                None => {
                    if is_exit {
                        warn!(
                            "Instruction {} for {} resolved to invalid spot quantity {:.8}; rejecting the paired exit",
                            instruction.intent, sym_upper, resolved_spot_qty
                        );
                        self.reject_instruction(
                            &instruction,
                            Some(&sym_upper),
                            "exit_leg_not_executable",
                            true,
                        );
                        return;
                    } else {
                        warn!(
                            "Instruction {} for {} resolved to invalid spot quantity {:.8} after exchange normalization",
                            instruction.intent, sym_upper, resolved_spot_qty
                        );
                        self.reject_instruction(
                            &instruction,
                            Some(&sym_upper),
                            "invalid_quantity",
                            true,
                        );
                        return;
                    }
                }
            }
        };

        let normalized_perp_qty = if skip_perp_leg {
            0.0
        } else {
            match self.normalize_quantity_for_market(
                &sym_upper,
                MarketType::Perp,
                resolved_perp_qty,
            ) {
                Some(q) => q,
                None => {
                    if is_exit {
                        warn!(
                            "Instruction {} for {} resolved to invalid perp quantity {:.8}; rejecting the paired exit",
                            instruction.intent, sym_upper, resolved_perp_qty
                        );
                        self.reject_instruction(
                            &instruction,
                            Some(&sym_upper),
                            "exit_leg_not_executable",
                            true,
                        );
                        return;
                    } else {
                        warn!(
                            "Instruction {} for {} resolved to invalid perp quantity {:.8} after exchange normalization",
                            instruction.intent, sym_upper, resolved_perp_qty
                        );
                        self.reject_instruction(
                            &instruction,
                            Some(&sym_upper),
                            "invalid_quantity",
                            true,
                        );
                        return;
                    }
                }
            }
        };

        // Min notional filter (prevent dust orders and exchange rejections)
        let mid_price = self
            .perp_mid_cache
            .get(&sym_upper)
            .or_else(|| self.spot_mid_cache.get(&sym_upper))
            .copied()
            .unwrap_or(0.0);
        if mid_price > 0.0 {
            let spot_notional = normalized_spot_qty * mid_price;
            let perp_notional = normalized_perp_qty * mid_price;
            if !skip_spot_leg && spot_notional < sym_info.spot_min_notional {
                if is_exit {
                    warn!(
                        "Instruction {} for {} spot leg notional ${:.2} is below minimum ${:.2}; rejecting the paired exit",
                        instruction.intent, sym_upper, spot_notional, sym_info.spot_min_notional
                    );
                    self.reject_instruction(
                        &instruction,
                        Some(&sym_upper),
                        "exit_leg_not_executable",
                        true,
                    );
                    return;
                } else {
                    warn!(
                        "Instruction {} for {} rejected: spot notional ${:.2} is below minimum ${:.2} (qty={:.8}, price={:.4})",
                        instruction.intent,
                        sym_upper,
                        spot_notional,
                        sym_info.spot_min_notional,
                        normalized_spot_qty,
                        mid_price
                    );
                    self.reject_instruction(&instruction, Some(&sym_upper), "min_notional", true);
                    return;
                }
            }
            if !skip_perp_leg && perp_notional < sym_info.futures_min_notional {
                if is_exit {
                    warn!(
                        "Instruction {} for {} perp leg notional ${:.2} is below minimum ${:.2}; rejecting the paired exit",
                        instruction.intent, sym_upper, perp_notional, sym_info.futures_min_notional
                    );
                    self.reject_instruction(
                        &instruction,
                        Some(&sym_upper),
                        "exit_leg_not_executable",
                        true,
                    );
                    return;
                } else {
                    warn!(
                        "Instruction {} for {} rejected: perp notional ${:.2} is below minimum ${:.2} (qty={:.8}, price={:.4})",
                        instruction.intent,
                        sym_upper,
                        perp_notional,
                        sym_info.futures_min_notional,
                        normalized_perp_qty,
                        mid_price
                    );
                    self.reject_instruction(&instruction, Some(&sym_upper), "min_notional", true);
                    return;
                }
            }
        }

        if skip_spot_leg && skip_perp_leg {
            warn!(
                "Received {} for {} with no executable legs after normalization; rejecting.",
                instruction.intent, sym_upper
            );
            self.reject_instruction(&instruction, Some(&sym_upper), "invalid_quantity", true);

            return;
        }

        let spot_client_order_id = if skip_spot_leg {
            String::new()
        } else {
            instruction.spot_client_order_id.clone().unwrap_or_default()
        };
        let futures_client_order_id = if skip_perp_leg {
            String::new()
        } else {
            instruction.perp_client_order_id.clone().unwrap_or_default()
        };

        let common_lineage = OrderLineage {
            account_id: instruction.account_id.clone(),
            environment: instruction.environment.clone(),
            strategy_id: instruction.strategy_id.clone(),
            cycle_id: instruction.cycle_id.clone(),
            intent_id: instruction.intent_id.clone(),
            config_version_hash: instruction.config_version_hash.clone(),
            ..OrderLineage::default()
        };
        if !spot_client_order_id.is_empty() {
            self.order_lineage.insert(
                spot_client_order_id.clone(),
                OrderLineage {
                    leg_id: instruction.spot_leg_id.clone(),
                    market: Some(MarketType::Spot),
                    side: Some(if is_buy { "BUY" } else { "SELL" }.to_string()),
                    ..common_lineage.clone()
                },
            );
            self.order_cumulative_fills
                .entry(spot_client_order_id.clone())
                .or_insert(0.0);
        }
        if !futures_client_order_id.is_empty() {
            self.order_lineage.insert(
                futures_client_order_id.clone(),
                OrderLineage {
                    leg_id: instruction.perp_leg_id.clone(),
                    market: Some(MarketType::Perp),
                    side: Some(if is_buy { "SELL" } else { "BUY" }.to_string()),
                    ..common_lineage
                },
            );
            self.order_cumulative_fills
                .entry(futures_client_order_id.clone())
                .or_insert(0.0);
        }

        if mid_price > 0.0 {
            let estimated_notional = normalized_perp_qty.max(normalized_spot_qty) * mid_price;
            let min_n = sym_info
                .spot_min_notional
                .max(sym_info.futures_min_notional);

            if !is_exit && estimated_notional < min_n {
                warn!(
                    "Instruction {} for {} rejected: estimated notional ${:.2} is below minimum ${:.2} (qty_spot={:.8}, qty_perp={:.8}, price={:.4})",
                    instruction.intent,
                    sym_upper,
                    estimated_notional,
                    min_n,
                    normalized_spot_qty,
                    normalized_perp_qty,
                    mid_price
                );
                self.reject_instruction(&instruction, Some(&sym_upper), "min_notional", true);
                return;
            }
        }

        let chase = ChaseState {
            symbol: sym_upper.clone(),
            quantity: normalized_perp_qty, // Keep perp as primary quantity for dashboard
            spot_quantity: normalized_spot_qty,
            perp_quantity: normalized_perp_qty,
            spot_client_order_id,
            futures_client_order_id,
            skip_spot_leg,
            skip_perp_leg,
            spot_side: if is_buy {
                TradeSide::Buy
            } else {
                TradeSide::Sell
            },
            futures_side: if is_buy {
                TradeSide::Sell
            } else {
                TradeSide::Buy
            },
            is_exit,
            max_slippage_bps: instruction.max_slippage_bps.max(0.0),
            phase: ChasePhase::Idle,
            start_time: Instant::now(),
            expected_spot_price: 0.0,
            expected_fut_price: 0.0,
            spot_fill_price: None,
            futures_fill_price: None,
            spot_cumulative_filled: 0.0,
            futures_cumulative_filled: 0.0,
            spot_terminal: false,
            futures_terminal: false,
        };
        self.chase_states.insert(sym_upper.clone(), chase);
        self.chase_intent_ids
            .insert(sym_upper.clone(), intent_id.clone());
        self.chase_unhedged_budgets.insert(
            sym_upper.clone(),
            instruction
                .max_unhedged_notional_ms
                .unwrap_or(crate::ipc::DEFAULT_MAX_UNHEDGED_NOTIONAL_MS),
        );
        if !self.persist_execution_state("accepted chase before SUBMITTED ACK") && !is_exit {
            self.remove_chase_state(&sym_upper, "rolled back undurable entry chase");
            self.chase_intent_ids.remove(&sym_upper);
            let _ = self.persist_execution_state("rolled back undurable entry lineage");
            self.reject_instruction(
                &instruction,
                Some(&sym_upper),
                "execution_state_journal_unavailable",
                true,
            );
            return;
        }
        if !self.transition_intent_ack(&intent_id, "SUBMITTED", "") {
            self.remove_chase_state(&sym_upper, "intent submission ACK failed");
            self.chase_intent_ids.remove(&sym_upper);
            let _ = self.persist_execution_state("failed intent lineage removed");
            self.emit_raw_intent_ack(&instruction, "REJECTED", "intent_journal_unavailable");
            return;
        }

        info!(
            "Dynamic chase state initialized from AlphaInstruction for {}.",
            sym_upper
        );
        // Kick the chase immediately with the cached top-of-book so a
        // single-leg market unwind (or a fresh maker placement on a
        // non-toxic symbol) doesn't sit Idle waiting for the next WS
        // tick. Safe: try_place_dual_maker is a no-op unless phase is
        // Idle and the relevant caches are populated.
        self.try_place_dual_maker(sym_upper.clone()).await;
    }

    async fn handle_ws_event(&mut self, event: WsEvent) {
        match event {
            WsEvent::Connected {
                symbol,
                stream_type,
            } => {
                info!(
                    "OrderManager received WebSocket Connected event for {} ({:?}).",
                    symbol, stream_type
                );
                if self.state == SystemState::Disconnected
                    && self.trading_mode == "paper"
                    && stream_type == WsStreamType::MarketData
                {
                    self.execute_reconciliation_sequence().await;
                } else if self.state == SystemState::Trading
                    && stream_type == WsStreamType::MarketData
                {
                    let symbol_upper = symbol.to_uppercase();
                    self.depth_sequences
                        .retain(|key, _| !key.starts_with(&format!("{}:", symbol_upper)));
                    info!(
                        "MarketData stream for {} connected while Trading. Performing targeted open orders check.",
                        symbol
                    );
                    let open_orders_json =
                        match self.get_open_orders_pumped(LegVenue::UsdtFutures).await {
                            Ok(json) => json,
                            Err(e) => {
                                warn!(
                                    "Failed to fetch open orders during targeted check for {}: {}",
                                    symbol, e
                                );
                                return;
                            }
                        };
                    if let Ok(parsed_orders) =
                        serde_json::from_str::<Vec<serde_json::Value>>(&open_orders_json)
                    {
                        for order in parsed_orders {
                            if let Some(order_sym) = order.get("symbol").and_then(|v| v.as_str()) {
                                if order_sym.to_uppercase() == symbol.to_uppercase() {
                                    if let Some(client_id) = order
                                        .get("clientOrderId")
                                        .and_then(|v| v.as_str())
                                        .filter(|client_id| {
                                            client_id.starts_with("bngs_")
                                                && !self.internal_orders.contains_key(*client_id)
                                        })
                                    {
                                        info!(
                                            "Targeted check: canceling bot-owned orphan {} for symbol {}",
                                            client_id, symbol
                                        );
                                        let _ = self
                                            .cancel_order_pumped(
                                                LegVenue::UsdtFutures,
                                                order_sym,
                                                client_id,
                                            )
                                            .await;
                                    }
                                }
                            }
                        }
                    }
                }
            }
            WsEvent::Disconnected {
                symbol,
                stream_type,
            } => {
                warn!(
                    "OrderManager received WebSocket Disconnected event for {} ({:?}).",
                    symbol, stream_type
                );

                if stream_type == WsStreamType::UserData {
                    warn!("User Data stream disconnected! Reverting to Disconnected state.");
                    self.state = SystemState::Disconnected;
                    self.emit_execution_readiness(
                        "DISCONNECTED",
                        "private user-data stream disconnected",
                    );
                }

                // Never discard an execution state because a feed disconnected:
                // the exchange-side effect may still exist. Clear only stale
                // market data and keep the chase durable for reconciliation.
                let symbol_upper = symbol.to_uppercase();
                if self.chase_states.contains_key(&symbol_upper) {
                    self.state = SystemState::Reconciling;
                    let _ = self.persist_execution_state("market stream disconnected during chase");
                    self.emit_execution_recovery_required(
                        "market_stream_disconnected_during_chase",
                    );
                }
                self.spot_top_cache.remove(&symbol_upper);
                self.perp_top_cache.remove(&symbol_upper);
                self.spot_depth_capacity.remove(&symbol_upper);
                self.perp_depth_capacity.remove(&symbol_upper);
                self.depth_sequences
                    .retain(|key, _| !key.starts_with(&format!("{}:", symbol_upper)));
            }
            WsEvent::PrivateStreamStatus {
                market,
                status,
                start_time_ms,
                end_time_ms,
                orders_replayed,
                trades_replayed,
                error,
            } => {
                self.private_stream_status_snapshots.insert(
                    market,
                    PrivateStreamStatusSnapshot {
                        market,
                        status: status.clone(),
                        start_time_ms,
                        end_time_ms,
                        orders_replayed,
                        trades_replayed,
                        error: error.clone(),
                    },
                );
                let quorum_ready = self.record_private_stream_status(market, &status);
                info!(
                    "Private stream {:?} status={} range={:?}..{:?} orders={} trades={} quorum_ready={}",
                    market,
                    status,
                    start_time_ms,
                    end_time_ms,
                    orders_replayed,
                    trades_replayed,
                    quorum_ready
                );
                if !status.eq_ignore_ascii_case("READY") {
                    self.emit_execution_readiness(
                        "BLOCKED",
                        error
                            .as_deref()
                            .unwrap_or("private stream backfill required"),
                    );
                    self.emit_execution_recovery_required(
                        error
                            .as_deref()
                            .unwrap_or("private_stream_backfill_required"),
                    );
                    return;
                }
                if quorum_ready && self.trading_mode != "paper" {
                    self.execute_reconciliation_sequence().await;
                }
            }
            WsEvent::TelemetryGap {
                skipped_messages,
                reason,
                event_time_ms,
            } => {
                warn!(
                    "Telemetry delivery gap detected at {}: skipped={} reason={}",
                    event_time_ms, skipped_messages, reason
                );
                self.state = SystemState::Reconciling;
                if self.trading_mode != "paper" {
                    self.private_stream_ready_markets.clear();
                    self.private_stream_status_snapshots.clear();
                }
                let _ = self.persist_execution_state("telemetry delivery gap");
                self.emit_execution_readiness("BLOCKED", "telemetry gap awaiting private replay");
                self.emit_execution_recovery_required("telemetry_gap_private_replay_required");
            }
            WsEvent::BookTicker {
                symbol,
                bid_price,
                ask_price,
            } => {
                if !self.market_processing_allowed(&symbol) {
                    return;
                }

                // Update mid-price history for adaptive volatility
                let mid_price = (bid_price + ask_price) / 2.0;
                let sym_upper = symbol.to_uppercase();
                self.perp_top_cache.insert(
                    sym_upper.clone(),
                    TopOfBook {
                        bid_price,
                        ask_price,
                        bid_qty: f64::NAN,
                        ask_qty: f64::NAN,
                    },
                );
                self.update_mid_price(&sym_upper, mid_price);
                self.apply_mark_price(&sym_upper, MarketType::Perp, mid_price);

                // Spread toxicity protection
                let spread_bps =
                    (ask_price - bid_price) / ((ask_price + bid_price) / 2.0) * 10000.0;
                let now = Instant::now();
                match toxicity_log_action(
                    spread_bps,
                    self.toxic_symbols.contains_key(&sym_upper),
                    self.toxic_symbols.get(&sym_upper).copied(),
                    now,
                ) {
                    ToxicityLogAction::Enter | ToxicityLogAction::Refresh => {
                        warn!(
                            "Spread toxicity detected for {}! ({:.1} bps). Pausing maker operations.",
                            sym_upper, spread_bps
                        );
                        self.toxic_symbols.insert(sym_upper.clone(), now);
                    }
                    ToxicityLogAction::Exit => {
                        info!("Toxicity resolved for {}. Resuming operations.", sym_upper);
                        self.toxic_symbols.remove(&sym_upper);
                    }
                    ToxicityLogAction::None => {}
                }
                self.is_toxic = !self.toxic_symbols.is_empty();

                // Per-symbol toxicity gate: a wide spread on an unrelated
                // symbol must not block maker placement for a healthy one.
                // try_place_dual_maker itself bypasses the gate for
                // single-leg market unwinds (taker orders).
                if !self.toxic_symbols.contains_key(&sym_upper) {
                    self.try_place_dual_maker(sym_upper).await;
                } else if self
                    .chase_states
                    .get(&sym_upper)
                    .map(|c| c.phase == ChasePhase::Idle && c.is_exit)
                    .unwrap_or(false)
                {
                    // Allow taker unwinds to proceed even while this symbol
                    // itself is toxic.
                    self.try_place_dual_maker(sym_upper).await;
                }
                self.maybe_fill_paper_resting_leg(&symbol, MarketType::Perp)
                    .await;
            }
            WsEvent::L2Depth {
                symbol,
                market,
                bids,
                asks,
                first_update_id,
                final_update_id,
                previous_final_update_id,
                is_snapshot,
            } => {
                if !self.market_processing_allowed(&symbol) {
                    return;
                }

                let sym_upper = symbol.to_uppercase();
                let market_name = match market {
                    MarketType::Spot => "spot",
                    MarketType::Perp => "perp",
                };
                let sequence_key = format!("{}:{}", sym_upper, market_name);
                if let Some(final_id) = final_update_id {
                    if let Some(last_id) = self.depth_sequences.get(&sequence_key).copied() {
                        if final_id <= last_id {
                            debug!(
                                "Ignoring duplicate/stale depth update {} <= {} for {}",
                                final_id, last_id, sequence_key
                            );
                            return;
                        }
                        let previous_mismatch = previous_final_update_id
                            .map(|previous| previous != last_id)
                            .unwrap_or(false);
                        let range_gap = !is_snapshot
                            && previous_final_update_id.is_none()
                            && first_update_id
                                .map(|first| first > last_id.saturating_add(1))
                                .unwrap_or(false);
                        if previous_mismatch || range_gap {
                            warn!(
                                "Depth sequence gap for {}: last={} first={:?} previous={:?} final={}",
                                sequence_key,
                                last_id,
                                first_update_id,
                                previous_final_update_id,
                                final_id
                            );
                            self.spot_top_cache.remove(&sym_upper);
                            self.perp_top_cache.remove(&sym_upper);
                            self.spot_depth_capacity.remove(&sym_upper);
                            self.perp_depth_capacity.remove(&sym_upper);
                            let gap_event = serde_json::json!({
                                "event": "FeedGap",
                                "symbol": sym_upper,
                                "market": market_name,
                                "last_update_id": last_id,
                                "first_update_id": first_update_id,
                                "previous_final_update_id": previous_final_update_id,
                                "final_update_id": final_id,
                                "reason": "depth_sequence_gap",
                            });
                            if let Ok(encoded) = rmp_serde::to_vec_named(&gap_event) {
                                let _ = self.dash_tx.send(encoded);
                            }
                            return;
                        }
                    }
                    self.depth_sequences.insert(sequence_key, final_id);
                }

                let total_bid_vol: f64 = bids.iter().map(|item| item[1]).sum();
                let total_ask_vol: f64 = asks.iter().map(|item| item[1]).sum();
                let executable_depth = ExecutableDepth {
                    bid_notional_usd: bids.iter().map(|item| item[0] * item[1]).sum(),
                    ask_notional_usd: asks.iter().map(|item| item[0] * item[1]).sum(),
                    observed_at: Instant::now(),
                };
                match market {
                    MarketType::Spot => {
                        self.spot_depth_capacity
                            .insert(sym_upper.clone(), executable_depth);
                    }
                    MarketType::Perp => {
                        self.perp_depth_capacity
                            .insert(sym_upper.clone(), executable_depth);
                    }
                }

                let obi = if total_bid_vol + total_ask_vol > 0.0 {
                    (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol)
                } else {
                    0.0
                };

                if let (Some(best_bid), Some(best_ask)) = (bids.first(), asks.first()) {
                    let mid_price = (best_bid[0] + best_ask[0]) / 2.0;
                    let top = TopOfBook {
                        bid_price: best_bid[0],
                        ask_price: best_ask[0],
                        bid_qty: best_bid[1],
                        ask_qty: best_ask[1],
                    };
                    match market {
                        MarketType::Spot => {
                            self.spot_top_cache.insert(sym_upper.clone(), top);
                        }
                        MarketType::Perp => {
                            self.perp_top_cache.insert(sym_upper.clone(), top);
                        }
                    }
                    self.apply_mark_price(&sym_upper, market, mid_price);
                }

                if market == MarketType::Perp {
                    let previous_obi = self.obi_cache.get(&sym_upper).copied().unwrap_or(0.0);
                    self.obi_cache.insert(sym_upper.clone(), obi);
                    let now = Instant::now();
                    let should_log_obi = obi.abs() > 0.4
                        && (previous_obi.abs() <= 0.4
                            || self
                                .obi_alert_at
                                .get(&sym_upper)
                                .map(|last| now.duration_since(*last) >= Duration::from_secs(30))
                                .unwrap_or(true));

                    if should_log_obi {
                        debug!(
                            "High OBI detected for {}: {:.2}. Skewing resting limits.",
                            sym_upper, obi
                        );
                        self.obi_alert_at.insert(sym_upper.clone(), now);
                    } else if obi.abs() <= 0.4 {
                        self.obi_alert_at.remove(&sym_upper);
                    }
                }

                // Broadcast depth data to Python (for DepthTracker)
                let dash = self.dash_tx.clone();
                let sym = symbol.clone();
                // Force lowercase market string serialization so Python's rust_data_subscriber matches "spot"
                let mkt_str = match market {
                    MarketType::Spot => "spot",
                    MarketType::Perp => "perp",
                };
                let bids_json: Vec<Vec<serde_json::Value>> = bids
                    .iter()
                    .map(|item| vec![serde_json::json!(item[0]), serde_json::json!(item[1])])
                    .collect();
                let asks_json: Vec<Vec<serde_json::Value>> = asks
                    .iter()
                    .map(|item| vec![serde_json::json!(item[0]), serde_json::json!(item[1])])
                    .collect();
                tokio::spawn(async move {
                    let depth_event = serde_json::json!({
                        "event": "L2Depth",
                        "symbol": sym,
                        "market": mkt_str,
                        "bids": bids_json,
                        "asks": asks_json,
                        "first_update_id": first_update_id,
                        "final_update_id": final_update_id,
                        "previous_final_update_id": previous_final_update_id,
                        "is_snapshot": is_snapshot,
                        "sequence_contiguous": true,
                    });
                    if let Ok(vec) = rmp_serde::to_vec_named(&depth_event) {
                        let _ = dash.send(vec);
                    }
                });

                // Per-symbol toxicity gate (see BookTicker handler above).
                if !self.toxic_symbols.contains_key(&sym_upper) {
                    self.try_place_dual_maker(sym_upper).await;
                } else if self
                    .chase_states
                    .get(&sym_upper)
                    .map(|c| c.phase == ChasePhase::Idle && c.is_exit)
                    .unwrap_or(false)
                {
                    self.try_place_dual_maker(sym_upper).await;
                }
                self.maybe_fill_paper_resting_leg(&symbol, market).await;
            }
            WsEvent::OrderUpdate {
                client_order_id,
                symbol,
                status,
                filled_qty: reported_last_fill_qty,
                cumulative_filled_qty,
                avg_fill_price,
                last_fill_price,
                cumulative_quote_qty: _cumulative_quote_qty,
                commission: _commission,
                commission_asset: _commission_asset,
                realized_pnl: _realized_pnl,
                maker,
                execution_type,
                event_time_ms,
                ..
            } => {
                info!(
                    "Order Update: {} {} {} filled={} avg={:?} last={:?} maker={:?} exec={:?}",
                    symbol,
                    client_order_id,
                    status,
                    reported_last_fill_qty,
                    avg_fill_price,
                    last_fill_price,
                    maker,
                    execution_type
                );
                // The exchange average is cumulative; the last-fill price is
                // the correct price for an incremental position mutation.
                let observed_fill_price = last_fill_price.or(avg_fill_price);

                // Slippage monitoring on fills
                if status == "FILLED" || status == "PARTIALLY_FILLED" {
                    if let Some(internal) = self.internal_orders.get(&client_order_id) {
                        if let Some(expected_price) = internal.limit_price {
                            if let Some(actual_fill_price) = observed_fill_price {
                                let slippage_bps = ((actual_fill_price - expected_price)
                                    / expected_price)
                                    * 10_000.0;
                                info!(
                                    "Fill monitoring: {} status={} expected_price={:.2} actual_fill={:.2} slippage={:.2}bps",
                                    client_order_id,
                                    status,
                                    expected_price,
                                    actual_fill_price,
                                    slippage_bps
                                );
                            }
                        }
                    }
                }

                let sym_clone = symbol.to_uppercase();
                let mut chase_snapshot = self.chase_states.get(&sym_clone).cloned();
                let mut matched_leg_was_terminal = false;
                let mut reconciliation_reason: Option<&'static str> = None;

                if let Some(mut chase) = chase_snapshot.take() {
                    let matched_leg = if client_order_id == chase.spot_client_order_id {
                        Some(Leg::Spot)
                    } else if client_order_id == chase.futures_client_order_id {
                        Some(Leg::Futures)
                    } else {
                        None
                    };

                    if let Some(matched_leg) = matched_leg {
                        let target = chase.target_for(matched_leg);
                        let tolerance = target.abs().mul_add(1e-9, 1e-12);
                        let previous_order_cumulative = self
                            .order_cumulative_fills
                            .get(&client_order_id)
                            .copied()
                            .unwrap_or(0.0);
                        let reported_order_cumulative =
                            cumulative_filled_qty.unwrap_or_else(|| {
                                if matches!(status.as_str(), "FILLED" | "PARTIALLY_FILLED") {
                                    previous_order_cumulative + reported_last_fill_qty.max(0.0)
                                } else {
                                    previous_order_cumulative
                                }
                            });

                        if !reported_order_cumulative.is_finite()
                            || !reported_last_fill_qty.is_finite()
                            || reported_order_cumulative < -tolerance
                            || reported_last_fill_qty < -tolerance
                        {
                            reconciliation_reason = Some("INVALID_FILL_QUANTITY");
                        } else if reported_order_cumulative + tolerance < previous_order_cumulative
                        {
                            warn!(
                                "Ignoring stale cumulative-fill regression for {}: {:.12} < {:.12}",
                                client_order_id,
                                reported_order_cumulative,
                                previous_order_cumulative
                            );
                            return;
                        } else {
                            let effective_filled_qty =
                                (reported_order_cumulative - previous_order_cumulative).max(0.0);
                            if reported_order_cumulative > previous_order_cumulative {
                                self.order_cumulative_fills
                                    .insert(client_order_id.clone(), reported_order_cumulative);
                            }

                            let previous_leg_cumulative = chase.cumulative_for(matched_leg);
                            let updated_leg_cumulative =
                                previous_leg_cumulative + effective_filled_qty;
                            matched_leg_was_terminal = chase.terminal_for(matched_leg);

                            if effective_filled_qty > tolerance {
                                let fallback_price = match matched_leg {
                                    Leg::Spot => chase.expected_spot_price,
                                    Leg::Futures => chase.expected_fut_price,
                                };
                                let fill_price = observed_fill_price.unwrap_or(fallback_price);
                                let prior_fill_price = match matched_leg {
                                    Leg::Spot => chase.spot_fill_price,
                                    Leg::Futures => chase.futures_fill_price,
                                };
                                let blended_fill_price = if updated_leg_cumulative > tolerance {
                                    ((prior_fill_price.unwrap_or(fill_price)
                                        * previous_leg_cumulative)
                                        + (fill_price * effective_filled_qty))
                                        / updated_leg_cumulative
                                } else {
                                    fill_price
                                };
                                match matched_leg {
                                    Leg::Spot => chase.spot_fill_price = Some(blended_fill_price),
                                    Leg::Futures => {
                                        chase.futures_fill_price = Some(blended_fill_price)
                                    }
                                }
                                let (market, side) = match matched_leg {
                                    Leg::Spot => (MarketType::Spot, chase.spot_side),
                                    Leg::Futures => (MarketType::Perp, chase.futures_side),
                                };
                                self.apply_fill_to_position(
                                    &sym_clone,
                                    market,
                                    side,
                                    effective_filled_qty,
                                    fill_price,
                                    chase.is_exit,
                                );
                            }

                            let target_reached =
                                (updated_leg_cumulative - target).abs() <= tolerance;
                            let terminal = status == "FILLED" && target_reached;
                            chase.set_progress(matched_leg, updated_leg_cumulative, terminal);

                            let imbalance = (chase.spot_cumulative_filled
                                - chase.futures_cumulative_filled)
                                .abs();
                            let cycle_tolerance = chase
                                .spot_quantity
                                .max(chase.perp_quantity)
                                .mul_add(1e-9, 1e-12);
                            if imbalance > cycle_tolerance {
                                self.chase_unhedged_started_at_ms
                                    .entry(sym_clone.clone())
                                    .or_insert_with(|| {
                                        event_time_ms.unwrap_or_else(Self::current_time_ms)
                                    });
                            } else {
                                self.chase_unhedged_started_at_ms.remove(&sym_clone);
                            }

                            if updated_leg_cumulative > target + tolerance {
                                reconciliation_reason = Some("LEG_OVERFILL");
                            } else if status == "FILLED" && !target_reached {
                                reconciliation_reason = Some("TERMINAL_FILL_QUANTITY_MISMATCH");
                            }
                        }
                    }
                    chase_snapshot = Some(chase);
                }

                // Update internal order state
                if let Some(internal_order) = self.internal_orders.get_mut(&client_order_id) {
                    internal_order.status = status.clone();
                } else {
                    self.internal_orders.insert(
                        client_order_id.clone(),
                        InternalOrder {
                            client_order_id: client_order_id.clone(),
                            symbol: symbol.clone(),
                            status: status.clone(),
                            limit_price: None,
                        },
                    );
                }

                if let Some(chase) = chase_snapshot.as_ref() {
                    self.chase_states.insert(sym_clone.clone(), chase.clone());
                }
                let _ = self.persist_execution_state("order update and cumulative fill progress");

                if let (Some(reason), Some(chase)) = (reconciliation_reason, chase_snapshot.clone())
                {
                    self.require_chase_reconciliation(&sym_clone, chase, &client_order_id, reason);
                    return;
                }

                // Handle chase state logic
                if let Some(mut chase) = chase_snapshot {
                    let matched_leg = if client_order_id == chase.spot_client_order_id {
                        Some(Leg::Spot)
                    } else if client_order_id == chase.futures_client_order_id {
                        Some(Leg::Futures)
                    } else {
                        None
                    };
                    let Some(matched_leg) = matched_leg else {
                        return;
                    };

                    if chase.phase == ChasePhase::ReconciliationRequired {
                        self.require_chase_reconciliation(
                            &sym_clone,
                            chase,
                            &client_order_id,
                            "AWAITING_ACCOUNT_RECONCILIATION",
                        );
                        return;
                    }

                    if status == "FILLED" {
                        if matched_leg_was_terminal {
                            warn!(
                                "Duplicate terminal update ignored for {} {} at cumulative {:.12}",
                                symbol,
                                client_order_id,
                                chase.cumulative_for(matched_leg)
                            );
                            let _ = self.store_chase_state(
                                sym_clone.clone(),
                                chase,
                                "duplicate terminal update retained",
                            );
                            return;
                        }
                        let mut trigger_timeout = false;

                        if chase.is_single_leg() {
                            self.taker_fills += 1;
                            info!(
                                "Single-leg unwind completed for {} on {:?}. Maker rate: {:.1}%",
                                sym_clone,
                                matched_leg,
                                self.maker_fill_rate() * 100.0
                            );
                            self.emit_maker_fill_rate();
                            chase.phase = ChasePhase::Completed;
                            if !self.emit_cycle_order_update(
                                &chase,
                                "FILLED",
                                chase.cycle_client_order_id(),
                                chase.quantity,
                                false,
                                "FILLED_CYCLE",
                            ) {
                                self.require_chase_reconciliation(
                                    &sym_clone,
                                    chase,
                                    &client_order_id,
                                    "TERMINAL_ACK_NOT_DURABLE",
                                );
                                return;
                            }
                            self.remove_chase_state(&sym_clone, "single-leg unwind completed");
                            return;
                        }

                        if !chase.terminal_for(matched_leg) {
                            self.require_chase_reconciliation(
                                &sym_clone,
                                chase,
                                &client_order_id,
                                "FILLED_WITHOUT_TERMINAL_LEG_PROGRESS",
                            );
                            return;
                        }

                        match chase.phase {
                            ChasePhase::Idle => {
                                self.maker_fills += 1;
                                info!(
                                    "Leg '{:?}' FILLED before maker phase transition completed. Waiting for the other leg...",
                                    matched_leg
                                );
                                chase.phase = ChasePhase::LegFilledWaiting(matched_leg);
                                let _ = self.store_chase_state(
                                    sym_clone.clone(),
                                    chase.clone(),
                                    "first leg filled before maker ACK",
                                );
                                trigger_timeout = true;
                            }
                            ChasePhase::DualMakerPlaced => {
                                let first_filled = matched_leg;
                                self.maker_fills += 1;
                                info!(
                                    "Leg '{:?}' FILLED (maker). Waiting for the other leg...",
                                    first_filled
                                );
                                chase.phase = ChasePhase::LegFilledWaiting(first_filled);
                                let _ = self.store_chase_state(
                                    sym_clone.clone(),
                                    chase.clone(),
                                    "first maker leg filled",
                                );
                                trigger_timeout = true;
                            }
                            ChasePhase::LegFilledWaiting(first_filled) => {
                                if first_filled != matched_leg {
                                    if !chase.both_legs_terminal() {
                                        self.require_chase_reconciliation(
                                            &sym_clone,
                                            chase,
                                            &client_order_id,
                                            "CYCLE_COMPLETION_WITH_NONTERMINAL_LEG",
                                        );
                                        return;
                                    }
                                    self.maker_fills += 1;
                                    info!(
                                        "Chase cycle completed (both legs filled as maker). Rate: {:.1}%",
                                        self.maker_fill_rate() * 100.0
                                    );
                                    self.emit_maker_fill_rate();
                                    chase.phase = ChasePhase::Completed;
                                    if !self.emit_cycle_order_update(
                                        &chase,
                                        "FILLED",
                                        &chase.spot_client_order_id,
                                        chase.quantity,
                                        true,
                                        "FILLED_CYCLE",
                                    ) {
                                        self.require_chase_reconciliation(
                                            &sym_clone,
                                            chase,
                                            &client_order_id,
                                            "TERMINAL_ACK_NOT_DURABLE",
                                        );
                                        return;
                                    }
                                    self.remove_chase_state(
                                        &sym_clone,
                                        "dual-maker cycle completed",
                                    );
                                }
                            }
                            ChasePhase::LeggingDefenseTakerPlaced => {
                                if !chase.both_legs_terminal() {
                                    self.require_chase_reconciliation(
                                        &sym_clone,
                                        chase,
                                        &client_order_id,
                                        "DEFENSE_COMPLETION_WITH_NONTERMINAL_LEG",
                                    );
                                    return;
                                }
                                self.taker_fills += 1;
                                info!(
                                    "Chase cycle completed (legging defense taker). Maker rate: {:.1}%",
                                    self.maker_fill_rate() * 100.0
                                );
                                self.emit_maker_fill_rate();
                                chase.phase = ChasePhase::Completed;
                                if !self.emit_cycle_order_update(
                                    &chase,
                                    "FILLED",
                                    &chase.spot_client_order_id,
                                    chase.quantity,
                                    false,
                                    "FILLED_CYCLE",
                                ) {
                                    self.require_chase_reconciliation(
                                        &sym_clone,
                                        chase,
                                        &client_order_id,
                                        "TERMINAL_ACK_NOT_DURABLE",
                                    );
                                    return;
                                }
                                self.remove_chase_state(
                                    &sym_clone,
                                    "legging-defense cycle completed",
                                );
                            }
                            ChasePhase::ReconciliationRequired => {}
                            _ => {}
                        }

                        if trigger_timeout {
                            let cid = client_order_id.clone();
                            let timeout_ms = self.bounded_legging_timeout_ms(&chase);
                            info!(
                                "Bounded legging timeout: {}ms (vol={:.1}bps, max_unhedged_notional_ms={:.0})",
                                timeout_ms,
                                self.recent_volatility_bps(&chase.symbol),
                                self.chase_unhedged_budgets
                                    .get(&sym_clone)
                                    .copied()
                                    .unwrap_or(crate::ipc::DEFAULT_MAX_UNHEDGED_NOTIONAL_MS)
                            );
                            self.schedule_legging_timeout(cid, timeout_ms);
                        }
                    } else if status == "PARTIALLY_FILLED" {
                        let _ = self.store_chase_state(
                            sym_clone.clone(),
                            chase,
                            "partial fill progress",
                        );
                    } else if matches!(
                        status.as_str(),
                        "REJECTED" | "EXPIRED" | "CANCELED" | "CANCELLED"
                    ) {
                        let any_progress = chase.spot_cumulative_filled > 1e-12
                            || chase.futures_cumulative_filled > 1e-12;
                        if chase.is_single_leg() {
                            if any_progress {
                                self.require_chase_reconciliation(
                                    &sym_clone,
                                    chase,
                                    &client_order_id,
                                    "PARTIALLY_FILLED_SINGLE_LEG_TERMINATED",
                                );
                                return;
                            }
                            warn!(
                                "Single-leg unwind failed for {} on client id {} with status {}",
                                chase.symbol, client_order_id, status
                            );
                            if !self.emit_cycle_order_update(
                                &chase,
                                "REJECTED",
                                &client_order_id,
                                0.0,
                                false,
                                "SINGLE_LEG_SUBMISSION_FAILED",
                            ) {
                                self.require_chase_reconciliation(
                                    &sym_clone,
                                    chase,
                                    &client_order_id,
                                    "TERMINAL_ACK_NOT_DURABLE",
                                );
                                return;
                            }
                            self.remove_chase_state(
                                &sym_clone,
                                "unfilled single-leg order terminated",
                            );
                            return;
                        }

                        let other_leg = match matched_leg {
                            Leg::Spot => Leg::Futures,
                            Leg::Futures => Leg::Spot,
                        };
                        if chase.terminal_for(other_leg) && !chase.terminal_for(matched_leg) {
                            let filled_leg_client_id = match other_leg {
                                Leg::Spot => chase.spot_client_order_id.clone(),
                                Leg::Futures => chase.futures_client_order_id.clone(),
                            };
                            chase.phase = ChasePhase::LegFilledWaiting(other_leg);
                            let _ = self.store_chase_state(
                                sym_clone.clone(),
                                chase,
                                "peer leg terminal before residual repair",
                            );
                            warn!(
                                "Leg {:?} terminated for {} after partial cumulative progress; repairing only the residual quantity",
                                matched_leg, sym_clone
                            );
                            self.handle_legging_timeout(filled_leg_client_id).await;
                            return;
                        }
                        if any_progress {
                            self.require_chase_reconciliation(
                                &sym_clone,
                                chase,
                                &client_order_id,
                                "AMBIGUOUS_PARTIAL_LEG_TERMINATION",
                            );
                            return;
                        }
                        match chase.phase {
                            ChasePhase::LegFilledWaiting(first_filled) => {
                                let expected_failed_leg = match first_filled {
                                    Leg::Spot => Leg::Futures,
                                    Leg::Futures => Leg::Spot,
                                };
                                if matched_leg == expected_failed_leg {
                                    let filled_leg_client_id = match first_filled {
                                        Leg::Spot => chase.spot_client_order_id.clone(),
                                        Leg::Futures => chase.futures_client_order_id.clone(),
                                    };
                                    warn!(
                                        "Leg {:?} failed for {} while {:?} was already filled. Triggering taker hedge.",
                                        matched_leg, sym_clone, first_filled
                                    );
                                    self.handle_legging_timeout(filled_leg_client_id).await;
                                }
                            }
                            ChasePhase::Idle | ChasePhase::DualMakerPlaced => {
                                let cancel_result = match other_leg {
                                    Leg::Spot => {
                                        self.cancel_order_pumped(
                                            LegVenue::Spot,
                                            &chase.symbol,
                                            &chase.spot_client_order_id,
                                        )
                                        .await
                                    }
                                    Leg::Futures => {
                                        self.cancel_order_pumped(
                                            LegVenue::UsdtFutures,
                                            &chase.symbol,
                                            &chase.futures_client_order_id,
                                        )
                                        .await
                                    }
                                };
                                if let Err(err) = cancel_result {
                                    error!(
                                        "Fail-closed cancel failed for {} after {} on {}: {}",
                                        chase.symbol, status, client_order_id, err
                                    );
                                    self.require_chase_reconciliation(
                                        &sym_clone,
                                        chase,
                                        &client_order_id,
                                        "PEER_CANCEL_UNCONFIRMED",
                                    );
                                    return;
                                }
                                if !self.emit_cycle_order_update(
                                    &chase,
                                    "REJECTED",
                                    &client_order_id,
                                    0.0,
                                    false,
                                    "DUAL_SUBMISSION_FAILED",
                                ) {
                                    self.require_chase_reconciliation(
                                        &sym_clone,
                                        chase,
                                        &client_order_id,
                                        "TERMINAL_ACK_NOT_DURABLE",
                                    );
                                    return;
                                }
                                self.remove_chase_state(
                                    &sym_clone,
                                    "unfilled dual-maker cycle rejected",
                                );
                            }
                            ChasePhase::LeggingDefenseTakerPlaced => {
                                error!(
                                    "Legging defense failed for {} on client id {}",
                                    chase.symbol, client_order_id
                                );
                                self.require_chase_reconciliation(
                                    &sym_clone,
                                    chase,
                                    &client_order_id,
                                    "LEGGING_DEFENSE_TERMINATED",
                                );
                            }
                            ChasePhase::ReconciliationRequired => {
                                self.require_chase_reconciliation(
                                    &sym_clone,
                                    chase,
                                    &client_order_id,
                                    "AWAITING_ACCOUNT_RECONCILIATION",
                                );
                            }
                            ChasePhase::Completed => {}
                        }
                    }
                }
            }
            WsEvent::MarkPrice {
                symbol,
                mark_price,
                next_funding_rate,
                next_funding_time_ms,
            } => {
                self.apply_mark_price(&symbol, MarketType::Perp, mark_price);
                let dash = self.dash_tx.clone();
                let sym = symbol.clone();
                let mp = mark_price;
                let nfr = next_funding_rate;
                let nft = next_funding_time_ms;
                tokio::spawn(async move {
                    let mark_event = serde_json::json!({
                        "event": "MarkPrice",
                        "symbol": sym,
                        "mark_price": mp,
                        "next_funding_rate": nfr,
                        "next_funding_time_ms": nft,
                    });
                    if let Ok(vec) = rmp_serde::to_vec_named(&mark_event) {
                        let _ = dash.send(vec);
                    }
                });
            }
            WsEvent::VolumeBar {
                symbol,
                minute_start_ms,
                notional_usd,
            } => {
                let dash = self.dash_tx.clone();
                let sym = symbol.clone();
                let ms = minute_start_ms;
                let notional = notional_usd;
                tokio::spawn(async move {
                    let vol_event = serde_json::json!({
                        "event": "VolumeBar",
                        "symbol": sym,
                        "minute_start_ms": ms,
                        "notional_usd": notional,
                    });
                    if let Ok(vec) = rmp_serde::to_vec_named(&vol_event) {
                        let _ = dash.send(vec);
                    }
                });
            }
            WsEvent::AccountUpdate { balances, source } => {
                if source == "spot" {
                    for (asset, balance) in balances {
                        self.spot_balances.insert(asset, balance);
                    }
                } else {
                    for (asset, balance) in balances {
                        self.balances.insert(asset, balance);
                    }

                    let mut total_equity = 0.0;
                    for asset in &["USDT", "USDC", "FDUSD"] {
                        if let Some(balance) = self.balances.get(*asset) {
                            total_equity += balance;
                        }
                    }

                    if total_equity > 0.0 {
                        info!("Updating account equity to ${:.2}", total_equity);
                        self.account_equity_usd = total_equity;
                    }
                }
            }
            WsEvent::PositionDivergence { .. } => {
                // Emitted internally, no need to handle here
            }
            WsEvent::ExecutionReadiness { .. } => {
                // Emitted by this actor directly to telemetry after reconciliation.
            }
        }
    }

    fn generate_client_order_id(prefix: &str) -> String {
        let ts_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let nonce: u32 = rand::thread_rng().gen_range(1000..9999);
        format!("bngs_{}_{}_{}", prefix, ts_ms, nonce)
    }

    async fn try_place_dual_maker(&mut self, symbol: String) {
        let sym_upper = symbol.to_uppercase();
        let Some(chase_snapshot) = self.chase_states.get(&sym_upper).cloned() else {
            return;
        };

        if chase_snapshot.phase != ChasePhase::Idle {
            return;
        }

        // Toxicity is an entry-readiness gate, never an exit/repair gate.
        // Exits must proceed through their exposure-clamped route even when
        // the entry book is classified as toxic.
        if !chase_snapshot.is_exit && self.toxic_symbols.contains_key(&sym_upper) {
            return;
        }

        let symbol_info = self.symbol_info(&chase_snapshot.symbol);
        let spot_tick_size = symbol_info.spot_tick_size.max(0.00000001);
        let futures_tick_size = symbol_info.futures_tick_size.max(0.00000001);
        let spot_top = if chase_snapshot.has_spot_leg() {
            match self.spot_top_cache.get(&sym_upper).copied() {
                Some(top) => Some(top),
                None => return,
            }
        } else {
            None
        };
        let perp_top = if chase_snapshot.has_futures_leg() {
            match self.perp_top_cache.get(&sym_upper).copied() {
                Some(top) => Some(top),
                None => return,
            }
        } else {
            None
        };

        if !chase_snapshot.is_exit && self.trading_mode != "paper" {
            let spot_notional = spot_top
                .map(|top| {
                    chase_snapshot.spot_quantity
                        * match chase_snapshot.spot_side {
                            TradeSide::Buy => top.ask_price,
                            TradeSide::Sell => top.bid_price,
                        }
                })
                .unwrap_or(0.0);
            let perp_notional = perp_top
                .map(|top| {
                    chase_snapshot.perp_quantity
                        * match chase_snapshot.futures_side {
                            TradeSide::Buy => top.ask_price,
                            TradeSide::Sell => top.bid_price,
                        }
                })
                .unwrap_or(0.0);
            let per_symbol_notional = spot_notional.max(perp_notional);
            let per_symbol_cap = self.active_per_symbol_notional_cap_usd();
            let projected_gross = self.current_gross_exposure_usd + spot_notional + perp_notional;
            let risk_rejection = if !per_symbol_notional.is_finite()
                || !projected_gross.is_finite()
                || per_symbol_notional <= 0.0
            {
                Some("INVALID_ENTRY_NOTIONAL")
            } else if per_symbol_notional > per_symbol_cap + 1e-9 {
                Some("PER_SYMBOL_NOTIONAL_CAP")
            } else if projected_gross > self.max_gross_exposure_usd + 1e-9 {
                Some("MAX_GROSS_EXPOSURE")
            } else {
                None
            };
            if let Some(reason) = risk_rejection {
                warn!(
                    "Rejecting {} entry before placement: {} (symbol=${:.2}/${:.2}, projected_gross=${:.2}/${:.2})",
                    sym_upper,
                    reason,
                    per_symbol_notional,
                    per_symbol_cap,
                    projected_gross,
                    self.max_gross_exposure_usd,
                );
                if !self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    chase_snapshot.cycle_client_order_id(),
                    0.0,
                    false,
                    reason,
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        chase_snapshot,
                        "",
                        "TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&sym_upper, "entry risk limit rejected");
                return;
            }

            let now = Instant::now();
            let spot_capacity = self.spot_depth_capacity.get(&sym_upper).copied();
            let perp_capacity = self.perp_depth_capacity.get(&sym_upper).copied();
            let capacities_fresh = spot_capacity
                .zip(perp_capacity)
                .map(|(spot, perp)| {
                    now.duration_since(spot.observed_at) <= EXECUTABLE_DEPTH_MAX_AGE
                        && now.duration_since(perp.observed_at) <= EXECUTABLE_DEPTH_MAX_AGE
                })
                .unwrap_or(false);
            if !capacities_fresh {
                // Wait for the subscribed L2 snapshots. A stale book must never
                // be converted into an entry-side execution decision.
                return;
            }
            let spot = spot_capacity.expect("freshness proved spot capacity");
            let perp = perp_capacity.expect("freshness proved perp capacity");
            let spot_available = match chase_snapshot.spot_side {
                TradeSide::Buy => spot.ask_notional_usd,
                TradeSide::Sell => spot.bid_notional_usd,
            };
            let perp_available = match chase_snapshot.futures_side {
                TradeSide::Buy => perp.ask_notional_usd,
                TradeSide::Sell => perp.bid_notional_usd,
            };
            let spot_required = chase_snapshot.spot_quantity
                * spot_top
                    .map(|top| (top.bid_price + top.ask_price) / 2.0)
                    .unwrap_or(0.0)
                * ENTRY_DEPTH_MULTIPLIER;
            let perp_required = chase_snapshot.perp_quantity
                * perp_top
                    .map(|top| (top.bid_price + top.ask_price) / 2.0)
                    .unwrap_or(0.0)
                * ENTRY_DEPTH_MULTIPLIER;
            if spot_available < spot_required || perp_available < perp_required {
                warn!(
                    "Rejecting {} entry: executable depth below {}x notional (spot {:.2}/{:.2}, perp {:.2}/{:.2})",
                    sym_upper,
                    ENTRY_DEPTH_MULTIPLIER,
                    spot_available,
                    spot_required,
                    perp_available,
                    perp_required,
                );
                if !self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    chase_snapshot.cycle_client_order_id(),
                    0.0,
                    false,
                    "INSUFFICIENT_EXECUTABLE_DEPTH",
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        chase_snapshot,
                        "",
                        "TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&sym_upper, "insufficient executable entry depth");
                return;
            }
        }

        if let Some(active_leg) = chase_snapshot.active_leg() {
            let (client_order_id, market, side, target_price) = match active_leg {
                Leg::Spot => {
                    let top = match spot_top {
                        Some(top) => top,
                        None => return,
                    };
                    let market_price = match chase_snapshot.spot_side {
                        TradeSide::Buy => top.ask_price,
                        TradeSide::Sell => top.bid_price,
                    };
                    (
                        chase_snapshot.spot_client_order_id.clone(),
                        MarketType::Spot,
                        chase_snapshot.spot_side,
                        market_price,
                    )
                }
                Leg::Futures => {
                    let top = match perp_top {
                        Some(top) => top,
                        None => return,
                    };
                    let market_price = match chase_snapshot.futures_side {
                        TradeSide::Buy => top.ask_price,
                        TradeSide::Sell => top.bid_price,
                    };
                    (
                        chase_snapshot.futures_client_order_id.clone(),
                        MarketType::Perp,
                        chase_snapshot.futures_side,
                        market_price,
                    )
                }
            };

            if target_price <= 0.0 {
                error!(
                    "Single-leg unwind for {} has invalid market price {}",
                    chase_snapshot.symbol, target_price
                );
                if !self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    &client_order_id,
                    0.0,
                    false,
                    "INVALID_PRICE_NORMALIZATION",
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        chase_snapshot,
                        &client_order_id,
                        "TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&sym_upper, "invalid single-leg unwind price");
                return;
            }

            if let Some(c) = self.chase_states.get_mut(&sym_upper) {
                c.phase = ChasePhase::DualMakerPlaced;
                match active_leg {
                    Leg::Spot => c.expected_spot_price = target_price,
                    Leg::Futures => c.expected_fut_price = target_price,
                }
            }

            self.internal_orders.insert(
                client_order_id.clone(),
                InternalOrder {
                    client_order_id: client_order_id.clone(),
                    symbol: chase_snapshot.symbol.clone(),
                    status: "PENDING_SUBMIT".to_string(),
                    limit_price: Some(target_price),
                },
            );

            let durable_before_submit =
                self.persist_execution_state("single-leg unwind before exchange submission");
            if !durable_before_submit && !chase_snapshot.is_exit {
                if !self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    &client_order_id,
                    0.0,
                    false,
                    "EXECUTION_STATE_JOURNAL_UNAVAILABLE",
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        chase_snapshot,
                        &client_order_id,
                        "TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&sym_upper, "undurable single-leg entry rejected");
                return;
            }

            info!(
                "Placing single-leg MARKET unwind for {} on {:?}",
                chase_snapshot.symbol, active_leg
            );

            if self.trading_mode == "paper" {
                if let Some(order) = self.internal_orders.get_mut(&client_order_id) {
                    order.status = "FILLED_PENDING".to_string();
                }
                let _ = self.persist_execution_state("paper single-leg fill queued");
                let fill_price = self.paper_market_fill_price(
                    &chase_snapshot.symbol,
                    market,
                    side,
                    target_price,
                );
                self.emit_paper_order_fill(
                    client_order_id,
                    chase_snapshot.symbol.clone(),
                    chase_snapshot.quantity,
                    fill_price,
                    false,
                    "PAPER_TAKER_FILL",
                )
                .await;
                return;
            }

            let quantity = self.format_quantity_for_market(
                &chase_snapshot.symbol,
                market,
                match active_leg {
                    Leg::Spot => chase_snapshot.spot_quantity,
                    Leg::Futures => chase_snapshot.perp_quantity,
                },
            );
            if let Some(order) = self.internal_orders.get_mut(&client_order_id) {
                order.status = "SUBMITTING".to_string();
            }
            let _ = self.persist_execution_state("single-leg submission started");
            let submission = match active_leg {
                Leg::Spot => {
                    self.place_market_order_pumped(
                        LegVenue::Spot,
                        &chase_snapshot.symbol,
                        side,
                        &quantity,
                        &client_order_id,
                        false,
                    )
                    .await
                }
                Leg::Futures => {
                    self.place_market_order_pumped(
                        LegVenue::UsdtFutures,
                        &chase_snapshot.symbol,
                        side,
                        &quantity,
                        &client_order_id,
                        chase_snapshot.is_exit,
                    )
                    .await
                }
            };

            if let Ok(receipt) = submission {
                info!("Single-leg unwind order placed: {}", receipt.body);
                if receipt.recovered_after_ambiguous_submit
                    && !Self::recovered_submission_is_resting_new(&receipt.body)
                {
                    let durable_chase = self
                        .chase_states
                        .get(&sym_upper)
                        .cloned()
                        .unwrap_or_else(|| chase_snapshot.clone());
                    self.require_chase_reconciliation(
                        &sym_upper,
                        durable_chase,
                        &client_order_id,
                        "SINGLE_LEG_SUBMISSION_RECOVERED_WITH_EXECUTION",
                    );
                    return;
                }
                if let Some(order) = self.internal_orders.get_mut(&client_order_id) {
                    order.status = "NEW".to_string();
                }
                let _ = self.persist_execution_state("single-leg submission acknowledged");

                // Spot FULL responses can include exchange trade IDs for every
                // fill. Replay those exact economics. Futures RESULT responses
                // do not carry trade IDs, so a terminal response without a
                // complete `fills` array remains reconciliation-only until the
                // private stream/history supplies authoritative trades.
                if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&receipt.body) {
                    if let Some(status) = parsed.get("status").and_then(|s| s.as_str()) {
                        if status == "FILLED" {
                            let fills = parsed.get("fills").and_then(|value| value.as_array());
                            let complete_fills = fills.filter(|rows| {
                                !rows.is_empty()
                                    && rows.iter().all(|fill| {
                                        fill.get("tradeId")
                                            .and_then(|value| value.as_i64())
                                            .is_some()
                                            && fill
                                                .get("price")
                                                .and_then(|value| value.as_str())
                                                .and_then(|raw| raw.parse::<f64>().ok())
                                                .is_some_and(|value| value > 0.0)
                                            && fill
                                                .get("qty")
                                                .and_then(|value| value.as_str())
                                                .and_then(|raw| raw.parse::<f64>().ok())
                                                .is_some_and(|value| value > 0.0)
                                    })
                            });
                            let Some(fills) = complete_fills else {
                                let durable_chase = self
                                    .chase_states
                                    .get(&sym_upper)
                                    .cloned()
                                    .unwrap_or_else(|| chase_snapshot.clone());
                                self.require_chase_reconciliation(
                                    &sym_upper,
                                    durable_chase,
                                    &client_order_id,
                                    "SINGLE_LEG_FILLED_AWAITING_TRADE_HISTORY",
                                );
                                return;
                            };
                            let mut cumulative_qty = 0.0;
                            let mut cumulative_quote = 0.0;
                            for (index, fill) in fills.iter().enumerate() {
                                let fill_qty = fill
                                    .get("qty")
                                    .and_then(|value| value.as_str())
                                    .and_then(|raw| raw.parse::<f64>().ok())
                                    .unwrap_or(0.0);
                                let fill_price = fill
                                    .get("price")
                                    .and_then(|value| value.as_str())
                                    .and_then(|raw| raw.parse::<f64>().ok())
                                    .unwrap_or(0.0);
                                cumulative_qty += fill_qty;
                                cumulative_quote += fill_qty * fill_price;
                                let evt = WsEvent::OrderUpdate {
                                    client_order_id: client_order_id.clone(),
                                    symbol: sym_upper.clone(),
                                    status: if index + 1 == fills.len() {
                                        "FILLED".to_string()
                                    } else {
                                        "PARTIALLY_FILLED".to_string()
                                    },
                                    filled_qty: fill_qty,
                                    cumulative_filled_qty: Some(cumulative_qty),
                                    avg_fill_price: Some(cumulative_quote / cumulative_qty),
                                    last_fill_price: Some(fill_price),
                                    cumulative_quote_qty: Some(cumulative_quote),
                                    commission: fill
                                        .get("commission")
                                        .and_then(|value| value.as_str())
                                        .and_then(|raw| raw.parse::<f64>().ok()),
                                    commission_asset: fill
                                        .get("commissionAsset")
                                        .and_then(|value| value.as_str())
                                        .map(str::to_string),
                                    realized_pnl: None,
                                    maker: Some(false),
                                    execution_type: Some("TRADE".to_string()),
                                    event_time_ms: parsed
                                        .get("transactTime")
                                        .and_then(|value| value.as_i64())
                                        .or_else(|| Some(Self::current_time_ms())),
                                    maker_fills: None,
                                    taker_fills: None,
                                    market: Some(market),
                                    side: Some(match side {
                                        TradeSide::Buy => "BUY".to_string(),
                                        TradeSide::Sell => "SELL".to_string(),
                                    }),
                                    order_id: parsed.get("orderId").and_then(|v| v.as_i64()),
                                    trade_id: fill.get("tradeId").and_then(|v| v.as_i64()),
                                    account_id: None,
                                    environment: None,
                                    strategy_id: None,
                                    cycle_id: None,
                                    intent_id: None,
                                    leg_id: None,
                                    config_version_hash: None,
                                };
                                let _ = self.engine_tx.try_send(EngineEvent::Ws(evt));
                            }
                        }
                    }
                }
            } else {
                error!("Failed single-leg unwind: {:?}", submission.err());
                let durable_chase = self
                    .chase_states
                    .get(&sym_upper)
                    .cloned()
                    .unwrap_or_else(|| chase_snapshot.clone());
                self.require_chase_reconciliation(
                    &sym_upper,
                    durable_chase,
                    &client_order_id,
                    "SINGLE_LEG_SUBMISSION_FAILED",
                );
            }
            return;
        }

        let current_obi = self.obi_cache.get(&sym_upper).copied().unwrap_or(0.0);
        let Some(spot_top) = spot_top else {
            return;
        };
        let Some(perp_top) = perp_top else {
            return;
        };

        let mut spot_target = match chase_snapshot.spot_side {
            TradeSide::Buy => spot_top.bid_price,
            TradeSide::Sell => spot_top.ask_price,
        };

        let mut fut_target = match chase_snapshot.futures_side {
            TradeSide::Buy => perp_top.bid_price,
            TradeSide::Sell => perp_top.ask_price,
        };

        // OBI-based price skewing
        if current_obi > 0.3 {
            spot_target += spot_tick_size;
            fut_target += futures_tick_size;
        } else if current_obi < -0.3 {
            spot_target -= spot_tick_size;
            fut_target -= futures_tick_size;
        }

        let spot_target =
            Self::quantize_price(spot_target, spot_tick_size, chase_snapshot.spot_side);
        let fut_target =
            Self::quantize_price(fut_target, futures_tick_size, chase_snapshot.futures_side);
        if spot_target <= 0.0 || fut_target <= 0.0 {
            error!(
                "Normalized maker prices are invalid for {}: spot_target={} fut_target={}",
                chase_snapshot.symbol, spot_target, fut_target
            );
            if !self.emit_cycle_order_update(
                &chase_snapshot,
                "REJECTED",
                chase_snapshot.cycle_client_order_id(),
                0.0,
                false,
                "INVALID_PRICE_NORMALIZATION",
            ) {
                self.require_chase_reconciliation(
                    &sym_upper,
                    chase_snapshot,
                    "",
                    "TERMINAL_ACK_NOT_DURABLE",
                );
                return;
            }
            self.remove_chase_state(&sym_upper, "invalid dual-maker price");
            return;
        }

        let spot_price_str = Self::format_with_increment(spot_target, spot_tick_size);
        let fut_price_str = Self::format_with_increment(fut_target, futures_tick_size);
        let spot_qty_str = self.format_quantity_for_market(
            &chase_snapshot.symbol,
            MarketType::Spot,
            chase_snapshot.spot_quantity,
        );
        let fut_qty_str = self.format_quantity_for_market(
            &chase_snapshot.symbol,
            MarketType::Perp,
            chase_snapshot.perp_quantity,
        );

        if self.trading_mode != "paper" && chase_snapshot.spot_side == TradeSide::Buy {
            let required_usdt = spot_target * chase_snapshot.spot_quantity;
            let available_usdt = self.spot_balances.get("USDT").copied().unwrap_or(0.0);
            if available_usdt < required_usdt {
                error!(
                    "Insufficient spot USDT for {}. Required: {}, Available: {}",
                    chase_snapshot.symbol, required_usdt, available_usdt
                );
                if !self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    chase_snapshot.cycle_client_order_id(),
                    0.0,
                    false,
                    "INSUFFICIENT_SPOT_BALANCE",
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        chase_snapshot,
                        "",
                        "TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&sym_upper, "insufficient spot balance");
                return;
            }
        }

        if let Some(c) = self.chase_states.get_mut(&sym_upper) {
            c.expected_spot_price = spot_target;
            c.expected_fut_price = fut_target;
        }

        info!("Placing DUAL maker LIMIT orders. OBI: {:.2}", current_obi);

        self.internal_orders.insert(
            chase_snapshot.spot_client_order_id.clone(),
            InternalOrder {
                client_order_id: chase_snapshot.spot_client_order_id.clone(),
                symbol: chase_snapshot.symbol.clone(),
                status: "PENDING_SUBMIT".to_string(),
                limit_price: Some(spot_target),
            },
        );
        self.internal_orders.insert(
            chase_snapshot.futures_client_order_id.clone(),
            InternalOrder {
                client_order_id: chase_snapshot.futures_client_order_id.clone(),
                symbol: chase_snapshot.symbol.clone(),
                status: "PENDING_SUBMIT".to_string(),
                limit_price: Some(fut_target),
            },
        );

        let durable_before_submit =
            self.persist_execution_state("dual-maker orders before exchange submission");
        if !durable_before_submit && !chase_snapshot.is_exit {
            if !self.emit_cycle_order_update(
                &chase_snapshot,
                "REJECTED",
                chase_snapshot.cycle_client_order_id(),
                0.0,
                false,
                "EXECUTION_STATE_JOURNAL_UNAVAILABLE",
            ) {
                self.require_chase_reconciliation(
                    &sym_upper,
                    chase_snapshot,
                    "",
                    "TERMINAL_ACK_NOT_DURABLE",
                );
                return;
            }
            self.remove_chase_state(&sym_upper, "undurable dual-maker entry rejected");
            return;
        }

        if self.trading_mode == "paper" {
            if let Some(c) = self.chase_states.get_mut(&sym_upper) {
                c.phase = ChasePhase::DualMakerPlaced;
            }
            if let Some(order) = self
                .internal_orders
                .get_mut(&chase_snapshot.spot_client_order_id)
            {
                order.status = "NEW".to_string();
            }
            if let Some(order) = self
                .internal_orders
                .get_mut(&chase_snapshot.futures_client_order_id)
            {
                order.status = "NEW".to_string();
            }
            let _ = self.persist_execution_state("paper dual-maker orders resting");
            return;
        }

        if let Some(order) = self
            .internal_orders
            .get_mut(&chase_snapshot.spot_client_order_id)
        {
            order.status = "SUBMITTING".to_string();
        }
        let _ = self.persist_execution_state("spot maker submission started");
        let spot_res = self
            .place_limit_order_pumped(
                LegVenue::Spot,
                &chase_snapshot.symbol,
                chase_snapshot.spot_side,
                &spot_qty_str,
                &spot_price_str,
                &chase_snapshot.spot_client_order_id,
                false,
            )
            .await;
        match spot_res {
            Ok(receipt) => {
                info!("Spot Maker order placed: {}", receipt.body);
                if receipt.recovered_after_ambiguous_submit
                    && !Self::recovered_submission_is_resting_new(&receipt.body)
                {
                    let durable_chase = self
                        .chase_states
                        .get(&sym_upper)
                        .cloned()
                        .unwrap_or_else(|| chase_snapshot.clone());
                    self.require_chase_reconciliation(
                        &sym_upper,
                        durable_chase,
                        &chase_snapshot.spot_client_order_id,
                        "SPOT_SUBMISSION_RECOVERED_WITH_EXECUTION",
                    );
                    return;
                }
                if let Some(order) = self
                    .internal_orders
                    .get_mut(&chase_snapshot.spot_client_order_id)
                {
                    order.status = "NEW".to_string();
                }
                let _ = self.persist_execution_state("spot maker submission acknowledged");
            }
            Err(err) => {
                error!(
                    "Spot maker submission outcome is ambiguous; entering reconciliation: {}",
                    err
                );
                let durable_chase = self
                    .chase_states
                    .get(&sym_upper)
                    .cloned()
                    .unwrap_or_else(|| chase_snapshot.clone());
                self.require_chase_reconciliation(
                    &sym_upper,
                    durable_chase,
                    &chase_snapshot.spot_client_order_id,
                    "SPOT_SUBMISSION_OUTCOME_UNKNOWN",
                );
                return;
            }
        }

        // A private fill or terminal event may have arrived while the spot
        // REST request was pending. Its transition owns the next action (often
        // an urgent residual hedge); never continue the stale outer flow and
        // place a duplicate futures leg.
        if self
            .chase_states
            .get(&sym_upper)
            .is_none_or(|chase| chase.phase != ChasePhase::Idle)
        {
            info!(
                "Spot submission for {} completed after newer private execution progress; suppressing planned futures maker",
                sym_upper
            );
            return;
        }
        if let Some(order) = self
            .internal_orders
            .get_mut(&chase_snapshot.futures_client_order_id)
        {
            order.status = "SUBMITTING".to_string();
        }
        let _ = self.persist_execution_state("futures maker submission started");

        let fut_res = self
            .place_limit_order_pumped(
                LegVenue::UsdtFutures,
                &chase_snapshot.symbol,
                chase_snapshot.futures_side,
                &fut_qty_str,
                &fut_price_str,
                &chase_snapshot.futures_client_order_id,
                chase_snapshot.is_exit,
            )
            .await;

        match fut_res {
            Ok(receipt) => {
                info!("Futures Maker order placed: {}", receipt.body);
                if receipt.recovered_after_ambiguous_submit
                    && !Self::recovered_submission_is_resting_new(&receipt.body)
                {
                    let durable_chase = self
                        .chase_states
                        .get(&sym_upper)
                        .cloned()
                        .unwrap_or_else(|| chase_snapshot.clone());
                    self.require_chase_reconciliation(
                        &sym_upper,
                        durable_chase,
                        &chase_snapshot.futures_client_order_id,
                        "FUTURES_SUBMISSION_RECOVERED_WITH_EXECUTION",
                    );
                    return;
                }
                if let Some(order) = self
                    .internal_orders
                    .get_mut(&chase_snapshot.futures_client_order_id)
                {
                    order.status = "NEW".to_string();
                }
                if let Some(c) = self.chase_states.get_mut(&sym_upper) {
                    c.phase = ChasePhase::DualMakerPlaced;
                }
                let _ = self.persist_execution_state("dual-maker submissions acknowledged");
            }
            Err(err) => {
                error!(
                    "Futures maker submission outcome is ambiguous; cancelling the accepted spot leg and entering reconciliation: {}",
                    err
                );
                warn!(
                    "Fail-closing {} after futures order submission failed; cancelling resting spot leg",
                    chase_snapshot.symbol
                );
                let spot_cancel = self
                    .cancel_order_pumped(
                        LegVenue::Spot,
                        &chase_snapshot.symbol,
                        &chase_snapshot.spot_client_order_id,
                    )
                    .await;
                if spot_cancel.is_ok() {
                    if let Some(order) = self
                        .internal_orders
                        .get_mut(&chase_snapshot.spot_client_order_id)
                    {
                        order.status = "CANCELED".to_string();
                    }
                } else if let Err(cancel_err) = spot_cancel {
                    error!(
                        "Spot cancel failed during fail-closed cleanup for {}: {}",
                        chase_snapshot.symbol, cancel_err
                    );
                }
                let _ = self.persist_execution_state("dual-maker failure cleanup");
                let durable_chase = self
                    .chase_states
                    .get(&sym_upper)
                    .cloned()
                    .unwrap_or_else(|| chase_snapshot.clone());
                self.require_chase_reconciliation(
                    &sym_upper,
                    durable_chase,
                    &chase_snapshot.futures_client_order_id,
                    "FUTURES_SUBMISSION_OUTCOME_UNKNOWN",
                );
            }
        }
    }

    async fn runtime_position_audit(&mut self) {
        if self.trading_mode == "paper" || self.state != SystemState::Trading {
            return;
        }
        info!("Running periodic position audit...");

        // 1. Fetch spot balances
        let acc_res = self.get_spot_account_pumped().await;
        match acc_res {
            Ok(json_str) => {
                if let Ok(json) = serde_json::from_str::<serde_json::Value>(&json_str) {
                    if let Some(balances) = json.get("balances").and_then(|v| v.as_array()) {
                        for b in balances {
                            if let (Some(asset), Some(free), Some(locked)) = (
                                b.get("asset").and_then(|v| v.as_str()),
                                b.get("free").and_then(|v| v.as_str()),
                                b.get("locked").and_then(|v| v.as_str()),
                            ) {
                                if let (Ok(f), Ok(l)) = (free.parse::<f64>(), locked.parse::<f64>())
                                {
                                    self.spot_balances.insert(asset.to_string(), f + l);
                                }
                            }
                        }
                    }
                }
            }
            Err(e) => {
                error!("Position audit failed to fetch spot account: {}", e);
                return;
            }
        }

        // 2. Fetch futures positions
        let pos_res = self.get_futures_positions_pumped().await;
        let mut exchange_positions = std::collections::HashMap::new();
        match pos_res {
            Ok(json_str) => {
                if let Ok(json) = serde_json::from_str::<serde_json::Value>(&json_str) {
                    if let Some(positions) = json.as_array() {
                        for p in positions {
                            if let (Some(symbol), Some(pos_amt_str)) = (
                                p.get("symbol").and_then(|v| v.as_str()),
                                p.get("positionAmt").and_then(|v| v.as_str()),
                            ) {
                                if let Ok(pos_amt) = pos_amt_str.parse::<f64>() {
                                    if pos_amt.abs() > 0.0 {
                                        exchange_positions.insert(symbol.to_uppercase(), pos_amt);
                                    }
                                }
                            }
                        }
                    }
                }
            }
            Err(e) => {
                error!("Position audit failed to fetch futures positions: {}", e);
                return;
            }
        }

        // 3. Reconcile
        let local_symbols: Vec<String> = self.tracked_positions.keys().cloned().collect();
        for symbol in local_symbols {
            let local_qty = match self.tracked_positions.get(&symbol) {
                Some(pos) => {
                    if let Some(perp) = &pos.perp {
                        if perp.side == "BUY" {
                            perp.quantity
                        } else {
                            -perp.quantity
                        }
                    } else {
                        continue;
                    }
                }
                None => continue,
            };

            let exchange_qty = exchange_positions.get(&symbol).cloned().unwrap_or(0.0);
            if exchange_qty == 0.0 && local_qty != 0.0 {
                // Local only divergence
                self.tracked_positions.remove(&symbol);
                if let Ok(vec) = rmp_serde::to_vec_named(&WsEvent::PositionDivergence {
                    symbol: symbol.clone(),
                    divergence_type: "local_only".to_string(),
                    local_qty,
                    exchange_qty,
                }) {
                    let _ = self.dash_tx.send(vec);
                }
                warn!(
                    "Position Audit: Divergence for {} - local_qty: {}, exchange_qty: 0.0 (Local removed)",
                    symbol, local_qty
                );
            } else if (exchange_qty - local_qty).abs() / exchange_qty.abs().max(1e-8) > 0.01 {
                // Quantity mismatch > 1%
                if let Some(pos) = self.tracked_positions.get_mut(&symbol) {
                    if let Some(perp) = &mut pos.perp {
                        perp.quantity = exchange_qty.abs();
                        perp.side = if exchange_qty > 0.0 {
                            "BUY".to_string()
                        } else {
                            "SELL".to_string()
                        };
                    }
                }
                if let Ok(vec) = rmp_serde::to_vec_named(&WsEvent::PositionDivergence {
                    symbol: symbol.clone(),
                    divergence_type: "qty_mismatch".to_string(),
                    local_qty,
                    exchange_qty,
                }) {
                    let _ = self.dash_tx.send(vec);
                }
                warn!(
                    "Position Audit: Divergence for {} - local_qty: {}, exchange_qty: {} (Local updated)",
                    symbol, local_qty, exchange_qty
                );
            }
            exchange_positions.remove(&symbol);
        }

        // Check for untracked exchange positions
        for (symbol, exchange_qty) in exchange_positions {
            if let Ok(vec) = rmp_serde::to_vec_named(&WsEvent::PositionDivergence {
                symbol: symbol.clone(),
                divergence_type: "exchange_only".to_string(),
                local_qty: 0.0,
                exchange_qty,
            }) {
                let _ = self.dash_tx.send(vec);
            }
            warn!(
                "Position Audit: Divergence for {} - Untracked position found (exchange_qty: {})",
                symbol, exchange_qty
            );
        }

        info!("Position audit complete.");
    }

    async fn execute_reconciliation_sequence(&mut self) {
        if self.execution_state_journal_error.is_some() {
            self.state = SystemState::Reconciling;
            self.emit_execution_readiness("BLOCKED", "execution_state_journal_unavailable");
            self.emit_execution_recovery_required("execution_state_journal_unavailable");
            error!("Execution state journal is unavailable or corrupt; refusing Trading readiness");
            return;
        }

        // Skip reconciliation in paper mode — no real account to reconcile
        if self.trading_mode == "paper" {
            if self.has_unresolved_execution_effects() {
                self.state = SystemState::Reconciling;
                self.emit_execution_readiness("BLOCKED", "paper_execution_state_unresolved");
                self.emit_execution_recovery_required("paper_execution_state_unresolved");
                warn!(
                    "Paper execution state contains unresolved order effects; remaining Reconciling"
                );
                return;
            }
            info!("Paper mode: no unresolved durable execution effects; entering Trading.");
            self.state = SystemState::Trading;
            self.emit_execution_readiness("READY", "paper execution state reconciled");
            return;
        }

        self.state = SystemState::Reconciling;
        self.emit_execution_readiness("RECONCILING", "exchange reconciliation in progress");
        info!("=== Beginning Reconciliation Sequence ===");

        if let Err(e) = self.sync_time_pumped().await {
            warn!("Failed to sync time with Binance: {}", e);
            self.emit_execution_readiness("BLOCKED", "exchange_time_sync_unavailable");
            return;
        }
        info!("Time synced successfully with Binance");

        info!("[Step 1] Pausing trading signal generation.");

        let jitter_ms = rand::thread_rng().gen_range(500..2500);
        info!(
            "[Step 2] Applying Jittered Backoff of {}ms before REST sync...",
            jitter_ms
        );
        sleep(Duration::from_millis(jitter_ms)).await;

        info!("[Step 2b] Fetching spot and futures open orders from Exchange...");
        let futures_open_orders_json =
            match self.get_open_orders_pumped(LegVenue::UsdtFutures).await {
                Ok(json) => json,
                Err(e) => {
                    warn!(
                        "Failed to fetch futures open orders: {}. Will retry reconciliation later.",
                        e
                    );
                    self.emit_execution_readiness("BLOCKED", "futures_open_orders_unavailable");
                    return;
                }
            };
        let spot_open_orders_json = match self.get_open_orders_pumped(LegVenue::Spot).await {
            Ok(json) => json,
            Err(e) => {
                warn!(
                    "Failed to fetch spot open orders: {}. Will retry reconciliation later.",
                    e
                );
                self.emit_execution_readiness("BLOCKED", "spot_open_orders_unavailable");
                return;
            }
        };

        let mut exchange_open_orders = Vec::<Value>::new();
        for (body, market_name) in [
            (&futures_open_orders_json, "perp"),
            (&spot_open_orders_json, "spot"),
        ] {
            let parsed = match serde_json::from_str::<Vec<Value>>(body) {
                Ok(parsed) => parsed,
                Err(err) => {
                    warn!("Failed to parse {market_name} open orders JSON: {err}");
                    self.emit_execution_readiness("BLOCKED", "open_orders_invalid_json");
                    return;
                }
            };
            for mut order in parsed {
                if let Some(object) = order.as_object_mut() {
                    object.insert(
                        "_bongus_market".to_string(),
                        Value::String(market_name.to_string()),
                    );
                }
                exchange_open_orders.push(order);
            }
        }

        info!("Fetching Account Balances...");
        let account_json = match self.get_futures_account_pumped().await {
            Ok(json) => json,
            Err(err) => {
                warn!("Failed to fetch futures account during reconciliation: {err}");
                self.emit_execution_readiness("BLOCKED", "futures_account_unavailable");
                return;
            }
        };
        let parsed_acc = match serde_json::from_str::<Value>(&account_json) {
            Ok(value) => value,
            Err(err) => {
                warn!("Failed to parse futures account during reconciliation: {err}");
                self.emit_execution_readiness("BLOCKED", "futures_account_invalid_json");
                return;
            }
        };
        let mut balances_map = serde_json::Map::new();
        let Some(assets) = parsed_acc.get("assets").and_then(|v| v.as_array()) else {
            self.emit_execution_readiness("BLOCKED", "futures_account_missing_assets");
            return;
        };
        for asset in assets {
            if let (Some(asset_name), Some(wallet_balance)) = (
                asset.get("asset").and_then(|v| v.as_str()),
                asset.get("walletBalance").and_then(|v| v.as_str()),
            ) {
                if let Ok(bal) = wallet_balance.parse::<f64>() {
                    if bal.is_finite() {
                        balances_map.insert(asset_name.to_string(), serde_json::json!(bal));
                    }
                }
            }
        }
        let update_event = serde_json::json!({
            "event": "AccountUpdate",
            "balances": balances_map,
            "source": "futures",
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&update_event) {
            let _ = self.dash_tx.send(payload);
        }

        info!("Fetching Spot Account Balances...");
        let spot_json = match self.get_spot_account_pumped().await {
            Ok(json) => json,
            Err(err) => {
                warn!("Failed to fetch spot account during reconciliation: {err}");
                self.emit_execution_readiness("BLOCKED", "spot_account_unavailable");
                return;
            }
        };
        let parsed_spot = match serde_json::from_str::<Value>(&spot_json) {
            Ok(value) => value,
            Err(err) => {
                warn!("Failed to parse spot account during reconciliation: {err}");
                self.emit_execution_readiness("BLOCKED", "spot_account_invalid_json");
                return;
            }
        };
        let Some(spot_balances) = parsed_spot.get("balances").and_then(|v| v.as_array()) else {
            self.emit_execution_readiness("BLOCKED", "spot_account_missing_balances");
            return;
        };
        for asset in spot_balances {
            if let (Some(asset_name), Some(free_str)) = (
                asset.get("asset").and_then(|v| v.as_str()),
                asset.get("free").and_then(|v| v.as_str()),
            ) {
                if let Ok(bal) = free_str.parse::<f64>() {
                    if bal.is_finite() {
                        self.spot_balances.insert(asset_name.to_string(), bal);
                    }
                }
            }
        }

        info!("[Step 3/4] Mapping internal orders to exchange truth and searching for orphans.");

        let mut exchange_known_client_ids: std::collections::HashSet<String> =
            std::collections::HashSet::new();

        for order in &exchange_open_orders {
            if let Some(client_id) = order.get("clientOrderId").and_then(|v| v.as_str()) {
                exchange_known_client_ids.insert(client_id.to_string());

                if client_id.starts_with("bngs_") && !self.internal_orders.contains_key(client_id) {
                    warn!(
                        "FOUND ORPHAN: Exchange has active order {}, but internal state does not.",
                        client_id
                    );
                    if let Some(symbol) = order.get("symbol").and_then(|v| v.as_str()) {
                        info!(
                            "    -> Issuing REST DELETE for orphan order {} ({})",
                            client_id, symbol
                        );
                        let market = order
                            .get("_bongus_market")
                            .and_then(|value| value.as_str())
                            .unwrap_or("perp");
                        let cancel_result = self
                            .cancel_order_pumped(
                                if market == "spot" {
                                    LegVenue::Spot
                                } else {
                                    LegVenue::UsdtFutures
                                },
                                symbol,
                                client_id,
                            )
                            .await;
                        if let Err(err) = cancel_result {
                            warn!("Failed to cancel bot-owned {market} orphan {client_id}: {err}");
                            self.emit_execution_readiness(
                                "BLOCKED",
                                "bot_owned_orphan_cancel_failed",
                            );
                            return;
                        }
                    }
                }
            }
        }

        // Resolve dangling internal orders via REST query
        let dangling: Vec<(String, String, LegVenue)> = self
            .internal_orders
            .iter()
            .filter(|(_, o)| {
                o.status == "NEW" && !exchange_known_client_ids.contains(&o.client_order_id)
            })
            .map(|(cid, o)| {
                let venue = match self
                    .order_lineage
                    .get(cid)
                    .and_then(|lineage| lineage.market)
                {
                    Some(MarketType::Spot) => LegVenue::Spot,
                    _ => LegVenue::UsdtFutures,
                };
                (cid.clone(), o.symbol.clone(), venue)
            })
            .collect();

        for (client_id, symbol, venue) in dangling {
            warn!(
                "DANGLING INTERNAL ORDER: {} not on exchange. Querying REST...",
                client_id
            );
            match self.get_order_pumped(venue, &symbol, &client_id).await {
                Ok(body) => {
                    if let Ok(json) = serde_json::from_str::<Value>(&body) {
                        let status = json
                            .get("status")
                            .and_then(|v| v.as_str())
                            .unwrap_or("UNKNOWN");
                        match status {
                            "FILLED" => {
                                if let Some(order) = self.internal_orders.get_mut(&client_id) {
                                    order.status = "FILLED".to_string();
                                }
                                info!(
                                    "Dangling order {} was actually FILLED on exchange.",
                                    client_id
                                );
                            }
                            "CANCELED" | "EXPIRED" | "REJECTED" => {
                                if let Some(order) = self.internal_orders.get_mut(&client_id) {
                                    order.status = status.to_string();
                                }
                                info!(
                                    "Dangling order {} resolved as {} on exchange.",
                                    client_id, status
                                );
                            }
                            _ => {
                                if let Some(order) = self.internal_orders.get_mut(&client_id) {
                                    order.status = format!("RECONCILED_{}", status);
                                }
                                warn!("Dangling order {} has status: {}", client_id, status);
                            }
                        }
                    }
                }
                Err(e) => {
                    warn!(
                        "Failed to query dangling order {}: {}. Marking stale.",
                        client_id, e
                    );
                    if let Some(order) = self.internal_orders.get_mut(&client_id) {
                        order.status = "STALE".to_string();
                    }
                }
            }
        }

        let _ = self.persist_execution_state("startup exchange reconciliation results");
        if self.execution_state_journal_error.is_some() || self.has_unresolved_execution_effects() {
            self.state = SystemState::Reconciling;
            self.emit_execution_readiness("BLOCKED", "startup_execution_effect_unresolved");
            self.emit_execution_recovery_required("startup_execution_effect_unresolved");
            warn!(
                "Startup reconciliation found unresolved submitted receipts/order effects; refusing Trading readiness"
            );
            return;
        }

        info!("[Step 5] State matrix synchronized (Dangling resolved, Orphans purged).");
        self.state = SystemState::Trading;
        self.emit_execution_readiness("READY", "spot and futures exchange truth reconciled");
        info!("=== System is TRADING ===");
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use sha2::{Digest, Sha256};
    use std::sync::{Arc, Mutex};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::Notify;
    use tokio::sync::{broadcast, mpsc, oneshot};
    use tokio::time::timeout;

    fn paper_test_manager() -> OrderManager {
        let (_event_tx, event_rx) = mpsc::channel(8);
        let (engine_tx, _engine_rx) = mpsc::channel(16);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(16);
        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;
        manager
    }

    fn config_sync_instruction(
        intent_id: &str,
        sequence: u64,
        pause_new_entries: bool,
        per_symbol_cap: u64,
        max_gross: u64,
    ) -> crate::ipc::AlphaInstruction {
        let canonical_json = format!(
            "{{\"max_gross_exposure_usd\":{max_gross},\"pause_new_entries\":{pause_new_entries},\"per_symbol_notional_cap_usd\":{per_symbol_cap}}}"
        );
        let config_hash = hex::encode(Sha256::digest(canonical_json.as_bytes()));
        let now_ms = OrderManager::current_time_ms();
        let mut instruction = crate::ipc::AlphaInstruction {
            schema_version: Some(crate::ipc::EXECUTION_PROTOCOL_VERSION),
            producer_id: Some("config-test-producer".to_string()),
            sequence: Some(sequence),
            created_at_ms: Some(now_ms),
            deadline_at_ms: Some(now_ms + 30_000),
            account_id: Some("account-a".to_string()),
            environment: Some("live".to_string()),
            strategy_id: Some("funding-v2".to_string()),
            cycle_id: Some(format!("cycle-{intent_id}")),
            config_version_hash: Some(config_hash),
            intent: CONFIG_SYNC_INTENT.to_string(),
            intent_id: Some(intent_id.to_string()),
            config_canonical_json: Some(canonical_json),
            ..crate::ipc::AlphaInstruction::default()
        };
        instruction.command_hash = Some(instruction.semantic_fingerprint());
        instruction
    }

    #[test]
    fn config_sync_is_durable_replayable_and_can_only_lower_compiled_limits() {
        let mut manager = paper_test_manager();
        manager.trading_mode = "live".to_string();
        let active = config_sync_instruction("config-active", 1, false, 2_000, 9_000);
        let active_hash = active.config_version_hash.clone().unwrap();
        manager.handle_config_sync_instruction(active.clone());

        assert_eq!(
            manager.config_consensus.applied_hash(),
            Some(active_hash.as_str())
        );
        assert_eq!(manager.active_per_symbol_notional_cap_usd(), 2_000.0);
        assert_eq!(manager.max_gross_exposure_usd, 9_000.0);
        assert_eq!(
            manager.config_consensus.entry_block_reason(&active_hash),
            None
        );

        // Consensus is process-local. Replaying the durable terminal command
        // after a simulated restart must reconstruct it, not return early.
        manager.config_consensus = ConfigConsensus::default();
        manager.max_gross_exposure_usd = manager.compiled_max_gross_exposure_usd;
        manager.handle_config_sync_instruction(active);
        assert_eq!(
            manager.config_consensus.applied_hash(),
            Some(active_hash.as_str())
        );
        assert_eq!(manager.max_gross_exposure_usd, 9_000.0);

        let paused = config_sync_instruction("config-paused", 2, true, 1_500, 8_000);
        let paused_hash = paused.config_version_hash.clone().unwrap();
        manager.handle_config_sync_instruction(paused);
        assert_eq!(
            manager.config_consensus.entry_block_reason(&paused_hash),
            Some("config_pause_new_entries")
        );

        let raised = config_sync_instruction("config-raised", 3, false, 2_501, 10_001);
        manager.handle_config_sync_instruction(raised);
        assert_eq!(
            manager.config_consensus.applied_hash(),
            Some(paused_hash.as_str())
        );
        assert_eq!(manager.max_gross_exposure_usd, 8_000.0);
    }

    #[tokio::test]
    async fn nonpaper_entry_requires_config_consensus_but_reduce_only_exit_does_not() {
        let mut manager = paper_test_manager();
        manager.trading_mode = "live".to_string();
        let mut dashboard = manager.dash_tx.subscribe();
        manager
            .handle_alpha_instruction(
                crate::ipc::AlphaInstruction {
                    symbol: Some("BTCUSDT".to_string()),
                    intent: "ENTER_LONG".to_string(),
                    quantity: 1.0,
                    urgency: 0.5,
                    max_slippage_bps: 5.0,
                    exposure_scale: 1.0,
                    intent_id: Some("entry-without-consensus".to_string()),
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;
        assert!(!manager.chase_states.contains_key("BTCUSDT"));

        let mut rejection_reason = None;
        for _ in 0..6 {
            let payload = timeout(Duration::from_secs(1), dashboard.recv())
                .await
                .expect("entry rejection event")
                .expect("dashboard broadcast");
            let event: serde_json::Value = rmp_serde::from_slice(&payload).unwrap();
            if event.get("event").and_then(|value| value.as_str()) == Some("OrderRejected") {
                rejection_reason = event
                    .get("reason")
                    .and_then(|value| value.as_str())
                    .map(str::to_string);
                break;
            }
        }
        assert_eq!(
            rejection_reason.as_deref(),
            Some("config_consensus_unavailable")
        );

        manager.state = SystemState::Reconciling;
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: None,
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 100.0,
                    quantity: 1.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
            },
        );
        manager
            .handle_alpha_instruction(
                crate::ipc::AlphaInstruction {
                    symbol: Some("BTCUSDT".to_string()),
                    intent: "EXIT_LONG".to_string(),
                    quantity: 1.0,
                    urgency: 1.0,
                    max_slippage_bps: 20.0,
                    exposure_scale: 1.0,
                    intent_id: Some("exit-without-consensus".to_string()),
                    direction: Some("long".to_string()),
                    skip_spot_leg: true,
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;
        assert!(
            manager.chase_states.contains_key("BTCUSDT"),
            "config consensus is an entry-only gate"
        );
    }

    #[tokio::test]
    async fn prospective_entry_limits_reject_before_any_exchange_submission() {
        let mut manager = paper_test_manager();
        manager.trading_mode = "live".to_string();
        manager.handle_config_sync_instruction(config_sync_instruction(
            "config-risk-limits",
            1,
            false,
            2_000,
            9_000,
        ));
        for cache in [&mut manager.spot_top_cache, &mut manager.perp_top_cache] {
            cache.insert(
                "BTCUSDT".to_string(),
                TopOfBook {
                    bid_price: 99.9,
                    ask_price: 100.0,
                    bid_qty: 100.0,
                    ask_qty: 100.0,
                },
            );
        }

        let mut oversized = dual_test_chase(21.0);
        oversized.phase = ChasePhase::Idle;
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), oversized);
        manager.try_place_dual_maker("BTCUSDT".to_string()).await;
        assert!(
            !manager.chase_states.contains_key("BTCUSDT"),
            "a $2,100 leg must be rejected against the synced $2,000 symbol cap"
        );
        assert!(manager.internal_orders.is_empty());

        let mut gross_breach = dual_test_chase(10.0);
        gross_breach.phase = ChasePhase::Idle;
        manager.current_gross_exposure_usd = 8_000.0;
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), gross_breach);
        manager.try_place_dual_maker("BTCUSDT".to_string()).await;
        assert!(
            !manager.chase_states.contains_key("BTCUSDT"),
            "the two proposed $1,000 legs must not take gross above $9,000"
        );
        assert!(manager.internal_orders.is_empty());
    }

    fn dual_test_chase(quantity: f64) -> ChaseState {
        ChaseState {
            symbol: "BTCUSDT".to_string(),
            quantity,
            spot_quantity: quantity,
            perp_quantity: quantity,
            spot_client_order_id: "spot-cid".to_string(),
            futures_client_order_id: "fut-cid".to_string(),
            skip_spot_leg: false,
            skip_perp_leg: false,
            spot_side: TradeSide::Buy,
            futures_side: TradeSide::Sell,
            is_exit: false,
            max_slippage_bps: 200.0,
            phase: ChasePhase::DualMakerPlaced,
            start_time: Instant::now(),
            expected_spot_price: 100.0,
            expected_fut_price: 101.0,
            spot_fill_price: None,
            futures_fill_price: None,
            spot_cumulative_filled: 0.0,
            futures_cumulative_filled: 0.0,
            spot_terminal: false,
            futures_terminal: false,
        }
    }

    fn test_exchange_symbol(symbol: &str) -> crate::binance_rest::ExchangeSymbolInfo {
        crate::binance_rest::ExchangeSymbolInfo {
            symbol: symbol.to_string(),
            spot_tick_size: 0.01,
            spot_step_size: 0.001,
            spot_max_qty: 100.0,
            spot_min_notional: 5.0,
            futures_tick_size: 0.01,
            futures_step_size: 0.001,
            futures_max_qty: 100.0,
            futures_min_notional: 5.0,
        }
    }

    #[test]
    fn active_cycle_filter_or_status_change_preserves_chase_and_revokes_readiness() {
        let baseline = test_exchange_symbol("BTCUSDT");
        let eth = test_exchange_symbol("ETHUSDT");
        let mut variants = Vec::new();

        let mut tick_changed = baseline.clone();
        tick_changed.spot_tick_size = 0.1;
        variants.push(Some(tick_changed));
        let mut lot_changed = baseline.clone();
        lot_changed.futures_step_size = 0.01;
        variants.push(Some(lot_changed));
        let mut minimum_changed = baseline.clone();
        minimum_changed.spot_min_notional = 10.0;
        variants.push(Some(minimum_changed));
        // A non-TRADING/missing leg is omitted by BinanceRest parsing. Keep an
        // unrelated tradable symbol so this is a valid non-empty snapshot.
        variants.push(None);

        for replacement in variants {
            let mut manager = paper_test_manager();
            manager
                .exchange_info
                .insert("BTCUSDT".to_string(), baseline.clone());
            manager
                .exchange_info
                .insert("ETHUSDT".to_string(), eth.clone());
            manager.exchange_info_updated_at = Some(Instant::now());
            manager
                .chase_states
                .insert("BTCUSDT".to_string(), dual_test_chase(1.0));

            let mut next = HashMap::from([("ETHUSDT".to_string(), eth.clone())]);
            if let Some(value) = replacement {
                next.insert("BTCUSDT".to_string(), value);
            }
            manager.apply_exchange_info_refresh(Ok(next.clone()));

            assert_eq!(manager.state, SystemState::Reconciling);
            assert!(manager.chase_states.contains_key("BTCUSDT"));
            assert_eq!(manager.exchange_info, next);
            assert!(manager.exchange_metadata_fresh());
        }

        let mut unchanged = paper_test_manager();
        unchanged
            .exchange_info
            .insert("BTCUSDT".to_string(), baseline.clone());
        unchanged.exchange_info_updated_at = Some(Instant::now());
        unchanged
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(1.0));
        unchanged
            .apply_exchange_info_refresh(Ok(HashMap::from([("BTCUSDT".to_string(), baseline)])));
        assert_eq!(unchanged.state, SystemState::Trading);
    }

    fn test_order_update(
        client_order_id: &str,
        status: &str,
        last_fill_qty: f64,
        cumulative_fill_qty: f64,
        market: MarketType,
        price: f64,
    ) -> WsEvent {
        WsEvent::OrderUpdate {
            client_order_id: client_order_id.to_string(),
            symbol: "BTCUSDT".to_string(),
            status: status.to_string(),
            filled_qty: last_fill_qty,
            cumulative_filled_qty: Some(cumulative_fill_qty),
            avg_fill_price: Some(price),
            last_fill_price: Some(price),
            cumulative_quote_qty: Some(cumulative_fill_qty * price),
            commission: None,
            commission_asset: None,
            realized_pnl: None,
            maker: Some(true),
            execution_type: Some("TRADE".to_string()),
            event_time_ms: Some(1),
            maker_fills: None,
            taker_fills: None,
            market: Some(market),
            side: Some(
                if market == MarketType::Spot {
                    "BUY"
                } else {
                    "SELL"
                }
                .to_string(),
            ),
            order_id: Some(10),
            trade_id: Some(20),
            account_id: Some("acct".to_string()),
            environment: Some("paper".to_string()),
            strategy_id: Some("funding-arb".to_string()),
            cycle_id: Some("cycle".to_string()),
            intent_id: Some("intent".to_string()),
            leg_id: Some(
                if market == MarketType::Spot {
                    "spot-leg"
                } else {
                    "perp-leg"
                }
                .to_string(),
            ),
            config_version_hash: Some("cfg".to_string()),
        }
    }

    #[tokio::test]
    async fn hanging_rest_wait_processes_private_fill_before_rest_completion() {
        let (event_tx, event_rx) = mpsc::channel(8);
        let (engine_tx, _engine_rx) = mpsc::channel(16);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, mut dash_rx) = broadcast::channel::<Vec<u8>>(16);
        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(1.0));

        let (release_tx, release_rx) = oneshot::channel::<()>();
        let observer = tokio::spawn(async move {
            let bytes = timeout(Duration::from_secs(1), dash_rx.recv())
                .await
                .expect("fill must be broadcast while REST is pending")
                .expect("dashboard channel must remain open");
            let payload: serde_json::Value = rmp_serde::from_slice(&bytes).unwrap();
            assert_eq!(payload["event"], "OrderUpdate");
            assert_eq!(payload["client_order_id"], "spot-cid");
            let _ = release_tx.send(());
        });
        event_tx
            .send(EngineEvent::Ws(test_order_update(
                "spot-cid",
                "PARTIALLY_FILLED",
                0.4,
                0.4,
                MarketType::Spot,
                100.0,
            )))
            .await
            .unwrap();

        let completed = timeout(
            Duration::from_secs(1),
            manager.await_rest_while_processing_ws(async move {
                release_rx
                    .await
                    .expect("fill observer releases REST fixture");
                "rest_completed"
            }),
        )
        .await
        .expect("REST fixture can complete only after the fill is processed");
        observer.await.unwrap();

        assert_eq!(completed, "rest_completed");
        let chase = manager.chase_states.get("BTCUSDT").unwrap();
        assert_eq!(chase.spot_cumulative_filled, 0.4);
        assert_eq!(manager.order_cumulative_fills.get("spot-cid"), Some(&0.4));
    }

    #[tokio::test]
    async fn fill_during_slow_spot_submit_hedges_without_duplicate_futures_limit() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let spot_submit_seen = Arc::new(Notify::new());
        let requests = Arc::new(Mutex::new(Vec::<String>::new()));
        let server_notify = spot_submit_seen.clone();
        let server_requests = requests.clone();
        let server = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };
                let notify = server_notify.clone();
                let requests = server_requests.clone();
                tokio::spawn(async move {
                    let mut request = vec![0u8; 8192];
                    let Ok(read) = socket.read(&mut request).await else {
                        return;
                    };
                    let request = String::from_utf8_lossy(&request[..read]).to_string();
                    let first_line = request.lines().next().unwrap_or("").to_string();
                    requests.lock().unwrap().push(first_line.clone());
                    let is_slow_spot_submit = first_line.starts_with("POST /api/v3/order?")
                        && first_line.contains("type=LIMIT");
                    if is_slow_spot_submit {
                        notify.notify_one();
                        tokio::time::sleep(Duration::from_millis(250)).await;
                    }
                    let body = if first_line.contains("/fapi/v1/order?") {
                        r#"{"symbol":"BTCUSDT","orderId":88,"clientOrderId":"legging","status":"NEW","executedQty":"0"}"#
                    } else {
                        r#"{"symbol":"BTCUSDT","orderId":77,"clientOrderId":"spot-cid","status":"NEW","executedQty":"0"}"#
                    };
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = socket.write_all(response.as_bytes()).await;
                });
            }
        });

        let (engine_tx, event_rx) = mpsc::channel(32);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(32);
        let mut manager = OrderManager::new(
            event_rx,
            engine_tx.clone(),
            subscription_tx,
            "test-key".to_string(),
            "test-secret".to_string(),
            dash_tx,
            "testnet".to_string(),
        );
        manager.state = SystemState::Trading;
        manager.binance_rest.spot_base_url = format!("http://{address}");
        manager.binance_rest.fut_base_url = format!("http://{address}");
        manager.binance_rest.client = reqwest::Client::builder()
            .connect_timeout(Duration::from_millis(100))
            .timeout(Duration::from_secs(2))
            .build()
            .unwrap();
        manager
            .exchange_info
            .insert("BTCUSDT".to_string(), test_exchange_symbol("BTCUSDT"));
        manager.exchange_info_updated_at = Some(Instant::now());
        manager.spot_balances.insert("USDT".to_string(), 10_000.0);
        manager.spot_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 99.9,
                ask_price: 100.0,
                bid_qty: 100.0,
                ask_qty: 100.0,
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 100.0,
                ask_price: 100.1,
                bid_qty: 100.0,
                ask_qty: 100.0,
            },
        );
        let capacity = ExecutableDepth {
            bid_notional_usd: 100_000.0,
            ask_notional_usd: 100_000.0,
            observed_at: Instant::now(),
        };
        manager
            .spot_depth_capacity
            .insert("BTCUSDT".to_string(), capacity);
        manager
            .perp_depth_capacity
            .insert("BTCUSDT".to_string(), capacity);
        let mut chase = dual_test_chase(1.0);
        chase.phase = ChasePhase::Idle;
        manager.chase_states.insert("BTCUSDT".to_string(), chase);
        manager
            .chase_unhedged_budgets
            .insert("BTCUSDT".to_string(), 1.0);

        let inject_tx = engine_tx.clone();
        let injector = tokio::spawn(async move {
            spot_submit_seen.notified().await;
            inject_tx
                .send(EngineEvent::Ws(test_order_update(
                    "spot-cid",
                    "FILLED",
                    1.0,
                    1.0,
                    MarketType::Spot,
                    100.0,
                )))
                .await
                .unwrap();
        });

        timeout(
            Duration::from_secs(3),
            manager.try_place_dual_maker("BTCUSDT".to_string()),
        )
        .await
        .expect("slow accepted spot submit must not starve private fill/hedge progress");
        injector.await.unwrap();

        let observed = requests.lock().unwrap().clone();
        assert!(
            observed
                .iter()
                .any(|line| line.contains("/api/v3/order?") && line.contains("type=LIMIT"))
        );
        assert!(
            observed
                .iter()
                .any(|line| line.contains("/fapi/v1/order?") && line.contains("type=MARKET")),
            "the fill must trigger an urgent futures market hedge: {observed:?}"
        );
        assert!(
            !observed
                .iter()
                .any(|line| line.contains("/fapi/v1/order?") && line.contains("type=LIMIT")),
            "the stale outer flow must not place a duplicate futures limit: {observed:?}"
        );
        assert_eq!(
            manager
                .internal_orders
                .get("fut-cid")
                .map(|order| order.status.as_str()),
            Some("NOT_SUBMITTED")
        );
        assert_eq!(
            manager.chase_states.get("BTCUSDT").map(|chase| chase.phase),
            Some(ChasePhase::LeggingDefenseTakerPlaced)
        );
        server.abort();
    }

    #[tokio::test]
    async fn favorable_basis_convergence_does_not_trip_long_spot_short_perp() {
        let mut manager = paper_test_manager();
        manager.basis_deviation_stop_bps = 20.0;
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: Some(TrackedLegPosition {
                    side: "LONG".to_string(),
                    entry_price: 100.0,
                    quantity: 1.0,
                    unrealized_pnl: 2.0,
                    last_mark_price: 102.0,
                }),
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 102.0,
                    quantity: 1.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 102.0,
                }),
            },
        );

        assert!(
            !manager.check_circuit_breakers().await,
            "200bps of favorable convergence must not trigger the adverse-basis stop"
        );

        manager
            .tracked_positions
            .get_mut("BTCUSDT")
            .and_then(|position| position.spot.as_mut())
            .expect("spot leg")
            .last_mark_price = 100.0;
        manager
            .tracked_positions
            .get_mut("BTCUSDT")
            .and_then(|position| position.perp.as_mut())
            .expect("perp leg")
            .last_mark_price = 103.0;
        assert!(
            manager.check_circuit_breakers().await,
            "100bps of adverse widening must trigger the stop"
        );
    }

    #[test]
    fn reverse_basis_direction_only_counts_adverse_contraction() {
        let spot = TrackedLegPosition {
            side: "SHORT".to_string(),
            entry_price: 100.0,
            quantity: 1.0,
            unrealized_pnl: 0.0,
            last_mark_price: 100.0,
        };
        let mut perp = TrackedLegPosition {
            side: "LONG".to_string(),
            entry_price: 101.0,
            quantity: 1.0,
            unrealized_pnl: 0.0,
            last_mark_price: 103.0,
        };
        assert_eq!(
            OrderManager::adverse_basis_deviation_bps(&spot, &perp),
            Some(0.0),
            "widening is favorable for short-spot/long-perp"
        );
        perp.last_mark_price = 99.0;
        assert!(
            OrderManager::adverse_basis_deviation_bps(&spot, &perp).unwrap() > 190.0,
            "basis contraction is adverse for short-spot/long-perp"
        );
    }

    #[test]
    fn max_unhedged_notional_ms_caps_legging_timeout() {
        let mut manager = paper_test_manager();
        let mut chase = dual_test_chase(1.0);
        chase.phase = ChasePhase::LegFilledWaiting(Leg::Spot);
        chase.spot_cumulative_filled = 1.0;
        chase.spot_fill_price = Some(100.0);
        chase.spot_terminal = true;
        manager
            .chase_unhedged_budgets
            .insert("BTCUSDT".to_string(), 1_000.0);
        manager
            .chase_unhedged_started_at_ms
            .insert("BTCUSDT".to_string(), OrderManager::current_time_ms());

        assert!(
            manager.bounded_legging_timeout_ms(&chase) <= 10,
            "$1,000 notional-ms budget at $100 unhedged notional permits at most 10ms"
        );
        manager
            .chase_unhedged_started_at_ms
            .insert("BTCUSDT".to_string(), OrderManager::current_time_ms() - 20);
        assert_eq!(manager.bounded_legging_timeout_ms(&chase), 1);
    }

    #[tokio::test]
    async fn durable_execution_state_recovers_lineage_and_blocks_readiness() {
        let journal_path = std::env::temp_dir().join(format!(
            "bongus-execution-restart-test-{}-{}.jsonl",
            std::process::id(),
            rand::random::<u64>()
        ));
        let mut first = paper_test_manager();
        first.execution_state_journal_path = journal_path.clone();
        let mut chase = dual_test_chase(2.0);
        chase.phase = ChasePhase::LegFilledWaiting(Leg::Spot);
        chase.spot_cumulative_filled = 2.0;
        chase.spot_fill_price = Some(100.0);
        chase.spot_terminal = true;
        first
            .chase_states
            .insert("BTCUSDT".to_string(), chase.clone());
        first.internal_orders.insert(
            "fut-cid".to_string(),
            InternalOrder {
                client_order_id: "fut-cid".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "NEW".to_string(),
                limit_price: Some(101.0),
            },
        );
        first
            .order_cumulative_fills
            .insert("spot-cid".to_string(), 2.0);
        first.order_lineage.insert(
            "spot-cid".to_string(),
            OrderLineage {
                intent_id: Some("intent-restart".to_string()),
                leg_id: Some("spot-leg".to_string()),
                market: Some(MarketType::Spot),
                side: Some("BUY".to_string()),
                ..OrderLineage::default()
            },
        );
        first
            .chase_intent_ids
            .insert("BTCUSDT".to_string(), "intent-restart".to_string());
        first
            .chase_unhedged_budgets
            .insert("BTCUSDT".to_string(), 5_000_000.0);
        assert!(first.persist_execution_state("restart test fixture"));

        let mut recovered = paper_test_manager();
        recovered
            .load_execution_state_from_path(journal_path.clone())
            .expect("durable execution snapshot should reload");
        assert_eq!(
            recovered
                .chase_states
                .get("BTCUSDT")
                .expect("recovered chase")
                .spot_cumulative_filled,
            2.0
        );
        assert_eq!(
            recovered
                .order_lineage
                .get("spot-cid")
                .and_then(|lineage| lineage.intent_id.as_deref()),
            Some("intent-restart")
        );
        recovered.execute_reconciliation_sequence().await;
        assert_eq!(recovered.state, SystemState::Reconciling);
        let _ = std::fs::remove_file(journal_path);
    }

    #[tokio::test]
    async fn reconciling_state_allows_verified_reduce_only_exit() {
        let mut manager = paper_test_manager();
        manager.state = SystemState::Reconciling;
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: None,
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 100.0,
                    quantity: 1.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 99.9,
                ask_price: 100.0,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );

        manager
            .handle_alpha_instruction(
                crate::ipc::AlphaInstruction {
                    symbol: Some("BTCUSDT".to_string()),
                    intent: "EXIT_LONG".to_string(),
                    quantity: 1.0,
                    urgency: 1.0,
                    max_slippage_bps: 20.0,
                    exposure_scale: 1.0,
                    intent_id: Some("exit-during-reconcile".to_string()),
                    direction: Some("long".to_string()),
                    skip_spot_leg: true,
                    skip_perp_leg: false,
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;

        assert_eq!(
            manager.chase_states.get("BTCUSDT").map(|chase| chase.phase),
            Some(ChasePhase::DualMakerPlaced),
            "entry readiness state must not block an exposure-clamped exit"
        );
        assert_eq!(
            manager
                .internal_orders
                .values()
                .find(|order| order.symbol == "BTCUSDT")
                .map(|order| order.status.as_str()),
            Some("FILLED_PENDING"),
            "verified exit should reach the paper execution boundary"
        );
    }

    #[tokio::test]
    async fn depth_sequence_gap_is_rejected_and_broadcast_without_affecting_other_symbols() {
        let mut manager = paper_test_manager();
        let mut dashboard = manager.dash_tx.subscribe();
        manager
            .handle_ws_event(WsEvent::L2Depth {
                symbol: "BTCUSDT".to_string(),
                market: MarketType::Perp,
                bids: vec![[100.0, 1.0]],
                asks: vec![[101.0, 1.0]],
                first_update_id: Some(10),
                final_update_id: Some(10),
                previous_final_update_id: None,
                is_snapshot: false,
            })
            .await;
        manager
            .depth_sequences
            .insert("ETHUSDT:perp".to_string(), 77);
        assert_eq!(manager.depth_sequences.get("BTCUSDT:perp"), Some(&10));

        manager
            .handle_ws_event(WsEvent::L2Depth {
                symbol: "BTCUSDT".to_string(),
                market: MarketType::Perp,
                bids: vec![[200.0, 1.0]],
                asks: vec![[201.0, 1.0]],
                first_update_id: Some(12),
                final_update_id: Some(12),
                previous_final_update_id: Some(11),
                is_snapshot: false,
            })
            .await;

        assert_eq!(manager.depth_sequences.get("BTCUSDT:perp"), Some(&10));
        assert_eq!(manager.depth_sequences.get("ETHUSDT:perp"), Some(&77));
        assert!(!manager.perp_top_cache.contains_key("BTCUSDT"));

        let mut found_gap = false;
        for _ in 0..3 {
            let bytes = timeout(Duration::from_millis(250), dashboard.recv())
                .await
                .expect("depth telemetry")
                .expect("broadcast open");
            let value: serde_json::Value =
                rmp_serde::from_slice(&bytes).expect("valid messagepack telemetry");
            if value.get("event").and_then(|item| item.as_str()) == Some("FeedGap") {
                assert_eq!(
                    value.get("last_update_id").and_then(|item| item.as_u64()),
                    Some(10)
                );
                assert_eq!(
                    value.get("final_update_id").and_then(|item| item.as_u64()),
                    Some(12)
                );
                found_gap = true;
                break;
            }
        }
        assert!(found_gap, "FeedGap telemetry was not emitted");
    }

    #[tokio::test]
    async fn cumulative_partial_updates_are_idempotent() {
        let mut manager = paper_test_manager();
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(10.0));

        let event = test_order_update(
            "spot-cid",
            "PARTIALLY_FILLED",
            2.0,
            2.0,
            MarketType::Spot,
            100.0,
        );
        manager.handle_ws_event(event.clone()).await;
        manager.handle_ws_event(event).await;

        let chase = manager.chase_states.get("BTCUSDT").expect("active chase");
        assert!((chase.spot_cumulative_filled - 2.0).abs() < 1e-12);
        assert!(!chase.spot_terminal);
        let spot = manager
            .tracked_positions
            .get("BTCUSDT")
            .and_then(|position| position.spot.as_ref())
            .expect("tracked spot fill");
        assert!((spot.quantity - 2.0).abs() < 1e-12);
    }

    #[tokio::test]
    async fn stale_cumulative_regression_cannot_reverse_or_double_progress() {
        let mut manager = paper_test_manager();
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(10.0));
        manager
            .handle_ws_event(test_order_update(
                "spot-cid",
                "PARTIALLY_FILLED",
                5.0,
                5.0,
                MarketType::Spot,
                100.0,
            ))
            .await;
        manager
            .handle_ws_event(test_order_update(
                "spot-cid",
                "PARTIALLY_FILLED",
                3.0,
                3.0,
                MarketType::Spot,
                99.0,
            ))
            .await;

        let chase = manager.chase_states.get("BTCUSDT").expect("active chase");
        assert!((chase.spot_cumulative_filled - 5.0).abs() < 1e-12);
        let spot = manager
            .tracked_positions
            .get("BTCUSDT")
            .and_then(|position| position.spot.as_ref())
            .expect("tracked spot fill");
        assert!((spot.quantity - 5.0).abs() < 1e-12);
    }

    #[tokio::test]
    async fn terminal_underfill_requires_reconciliation_and_preserves_exposure() {
        let mut manager = paper_test_manager();
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(10.0));
        manager
            .handle_ws_event(test_order_update(
                "spot-cid",
                "FILLED",
                3.0,
                3.0,
                MarketType::Spot,
                100.0,
            ))
            .await;

        let chase = manager
            .chase_states
            .get("BTCUSDT")
            .expect("ambiguous chase retained");
        assert_eq!(chase.phase, ChasePhase::ReconciliationRequired);
        assert!((chase.spot_cumulative_filled - 3.0).abs() < 1e-12);
        assert!(!chase.spot_terminal);
    }

    #[tokio::test]
    async fn overfill_requires_reconciliation_instead_of_completing_cycle() {
        let mut manager = paper_test_manager();
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(10.0));
        manager
            .handle_ws_event(test_order_update(
                "spot-cid",
                "FILLED",
                11.0,
                11.0,
                MarketType::Spot,
                100.0,
            ))
            .await;

        let chase = manager
            .chase_states
            .get("BTCUSDT")
            .expect("overfill retained");
        assert_eq!(chase.phase, ChasePhase::ReconciliationRequired);
        assert!((chase.spot_cumulative_filled - 11.0).abs() < 1e-12);
    }

    #[tokio::test]
    async fn cycle_completes_only_after_both_legs_reach_terminal_targets() {
        let mut manager = paper_test_manager();
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(10.0));
        manager
            .handle_ws_event(test_order_update(
                "spot-cid",
                "FILLED",
                10.0,
                10.0,
                MarketType::Spot,
                100.0,
            ))
            .await;
        manager
            .handle_ws_event(test_order_update(
                "fut-cid",
                "PARTIALLY_FILLED",
                9.0,
                9.0,
                MarketType::Perp,
                101.0,
            ))
            .await;
        assert!(manager.chase_states.contains_key("BTCUSDT"));

        manager
            .handle_ws_event(test_order_update(
                "fut-cid",
                "FILLED",
                1.0,
                10.0,
                MarketType::Perp,
                101.0,
            ))
            .await;
        assert!(!manager.chase_states.contains_key("BTCUSDT"));
    }

    #[test]
    fn toxicity_log_action_refreshes_only_after_cooldown() {
        let now = Instant::now();
        assert_eq!(
            toxicity_log_action(75.0, false, None, now),
            ToxicityLogAction::Enter
        );
        assert_eq!(
            toxicity_log_action(75.0, true, Some(now), now + Duration::from_secs(10)),
            ToxicityLogAction::None
        );
        assert_eq!(
            toxicity_log_action(75.0, true, Some(now), now + Duration::from_secs(31)),
            ToxicityLogAction::Refresh
        );
    }

    #[test]
    fn toxicity_log_action_emits_exit_on_recovery() {
        let now = Instant::now();
        assert_eq!(
            toxicity_log_action(12.0, true, Some(now), now + Duration::from_secs(5)),
            ToxicityLogAction::Exit
        );
        assert_eq!(
            toxicity_log_action(12.0, false, None, now + Duration::from_secs(5)),
            ToxicityLogAction::None
        );
    }

    #[tokio::test]
    async fn paper_dual_maker_waits_for_cross_before_filling() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, mut engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;

        manager.chase_states.insert(
            "BTCUSDT".to_string(),
            ChaseState {
                symbol: "BTCUSDT".to_string(),
                quantity: 1.0,
                spot_quantity: 1.0,
                perp_quantity: 1.0,
                spot_client_order_id: "spot-cid".to_string(),
                futures_client_order_id: "fut-cid".to_string(),
                skip_spot_leg: false,
                skip_perp_leg: false,
                spot_side: TradeSide::Buy,
                futures_side: TradeSide::Sell,
                is_exit: false,
                max_slippage_bps: 20.0,
                phase: ChasePhase::Idle,
                start_time: Instant::now(),
                expected_spot_price: 0.0,
                expected_fut_price: 0.0,
                spot_fill_price: None,
                futures_fill_price: None,
                spot_cumulative_filled: 0.0,
                futures_cumulative_filled: 0.0,
                spot_terminal: false,
                futures_terminal: false,
            },
        );
        manager.spot_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 100.0,
                ask_price: 101.0,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 103.0,
                ask_price: 104.0,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );

        manager.try_place_dual_maker("BTCUSDT".to_string()).await;

        let no_fill = timeout(Duration::from_millis(150), engine_rx.recv()).await;
        assert!(
            no_fill.is_err(),
            "paper maker orders should rest until the book crosses"
        );

        manager
            .handle_ws_event(WsEvent::L2Depth {
                symbol: "BTCUSDT".to_string(),
                market: MarketType::Spot,
                bids: vec![[99.0, 1.0]],
                asks: vec![[100.0, 1.0]],
                first_update_id: None,
                final_update_id: None,
                previous_final_update_id: None,
                is_snapshot: true,
            })
            .await;
        manager
            .handle_ws_event(WsEvent::L2Depth {
                symbol: "BTCUSDT".to_string(),
                market: MarketType::Perp,
                bids: vec![[104.0, 1.0]],
                asks: vec![[104.5, 1.0]],
                first_update_id: None,
                final_update_id: None,
                previous_final_update_id: None,
                is_snapshot: true,
            })
            .await;

        let first = timeout(Duration::from_millis(400), engine_rx.recv())
            .await
            .expect("first paper fill event should arrive")
            .expect("first paper fill event should be present");
        let second = timeout(Duration::from_millis(400), engine_rx.recv())
            .await
            .expect("second paper fill event should arrive")
            .expect("second paper fill event should be present");

        match first {
            EngineEvent::Ws(WsEvent::OrderUpdate {
                client_order_id,
                avg_fill_price,
                execution_type,
                ..
            }) => {
                assert_eq!(client_order_id, "spot-cid");
                assert_eq!(avg_fill_price, Some(100.0));
                assert_eq!(execution_type.as_deref(), Some("PAPER_RESTING_CROSS_FILL"));
            }
            other => panic!(
                "unexpected first engine event: {:?}",
                other_type_name(&other)
            ),
        }

        match second {
            EngineEvent::Ws(WsEvent::OrderUpdate {
                client_order_id,
                avg_fill_price,
                execution_type,
                ..
            }) => {
                assert_eq!(client_order_id, "fut-cid");
                assert_eq!(avg_fill_price, Some(104.0));
                assert_eq!(execution_type.as_deref(), Some("PAPER_RESTING_CROSS_FILL"));
            }
            other => panic!(
                "unexpected second engine event: {:?}",
                other_type_name(&other)
            ),
        }
    }

    #[tokio::test]
    async fn single_leg_exit_fill_completes_without_waiting_for_second_leg() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: None,
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 104.0,
                    quantity: 1.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 104.0,
                }),
            },
        );
        manager.chase_states.insert(
            "BTCUSDT".to_string(),
            ChaseState {
                symbol: "BTCUSDT".to_string(),
                quantity: 1.0,
                spot_quantity: 0.0,
                perp_quantity: 1.0,
                spot_client_order_id: String::new(),
                futures_client_order_id: "fut-cid".to_string(),
                skip_spot_leg: true,
                skip_perp_leg: false,
                spot_side: TradeSide::Sell,
                futures_side: TradeSide::Buy,
                is_exit: true,
                max_slippage_bps: 20.0,
                phase: ChasePhase::DualMakerPlaced,
                start_time: Instant::now(),
                expected_spot_price: 0.0,
                expected_fut_price: 103.5,
                spot_fill_price: None,
                futures_fill_price: None,
                spot_cumulative_filled: 0.0,
                futures_cumulative_filled: 0.0,
                spot_terminal: false,
                futures_terminal: false,
            },
        );
        manager.internal_orders.insert(
            "fut-cid".to_string(),
            InternalOrder {
                client_order_id: "fut-cid".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "NEW".to_string(),
                limit_price: Some(103.5),
            },
        );

        manager
            .handle_ws_event(WsEvent::OrderUpdate {
                client_order_id: "fut-cid".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "FILLED".to_string(),
                filled_qty: 1.0,
                cumulative_filled_qty: Some(1.0),
                avg_fill_price: Some(103.5),
                last_fill_price: Some(103.5),
                cumulative_quote_qty: Some(103.5),
                commission: None,
                commission_asset: None,
                realized_pnl: None,
                maker: Some(false),
                execution_type: Some("TRADE".to_string()),
                event_time_ms: Some(0),
                maker_fills: None,
                taker_fills: None,
                market: Some(MarketType::Perp),
                side: Some("BUY".to_string()),
                order_id: None,
                trade_id: None,
                account_id: None,
                environment: None,
                strategy_id: None,
                cycle_id: None,
                intent_id: None,
                leg_id: None,
                config_version_hash: None,
            })
            .await;

        assert!(
            !manager.chase_states.contains_key("BTCUSDT"),
            "single-leg unwind should complete immediately after the active leg fills"
        );
        assert!(
            !manager.tracked_positions.contains_key("BTCUSDT"),
            "filled single-leg exit should flatten the tracked futures position"
        );
        assert_eq!(manager.taker_fills, 1);
    }

    #[tokio::test]
    async fn ambiguous_single_leg_submission_requires_reconciliation() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, mut dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "live".to_string(),
        );
        manager.state = SystemState::Trading;
        manager.binance_rest.fut_base_url = "http://127.0.0.1:1".to_string();
        manager.spot_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 100.0,
                ask_price: 101.0,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 103.0,
                ask_price: 103.5,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );
        manager.chase_states.insert(
            "BTCUSDT".to_string(),
            ChaseState {
                symbol: "BTCUSDT".to_string(),
                quantity: 1.0,
                spot_quantity: 0.0,
                perp_quantity: 1.0,
                spot_client_order_id: String::new(),
                futures_client_order_id: "fut-cid".to_string(),
                skip_spot_leg: true,
                skip_perp_leg: false,
                spot_side: TradeSide::Sell,
                futures_side: TradeSide::Buy,
                is_exit: true,
                max_slippage_bps: 20.0,
                phase: ChasePhase::Idle,
                start_time: Instant::now(),
                expected_spot_price: 0.0,
                expected_fut_price: 0.0,
                spot_fill_price: None,
                futures_fill_price: None,
                spot_cumulative_filled: 0.0,
                futures_cumulative_filled: 0.0,
                spot_terminal: false,
                futures_terminal: false,
            },
        );

        manager.try_place_dual_maker("BTCUSDT".to_string()).await;

        let reject_msg = timeout(Duration::from_secs(1), dash_rx.recv())
            .await
            .expect("single-leg reconciliation requirement should be broadcast")
            .expect("broadcast payload should be present");
        let payload: serde_json::Value =
            rmp_serde::from_slice(&reject_msg).expect("broadcast payload should be valid msgpack");

        assert_eq!(
            payload.get("event").and_then(|v| v.as_str()),
            Some("OrderUpdate")
        );
        assert_eq!(
            payload.get("symbol").and_then(|v| v.as_str()),
            Some("BTCUSDT")
        );
        assert_eq!(
            payload.get("status").and_then(|v| v.as_str()),
            Some("RECONCILIATION_REQUIRED")
        );
        assert_eq!(
            payload.get("execution_type").and_then(|v| v.as_str()),
            Some("SINGLE_LEG_SUBMISSION_FAILED")
        );
        assert!(
            manager.chase_states.contains_key("BTCUSDT"),
            "ambiguous submission must retain its deterministic client id for reconciliation"
        );
        assert_eq!(manager.state, SystemState::Reconciling);
    }

    #[tokio::test]
    async fn exit_short_with_skip_perp_leg_is_rejected_before_submission() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, mut dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "live".to_string(),
        );
        manager.state = SystemState::Trading;

        manager
            .handle_alpha_instruction(
                crate::ipc::AlphaInstruction {
                    symbol: Some("ATAUSDT".to_string()),
                    intent: "EXIT_SHORT".to_string(),
                    quantity: 1.0,
                    urgency: 1.0,
                    max_slippage_bps: 20.0,
                    exposure_scale: 1.0,
                    heartbeat_id: None,
                    intent_id: Some("intent-ata-1".to_string()),
                    direction: Some("short".to_string()),
                    skip_spot_leg: false,
                    skip_perp_leg: true,
                    spot_entry_price: None,
                    perp_entry_price: None,
                    spot_mark_price: None,
                    perp_mark_price: None,
                    spot_quantity: None,
                    perp_quantity: None,
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;

        let payload = loop {
            let reject_msg = timeout(Duration::from_secs(1), dash_rx.recv())
                .await
                .expect("invalid EXIT_SHORT flags should be rejected")
                .expect("broadcast payload should be present");
            let payload: serde_json::Value = rmp_serde::from_slice(&reject_msg)
                .expect("broadcast payload should be valid msgpack");
            if payload.get("event").and_then(|value| value.as_str()) == Some("OrderRejected") {
                break payload;
            }
        };

        assert_eq!(
            payload.get("event").and_then(|v| v.as_str()),
            Some("OrderRejected")
        );
        assert_eq!(
            payload.get("symbol").and_then(|v| v.as_str()),
            Some("ATAUSDT")
        );
        assert_eq!(
            payload.get("reason").and_then(|v| v.as_str()),
            Some("invalid_exit_short_skip_flags")
        );
        assert!(
            !manager.chase_states.contains_key("ATAUSDT"),
            "rejected invalid EXIT_SHORT should not initialize chase state"
        );
    }

    #[tokio::test]
    async fn paired_exit_rejects_when_one_required_leg_is_below_exchange_step() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, mut dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;
        manager
            .exchange_info
            .insert("BTCUSDT".to_string(), test_exchange_symbol("BTCUSDT"));
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: Some(TrackedLegPosition {
                    side: "LONG".to_string(),
                    entry_price: 100.0,
                    quantity: 0.001,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 100.0,
                    quantity: 0.0001,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
            },
        );

        manager
            .handle_alpha_instruction(
                crate::ipc::AlphaInstruction {
                    symbol: Some("BTCUSDT".to_string()),
                    intent: "EXIT_LONG".to_string(),
                    quantity: 0.0001,
                    urgency: 1.0,
                    max_slippage_bps: 20.0,
                    exposure_scale: 1.0,
                    intent_id: Some("paired-dust-exit".to_string()),
                    direction: Some("long".to_string()),
                    spot_quantity: Some(0.001),
                    perp_quantity: Some(0.0001),
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;

        let payload = loop {
            let message = timeout(Duration::from_secs(1), dash_rx.recv())
                .await
                .expect("non-executable paired exit should be rejected")
                .expect("rejection broadcast should be present");
            let payload: serde_json::Value =
                rmp_serde::from_slice(&message).expect("broadcast payload should be valid msgpack");
            if payload.get("event").and_then(|value| value.as_str()) == Some("OrderRejected") {
                break payload;
            }
        };

        assert_eq!(
            payload.get("reason").and_then(|value| value.as_str()),
            Some("exit_leg_not_executable")
        );
        assert!(!manager.chase_states.contains_key("BTCUSDT"));
        assert!(
            manager.tracked_positions.contains_key("BTCUSDT"),
            "rejecting a dust leg must retain tracked exposure for reconciliation"
        );
        assert!(manager.internal_orders.is_empty());
    }

    #[tokio::test]
    async fn unsupported_alpha_intents_fail_closed_before_subscription_or_chase() {
        for invalid_intent in ["", "ENTER_LON", "enter_long", " EXIT_LONG"] {
            let (_event_tx, event_rx) = mpsc::channel(4);
            let (engine_tx, _engine_rx) = mpsc::channel(8);
            let (subscription_tx, mut subscription_rx) = mpsc::channel(4);
            let (dash_tx, mut dash_rx) = broadcast::channel::<Vec<u8>>(8);

            let mut manager = OrderManager::new(
                event_rx,
                engine_tx,
                subscription_tx,
                String::new(),
                String::new(),
                dash_tx,
                "paper".to_string(),
            );
            manager.state = SystemState::Trading;
            // If an invalid intent fell through to the old default branch, these
            // books would let it initialize and place an inverse maker chase.
            manager.spot_top_cache.insert(
                "BTCUSDT".to_string(),
                TopOfBook {
                    bid_price: 100.0,
                    ask_price: 100.01,
                    bid_qty: 1.0,
                    ask_qty: 1.0,
                },
            );
            manager.perp_top_cache.insert(
                "BTCUSDT".to_string(),
                TopOfBook {
                    bid_price: 100.02,
                    ask_price: 100.03,
                    bid_qty: 1.0,
                    ask_qty: 1.0,
                },
            );

            manager
                .handle_alpha_instruction(crate::ipc::AlphaInstruction {
                    symbol: Some("BTCUSDT".to_string()),
                    intent: invalid_intent.to_string(),
                    quantity: 1.0,
                    urgency: 1.0,
                    max_slippage_bps: 20.0,
                    exposure_scale: 1.0,
                    heartbeat_id: None,
                    intent_id: Some("invalid-intent-1".to_string()),
                    direction: None,
                    skip_spot_leg: false,
                    skip_perp_leg: false,
                    spot_entry_price: None,
                    perp_entry_price: None,
                    spot_mark_price: None,
                    perp_mark_price: None,
                    spot_quantity: None,
                    perp_quantity: None,
                    ..crate::ipc::AlphaInstruction::default()
                })
                .await;

            let reject_msg = timeout(Duration::from_secs(1), dash_rx.recv())
                .await
                .expect("invalid intent should be rejected")
                .expect("broadcast payload should be present");
            let payload: serde_json::Value = rmp_serde::from_slice(&reject_msg)
                .expect("broadcast payload should be valid msgpack");

            assert_eq!(
                payload.get("event").and_then(|v| v.as_str()),
                Some("OrderRejected")
            );
            assert_eq!(
                payload.get("reason").and_then(|v| v.as_str()),
                Some("unsupported_intent")
            );
            assert_eq!(
                payload.get("intent").and_then(|v| v.as_str()),
                Some(invalid_intent)
            );
            assert!(
                manager.chase_states.is_empty(),
                "invalid intent {invalid_intent:?} must not initialize a chase"
            );
            assert!(
                matches!(
                    subscription_rx.try_recv(),
                    Err(mpsc::error::TryRecvError::Empty)
                ),
                "invalid intent {invalid_intent:?} must not request a market-data subscription"
            );
        }
    }

    #[tokio::test]
    async fn paper_legging_timeout_uses_current_book_and_only_residual_quantity() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, mut engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );

        manager.chase_states.insert(
            "BTCUSDT".to_string(),
            ChaseState {
                symbol: "BTCUSDT".to_string(),
                quantity: 1.0,
                spot_quantity: 1.0,
                perp_quantity: 1.0,
                spot_client_order_id: "spot-cid".to_string(),
                futures_client_order_id: "fut-cid".to_string(),
                skip_spot_leg: false,
                skip_perp_leg: false,
                spot_side: TradeSide::Buy,
                futures_side: TradeSide::Sell,
                is_exit: false,
                max_slippage_bps: 200.0,
                phase: ChasePhase::LegFilledWaiting(Leg::Spot),
                start_time: Instant::now(),
                expected_spot_price: 100.0,
                expected_fut_price: 104.0,
                spot_fill_price: Some(100.0),
                futures_fill_price: Some(104.0),
                spot_cumulative_filled: 1.0,
                futures_cumulative_filled: 0.4,
                spot_terminal: true,
                futures_terminal: false,
            },
        );
        manager.internal_orders.insert(
            "fut-cid".to_string(),
            InternalOrder {
                client_order_id: "fut-cid".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "NEW".to_string(),
                limit_price: Some(104.0),
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 103.0,
                ask_price: 103.5,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );

        manager.handle_legging_timeout("spot-cid".to_string()).await;

        let chase = manager
            .chase_states
            .get("BTCUSDT")
            .expect("legging defense chase state should remain active until taker fill arrives");
        assert_eq!(chase.phase, ChasePhase::LeggingDefenseTakerPlaced);
        assert_ne!(chase.futures_client_order_id, "fut-cid");
        assert!(
            manager
                .internal_orders
                .contains_key(&chase.futures_client_order_id),
            "replacement taker order should be tracked under the new client id"
        );

        let taker_fill = timeout(Duration::from_millis(400), engine_rx.recv())
            .await
            .expect("paper taker fill should arrive")
            .expect("paper taker fill should be present");

        match taker_fill {
            EngineEvent::Ws(WsEvent::OrderUpdate {
                symbol,
                filled_qty,
                avg_fill_price,
                maker,
                execution_type,
                ..
            }) => {
                assert_eq!(symbol, "BTCUSDT");
                assert!((filled_qty - 0.6).abs() < 1e-12);
                assert_eq!(avg_fill_price, Some(103.0));
                assert_eq!(maker, Some(false));
                assert_eq!(execution_type.as_deref(), Some("PAPER_TAKER_FILL"));
            }
            other => panic!(
                "unexpected taker engine event: {:?}",
                other_type_name(&other)
            ),
        }

        let canceled = manager
            .internal_orders
            .get("fut-cid")
            .expect("unfilled maker leg should still be tracked");
        assert_eq!(canceled.status, "CANCELED");
    }

    #[tokio::test]
    async fn legging_timeout_blocks_market_fallback_when_slippage_exceeds_cap() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, mut engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );

        manager.chase_states.insert(
            "BTCUSDT".to_string(),
            ChaseState {
                symbol: "BTCUSDT".to_string(),
                quantity: 1.0,
                spot_quantity: 1.0,
                perp_quantity: 1.0,
                spot_client_order_id: "spot-cid".to_string(),
                futures_client_order_id: "fut-cid".to_string(),
                skip_spot_leg: false,
                skip_perp_leg: false,
                spot_side: TradeSide::Buy,
                futures_side: TradeSide::Sell,
                is_exit: false,
                max_slippage_bps: 20.0,
                phase: ChasePhase::LegFilledWaiting(Leg::Spot),
                start_time: Instant::now(),
                expected_spot_price: 100.0,
                expected_fut_price: 104.0,
                spot_fill_price: Some(100.0),
                futures_fill_price: None,
                spot_cumulative_filled: 1.0,
                futures_cumulative_filled: 0.0,
                spot_terminal: true,
                futures_terminal: false,
            },
        );
        manager.internal_orders.insert(
            "fut-cid".to_string(),
            InternalOrder {
                client_order_id: "fut-cid".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "NEW".to_string(),
                limit_price: Some(104.0),
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 100.0,
                ask_price: 100.5,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );

        manager.handle_legging_timeout("spot-cid".to_string()).await;

        let chase = manager
            .chase_states
            .get("BTCUSDT")
            .expect("chase should remain active with passive hedge resting");
        assert_eq!(chase.phase, ChasePhase::LegFilledWaiting(Leg::Spot));
        assert_eq!(chase.futures_client_order_id, "fut-cid");
        assert_eq!(
            manager
                .internal_orders
                .get("fut-cid")
                .expect("resting futures order")
                .status,
            "NEW"
        );
        assert!(
            timeout(Duration::from_millis(150), engine_rx.recv())
                .await
                .is_err(),
            "slippage-blocked fallback must not synthesize a market fill"
        );
    }

    #[tokio::test]
    async fn legging_timeout_unconfirmed_cancel_preserves_exposure_for_reconciliation() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, mut dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "live".to_string(),
        );
        manager.binance_rest.fut_base_url = "http://127.0.0.1:1".to_string();
        manager.binance_rest.spot_base_url = "http://127.0.0.1:1".to_string();

        manager.chase_states.insert(
            "BTCUSDT".to_string(),
            ChaseState {
                symbol: "BTCUSDT".to_string(),
                quantity: 1.0,
                spot_quantity: 1.0,
                perp_quantity: 1.0,
                spot_client_order_id: "spot-cid".to_string(),
                futures_client_order_id: "fut-cid".to_string(),
                skip_spot_leg: false,
                skip_perp_leg: false,
                spot_side: TradeSide::Buy,
                futures_side: TradeSide::Sell,
                is_exit: false,
                max_slippage_bps: 20.0,
                phase: ChasePhase::LegFilledWaiting(Leg::Spot),
                start_time: Instant::now(),
                expected_spot_price: 100.0,
                expected_fut_price: 104.0,
                spot_fill_price: Some(100.0),
                futures_fill_price: None,
                spot_cumulative_filled: 1.0,
                futures_cumulative_filled: 0.0,
                spot_terminal: true,
                futures_terminal: false,
            },
        );
        manager.internal_orders.insert(
            "fut-cid".to_string(),
            InternalOrder {
                client_order_id: "fut-cid".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "NEW".to_string(),
                limit_price: Some(104.0),
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 103.95,
                ask_price: 104.05,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );

        manager.handle_legging_timeout("spot-cid".to_string()).await;

        let reject_msg = timeout(Duration::from_secs(1), dash_rx.recv())
            .await
            .expect("reconciliation requirement should be broadcast")
            .expect("broadcast payload should be present");
        let payload: serde_json::Value =
            rmp_serde::from_slice(&reject_msg).expect("broadcast payload should be valid msgpack");

        assert_eq!(
            payload.get("event").and_then(|v| v.as_str()),
            Some("OrderUpdate")
        );
        assert_eq!(
            payload.get("symbol").and_then(|v| v.as_str()),
            Some("BTCUSDT")
        );
        assert_eq!(
            payload.get("status").and_then(|v| v.as_str()),
            Some("RECONCILIATION_REQUIRED")
        );
        assert_eq!(
            payload.get("execution_type").and_then(|v| v.as_str()),
            Some("HEDGE_CANCEL_UNCONFIRMED")
        );
        assert_eq!(manager.state, SystemState::Reconciling);
        assert_eq!(
            manager
                .chase_states
                .get("BTCUSDT")
                .expect("ambiguous exposure must remain represented")
                .phase,
            ChasePhase::ReconciliationRequired
        );
        let canceled = manager
            .internal_orders
            .get("fut-cid")
            .expect("unfilled maker leg should still be tracked");
        assert_eq!(canceled.status, "NEW");
    }

    fn other_type_name(event: &EngineEvent) -> &'static str {
        match event {
            EngineEvent::Ws(_) => "ws",
            EngineEvent::Alpha(_) => "alpha",
            EngineEvent::LeggingTimeout(_) => "legging_timeout",
            EngineEvent::StrategyTick => "strategy_tick",
            EngineEvent::PositionAuditTick => "position_audit_tick",
            EngineEvent::ExchangeInfoRefreshResult(_) => "exchange_info_refresh_result",
        }
    }

    #[tokio::test]
    async fn toxic_unrelated_symbol_does_not_block_placement() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;
        // Mark an unrelated symbol toxic — must NOT block a different
        // symbol's dual-maker placement.
        manager
            .toxic_symbols
            .insert("AGLDUSDT".to_string(), Instant::now());
        manager.is_toxic = true;

        manager.chase_states.insert(
            "BTCUSDT".to_string(),
            ChaseState {
                symbol: "BTCUSDT".to_string(),
                quantity: 1.0,
                spot_quantity: 1.0,
                perp_quantity: 1.0,
                spot_client_order_id: "spot-cid".to_string(),
                futures_client_order_id: "fut-cid".to_string(),
                skip_spot_leg: false,
                skip_perp_leg: false,
                spot_side: TradeSide::Buy,
                futures_side: TradeSide::Sell,
                is_exit: false,
                max_slippage_bps: 20.0,
                phase: ChasePhase::Idle,
                start_time: Instant::now(),
                expected_spot_price: 0.0,
                expected_fut_price: 0.0,
                spot_fill_price: None,
                futures_fill_price: None,
                spot_cumulative_filled: 0.0,
                futures_cumulative_filled: 0.0,
                spot_terminal: false,
                futures_terminal: false,
            },
        );
        manager.spot_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 100.00,
                ask_price: 100.01,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 100.02,
                ask_price: 100.03,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );

        // Tight spread here so BTCUSDT itself is NOT flagged toxic.
        manager
            .handle_ws_event(WsEvent::BookTicker {
                symbol: "BTCUSDT".to_string(),
                bid_price: 100.02,
                ask_price: 100.03,
            })
            .await;

        let phase = manager
            .chase_states
            .get("BTCUSDT")
            .map(|c| c.phase)
            .expect("chase state should still exist");
        assert_eq!(
            phase,
            ChasePhase::DualMakerPlaced,
            "dual-maker placement must proceed for a non-toxic symbol even while an unrelated symbol is toxic"
        );
    }

    #[tokio::test]
    async fn single_leg_market_unwind_ignores_toxicity() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;
        // Mark BOTH the symbol itself and unrelated symbols toxic —
        // a single-leg market unwind must still place.
        manager
            .toxic_symbols
            .insert("DYMUSDT".to_string(), Instant::now());
        manager
            .toxic_symbols
            .insert("AGLDUSDT".to_string(), Instant::now());
        manager.is_toxic = true;
        manager.tracked_positions.insert(
            "DYMUSDT".to_string(),
            TrackedPosition {
                symbol: "DYMUSDT".to_string(),
                spot: None,
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 1.0,
                    quantity: 100.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 1.0,
                }),
            },
        );
        manager.chase_states.insert(
            "DYMUSDT".to_string(),
            ChaseState {
                symbol: "DYMUSDT".to_string(),
                quantity: 100.0,
                spot_quantity: 0.0,
                perp_quantity: 100.0,
                spot_client_order_id: String::new(),
                futures_client_order_id: "fut-cid".to_string(),
                skip_spot_leg: true,
                skip_perp_leg: false,
                spot_side: TradeSide::Sell,
                futures_side: TradeSide::Buy,
                is_exit: true,
                max_slippage_bps: 20.0,
                phase: ChasePhase::Idle,
                start_time: Instant::now(),
                expected_spot_price: 0.0,
                expected_fut_price: 0.0,
                spot_fill_price: None,
                futures_fill_price: None,
                spot_cumulative_filled: 0.0,
                futures_cumulative_filled: 0.0,
                spot_terminal: false,
                futures_terminal: false,
            },
        );
        manager.perp_top_cache.insert(
            "DYMUSDT".to_string(),
            TopOfBook {
                bid_price: 1.0,
                ask_price: 1.01,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );

        manager.try_place_dual_maker("DYMUSDT".to_string()).await;

        let phase = manager
            .chase_states
            .get("DYMUSDT")
            .map(|c| c.phase)
            .unwrap_or(ChasePhase::Idle);
        assert_ne!(
            phase,
            ChasePhase::Idle,
            "single-leg market unwind must advance past Idle even when symbol itself is toxic"
        );
    }

    #[tokio::test]
    async fn chase_init_kicks_try_place_immediately() {
        // Regression for §4.3: after handle_alpha_instruction registers
        // a new chase, try_place_dual_maker must be invoked immediately
        // from the cached top-of-book — without requiring a subsequent
        // WS tick.
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;
        manager.tracked_positions.insert(
            "DYMUSDT".to_string(),
            TrackedPosition {
                symbol: "DYMUSDT".to_string(),
                spot: None,
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 1.0,
                    quantity: 100.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 1.0,
                }),
            },
        );
        manager.perp_top_cache.insert(
            "DYMUSDT".to_string(),
            TopOfBook {
                bid_price: 1.0,
                ask_price: 1.01,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );

        manager
            .handle_alpha_instruction(
                crate::ipc::AlphaInstruction {
                    symbol: Some("DYMUSDT".to_string()),
                    intent: "EXIT_LONG".to_string(),
                    quantity: 100.0,
                    urgency: 1.0,
                    max_slippage_bps: 20.0,
                    exposure_scale: 1.0,
                    heartbeat_id: None,
                    intent_id: Some("intent-dym-1".to_string()),
                    direction: Some("long".to_string()),
                    skip_spot_leg: true,
                    skip_perp_leg: false,
                    spot_entry_price: None,
                    perp_entry_price: None,
                    spot_mark_price: None,
                    perp_mark_price: None,
                    spot_quantity: None,
                    perp_quantity: None,
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;

        // A fresh chase must have advanced past Idle — i.e. the
        // immediate kick in handle_alpha_instruction ran the single-leg
        // market unwind without waiting for any WS tick. In paper mode
        // the market fill is synthesised immediately and the chase is
        // removed on completion.
        let phase = manager.chase_states.get("DYMUSDT").map(|c| c.phase);
        assert!(
            phase != Some(ChasePhase::Idle),
            "chase should not remain Idle after handle_alpha_instruction: phase={:?}",
            phase
        );
    }

    #[tokio::test]
    async fn identical_intent_replay_does_not_create_second_chase() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(16);
        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;
        let instruction = crate::ipc::AlphaInstruction {
            symbol: Some("REPLAYUSDT".to_string()),
            intent: "ENTER_LONG".to_string(),
            quantity: 10.0,
            urgency: 0.5,
            max_slippage_bps: 5.0,
            exposure_scale: 1.0,
            intent_id: Some("replay-intent".to_string()),
            ..crate::ipc::AlphaInstruction::default()
        }
        .seal_internal();

        manager.handle_alpha_instruction(instruction.clone()).await;
        let first_ids = manager
            .chase_states
            .get("REPLAYUSDT")
            .map(|chase| {
                (
                    chase.spot_client_order_id.clone(),
                    chase.futures_client_order_id.clone(),
                )
            })
            .expect("first command should initialize exactly one chase");
        manager.handle_alpha_instruction(instruction).await;

        assert_eq!(manager.chase_states.len(), 1);
        let replay_ids = manager
            .chase_states
            .get("REPLAYUSDT")
            .map(|chase| {
                (
                    chase.spot_client_order_id.clone(),
                    chase.futures_client_order_id.clone(),
                )
            })
            .unwrap();
        assert_eq!(replay_ids, first_ids);
    }

    #[test]
    fn private_stream_readiness_requires_both_backfilled_markets_and_revokes_on_gap() {
        let mut manager = paper_test_manager();
        manager.trading_mode = "testnet".to_string();
        manager.state = SystemState::Disconnected;

        assert!(!manager.private_stream_quorum_ready());
        assert!(!manager.record_private_stream_status(MarketType::Spot, "READY"));
        assert_eq!(manager.state, SystemState::Disconnected);
        assert!(manager.record_private_stream_status(MarketType::Perp, "READY"));
        assert!(manager.private_stream_quorum_ready());

        manager.state = SystemState::Trading;
        assert!(!manager.record_private_stream_status(MarketType::Spot, "GAP_DETECTED"));
        assert_eq!(manager.state, SystemState::Disconnected);
        assert!(!manager.private_stream_quorum_ready());
        assert!(
            manager
                .private_stream_ready_markets
                .contains(&MarketType::Perp)
        );
        assert!(
            !manager
                .private_stream_ready_markets
                .contains(&MarketType::Spot)
        );
    }

    #[tokio::test]
    async fn telemetry_overflow_revokes_quorum_and_execution_readiness() {
        let mut manager = paper_test_manager();
        manager.trading_mode = "testnet".to_string();
        manager.state = SystemState::Trading;
        manager
            .private_stream_ready_markets
            .insert(MarketType::Spot);
        manager
            .private_stream_ready_markets
            .insert(MarketType::Perp);

        manager
            .handle_ws_event(WsEvent::TelemetryGap {
                skipped_messages: 23,
                reason: "broadcast_receiver_overflow".to_string(),
                event_time_ms: 123_456,
            })
            .await;

        assert_eq!(manager.state, SystemState::Reconciling);
        assert!(manager.private_stream_ready_markets.is_empty());
        assert!(!manager.private_stream_quorum_ready());
    }

    #[tokio::test]
    async fn heartbeat_replays_private_stream_and_execution_readiness_snapshots() {
        let mut manager = paper_test_manager();
        manager.trading_mode = "testnet".to_string();
        manager.state = SystemState::Trading;
        let mut dashboard = manager.dash_tx.subscribe();

        for market in [MarketType::Spot, MarketType::Perp] {
            manager.private_stream_ready_markets.insert(market);
            manager.private_stream_status_snapshots.insert(
                market,
                PrivateStreamStatusSnapshot {
                    market,
                    status: "READY".to_string(),
                    start_time_ms: Some(100),
                    end_time_ms: Some(200),
                    orders_replayed: 2,
                    trades_replayed: 3,
                    error: None,
                },
            );
        }

        manager
            .handle_alpha_instruction(crate::ipc::AlphaInstruction {
                intent: "HEARTBEAT".to_string(),
                heartbeat_id: Some("heartbeat-replay".to_string()),
                ..crate::ipc::AlphaInstruction::default()
            })
            .await;

        let mut events = Vec::new();
        for _ in 0..4 {
            let payload = timeout(Duration::from_secs(1), dashboard.recv())
                .await
                .expect("heartbeat snapshot event timed out")
                .expect("dashboard channel closed");
            events.push(
                rmp_serde::from_slice::<serde_json::Value>(&payload)
                    .expect("heartbeat snapshot should be valid msgpack"),
            );
        }

        assert_eq!(events[0]["event"], "PrivateStreamStatus");
        assert_eq!(events[0]["market"], "spot");
        assert_eq!(events[1]["event"], "PrivateStreamStatus");
        assert_eq!(events[1]["market"], "perp");
        assert_eq!(events[2]["event"], "ExecutionReadiness");
        assert_eq!(events[2]["status"], "READY");
        assert_eq!(events[3]["event"], "HeartbeatAck");
        assert_eq!(events[3]["heartbeat_id"], "heartbeat-replay");
    }
}
