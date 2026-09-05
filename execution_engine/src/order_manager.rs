use rand::Rng;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap, HashSet, VecDeque};
use std::fs::OpenOptions;
use std::future::Future;
use std::io::Write;
use std::path::PathBuf;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc::{Receiver, Sender};
use tokio::sync::{broadcast, oneshot};
use tokio::time::sleep;
use tracing::{debug, error, info, warn};

use crate::binance_rest::{BinanceRest, LegVenue, ReconciledSubmission, RestWorkClass, TradeSide};
use crate::collateral_engine::UnifiedPortfolioMarginCalculator;
use crate::exact_decimal::ExactDecimal;
#[cfg(not(test))]
use crate::ipc::default_rust_runtime_path;
use crate::ipc::{
    CONFIG_SYNC_INTENT, ConfigAck, ConfigConsensus, IntentJournal, IntentReceipt, ReceiptDecision,
    StorageControlUpdate,
};
use crate::storage::StorageControlRecord;

const MIN_ENTRY_RATE_LIMIT_WEIGHT: u64 = 4;
const MAX_EXECUTION_MARKET_EVENT_AGE_MS: i64 = 3_000;
const MAX_EXECUTION_MARKET_FUTURE_SKEW_MS: i64 = 1_000;
const ACCOUNT_TRUTH_MAX_AGE_MS: i64 = 180_000;
const PYTHON_BRAIN_STALE_AFTER: Duration = Duration::from_secs(12 * 60);

#[derive(Debug, PartialEq, Eq, Clone)]
pub enum SystemState {
    Disconnected,
    Reconciling,
    Trading,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum ContinuousRiskState {
    #[default]
    Normal,
    EntryFrozen,
    CancelingEntries,
    Reconciling,
    Derisking,
    ManualReview,
}

impl ContinuousRiskState {
    fn as_str(self) -> &'static str {
        match self {
            Self::Normal => "NORMAL",
            Self::EntryFrozen => "ENTRY_FROZEN",
            Self::CancelingEntries => "CANCELING_ENTRIES",
            Self::Reconciling => "RECONCILING",
            Self::Derisking => "DERISKING",
            Self::ManualReview => "MANUAL_REVIEW",
        }
    }
}

/// Signed Standard Spot account evidence. Standard Spot has no borrow
/// lifecycle; short-spot strategies require a separately authorized account
/// topology instead of silently reclassifying this one.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
struct StandardSpotAccountTruth {
    wallet_balance: HashMap<String, String>,
    available_balance: HashMap<String, String>,
    open_orders: usize,
    borrow_state: String,
    observed_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
struct UsdmPositionRiskTruth {
    position_amount: String,
    leverage: u32,
    liquidation_price: String,
}

/// Signed USD-M account evidence. These values remain separate from Spot
/// inventory so a delta-neutral pair cannot net away maintenance margin or
/// liquidation risk.
#[derive(Debug, Clone, PartialEq, serde::Serialize, serde::Deserialize)]
struct UsdmAccountRiskTruth {
    wallet_balance: String,
    available_balance: String,
    positions: HashMap<String, UsdmPositionRiskTruth>,
    maintenance_margin: String,
    margin_ratio: f64,
    liquidation_price: HashMap<String, String>,
    position_mode: String,
    open_orders: usize,
    observed_at_ms: i64,
}

/// Durable emergency exits deliberately use a state machine independent of
/// the normal maker chase. A restart resumes the first non-terminal state; a
/// failure may only advance to MANUAL_REVIEW and never erase the record.
#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
enum EmergencyExitState {
    Detected,
    CancelingCurrentOrders,
    SignedReadback,
    InventoryClassified,
    ReduceOnlyDerisking,
    VerifyingFlat,
    Flat,
    ManualReview,
}

impl EmergencyExitState {
    fn as_str(self) -> &'static str {
        match self {
            Self::Detected => "DETECTED",
            Self::CancelingCurrentOrders => "CANCELING_CURRENT_ORDERS",
            Self::SignedReadback => "SIGNED_READBACK",
            Self::InventoryClassified => "INVENTORY_CLASSIFIED",
            Self::ReduceOnlyDerisking => "REDUCE_ONLY_DERISKING",
            Self::VerifyingFlat => "VERIFYING_FLAT",
            Self::Flat => "FLAT",
            Self::ManualReview => "MANUAL_REVIEW",
        }
    }

    fn is_terminal(self) -> bool {
        matches!(self, Self::Flat | Self::ManualReview)
    }

    fn allows(self, next: Self) -> bool {
        next == Self::ManualReview
            || matches!(
                (self, next),
                (Self::Detected, Self::CancelingCurrentOrders)
                    | (Self::CancelingCurrentOrders, Self::SignedReadback)
                    | (Self::SignedReadback, Self::InventoryClassified)
                    | (Self::InventoryClassified, Self::ReduceOnlyDerisking)
                    | (Self::ReduceOnlyDerisking, Self::VerifyingFlat)
                    | (Self::VerifyingFlat, Self::Flat)
                    | (Self::VerifyingFlat, Self::SignedReadback)
            )
    }
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct EmergencyExitTransition {
    state: EmergencyExitState,
    sequence: u64,
    persisted_at_ms: i64,
    reason: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct EmergencyRepairGeneration {
    leg: Leg,
    generation: u16,
    client_order_id: String,
    requested_quantity_decimal: String,
    cumulative_filled_decimal: String,
    final_status: String,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct EmergencyExitRecord {
    schema_version: u16,
    symbol: String,
    intent_id: String,
    #[serde(default)]
    lineage: OrderLineage,
    #[serde(default)]
    original_exit_spot_client_order_ids: Vec<String>,
    #[serde(default)]
    original_exit_futures_client_order_ids: Vec<String>,
    direction: String,
    state: EmergencyExitState,
    transition_sequence: u64,
    updated_at_ms: i64,
    requested_quantity_decimal: String,
    actual_spot_inventory_decimal: String,
    actual_futures_inventory_decimal: String,
    exit_spot_quantity_decimal: String,
    exit_futures_quantity_decimal: String,
    signed_spot_total_decimal: String,
    signed_spot_available_decimal: String,
    signed_futures_position_decimal: String,
    initial_signed_spot_total_decimal: String,
    initial_signed_futures_position_decimal: String,
    initial_inventory_captured: bool,
    classified_spot_exit_quantity_decimal: String,
    classified_futures_exit_quantity_decimal: String,
    cumulative_spot_emergency_filled_decimal: String,
    cumulative_futures_emergency_filled_decimal: String,
    verified_spot_inventory_decimal: String,
    verified_futures_inventory_decimal: String,
    spot_reference_price_decimal: String,
    futures_reference_price_decimal: String,
    spot_repair_client_order_id: String,
    futures_repair_client_order_id: String,
    spot_generation: u16,
    futures_generation: u16,
    spot_generations: Vec<EmergencyRepairGeneration>,
    futures_generations: Vec<EmergencyRepairGeneration>,
    cancel_attempts: u16,
    readback_attempts: u16,
    submit_attempts: u16,
    verify_attempts: u16,
    derisk_attempts: u16,
    spot_submission_confirmed: bool,
    futures_submission_confirmed: bool,
    max_retries: u16,
    readback_budget: u16,
    max_slippage_bps_decimal: String,
    last_error: String,
    /// Present only when the Rust continuous-risk actor, rather than an Alpha
    /// command, created the emergency exit. The durable risk sequence makes
    /// re-evaluation and restart replay idempotent for the whole risk episode.
    #[serde(default)]
    autonomous_risk_sequence: Option<u64>,
    #[serde(default)]
    trigger_reason: String,
    transitions: Vec<EmergencyExitTransition>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum AlphaIntent {
    Heartbeat,
    SubscribeMarketData,
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
            "SUBSCRIBE_MARKET_DATA" => Some(Self::SubscribeMarketData),
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

#[allow(clippy::large_enum_variant)]
#[derive(Debug, Clone, serde::Serialize)]
#[serde(tag = "event")]
pub enum WsEvent {
    Connected {
        symbol: String,
        stream_type: WsStreamType,
        connection_id: Option<String>,
        connection_role: Option<String>,
    },
    Disconnected {
        symbol: String,
        stream_type: WsStreamType,
        connection_id: Option<String>,
        connection_role: Option<String>,
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
    ExchangeQuota {
        status: String,
        reason: String,
        spot_limit_weight: u64,
        spot_used_weight: u64,
        spot_remaining_weight: u64,
        spot_observed_at_ms: i64,
        futures_limit_weight: u64,
        futures_used_weight: u64,
        futures_remaining_weight: u64,
        futures_observed_at_ms: i64,
        combined_remaining_weight: u64,
        spot_order_limit_10s: u64,
        spot_order_used_10s: u64,
        spot_order_remaining_10s: u64,
        spot_order_limit_1m: u64,
        spot_order_used_1m: u64,
        spot_order_remaining_1m: u64,
        futures_order_limit_10s: u64,
        futures_order_used_10s: u64,
        futures_order_remaining_10s: u64,
        futures_order_limit_1m: u64,
        futures_order_used_1m: u64,
        futures_order_remaining_1m: u64,
        max_utilization_bps: u64,
        nonessential_allowed: bool,
        entry_allowed: bool,
        critical_allowed: bool,
        reserved_request_weight: u64,
        reserved_order_count: u64,
        ambiguous_until_ms: i64,
        last_failure_class: Option<String>,
        blocked_until_ms: i64,
        event_time_ms: i64,
    },
    TelemetryGap {
        skipped_messages: u64,
        reason: String,
        event_time_ms: i64,
    },
    /// Internal relay handoff, emitted only after the terminal payload is fsynced.
    TerminalPublicationPersisted { publication_id: String },
    BookTicker {
        symbol: String,
        bid_price: f64,
        ask_price: f64,
        connection_id: String,
        exchange_event_time_ms: Option<i64>,
        receive_time_ms: i64,
        process_time_ms: i64,
        persist_time_ms: Option<i64>,
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
        sequence_contiguous: bool,
        connection_id: String,
        exchange_event_time_ms: Option<i64>,
        receive_time_ms: i64,
        process_time_ms: i64,
        persist_time_ms: Option<i64>,
    },
    /// Emitted on every markPriceUpdate from Binance perp streams (~1s cadence).
    /// `next_funding_rate` is the predicted rate for the upcoming settlement —
    /// more actionable than lastFundingRate for entry/exit decisions.
    MarkPrice {
        symbol: String,
        mark_price: f64,
        next_funding_rate: f64,
        next_funding_time_ms: i64,
        connection_id: String,
        exchange_event_time_ms: Option<i64>,
        receive_time_ms: i64,
        process_time_ms: i64,
        persist_time_ms: Option<i64>,
    },
    VolumeBar {
        symbol: String,
        minute_start_ms: i64,
        notional_usd: f64,
        connection_id: String,
        exchange_event_time_ms: Option<i64>,
        receive_time_ms: i64,
        process_time_ms: i64,
        persist_time_ms: Option<i64>,
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
        connection_id: Option<String>,
        exchange_event_time_ms: Option<i64>,
        receive_time_ms: Option<i64>,
        process_time_ms: Option<i64>,
        persist_time_ms: Option<i64>,
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
        available_balances: HashMap<String, f64>,
        positions: HashMap<String, f64>,
        source: String,
    },
    PositionDivergence {
        symbol: String,
        divergence_type: String,
        local_qty: f64,
        exchange_qty: f64,
    },
}

#[cfg_attr(not(unix), allow(dead_code))]
pub enum EngineEvent {
    Ws(Box<WsEvent>),
    Alpha(Box<crate::ipc::AlphaInstruction>),
    LeggingTimeout(String),
    CycleDeadline {
        cycle_client_order_id: String,
        deadline_at_ms: i64,
    },
    StrategyTick,
    PositionAuditTick,
    ExchangeInfoRefreshResult(
        Result<HashMap<String, crate::binance_rest::ExchangeSymbolInfo>, String>,
    ),
    RecoveryBarrier {
        request_id: String,
        reply: oneshot::Sender<Result<OrderRecoverySnapshot, String>>,
        release: oneshot::Receiver<RecoveryBarrierRelease>,
        resumed: oneshot::Sender<Result<(), String>>,
    },
    RecoveryBarrierFailed {
        request_id: String,
        reason: String,
    },
}

#[cfg_attr(not(unix), allow(dead_code))]
#[derive(Debug, Clone)]
pub(crate) struct OrderRecoverySnapshot {
    pub barrier_request_id: String,
    pub execution_state_path: PathBuf,
    pub intent_journal_path: PathBuf,
    pub terminal_sequence_watermark: u64,
    pub intent_producer_high_watermarks: BTreeMap<String, u64>,
}

#[cfg_attr(not(unix), allow(dead_code))]
#[derive(Debug)]
pub(crate) enum RecoveryBarrierRelease {
    Published { generation_id: String },
    Failed { reason: String },
}

#[derive(Debug, Clone)]
struct ContinuousRiskCheckpoint {
    state: ContinuousRiskState,
    reason: String,
}

struct CommissionObservation<'a> {
    symbol: &'a str,
    client_order_id: &'a str,
    market: MarketType,
    amount: f64,
    asset: &'a str,
    order_id: Option<i64>,
    trade_id: Option<i64>,
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

#[derive(Debug, Clone, PartialEq)]
struct SpotAccountBalances {
    total: HashMap<String, f64>,
    available: HashMap<String, f64>,
}

#[derive(Debug, Clone)]
struct ExactSpotAccountBalances {
    total: HashMap<String, ExactDecimal>,
    available: HashMap<String, ExactDecimal>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct InternalOrder {
    pub client_order_id: String,
    pub symbol: String,
    pub status: String,
    pub limit_price: Option<f64>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct TrackedLegPosition {
    pub side: String,
    pub entry_price: f64,
    pub quantity: f64,
    pub unrealized_pnl: f64,
    pub last_mark_price: f64,
}

#[allow(dead_code)]
#[derive(Debug, Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct TrackedPosition {
    pub symbol: String,
    pub spot: Option<TrackedLegPosition>,
    pub perp: Option<TrackedLegPosition>,
}

#[derive(Debug, Clone, Copy)]
pub struct TopOfBook {
    pub bid_price: f64,
    pub ask_price: f64,
    #[allow(dead_code)]
    pub bid_qty: f64,
    #[allow(dead_code)]
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
const COMPILED_MAX_CONCURRENT_SYMBOLS: usize = 4;
const EXECUTION_STATE_ENTRY_BUDGET_BYTES: u64 = 30_000_000;
const EXECUTION_STATE_TRANSITION_RESERVE_BYTES: u64 = 64 * 1024;
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum StorageControlApplyOutcome {
    Applied,
    VolatileLatched,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ExchangeOrderStatus {
    New,
    PartiallyFilled,
    Filled,
    Canceled,
    PendingCancel,
    Rejected,
    Expired,
    ExpiredInMatch,
}

impl ExchangeOrderStatus {
    fn parse(value: &str) -> Option<Self> {
        match value.trim().to_ascii_uppercase().as_str() {
            "NEW" => Some(Self::New),
            "PARTIALLY_FILLED" => Some(Self::PartiallyFilled),
            "FILLED" => Some(Self::Filled),
            "CANCELED" | "CANCELLED" => Some(Self::Canceled),
            "PENDING_CANCEL" => Some(Self::PendingCancel),
            "REJECTED" => Some(Self::Rejected),
            "EXPIRED" => Some(Self::Expired),
            "EXPIRED_IN_MATCH" => Some(Self::ExpiredInMatch),
            _ => None,
        }
    }

    fn is_filled(self) -> bool {
        self == Self::Filled
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::New => "NEW",
            Self::PartiallyFilled => "PARTIALLY_FILLED",
            Self::Filled => "FILLED",
            Self::Canceled => "CANCELED",
            Self::PendingCancel => "PENDING_CANCEL",
            Self::Rejected => "REJECTED",
            Self::Expired => "EXPIRED",
            Self::ExpiredInMatch => "EXPIRED_IN_MATCH",
        }
    }

    fn is_partial(self) -> bool {
        self == Self::PartiallyFilled
    }

    fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Filled | Self::Canceled | Self::Rejected | Self::Expired | Self::ExpiredInMatch
        )
    }

    fn is_terminal_without_full_fill(self) -> bool {
        self.is_terminal() && !self.is_filled()
    }
}

fn is_terminal_internal_status(value: &str) -> bool {
    ExchangeOrderStatus::parse(value).is_some_and(ExchangeOrderStatus::is_terminal)
        || value.eq_ignore_ascii_case("NOT_SUBMITTED")
}

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
    #[cfg(test)]
    brain_ping_age_override: Option<Duration>,
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
    /// Authoritative total spot inventory (`free + locked`) used for account
    /// reconciliation. Keep this separate from spendable collateral because
    /// resting orders move funds from `free` to `locked`.
    pub spot_balances: HashMap<String, f64>,
    /// Latest authoritative free spot balances. Pending entry chases reserve
    /// their quote collateral against this map before either leg is submitted.
    pub spot_available_balances: HashMap<String, f64>,
    standard_spot_account_truth: Option<StandardSpotAccountTruth>,
    usdm_account_truth: Option<UsdmAccountRiskTruth>,
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
    market_stream_ready_roles: HashSet<String>,
    public_stream_recovery_symbols: HashSet<String>,
    private_stream_status_snapshots: HashMap<MarketType, PrivateStreamStatusSnapshot>,
    execution_state_journal_path: PathBuf,
    execution_state_journal_error: Option<String>,
    terminal_tombstones: HashMap<String, TerminalLifecycleTombstone>,
    terminal_publications: HashMap<String, serde_json::Value>,
    terminal_sequence_watermark: u64,
    symbol_persistence_latches: HashMap<String, SymbolPersistenceLatch>,
    #[cfg(test)]
    execution_state_persist_failure: Option<String>,
    storage_control_path: PathBuf,
    storage_control_generation: u64,
    storage_emergency_latched: bool,
    storage_control_volatile_latched: bool,
    storage_control_error: Option<String>,
    #[cfg(test)]
    storage_control_persist_failure: Option<String>,
    chase_unhedged_budgets: HashMap<String, f64>,
    chase_unhedged_started_at_ms: HashMap<String, i64>,
    applied_commission_keys: HashMap<String, String>,
    commission_cycles: HashMap<String, String>,
    unvalued_commission_assets: HashSet<String>,
    cycle_deadlines: HashMap<String, i64>,
    cycle_deadline_records: HashMap<String, CycleDeadlineRecord>,
    pub continuous_risk_state: ContinuousRiskState,
    continuous_risk_reason: String,
    continuous_risk_sequence: u64,
    continuous_risk_updated_at_ms: i64,
    emergency_exits: HashMap<String, EmergencyExitRecord>,
    risk_evaluation_active: bool,
    clock_warning_latched: bool,
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
    requested_quantity_decimal: Option<String>,
    risk_adjusted_requested_quantity_decimal: Option<String>,
    normalized_common_entry_quantity_decimal: Option<String>,
    actual_spot_inventory_decimal: Option<String>,
    actual_futures_inventory_decimal: Option<String>,
    exit_spot_quantity_decimal: Option<String>,
    exit_futures_quantity_decimal: Option<String>,
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
    DeadlineFreezing,
    ReconciliationRequired,
    Completed,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
enum CycleDeadlineClassification {
    Flat,
    EqualPartial,
    Divergent,
    Unknown,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct CycleDeadlineRecord {
    symbol: String,
    cycle_client_order_id: String,
    classification: CycleDeadlineClassification,
    spot_cumulative_filled: f64,
    futures_cumulative_filled: f64,
    is_exit: bool,
    deadline_at_ms: i64,
    classified_at_ms: i64,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct ChaseState {
    symbol: String,
    quantity: f64,
    spot_quantity: f64,
    perp_quantity: f64,
    spot_client_order_id: String,
    futures_client_order_id: String,
    /// Every concrete exchange order generation for the logical spot leg.
    /// The active client id above may change during residual repair, while
    /// aliases remain durable so delayed private-stream fills are still
    /// attributed to the original cycle.
    #[serde(default)]
    spot_order_aliases: Vec<String>,
    /// Every concrete exchange order generation for the logical futures leg.
    #[serde(default)]
    futures_order_aliases: Vec<String>,
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
    #[serde(default)]
    requested_quantity_decimal: String,
    #[serde(default)]
    normalized_common_entry_quantity_decimal: Option<String>,
    #[serde(default)]
    actual_spot_inventory_decimal: String,
    #[serde(default)]
    actual_futures_inventory_decimal: String,
    #[serde(default)]
    exit_spot_quantity_decimal: String,
    #[serde(default)]
    exit_futures_quantity_decimal: String,
    #[serde(default = "default_legacy_route_policy")]
    route_policy: String,
    #[serde(default)]
    last_exchange_event_time_ms: Option<i64>,
    #[serde(default)]
    last_receive_time_ms: Option<i64>,
    #[serde(default)]
    last_persist_time_ms: Option<i64>,
}

fn default_legacy_route_policy() -> String {
    "legacy_dual_maker".to_string()
}

fn instant_now() -> Instant {
    Instant::now()
}

impl Default for ChaseState {
    fn default() -> Self {
        Self {
            symbol: String::new(),
            quantity: 0.0,
            spot_quantity: 0.0,
            perp_quantity: 0.0,
            spot_client_order_id: String::new(),
            futures_client_order_id: String::new(),
            spot_order_aliases: Vec::new(),
            futures_order_aliases: Vec::new(),
            skip_spot_leg: false,
            skip_perp_leg: false,
            spot_side: TradeSide::Buy,
            futures_side: TradeSide::Sell,
            is_exit: false,
            max_slippage_bps: 0.0,
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
            requested_quantity_decimal: "0".to_string(),
            normalized_common_entry_quantity_decimal: None,
            actual_spot_inventory_decimal: "0".to_string(),
            actual_futures_inventory_decimal: "0".to_string(),
            exit_spot_quantity_decimal: "0".to_string(),
            exit_futures_quantity_decimal: "0".to_string(),
            route_policy: default_legacy_route_policy(),
            last_exchange_event_time_ms: None,
            last_receive_time_ms: None,
            last_persist_time_ms: None,
        }
    }
}

const EXECUTION_STATE_SCHEMA_VERSION: u32 = 8;
const ENTRY_MAKER_TTL_MS: i64 = 15_000;
const TERMINAL_TOMBSTONE_SCHEMA_VERSION: u16 = 1;
const SYMBOL_PERSISTENCE_LATCH_SCHEMA_VERSION: u16 = 1;
const TERMINAL_TOMBSTONE_RETENTION_MS: i64 = 30 * 24 * 60 * 60 * 1_000;

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct TerminalLifecycleTombstone {
    schema_version: u16,
    symbol: String,
    cycle_client_order_id: String,
    intent_id: Option<String>,
    lifecycle_state: String,
    terminal_sequence_watermark: u64,
    reconciliation_status: String,
    tombstoned_at_ms: i64,
    retention_deadline_ms: i64,
    reason: String,
    client_order_ids: Vec<String>,
    chase_state: ChaseState,
    internal_orders: HashMap<String, InternalOrder>,
    order_cumulative_fills: HashMap<String, f64>,
    order_lineage: HashMap<String, OrderLineage>,
}

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct SymbolPersistenceLatch {
    schema_version: u16,
    symbol: String,
    reason: String,
    first_failed_at_ms: i64,
    last_failed_at_ms: i64,
    failure_count: u64,
    last_error: String,
}

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
    #[serde(default)]
    tracked_positions: HashMap<String, TrackedPosition>,
    #[serde(default)]
    applied_commission_keys: HashMap<String, String>,
    #[serde(default)]
    commission_cycles: HashMap<String, String>,
    #[serde(default)]
    unvalued_commission_assets: HashSet<String>,
    #[serde(default)]
    cycle_deadlines: HashMap<String, i64>,
    #[serde(default)]
    cycle_deadline_records: HashMap<String, CycleDeadlineRecord>,
    #[serde(default)]
    continuous_risk_state: ContinuousRiskState,
    #[serde(default)]
    continuous_risk_reason: String,
    #[serde(default)]
    continuous_risk_sequence: u64,
    #[serde(default)]
    continuous_risk_updated_at_ms: i64,
    #[serde(default)]
    public_stream_recovery_symbols: HashSet<String>,
    #[serde(default)]
    emergency_exits: HashMap<String, EmergencyExitRecord>,
    #[serde(default)]
    terminal_tombstones: HashMap<String, TerminalLifecycleTombstone>,
    #[serde(default)]
    terminal_publications: HashMap<String, serde_json::Value>,
    #[serde(default)]
    terminal_sequence_watermark: u64,
    #[serde(default)]
    symbol_persistence_latches: HashMap<String, SymbolPersistenceLatch>,
    #[serde(default)]
    standard_spot_account_truth: Option<StandardSpotAccountTruth>,
    #[serde(default)]
    usdm_account_truth: Option<UsdmAccountRiskTruth>,
}

#[derive(Debug, Clone, Copy)]
struct TerminalOrderSnapshot {
    status: ExchangeOrderStatus,
    cumulative_filled_qty: f64,
    average_fill_price: Option<f64>,
}

#[derive(Debug, Clone, Copy)]
struct ExactOrderSnapshot {
    status: ExchangeOrderStatus,
    cumulative_filled_qty: ExactDecimal,
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

    fn aliases_for(&self, leg: Leg) -> &[String] {
        match leg {
            Leg::Spot => &self.spot_order_aliases,
            Leg::Futures => &self.futures_order_aliases,
        }
    }

    fn aliases_for_mut(&mut self, leg: Leg) -> &mut Vec<String> {
        match leg {
            Leg::Spot => &mut self.spot_order_aliases,
            Leg::Futures => &mut self.futures_order_aliases,
        }
    }

    fn active_client_order_id(&self, leg: Leg) -> &str {
        match leg {
            Leg::Spot => &self.spot_client_order_id,
            Leg::Futures => &self.futures_client_order_id,
        }
    }

    fn set_active_client_order_id(&mut self, leg: Leg, client_order_id: String) {
        let aliases = self.aliases_for_mut(leg);
        if !aliases.iter().any(|alias| alias == &client_order_id) {
            aliases.push(client_order_id.clone());
        }
        match leg {
            Leg::Spot => self.spot_client_order_id = client_order_id,
            Leg::Futures => self.futures_client_order_id = client_order_id,
        }
    }

    fn leg_for_client_order_id(&self, client_order_id: &str) -> Option<Leg> {
        if self.spot_client_order_id == client_order_id
            || self
                .spot_order_aliases
                .iter()
                .any(|alias| alias == client_order_id)
        {
            Some(Leg::Spot)
        } else if self.futures_client_order_id == client_order_id
            || self
                .futures_order_aliases
                .iter()
                .any(|alias| alias == client_order_id)
        {
            Some(Leg::Futures)
        } else {
            None
        }
    }

    fn ensure_active_aliases(&mut self) {
        for (leg, active) in [
            (Leg::Spot, self.spot_client_order_id.clone()),
            (Leg::Futures, self.futures_client_order_id.clone()),
        ] {
            if !active.is_empty() && !self.aliases_for(leg).iter().any(|alias| alias == &active) {
                self.aliases_for_mut(leg).push(active);
            }
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
            .unwrap_or_else(|_| default_rust_runtime_path("execution_state.jsonl"));
        #[cfg(test)]
        let execution_state_journal_path = std::env::temp_dir().join(format!(
            "bongus-execution-state-test-{}-{}.jsonl",
            std::process::id(),
            rand::random::<u64>()
        ));

        #[cfg(not(test))]
        let storage_control_path = std::env::var("EXECUTION_STORAGE_CONTROL_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| default_rust_runtime_path("storage_control.json"));
        #[cfg(test)]
        let storage_control_path = std::env::temp_dir().join(format!(
            "bongus-storage-control-test-{}-{}.json",
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
            #[cfg(test)]
            brain_ping_age_override: None,
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
            spot_available_balances: HashMap::new(),
            standard_spot_account_truth: None,
            usdm_account_truth: None,
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
            market_stream_ready_roles: HashSet::new(),
            public_stream_recovery_symbols: HashSet::new(),
            private_stream_status_snapshots: HashMap::new(),
            execution_state_journal_path,
            execution_state_journal_error: None,
            terminal_tombstones: HashMap::new(),
            terminal_publications: HashMap::new(),
            terminal_sequence_watermark: 0,
            symbol_persistence_latches: HashMap::new(),
            #[cfg(test)]
            execution_state_persist_failure: None,
            storage_control_path,
            storage_control_generation: 0,
            storage_emergency_latched: false,
            storage_control_volatile_latched: false,
            storage_control_error: None,
            #[cfg(test)]
            storage_control_persist_failure: None,
            chase_unhedged_budgets: HashMap::new(),
            chase_unhedged_started_at_ms: HashMap::new(),
            applied_commission_keys: HashMap::new(),
            commission_cycles: HashMap::new(),
            unvalued_commission_assets: HashSet::new(),
            cycle_deadlines: HashMap::new(),
            cycle_deadline_records: HashMap::new(),
            continuous_risk_state: ContinuousRiskState::Normal,
            continuous_risk_reason: "startup".to_string(),
            continuous_risk_sequence: 0,
            continuous_risk_updated_at_ms: 0,
            emergency_exits: HashMap::new(),
            risk_evaluation_active: false,
            clock_warning_latched: false,
        };
        if let Err(err) = manager.load_execution_state() {
            error!("Execution state journal unavailable; startup will fail closed: {err}");
            manager.execution_state_journal_error = Some(err);
        } else if manager.recover_interrupted_recovery_barrier() {
        } else if manager.has_unresolved_execution_effects() {
            warn!(
                "Recovered unresolved execution state for {} symbol(s); startup will remain Reconciling until explicitly repaired",
                manager.chase_states.len()
            );
            manager.state = SystemState::Reconciling;
        } else if manager
            .emergency_exits
            .values()
            .any(|record| record.state != EmergencyExitState::Flat)
        {
            warn!("Recovered an unfinished emergency exit; restart recovery is required");
            manager.state = SystemState::Reconciling;
        } else if manager.continuous_risk_state != ContinuousRiskState::Normal {
            warn!(
                "Recovered continuous risk state {}; signed reconciliation is required before entries can resume",
                manager.continuous_risk_state.as_str()
            );
            manager.state = SystemState::Reconciling;
        }
        if let Err(err) = manager.load_storage_control() {
            error!("Storage-control checkpoint is unavailable; entries will fail closed: {err}");
            manager.storage_control_error = Some(err);
            manager.storage_emergency_latched = true;
        }
        manager
    }

    fn validate_execution_snapshot(snapshot: &ExecutionStateSnapshot) -> Result<(), String> {
        for (publication_id, event) in &snapshot.terminal_publications {
            if publication_id.is_empty()
                || publication_id.len() > 512
                || event.get("publication_id").and_then(Value::as_str)
                    != Some(publication_id.as_str())
                || !matches!(
                    event.get("event").and_then(Value::as_str),
                    Some("OrderUpdate" | "EmergencyExitState")
                )
                || event
                    .get("symbol")
                    .and_then(Value::as_str)
                    .is_none_or(|symbol| symbol.is_empty())
            {
                return Err("invalid durable terminal publication identity".to_string());
            }
        }
        if snapshot.schema_version == 0 || snapshot.schema_version > EXECUTION_STATE_SCHEMA_VERSION
        {
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
        for (intent_id, record) in &snapshot.emergency_exits {
            if intent_id.trim().is_empty()
                || record.intent_id != *intent_id
                || record.schema_version != 1
                || record.symbol.trim().is_empty()
                || !matches!(record.direction.as_str(), "EXIT_LONG" | "EXIT_SHORT")
                || record.transition_sequence == 0
                || record.updated_at_ms <= 0
                || record.max_retries == 0
                || record.readback_budget == 0
                || record.spot_repair_client_order_id.is_empty()
                || record.spot_repair_client_order_id.len() > 36
                || record.futures_repair_client_order_id.is_empty()
                || record.futures_repair_client_order_id.len() > 36
                || record.autonomous_risk_sequence.is_some_and(|sequence| {
                    sequence == 0 || record.trigger_reason.trim().is_empty()
                })
            {
                return Err(format!("invalid recovered emergency exit {intent_id}"));
            }
            for raw in [
                record.requested_quantity_decimal.as_str(),
                record.actual_spot_inventory_decimal.as_str(),
                record.actual_futures_inventory_decimal.as_str(),
                record.exit_spot_quantity_decimal.as_str(),
                record.exit_futures_quantity_decimal.as_str(),
                record.signed_spot_total_decimal.as_str(),
                record.signed_spot_available_decimal.as_str(),
                record.initial_signed_spot_total_decimal.as_str(),
                record.classified_spot_exit_quantity_decimal.as_str(),
                record.classified_futures_exit_quantity_decimal.as_str(),
                record.cumulative_spot_emergency_filled_decimal.as_str(),
                record.cumulative_futures_emergency_filled_decimal.as_str(),
                record.verified_spot_inventory_decimal.as_str(),
                record.verified_futures_inventory_decimal.as_str(),
                record.spot_reference_price_decimal.as_str(),
                record.futures_reference_price_decimal.as_str(),
                record.max_slippage_bps_decimal.as_str(),
            ] {
                if Self::canonical_exact(Some(raw)).is_none() {
                    return Err(format!(
                        "noncanonical exact quantity in recovered emergency exit {intent_id}"
                    ));
                }
            }
            if Self::canonical_signed_exact(Some(&record.signed_futures_position_decimal)).is_none()
                || Self::canonical_signed_exact(Some(
                    &record.initial_signed_futures_position_decimal,
                ))
                .is_none()
            {
                return Err(format!(
                    "noncanonical signed futures inventory in emergency exit {intent_id}"
                ));
            }
            for (expected_leg, generations) in [
                (Leg::Spot, record.spot_generations.as_slice()),
                (Leg::Futures, record.futures_generations.as_slice()),
            ] {
                for (index, generation) in generations.iter().enumerate() {
                    if generation.leg != expected_leg
                        || generation.generation != index as u16
                        || generation.client_order_id.is_empty()
                        || generation.client_order_id.len() > 36
                        || generation.final_status.trim().is_empty()
                        || Self::canonical_exact(Some(&generation.requested_quantity_decimal))
                            .is_none()
                        || Self::canonical_exact(Some(&generation.cumulative_filled_decimal))
                            .is_none()
                    {
                        return Err(format!(
                            "invalid repair generation in emergency exit {intent_id}"
                        ));
                    }
                }
            }
            if record.transitions.len() != record.transition_sequence as usize
                || record
                    .transitions
                    .last()
                    .is_none_or(|transition| transition.state != record.state)
            {
                return Err(format!(
                    "invalid transition watermark in recovered emergency exit {intent_id}"
                ));
            }
            let mut previous: Option<EmergencyExitState> = None;
            for (index, transition) in record.transitions.iter().enumerate() {
                if transition.sequence != (index as u64).saturating_add(1)
                    || transition.persisted_at_ms <= 0
                    || transition.reason.trim().is_empty()
                    || previous.is_some_and(|state| !state.allows(transition.state))
                {
                    return Err(format!(
                        "invalid transition history in recovered emergency exit {intent_id}"
                    ));
                }
                previous = Some(transition.state);
            }
            if record
                .transitions
                .first()
                .is_none_or(|transition| transition.state != EmergencyExitState::Detected)
            {
                return Err(format!(
                    "emergency exit {intent_id} did not begin at DETECTED"
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
        for (symbol, position) in &snapshot.tracked_positions {
            if symbol.trim().is_empty()
                || position.symbol.to_uppercase() != symbol.to_uppercase()
                || [position.spot.as_ref(), position.perp.as_ref()]
                    .into_iter()
                    .flatten()
                    .any(|leg| {
                        Self::side_is_long(&leg.side).is_none()
                            || !leg.entry_price.is_finite()
                            || leg.entry_price <= 0.0
                            || !leg.quantity.is_finite()
                            || leg.quantity <= 0.0
                            || !leg.last_mark_price.is_finite()
                            || leg.last_mark_price <= 0.0
                            || !leg.unrealized_pnl.is_finite()
                    })
            {
                return Err(format!("invalid recovered tracked position for {symbol}"));
            }
        }
        if snapshot
            .applied_commission_keys
            .iter()
            .any(|(key, fingerprint)| key.trim().is_empty() || fingerprint.trim().is_empty())
            || snapshot
                .unvalued_commission_assets
                .iter()
                .any(|asset| asset.trim().is_empty())
        {
            return Err("invalid recovered commission state".to_string());
        }
        if snapshot
            .commission_cycles
            .iter()
            .any(|(identity, cycle_id)| {
                identity.trim().is_empty()
                    || cycle_id.trim().is_empty()
                    || !snapshot.applied_commission_keys.contains_key(identity)
            })
        {
            return Err("invalid recovered commission-cycle lineage".to_string());
        }
        if snapshot
            .cycle_deadlines
            .iter()
            .any(|(cycle_id, deadline)| cycle_id.trim().is_empty() || *deadline <= 0)
        {
            return Err("invalid recovered cycle deadline".to_string());
        }
        for (cycle_id, record) in &snapshot.cycle_deadline_records {
            if cycle_id.trim().is_empty()
                || record.symbol.trim().is_empty()
                || record.cycle_client_order_id != *cycle_id
                || !record.spot_cumulative_filled.is_finite()
                || record.spot_cumulative_filled < 0.0
                || !record.futures_cumulative_filled.is_finite()
                || record.futures_cumulative_filled < 0.0
                || record.deadline_at_ms <= 0
                || record.classified_at_ms <= 0
            {
                return Err("invalid recovered cycle deadline classification".to_string());
            }
        }
        if snapshot.continuous_risk_state != ContinuousRiskState::Normal
            && (snapshot.continuous_risk_reason.trim().is_empty()
                || snapshot.continuous_risk_sequence == 0
                || snapshot.continuous_risk_updated_at_ms <= 0)
        {
            return Err("invalid recovered continuous risk state".to_string());
        }
        if snapshot
            .public_stream_recovery_symbols
            .iter()
            .any(|symbol| symbol.trim().is_empty())
        {
            return Err("invalid recovered public-stream recovery symbol".to_string());
        }
        for (cycle_id, tombstone) in &snapshot.terminal_tombstones {
            let identity_valid = !cycle_id.trim().is_empty()
                && tombstone.schema_version == TERMINAL_TOMBSTONE_SCHEMA_VERSION
                && tombstone.cycle_client_order_id == *cycle_id
                && !tombstone.symbol.trim().is_empty()
                && tombstone
                    .chase_state
                    .symbol
                    .eq_ignore_ascii_case(&tombstone.symbol)
                && tombstone.terminal_sequence_watermark > 0
                && tombstone.terminal_sequence_watermark <= snapshot.terminal_sequence_watermark
                && tombstone.tombstoned_at_ms > 0
                && tombstone.retention_deadline_ms > tombstone.tombstoned_at_ms
                && !tombstone.reason.trim().is_empty()
                && !tombstone.reconciliation_status.trim().is_empty()
                && matches!(
                    tombstone.lifecycle_state.as_str(),
                    "TERMINAL_RECONCILED" | "EXCHANGE_FLAT_AWAITING_TERMINAL" | "RETAINED_PRUNED"
                )
                && !tombstone.client_order_ids.is_empty()
                && tombstone
                    .client_order_ids
                    .iter()
                    .all(|client_id| !client_id.trim().is_empty());
            let evidence_valid =
                tombstone
                    .order_cumulative_fills
                    .iter()
                    .all(|(client_id, fill)| {
                        tombstone.client_order_ids.contains(client_id)
                            && fill.is_finite()
                            && *fill >= 0.0
                    })
                    && tombstone
                        .internal_orders
                        .keys()
                        .all(|client_id| tombstone.client_order_ids.contains(client_id))
                    && tombstone
                        .order_lineage
                        .keys()
                        .all(|client_id| tombstone.client_order_ids.contains(client_id));
            if !identity_valid || !evidence_valid {
                return Err(format!("invalid terminal lifecycle tombstone {cycle_id}"));
            }
        }
        for (symbol, latch) in &snapshot.symbol_persistence_latches {
            if symbol.trim().is_empty()
                || latch.schema_version != SYMBOL_PERSISTENCE_LATCH_SCHEMA_VERSION
                || !latch.symbol.eq_ignore_ascii_case(symbol)
                || latch.reason.trim().is_empty()
                || latch.last_error.trim().is_empty()
                || latch.failure_count == 0
                || latch.first_failed_at_ms <= 0
                || latch.last_failed_at_ms < latch.first_failed_at_ms
            {
                return Err(format!("invalid symbol persistence latch {symbol}"));
            }
        }
        if let Some(spot) = snapshot.standard_spot_account_truth.as_ref()
            && (spot.observed_at_ms <= 0
                || spot.borrow_state != "NOT_APPLICABLE_STANDARD_SPOT"
                || spot
                    .wallet_balance
                    .keys()
                    .any(|asset| asset.trim().is_empty())
                || spot
                    .available_balance
                    .keys()
                    .any(|asset| asset.trim().is_empty())
                || spot
                    .wallet_balance
                    .values()
                    .chain(spot.available_balance.values())
                    .any(|value| {
                        value
                            .parse::<ExactDecimal>()
                            .map_or(true, |decimal| decimal < ExactDecimal::ZERO)
                    }))
        {
            return Err("invalid recovered Standard Spot account truth".to_string());
        }
        if let Some(usdm) = snapshot.usdm_account_truth.as_ref() {
            let top_level_decimals_valid = [
                &usdm.wallet_balance,
                &usdm.available_balance,
                &usdm.maintenance_margin,
            ]
            .into_iter()
            .all(|value| value.parse::<ExactDecimal>().is_ok());
            let positions_valid = usdm.positions.iter().all(|(key, position)| {
                !key.trim().is_empty()
                    && position.leverage > 0
                    && position
                        .position_amount
                        .parse::<ExactDecimal>()
                        .is_ok_and(|quantity| quantity != ExactDecimal::ZERO)
                    && position
                        .liquidation_price
                        .parse::<ExactDecimal>()
                        .is_ok_and(ExactDecimal::is_positive)
                    && usdm.liquidation_price.get(key) == Some(&position.liquidation_price)
            });
            if usdm.observed_at_ms <= 0
                || !top_level_decimals_valid
                || !usdm.margin_ratio.is_finite()
                || usdm.margin_ratio < 0.0
                || !matches!(usdm.position_mode.as_str(), "ONE_WAY" | "HEDGE")
                || !positions_valid
                || usdm.liquidation_price.len() != usdm.positions.len()
            {
                return Err("invalid recovered USD-M account truth".to_string());
            }
        }
        Ok(())
    }

    fn apply_execution_snapshot(&mut self, mut snapshot: ExecutionStateSnapshot) {
        // Schema-v1 snapshots written before order-generation aliases existed
        // deserialize with empty vectors. Promote their active IDs into the
        // alias sets so restart recovery cannot lose a late fill.
        for chase in snapshot.chase_states.values_mut() {
            chase.ensure_active_aliases();
        }
        self.chase_states = snapshot.chase_states;
        self.internal_orders = snapshot.internal_orders;
        self.chase_intent_ids = snapshot.chase_intent_ids;
        self.order_cumulative_fills = snapshot.order_cumulative_fills;
        self.order_lineage = snapshot.order_lineage;
        self.chase_unhedged_budgets = snapshot.chase_unhedged_budgets;
        self.chase_unhedged_started_at_ms = snapshot.chase_unhedged_started_at_ms;
        self.tracked_positions = snapshot.tracked_positions;
        self.applied_commission_keys = snapshot.applied_commission_keys;
        self.commission_cycles = snapshot.commission_cycles;
        self.unvalued_commission_assets = snapshot.unvalued_commission_assets;
        self.cycle_deadlines = snapshot.cycle_deadlines;
        self.cycle_deadline_records = snapshot.cycle_deadline_records;
        self.continuous_risk_state = snapshot.continuous_risk_state;
        self.continuous_risk_reason = snapshot.continuous_risk_reason;
        self.continuous_risk_sequence = snapshot.continuous_risk_sequence;
        self.continuous_risk_updated_at_ms = snapshot.continuous_risk_updated_at_ms;
        self.public_stream_recovery_symbols = snapshot.public_stream_recovery_symbols;
        self.emergency_exits = snapshot.emergency_exits;
        self.terminal_tombstones = snapshot.terminal_tombstones;
        self.terminal_publications = snapshot.terminal_publications;
        self.terminal_sequence_watermark = snapshot.terminal_sequence_watermark;
        self.symbol_persistence_latches = snapshot.symbol_persistence_latches;
        self.standard_spot_account_truth = snapshot.standard_spot_account_truth;
        self.usdm_account_truth = snapshot.usdm_account_truth;
        self.recompute_gross_exposure();
    }

    fn load_execution_state(&mut self) -> Result<(), String> {
        Self::recover_rotated_file(&self.execution_state_journal_path, "execution state")?;
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

    fn path_with_suffix(path: &std::path::Path, suffix: &str) -> PathBuf {
        let mut value = path.as_os_str().to_os_string();
        value.push(suffix);
        PathBuf::from(value)
    }

    fn recover_rotated_file(path: &std::path::Path, label: &str) -> Result<(), String> {
        if path.exists() {
            return Ok(());
        }
        let previous = Self::path_with_suffix(path, ".previous");
        if previous.exists() {
            std::fs::rename(&previous, path)
                .map_err(|err| format!("recover prior {label} file: {err}"))?;
        }
        Ok(())
    }

    fn load_storage_control(&mut self) -> Result<(), String> {
        let Some(record) = StorageControlRecord::load(&self.storage_control_path)? else {
            return Ok(());
        };
        self.storage_control_generation = record.generation;
        self.storage_emergency_latched = record.emergency_latched;
        self.storage_control_volatile_latched = false;
        self.storage_control_error = None;
        Ok(())
    }

    fn persist_storage_control(&self, record: &StorageControlRecord) -> Result<(), String> {
        #[cfg(test)]
        if let Some(reason) = self.storage_control_persist_failure.as_deref() {
            return Err(format!(
                "injected storage-control persistence failure: {reason}"
            ));
        }
        record.persist(&self.storage_control_path)
    }

    fn apply_storage_control(
        &mut self,
        update: Option<&StorageControlUpdate>,
    ) -> Result<StorageControlApplyOutcome, &'static str> {
        let Some(update) = update else {
            // An ordinary configuration update is deliberately incapable of
            // clearing the independent storage latch.
            return Ok(StorageControlApplyOutcome::Applied);
        };
        if update.generation < self.storage_control_generation {
            if update.emergency_latched == self.storage_emergency_latched {
                // Canonical application config carries the generation-zero
                // neutral tuple. After an explicit newer recovery, accept it
                // only as a no-op so ordinary config consensus can resume;
                // never roll back the durable generation.
                return Ok(if self.storage_control_volatile_latched {
                    StorageControlApplyOutcome::VolatileLatched
                } else {
                    StorageControlApplyOutcome::Applied
                });
            }
            return Err("stale_storage_control_generation");
        }
        if update.generation == self.storage_control_generation {
            if update.emergency_latched == self.storage_emergency_latched {
                if !self.storage_control_volatile_latched {
                    return Ok(StorageControlApplyOutcome::Applied);
                }
                // A replay of the same emergency generation is also a retry
                // of its failed checkpoint. Do not strand the process in a
                // permanently volatile state after disk space is restored.
                if !update.emergency_latched {
                    return Err("volatile_storage_recovery_requires_new_generation");
                }
            } else {
                return Err("conflicting_storage_control_generation");
            }
        }
        if !update.emergency_latched && !update.recovery_acknowledged {
            return Err("storage_recovery_acknowledgement_required");
        }
        let record = StorageControlRecord::new(update.generation, update.emergency_latched);
        if let Err(err) = self.persist_storage_control(&record) {
            error!(
                "Could not durably apply storage-control generation {}: {err}",
                update.generation
            );
            self.storage_control_error = Some(err);
            self.storage_emergency_latched = true;
            self.state = SystemState::Reconciling;
            if update.emergency_latched {
                self.storage_control_generation = update.generation;
                self.storage_control_volatile_latched = true;
                warn!(
                    "VOLATILE storage emergency latch active at generation {}; cancellation and reconciliation are required, recovery remains blocked",
                    update.generation
                );
                return Ok(StorageControlApplyOutcome::VolatileLatched);
            }
            return Err("storage_control_persistence_failed");
        }
        self.storage_control_generation = record.generation;
        self.storage_emergency_latched = record.emergency_latched;
        self.storage_control_volatile_latched = false;
        self.storage_control_error = None;
        if record.emergency_latched {
            warn!(
                "Durably latched Rust storage emergency at generation {}; new risk is disabled",
                record.generation
            );
        } else {
            info!(
                "Cleared Rust storage emergency at operator-acknowledged generation {}",
                record.generation
            );
        }
        Ok(StorageControlApplyOutcome::Applied)
    }

    fn halt_entry_chases_for_storage_latch(&mut self) {
        let entry_symbols: Vec<String> = self
            .chase_states
            .iter()
            .filter(|(_, chase)| !chase.is_exit && chase.phase != ChasePhase::Completed)
            .map(|(symbol, _)| symbol.clone())
            .collect();
        for symbol in entry_symbols {
            let Some(mut chase) = self.chase_states.get(&symbol).cloned() else {
                continue;
            };
            if chase.phase == ChasePhase::Idle {
                let _ = self.emit_cycle_order_update(
                    &chase,
                    "REJECTED",
                    chase.cycle_client_order_id(),
                    0.0,
                    false,
                    "STORAGE_CONTROL_LATCHED_BEFORE_SUBMISSION",
                );
                self.remove_chase_state(&symbol, "storage latch removed unsubmitted entry chase");
                continue;
            }

            // A resting or ambiguous entry may already exist at the exchange.
            // Freeze Rust-side progression immediately and retain every alias
            // for authoritative Python cancellation/reconciliation.
            chase.phase = ChasePhase::ReconciliationRequired;
            self.state = SystemState::Reconciling;
            let _ = self.store_chase_state(
                symbol,
                chase,
                "storage latch froze active entry chase for cancellation",
            );
        }
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
            tracked_positions: self.tracked_positions.clone(),
            applied_commission_keys: self.applied_commission_keys.clone(),
            commission_cycles: self.commission_cycles.clone(),
            unvalued_commission_assets: self.unvalued_commission_assets.clone(),
            cycle_deadlines: self.cycle_deadlines.clone(),
            cycle_deadline_records: self.cycle_deadline_records.clone(),
            continuous_risk_state: self.continuous_risk_state,
            continuous_risk_reason: self.continuous_risk_reason.clone(),
            continuous_risk_sequence: self.continuous_risk_sequence,
            continuous_risk_updated_at_ms: self.continuous_risk_updated_at_ms,
            public_stream_recovery_symbols: self.public_stream_recovery_symbols.clone(),
            emergency_exits: self.emergency_exits.clone(),
            terminal_tombstones: self.terminal_tombstones.clone(),
            terminal_publications: self.terminal_publications.clone(),
            terminal_sequence_watermark: self.terminal_sequence_watermark,
            symbol_persistence_latches: self.symbol_persistence_latches.clone(),
            standard_spot_account_truth: self.standard_spot_account_truth.clone(),
            usdm_account_truth: self.usdm_account_truth.clone(),
        }
    }

    fn execution_state_byte_limit() -> u64 {
        std::env::var("EXECUTION_STATE_ENTRY_MAX_BYTES")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .filter(|value| *value > 2 * EXECUTION_STATE_TRANSITION_RESERVE_BYTES)
            .unwrap_or(EXECUTION_STATE_ENTRY_BUDGET_BYTES)
            .min(EXECUTION_STATE_ENTRY_BUDGET_BYTES)
    }

    fn chase_client_order_ids(chase: &ChaseState) -> Vec<String> {
        let mut client_order_ids = Vec::new();
        for client_order_id in chase
            .spot_order_aliases
            .iter()
            .chain(chase.futures_order_aliases.iter())
            .chain([&chase.spot_client_order_id, &chase.futures_client_order_id])
        {
            if !client_order_id.trim().is_empty() && !client_order_ids.contains(client_order_id) {
                client_order_ids.push(client_order_id.clone());
            }
        }
        client_order_ids.sort();
        client_order_ids
    }

    fn ensure_terminal_tombstone(
        &mut self,
        chase: &ChaseState,
        lifecycle_state: &str,
        reconciliation_status: &str,
        reason: &str,
    ) {
        let cycle_client_order_id = chase.cycle_client_order_id().to_string();
        if cycle_client_order_id.trim().is_empty() {
            error!(
                "Cannot retain terminal lifecycle evidence for {} without a cycle client id",
                chase.symbol
            );
            return;
        }
        let client_order_ids = Self::chase_client_order_ids(chase);
        let internal_orders = client_order_ids
            .iter()
            .filter_map(|client_id| {
                self.internal_orders
                    .get(client_id)
                    .cloned()
                    .map(|order| (client_id.clone(), order))
            })
            .collect();
        let order_cumulative_fills = client_order_ids
            .iter()
            .filter_map(|client_id| {
                self.order_cumulative_fills
                    .get(client_id)
                    .copied()
                    .map(|fill| (client_id.clone(), fill))
            })
            .collect();
        let order_lineage: HashMap<String, OrderLineage> = client_order_ids
            .iter()
            .filter_map(|client_id| {
                self.order_lineage
                    .get(client_id)
                    .cloned()
                    .map(|lineage| (client_id.clone(), lineage))
            })
            .collect();
        let intent_id = self
            .chase_intent_ids
            .get(&chase.symbol.to_ascii_uppercase())
            .cloned()
            .or_else(|| {
                order_lineage
                    .values()
                    .find_map(|lineage| lineage.intent_id.clone())
            });
        let now_ms = Self::current_time_ms().max(1);
        let is_new_transition = self
            .terminal_tombstones
            .get(&cycle_client_order_id)
            .is_none_or(|existing| {
                existing.lifecycle_state != lifecycle_state
                    || existing.reconciliation_status != reconciliation_status
            });
        if is_new_transition {
            self.terminal_sequence_watermark =
                self.terminal_sequence_watermark.saturating_add(1).max(1);
        }
        let sequence = self
            .terminal_tombstones
            .get(&cycle_client_order_id)
            .filter(|_| !is_new_transition)
            .map(|existing| existing.terminal_sequence_watermark)
            .unwrap_or(self.terminal_sequence_watermark);
        let tombstoned_at_ms = self
            .terminal_tombstones
            .get(&cycle_client_order_id)
            .map(|existing| existing.tombstoned_at_ms)
            .unwrap_or(now_ms);
        self.terminal_tombstones.insert(
            cycle_client_order_id.clone(),
            TerminalLifecycleTombstone {
                schema_version: TERMINAL_TOMBSTONE_SCHEMA_VERSION,
                symbol: chase.symbol.to_ascii_uppercase(),
                cycle_client_order_id,
                intent_id,
                lifecycle_state: lifecycle_state.to_string(),
                terminal_sequence_watermark: sequence,
                reconciliation_status: reconciliation_status.to_string(),
                tombstoned_at_ms,
                retention_deadline_ms: tombstoned_at_ms
                    .saturating_add(TERMINAL_TOMBSTONE_RETENTION_MS),
                reason: reason.to_string(),
                client_order_ids,
                chase_state: chase.clone(),
                internal_orders,
                order_cumulative_fills,
                order_lineage,
            },
        );
    }

    fn prune_resolved_execution_artifacts(&mut self) {
        let now_ms = Self::current_time_ms();
        let mut active_client_ids = HashSet::new();
        for chase in self.chase_states.values() {
            active_client_ids.extend(Self::chase_client_order_ids(chase));
        }
        let mut protected_client_ids = active_client_ids.clone();
        let mut expired_client_ids = HashSet::new();
        let mut terminal_sequence_watermark = self.terminal_sequence_watermark;
        for tombstone in self.terminal_tombstones.values_mut() {
            if tombstone.retention_deadline_ms > now_ms {
                protected_client_ids.extend(tombstone.client_order_ids.iter().cloned());
            } else {
                if tombstone.lifecycle_state != "RETAINED_PRUNED"
                    || tombstone.reconciliation_status != "RETENTION_EXPIRED"
                {
                    terminal_sequence_watermark =
                        terminal_sequence_watermark.saturating_add(1).max(1);
                    tombstone.terminal_sequence_watermark = terminal_sequence_watermark;
                }
                tombstone.lifecycle_state = "RETAINED_PRUNED".to_string();
                tombstone.reconciliation_status = "RETENTION_EXPIRED".to_string();
                expired_client_ids.extend(tombstone.client_order_ids.iter().cloned());
            }
        }
        self.terminal_sequence_watermark = terminal_sequence_watermark;
        expired_client_ids.retain(|client_id| !protected_client_ids.contains(client_id));
        self.internal_orders.retain(|client_id, order| {
            !expired_client_ids.contains(client_id) || !is_terminal_internal_status(&order.status)
        });
        let unresolved_client_ids: HashSet<String> = self.internal_orders.keys().cloned().collect();
        self.order_cumulative_fills.retain(|client_id, _| {
            !expired_client_ids.contains(client_id) || unresolved_client_ids.contains(client_id)
        });
        self.order_lineage.retain(|client_id, _| {
            !expired_client_ids.contains(client_id) || unresolved_client_ids.contains(client_id)
        });
    }

    fn append_execution_snapshot(&self) -> Result<(), String> {
        self.append_execution_snapshot_at_limit(Self::execution_state_byte_limit())
    }

    fn append_execution_snapshot_at_limit(&self, max_bytes: u64) -> Result<(), String> {
        #[cfg(test)]
        if let Some(error) = self.execution_state_persist_failure.as_ref() {
            return Err(error.clone());
        }
        if max_bytes <= 2 * EXECUTION_STATE_TRANSITION_RESERVE_BYTES {
            return Err(format!(
                "execution state cap {max_bytes} does not preserve the transition reserve"
            ));
        }
        if let Some(parent) = self
            .execution_state_journal_path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            std::fs::create_dir_all(parent)
                .map_err(|err| format!("create execution state journal directory: {err}"))?;
        }
        let mut encoded = serde_json::to_vec(&self.execution_snapshot())
            .map_err(|err| format!("encode execution state snapshot: {err}"))?;
        encoded.push(b'\n');
        let snapshot_bytes = encoded.len() as u64;
        if snapshot_bytes.saturating_add(EXECUTION_STATE_TRANSITION_RESERVE_BYTES) > max_bytes {
            return Err(format!(
                "execution state snapshot exceeds bounded journal: snapshot={snapshot_bytes}, transition_reserve={EXECUTION_STATE_TRANSITION_RESERVE_BYTES}, limit={max_bytes}"
            ));
        }
        let current_bytes = match self.execution_state_journal_path.metadata() {
            Ok(metadata) => metadata.len(),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => 0,
            Err(err) => return Err(format!("inspect execution state journal: {err}")),
        };
        let append_projected = current_bytes.saturating_add(snapshot_bytes);
        let compact_before_write = current_bytes >= max_bytes / 2
            || append_projected.saturating_add(EXECUTION_STATE_TRANSITION_RESERVE_BYTES)
                > max_bytes;
        if compact_before_write {
            return self.install_compacted_execution_snapshot(&encoded, max_bytes);
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.execution_state_journal_path)
            .map_err(|err| format!("append execution state journal: {err}"))?;
        file.write_all(&encoded)
            .map_err(|err| format!("write execution state snapshot: {err}"))?;
        file.sync_data()
            .map_err(|err| format!("sync execution state snapshot: {err}"))
    }

    fn install_compacted_execution_snapshot(
        &self,
        encoded: &[u8],
        max_bytes: u64,
    ) -> Result<(), String> {
        if encoded.len() as u64 > max_bytes {
            return Err("compacted execution state exceeds configured cap".to_string());
        }
        let next = Self::path_with_suffix(&self.execution_state_journal_path, ".next");
        let previous = Self::path_with_suffix(&self.execution_state_journal_path, ".previous");
        {
            let mut file = OpenOptions::new()
                .create(true)
                .truncate(true)
                .write(true)
                .open(&next)
                .map_err(|err| format!("create compacted execution state: {err}"))?;
            file.write_all(encoded)
                .map_err(|err| format!("write compacted execution state: {err}"))?;
            file.sync_all()
                .map_err(|err| format!("sync compacted execution state: {err}"))?;
        }
        if previous.exists() {
            std::fs::remove_file(&previous)
                .map_err(|err| format!("remove stale execution state checkpoint: {err}"))?;
        }
        if self.execution_state_journal_path.exists() {
            std::fs::rename(&self.execution_state_journal_path, &previous)
                .map_err(|err| format!("rotate execution state journal: {err}"))?;
        }
        if let Err(err) = std::fs::rename(&next, &self.execution_state_journal_path) {
            if !self.execution_state_journal_path.exists() && previous.exists() {
                let _ = std::fs::rename(&previous, &self.execution_state_journal_path);
            }
            return Err(format!("install compacted execution state: {err}"));
        }
        if previous.exists() {
            let _ = std::fs::remove_file(previous);
        }
        Ok(())
    }

    fn try_persist_execution_state(&mut self) -> Result<(), String> {
        self.prune_resolved_execution_artifacts();
        self.append_execution_snapshot()
    }

    fn persist_execution_state(&mut self, context: &str) -> bool {
        match self.try_persist_execution_state() {
            Ok(()) => true,
            Err(err) => {
                error!("Execution state persistence failed during {context}: {err}");
                self.execution_state_journal_error = Some(err);
                self.state = SystemState::Reconciling;
                self.continuous_risk_state = ContinuousRiskState::ManualReview;
                self.continuous_risk_reason =
                    format!("execution_state_persistence_failed:{context}");
                self.continuous_risk_sequence = self.continuous_risk_sequence.saturating_add(1);
                self.continuous_risk_updated_at_ms = Self::current_time_ms();
                self.emit_continuous_risk_state(false);
                false
            }
        }
    }

    fn recovery_barrier_identifier_is_valid(value: &str) -> bool {
        !value.is_empty()
            && value.len() <= 128
            && value
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    }

    fn recover_interrupted_recovery_barrier(&mut self) -> bool {
        let Some(request_id) = self
            .continuous_risk_reason
            .strip_prefix("recovery_generation_barrier_active:")
            .map(str::to_string)
        else {
            return false;
        };
        warn!(
            "Recovered an interrupted recovery generation barrier {}; manual review is required",
            request_id
        );
        self.fail_recovery_barrier(
            &request_id,
            "engine restarted before the barrier received an unambiguous published release",
        );
        true
    }

    fn sync_recovery_source(path: &std::path::Path, label: &str) -> Result<(), String> {
        let metadata = std::fs::symlink_metadata(path)
            .map_err(|error| format!("inspect {label} for recovery barrier: {error}"))?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(format!("{label} is not a regular recovery source"));
        }
        let file = OpenOptions::new()
            .read(true)
            .write(true)
            .open(path)
            .map_err(|error| format!("open {label} for recovery barrier: {error}"))?;
        file.sync_all()
            .map_err(|error| format!("sync {label} for recovery barrier: {error}"))?;
        #[cfg(unix)]
        if let Some(parent) = path.parent() {
            std::fs::File::open(parent)
                .map_err(|error| format!("open {label} directory for recovery barrier: {error}"))?
                .sync_all()
                .map_err(|error| format!("sync {label} directory for recovery barrier: {error}"))?;
        }
        Ok(())
    }

    fn prepare_recovery_barrier(
        &mut self,
        request_id: &str,
    ) -> Result<OrderRecoverySnapshot, String> {
        if !Self::recovery_barrier_identifier_is_valid(request_id) {
            return Err("recovery barrier request id is invalid".to_string());
        }
        if self
            .continuous_risk_reason
            .starts_with("recovery_generation_barrier_active:")
        {
            return Err("a recovery generation barrier is already active".to_string());
        }

        if self.continuous_risk_state == ContinuousRiskState::Normal {
            self.continuous_risk_state = ContinuousRiskState::EntryFrozen;
        }
        self.continuous_risk_reason = format!("recovery_generation_barrier_active:{request_id}");
        self.continuous_risk_sequence = self.continuous_risk_sequence.saturating_add(1);
        self.continuous_risk_updated_at_ms = Self::current_time_ms().max(1);
        if !self.persist_execution_state("recovery generation barrier entered") {
            return Err("could not durably enter recovery generation barrier".to_string());
        }
        self.emit_continuous_risk_state(true);
        Self::sync_recovery_source(
            &self.execution_state_journal_path,
            "execution state journal",
        )?;

        let intent = self
            .intent_journal
            .as_ref()
            .ok_or_else(|| "intent journal is unavailable at recovery barrier".to_string())?
            .prepare_recovery_snapshot()?;
        Ok(OrderRecoverySnapshot {
            barrier_request_id: request_id.to_string(),
            execution_state_path: self.execution_state_journal_path.clone(),
            intent_journal_path: intent.path,
            terminal_sequence_watermark: self.terminal_sequence_watermark,
            intent_producer_high_watermarks: intent.producer_high_watermarks,
        })
    }

    fn restore_after_recovery_barrier(
        &mut self,
        checkpoint: ContinuousRiskCheckpoint,
        request_id: &str,
        generation_id: &str,
    ) -> Result<(), String> {
        if !Self::recovery_barrier_identifier_is_valid(generation_id) {
            self.fail_recovery_barrier(request_id, "publisher returned an invalid generation id");
            return Err("publisher returned an invalid generation id".to_string());
        }
        self.continuous_risk_state = checkpoint.state;
        self.continuous_risk_reason = checkpoint.reason;
        self.continuous_risk_sequence = self.continuous_risk_sequence.saturating_add(1);
        self.continuous_risk_updated_at_ms = Self::current_time_ms().max(1);
        if !self.persist_execution_state("recovery generation barrier released") {
            return Err(format!(
                "generation {generation_id} published but barrier release was not durable"
            ));
        }
        self.emit_continuous_risk_state(true);
        Ok(())
    }

    fn fail_recovery_barrier(&mut self, request_id: &str, reason: &str) {
        let safe_reason: String = reason
            .chars()
            .filter(|character| !character.is_control())
            .take(512)
            .collect();
        self.state = SystemState::Reconciling;
        self.continuous_risk_state = ContinuousRiskState::ManualReview;
        self.continuous_risk_reason =
            format!("recovery_generation_barrier_failed:{request_id}:{safe_reason}");
        self.continuous_risk_sequence = self.continuous_risk_sequence.saturating_add(1);
        self.continuous_risk_updated_at_ms = Self::current_time_ms().max(1);
        let durable = self.persist_execution_state("recovery generation barrier failed");
        self.emit_continuous_risk_state(durable);
        self.emit_execution_readiness("BLOCKED", "recovery_generation_barrier_failed");
    }

    async fn handle_recovery_barrier_event(
        &mut self,
        request_id: String,
        reply: oneshot::Sender<Result<OrderRecoverySnapshot, String>>,
        release: oneshot::Receiver<RecoveryBarrierRelease>,
        resumed: oneshot::Sender<Result<(), String>>,
    ) {
        let checkpoint = ContinuousRiskCheckpoint {
            state: self.continuous_risk_state,
            reason: self.continuous_risk_reason.clone(),
        };
        match self.prepare_recovery_barrier(&request_id) {
            Ok(snapshot) => {
                if reply.send(Ok(snapshot)).is_err() {
                    self.fail_recovery_barrier(
                        &request_id,
                        "coordinator disappeared before accepting the durable barrier",
                    );
                    let _ = resumed.send(Err(
                        "coordinator disappeared before accepting the barrier".to_string(),
                    ));
                    return;
                }
                let resume_result = match release.await {
                    Ok(RecoveryBarrierRelease::Published { generation_id }) => {
                        self.restore_after_recovery_barrier(checkpoint, &request_id, &generation_id)
                    }
                    Ok(RecoveryBarrierRelease::Failed { reason }) => {
                        self.fail_recovery_barrier(&request_id, &reason);
                        Err(reason)
                    }
                    Err(_) => {
                        let reason =
                            "coordinator disappeared while the recovery barrier was active";
                        self.fail_recovery_barrier(&request_id, reason);
                        Err(reason.to_string())
                    }
                };
                let release_was_durable = resume_result.is_ok();
                if resumed.send(resume_result).is_err() && release_was_durable {
                    self.fail_recovery_barrier(
                        &request_id,
                        "coordinator did not receive the order-actor resume acknowledgement",
                    );
                }
            }
            Err(error) => {
                self.fail_recovery_barrier(&request_id, &error);
                let _ = reply.send(Err(error.clone()));
                let _ = resumed.send(Err(error));
            }
        }
    }

    fn latch_symbol_persistence_failure(&mut self, symbol: &str, context: &str, error: &str) {
        let symbol = symbol.to_ascii_uppercase();
        let now_ms = Self::current_time_ms().max(1);
        let latch = self
            .symbol_persistence_latches
            .entry(symbol.clone())
            .or_insert_with(|| SymbolPersistenceLatch {
                schema_version: SYMBOL_PERSISTENCE_LATCH_SCHEMA_VERSION,
                symbol: symbol.clone(),
                reason: context.to_string(),
                first_failed_at_ms: now_ms,
                last_failed_at_ms: now_ms,
                failure_count: 0,
                last_error: error.to_string(),
            });
        latch.last_failed_at_ms = now_ms;
        latch.failure_count = latch.failure_count.saturating_add(1);
        latch.reason = context.to_string();
        latch.last_error = error.to_string();
        error!(
            "Execution-state persistence latched {} during {}: {}",
            symbol, context, error
        );
        let alert = serde_json::json!({
            "event": "SymbolPersistenceLatch",
            "symbol": symbol,
            "status": "LATCHED",
            "reason": context,
            "error": error,
            "failure_count": latch.failure_count,
            "event_time_ms": now_ms,
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&alert) {
            let _ = self.dash_tx.send(payload);
        }
    }

    fn is_symbol_persistence_latched(&self, symbol: &str) -> bool {
        self.symbol_persistence_latches
            .contains_key(&symbol.to_ascii_uppercase())
    }

    fn clear_symbol_persistence_latches_after_reconciliation(&mut self) -> bool {
        if self.symbol_persistence_latches.is_empty() {
            return true;
        }
        let retained = std::mem::take(&mut self.symbol_persistence_latches);
        match self.try_persist_execution_state() {
            Ok(()) => {
                for symbol in retained.keys() {
                    let alert = serde_json::json!({
                        "event": "SymbolPersistenceLatch",
                        "symbol": symbol,
                        "status": "CLEARED",
                        "reason": "signed account and open-order reconciliation proved exchange truth",
                        "event_time_ms": Self::current_time_ms(),
                    });
                    if let Ok(payload) = rmp_serde::to_vec_named(&alert) {
                        let _ = self.dash_tx.send(payload);
                    }
                }
                true
            }
            Err(error) => {
                self.symbol_persistence_latches = retained;
                error!(
                    "Could not durably clear symbol persistence latches after reconciliation: {}",
                    error
                );
                false
            }
        }
    }

    fn persist_execution_state_for_symbol(&mut self, symbol: &str, context: &str) -> bool {
        match self.try_persist_execution_state() {
            Ok(()) => true,
            Err(error) => {
                self.latch_symbol_persistence_failure(symbol, context, &error);
                false
            }
        }
    }

    fn store_chase_state(&mut self, symbol: String, chase: ChaseState, context: &str) -> bool {
        let symbol = symbol.to_ascii_uppercase();
        self.chase_states.insert(symbol.clone(), chase);
        self.persist_execution_state_for_symbol(&symbol, context)
    }

    fn remove_chase_state(&mut self, symbol: &str, context: &str) -> Option<ChaseState> {
        let symbol = symbol.to_ascii_uppercase();
        let removed = self.chase_states.remove(&symbol)?;
        let unhedged_budget = self.chase_unhedged_budgets.remove(&symbol);
        let unhedged_started_at_ms = self.chase_unhedged_started_at_ms.remove(&symbol);
        let cycle_id = removed.cycle_client_order_id().to_string();
        let cycle_deadline = self.cycle_deadlines.remove(&cycle_id);
        if !self.terminal_tombstones.contains_key(&cycle_id) {
            self.ensure_terminal_tombstone(
                &removed,
                "EXCHANGE_FLAT_AWAITING_TERMINAL",
                "AWAITING_LATE_PRIVATE_EVENTS",
                context,
            );
        }
        if self.persist_execution_state_for_symbol(&symbol, context) {
            return Some(removed);
        }

        self.chase_states.insert(symbol.clone(), removed);
        if let Some(budget) = unhedged_budget {
            self.chase_unhedged_budgets.insert(symbol.clone(), budget);
        }
        if let Some(started_at_ms) = unhedged_started_at_ms {
            self.chase_unhedged_started_at_ms
                .insert(symbol.clone(), started_at_ms);
        }
        if let Some(deadline) = cycle_deadline {
            self.cycle_deadlines.insert(cycle_id, deadline);
        }
        None
    }

    fn recover_chase_from_terminal_tombstone(
        &mut self,
        symbol: &str,
        client_order_id: &str,
    ) -> Option<ChaseState> {
        let symbol = symbol.to_ascii_uppercase();
        let cycle_id = self
            .terminal_tombstones
            .iter()
            .find(|(_, tombstone)| {
                tombstone.symbol == symbol
                    && tombstone
                        .client_order_ids
                        .iter()
                        .any(|known| known == client_order_id)
            })
            .map(|(cycle_id, _)| cycle_id.clone())?;
        let tombstone = self.terminal_tombstones.get(&cycle_id)?.clone();
        for (client_id, order) in tombstone.internal_orders {
            self.internal_orders.entry(client_id).or_insert(order);
        }
        for (client_id, fill) in tombstone.order_cumulative_fills {
            self.order_cumulative_fills.entry(client_id).or_insert(fill);
        }
        for (client_id, lineage) in tombstone.order_lineage {
            self.order_lineage.entry(client_id).or_insert(lineage);
        }
        if let Some(intent_id) = tombstone.intent_id {
            self.chase_intent_ids
                .entry(symbol.clone())
                .or_insert(intent_id);
        }
        let mut chase = tombstone.chase_state;
        chase.ensure_active_aliases();
        chase.phase = ChasePhase::ReconciliationRequired;
        self.terminal_sequence_watermark =
            self.terminal_sequence_watermark.saturating_add(1).max(1);
        if let Some(retained) = self.terminal_tombstones.get_mut(&cycle_id) {
            retained.lifecycle_state = "EXCHANGE_FLAT_AWAITING_TERMINAL".to_string();
            retained.reconciliation_status = "LATE_FILL_REPAIR_REQUIRED".to_string();
            retained.terminal_sequence_watermark = self.terminal_sequence_watermark;
            retained.reason = "late private fill recovered from terminal tombstone".to_string();
        }
        warn!(
            "Recovered terminal lifecycle {} for late private update {} on {}; signed repair is required",
            cycle_id, client_order_id, symbol
        );
        Some(chase)
    }

    fn has_unresolved_execution_effects(&self) -> bool {
        !self.chase_states.is_empty()
            || !self.symbol_persistence_latches.is_empty()
            || self
                .emergency_exits
                .values()
                .any(|record| record.state != EmergencyExitState::Flat)
            || !self.unvalued_commission_assets.is_empty()
            || self
                .internal_orders
                .values()
                .any(|order| !is_terminal_internal_status(&order.status))
    }

    fn execution_state_storage_allows_new_risk(&self) -> Result<(), String> {
        self.execution_state_storage_allows_new_risk_at_limit(Self::execution_state_byte_limit())
    }

    fn execution_state_storage_allows_new_risk_at_limit(
        &self,
        configured: u64,
    ) -> Result<(), String> {
        let current = match self.execution_state_journal_path.metadata() {
            Ok(metadata) => metadata.len(),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => 0,
            Err(err) => return Err(format!("execution state size probe failed: {err}")),
        };
        let next_snapshot_bytes = serde_json::to_vec(&self.execution_snapshot())
            .map_err(|err| format!("execution state projection encode failed: {err}"))?
            .len() as u64
            + 1;
        let append_projected = current.saturating_add(next_snapshot_bytes);
        let projected = if current >= configured / 2
            || append_projected.saturating_add(EXECUTION_STATE_TRANSITION_RESERVE_BYTES)
                > configured
        {
            next_snapshot_bytes.saturating_add(EXECUTION_STATE_TRANSITION_RESERVE_BYTES)
        } else {
            append_projected.saturating_add(EXECUTION_STATE_TRANSITION_RESERVE_BYTES)
        };
        if projected > configured {
            Err(format!(
                "execution state entry budget exhausted: current={current}, next={next_snapshot_bytes}, transition_reserve={EXECUTION_STATE_TRANSITION_RESERVE_BYTES}, projected={projected}, limit={configured}"
            ))
        } else {
            Ok(())
        }
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

    fn market_stream_role_key(symbol: &str, connection_role: &str) -> String {
        format!("{}:{}", symbol.trim().to_uppercase(), connection_role)
    }

    fn market_data_quorum_ready(&self, symbol: &str) -> bool {
        ["spot-public", "futures-public", "futures-market"]
            .into_iter()
            .all(|role| {
                self.market_stream_ready_roles
                    .contains(&Self::market_stream_role_key(symbol, role))
            })
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
                !is_terminal_internal_status(&order.status)
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

    fn emit_exchange_quota_snapshot(rest: &BinanceRest, dash_tx: &broadcast::Sender<Vec<u8>>) {
        let snapshot = rest.rate_limit_snapshot();
        let event = WsEvent::ExchangeQuota {
            status: snapshot.status,
            reason: snapshot.reason,
            spot_limit_weight: snapshot.spot_limit_weight,
            spot_used_weight: snapshot.spot_used_weight,
            spot_remaining_weight: snapshot.spot_remaining_weight,
            spot_observed_at_ms: snapshot.spot_observed_at_ms,
            futures_limit_weight: snapshot.futures_limit_weight,
            futures_used_weight: snapshot.futures_used_weight,
            futures_remaining_weight: snapshot.futures_remaining_weight,
            futures_observed_at_ms: snapshot.futures_observed_at_ms,
            combined_remaining_weight: snapshot.combined_remaining_weight,
            spot_order_limit_10s: snapshot.spot_order_limit_10s,
            spot_order_used_10s: snapshot.spot_order_used_10s,
            spot_order_remaining_10s: snapshot.spot_order_remaining_10s,
            spot_order_limit_1m: snapshot.spot_order_limit_1m,
            spot_order_used_1m: snapshot.spot_order_used_1m,
            spot_order_remaining_1m: snapshot.spot_order_remaining_1m,
            futures_order_limit_10s: snapshot.futures_order_limit_10s,
            futures_order_used_10s: snapshot.futures_order_used_10s,
            futures_order_remaining_10s: snapshot.futures_order_remaining_10s,
            futures_order_limit_1m: snapshot.futures_order_limit_1m,
            futures_order_used_1m: snapshot.futures_order_used_1m,
            futures_order_remaining_1m: snapshot.futures_order_remaining_1m,
            max_utilization_bps: snapshot.max_utilization_bps,
            nonessential_allowed: snapshot.nonessential_allowed,
            entry_allowed: snapshot.entry_allowed,
            critical_allowed: snapshot.critical_allowed,
            reserved_request_weight: snapshot.reserved_request_weight,
            reserved_order_count: snapshot.reserved_order_count,
            ambiguous_until_ms: snapshot.ambiguous_until_ms,
            last_failure_class: snapshot.last_failure_class,
            blocked_until_ms: snapshot.blocked_until_ms,
            event_time_ms: snapshot.event_time_ms,
        };
        if let Ok(payload) = rmp_serde::to_vec_named(&event) {
            let _ = dash_tx.send(payload);
        }
        let clock = rest.clock_health_snapshot();
        let clock_event = serde_json::json!({
            "event": "ExchangeClockHealth",
            "status": clock.status,
            "reason": clock.reason,
            "synchronized": clock.synchronized,
            "warning": clock.warning,
            "entry_allowed": clock.entry_allowed,
            "offset_ms": clock.offset_ms,
            "round_trip_ms": clock.round_trip_ms,
            "observed_at_ms": clock.observed_at_ms,
            "event_time_ms": clock.event_time_ms,
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&clock_event) {
            let _ = dash_tx.send(payload);
        }
    }

    fn entry_quota_block_reason(&self) -> Option<&'static str> {
        if self.binance_rest.trading_mode == "paper" {
            return None;
        }
        if let Some(reason) = self.binance_rest.quota_block_reason(RestWorkClass::Entry) {
            return Some(reason);
        }
        let snapshot = self.binance_rest.rate_limit_snapshot();
        (snapshot.combined_remaining_weight < MIN_ENTRY_RATE_LIMIT_WEIGHT)
            .then_some("insufficient_exchange_rate_limit_budget")
    }

    fn critical_quota_guard(&self) -> Result<(), String> {
        self.binance_rest
            .quota_block_reason(RestWorkClass::Critical)
            .map_or(Ok(()), |reason| Err(reason.to_string()))
    }

    fn emit_continuous_risk_state(&self, durable: bool) {
        let event = serde_json::json!({
            "event": "ContinuousRiskState",
            "state": self.continuous_risk_state.as_str(),
            "reason": self.continuous_risk_reason,
            "transition_sequence": self.continuous_risk_sequence,
            "event_time_ms": self.continuous_risk_updated_at_ms,
            "persist_time_ms": durable.then_some(self.continuous_risk_updated_at_ms),
            "durable": durable,
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&event) {
            let _ = self.dash_tx.send(payload);
        }
    }

    fn transition_continuous_risk_step(&mut self, next: ContinuousRiskState, reason: &str) -> bool {
        // Re-evaluation happens on every market/account/order event. Once a
        // state is latched, identical observations must not rewrite the
        // durable journal or duplicate state-transition telemetry.
        if self.continuous_risk_state == next {
            return true;
        }
        let allowed = matches!(
            (self.continuous_risk_state, next),
            (
                ContinuousRiskState::Normal,
                ContinuousRiskState::EntryFrozen
            ) | (
                ContinuousRiskState::EntryFrozen,
                ContinuousRiskState::CancelingEntries
            ) | (
                ContinuousRiskState::CancelingEntries,
                ContinuousRiskState::Reconciling
            ) | (
                ContinuousRiskState::Reconciling,
                ContinuousRiskState::Derisking
            ) | (
                ContinuousRiskState::Reconciling,
                ContinuousRiskState::ManualReview
            ) | (
                ContinuousRiskState::Derisking,
                ContinuousRiskState::ManualReview
            )
        );
        if !allowed {
            error!(
                "Rejected non-monotonic continuous-risk transition {} -> {} ({reason})",
                self.continuous_risk_state.as_str(),
                next.as_str()
            );
            return false;
        }
        self.continuous_risk_state = next;
        self.continuous_risk_reason = reason.to_string();
        self.continuous_risk_sequence = self.continuous_risk_sequence.saturating_add(1);
        self.continuous_risk_updated_at_ms = Self::current_time_ms();
        if !self.persist_execution_state("continuous risk transition") {
            return false;
        }
        self.emit_continuous_risk_state(true);
        true
    }

    fn advance_continuous_risk(&mut self, target: ContinuousRiskState, reason: &str) -> bool {
        loop {
            if self.continuous_risk_state == target {
                return self.transition_continuous_risk_step(target, reason);
            }
            let next = match self.continuous_risk_state {
                ContinuousRiskState::Normal => ContinuousRiskState::EntryFrozen,
                ContinuousRiskState::EntryFrozen => ContinuousRiskState::CancelingEntries,
                ContinuousRiskState::CancelingEntries => ContinuousRiskState::Reconciling,
                ContinuousRiskState::Reconciling => match target {
                    ContinuousRiskState::ManualReview => ContinuousRiskState::ManualReview,
                    ContinuousRiskState::Derisking => ContinuousRiskState::Derisking,
                    _ => return false,
                },
                ContinuousRiskState::Derisking if target == ContinuousRiskState::ManualReview => {
                    ContinuousRiskState::ManualReview
                }
                ContinuousRiskState::Derisking | ContinuousRiskState::ManualReview => return false,
            };
            if !self.transition_continuous_risk_step(next, reason) {
                return false;
            }
        }
    }

    fn clear_continuous_risk_after_proof(&mut self, reason: &str) -> bool {
        if matches!(
            self.continuous_risk_state,
            ContinuousRiskState::Derisking | ContinuousRiskState::ManualReview
        ) {
            return false;
        }
        self.continuous_risk_state = ContinuousRiskState::Normal;
        self.continuous_risk_reason = reason.to_string();
        self.continuous_risk_sequence = self.continuous_risk_sequence.saturating_add(1);
        self.continuous_risk_updated_at_ms = Self::current_time_ms();
        if !self.persist_execution_state("continuous risk cleared after authoritative proof") {
            return false;
        }
        self.emit_continuous_risk_state(true);
        true
    }

    fn entry_chase_has_exchange_effect(&self, chase: &ChaseState) -> bool {
        [Leg::Spot, Leg::Futures].into_iter().any(|leg| {
            if (leg == Leg::Spot && !chase.has_spot_leg())
                || (leg == Leg::Futures && !chase.has_futures_leg())
            {
                return false;
            }
            let client_id = chase.active_client_order_id(leg);
            self.internal_orders.get(client_id).is_some_and(|order| {
                !is_terminal_internal_status(&order.status)
                    && order.status != "PENDING_SUBMIT"
                    && order.status != "NOT_SUBMITTED"
            }) || self
                .order_cumulative_fills
                .get(client_id)
                .is_some_and(|filled| *filled > 0.0)
        })
    }

    async fn activate_continuous_risk(&mut self, reason: String, terminal: ContinuousRiskState) {
        if self.risk_evaluation_active
            || self.continuous_risk_state == ContinuousRiskState::ManualReview
        {
            return;
        }
        self.risk_evaluation_active = true;
        if self.continuous_risk_state == ContinuousRiskState::Normal
            && !self.transition_continuous_risk_step(ContinuousRiskState::EntryFrozen, &reason)
        {
            self.risk_evaluation_active = false;
            return;
        }

        let entry_symbols: Vec<String> = self
            .chase_states
            .iter()
            .filter(|(_, chase)| !chase.is_exit && chase.phase != ChasePhase::Completed)
            .map(|(symbol, _)| symbol.clone())
            .collect();
        let requires_reconciliation = !entry_symbols.is_empty()
            || matches!(
                terminal,
                ContinuousRiskState::Reconciling
                    | ContinuousRiskState::Derisking
                    | ContinuousRiskState::ManualReview
            );

        let mut cancellation_failed = false;
        if requires_reconciliation
            && self.continuous_risk_state == ContinuousRiskState::EntryFrozen
            && !self.transition_continuous_risk_step(ContinuousRiskState::CancelingEntries, &reason)
        {
            self.risk_evaluation_active = false;
            return;
        }

        for symbol in entry_symbols {
            let Some(mut chase) = self.chase_states.get(&symbol).cloned() else {
                continue;
            };
            if !self.entry_chase_has_exchange_effect(&chase) {
                let _ = self.emit_cycle_order_update(
                    &chase,
                    "REJECTED",
                    chase.cycle_client_order_id(),
                    0.0,
                    false,
                    "CONTINUOUS_RISK_FROZEN_BEFORE_SUBMISSION",
                );
                self.remove_chase_state(&symbol, "continuous risk removed unsubmitted entry chase");
                continue;
            }

            chase.phase = ChasePhase::DeadlineFreezing;
            if !self.store_chase_state(
                symbol.clone(),
                chase.clone(),
                "continuous risk froze active entry chase",
            ) {
                cancellation_failed = true;
                continue;
            }
            for leg in [Leg::Spot, Leg::Futures] {
                if (leg == Leg::Spot && !chase.has_spot_leg())
                    || (leg == Leg::Futures && !chase.has_futures_leg())
                {
                    continue;
                }
                let client_id = chase.active_client_order_id(leg).to_string();
                let status = self
                    .internal_orders
                    .get(&client_id)
                    .map(|order| order.status.as_str())
                    .unwrap_or("UNKNOWN");
                if is_terminal_internal_status(status) || status == "NOT_SUBMITTED" {
                    continue;
                }
                if status == "PENDING_SUBMIT" {
                    if let Some(order) = self.internal_orders.get_mut(&client_id) {
                        order.status = "NOT_SUBMITTED".to_string();
                    }
                    continue;
                }
                if self.trading_mode == "paper" {
                    if let Some(order) = self.internal_orders.get_mut(&client_id) {
                        order.status = "CANCELED".to_string();
                    }
                    continue;
                }
                let venue = match leg {
                    Leg::Spot => LegVenue::Spot,
                    Leg::Futures => LegVenue::UsdtFutures,
                };
                match self
                    .cancel_order_pumped(venue, &chase.symbol, &client_id)
                    .await
                {
                    Ok(_) => {
                        if let Some(order) = self.internal_orders.get_mut(&client_id) {
                            order.status = "CANCEL_REQUESTED".to_string();
                        }
                    }
                    Err(error) => {
                        error!(
                            "Continuous risk could not cancel {} {}: {}",
                            chase.symbol, client_id, error
                        );
                        cancellation_failed = true;
                    }
                }
            }
            if let Some(mut latest) = self.chase_states.get(&symbol).cloned() {
                latest.phase = ChasePhase::ReconciliationRequired;
                let _ = self.store_chase_state(
                    symbol,
                    latest,
                    "continuous risk retained canceled entry for signed reconciliation",
                );
            }
        }

        if requires_reconciliation {
            self.state = SystemState::Reconciling;
            let _ = self.advance_continuous_risk(ContinuousRiskState::Reconciling, &reason);
        }
        let final_state = if cancellation_failed {
            ContinuousRiskState::ManualReview
        } else {
            terminal
        };
        let terminal_transitioned = if matches!(
            final_state,
            ContinuousRiskState::Derisking | ContinuousRiskState::ManualReview
        ) {
            self.advance_continuous_risk(final_state, &reason)
        } else {
            true
        };
        let mut readiness_reason = reason;
        if final_state == ContinuousRiskState::Derisking && terminal_transitioned {
            if let Err(error) = self
                .drive_autonomous_emergency_exits(&readiness_reason)
                .await
            {
                let failure_reason = format!("autonomous_emergency_flatten_failed:{error}");
                error!("{failure_reason}");
                if self.continuous_risk_state != ContinuousRiskState::ManualReview {
                    let _ = self.advance_continuous_risk(
                        ContinuousRiskState::ManualReview,
                        &failure_reason,
                    );
                }
                readiness_reason = failure_reason;
            }
        } else if final_state == ContinuousRiskState::Derisking {
            readiness_reason =
                "autonomous_emergency_flatten_blocked:derisk_transition_not_durable".to_string();
        }
        self.emit_execution_readiness("BLOCKED", &readiness_reason);
        self.risk_evaluation_active = false;
    }

    fn continuous_risk_assessment(&self) -> Option<(String, ContinuousRiskState)> {
        if self.execution_state_journal_error.is_some() {
            return Some((
                "execution_state_journal_unavailable".to_string(),
                ContinuousRiskState::ManualReview,
            ));
        }
        if self.storage_control_error.is_some() || self.storage_emergency_latched {
            return Some((
                "storage_control_entry_freeze".to_string(),
                ContinuousRiskState::Reconciling,
            ));
        }
        if !self.public_stream_recovery_symbols.is_empty() {
            let mut symbols: Vec<&str> = self
                .public_stream_recovery_symbols
                .iter()
                .map(String::as_str)
                .collect();
            symbols.sort_unstable();
            return Some((
                format!(
                    "public_market_data_quorum_unavailable:{}",
                    symbols.join(",")
                ),
                ContinuousRiskState::Reconciling,
            ));
        }
        if self.trading_mode != "paper" {
            let clock = self.binance_rest.clock_health_snapshot();
            if !clock.entry_allowed {
                return Some((
                    format!(
                        "clock:{}:offset_ms={}:round_trip_ms={}",
                        clock.reason, clock.offset_ms, clock.round_trip_ms
                    ),
                    ContinuousRiskState::Reconciling,
                ));
            }
        }
        if self.brain_ping_age() > PYTHON_BRAIN_STALE_AFTER {
            return Some((
                "python_brain_stale".to_string(),
                if self.has_tracked_risk_inventory() {
                    ContinuousRiskState::Derisking
                } else {
                    ContinuousRiskState::Reconciling
                },
            ));
        }
        let circuit_breaker = self.circuit_breaker_assessment();
        if circuit_breaker.as_ref().is_some_and(|(_, target)| {
            matches!(
                target,
                ContinuousRiskState::Derisking | ContinuousRiskState::ManualReview
            )
        }) {
            return circuit_breaker;
        }
        // Entry quota pressure must not mask a stale-brain, margin, basis, or
        // gross-exposure liquidation. Emergency work uses the separately
        // reserved Critical quota class.
        if let Some(reason) = self.entry_quota_block_reason() {
            return Some((format!("quota:{reason}"), ContinuousRiskState::EntryFrozen));
        }
        circuit_breaker
    }

    fn brain_ping_age(&self) -> Duration {
        #[cfg(test)]
        if let Some(age) = self.brain_ping_age_override {
            return age;
        }
        self.last_brain_ping.elapsed()
    }

    fn has_tracked_risk_inventory(&self) -> bool {
        self.tracked_positions.values().any(|position| {
            [position.spot.as_ref(), position.perp.as_ref()]
                .into_iter()
                .flatten()
                .any(|leg| leg.quantity.is_finite() && leg.quantity > 0.0)
        })
    }

    async fn reevaluate_continuous_risk(&mut self, trigger: &str) {
        if self.risk_evaluation_active {
            return;
        }
        if self.trading_mode != "paper" {
            let clock = self.binance_rest.clock_health_snapshot();
            if clock.warning && !self.clock_warning_latched {
                warn!(
                    "Exchange clock health warning on {trigger}: offset={}ms round_trip={}ms ({})",
                    clock.offset_ms, clock.round_trip_ms, clock.reason
                );
            }
            self.clock_warning_latched = clock.warning;
        }
        if let Some((reason, target)) = self.continuous_risk_assessment() {
            self.activate_continuous_risk(reason, target).await;
            return;
        }
        let auto_clear = self.continuous_risk_state == ContinuousRiskState::EntryFrozen
            && self.state == SystemState::Trading
            && self.continuous_risk_reason.starts_with("quota:")
            && !self
                .chase_states
                .values()
                .any(|chase| !chase.is_exit && chase.phase != ChasePhase::Completed);
        if auto_clear {
            let _ = self.clear_continuous_risk_after_proof("quota capacity recovered");
        }
    }

    fn emit_current_execution_state_snapshot(&self) {
        self.replay_terminal_publications();
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
        } else if self.continuous_risk_state != ContinuousRiskState::Normal {
            ("BLOCKED", "continuous risk actor is not NORMAL")
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
        if self.binance_rest.trading_mode != "paper" {
            Self::emit_exchange_quota_snapshot(&self.binance_rest, &self.dash_tx);
        }
    }

    fn circuit_breaker_assessment(&self) -> Option<(String, ContinuousRiskState)> {
        if self.current_gross_exposure_usd > self.max_gross_exposure_usd {
            return Some((
                format!(
                    "gross_exposure_limit:{:.2}>{:.2}",
                    self.current_gross_exposure_usd, self.max_gross_exposure_usd
                ),
                ContinuousRiskState::Derisking,
            ));
        }

        if self.trading_mode != "paper" {
            let now_ms = Self::current_time_ms();
            let Some(spot_truth) = self.standard_spot_account_truth.as_ref() else {
                return Some((
                    "standard_spot_account_truth_unknown".to_string(),
                    ContinuousRiskState::EntryFrozen,
                ));
            };
            let Some(usdm_truth) = self.usdm_account_truth.as_ref() else {
                return Some((
                    "usdm_account_truth_unknown".to_string(),
                    ContinuousRiskState::EntryFrozen,
                ));
            };
            if spot_truth.borrow_state != "NOT_APPLICABLE_STANDARD_SPOT" {
                return Some((
                    "standard_spot_borrow_state_unknown".to_string(),
                    ContinuousRiskState::ManualReview,
                ));
            }
            let oldest_observation = spot_truth.observed_at_ms.min(usdm_truth.observed_at_ms);
            if oldest_observation <= 0
                || now_ms.saturating_sub(oldest_observation) > ACCOUNT_TRUTH_MAX_AGE_MS
            {
                return Some((
                    "signed_account_truth_stale".to_string(),
                    ContinuousRiskState::EntryFrozen,
                ));
            }
            // Order submission in this engine intentionally uses one-way-mode
            // semantics (reduceOnly and no positionSide). Hedge-mode accounts
            // require a different, explicitly tested command contract.
            if usdm_truth.position_mode != "ONE_WAY" {
                return Some((
                    "unsupported_usdm_position_mode".to_string(),
                    ContinuousRiskState::EntryFrozen,
                ));
            }
            if usdm_truth.margin_ratio >= 0.8 {
                return Some((
                    format!("signed_usdm_margin_ratio:{:.6}", usdm_truth.margin_ratio),
                    ContinuousRiskState::Derisking,
                ));
            }
            let available_balance = usdm_truth
                .available_balance
                .parse::<f64>()
                .ok()
                .filter(|value| value.is_finite());
            if available_balance.is_none_or(|value| value < 0.0) {
                return Some((
                    "signed_usdm_available_balance_invalid".to_string(),
                    ContinuousRiskState::Derisking,
                ));
            }
        }

        if !self.tracked_positions.is_empty() {
            let total_perp_notional: f64 = self
                .tracked_positions
                .values()
                .filter_map(|position| position.perp.as_ref())
                .map(|leg| (leg.last_mark_price * leg.quantity).abs())
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
                return Some((
                    "nonpositive_unified_equity".to_string(),
                    ContinuousRiskState::Derisking,
                ));
            }

            // Spot and USD-M are separate account topologies. A delta-neutral
            // spot/perp pair does not net away the futures maintenance-margin
            // requirement; estimate it from gross USD-M notional only.
            let estimated_usdm_mmr = total_perp_notional * 0.004 / unified_equity;
            if estimated_usdm_mmr >= 0.8 {
                return Some((
                    format!("estimated_usdm_mmr:{estimated_usdm_mmr:.6}"),
                    ContinuousRiskState::Derisking,
                ));
            }
        }

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
                    return Some((
                        format!(
                            "adverse_basis_stop:{symbol}:{adverse_deviation_bps:.4}>{:.4}",
                            self.basis_deviation_stop_bps
                        ),
                        ContinuousRiskState::Derisking,
                    ));
                }
            }
        }

        None
    }

    async fn check_circuit_breakers(&mut self) -> bool {
        if self.brain_ping_age() > PYTHON_BRAIN_STALE_AFTER {
            warn!(
                "CRITICAL: Python brain has not sent instructions in > 12 mins. Halting trading."
            );
            return true;
        }
        if let Some((reason, _)) = self.circuit_breaker_assessment() {
            warn!("CRITICAL: Continuous risk breaker triggered: {reason}");
            return true;
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

    fn base_asset_for_symbol(symbol: &str) -> Option<&str> {
        ["USDT", "USDC", "FDUSD"]
            .iter()
            .find_map(|quote| symbol.strip_suffix(quote))
            .filter(|asset| !asset.is_empty())
    }

    fn quote_asset_for_symbol(symbol: &str) -> Option<&'static str> {
        ["USDT", "USDC", "FDUSD"]
            .into_iter()
            .find(|quote| symbol.ends_with(quote))
    }

    fn parse_spot_account_balances(body: &str) -> Result<SpotAccountBalances, String> {
        let document: Value = serde_json::from_str(body)
            .map_err(|err| format!("invalid spot account JSON: {err}"))?;
        let rows = document
            .get("balances")
            .and_then(Value::as_array)
            .ok_or_else(|| "spot account is missing balances".to_string())?;
        let mut total_balances = HashMap::new();
        let mut available_balances = HashMap::new();
        for row in rows {
            let asset = row
                .get("asset")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| "spot balance row is missing asset".to_string())?;
            let parse_amount = |field: &str| -> Result<f64, String> {
                row.get(field)
                    .and_then(|node| {
                        node.as_f64()
                            .or_else(|| node.as_str().and_then(|raw| raw.parse::<f64>().ok()))
                    })
                    .filter(|value| value.is_finite() && *value >= 0.0)
                    .ok_or_else(|| format!("spot balance {asset} has invalid {field}"))
            };
            let free = parse_amount("free")?;
            let total = free + parse_amount("locked")?;
            if !total.is_finite() {
                return Err(format!("spot balance {asset} overflowed"));
            }
            if total_balances.insert(asset.to_string(), total).is_some()
                || available_balances.insert(asset.to_string(), free).is_some()
            {
                return Err(format!(
                    "spot account contains duplicate balance for {asset}"
                ));
            }
        }
        Ok(SpotAccountBalances {
            total: total_balances,
            available: available_balances,
        })
    }

    #[cfg(test)]
    fn parse_spot_balances(body: &str) -> Result<HashMap<String, f64>, String> {
        Self::parse_spot_account_balances(body).map(|balances| balances.total)
    }

    fn spot_inventory_divergences(
        &self,
        exchange_balances: &HashMap<String, f64>,
    ) -> Vec<(String, &'static str, f64, f64)> {
        let mut expected_by_asset = HashMap::<String, (String, f64, f64)>::new();
        for (symbol, position) in &self.tracked_positions {
            let Some(spot) = position.spot.as_ref() else {
                continue;
            };
            let Some(asset) = Self::base_asset_for_symbol(symbol) else {
                continue;
            };
            let tolerance = self
                .exchange_info
                .get(symbol)
                .and_then(|info| info.spot_step_size.to_f64())
                .map(|step| step / 2.0)
                .unwrap_or(1e-8)
                .max(1e-8);
            let entry = expected_by_asset
                .entry(asset.to_string())
                .or_insert_with(|| (symbol.clone(), 0.0, tolerance));
            entry.1 += spot.quantity;
            entry.2 = entry.2.max(tolerance);
        }

        let mut divergences = Vec::new();
        for (asset, (symbol, local_qty, tolerance)) in &expected_by_asset {
            let exchange_qty = exchange_balances.get(asset).copied().unwrap_or(0.0);
            if (exchange_qty - *local_qty).abs() > *tolerance {
                divergences.push((
                    symbol.clone(),
                    "spot_quantity_mismatch",
                    *local_qty,
                    exchange_qty,
                ));
            }
        }
        for (asset, exchange_qty) in exchange_balances {
            if *exchange_qty <= 0.0
                || matches!(asset.as_str(), "USDT" | "USDC" | "FDUSD")
                || expected_by_asset.contains_key(asset)
            {
                continue;
            }
            let symbol = ["USDT", "USDC", "FDUSD"]
                .into_iter()
                .map(|quote| format!("{asset}{quote}"))
                .find(|candidate| self.exchange_info.contains_key(candidate));
            let Some(symbol) = symbol else {
                continue;
            };
            let non_actionable_dust = self
                .exchange_info
                .get(&symbol)
                .and_then(|info| {
                    Some(
                        info.spot_min_qty
                            .to_f64()?
                            .max(info.spot_market_min_qty.to_f64()?),
                    )
                })
                .is_some_and(|minimum_qty| {
                    minimum_qty.is_finite() && minimum_qty > 0.0 && *exchange_qty < minimum_qty
                });
            if non_actionable_dust {
                continue;
            }
            let tolerance = self
                .exchange_info
                .get(&symbol)
                .and_then(|info| info.spot_step_size.to_f64())
                .map(|step| step / 2.0)
                .unwrap_or(1e-8)
                .max(1e-8);
            if *exchange_qty > tolerance {
                divergences.push((symbol, "spot_exchange_only", 0.0, *exchange_qty));
            }
        }
        divergences
    }

    fn parse_futures_positions(body: &str) -> Result<HashMap<String, f64>, String> {
        let rows: Vec<Value> = serde_json::from_str(body)
            .map_err(|err| format!("invalid futures position JSON: {err}"))?;
        let mut positions = HashMap::new();
        let mut hedge_mode_gross = HashMap::<String, (f64, f64)>::new();
        for row in rows {
            let symbol = row
                .get("symbol")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|symbol| !symbol.is_empty())
                .ok_or_else(|| "futures position row is missing symbol".to_string())?
                .to_uppercase();
            let quantity = row
                .get("positionAmt")
                .and_then(|node| {
                    node.as_f64()
                        .or_else(|| node.as_str().and_then(|raw| raw.parse::<f64>().ok()))
                })
                .filter(|quantity| quantity.is_finite())
                .ok_or_else(|| format!("futures position {symbol} has invalid positionAmt"))?;
            let position_side = row
                .get("positionSide")
                .and_then(Value::as_str)
                .unwrap_or("BOTH")
                .trim()
                .to_ascii_uppercase();
            match position_side.as_str() {
                "LONG" => {
                    if quantity < -1e-12 {
                        return Err(format!(
                            "futures position {symbol} LONG row has negative positionAmt"
                        ));
                    }
                    hedge_mode_gross.entry(symbol.clone()).or_default().0 += quantity.max(0.0);
                }
                "SHORT" => {
                    if quantity > 1e-12 {
                        return Err(format!(
                            "futures position {symbol} SHORT row has positive positionAmt"
                        ));
                    }
                    hedge_mode_gross.entry(symbol.clone()).or_default().1 += (-quantity).max(0.0);
                }
                "BOTH" => {}
                _ => {
                    return Err(format!(
                        "futures position {symbol} has unsupported positionSide {position_side}"
                    ));
                }
            }
            *positions.entry(symbol).or_insert(0.0) += quantity;
        }
        for (symbol, (long_gross, short_gross)) in hedge_mode_gross {
            if long_gross > 1e-12 && short_gross > 1e-12 {
                return Err(format!(
                    "futures position {symbol} has simultaneous LONG and SHORT hedge-mode exposure"
                ));
            }
        }
        positions.retain(|_, quantity| quantity.abs() > 1e-12);
        Ok(positions)
    }

    fn parse_canonical_exchange_decimal(
        node: Option<&Value>,
        field: &str,
        allow_negative: bool,
    ) -> Result<ExactDecimal, String> {
        let raw = node
            .and_then(Value::as_str)
            .ok_or_else(|| format!("{field} must be an exact decimal string"))?;
        let value = raw
            .parse::<ExactDecimal>()
            .map_err(|error| format!("{field} is not a supported decimal: {error}"))?;
        if value.to_string() != raw {
            return Err(format!("{field} is not canonical"));
        }
        if !allow_negative && value < ExactDecimal::ZERO {
            return Err(format!("{field} must be non-negative"));
        }
        Ok(value)
    }

    fn parse_exact_spot_account_balances(body: &str) -> Result<ExactSpotAccountBalances, String> {
        let document: Value = serde_json::from_str(body)
            .map_err(|error| format!("invalid exact spot account JSON: {error}"))?;
        let rows = document
            .get("balances")
            .and_then(Value::as_array)
            .ok_or_else(|| "exact spot account is missing balances".to_string())?;
        let mut total = HashMap::new();
        let mut available = HashMap::new();
        for row in rows {
            let asset = row
                .get("asset")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| "exact spot balance row is missing asset".to_string())?;
            let free = Self::parse_canonical_exchange_decimal(
                row.get("free"),
                &format!("spot balance {asset} free"),
                false,
            )?;
            let locked = Self::parse_canonical_exchange_decimal(
                row.get("locked"),
                &format!("spot balance {asset} locked"),
                false,
            )?;
            let combined = free
                .checked_add(locked)
                .ok_or_else(|| format!("spot balance {asset} exact total overflowed"))?;
            if total.insert(asset.to_string(), combined).is_some()
                || available.insert(asset.to_string(), free).is_some()
            {
                return Err(format!(
                    "exact spot account contains duplicate balance for {asset}"
                ));
            }
        }
        Ok(ExactSpotAccountBalances { total, available })
    }

    fn parse_exact_futures_positions(body: &str) -> Result<HashMap<String, ExactDecimal>, String> {
        let rows: Vec<Value> = serde_json::from_str(body)
            .map_err(|error| format!("invalid exact futures position JSON: {error}"))?;
        let mut positions = HashMap::<String, ExactDecimal>::new();
        let mut hedge_mode_gross = HashMap::<String, (ExactDecimal, ExactDecimal)>::new();
        for row in rows {
            let symbol = row
                .get("symbol")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| "exact futures position row is missing symbol".to_string())?
                .to_uppercase();
            let quantity = Self::parse_canonical_exchange_decimal(
                row.get("positionAmt"),
                &format!("futures position {symbol} positionAmt"),
                true,
            )?;
            let position_side = row
                .get("positionSide")
                .and_then(Value::as_str)
                .unwrap_or("BOTH")
                .trim()
                .to_ascii_uppercase();
            match position_side.as_str() {
                "LONG" => {
                    if quantity < ExactDecimal::ZERO {
                        return Err(format!(
                            "futures position {symbol} LONG row has negative positionAmt"
                        ));
                    }
                    let entry = hedge_mode_gross
                        .entry(symbol.clone())
                        .or_insert((ExactDecimal::ZERO, ExactDecimal::ZERO));
                    entry.0 = entry
                        .0
                        .checked_add(quantity)
                        .ok_or_else(|| "exact futures LONG quantity overflowed".to_string())?;
                }
                "SHORT" => {
                    if quantity > ExactDecimal::ZERO {
                        return Err(format!(
                            "futures position {symbol} SHORT row has positive positionAmt"
                        ));
                    }
                    let absolute = Self::exact_abs(quantity)
                        .ok_or_else(|| "exact futures SHORT quantity overflowed".to_string())?;
                    let entry = hedge_mode_gross
                        .entry(symbol.clone())
                        .or_insert((ExactDecimal::ZERO, ExactDecimal::ZERO));
                    entry.1 = entry
                        .1
                        .checked_add(absolute)
                        .ok_or_else(|| "exact futures SHORT quantity overflowed".to_string())?;
                }
                "BOTH" => {}
                _ => {
                    return Err(format!(
                        "futures position {symbol} has unsupported positionSide {position_side}"
                    ));
                }
            }
            let entry = positions.entry(symbol).or_insert(ExactDecimal::ZERO);
            *entry = entry
                .checked_add(quantity)
                .ok_or_else(|| "exact futures position sum overflowed".to_string())?;
        }
        for (symbol, (long_gross, short_gross)) in hedge_mode_gross {
            if long_gross > ExactDecimal::ZERO && short_gross > ExactDecimal::ZERO {
                return Err(format!(
                    "futures position {symbol} has simultaneous LONG and SHORT hedge-mode exposure"
                ));
            }
        }
        positions.retain(|_, quantity| *quantity != ExactDecimal::ZERO);
        Ok(positions)
    }

    fn normalized_account_decimal(
        node: Option<&Value>,
        field: &str,
        allow_negative: bool,
    ) -> Result<(ExactDecimal, String, f64), String> {
        let raw = node
            .and_then(Value::as_str)
            .ok_or_else(|| format!("{field} must be an exchange decimal string"))?;
        let exact = raw
            .parse::<ExactDecimal>()
            .map_err(|error| format!("{field} is not a supported decimal: {error}"))?;
        if !allow_negative && exact < ExactDecimal::ZERO {
            return Err(format!("{field} must be non-negative"));
        }
        let as_f64 = exact
            .to_f64()
            .filter(|value| value.is_finite())
            .ok_or_else(|| format!("{field} cannot be represented for risk comparison"))?;
        Ok((exact, exact.to_string(), as_f64))
    }

    fn account_position_key(symbol: &str, position_side: &str) -> String {
        format!(
            "{}:{}",
            symbol.trim().to_ascii_uppercase(),
            position_side.trim().to_ascii_uppercase()
        )
    }

    fn parse_standard_spot_account_truth(
        body: &str,
        open_orders: usize,
        observed_at_ms: i64,
    ) -> Result<StandardSpotAccountTruth, String> {
        let document: Value = serde_json::from_str(body)
            .map_err(|error| format!("invalid Standard Spot account JSON: {error}"))?;
        if document.get("canTrade").and_then(Value::as_bool) != Some(true) {
            return Err("Standard Spot account is not authorized to trade".to_string());
        }
        let permissions = document
            .get("permissions")
            .and_then(Value::as_array)
            .ok_or_else(|| "Standard Spot account is missing permissions".to_string())?;
        if !permissions
            .iter()
            .any(|value| value.as_str() == Some("SPOT"))
        {
            return Err("signed account is not a Standard Spot topology".to_string());
        }
        let balances = document
            .get("balances")
            .and_then(Value::as_array)
            .ok_or_else(|| "Standard Spot account is missing balances".to_string())?;
        let mut wallet_balance = HashMap::new();
        let mut available_balance = HashMap::new();
        for row in balances {
            let asset = row
                .get("asset")
                .and_then(Value::as_str)
                .map(str::trim)
                .filter(|value| !value.is_empty())
                .ok_or_else(|| "Standard Spot balance is missing asset".to_string())?;
            let (free, free_string, _) = Self::normalized_account_decimal(
                row.get("free"),
                &format!("Standard Spot {asset} available_balance"),
                false,
            )?;
            let (locked, _, _) = Self::normalized_account_decimal(
                row.get("locked"),
                &format!("Standard Spot {asset} locked balance"),
                false,
            )?;
            let total = free
                .checked_add(locked)
                .ok_or_else(|| format!("Standard Spot {asset} wallet_balance overflowed"))?;
            if wallet_balance
                .insert(asset.to_string(), total.to_string())
                .is_some()
                || available_balance
                    .insert(asset.to_string(), free_string)
                    .is_some()
            {
                return Err(format!("duplicate Standard Spot balance for {asset}"));
            }
        }
        Ok(StandardSpotAccountTruth {
            wallet_balance,
            available_balance,
            open_orders,
            borrow_state: "NOT_APPLICABLE_STANDARD_SPOT".to_string(),
            observed_at_ms,
        })
    }

    fn parse_usdm_account_risk_truth(
        account_body: &str,
        position_risk_body: &str,
        position_mode_body: &str,
        open_orders: usize,
        observed_at_ms: i64,
    ) -> Result<(UsdmAccountRiskTruth, f64), String> {
        let account: Value = serde_json::from_str(account_body)
            .map_err(|error| format!("invalid USD-M account JSON: {error}"))?;
        let (_, wallet_balance, _) = Self::normalized_account_decimal(
            account.get("totalWalletBalance"),
            "USD-M wallet_balance",
            false,
        )?;
        let (_, available_balance, _) = Self::normalized_account_decimal(
            account.get("availableBalance"),
            "USD-M available_balance",
            true,
        )?;
        let (_, maintenance_margin, maintenance_margin_f64) = Self::normalized_account_decimal(
            account.get("totalMaintMargin"),
            "USD-M maintenance_margin",
            false,
        )?;
        let (_, _, margin_balance_f64) = Self::normalized_account_decimal(
            account.get("totalMarginBalance"),
            "USD-M total margin balance",
            true,
        )?;
        let position_rows = account
            .get("positions")
            .and_then(Value::as_array)
            .ok_or_else(|| "USD-M account is missing positions".to_string())?;
        let risk_rows: Vec<Value> = serde_json::from_str(position_risk_body)
            .map_err(|error| format!("invalid USD-M position-risk JSON: {error}"))?;
        let position_mode_document: Value = serde_json::from_str(position_mode_body)
            .map_err(|error| format!("invalid USD-M position_mode JSON: {error}"))?;
        let position_mode = match position_mode_document
            .get("dualSidePosition")
            .and_then(Value::as_bool)
        {
            Some(true) => "HEDGE",
            Some(false) => "ONE_WAY",
            None => return Err("USD-M position_mode evidence is missing".to_string()),
        };
        let mut risk_by_key = HashMap::<String, String>::new();
        for row in risk_rows {
            let symbol = row
                .get("symbol")
                .and_then(Value::as_str)
                .ok_or_else(|| "USD-M position-risk row is missing symbol".to_string())?;
            let side = row
                .get("positionSide")
                .and_then(Value::as_str)
                .unwrap_or("BOTH")
                .to_ascii_uppercase();
            if !matches!(side.as_str(), "BOTH" | "LONG" | "SHORT") {
                return Err(format!(
                    "USD-M position-risk row has invalid positionSide {side}"
                ));
            }
            let (_, liquidation_price, _) = Self::normalized_account_decimal(
                row.get("liquidationPrice"),
                &format!("USD-M {symbol}:{side} liquidation_price"),
                false,
            )?;
            let key = Self::account_position_key(symbol, &side);
            if risk_by_key.insert(key.clone(), liquidation_price).is_some() {
                return Err(format!("duplicate USD-M position-risk row {key}"));
            }
        }

        let mut saw_both = false;
        let mut saw_hedge_side = false;
        let mut positions = HashMap::new();
        let mut liquidation_price = HashMap::new();
        for row in position_rows {
            let symbol = row
                .get("symbol")
                .and_then(Value::as_str)
                .ok_or_else(|| "USD-M account position is missing symbol".to_string())?;
            let side = row
                .get("positionSide")
                .and_then(Value::as_str)
                .unwrap_or("BOTH")
                .to_ascii_uppercase();
            match side.as_str() {
                "BOTH" => saw_both = true,
                "LONG" | "SHORT" => saw_hedge_side = true,
                _ => return Err(format!("USD-M account has invalid positionSide {side}")),
            }
            let (quantity, quantity_string, _) = Self::normalized_account_decimal(
                row.get("positionAmt"),
                &format!("USD-M {symbol}:{side} position amount"),
                true,
            )?;
            if quantity == ExactDecimal::ZERO {
                continue;
            }
            let leverage = row
                .get("leverage")
                .and_then(Value::as_str)
                .and_then(|value| value.parse::<u32>().ok())
                .filter(|value| *value > 0)
                .ok_or_else(|| format!("USD-M {symbol}:{side} leverage is invalid"))?;
            let key = Self::account_position_key(symbol, &side);
            let liquidate_at = risk_by_key
                .get(&key)
                .filter(|value| value.as_str() != "0")
                .cloned()
                .ok_or_else(|| format!("USD-M active position {key} lacks a liquidation_price"))?;
            liquidation_price.insert(key.clone(), liquidate_at.clone());
            positions.insert(
                key,
                UsdmPositionRiskTruth {
                    position_amount: quantity_string,
                    leverage,
                    liquidation_price: liquidate_at,
                },
            );
        }
        // Binance Demo may omit all zero-quantity rows for a completely flat
        // USD-M account. In that case the separately signed position-mode
        // endpoint is the authoritative topology evidence. Non-empty account
        // rows must still agree with it exactly.
        let mode_matches_rows = position_rows.is_empty()
            || match position_mode {
                "HEDGE" => saw_hedge_side && !saw_both,
                "ONE_WAY" => saw_both && !saw_hedge_side,
                _ => false,
            };
        if !mode_matches_rows {
            return Err("USD-M position_mode contradicts position rows".to_string());
        }
        let margin_ratio = if margin_balance_f64 > 0.0 {
            maintenance_margin_f64 / margin_balance_f64
        } else if maintenance_margin_f64 == 0.0 && positions.is_empty() {
            0.0
        } else {
            return Err("USD-M margin_ratio denominator is non-positive".to_string());
        };
        if !margin_ratio.is_finite() || margin_ratio < 0.0 {
            return Err("USD-M margin_ratio is invalid".to_string());
        }
        Ok((
            UsdmAccountRiskTruth {
                wallet_balance,
                available_balance,
                positions,
                maintenance_margin,
                margin_ratio,
                liquidation_price,
                position_mode: position_mode.to_string(),
                open_orders,
                observed_at_ms,
            },
            margin_balance_f64,
        ))
    }

    fn apply_signed_account_truth(
        &mut self,
        spot_body: &str,
        futures_account_body: &str,
        futures_position_risk_body: &str,
        futures_position_mode_body: &str,
        spot_open_orders: usize,
        futures_open_orders: usize,
    ) -> Result<(), String> {
        let observed_at_ms = Self::current_time_ms();
        let spot =
            Self::parse_standard_spot_account_truth(spot_body, spot_open_orders, observed_at_ms)?;
        let (usdm, margin_balance) = Self::parse_usdm_account_risk_truth(
            futures_account_body,
            futures_position_risk_body,
            futures_position_mode_body,
            futures_open_orders,
            observed_at_ms,
        )?;
        self.account_equity_usd = margin_balance;
        self.standard_spot_account_truth = Some(spot);
        self.usdm_account_truth = Some(usdm);
        if !self.persist_execution_state("signed Standard Spot and USD-M account truth") {
            return Err("signed account truth was not durably persisted".to_string());
        }
        Ok(())
    }

    fn local_signed_perp_quantity(position: &TrackedPosition) -> Result<f64, String> {
        let Some(perp) = position.perp.as_ref() else {
            return Ok(0.0);
        };
        match Self::side_is_long(&perp.side) {
            Some(true) => Ok(perp.quantity),
            Some(false) => Ok(-perp.quantity),
            None => Err(format!("unknown local perp side {}", perp.side)),
        }
    }

    fn futures_position_divergences(
        &self,
        exchange_positions: &HashMap<String, f64>,
    ) -> Vec<(String, &'static str, f64, f64)> {
        let mut symbols: HashSet<String> = self.tracked_positions.keys().cloned().collect();
        symbols.extend(exchange_positions.keys().cloned());
        let mut divergences = Vec::new();
        for symbol in symbols {
            let local_position = self.tracked_positions.get(&symbol);
            let local_qty = match local_position.map(Self::local_signed_perp_quantity) {
                Some(Ok(quantity)) => quantity,
                Some(Err(_)) => {
                    divergences.push((
                        symbol.clone(),
                        "invalid_local_side",
                        f64::NAN,
                        exchange_positions.get(&symbol).copied().unwrap_or(0.0),
                    ));
                    continue;
                }
                None => 0.0,
            };
            let exchange_qty = exchange_positions.get(&symbol).copied().unwrap_or(0.0);
            let tolerance = self
                .exchange_info
                .get(&symbol)
                .and_then(|info| info.futures_step_size.to_f64())
                .map(|step| step / 2.0)
                .unwrap_or(1e-8)
                .max(1e-8);
            let local_has_unpaired_spot = local_position
                .is_some_and(|position| position.spot.is_some() && position.perp.is_none());
            if local_has_unpaired_spot {
                divergences.push((symbol, "local_unpaired_spot", local_qty, exchange_qty));
            } else if (local_qty - exchange_qty).abs() > tolerance {
                let kind = if local_qty.abs() <= tolerance {
                    "exchange_only"
                } else if exchange_qty.abs() <= tolerance {
                    "local_only"
                } else {
                    "qty_or_side_mismatch"
                };
                divergences.push((symbol, kind, local_qty, exchange_qty));
            }
        }
        divergences.sort_by(|left, right| left.0.cmp(&right.0));
        divergences
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

    fn schedule_cycle_deadline(&self, cycle_client_order_id: String, deadline_at_ms: i64) {
        let remaining_ms = deadline_at_ms
            .saturating_sub(Self::current_time_ms())
            .max(1) as u64;
        let tx = self.engine_tx.clone();
        tokio::spawn(async move {
            sleep(Duration::from_millis(remaining_ms)).await;
            let _ = tx
                .send(EngineEvent::CycleDeadline {
                    cycle_client_order_id,
                    deadline_at_ms,
                })
                .await;
        });
    }

    fn arm_cycle_deadline(&mut self, symbol: &str) -> Result<i64, String> {
        let chase = self
            .chase_states
            .get(symbol)
            .cloned()
            .ok_or_else(|| format!("cannot arm deadline for missing chase {symbol}"))?;
        let cycle_id = chase.cycle_client_order_id().to_string();
        let (deadline_at_ms, newly_armed) = match self.cycle_deadlines.get(&cycle_id).copied() {
            Some(deadline) => (deadline, false),
            None => {
                let deadline = Self::current_time_ms().saturating_add(ENTRY_MAKER_TTL_MS);
                self.cycle_deadlines.insert(cycle_id.clone(), deadline);
                (deadline, true)
            }
        };
        if newly_armed
            && !self.persist_execution_state_for_symbol(
                symbol,
                "first exchange ACK armed absolute cycle deadline",
            )
        {
            return Err("absolute cycle deadline was not durable".to_string());
        }
        self.schedule_cycle_deadline(cycle_id, deadline_at_ms);
        Ok(deadline_at_ms)
    }

    fn rearm_recovered_cycle_deadlines(&mut self) {
        let now_ms = Self::current_time_ms();
        let recovered: Vec<(String, String)> = self
            .chase_states
            .values()
            .filter(|chase| {
                !matches!(
                    chase.phase,
                    ChasePhase::Completed | ChasePhase::ReconciliationRequired
                )
            })
            .map(|chase| {
                (
                    chase.symbol.to_ascii_uppercase(),
                    chase.cycle_client_order_id().to_string(),
                )
            })
            .collect();
        let mut migrated_missing_deadline = false;
        for (symbol, cycle_id) in recovered {
            let deadline_at_ms = match self.cycle_deadlines.get(&cycle_id).copied() {
                Some(deadline) => deadline,
                None => {
                    // Older snapshots cannot prove when their first ACK occurred.
                    // Expire them immediately instead of granting a fresh TTL.
                    warn!(
                        "Recovered active cycle {} for {} without an absolute deadline; expiring fail-closed",
                        cycle_id, symbol
                    );
                    self.cycle_deadlines.insert(cycle_id.clone(), now_ms);
                    migrated_missing_deadline = true;
                    now_ms
                }
            };
            self.schedule_cycle_deadline(cycle_id, deadline_at_ms);
        }
        if migrated_missing_deadline {
            let _ = self.persist_execution_state("migrated recovered chase to immediate deadline");
        }
    }

    fn classify_cycle_deadline(chase: &ChaseState) -> CycleDeadlineClassification {
        let Some(spot) = ExactDecimal::from_f64(chase.spot_cumulative_filled) else {
            return CycleDeadlineClassification::Unknown;
        };
        let Some(futures) = ExactDecimal::from_f64(chase.futures_cumulative_filled) else {
            return CycleDeadlineClassification::Unknown;
        };
        if spot < ExactDecimal::ZERO || futures < ExactDecimal::ZERO {
            CycleDeadlineClassification::Unknown
        } else if spot == ExactDecimal::ZERO && futures == ExactDecimal::ZERO {
            CycleDeadlineClassification::Flat
        } else if spot == futures {
            CycleDeadlineClassification::EqualPartial
        } else {
            CycleDeadlineClassification::Divergent
        }
    }

    fn record_cycle_deadline_classification(
        &mut self,
        chase: &ChaseState,
        classification: CycleDeadlineClassification,
    ) -> bool {
        let cycle_id = chase.cycle_client_order_id().to_string();
        let deadline_at_ms = self
            .cycle_deadlines
            .get(&cycle_id)
            .copied()
            .unwrap_or_else(Self::current_time_ms);
        self.cycle_deadline_records.insert(
            cycle_id.clone(),
            CycleDeadlineRecord {
                symbol: chase.symbol.to_ascii_uppercase(),
                cycle_client_order_id: cycle_id,
                classification,
                spot_cumulative_filled: chase.spot_cumulative_filled,
                futures_cumulative_filled: chase.futures_cumulative_filled,
                is_exit: chase.is_exit,
                deadline_at_ms,
                classified_at_ms: Self::current_time_ms(),
            },
        );
        self.persist_execution_state_for_symbol(&chase.symbol, "absolute cycle deadline classified")
    }

    fn require_cycle_deadline_reconciliation(
        &mut self,
        symbol: &str,
        chase: ChaseState,
        client_order_id: &str,
        reason: &'static str,
    ) {
        let _ =
            self.record_cycle_deadline_classification(&chase, CycleDeadlineClassification::Unknown);
        self.require_chase_reconciliation(symbol, chase, client_order_id, reason);
    }

    fn current_time_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }

    fn market_event_time_is_current(
        exchange_event_time_ms: Option<i64>,
        receive_time_ms: i64,
        allow_missing_exchange_time: bool,
    ) -> bool {
        let now_ms = Self::current_time_ms();
        let receive_is_current = receive_time_ms > 0
            && receive_time_ms >= now_ms - MAX_EXECUTION_MARKET_EVENT_AGE_MS
            && receive_time_ms <= now_ms + MAX_EXECUTION_MARKET_FUTURE_SKEW_MS;
        if !receive_is_current {
            return false;
        }
        let Some(exchange_event_time_ms) = exchange_event_time_ms else {
            return allow_missing_exchange_time;
        };
        exchange_event_time_ms > 0
            && exchange_event_time_ms >= now_ms - MAX_EXECUTION_MARKET_EVENT_AGE_MS
            && exchange_event_time_ms <= now_ms + MAX_EXECUTION_MARKET_FUTURE_SKEW_MS
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
            .send(EngineEvent::Ws(Box::new(WsEvent::OrderUpdate {
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
                connection_id: Some("paper-simulator".to_string()),
                exchange_event_time_ms: None,
                receive_time_ms: Some(Self::current_time_ms()),
                process_time_ms: Some(Self::current_time_ms()),
                persist_time_ms: None,
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
            })))
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

    fn exact_live_value(value: f64) -> Option<ExactDecimal> {
        (value.is_finite() && value > 0.0)
            .then(|| ExactDecimal::from_f64(value))
            .flatten()
    }

    fn canonical_exact(raw: Option<&str>) -> Option<ExactDecimal> {
        let raw = raw?;
        let value = raw.parse::<ExactDecimal>().ok()?;
        (value >= ExactDecimal::ZERO && value.to_string() == raw).then_some(value)
    }

    fn canonical_signed_exact(raw: Option<&str>) -> Option<ExactDecimal> {
        let raw = raw?;
        let value = raw.parse::<ExactDecimal>().ok()?;
        (value.to_string() == raw).then_some(value)
    }

    fn exact_abs(value: ExactDecimal) -> Option<ExactDecimal> {
        if value >= ExactDecimal::ZERO {
            Some(value)
        } else {
            ExactDecimal::ZERO.checked_sub(value)
        }
    }

    fn exact_min(values: impl IntoIterator<Item = ExactDecimal>) -> ExactDecimal {
        values.into_iter().min().unwrap_or(ExactDecimal::ZERO)
    }

    fn round_down_to_step(value: f64, step: ExactDecimal) -> Option<ExactDecimal> {
        Self::exact_live_value(value)?.floor_to_increment(step)
    }

    fn quantize_price(
        value: ExactDecimal,
        tick_size: ExactDecimal,
        side: TradeSide,
    ) -> Option<ExactDecimal> {
        match side {
            TradeSide::Buy => value.floor_to_increment(tick_size),
            TradeSide::Sell => value.ceil_to_increment(tick_size),
        }
    }

    fn exact_notional(quantity: f64, price: f64) -> Option<ExactDecimal> {
        Self::exact_live_value(quantity)?.checked_mul(Self::exact_live_value(price)?)
    }

    fn symbol_info(&self, symbol: &str) -> crate::binance_rest::ExchangeSymbolInfo {
        self.exchange_info
            .get(symbol)
            .cloned()
            .unwrap_or(crate::binance_rest::ExchangeSymbolInfo {
                symbol: symbol.to_string(),
                spot_tick_size: "0.1".parse().expect("static decimal"),
                spot_min_price: ExactDecimal::ZERO,
                spot_max_price: ExactDecimal::MAX,
                spot_min_qty: "0.1".parse().expect("static decimal"),
                spot_step_size: "0.1".parse().expect("static decimal"),
                spot_max_qty: ExactDecimal::MAX,
                spot_market_min_qty: "0.1".parse().expect("static decimal"),
                spot_market_step_size: "0.1".parse().expect("static decimal"),
                spot_market_max_qty: ExactDecimal::MAX,
                spot_min_notional: ExactDecimal::from_integer(5),
                spot_max_notional: None,
                spot_min_notional_apply_to_market: true,
                spot_max_notional_apply_to_market: false,
                futures_tick_size: "0.1".parse().expect("static decimal"),
                futures_min_price: ExactDecimal::ZERO,
                futures_max_price: ExactDecimal::MAX,
                futures_min_qty: "0.1".parse().expect("static decimal"),
                futures_step_size: "0.1".parse().expect("static decimal"),
                futures_max_qty: ExactDecimal::MAX,
                futures_market_min_qty: "0.1".parse().expect("static decimal"),
                futures_market_step_size: "0.1".parse().expect("static decimal"),
                futures_market_max_qty: ExactDecimal::MAX,
                futures_min_notional: ExactDecimal::from_integer(5),
                futures_max_notional: None,
                futures_min_notional_apply_to_market: true,
                futures_max_notional_apply_to_market: false,
            })
    }

    fn normalize_quantity_for_market(
        &self,
        symbol: &str,
        market: MarketType,
        requested_quantity: f64,
    ) -> Option<f64> {
        let info = self.symbol_info(symbol);
        let (step_size, minimum, maximum) = match market {
            MarketType::Spot => (
                info.spot_step_size.max(info.spot_market_step_size),
                info.spot_min_qty.max(info.spot_market_min_qty),
                info.spot_max_qty.min(info.spot_market_max_qty),
            ),
            MarketType::Perp => (
                info.futures_step_size.max(info.futures_market_step_size),
                info.futures_min_qty.max(info.futures_market_min_qty),
                info.futures_max_qty.min(info.futures_market_max_qty),
            ),
        };
        let normalized = Self::round_down_to_step(requested_quantity, step_size)?;
        if normalized >= minimum && normalized <= maximum {
            normalized.to_f64()
        } else {
            None
        }
    }

    fn normalize_exact_quantity_for_market(
        &self,
        symbol: &str,
        market: MarketType,
        requested_quantity: ExactDecimal,
    ) -> Option<ExactDecimal> {
        let info = self.symbol_info(symbol);
        let (lot_increment, market_increment, minimum, maximum) = match market {
            MarketType::Spot => (
                info.spot_step_size,
                info.spot_market_step_size,
                info.spot_min_qty.max(info.spot_market_min_qty),
                info.spot_max_qty.min(info.spot_market_max_qty),
            ),
            MarketType::Perp => (
                info.futures_step_size,
                info.futures_market_step_size,
                info.futures_min_qty.max(info.futures_market_min_qty),
                info.futures_max_qty.min(info.futures_market_max_qty),
            ),
        };
        let increment = lot_increment.checked_common_increment(market_increment)?;
        let normalized = requested_quantity.floor_to_increment(increment)?;
        (normalized >= minimum && normalized <= maximum).then_some(normalized)
    }

    fn normalize_common_entry_quantity(
        &self,
        symbol: &str,
        requested_quantity: f64,
    ) -> Option<f64> {
        let info = self.symbol_info(symbol);
        let spot_increment = info
            .spot_step_size
            .checked_common_increment(info.spot_market_step_size)?;
        let futures_increment = info
            .futures_step_size
            .checked_common_increment(info.futures_market_step_size)?;
        let common_increment = spot_increment.checked_common_increment(futures_increment)?;
        let minimum = info
            .spot_min_qty
            .max(info.spot_market_min_qty)
            .max(info.futures_min_qty)
            .max(info.futures_market_min_qty);
        let maximum = info
            .spot_max_qty
            .min(info.spot_market_max_qty)
            .min(info.futures_max_qty)
            .min(info.futures_market_max_qty);
        let normalized = Self::round_down_to_step(requested_quantity, common_increment)?;
        if normalized >= minimum && normalized <= maximum {
            normalized.to_f64()
        } else {
            None
        }
    }

    fn format_quantity_for_market(
        &self,
        symbol: &str,
        market: MarketType,
        quantity: f64,
        market_order: bool,
    ) -> Option<String> {
        let info = self.symbol_info(symbol);
        let (step_size, minimum, maximum) = match (market, market_order) {
            (MarketType::Spot, false) => {
                (info.spot_step_size, info.spot_min_qty, info.spot_max_qty)
            }
            (MarketType::Spot, true) => (
                info.spot_market_step_size,
                info.spot_market_min_qty,
                info.spot_market_max_qty,
            ),
            (MarketType::Perp, false) => (
                info.futures_step_size,
                info.futures_min_qty,
                info.futures_max_qty,
            ),
            (MarketType::Perp, true) => (
                info.futures_market_step_size,
                info.futures_market_min_qty,
                info.futures_market_max_qty,
            ),
        };
        let normalized = Self::round_down_to_step(quantity, step_size)?;
        if normalized < minimum || normalized > maximum {
            return None;
        }
        normalized.format_to_scale(step_size.scale())
    }

    fn format_market_quantity_with_price(
        &self,
        symbol: &str,
        market: MarketType,
        quantity: f64,
        reference_price: f64,
    ) -> Option<String> {
        let formatted = self.format_quantity_for_market(symbol, market, quantity, true)?;
        let quantity = formatted.parse::<ExactDecimal>().ok()?;
        let price = Self::exact_live_value(reference_price)?;
        let notional = quantity.checked_mul(price)?;
        let info = self.symbol_info(symbol);
        let (minimum, maximum) = match market {
            MarketType::Spot => (
                info.spot_min_notional_apply_to_market
                    .then_some(info.spot_min_notional),
                info.spot_max_notional_apply_to_market
                    .then_some(info.spot_max_notional)
                    .flatten(),
            ),
            MarketType::Perp => (
                info.futures_min_notional_apply_to_market
                    .then_some(info.futures_min_notional),
                info.futures_max_notional_apply_to_market
                    .then_some(info.futures_max_notional)
                    .flatten(),
            ),
        };
        if minimum.is_some_and(|minimum| notional < minimum)
            || maximum.is_some_and(|maximum| notional > maximum)
        {
            None
        } else {
            Some(formatted)
        }
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

    pub fn private_recovery_symbols(&self) -> HashSet<String> {
        let mut symbols: HashSet<String> = self
            .tracked_positions
            .keys()
            .chain(self.chase_states.keys())
            .map(|symbol| symbol.to_uppercase())
            .collect();
        symbols.extend(
            self.internal_orders
                .values()
                .filter(|order| !is_terminal_internal_status(&order.status))
                .map(|order| order.symbol.to_uppercase()),
        );
        symbols
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

    fn apply_commission_once(
        &mut self,
        observation: CommissionObservation<'_>,
    ) -> Result<(), &'static str> {
        let CommissionObservation {
            symbol,
            client_order_id,
            market,
            amount,
            asset: commission_asset,
            order_id,
            trade_id,
        } = observation;
        let Some(commission_exact) = ExactDecimal::from_f64(amount) else {
            return Err("INVALID_COMMISSION_AMOUNT");
        };
        if commission_exact < ExactDecimal::ZERO {
            return Err("INVALID_COMMISSION_AMOUNT");
        }
        if commission_exact == ExactDecimal::ZERO {
            return Ok(());
        }
        let asset = commission_asset.trim().to_ascii_uppercase();
        if asset.is_empty() {
            return Err("COMMISSION_ASSET_MISSING");
        }
        let (Some(order_id), Some(trade_id)) = (order_id, trade_id) else {
            return Err("COMMISSION_IDENTITY_MISSING");
        };
        if order_id < 0 || trade_id < 0 {
            return Err("COMMISSION_IDENTITY_INVALID");
        }
        if client_order_id.trim().is_empty() {
            return Err("COMMISSION_CLIENT_ORDER_ID_MISSING");
        }
        let venue = match market {
            MarketType::Spot => "spot",
            MarketType::Perp => "futures",
        };
        let sym_upper = symbol.to_ascii_uppercase();
        let identity = format!("{venue}:{sym_upper}:{order_id}:{trade_id}");
        let fingerprint = format!("{asset}:{}", commission_exact);
        if let Some(existing) = self.applied_commission_keys.get(&identity) {
            return if existing == &fingerprint {
                Ok(())
            } else {
                Err("COMMISSION_IDENTITY_CONFLICT")
            };
        }
        let cycle_client_order_id = self
            .chase_states
            .values()
            .find(|chase| chase.leg_for_client_order_id(client_order_id).is_some())
            .map(|chase| chase.cycle_client_order_id().to_string())
            .unwrap_or_else(|| client_order_id.to_string());

        let base_asset =
            Self::base_asset_for_symbol(&sym_upper).ok_or("COMMISSION_SYMBOL_ASSET_UNKNOWN")?;
        let quote_asset =
            Self::quote_asset_for_symbol(&sym_upper).ok_or("COMMISSION_SYMBOL_ASSET_UNKNOWN")?;
        let asset_is_known = asset == base_asset
            || asset == quote_asset
            || matches!(asset.as_str(), "BNB" | "USDT" | "USDC" | "FDUSD");
        if !asset_is_known {
            self.unvalued_commission_assets.insert(asset);
            self.applied_commission_keys
                .insert(identity.clone(), fingerprint);
            self.commission_cycles
                .insert(identity, cycle_client_order_id);
            return Err("UNKNOWN_COMMISSION_ASSET");
        }

        if market == MarketType::Spot && asset == base_asset {
            let mut exhausted_inventory = false;
            let remove_symbol = {
                let position = self
                    .tracked_positions
                    .get_mut(&sym_upper)
                    .ok_or("BASE_COMMISSION_WITHOUT_POSITION")?;
                let spot = position
                    .spot
                    .as_mut()
                    .ok_or("BASE_COMMISSION_WITHOUT_SPOT_INVENTORY")?;
                let current = ExactDecimal::from_f64(spot.quantity)
                    .ok_or("INVALID_TRACKED_SPOT_INVENTORY")?;
                let remaining = current
                    .checked_sub(commission_exact)
                    .ok_or("COMMISSION_INVENTORY_ARITHMETIC_OVERFLOW")?;
                if remaining < ExactDecimal::ZERO {
                    return Err("BASE_COMMISSION_EXCEEDS_SPOT_INVENTORY");
                }
                if remaining == ExactDecimal::ZERO {
                    position.spot = None;
                    exhausted_inventory = true;
                } else {
                    spot.quantity = remaining
                        .to_f64()
                        .ok_or("COMMISSION_INVENTORY_CONVERSION_OVERFLOW")?;
                    Self::refresh_leg_pnl(spot);
                }
                position.spot.is_none() && position.perp.is_none()
            };
            if remove_symbol {
                self.tracked_positions.remove(&sym_upper);
            }
            self.recompute_gross_exposure();
            self.emit_position_snapshot(&sym_upper);
            if exhausted_inventory {
                self.applied_commission_keys
                    .insert(identity.clone(), fingerprint);
                self.commission_cycles
                    .insert(identity, cycle_client_order_id);
                return Err("BASE_COMMISSION_EXHAUSTED_SPOT_INVENTORY");
            }
        }

        self.applied_commission_keys
            .insert(identity.clone(), fingerprint);
        self.commission_cycles
            .insert(identity, cycle_client_order_id);
        Ok(())
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

    fn chase_pending_entry_gross_usd(&self, chase: &ChaseState) -> f64 {
        if chase.is_exit || chase.phase == ChasePhase::Completed {
            return 0.0;
        }
        let conservative_price = self.active_per_symbol_notional_cap_usd()
            / chase.spot_quantity.max(chase.perp_quantity).max(1e-12);
        let spot_price = if chase.expected_spot_price.is_finite() && chase.expected_spot_price > 0.0
        {
            chase.expected_spot_price
        } else {
            self.spot_mid_cache
                .get(&chase.symbol)
                .copied()
                .filter(|price| price.is_finite() && *price > 0.0)
                .unwrap_or(conservative_price)
        };
        let perp_price = if chase.expected_fut_price.is_finite() && chase.expected_fut_price > 0.0 {
            chase.expected_fut_price
        } else {
            self.perp_mid_cache
                .get(&chase.symbol)
                .copied()
                .filter(|price| price.is_finite() && *price > 0.0)
                .unwrap_or(conservative_price)
        };
        let spot_remaining = if chase.has_spot_leg() {
            (chase.spot_quantity - chase.spot_cumulative_filled).max(0.0)
        } else {
            0.0
        };
        let perp_remaining = if chase.has_futures_leg() {
            (chase.perp_quantity - chase.futures_cumulative_filled).max(0.0)
        } else {
            0.0
        };
        spot_remaining * spot_price + perp_remaining * perp_price
    }

    fn pending_entry_reserved_gross_usd(&self, exclude_symbol: Option<&str>) -> f64 {
        self.chase_states
            .iter()
            .filter(|(symbol, _)| {
                exclude_symbol.is_none_or(|excluded| !symbol.eq_ignore_ascii_case(excluded))
            })
            .map(|(_, chase)| self.chase_pending_entry_gross_usd(chase))
            .sum()
    }

    fn chase_pending_spot_collateral_usd(&self, chase: &ChaseState) -> f64 {
        if chase.is_exit
            || chase.phase == ChasePhase::Completed
            || !chase.has_spot_leg()
            || chase.spot_side != TradeSide::Buy
        {
            return 0.0;
        }
        let conservative_price =
            self.active_per_symbol_notional_cap_usd() / chase.spot_quantity.max(1e-12);
        let price = if chase.expected_spot_price.is_finite() && chase.expected_spot_price > 0.0 {
            chase.expected_spot_price
        } else {
            self.spot_mid_cache
                .get(&chase.symbol)
                .copied()
                .filter(|value| value.is_finite() && *value > 0.0)
                .unwrap_or(conservative_price)
        };
        // Reserve the complete target until the durable chase terminates. A
        // fill and its outboundAccountPosition balance update can arrive in
        // either order; releasing only the filled slice here would briefly
        // allow another entry to spend a stale free-balance observation.
        chase.spot_quantity * price
    }

    fn pending_spot_collateral_reserved_usd(
        &self,
        quote_asset: &str,
        exclude_symbol: Option<&str>,
    ) -> f64 {
        self.chase_states
            .iter()
            .filter(|(symbol, chase)| {
                exclude_symbol.is_none_or(|excluded| !symbol.eq_ignore_ascii_case(excluded))
                    && Self::quote_asset_for_symbol(&chase.symbol)
                        .is_some_and(|quote| quote.eq_ignore_ascii_case(quote_asset))
            })
            .map(|(_, chase)| self.chase_pending_spot_collateral_usd(chase))
            .sum()
    }

    fn spot_collateral_available_for_entry(
        &self,
        symbol: &str,
        required_quote: f64,
        exclude_symbol: Option<&str>,
    ) -> bool {
        let Some(quote_asset) = Self::quote_asset_for_symbol(symbol) else {
            return false;
        };
        let available = self
            .spot_available_balances
            .get(quote_asset)
            .copied()
            .filter(|value| value.is_finite() && *value >= 0.0)
            .unwrap_or(0.0);
        let reserved = self.pending_spot_collateral_reserved_usd(quote_asset, exclude_symbol);
        required_quote.is_finite()
            && required_quote > 0.0
            && reserved.is_finite()
            && reserved + required_quote <= available + 1e-9
    }

    fn tracked_symbol_leg_notionals_usd(&self, symbol: &str) -> (f64, f64) {
        let Some(position) = self.tracked_positions.get(&symbol.to_uppercase()) else {
            return (0.0, 0.0);
        };
        let notional = |leg: Option<&TrackedLegPosition>| {
            leg.map(|value| value.quantity * value.last_mark_price)
                .filter(|value| value.is_finite() && *value >= 0.0)
                .unwrap_or(f64::INFINITY)
        };
        (
            notional(position.spot.as_ref()),
            notional(position.perp.as_ref()),
        )
    }

    fn risk_bearing_symbol_count(&self, exclude_symbol: Option<&str>) -> usize {
        let mut symbols: HashSet<String> = self
            .tracked_positions
            .iter()
            .filter(|(_, position)| position.spot.is_some() || position.perp.is_some())
            .map(|(symbol, _)| symbol.to_uppercase())
            .collect();
        for (symbol, chase) in &self.chase_states {
            if !chase.is_exit && chase.phase != ChasePhase::Completed {
                symbols.insert(symbol.to_uppercase());
            }
        }
        if let Some(excluded) = exclude_symbol {
            symbols.remove(&excluded.to_uppercase());
        }
        symbols.len()
    }

    fn replacement_client_order_id(&self, chase: &ChaseState, leg: Leg) -> String {
        let logical_intent = self
            .chase_intent_ids
            .get(&chase.symbol)
            .map(String::as_str)
            .unwrap_or_else(|| chase.cycle_client_order_id());
        let generation = chase.aliases_for(leg).len().saturating_add(1);
        let leg_label = match leg {
            Leg::Spot => "s",
            Leg::Futures => "p",
        };
        let mut digest = Sha256::new();
        digest.update(format!("{logical_intent}:{leg_label}:repair:{generation}").as_bytes());
        format!(
            "bngs_r_{}_{}",
            leg_label,
            &hex::encode(digest.finalize())[..24]
        )
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
                    let capacity_exhausted = err.contains("byte budget exceeded");
                    let volatile_reason = if capacity_exhausted {
                        "intent_journal_survival_reserve_exhausted"
                    } else {
                        "intent_journal_unavailable"
                    };
                    if !capacity_exhausted {
                        self.intent_journal_error = Some(err);
                        self.intent_journal = None;
                    } else {
                        self.state = SystemState::Reconciling;
                    }
                    if self.activate_volatile_storage_barrier_without_journal(
                        &instruction,
                        volatile_reason,
                        now_ms,
                    ) {
                        return;
                    }
                    self.emit_config_ack(&ConfigAck::rejected(
                        &instruction,
                        active_hash.as_deref(),
                        volatile_reason,
                        now_ms,
                        false,
                    ));
                    return;
                }
            },
            None => {
                if self.activate_volatile_storage_barrier_without_journal(
                    &instruction,
                    "intent_journal_unavailable",
                    now_ms,
                ) {
                    return;
                }
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
            if self.activate_volatile_storage_barrier_without_journal(
                &instruction,
                "intent_journal_validation_not_durable",
                now_ms,
            ) {
                return;
            }
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
        let storage_outcome = match self.apply_storage_control(snapshot.storage_control.as_ref()) {
            Ok(outcome) => outcome,
            Err(reason) => {
                self.config_consensus = previous_consensus;
                self.max_gross_exposure_usd = previous_max_gross;
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
        let emergency_update = snapshot
            .storage_control
            .as_ref()
            .is_some_and(|control| control.emergency_latched);
        if emergency_update {
            self.halt_entry_chases_for_storage_latch();
        }

        let terminal_reason = match storage_outcome {
            StorageControlApplyOutcome::Applied => "config_applied",
            StorageControlApplyOutcome::VolatileLatched => "storage_control_volatile_latched",
        };
        if !self.transition_intent_ack(&intent_id, "TERMINAL", terminal_reason) {
            if emergency_update {
                // Even if the intent journal's final transition also runs out
                // of space, this actor has crossed the FIFO boundary and the
                // in-memory latch is already active. Keep the config paused
                // and publish a non-terminal volatile barrier so Python can
                // cancel exchange entries while retaining the command for
                // replay after restart.
                self.storage_control_error =
                    Some("storage-control latch lacks durable terminal config ACK".to_string());
                self.storage_emergency_latched = true;
                self.storage_control_volatile_latched = true;
                self.state = SystemState::Reconciling;
                self.emit_config_ack(&ConfigAck::volatile_latched(
                    &instruction,
                    &snapshot,
                    "storage_control_terminal_ack_not_durable",
                    Self::current_time_ms(),
                    replay,
                    "VALIDATED",
                ));
                return;
            }
            self.config_consensus = previous_consensus;
            self.max_gross_exposure_usd = previous_max_gross;
            if snapshot.storage_control.is_some() {
                // The control checkpoint may already have reached durable
                // storage. Until Python replays and observes its terminal ACK,
                // keep the process entry gate closed rather than guessing.
                self.storage_control_error =
                    Some("storage-control applied without durable terminal config ACK".to_string());
                self.storage_emergency_latched = true;
            }
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
            "Applied CONFIG_SYNC {} (pause_new_entries={}, per_symbol_cap=${}, max_gross=${}, storage_generation={}, storage_emergency_latched={})",
            snapshot.config_hash,
            snapshot.pause_new_entries,
            snapshot.per_symbol_notional_cap_usd,
            self.max_gross_exposure_usd,
            self.storage_control_generation,
            self.storage_emergency_latched,
        );
        match storage_outcome {
            StorageControlApplyOutcome::Applied => self.emit_config_ack(&ConfigAck::applied(
                &instruction,
                &snapshot,
                Self::current_time_ms(),
                replay,
            )),
            StorageControlApplyOutcome::VolatileLatched => {
                self.emit_config_ack(&ConfigAck::volatile_latched(
                    &instruction,
                    &snapshot,
                    "storage_control_checkpoint_not_durable",
                    Self::current_time_ms(),
                    replay,
                    "TERMINAL",
                ));
            }
        }
    }

    /// Arm the independent in-memory entry gate after a valid emergency
    /// CONFIG_SYNC crosses this actor's FIFO boundary, even when the intent
    /// journal itself cannot accept or transition the receipt.  The ACK is
    /// deliberately VOLATILE_LATCHED: it authorizes Python cancellation and
    /// reconciliation only, never recovery or new risk.
    fn activate_volatile_storage_barrier_without_journal(
        &mut self,
        instruction: &crate::ipc::AlphaInstruction,
        reason: &str,
        now_ms: i64,
    ) -> bool {
        let snapshot = match ConfigConsensus::validate(instruction) {
            Ok(snapshot) => snapshot,
            Err(_) => return false,
        };
        let Some(update) = snapshot.storage_control.as_ref() else {
            return false;
        };
        if !snapshot.pause_new_entries || !update.emergency_latched {
            return false;
        }
        if update.generation < self.storage_control_generation
            || (update.generation == self.storage_control_generation
                && !self.storage_emergency_latched)
        {
            return false;
        }

        // Persist the dedicated checkpoint if that smaller write still works,
        // but retain volatile status because the command receipt/transition is
        // not durable. A later replay or a newer acknowledged recovery is
        // required to leave this state.
        if self.apply_storage_control(Some(update)).is_err() && !self.storage_emergency_latched {
            return false;
        }
        self.storage_control_generation = self.storage_control_generation.max(update.generation);
        self.storage_emergency_latched = true;
        self.storage_control_volatile_latched = true;
        self.storage_control_error = Some(format!(
            "storage emergency crossed FIFO without durable intent journal: {reason}"
        ));
        self.state = SystemState::Reconciling;
        self.halt_entry_chases_for_storage_latch();
        self.emit_config_ack(&ConfigAck::volatile_latched(
            instruction,
            &snapshot,
            reason,
            now_ms,
            false,
            "VALIDATED",
        ));
        true
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
        let terminal_status = if status == "FILLED" && execution_type == "FILLED_CYCLE" {
            Some(("TERMINAL", "filled_cycle"))
        } else if matches!(
            status,
            "REJECTED" | "CANCELED" | "CANCELLED" | "EXPIRED" | "EXPIRED_IN_MATCH"
        ) {
            Some(("REJECTED", execution_type))
        } else {
            None
        };
        let cycle_id = chase.cycle_client_order_id().to_string();
        let lineage = self.order_lineage.get(resolved_client_order_id).cloned();
        let exact = |value: f64| ExactDecimal::from_f64(value).map(|item| item.to_string());
        let generation_statuses = |aliases: &[String]| {
            aliases
                .iter()
                .map(|client_id| {
                    serde_json::json!({
                        "client_order_id": client_id,
                        "status": self.internal_orders.get(client_id).map(|order| order.status.clone()).unwrap_or_else(|| "NOT_SUBMITTED".to_string()),
                    })
                })
                .collect::<Vec<_>>()
        };
        let mut commission_rows = self
            .commission_cycles
            .iter()
            .filter(|(_, observed_cycle)| *observed_cycle == &cycle_id)
            .filter_map(|(identity, _)| {
                let fingerprint = self.applied_commission_keys.get(identity)?;
                let (asset, amount) = fingerprint.split_once(':')?;
                Some(serde_json::json!({
                    "identity": identity,
                    "asset": asset,
                    "amount": amount,
                }))
            })
            .collect::<Vec<_>>();
        commission_rows.sort_by(|left, right| {
            left.get("identity")
                .and_then(Value::as_str)
                .cmp(&right.get("identity").and_then(Value::as_str))
        });
        let mut commission_assets = commission_rows
            .iter()
            .filter_map(|row| row.get("asset").and_then(Value::as_str).map(str::to_string))
            .collect::<Vec<_>>();
        commission_assets.sort();
        commission_assets.dedup();
        let unvalued_assets = commission_assets
            .iter()
            .filter(|asset| self.unvalued_commission_assets.contains(*asset))
            .cloned()
            .collect::<Vec<_>>();
        let post_position = self
            .tracked_positions
            .get(&chase.symbol.to_ascii_uppercase());
        let actual_spot_inventory = post_position
            .and_then(|position| position.spot.as_ref())
            .map(|leg| leg.quantity)
            .or_else(|| (chase.is_exit || chase.spot_cumulative_filled == 0.0).then_some(0.0));
        let actual_futures_inventory = post_position
            .and_then(|position| position.perp.as_ref())
            .map(|leg| leg.quantity)
            .or_else(|| (chase.is_exit || chase.futures_cumulative_filled == 0.0).then_some(0.0));
        let now_ms = Self::current_time_ms();
        let deadline_classification = self
            .cycle_deadline_records
            .get(&cycle_id)
            .map(|record| record.classification);
        let cycle_fill_price = chase.cycle_fill_price();
        let spot_vwap_decimal = (chase.spot_cumulative_filled > 0.0)
            .then(|| chase.spot_fill_price.and_then(exact))
            .flatten();
        let futures_vwap_decimal = (chase.futures_cumulative_filled > 0.0)
            .then(|| chase.futures_fill_price.and_then(exact))
            .flatten();
        let actual_spot_inventory_decimal = actual_spot_inventory
            .and_then(exact)
            .unwrap_or_else(|| "0".to_string());
        let actual_futures_inventory_decimal = actual_futures_inventory
            .and_then(exact)
            .unwrap_or_else(|| "0".to_string());
        let spot_final_status = if chase.skip_spot_leg {
            "NOT_SUBMITTED".to_string()
        } else {
            self.internal_orders
                .get(&chase.spot_client_order_id)
                .map(|order| order.status.clone())
                .unwrap_or_else(|| {
                    if chase.spot_terminal {
                        "FILLED"
                    } else {
                        "UNKNOWN"
                    }
                    .to_string()
                })
        };
        let futures_final_status = if chase.skip_perp_leg {
            "NOT_SUBMITTED".to_string()
        } else {
            self.internal_orders
                .get(&chase.futures_client_order_id)
                .map(|order| order.status.clone())
                .unwrap_or_else(|| {
                    if chase.futures_terminal {
                        "FILLED"
                    } else {
                        "UNKNOWN"
                    }
                    .to_string()
                })
        };
        let mut cycle_event = serde_json::json!({
            "event": "OrderUpdate",
            "schema_version": crate::ipc::EXECUTION_PROTOCOL_VERSION,
            "terminal_summary_version": crate::ipc::EXECUTION_PROTOCOL_VERSION,
            "symbol": &chase.symbol,
            "status": status,
            "filled_qty": filled_qty,
            "filled_qty_decimal": exact(filled_qty),
            "client_order_id": resolved_client_order_id,
            "avg_fill_price": cycle_fill_price,
            "last_fill_price": cycle_fill_price,
            "cumulative_quote_qty": serde_json::Value::Null,
            "commission": serde_json::Value::Null,
            "commission_asset": serde_json::Value::Null,
            "commissions": commission_rows,
            "commission_assets": commission_assets,
            "commission_status": if unvalued_assets.is_empty() { "VALUED_OR_ZERO" } else { "UNKNOWN" },
            "unvalued_commission_assets": unvalued_assets,
            "realized_pnl": serde_json::Value::Null,
            "maker": maker,
            "execution_type": execution_type,
            "event_time_ms": now_ms,
            "exchange_event_time_ms": chase.last_exchange_event_time_ms.unwrap_or(now_ms),
            "receive_time_ms": chase.last_receive_time_ms.unwrap_or(now_ms),
            "process_time_ms": now_ms,
            "persist_time_ms": chase.last_persist_time_ms.unwrap_or(now_ms),
            "spot_fill_price": chase.spot_fill_price.unwrap_or(chase.expected_spot_price),
            "perp_fill_price": chase.futures_fill_price.unwrap_or(chase.expected_fut_price),
            "requested_quantity_decimal": &chase.requested_quantity_decimal,
            "risk_adjusted_requested_quantity_decimal": lineage.as_ref().and_then(|v| v.risk_adjusted_requested_quantity_decimal.clone()),
            "normalized_common_entry_quantity_decimal": &chase.normalized_common_entry_quantity_decimal,
            "spot_target_quantity_decimal": exact(chase.spot_quantity),
            "futures_target_quantity_decimal": exact(chase.perp_quantity),
            "spot_cumulative_filled_quantity_decimal": exact(chase.spot_cumulative_filled).unwrap_or_else(|| "0".to_string()),
            "futures_cumulative_filled_quantity_decimal": exact(chase.futures_cumulative_filled).unwrap_or_else(|| "0".to_string()),
            "spot_vwap_decimal": spot_vwap_decimal,
            "futures_vwap_decimal": futures_vwap_decimal,
            "actual_spot_inventory_decimal": actual_spot_inventory_decimal,
            "actual_futures_inventory_decimal": actual_futures_inventory_decimal,
            "exit_spot_quantity_decimal": &chase.exit_spot_quantity_decimal,
            "exit_futures_quantity_decimal": &chase.exit_futures_quantity_decimal,
            "spot_generations": generation_statuses(&chase.spot_order_aliases),
            "futures_generations": generation_statuses(&chase.futures_order_aliases),
            "spot_final_status": spot_final_status,
            "futures_final_status": futures_final_status,
            "deadline_classification": deadline_classification,
            "account_id": lineage.as_ref().and_then(|v| v.account_id.clone()),
            "environment": lineage.as_ref().and_then(|v| v.environment.clone()),
            "strategy_id": lineage.as_ref().and_then(|v| v.strategy_id.clone()),
            "cycle_id": lineage.as_ref().and_then(|v| v.cycle_id.clone()),
            "intent_id": lineage.as_ref().and_then(|v| v.intent_id.clone()),
            "leg_id": lineage.as_ref().and_then(|v| v.leg_id.clone()),
            "config_version_hash": lineage.as_ref().and_then(|v| v.config_version_hash.clone()),
        });
        if let Some((ack_status, reason)) = terminal_status {
            let publication_id = format!(
                "cycle:{}:{}:{}",
                chase.cycle_client_order_id(),
                status,
                execution_type
            );
            cycle_event["publication_id"] = serde_json::Value::String(publication_id.clone());
            let mut durable_terminal = chase.clone();
            durable_terminal.phase = ChasePhase::Completed;
            self.ensure_terminal_tombstone(
                &durable_terminal,
                "TERMINAL_RECONCILED",
                "TERMINAL_EVENT_PERSISTED",
                reason,
            );
            // Persist the complete economic terminal payload in the same
            // checkpoint as Completed, before either journal ACK or broadcast.
            self.terminal_publications
                .entry(publication_id.clone())
                .or_insert(cycle_event);
            if !self.store_chase_state(
                chase.symbol.to_ascii_uppercase(),
                durable_terminal,
                "terminal lifecycle and publication outbox before ACK",
            ) {
                return false;
            }
            if let Some(intent_id) = self.chase_intent_ids.get(&chase.symbol).cloned() {
                if !self.transition_intent_ack(&intent_id, ack_status, reason) {
                    return false;
                }
                self.chase_intent_ids.remove(&chase.symbol);
            }
            self.publish_terminal(&publication_id);
            return true;
        }
        if let Ok(vec) = rmp_serde::to_vec_named(&cycle_event) {
            let _ = self.dash_tx.send(vec);
        }
        true
    }

    fn publish_terminal(&self, publication_id: &str) {
        if let Some(payload) = self.terminal_publications.get(publication_id)
            && self.execution_state_journal_error.is_none()
            && payload
                .get("symbol")
                .and_then(Value::as_str)
                .is_some_and(|symbol| !self.is_symbol_persistence_latched(symbol))
            && let Ok(encoded) = rmp_serde::to_vec_named(payload)
        {
            let _ = self.dash_tx.send(encoded);
        }
    }

    fn replay_terminal_publications(&self) {
        let mut publications: Vec<_> = self.terminal_publications.iter().collect();
        publications
            .sort_by_key(|(id, payload)| (payload["event_time_ms"].as_i64().unwrap_or(0), *id));
        for (publication_id, _) in publications {
            self.publish_terminal(publication_id);
        }
    }

    fn complete_terminal_publication(&mut self, publication_id: &str) {
        let Some(payload) = self.terminal_publications.remove(publication_id) else {
            return;
        };
        if !self.persist_execution_state("terminal telemetry durable handoff") {
            self.terminal_publications
                .insert(publication_id.to_string(), payload);
        }
    }

    fn acknowledge_submitted_order(&mut self, client_order_id: &str) {
        if let Some(order) = self.internal_orders.get_mut(client_order_id)
            && matches!(order.status.as_str(), "PENDING_SUBMIT" | "SUBMITTING")
        {
            order.status = "NEW".to_string();
        }
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
        if !self.store_chase_state(
            symbol.to_uppercase(),
            chase.clone(),
            "chase marked reconciliation-required",
        ) {
            return;
        }
        if self.trading_mode != "paper" && !self.is_symbol_persistence_latched(symbol) {
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

    fn emergency_config_budgets(&self) -> (u16, u16, String) {
        let active = self.config_consensus.active();
        (
            active
                .map(|snapshot| snapshot.emergency_exit_max_retries)
                .unwrap_or(crate::ipc::DEFAULT_EMERGENCY_EXIT_MAX_RETRIES),
            active
                .map(|snapshot| snapshot.emergency_exit_readback_attempts)
                .unwrap_or(crate::ipc::DEFAULT_EMERGENCY_EXIT_READBACK_ATTEMPTS),
            active
                .map(|snapshot| snapshot.emergency_exit_max_slippage_bps.clone())
                .unwrap_or_else(|| {
                    ExactDecimal::from_f64(crate::ipc::DEFAULT_EMERGENCY_EXIT_MAX_SLIPPAGE_BPS)
                        .expect("compiled emergency slippage budget is finite")
                        .to_string()
                }),
        )
    }

    fn emergency_repair_client_order_id(intent_id: &str, leg: Leg, generation: u16) -> String {
        let mut digest = Sha256::new();
        digest.update(b"bongus-emergency-repair-v1\0");
        digest.update(intent_id.as_bytes());
        digest.update(match leg {
            Leg::Spot => b"\0spot".as_slice(),
            Leg::Futures => b"\0futures".as_slice(),
        });
        digest.update(generation.to_be_bytes());
        let leg_code = if leg == Leg::Spot { "s" } else { "f" };
        format!(
            "bngs_er_{leg_code}_{}",
            &hex::encode(digest.finalize())[..22]
        )
    }

    fn autonomous_emergency_intent_id(symbol: &str, risk_sequence: u64) -> String {
        let mut digest = Sha256::new();
        digest.update(b"bongus-continuous-risk-emergency-v1\0");
        digest.update(risk_sequence.to_be_bytes());
        digest.update(symbol.as_bytes());
        format!(
            "rust-risk-{risk_sequence}-{}",
            &hex::encode(digest.finalize())[..20]
        )
    }

    fn autonomous_emergency_instruction(
        &self,
        symbol: &str,
        position: &TrackedPosition,
        risk_sequence: u64,
    ) -> Result<crate::ipc::AlphaInstruction, String> {
        let exact_leg_quantity =
            |leg_name: &str, leg: Option<&TrackedLegPosition>| -> Result<ExactDecimal, String> {
                match leg {
                Some(leg) => ExactDecimal::from_f64(leg.quantity)
                    .filter(|quantity| quantity.is_positive())
                    .ok_or_else(|| {
                        format!(
                            "{symbol} tracked {leg_name} quantity is not a finite positive value"
                        )
                    }),
                None => Ok(ExactDecimal::ZERO),
            }
            };
        let spot_quantity = exact_leg_quantity("spot", position.spot.as_ref())?;
        let futures_quantity = exact_leg_quantity("futures", position.perp.as_ref())?;
        let requested_quantity = spot_quantity.max(futures_quantity);
        if !requested_quantity.is_positive() {
            return Err(format!("{symbol} has no tracked inventory to derisk"));
        }

        let mut direction: Option<&'static str> = None;
        let mut merge_direction = |candidate: &'static str, leg_name: &str| -> Result<(), String> {
            if direction.is_some_and(|current| current != candidate) {
                return Err(format!(
                    "{symbol} tracked {leg_name} side conflicts with the other leg"
                ));
            }
            direction = Some(candidate);
            Ok(())
        };
        if let Some(spot) = position.spot.as_ref() {
            let is_long = Self::side_is_long(&spot.side)
                .ok_or_else(|| format!("{symbol} tracked spot side is invalid"))?;
            if !is_long {
                return Err(format!(
                    "{symbol} tracked short-spot liability cannot be autonomously repaid on Standard Spot"
                ));
            }
            merge_direction("EXIT_LONG", "spot")?;
        }
        if let Some(futures) = position.perp.as_ref() {
            let is_long = Self::side_is_long(&futures.side)
                .ok_or_else(|| format!("{symbol} tracked futures side is invalid"))?;
            merge_direction(if is_long { "EXIT_SHORT" } else { "EXIT_LONG" }, "futures")?;
        }
        let intent = direction
            .ok_or_else(|| format!("{symbol} emergency direction could not be classified"))?;
        let (_, _, configured_slippage) = self.emergency_config_budgets();
        let max_slippage_bps = Self::canonical_exact(Some(&configured_slippage))
            .and_then(ExactDecimal::to_f64)
            .filter(|value| value.is_finite() && *value >= 0.0)
            .ok_or_else(|| "configured emergency slippage budget is invalid".to_string())?;
        let requested_f64 = requested_quantity
            .to_f64()
            .ok_or_else(|| format!("{symbol} emergency quantity does not fit execution input"))?;
        let spot_f64 = spot_quantity
            .to_f64()
            .ok_or_else(|| format!("{symbol} spot quantity does not fit execution input"))?;
        let futures_f64 = futures_quantity
            .to_f64()
            .ok_or_else(|| format!("{symbol} futures quantity does not fit execution input"))?;

        Ok(crate::ipc::AlphaInstruction {
            symbol: Some(symbol.to_string()),
            intent: intent.to_string(),
            quantity: requested_f64,
            urgency: 1.0,
            max_slippage_bps,
            route_policy: Some("emergency_reduce_only".to_string()),
            route_model_version: Some("continuous-risk-emergency-v1".to_string()),
            exposure_scale: 1.0,
            intent_id: Some(Self::autonomous_emergency_intent_id(symbol, risk_sequence)),
            direction: Some(
                if intent == "EXIT_LONG" {
                    "long"
                } else {
                    "short"
                }
                .to_string(),
            ),
            skip_spot_leg: spot_quantity == ExactDecimal::ZERO,
            skip_perp_leg: futures_quantity == ExactDecimal::ZERO,
            spot_quantity: Some(spot_f64),
            perp_quantity: Some(futures_f64),
            requested_quantity_decimal: Some(requested_quantity.to_string()),
            actual_spot_inventory_decimal: Some(spot_quantity.to_string()),
            actual_futures_inventory_decimal: Some(futures_quantity.to_string()),
            exit_spot_quantity_decimal: Some(spot_quantity.to_string()),
            exit_futures_quantity_decimal: Some(futures_quantity.to_string()),
            ..crate::ipc::AlphaInstruction::default()
        })
    }

    async fn drive_autonomous_emergency_exits(&mut self, risk_reason: &str) -> Result<(), String> {
        if self.continuous_risk_state != ContinuousRiskState::Derisking {
            return Err("continuous risk is not durably DERISKING".to_string());
        }
        let risk_sequence = self.continuous_risk_sequence;
        let mut symbols = self
            .tracked_positions
            .iter()
            .filter(|(_, position)| {
                [position.spot.as_ref(), position.perp.as_ref()]
                    .into_iter()
                    .flatten()
                    .any(|leg| leg.quantity.is_finite() && leg.quantity > 0.0)
            })
            .map(|(symbol, _)| symbol.clone())
            .collect::<Vec<_>>();
        symbols.sort_unstable();
        if symbols.is_empty() {
            return Err(
                "DERISKING has no attributable positive tracked inventory; exchange-only risk requires manual review"
                    .to_string(),
            );
        }

        // Materialize every safe symbol record before driving any one of them.
        // A failed symbol moves the portfolio actor to MANUAL_REVIEW, but that
        // must not prevent already-durable exits for the remaining symbols.
        let mut intents_to_drive = Vec::<(String, String)>::new();
        let mut errors = Vec::<String>::new();

        for symbol in symbols {
            if let Some(existing) = self
                .emergency_exits
                .values()
                .find(|record| record.symbol == symbol && record.state != EmergencyExitState::Flat)
                .cloned()
            {
                if existing.state == EmergencyExitState::ManualReview {
                    errors.push(format!(
                        "{} emergency exit {} already requires manual review: {}",
                        symbol, existing.intent_id, existing.last_error
                    ));
                    continue;
                }
                // A command-driven emergency may be inside a REST call that is
                // pumping this risk evaluation recursively. It already owns the
                // symbol, so never start or recursively drive a competing exit.
                if existing.autonomous_risk_sequence.is_none() {
                    continue;
                }
                intents_to_drive.push((symbol, existing.intent_id));
                continue;
            }

            if self.continuous_risk_reason == format!("emergency_exit:{symbol}")
                && self.emergency_exits.values().any(|record| {
                    record.symbol == symbol
                        && record.autonomous_risk_sequence.is_none()
                        && record.state == EmergencyExitState::Flat
                })
            {
                // The command-driven emergency that latched DERISKING already
                // completed. A stale in-memory tracked position must not cause
                // the continuous actor to submit the same liquidation twice.
                continue;
            }

            let position = self
                .tracked_positions
                .get(&symbol)
                .cloned()
                .ok_or_else(|| format!("{symbol} tracked position disappeared during derisk"));
            let position = match position {
                Ok(position) => position,
                Err(error) => {
                    errors.push(error);
                    continue;
                }
            };
            let instruction =
                match self.autonomous_emergency_instruction(&symbol, &position, risk_sequence) {
                    Ok(instruction) => instruction,
                    Err(error) => {
                        errors.push(error);
                        continue;
                    }
                };
            let detection_reason = format!("continuous risk autonomous emergency: {risk_reason}");
            match self.begin_emergency_exit_for_risk(
                &instruction,
                &symbol,
                Some(risk_sequence),
                &detection_reason,
            ) {
                Ok(intent_id) => intents_to_drive.push((symbol, intent_id)),
                Err(error) => errors.push(format!("{symbol} emergency detection failed: {error}")),
            }
        }

        intents_to_drive.sort_unstable();
        intents_to_drive.dedup();
        for (symbol, intent_id) in intents_to_drive {
            self.drive_emergency_exit(intent_id.clone()).await;
            match self
                .emergency_exits
                .get(&intent_id)
                .map(|record| (record.state, record.last_error.clone()))
            {
                Some((EmergencyExitState::Flat, _)) => {}
                Some((EmergencyExitState::ManualReview, last_error)) => {
                    errors.push(format!(
                        "{symbol} autonomous emergency exit {intent_id} entered manual review: {last_error}"
                    ));
                }
                Some((state, _)) => {
                    errors.push(format!(
                        "{symbol} autonomous emergency exit {intent_id} stopped in {}",
                        state.as_str()
                    ));
                }
                None => errors.push(format!(
                    "{symbol} autonomous emergency exit {intent_id} disappeared"
                )),
            }
        }

        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; "))
        }
    }

    fn emergency_exit_event(record: &EmergencyExitRecord) -> serde_json::Value {
        let mut event = serde_json::json!({
            "event": "EmergencyExitState",
            "schema_version": crate::ipc::EXECUTION_PROTOCOL_VERSION,
            "symbol": record.symbol,
            "intent_id": record.intent_id,
            "direction": record.direction,
            "state": record.state.as_str(),
            "transition_sequence": record.transition_sequence,
            "persisted_at_ms": record.updated_at_ms,
            "event_time_ms": record.updated_at_ms,
            "account_id": record.lineage.account_id,
            "environment": record.lineage.environment,
            "strategy_id": record.lineage.strategy_id,
            "cycle_id": record.lineage.cycle_id,
            "config_version_hash": record.lineage.config_version_hash,
            "verified_spot_inventory_decimal": record.verified_spot_inventory_decimal,
            "verified_futures_inventory_decimal": record.verified_futures_inventory_decimal,
            "requested_quantity_decimal": record.requested_quantity_decimal,
            "actual_spot_inventory_decimal": record.actual_spot_inventory_decimal,
            "actual_futures_inventory_decimal": record.actual_futures_inventory_decimal,
            "exit_spot_quantity_decimal": record.exit_spot_quantity_decimal,
            "exit_futures_quantity_decimal": record.exit_futures_quantity_decimal,
            "signed_spot_total_decimal": record.signed_spot_total_decimal,
            "signed_spot_available_decimal": record.signed_spot_available_decimal,
            "signed_futures_position_decimal": record.signed_futures_position_decimal,
            "classified_spot_exit_quantity_decimal": record.classified_spot_exit_quantity_decimal,
            "classified_futures_exit_quantity_decimal": record.classified_futures_exit_quantity_decimal,
            "cumulative_spot_emergency_filled_decimal": record.cumulative_spot_emergency_filled_decimal,
            "cumulative_futures_emergency_filled_decimal": record.cumulative_futures_emergency_filled_decimal,
            "spot_repair_client_order_id": record.spot_repair_client_order_id,
            "futures_repair_client_order_id": record.futures_repair_client_order_id,
            "spot_generations": record.spot_generations,
            "futures_generations": record.futures_generations,
            "original_exit_spot_client_order_ids": record.original_exit_spot_client_order_ids,
            "original_exit_futures_client_order_ids": record.original_exit_futures_client_order_ids,
            "max_retries": record.max_retries,
            "readback_budget": record.readback_budget,
            "max_slippage_bps_decimal": record.max_slippage_bps_decimal,
            "last_error": record.last_error,
            "autonomous": record.autonomous_risk_sequence.is_some(),
            "autonomous_risk_sequence": record.autonomous_risk_sequence,
            "trigger_reason": record.trigger_reason,
        });
        if record.state == EmergencyExitState::Flat {
            event["publication_id"] =
                serde_json::json!(format!("emergency:{}:FLAT", record.intent_id));
            event["flat_proof"] = serde_json::json!(true);
            // Signed inventory closure does not invent missing trade prices,
            // commissions or funding. Python must reconcile their accounting.
            event["accounting_status"] = serde_json::json!("RECONCILIATION_REQUIRED");
        }
        event
    }

    fn emit_emergency_exit_state(&self, record: &EmergencyExitRecord) {
        let event = Self::emergency_exit_event(record);
        if let Ok(payload) = rmp_serde::to_vec_named(&event) {
            let _ = self.dash_tx.send(payload);
        }
    }

    fn update_emergency_exit(
        &mut self,
        intent_id: &str,
        context: &str,
        update: impl FnOnce(&mut EmergencyExitRecord),
    ) -> bool {
        let Some(mut record) = self.emergency_exits.get(intent_id).cloned() else {
            return false;
        };
        update(&mut record);
        record.updated_at_ms = Self::current_time_ms();
        let symbol = record.symbol.clone();
        self.emergency_exits
            .insert(intent_id.to_string(), record.clone());
        if !self.persist_execution_state_for_symbol(&symbol, context) {
            return false;
        }
        self.emit_emergency_exit_state(&record);
        true
    }

    fn transition_emergency_exit(
        &mut self,
        intent_id: &str,
        next: EmergencyExitState,
        reason: &str,
    ) -> bool {
        let Some(mut record) = self.emergency_exits.get(intent_id).cloned() else {
            return false;
        };
        if record.state.is_terminal() || !record.state.allows(next) || reason.trim().is_empty() {
            error!(
                "Invalid emergency exit transition for {intent_id}: {} -> {}",
                record.state.as_str(),
                next.as_str()
            );
            return false;
        }
        let sequence = record.transition_sequence.saturating_add(1);
        let now_ms = Self::current_time_ms();
        record.state = next;
        record.transition_sequence = sequence;
        record.updated_at_ms = now_ms;
        record.last_error = if next == EmergencyExitState::ManualReview {
            reason.to_string()
        } else {
            String::new()
        };
        record.transitions.push(EmergencyExitTransition {
            state: next,
            sequence,
            persisted_at_ms: now_ms,
            reason: reason.to_string(),
        });
        let symbol = record.symbol.clone();
        self.emergency_exits
            .insert(intent_id.to_string(), record.clone());
        if next == EmergencyExitState::Flat {
            let event = Self::emergency_exit_event(&record);
            let publication_id = event["publication_id"]
                .as_str()
                .expect("flat publication id")
                .to_string();
            self.terminal_publications
                .entry(publication_id)
                .or_insert(event);
        }
        if !self.persist_execution_state_for_symbol(&symbol, "emergency exit state transition") {
            return false;
        }
        self.emit_emergency_exit_state(&record);
        true
    }

    fn fail_emergency_exit(&mut self, intent_id: &str, reason: &str) {
        let command_driven = self
            .emergency_exits
            .get(intent_id)
            .is_some_and(|record| record.autonomous_risk_sequence.is_none());
        let current = self
            .emergency_exits
            .get(intent_id)
            .map(|record| record.state);
        if current == Some(EmergencyExitState::ManualReview) {
            let _ = self.update_emergency_exit(
                intent_id,
                "emergency manual-review reason updated",
                |record| record.last_error = reason.to_string(),
            );
        } else if current.is_some_and(|state| !state.is_terminal()) {
            let _ =
                self.transition_emergency_exit(intent_id, EmergencyExitState::ManualReview, reason);
        }
        self.state = SystemState::Reconciling;
        self.continuous_risk_state = ContinuousRiskState::ManualReview;
        self.continuous_risk_reason = format!("emergency_exit:{reason}");
        self.continuous_risk_sequence = self.continuous_risk_sequence.saturating_add(1);
        self.continuous_risk_updated_at_ms = Self::current_time_ms();
        if let Some(symbol) = self
            .emergency_exits
            .get(intent_id)
            .map(|record| record.symbol.clone())
        {
            let _ = self.persist_execution_state_for_symbol(
                &symbol,
                "emergency exit entered manual review",
            );
        }
        if command_driven {
            let _ = self.transition_intent_ack(intent_id, "REJECTED", reason);
        }
    }

    fn begin_emergency_exit(
        &mut self,
        instruction: &crate::ipc::AlphaInstruction,
        symbol: &str,
    ) -> Result<String, &'static str> {
        self.begin_emergency_exit_for_risk(instruction, symbol, None, "emergency route accepted")
    }

    fn begin_emergency_exit_for_risk(
        &mut self,
        instruction: &crate::ipc::AlphaInstruction,
        symbol: &str,
        autonomous_risk_sequence: Option<u64>,
        detection_reason: &str,
    ) -> Result<String, &'static str> {
        if detection_reason.trim().is_empty() {
            return Err("missing_emergency_detection_reason");
        }
        if autonomous_risk_sequence.is_some_and(|sequence| {
            sequence == 0
                || sequence != self.continuous_risk_sequence
                || self.continuous_risk_state != ContinuousRiskState::Derisking
        }) {
            return Err("autonomous_emergency_risk_episode_changed");
        }
        let intent_id = instruction.intent_id.clone().unwrap_or_default();
        let requested_quantity_decimal = instruction
            .requested_quantity_decimal
            .clone()
            .ok_or("missing_requested_quantity_decimal")?;
        let actual_spot_inventory_decimal = instruction
            .actual_spot_inventory_decimal
            .clone()
            .ok_or("missing_actual_spot_inventory_decimal")?;
        let actual_futures_inventory_decimal = instruction
            .actual_futures_inventory_decimal
            .clone()
            .ok_or("missing_actual_futures_inventory_decimal")?;
        let exit_spot_quantity_decimal = instruction
            .exit_spot_quantity_decimal
            .clone()
            .ok_or("missing_exit_spot_quantity_decimal")?;
        let exit_futures_quantity_decimal = instruction
            .exit_futures_quantity_decimal
            .clone()
            .ok_or("missing_exit_futures_quantity_decimal")?;

        if let Some(sequence) = autonomous_risk_sequence
            && let Some(existing) = self.emergency_exits.get(&intent_id)
        {
            let requested_spot = Self::canonical_exact(Some(&exit_spot_quantity_decimal));
            let requested_futures = Self::canonical_exact(Some(&exit_futures_quantity_decimal));
            let existing_spot = Self::canonical_exact(Some(&existing.exit_spot_quantity_decimal));
            let existing_futures =
                Self::canonical_exact(Some(&existing.exit_futures_quantity_decimal));
            if existing.autonomous_risk_sequence == Some(sequence)
                && existing.symbol == symbol
                && existing.direction == instruction.intent
                && requested_spot
                    .zip(existing_spot)
                    .is_some_and(|(requested, budget)| requested <= budget)
                && requested_futures
                    .zip(existing_futures)
                    .is_some_and(|(requested, budget)| requested <= budget)
            {
                return Ok(intent_id);
            }
            return Err("autonomous_emergency_intent_conflict");
        }
        if self
            .emergency_exits
            .values()
            .any(|record| record.symbol == symbol && record.state != EmergencyExitState::Flat)
        {
            return Err("emergency_exit_already_active");
        }
        let (max_retries, readback_budget, configured_slippage) = self.emergency_config_budgets();
        let command_slippage = ExactDecimal::from_f64(instruction.max_slippage_bps)
            .ok_or("invalid_emergency_slippage_budget")?;
        let configured_slippage = Self::canonical_exact(Some(&configured_slippage))
            .ok_or("invalid_emergency_slippage_budget")?;
        let effective_slippage = command_slippage.min(configured_slippage).to_string();
        let now_ms = Self::current_time_ms();
        // Keep pre-emergency exit orders separate from repair generations: they
        // belong to the economic exit but must not inflate the repair budget.
        let original_exit_ids = |leg: Leg| {
            let mut ids = self
                .chase_states
                .get(symbol)
                .filter(|chase| chase.is_exit)
                .map(|chase| {
                    let (aliases, primary) = match leg {
                        Leg::Spot => (&chase.spot_order_aliases, &chase.spot_client_order_id),
                        Leg::Futures => {
                            (&chase.futures_order_aliases, &chase.futures_client_order_id)
                        }
                    };
                    aliases
                        .iter()
                        .chain(std::iter::once(primary))
                        .filter(|id| !id.is_empty())
                        .cloned()
                        .collect::<Vec<_>>()
                })
                .unwrap_or_default();
            ids.sort();
            ids.dedup();
            ids
        };
        let record = EmergencyExitRecord {
            schema_version: 1,
            symbol: symbol.to_string(),
            intent_id: intent_id.clone(),
            lineage: OrderLineage {
                account_id: instruction.account_id.clone(),
                environment: instruction.environment.clone(),
                strategy_id: instruction.strategy_id.clone(),
                cycle_id: instruction.cycle_id.clone(),
                intent_id: instruction.intent_id.clone(),
                config_version_hash: instruction.config_version_hash.clone(),
                ..OrderLineage::default()
            },
            original_exit_spot_client_order_ids: original_exit_ids(Leg::Spot),
            original_exit_futures_client_order_ids: original_exit_ids(Leg::Futures),
            direction: instruction.intent.clone(),
            state: EmergencyExitState::Detected,
            transition_sequence: 1,
            updated_at_ms: now_ms,
            requested_quantity_decimal,
            actual_spot_inventory_decimal,
            actual_futures_inventory_decimal,
            exit_spot_quantity_decimal,
            exit_futures_quantity_decimal,
            signed_spot_total_decimal: "0".to_string(),
            signed_spot_available_decimal: "0".to_string(),
            signed_futures_position_decimal: "0".to_string(),
            initial_signed_spot_total_decimal: "0".to_string(),
            initial_signed_futures_position_decimal: "0".to_string(),
            initial_inventory_captured: false,
            classified_spot_exit_quantity_decimal: "0".to_string(),
            classified_futures_exit_quantity_decimal: "0".to_string(),
            cumulative_spot_emergency_filled_decimal: "0".to_string(),
            cumulative_futures_emergency_filled_decimal: "0".to_string(),
            verified_spot_inventory_decimal: "0".to_string(),
            verified_futures_inventory_decimal: "0".to_string(),
            spot_reference_price_decimal: self
                .spot_mid_cache
                .get(symbol)
                .copied()
                .and_then(ExactDecimal::from_f64)
                .map(|value| value.to_string())
                .unwrap_or_else(|| "0".to_string()),
            futures_reference_price_decimal: self
                .perp_mid_cache
                .get(symbol)
                .copied()
                .and_then(ExactDecimal::from_f64)
                .map(|value| value.to_string())
                .unwrap_or_else(|| "0".to_string()),
            spot_repair_client_order_id: Self::emergency_repair_client_order_id(
                &intent_id,
                Leg::Spot,
                0,
            ),
            futures_repair_client_order_id: Self::emergency_repair_client_order_id(
                &intent_id,
                Leg::Futures,
                0,
            ),
            spot_generation: 0,
            futures_generation: 0,
            spot_generations: Vec::new(),
            futures_generations: Vec::new(),
            cancel_attempts: 0,
            readback_attempts: 0,
            submit_attempts: 0,
            verify_attempts: 0,
            derisk_attempts: 0,
            spot_submission_confirmed: false,
            futures_submission_confirmed: false,
            max_retries,
            readback_budget,
            max_slippage_bps_decimal: effective_slippage,
            last_error: String::new(),
            autonomous_risk_sequence,
            trigger_reason: detection_reason.to_string(),
            transitions: vec![EmergencyExitTransition {
                state: EmergencyExitState::Detected,
                sequence: 1,
                persisted_at_ms: now_ms,
                reason: detection_reason.to_string(),
            }],
        };
        self.state = SystemState::Reconciling;
        if autonomous_risk_sequence.is_none() {
            self.continuous_risk_state = ContinuousRiskState::Derisking;
            self.continuous_risk_reason = format!("emergency_exit:{symbol}");
            self.continuous_risk_sequence = self.continuous_risk_sequence.saturating_add(1);
            self.continuous_risk_updated_at_ms = now_ms;
        }
        self.emergency_exits
            .insert(intent_id.clone(), record.clone());
        if !self
            .persist_execution_state_for_symbol(symbol, "emergency exit DETECTED before effects")
        {
            return Err("emergency_state_not_durable");
        }
        self.emit_emergency_exit_state(&record);
        Ok(intent_id)
    }

    async fn cancel_current_orders_for_emergency(&mut self, intent_id: &str) -> Result<(), String> {
        let record = self
            .emergency_exits
            .get(intent_id)
            .cloned()
            .ok_or_else(|| "emergency exit disappeared before cancellation".to_string())?;
        let symbol = record.symbol.clone();
        let mut targets = Vec::<(String, LegVenue, Option<Leg>)>::new();
        if let Some(chase) = self.chase_states.get(&symbol) {
            for client_id in chase
                .spot_order_aliases
                .iter()
                .chain(std::iter::once(&chase.spot_client_order_id))
                .filter(|client_id| !client_id.is_empty())
            {
                targets.push((client_id.clone(), LegVenue::Spot, Some(Leg::Spot)));
            }
            for client_id in chase
                .futures_order_aliases
                .iter()
                .chain(std::iter::once(&chase.futures_client_order_id))
                .filter(|client_id| !client_id.is_empty())
            {
                targets.push((client_id.clone(), LegVenue::UsdtFutures, Some(Leg::Futures)));
            }
        }
        for (client_id, order) in &self.internal_orders {
            if order.symbol.eq_ignore_ascii_case(&symbol)
                && !is_terminal_internal_status(&order.status)
            {
                let venue = match self
                    .order_lineage
                    .get(client_id)
                    .and_then(|item| item.market)
                {
                    Some(MarketType::Spot) => LegVenue::Spot,
                    _ => LegVenue::UsdtFutures,
                };
                targets.push((client_id.clone(), venue, None));
            }
        }
        targets.sort_by(|left, right| left.0.cmp(&right.0));
        targets.dedup_by(|left, right| left.0 == right.0);

        for (client_order_id, venue, hinted_leg) in targets {
            if self
                .internal_orders
                .get(&client_order_id)
                .is_some_and(|order| is_terminal_internal_status(&order.status))
            {
                continue;
            }
            let snapshot = if self.trading_mode == "paper" {
                let cumulative_filled_qty = self
                    .order_cumulative_fills
                    .get(&client_order_id)
                    .copied()
                    .unwrap_or(0.0);
                let average_fill_price =
                    self.chase_states
                        .get(&symbol)
                        .and_then(|chase| match hinted_leg {
                            Some(Leg::Spot) => {
                                chase.spot_fill_price.or((chase.expected_spot_price > 0.0)
                                    .then_some(chase.expected_spot_price))
                            }
                            Some(Leg::Futures) => {
                                chase.futures_fill_price.or((chase.expected_fut_price > 0.0)
                                    .then_some(chase.expected_fut_price))
                            }
                            None => None,
                        });
                TerminalOrderSnapshot {
                    status: ExchangeOrderStatus::Canceled,
                    cumulative_filled_qty,
                    average_fill_price,
                }
            } else {
                let mut terminal = None;
                let mut last_error = String::new();
                for _ in 0..=record.max_retries {
                    if !self.update_emergency_exit(
                        intent_id,
                        "emergency cancel attempt checkpoint",
                        |item| item.cancel_attempts = item.cancel_attempts.saturating_add(1),
                    ) {
                        return Err("emergency cancel attempt was not durable".to_string());
                    }
                    match self
                        .cancel_order_pumped(venue, &symbol, &client_order_id)
                        .await
                    {
                        Ok(body) => {
                            match Self::parse_terminal_order_snapshot(&body, &client_order_id) {
                                Ok(value) => {
                                    terminal = Some(value);
                                    break;
                                }
                                Err(error) => last_error = error,
                            }
                        }
                        Err(error) => last_error = error,
                    }
                    for _ in 0..record.readback_budget {
                        if !self.update_emergency_exit(
                            intent_id,
                            "emergency cancel readback checkpoint",
                            |item| {
                                item.readback_attempts = item.readback_attempts.saturating_add(1)
                            },
                        ) {
                            return Err("emergency cancel readback was not durable".to_string());
                        }
                        match self
                            .get_order_pumped(venue, &symbol, &client_order_id)
                            .await
                        {
                            Ok(body) => {
                                match Self::parse_terminal_order_snapshot(&body, &client_order_id) {
                                    Ok(value) => {
                                        terminal = Some(value);
                                        break;
                                    }
                                    Err(error) => last_error = error,
                                }
                            }
                            Err(error) => last_error = error,
                        }
                    }
                    if terminal.is_some() {
                        break;
                    }
                }
                terminal.ok_or_else(|| {
                    format!("cancel/readback budget exhausted for {client_order_id}: {last_error}")
                })?
            };

            let matching = self
                .chase_states
                .get(&symbol)
                .and_then(|chase| chase.leg_for_client_order_id(&client_order_id))
                .or(hinted_leg);
            if let (Some(mut chase), Some(leg)) =
                (self.chase_states.get(&symbol).cloned(), matching)
            {
                self.apply_terminal_order_snapshot(&mut chase, leg, &client_order_id, snapshot)?;
                chase.phase = ChasePhase::ReconciliationRequired;
                if !self.store_chase_state(
                    symbol.clone(),
                    chase,
                    "fill-during-cancel incorporated into emergency inventory",
                ) {
                    return Err("emergency cancel snapshot was not durable".to_string());
                }
            } else {
                self.internal_orders
                    .entry(client_order_id.clone())
                    .and_modify(|order| {
                        order.status = match snapshot.status {
                            ExchangeOrderStatus::Filled => "FILLED",
                            ExchangeOrderStatus::Canceled => "CANCELED",
                            ExchangeOrderStatus::Rejected => "REJECTED",
                            ExchangeOrderStatus::Expired => "EXPIRED",
                            ExchangeOrderStatus::ExpiredInMatch => "EXPIRED_IN_MATCH",
                            _ => "UNKNOWN",
                        }
                        .to_string();
                    });
                self.order_cumulative_fills
                    .insert(client_order_id, snapshot.cumulative_filled_qty);
                if !self
                    .persist_execution_state_for_symbol(&symbol, "emergency orphan cancel snapshot")
                {
                    return Err("emergency orphan cancel snapshot was not durable".to_string());
                }
            }
        }
        Ok(())
    }

    fn paper_emergency_inventory(
        &self,
        record: &EmergencyExitRecord,
    ) -> Result<(ExactDecimal, ExactDecimal, ExactDecimal), String> {
        if record.derisk_attempts > 0 {
            let spot_total = Self::canonical_exact(Some(&record.signed_spot_total_decimal))
                .ok_or_else(|| "paper emergency spot total is invalid".to_string())?;
            let spot_available = Self::canonical_exact(Some(&record.signed_spot_available_decimal))
                .ok_or_else(|| "paper emergency spot available is invalid".to_string())?;
            let futures =
                Self::canonical_signed_exact(Some(&record.signed_futures_position_decimal))
                    .ok_or_else(|| "paper emergency futures position is invalid".to_string())?;
            return Ok((spot_total, spot_available, futures));
        }
        let declared_spot = Self::canonical_exact(Some(&record.actual_spot_inventory_decimal))
            .ok_or_else(|| "declared spot inventory is invalid".to_string())?;
        let declared_futures =
            Self::canonical_exact(Some(&record.actual_futures_inventory_decimal))
                .ok_or_else(|| "declared futures inventory is invalid".to_string())?;
        let tracked = self.tracked_positions.get(&record.symbol);
        let tracked_spot = tracked
            .and_then(|position| position.spot.as_ref())
            .and_then(|leg| ExactDecimal::from_f64(leg.quantity))
            .unwrap_or(declared_spot);
        let base_asset = Self::base_asset_for_symbol(&record.symbol);
        let spot = base_asset
            .and_then(|asset| self.spot_balances.get(asset))
            .copied()
            .and_then(ExactDecimal::from_f64)
            .unwrap_or(tracked_spot);
        let available = base_asset
            .and_then(|asset| self.spot_available_balances.get(asset))
            .copied()
            .and_then(ExactDecimal::from_f64)
            .unwrap_or(spot);
        let futures_abs = tracked
            .and_then(|position| position.perp.as_ref())
            .and_then(|leg| ExactDecimal::from_f64(leg.quantity))
            .unwrap_or(declared_futures);
        let futures = match tracked.and_then(|position| position.perp.as_ref()) {
            Some(leg) if Self::side_is_long(&leg.side) == Some(false) => ExactDecimal::ZERO
                .checked_sub(futures_abs)
                .ok_or_else(|| "paper futures sign overflow".to_string())?,
            Some(_) => futures_abs,
            None if record.direction == "EXIT_LONG" => ExactDecimal::ZERO
                .checked_sub(futures_abs)
                .ok_or_else(|| "paper futures sign overflow".to_string())?,
            None => futures_abs,
        };
        Ok((spot, available.min(spot), futures))
    }

    async fn signed_emergency_inventory(
        &mut self,
        intent_id: &str,
    ) -> Result<(ExactDecimal, ExactDecimal, ExactDecimal), String> {
        let record = self
            .emergency_exits
            .get(intent_id)
            .cloned()
            .ok_or_else(|| "emergency exit disappeared before readback".to_string())?;
        if self.trading_mode == "paper" {
            return self.paper_emergency_inventory(&record);
        }
        let base_asset = Self::base_asset_for_symbol(&record.symbol)
            .ok_or_else(|| "emergency spot base asset is unknown".to_string())?
            .to_string();
        let mut last_error = String::new();
        for _ in 0..record.readback_budget {
            if !self.update_emergency_exit(
                intent_id,
                "signed emergency inventory readback checkpoint",
                |item| item.readback_attempts = item.readback_attempts.saturating_add(1),
            ) {
                return Err("signed emergency readback was not durable".to_string());
            }
            let spot_body = self.get_spot_account_pumped().await;
            let futures_body = self.get_futures_positions_pumped().await;
            match (spot_body, futures_body) {
                (Ok(spot_body), Ok(futures_body)) => {
                    let balances = Self::parse_exact_spot_account_balances(&spot_body)?;
                    let futures = Self::parse_exact_futures_positions(&futures_body)?;
                    let total = balances
                        .total
                        .get(&base_asset)
                        .copied()
                        .unwrap_or(ExactDecimal::ZERO);
                    let available = balances
                        .available
                        .get(&base_asset)
                        .copied()
                        .unwrap_or(ExactDecimal::ZERO);
                    let signed_futures = futures
                        .get(&record.symbol)
                        .copied()
                        .unwrap_or(ExactDecimal::ZERO);
                    return Ok((total, available.min(total), signed_futures));
                }
                (spot, futures) => {
                    last_error = format!("spot={spot:?};futures={futures:?}");
                }
            }
        }
        Err(format!(
            "signed inventory readback budget exhausted: {last_error}"
        ))
    }

    fn classify_emergency_inventory(&mut self, intent_id: &str) -> Result<(), String> {
        let record = self
            .emergency_exits
            .get(intent_id)
            .cloned()
            .ok_or_else(|| "emergency exit disappeared before classification".to_string())?;
        let exit_spot = Self::canonical_exact(Some(&record.exit_spot_quantity_decimal))
            .ok_or_else(|| "invalid exact emergency spot exit".to_string())?;
        let exit_futures = Self::canonical_exact(Some(&record.exit_futures_quantity_decimal))
            .ok_or_else(|| "invalid exact emergency futures exit".to_string())?;
        let cumulative_spot =
            Self::canonical_exact(Some(&record.cumulative_spot_emergency_filled_decimal))
                .ok_or_else(|| "invalid cumulative emergency spot fill".to_string())?;
        let cumulative_futures =
            Self::canonical_exact(Some(&record.cumulative_futures_emergency_filled_decimal))
                .ok_or_else(|| "invalid cumulative emergency futures fill".to_string())?;
        let remaining_spot = exit_spot
            .checked_sub(cumulative_spot)
            .filter(|value| *value >= ExactDecimal::ZERO)
            .ok_or_else(|| "emergency spot fills exceeded original bot budget".to_string())?;
        let remaining_futures = exit_futures
            .checked_sub(cumulative_futures)
            .filter(|value| *value >= ExactDecimal::ZERO)
            .ok_or_else(|| "emergency futures fills exceeded original bot budget".to_string())?;
        let spot_total = Self::canonical_exact(Some(&record.signed_spot_total_decimal))
            .ok_or_else(|| "invalid signed spot total".to_string())?;
        let spot_available = Self::canonical_exact(Some(&record.signed_spot_available_decimal))
            .ok_or_else(|| "invalid signed spot available".to_string())?;
        let signed_futures =
            Self::canonical_signed_exact(Some(&record.signed_futures_position_decimal))
                .ok_or_else(|| "invalid signed futures position".to_string())?;
        let futures_abs = Self::exact_abs(signed_futures)
            .ok_or_else(|| "signed futures absolute value overflow".to_string())?;
        let spot_target = if record.direction == "EXIT_LONG" {
            Self::exact_min([remaining_spot, spot_total, spot_available])
        } else {
            remaining_spot
        };
        let expected_futures_sign = (record.direction == "EXIT_LONG"
            && signed_futures < ExactDecimal::ZERO)
            || (record.direction == "EXIT_SHORT" && signed_futures > ExactDecimal::ZERO);
        let futures_target = if expected_futures_sign {
            remaining_futures.min(futures_abs)
        } else {
            ExactDecimal::ZERO
        };
        let normalize =
            |manager: &Self, market: MarketType, value: ExactDecimal| -> Result<String, String> {
                if value == ExactDecimal::ZERO {
                    return Ok("0".to_string());
                }
                manager
                    .normalize_exact_quantity_for_market(&record.symbol, market, value)
                    .map(|item| item.to_string())
                    .ok_or_else(|| "emergency quantity is below exchange filters".to_string())
            };
        let spot_target = normalize(self, MarketType::Spot, spot_target)?;
        let futures_target = normalize(self, MarketType::Perp, futures_target)?;
        if spot_target == "0" && futures_target == "0" {
            if remaining_spot == ExactDecimal::ZERO && remaining_futures == ExactDecimal::ZERO {
                return Ok(());
            }
            return Err(
                "signed inventory exists but no safe reduce-only quantity is executable"
                    .to_string(),
            );
        }
        if !self.update_emergency_exit(intent_id, "emergency inventory classification", |item| {
            item.classified_spot_exit_quantity_decimal = spot_target;
            item.classified_futures_exit_quantity_decimal = futures_target;
        }) {
            return Err("emergency inventory classification was not durable".to_string());
        }
        Ok(())
    }

    fn emergency_slippage_allows(
        &self,
        record: &EmergencyExitRecord,
        market: MarketType,
        side: TradeSide,
    ) -> Result<(), String> {
        let (reference, quantity) = match market {
            MarketType::Spot => (
                &record.spot_reference_price_decimal,
                &record.classified_spot_exit_quantity_decimal,
            ),
            MarketType::Perp => (
                &record.futures_reference_price_decimal,
                &record.classified_futures_exit_quantity_decimal,
            ),
        };
        if quantity == "0" {
            return Ok(());
        }
        let reference = Self::canonical_exact(Some(reference))
            .and_then(ExactDecimal::to_f64)
            .filter(|value| *value > 0.0)
            .ok_or_else(|| "emergency slippage reference is unavailable".to_string())?;
        let cap = Self::canonical_exact(Some(&record.max_slippage_bps_decimal))
            .and_then(ExactDecimal::to_f64)
            .ok_or_else(|| "emergency slippage cap is invalid".to_string())?;
        let observed = self
            .market_order_slippage_bps(&record.symbol, market, side, reference)
            .ok_or_else(|| "emergency executable price is unavailable".to_string())?;
        if observed > cap {
            return Err(format!(
                "emergency slippage budget exceeded: observed={observed:.8} cap={cap:.8}"
            ));
        }
        Ok(())
    }

    async fn submit_emergency_derisk(&mut self, intent_id: &str) -> Result<(), String> {
        let record = self
            .emergency_exits
            .get(intent_id)
            .cloned()
            .ok_or_else(|| "emergency exit disappeared before derisk".to_string())?;
        let (spot_side, futures_side) = if record.direction == "EXIT_LONG" {
            (TradeSide::Sell, TradeSide::Buy)
        } else {
            (TradeSide::Buy, TradeSide::Sell)
        };
        self.emergency_slippage_allows(&record, MarketType::Spot, spot_side)?;
        self.emergency_slippage_allows(&record, MarketType::Perp, futures_side)?;
        if !self.update_emergency_exit(intent_id, "emergency derisk attempt checkpoint", |item| {
            item.derisk_attempts = item.derisk_attempts.saturating_add(1)
        }) {
            return Err("emergency derisk attempt was not durable".to_string());
        }

        for (leg, venue, market, side, quantity) in [
            (
                Leg::Spot,
                LegVenue::Spot,
                MarketType::Spot,
                spot_side,
                record.classified_spot_exit_quantity_decimal.clone(),
            ),
            (
                Leg::Futures,
                LegVenue::UsdtFutures,
                MarketType::Perp,
                futures_side,
                record.classified_futures_exit_quantity_decimal.clone(),
            ),
        ] {
            if quantity == "0" {
                continue;
            }
            let latest = self
                .emergency_exits
                .get(intent_id)
                .cloned()
                .ok_or_else(|| "emergency exit disappeared before generation".to_string())?;
            let prior_generations = match leg {
                Leg::Spot => &latest.spot_generations,
                Leg::Futures => &latest.futures_generations,
            };
            if let Some(previous) = prior_generations.last()
                && !is_terminal_internal_status(&previous.final_status)
            {
                return Err(format!(
                    "prior emergency {:?} generation {} is not terminal before residual repair",
                    leg, previous.generation
                ));
            }
            let generation = u16::try_from(prior_generations.len())
                .map_err(|_| "emergency repair generation overflow".to_string())?;
            let client_order_id =
                Self::emergency_repair_client_order_id(intent_id, leg, generation);
            self.internal_orders.insert(
                client_order_id.clone(),
                InternalOrder {
                    client_order_id: client_order_id.clone(),
                    symbol: record.symbol.clone(),
                    status: "SUBMITTING".to_string(),
                    limit_price: None,
                },
            );
            self.order_lineage.insert(
                client_order_id.clone(),
                OrderLineage {
                    intent_id: Some(record.intent_id.clone()),
                    cycle_id: Some(record.intent_id.clone()),
                    leg_id: Some(match leg {
                        Leg::Spot => {
                            format!("{}:emergency:spot:{generation}", record.intent_id)
                        }
                        Leg::Futures => {
                            format!("{}:emergency:futures:{generation}", record.intent_id)
                        }
                    }),
                    market: Some(market),
                    side: Some(match side {
                        TradeSide::Buy => "BUY".to_string(),
                        TradeSide::Sell => "SELL".to_string(),
                    }),
                    requested_quantity_decimal: Some(record.requested_quantity_decimal.clone()),
                    actual_spot_inventory_decimal: Some(
                        record.actual_spot_inventory_decimal.clone(),
                    ),
                    actual_futures_inventory_decimal: Some(
                        record.actual_futures_inventory_decimal.clone(),
                    ),
                    exit_spot_quantity_decimal: Some(
                        record.classified_spot_exit_quantity_decimal.clone(),
                    ),
                    exit_futures_quantity_decimal: Some(
                        record.classified_futures_exit_quantity_decimal.clone(),
                    ),
                    ..OrderLineage::default()
                },
            );
            self.order_cumulative_fills
                .entry(client_order_id.clone())
                .or_insert(0.0);
            if !self.update_emergency_exit(
                intent_id,
                "emergency deterministic order before submission",
                |item| {
                    item.submit_attempts = item.submit_attempts.saturating_add(1);
                    let repair = EmergencyRepairGeneration {
                        leg,
                        generation,
                        client_order_id: client_order_id.clone(),
                        requested_quantity_decimal: quantity.clone(),
                        cumulative_filled_decimal: "0".to_string(),
                        final_status: "SUBMITTING".to_string(),
                    };
                    match leg {
                        Leg::Spot => {
                            item.spot_generation = generation;
                            item.spot_repair_client_order_id = client_order_id.clone();
                            item.spot_generations.push(repair);
                        }
                        Leg::Futures => {
                            item.futures_generation = generation;
                            item.futures_repair_client_order_id = client_order_id.clone();
                            item.futures_generations.push(repair);
                        }
                    }
                },
            ) {
                return Err("emergency submission was not durable".to_string());
            }

            if self.trading_mode != "paper" {
                self.place_emergency_market_order_pumped(
                    venue,
                    &record.symbol,
                    side,
                    &quantity,
                    &client_order_id,
                    record.max_retries,
                    record.readback_budget,
                )
                .await?;
            }
            if let Some(order) = self.internal_orders.get_mut(&client_order_id) {
                order.status = if self.trading_mode == "paper" {
                    "FILLED".to_string()
                } else {
                    "ACKNOWLEDGED".to_string()
                };
            }
            let exact_quantity = Self::canonical_exact(Some(&quantity))
                .ok_or_else(|| "submitted emergency quantity lost exactness".to_string())?;
            let is_paper = self.trading_mode == "paper";
            if !self.update_emergency_exit(
                intent_id,
                "emergency deterministic order acknowledged",
                |item| match leg {
                    Leg::Spot => {
                        item.spot_submission_confirmed = true;
                        if is_paper {
                            if let Some(generation) = item.spot_generations.last_mut() {
                                generation.cumulative_filled_decimal = exact_quantity.to_string();
                                generation.final_status = "FILLED".to_string();
                            }
                            let cumulative = Self::canonical_exact(Some(
                                &item.cumulative_spot_emergency_filled_decimal,
                            ))
                            .unwrap_or(ExactDecimal::ZERO);
                            item.cumulative_spot_emergency_filled_decimal = cumulative
                                .checked_add(exact_quantity)
                                .unwrap_or(cumulative)
                                .to_string();
                            let total =
                                Self::canonical_exact(Some(&item.signed_spot_total_decimal))
                                    .unwrap_or(ExactDecimal::ZERO);
                            let available =
                                Self::canonical_exact(Some(&item.signed_spot_available_decimal))
                                    .unwrap_or(ExactDecimal::ZERO);
                            if side == TradeSide::Sell {
                                item.signed_spot_total_decimal = total
                                    .checked_sub(exact_quantity)
                                    .unwrap_or(ExactDecimal::ZERO)
                                    .to_string();
                                item.signed_spot_available_decimal = available
                                    .checked_sub(exact_quantity)
                                    .unwrap_or(ExactDecimal::ZERO)
                                    .to_string();
                            } else {
                                item.signed_spot_total_decimal = total
                                    .checked_add(exact_quantity)
                                    .unwrap_or(total)
                                    .to_string();
                                item.signed_spot_available_decimal = available
                                    .checked_add(exact_quantity)
                                    .unwrap_or(available)
                                    .to_string();
                            }
                        }
                    }
                    Leg::Futures => {
                        item.futures_submission_confirmed = true;
                        if is_paper {
                            if let Some(generation) = item.futures_generations.last_mut() {
                                generation.cumulative_filled_decimal = exact_quantity.to_string();
                                generation.final_status = "FILLED".to_string();
                            }
                            let cumulative = Self::canonical_exact(Some(
                                &item.cumulative_futures_emergency_filled_decimal,
                            ))
                            .unwrap_or(ExactDecimal::ZERO);
                            item.cumulative_futures_emergency_filled_decimal = cumulative
                                .checked_add(exact_quantity)
                                .unwrap_or(cumulative)
                                .to_string();
                            let signed = Self::canonical_signed_exact(Some(
                                &item.signed_futures_position_decimal,
                            ))
                            .unwrap_or(ExactDecimal::ZERO);
                            item.signed_futures_position_decimal = if side == TradeSide::Buy {
                                signed.checked_add(exact_quantity).unwrap_or(signed)
                            } else {
                                signed.checked_sub(exact_quantity).unwrap_or(signed)
                            }
                            .to_string();
                        }
                    }
                },
            ) {
                return Err("emergency submission acknowledgement was not durable".to_string());
            }
        }
        Ok(())
    }

    async fn reconcile_emergency_generation_fills(
        &mut self,
        intent_id: &str,
    ) -> Result<(), String> {
        let record = self
            .emergency_exits
            .get(intent_id)
            .cloned()
            .ok_or_else(|| "emergency exit disappeared before order readback".to_string())?;
        let mut spot_generations = record.spot_generations.clone();
        let mut futures_generations = record.futures_generations.clone();
        if self.trading_mode != "paper" {
            for (venue, generations) in [
                (LegVenue::Spot, &mut spot_generations),
                (LegVenue::UsdtFutures, &mut futures_generations),
            ] {
                for generation in generations.iter_mut() {
                    let mut snapshot = None;
                    let mut last_error = String::new();
                    for _ in 0..record.readback_budget {
                        if !self.update_emergency_exit(
                            intent_id,
                            "emergency generation fill readback checkpoint",
                            |item| {
                                item.readback_attempts = item.readback_attempts.saturating_add(1)
                            },
                        ) {
                            return Err("emergency generation readback was not durable".to_string());
                        }
                        match self
                            .get_order_pumped(venue, &record.symbol, &generation.client_order_id)
                            .await
                        {
                            Ok(body) => match Self::parse_exact_order_snapshot(
                                &body,
                                &generation.client_order_id,
                            ) {
                                Ok(value) => {
                                    snapshot = Some(value);
                                    break;
                                }
                                Err(error) => last_error = error,
                            },
                            Err(error) => last_error = error,
                        }
                    }
                    let snapshot = snapshot.ok_or_else(|| {
                        format!(
                            "repair generation {} readback exhausted: {last_error}",
                            generation.client_order_id
                        )
                    })?;
                    let requested =
                        Self::canonical_exact(Some(&generation.requested_quantity_decimal))
                            .ok_or_else(|| {
                                "repair generation request lost exactness".to_string()
                            })?;
                    if snapshot.cumulative_filled_qty > requested {
                        return Err(format!(
                            "repair generation {} overfilled its exact budget",
                            generation.client_order_id
                        ));
                    }
                    generation.cumulative_filled_decimal =
                        snapshot.cumulative_filled_qty.to_string();
                    generation.final_status = snapshot.status.as_str().to_string();
                    if let Some(order) = self.internal_orders.get_mut(&generation.client_order_id) {
                        order.status = generation.final_status.clone();
                    }
                }
            }
        }
        let sum = |generations: &[EmergencyRepairGeneration]| -> Result<ExactDecimal, String> {
            generations
                .iter()
                .try_fold(ExactDecimal::ZERO, |total, generation| {
                    let value = Self::canonical_exact(Some(&generation.cumulative_filled_decimal))
                        .ok_or_else(|| "repair generation fill lost exactness".to_string())?;
                    total
                        .checked_add(value)
                        .ok_or_else(|| "repair generation cumulative fill overflowed".to_string())
                })
        };
        let cumulative_spot = sum(&spot_generations)?;
        let cumulative_futures = sum(&futures_generations)?;
        let spot_budget = Self::canonical_exact(Some(&record.exit_spot_quantity_decimal))
            .ok_or_else(|| "emergency spot lifetime budget is invalid".to_string())?;
        let futures_budget = Self::canonical_exact(Some(&record.exit_futures_quantity_decimal))
            .ok_or_else(|| "emergency futures lifetime budget is invalid".to_string())?;
        if cumulative_spot > spot_budget || cumulative_futures > futures_budget {
            return Err(
                "emergency cumulative fills exceeded original bot inventory budget".to_string(),
            );
        }
        if !self.update_emergency_exit(intent_id, "emergency generation cumulative fills", |item| {
            item.spot_generations = spot_generations;
            item.futures_generations = futures_generations;
            item.cumulative_spot_emergency_filled_decimal = cumulative_spot.to_string();
            item.cumulative_futures_emergency_filled_decimal = cumulative_futures.to_string();
        }) {
            return Err("emergency cumulative generation fills were not durable".to_string());
        }
        Ok(())
    }

    async fn emergency_open_orders_clear(
        &mut self,
        record: &EmergencyExitRecord,
    ) -> Result<bool, String> {
        if self.trading_mode == "paper" {
            return Ok(true);
        }
        for venue in [LegVenue::Spot, LegVenue::UsdtFutures] {
            let body = self.get_open_orders_pumped(venue).await?;
            let rows: Vec<Value> = serde_json::from_str(&body)
                .map_err(|error| format!("invalid emergency open-order readback: {error}"))?;
            let repair_ids = record
                .spot_generations
                .iter()
                .chain(record.futures_generations.iter())
                .map(|generation| generation.client_order_id.as_str())
                .collect::<HashSet<_>>();
            if rows.iter().any(|row| {
                row.get("symbol").and_then(Value::as_str) == Some(record.symbol.as_str())
                    && row
                        .get("clientOrderId")
                        .or_else(|| row.get("origClientOrderId"))
                        .and_then(Value::as_str)
                        .is_some_and(|client_id| repair_ids.contains(client_id))
            }) {
                return Ok(false);
            }
        }
        Ok(true)
    }

    async fn verify_emergency_flat(&mut self, intent_id: &str) -> Result<bool, String> {
        self.reconcile_emergency_generation_fills(intent_id).await?;
        let (spot_total, _spot_available, signed_futures) =
            self.signed_emergency_inventory(intent_id).await?;
        let before = self
            .emergency_exits
            .get(intent_id)
            .cloned()
            .ok_or_else(|| "emergency exit disappeared before verification".to_string())?;
        let spot_budget = Self::canonical_exact(Some(&before.exit_spot_quantity_decimal))
            .ok_or_else(|| "emergency spot lifetime budget is invalid".to_string())?;
        let futures_budget = Self::canonical_exact(Some(&before.exit_futures_quantity_decimal))
            .ok_or_else(|| "emergency futures lifetime budget is invalid".to_string())?;
        let cumulative_spot =
            Self::canonical_exact(Some(&before.cumulative_spot_emergency_filled_decimal))
                .ok_or_else(|| "emergency cumulative spot fill is invalid".to_string())?;
        let cumulative_futures =
            Self::canonical_exact(Some(&before.cumulative_futures_emergency_filled_decimal))
                .ok_or_else(|| "emergency cumulative futures fill is invalid".to_string())?;
        let remaining_spot = spot_budget
            .checked_sub(cumulative_spot)
            .filter(|value| *value >= ExactDecimal::ZERO)
            .ok_or_else(|| "emergency spot lifetime budget was exceeded".to_string())?;
        let remaining_futures = futures_budget
            .checked_sub(cumulative_futures)
            .filter(|value| *value >= ExactDecimal::ZERO)
            .ok_or_else(|| "emergency futures lifetime budget was exceeded".to_string())?;
        let initial_spot = Self::canonical_exact(Some(&before.initial_signed_spot_total_decimal))
            .ok_or_else(|| "initial exact spot truth is invalid".to_string())?;
        let initial_futures =
            Self::canonical_signed_exact(Some(&before.initial_signed_futures_position_decimal))
                .ok_or_else(|| "initial exact futures truth is invalid".to_string())?;
        let expected_spot = if before.direction == "EXIT_LONG" {
            initial_spot
                .checked_sub(cumulative_spot)
                .ok_or_else(|| "emergency spot account delta underflowed".to_string())?
        } else {
            initial_spot
                .checked_add(cumulative_spot)
                .ok_or_else(|| "emergency spot account delta overflowed".to_string())?
        };
        let expected_futures = if before.direction == "EXIT_LONG" {
            initial_futures
                .checked_add(cumulative_futures)
                .ok_or_else(|| "emergency futures account delta overflowed".to_string())?
        } else {
            initial_futures
                .checked_sub(cumulative_futures)
                .ok_or_else(|| "emergency futures account delta overflowed".to_string())?
        };
        if spot_total != expected_spot || signed_futures != expected_futures {
            return Err(format!(
                "signed account delta diverged from deterministic emergency fills: spot={spot_total}/{expected_spot}, futures={signed_futures}/{expected_futures}"
            ));
        }
        if !self.update_emergency_exit(intent_id, "signed flat-verification result", |item| {
            item.verify_attempts = item.verify_attempts.saturating_add(1);
            item.verified_spot_inventory_decimal = remaining_spot.to_string();
            item.verified_futures_inventory_decimal = remaining_futures.to_string();
            item.signed_spot_total_decimal = spot_total.to_string();
            item.signed_futures_position_decimal = signed_futures.to_string();
        }) {
            return Err("signed flat verification was not durable".to_string());
        }
        let record = self
            .emergency_exits
            .get(intent_id)
            .cloned()
            .ok_or_else(|| "emergency exit disappeared after verification".to_string())?;
        let all_generations_terminal = record
            .spot_generations
            .iter()
            .chain(record.futures_generations.iter())
            .all(|generation| is_terminal_internal_status(&generation.final_status));
        Ok(remaining_spot == ExactDecimal::ZERO
            && remaining_futures == ExactDecimal::ZERO
            && all_generations_terminal
            && self.emergency_open_orders_clear(&record).await?)
    }

    async fn drive_emergency_exit(&mut self, intent_id: String) {
        loop {
            let Some(record) = self.emergency_exits.get(&intent_id).cloned() else {
                return;
            };
            match record.state {
                EmergencyExitState::Detected => {
                    if !self.transition_emergency_exit(
                        &intent_id,
                        EmergencyExitState::CancelingCurrentOrders,
                        "begin canceling all current symbol orders",
                    ) {
                        self.fail_emergency_exit(&intent_id, "cancel transition was not durable");
                        return;
                    }
                }
                EmergencyExitState::CancelingCurrentOrders => {
                    if let Err(error) = self.cancel_current_orders_for_emergency(&intent_id).await {
                        self.fail_emergency_exit(&intent_id, &error);
                        return;
                    }
                    if !self.transition_emergency_exit(
                        &intent_id,
                        EmergencyExitState::SignedReadback,
                        "current orders terminal; request signed inventory truth",
                    ) {
                        self.fail_emergency_exit(&intent_id, "readback transition was not durable");
                        return;
                    }
                }
                EmergencyExitState::SignedReadback => {
                    let (spot_total, spot_available, signed_futures) =
                        match self.signed_emergency_inventory(&intent_id).await {
                            Ok(value) => value,
                            Err(error) => {
                                self.fail_emergency_exit(&intent_id, &error);
                                return;
                            }
                        };
                    if !self.update_emergency_exit(
                        &intent_id,
                        "signed emergency inventory truth",
                        |item| {
                            item.signed_spot_total_decimal = spot_total.to_string();
                            item.signed_spot_available_decimal = spot_available.to_string();
                            item.signed_futures_position_decimal = signed_futures.to_string();
                            if !item.initial_inventory_captured {
                                item.initial_signed_spot_total_decimal = spot_total.to_string();
                                item.initial_signed_futures_position_decimal =
                                    signed_futures.to_string();
                                item.initial_inventory_captured = true;
                            }
                        },
                    ) || !self.transition_emergency_exit(
                        &intent_id,
                        EmergencyExitState::InventoryClassified,
                        "signed inventory durably classified",
                    ) {
                        self.fail_emergency_exit(
                            &intent_id,
                            "inventory classification transition was not durable",
                        );
                        return;
                    }
                }
                EmergencyExitState::InventoryClassified => {
                    if let Err(error) = self.classify_emergency_inventory(&intent_id) {
                        self.fail_emergency_exit(&intent_id, &error);
                        return;
                    }
                    if !self.transition_emergency_exit(
                        &intent_id,
                        EmergencyExitState::ReduceOnlyDerisking,
                        "safe inventory-limited quantities are durable",
                    ) {
                        self.fail_emergency_exit(&intent_id, "derisk transition was not durable");
                        return;
                    }
                }
                EmergencyExitState::ReduceOnlyDerisking => {
                    if let Err(error) = self.submit_emergency_derisk(&intent_id).await {
                        self.fail_emergency_exit(&intent_id, &error);
                        return;
                    }
                    if !self.transition_emergency_exit(
                        &intent_id,
                        EmergencyExitState::VerifyingFlat,
                        "reduce-only orders reconciled; verify signed flat",
                    ) {
                        self.fail_emergency_exit(
                            &intent_id,
                            "verification transition was not durable",
                        );
                        return;
                    }
                }
                EmergencyExitState::VerifyingFlat => match self
                    .verify_emergency_flat(&intent_id)
                    .await
                {
                    Ok(true) => {
                        if let Some(symbol) = self
                            .emergency_exits
                            .get(&intent_id)
                            .map(|item| item.symbol.clone())
                        {
                            self.chase_states.remove(&symbol);
                            self.chase_intent_ids.remove(&symbol);
                            self.chase_unhedged_budgets.remove(&symbol);
                            self.chase_unhedged_started_at_ms.remove(&symbol);
                        }
                        if !self.transition_emergency_exit(
                            &intent_id,
                            EmergencyExitState::Flat,
                            "signed balances, positions, and repair orders prove flat",
                        ) {
                            self.fail_emergency_exit(&intent_id, "flat transition was not durable");
                            return;
                        }
                        if record.autonomous_risk_sequence.is_none() {
                            let _ = self.transition_intent_ack(
                                &intent_id,
                                "TERMINAL",
                                "emergency_reduce_only_flat",
                            );
                        }
                        return;
                    }
                    Ok(false) => {
                        if record.derisk_attempts <= record.max_retries {
                            if !self.transition_emergency_exit(
                                &intent_id,
                                EmergencyExitState::SignedReadback,
                                "signed verification found residual inventory; retry budget remains",
                            ) {
                                self.fail_emergency_exit(
                                    &intent_id,
                                    "residual retry transition was not durable",
                                );
                                return;
                            }
                        } else {
                            self.fail_emergency_exit(
                                &intent_id,
                                "signed flat verification exhausted derisk retry budget",
                            );
                            return;
                        }
                    }
                    Err(error) => {
                        self.fail_emergency_exit(&intent_id, &error);
                        return;
                    }
                },
                EmergencyExitState::Flat | EmergencyExitState::ManualReview => return,
            }
        }
    }

    async fn resume_emergency_exits(&mut self) {
        let pending = self
            .emergency_exits
            .iter()
            .filter(|(_, record)| !record.state.is_terminal())
            .map(|(intent_id, _)| intent_id.clone())
            .collect::<Vec<_>>();
        for intent_id in pending {
            self.drive_emergency_exit(intent_id).await;
        }
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

        self.rearm_recovered_cycle_deadlines();
        self.replay_terminal_publications();
        self.resume_emergency_exits().await;
        self.reevaluate_continuous_risk("startup").await;

        loop {
            let event = match self.deferred_actor_events.pop_front() {
                Some(event) => Some(event),
                None => self.event_receiver.recv().await,
            };
            let Some(event) = event else { break };
            let risk_trigger = match &event {
                EngineEvent::Ws(_) => None,
                EngineEvent::Alpha(_) => Some("alpha_instruction"),
                EngineEvent::LeggingTimeout(_) => Some("legging_timeout"),
                EngineEvent::CycleDeadline { .. } => Some("cycle_deadline"),
                EngineEvent::StrategyTick => Some("strategy_tick"),
                EngineEvent::PositionAuditTick => Some("position_audit"),
                EngineEvent::ExchangeInfoRefreshResult(_) => Some("exchange_metadata_refresh"),
                EngineEvent::RecoveryBarrier { .. } | EngineEvent::RecoveryBarrierFailed { .. } => {
                    None
                }
            };
            match event {
                EngineEvent::Ws(ws_event) => self.process_ws_event(*ws_event).await,
                EngineEvent::Alpha(alpha_instruction) => {
                    self.handle_alpha_instruction(*alpha_instruction).await;
                }
                EngineEvent::LeggingTimeout(client_id) => {
                    self.handle_legging_timeout(client_id).await;
                }
                EngineEvent::CycleDeadline {
                    cycle_client_order_id,
                    deadline_at_ms,
                } => {
                    self.handle_cycle_deadline(cycle_client_order_id, deadline_at_ms)
                        .await;
                }
                EngineEvent::StrategyTick => {
                    self.tick_strategy().await;
                }
                EngineEvent::PositionAuditTick => {
                    if self.state != SystemState::Trading
                        && self.private_stream_quorum_ready()
                        && self.public_stream_recovery_symbols.is_empty()
                    {
                        self.execute_reconciliation_sequence().await;
                    } else {
                        self.runtime_position_audit().await;
                    }
                }
                EngineEvent::ExchangeInfoRefreshResult(result) => {
                    self.apply_exchange_info_refresh(result);
                }
                EngineEvent::RecoveryBarrier {
                    request_id,
                    reply,
                    release,
                    resumed,
                } => {
                    self.handle_recovery_barrier_event(request_id, reply, release, resumed)
                        .await;
                }
                EngineEvent::RecoveryBarrierFailed { request_id, reason } => {
                    self.fail_recovery_barrier(&request_id, &reason);
                }
            }
            if let Some(trigger) = risk_trigger {
                self.reevaluate_continuous_risk(trigger).await;
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
        // Order updates are published inside handle_ws_event only after their
        // cumulative fill, inventory, fee, lineage, and order status are
        // durably persisted. Market-data events remain publish-before-apply.
        if !matches!(
            &ws_event,
            WsEvent::OrderUpdate { .. } | WsEvent::TerminalPublicationPersisted { .. }
        ) && let Ok(vec) = rmp_serde::to_vec_named(&ws_event)
        {
            let _ = self.dash_tx.send(vec);
        }
        // REST waits can recursively pump another websocket event. Box the
        // state transition future so that recursion has an explicit boundary.
        Box::pin(self.handle_ws_event(ws_event)).await;
        Box::pin(self.reevaluate_continuous_risk("websocket_event")).await;
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
                            Box::pin(self.process_ws_event(*ws_event)).await;
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
        self.critical_quota_guard()?;
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

    #[allow(clippy::too_many_arguments)]
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
        self.critical_quota_guard()?;
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
        self.critical_quota_guard()?;
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

    #[allow(clippy::too_many_arguments)]
    async fn place_emergency_market_order_pumped(
        &mut self,
        venue: LegVenue,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
        max_retries: u16,
        readback_attempts: u16,
    ) -> Result<ReconciledSubmission, String> {
        self.critical_quota_guard()?;
        let rest = self.binance_rest.clone();
        let symbol = symbol.to_string();
        let quantity = quantity.to_string();
        let client_order_id = client_order_id.to_string();
        self.await_rest_while_processing_ws(async move {
            match venue {
                LegVenue::Spot => {
                    rest.place_spot_market_order_read_before_retry_with_budget(
                        &symbol,
                        side,
                        &quantity,
                        &client_order_id,
                        max_retries,
                        readback_attempts,
                    )
                    .await
                }
                LegVenue::UsdtFutures => {
                    rest.place_futures_market_order_read_before_retry_with_budget(
                        &symbol,
                        side,
                        &quantity,
                        &client_order_id,
                        true,
                        max_retries,
                        readback_attempts,
                    )
                    .await
                }
            }
        })
        .await
    }

    async fn get_open_orders_pumped(&mut self, venue: LegVenue) -> Result<String, String> {
        self.critical_quota_guard()?;
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(
            async move { rest.get_open_orders_for_venue(venue).await },
        )
        .await
    }

    async fn get_spot_account_pumped(&mut self) -> Result<String, String> {
        self.critical_quota_guard()?;
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(async move { rest.get_account().await })
            .await
    }

    async fn get_futures_account_pumped(&mut self) -> Result<String, String> {
        self.critical_quota_guard()?;
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(async move { rest.get_fapi_account().await })
            .await
    }

    async fn get_futures_positions_pumped(&mut self) -> Result<String, String> {
        self.critical_quota_guard()?;
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(async move { rest.get_fapi_position_risk().await })
            .await
    }

    async fn get_futures_position_mode_pumped(&mut self) -> Result<String, String> {
        self.critical_quota_guard()?;
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(async move { rest.get_fapi_position_mode().await })
            .await
    }

    async fn get_futures_funding_income_pumped(
        &mut self,
        start_time_ms: i64,
        end_time_ms: i64,
    ) -> Result<String, String> {
        self.critical_quota_guard()?;
        let rest = self.binance_rest.clone();
        self.await_rest_while_processing_ws(async move {
            rest.get_futures_funding_income_history(start_time_ms, end_time_ms, 1000)
                .await
        })
        .await
    }

    fn validate_funding_income_history(body: &str) -> Result<usize, String> {
        let rows: Vec<Value> = serde_json::from_str(body)
            .map_err(|err| format!("invalid futures funding-income JSON: {err}"))?;
        if rows.len() >= 1000 {
            return Err(
                "futures funding-income history may be truncated at page limit".to_string(),
            );
        }
        for row in &rows {
            let asset_valid = row
                .get("asset")
                .and_then(Value::as_str)
                .is_some_and(|value| !value.trim().is_empty());
            let income_valid = row
                .get("income")
                .and_then(|node| {
                    node.as_f64()
                        .or_else(|| node.as_str().and_then(|raw| raw.parse::<f64>().ok()))
                })
                .is_some_and(f64::is_finite);
            let time_valid = row
                .get("time")
                .and_then(|node| {
                    node.as_i64()
                        .or_else(|| node.as_u64().and_then(|value| i64::try_from(value).ok()))
                })
                .is_some_and(|value| value >= 0);
            if !asset_valid || !income_valid || !time_valid {
                return Err("futures funding-income history contains an invalid row".to_string());
            }
        }
        Ok(rows.len())
    }

    async fn sync_time_pumped(&mut self) -> Result<(), String> {
        self.critical_quota_guard()?;
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
        self.critical_quota_guard()?;
        let rest = self.binance_rest.clone();
        let symbol = symbol.to_string();
        let client_order_id = client_order_id.to_string();
        self.await_rest_while_processing_ws(async move {
            rest.get_order_by_client_id(venue, &symbol, &client_order_id)
                .await
        })
        .await
    }

    fn parse_terminal_order_snapshot(
        body: &str,
        expected_client_order_id: &str,
    ) -> Result<TerminalOrderSnapshot, String> {
        let value: Value = serde_json::from_str(body)
            .map_err(|err| format!("invalid cancel/order response JSON: {err}"))?;
        if let Some(observed_client_id) = value
            .get("clientOrderId")
            .or_else(|| value.get("origClientOrderId"))
            .and_then(Value::as_str)
            && observed_client_id != expected_client_order_id
        {
            return Err(format!(
                "cancel/order response client id mismatch: expected {expected_client_order_id}, got {observed_client_id}"
            ));
        }
        let status_raw = value
            .get("status")
            .and_then(Value::as_str)
            .ok_or_else(|| "cancel/order response is missing status".to_string())?;
        let status = ExchangeOrderStatus::parse(status_raw)
            .ok_or_else(|| format!("unsupported exchange order status {status_raw}"))?;
        if !status.is_terminal() {
            return Err(format!(
                "cancel/order response is not terminal: {status_raw}"
            ));
        }
        let cumulative_filled_qty = value
            .get("executedQty")
            .and_then(|node| {
                node.as_f64()
                    .or_else(|| node.as_str().and_then(|raw| raw.parse::<f64>().ok()))
            })
            .filter(|quantity| quantity.is_finite() && *quantity >= 0.0)
            .ok_or_else(|| "cancel/order response is missing valid executedQty".to_string())?;
        let average_fill_price = value
            .get("avgPrice")
            .or_else(|| value.get("price"))
            .and_then(|node| {
                node.as_f64()
                    .or_else(|| node.as_str().and_then(|raw| raw.parse::<f64>().ok()))
            })
            .filter(|price| price.is_finite() && *price > 0.0);
        Ok(TerminalOrderSnapshot {
            status,
            cumulative_filled_qty,
            average_fill_price,
        })
    }

    fn parse_exact_order_snapshot(
        body: &str,
        expected_client_order_id: &str,
    ) -> Result<ExactOrderSnapshot, String> {
        let value: Value = serde_json::from_str(body)
            .map_err(|error| format!("invalid exact order response JSON: {error}"))?;
        let observed_client_id = value
            .get("clientOrderId")
            .or_else(|| value.get("origClientOrderId"))
            .and_then(Value::as_str)
            .ok_or_else(|| "exact order response is missing client id".to_string())?;
        if observed_client_id != expected_client_order_id {
            return Err(format!(
                "exact order response client id mismatch: expected {expected_client_order_id}, got {observed_client_id}"
            ));
        }
        let status_raw = value
            .get("status")
            .and_then(Value::as_str)
            .ok_or_else(|| "exact order response is missing status".to_string())?;
        let status = ExchangeOrderStatus::parse(status_raw)
            .ok_or_else(|| format!("unsupported exact order status {status_raw}"))?;
        let cumulative_filled_qty = Self::parse_canonical_exchange_decimal(
            value.get("executedQty"),
            "order executedQty",
            false,
        )?;
        Ok(ExactOrderSnapshot {
            status,
            cumulative_filled_qty,
        })
    }

    fn apply_terminal_order_snapshot(
        &mut self,
        chase: &mut ChaseState,
        leg: Leg,
        client_order_id: &str,
        snapshot: TerminalOrderSnapshot,
    ) -> Result<(), String> {
        let previous_order_cumulative = self
            .order_cumulative_fills
            .get(client_order_id)
            .copied()
            .unwrap_or(0.0);
        let target = chase.target_for(leg);
        let tolerance = target.abs().mul_add(1e-9, 1e-12);
        if snapshot.cumulative_filled_qty + tolerance < previous_order_cumulative {
            return Err(format!(
                "terminal snapshot regressed cumulative fill for {client_order_id}: {:.12} < {:.12}",
                snapshot.cumulative_filled_qty, previous_order_cumulative
            ));
        }
        let delta = (snapshot.cumulative_filled_qty - previous_order_cumulative).max(0.0);
        if delta > tolerance {
            let fallback_price = match leg {
                Leg::Spot => chase.expected_spot_price,
                Leg::Futures => chase.expected_fut_price,
            };
            let fill_price = snapshot.average_fill_price.unwrap_or(fallback_price);
            let (market, side) = match leg {
                Leg::Spot => (MarketType::Spot, chase.spot_side),
                Leg::Futures => (MarketType::Perp, chase.futures_side),
            };
            self.apply_fill_to_position(
                &chase.symbol,
                market,
                side,
                delta,
                fill_price,
                chase.is_exit,
            );
            match leg {
                Leg::Spot => chase.spot_fill_price = Some(fill_price),
                Leg::Futures => chase.futures_fill_price = Some(fill_price),
            }
        }
        self.order_cumulative_fills
            .insert(client_order_id.to_string(), snapshot.cumulative_filled_qty);
        let updated_cycle_cumulative = chase.cumulative_for(leg) + delta;
        if updated_cycle_cumulative > target + tolerance {
            return Err(format!(
                "terminal snapshot overfilled logical {:?} leg: {:.12} > {:.12}",
                leg, updated_cycle_cumulative, target
            ));
        }
        let target_reached = (updated_cycle_cumulative - target).abs() <= tolerance;
        chase.set_progress(
            leg,
            updated_cycle_cumulative,
            snapshot.status.is_filled() && target_reached,
        );
        if let Some(order) = self.internal_orders.get_mut(client_order_id) {
            order.status = match snapshot.status {
                ExchangeOrderStatus::Filled => "FILLED",
                ExchangeOrderStatus::Canceled => "CANCELED",
                ExchangeOrderStatus::Rejected => "REJECTED",
                ExchangeOrderStatus::Expired => "EXPIRED",
                ExchangeOrderStatus::ExpiredInMatch => "EXPIRED_IN_MATCH",
                ExchangeOrderStatus::New => "NEW",
                ExchangeOrderStatus::PartiallyFilled => "PARTIALLY_FILLED",
                ExchangeOrderStatus::PendingCancel => "PENDING_CANCEL",
            }
            .to_string();
        }
        Ok(())
    }

    async fn handle_cycle_deadline(&mut self, cycle_client_order_id: String, deadline_at_ms: i64) {
        if self.cycle_deadlines.get(&cycle_client_order_id).copied() != Some(deadline_at_ms) {
            return;
        }
        if Self::current_time_ms() < deadline_at_ms {
            self.schedule_cycle_deadline(cycle_client_order_id, deadline_at_ms);
            return;
        }
        let Some((symbol, mut chase)) = self
            .chase_states
            .iter()
            .find(|(_, chase)| chase.cycle_client_order_id() == cycle_client_order_id)
            .map(|(symbol, chase)| (symbol.clone(), chase.clone()))
        else {
            self.cycle_deadlines.remove(&cycle_client_order_id);
            let _ = self.persist_execution_state("removed orphaned absolute cycle deadline");
            return;
        };
        if matches!(
            chase.phase,
            ChasePhase::Completed | ChasePhase::ReconciliationRequired
        ) {
            return;
        }
        chase.phase = ChasePhase::DeadlineFreezing;
        if !self.store_chase_state(
            symbol.clone(),
            chase.clone(),
            "absolute cycle deadline stopped maker activity",
        ) {
            self.require_chase_reconciliation(
                &symbol,
                chase,
                &cycle_client_order_id,
                "CYCLE_DEADLINE_PHASE_NOT_DURABLE",
            );
            return;
        }
        self.handle_partial_fill_deadline(symbol, chase).await;
    }

    async fn handle_partial_fill_deadline(&mut self, symbol: String, mut chase: ChaseState) {
        for leg in [Leg::Spot, Leg::Futures] {
            if (leg == Leg::Spot && !chase.has_spot_leg())
                || (leg == Leg::Futures && !chase.has_futures_leg())
            {
                continue;
            }
            let client_order_id = chase.active_client_order_id(leg).to_string();
            let status = self
                .internal_orders
                .get(&client_order_id)
                .map(|order| order.status.as_str())
                .unwrap_or("UNKNOWN");
            if status == "PENDING_SUBMIT" {
                if let Some(order) = self.internal_orders.get_mut(&client_order_id) {
                    order.status = "NOT_SUBMITTED".to_string();
                }
                continue;
            }
            let already_terminal = is_terminal_internal_status(status);
            if already_terminal && self.trading_mode == "paper" {
                continue;
            }
            let venue = match leg {
                Leg::Spot => LegVenue::Spot,
                Leg::Futures => LegVenue::UsdtFutures,
            };
            let cancel_result = if already_terminal {
                Ok(String::new())
            } else {
                match leg {
                    Leg::Spot => {
                        self.cancel_order_pumped(LegVenue::Spot, &chase.symbol, &client_order_id)
                            .await
                    }
                    Leg::Futures => {
                        self.cancel_order_pumped(
                            LegVenue::UsdtFutures,
                            &chase.symbol,
                            &client_order_id,
                        )
                        .await
                    }
                }
            };
            if let Err(err) = cancel_result.as_ref() {
                warn!(
                    "Cycle deadline cancel for {:?} generation {} returned {}; signed readback will decide the outcome",
                    leg, client_order_id, err
                );
            }
            // cancel_order_pumped deliberately processes private fills while
            // REST is in flight. Merge progress into the latest cycle;
            // retaining the pre-await clone here can erase fill progress.
            let Some(latest) = self.chase_states.get(&symbol).cloned() else {
                // The nested fill path can legitimately finish and durably
                // remove the cycle while the cancel response is in flight.
                return;
            };
            if latest.leg_for_client_order_id(&client_order_id).is_none() {
                self.require_cycle_deadline_reconciliation(
                    &symbol,
                    latest,
                    &client_order_id,
                    "PARTIAL_FILL_CANCEL_LINEAGE_CHANGED",
                );
                return;
            }
            chase = latest;
            let snapshot_result = if self.trading_mode == "paper" {
                Ok(TerminalOrderSnapshot {
                    status: ExchangeOrderStatus::Canceled,
                    cumulative_filled_qty: self
                        .order_cumulative_fills
                        .get(&client_order_id)
                        .copied()
                        .unwrap_or(0.0),
                    average_fill_price: None,
                })
            } else {
                match self
                    .get_order_pumped(venue, &chase.symbol, &client_order_id)
                    .await
                {
                    Ok(body) => Self::parse_terminal_order_snapshot(&body, &client_order_id),
                    Err(readback_err) => Err(format!(
                        "signed readback failed after cancel outcome {:?}: {}",
                        cancel_result.err(),
                        readback_err
                    )),
                }
            };
            let Some(latest) = self.chase_states.get(&symbol).cloned() else {
                // A private fill processed during signed readback can complete
                // and durably remove the cycle.
                return;
            };
            if latest.leg_for_client_order_id(&client_order_id).is_none() {
                self.require_cycle_deadline_reconciliation(
                    &symbol,
                    latest,
                    &client_order_id,
                    "PARTIAL_FILL_READBACK_LINEAGE_CHANGED",
                );
                return;
            }
            chase = latest;
            let snapshot = match snapshot_result {
                Ok(snapshot) => snapshot,
                Err(err) => {
                    error!(
                        "Cycle deadline received an unusable signed readback for {}: {}",
                        client_order_id, err
                    );
                    self.require_cycle_deadline_reconciliation(
                        &symbol,
                        chase,
                        &client_order_id,
                        "PARTIAL_FILL_SIGNED_READBACK_INVALID",
                    );
                    return;
                }
            };
            if let Err(err) =
                self.apply_terminal_order_snapshot(&mut chase, leg, &client_order_id, snapshot)
            {
                error!(
                    "Partial-fill deadline could not aggregate terminal snapshot for {}: {}",
                    client_order_id, err
                );
                self.require_cycle_deadline_reconciliation(
                    &symbol,
                    chase,
                    &client_order_id,
                    "PARTIAL_FILL_CANCEL_AGGREGATION_FAILED",
                );
                return;
            }
            if !self.store_chase_state(
                symbol.clone(),
                chase.clone(),
                "partial-fill cancel snapshot aggregated",
            ) {
                self.require_cycle_deadline_reconciliation(
                    &symbol,
                    chase,
                    &client_order_id,
                    "PARTIAL_FILL_CANCEL_NOT_DURABLE",
                );
                return;
            }
        }

        let classification = Self::classify_cycle_deadline(&chase);
        if !self.record_cycle_deadline_classification(&chase, classification) {
            self.require_chase_reconciliation(
                &symbol,
                chase,
                "",
                "CYCLE_DEADLINE_CLASSIFICATION_NOT_DURABLE",
            );
            return;
        }

        if chase.is_exit {
            // Never redefine a partial exit as a smaller completed cycle. The
            // original target remains durable so a subsequent authoritative
            // reconcile/repair can close the full requested exposure.
            self.require_chase_reconciliation(
                &symbol,
                chase,
                "",
                "PARTIAL_EXIT_REMAINS_AFTER_HEDGE_DEADLINE",
            );
            return;
        }

        if classification == CycleDeadlineClassification::Flat {
            if !self.emit_cycle_order_update(
                &chase,
                "CANCELED",
                chase.cycle_client_order_id(),
                0.0,
                false,
                "CYCLE_DEADLINE_FLAT",
            ) {
                self.require_chase_reconciliation(&symbol, chase, "", "TERMINAL_ACK_NOT_DURABLE");
                return;
            }
            self.remove_chase_state(&symbol, "zero-fill entry deadline classified flat");
            return;
        }

        if classification == CycleDeadlineClassification::Unknown {
            self.require_chase_reconciliation(
                &symbol,
                chase,
                "",
                "CYCLE_DEADLINE_INVENTORY_UNKNOWN",
            );
            return;
        }

        let reduced_target = chase
            .spot_cumulative_filled
            .max(chase.futures_cumulative_filled);
        if !reduced_target.is_finite() || reduced_target <= 0.0 {
            self.require_chase_reconciliation(
                &symbol,
                chase,
                "",
                "PARTIAL_FILL_DEADLINE_WITHOUT_DURABLE_FILL",
            );
            return;
        }
        chase.spot_quantity = reduced_target;
        chase.perp_quantity = reduced_target;
        let tolerance = reduced_target.mul_add(1e-9, 1e-12);
        chase.spot_terminal = (chase.spot_cumulative_filled - reduced_target).abs() <= tolerance;
        chase.futures_terminal =
            (chase.futures_cumulative_filled - reduced_target).abs() <= tolerance;

        if chase.both_legs_terminal() {
            chase.phase = ChasePhase::Completed;
            if !self.emit_cycle_order_update(
                &chase,
                "FILLED",
                chase.cycle_client_order_id(),
                reduced_target,
                false,
                "FILLED_CYCLE",
            ) {
                self.require_chase_reconciliation(&symbol, chase, "", "TERMINAL_ACK_NOT_DURABLE");
                return;
            }
            self.remove_chase_state(&symbol, "partial entry completed at reduced neutral size");
            return;
        }

        let ahead_leg = if chase.spot_cumulative_filled >= chase.futures_cumulative_filled {
            Leg::Spot
        } else {
            Leg::Futures
        };
        chase.phase = ChasePhase::LegFilledWaiting(ahead_leg);
        let trigger = chase.active_client_order_id(ahead_leg).to_string();
        let _ = self.store_chase_state(
            symbol.clone(),
            chase,
            "partial-fill generations frozen before residual repair",
        );
        Box::pin(self.handle_legging_timeout(trigger)).await;
    }

    async fn tick_strategy(&mut self) {
        if let Some(reason) = self
            .binance_rest
            .quota_block_reason(RestWorkClass::Nonessential)
        {
            warn!("Strategy research refresh shed to preserve exchange quota: {reason}");
            Self::emit_exchange_quota_snapshot(&self.binance_rest, &self.dash_tx);
            return;
        }
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
            .find(|(_, chase)| chase.leg_for_client_order_id(&trigger_client_id).is_some())
            .map(|(k, _)| k.clone());
        let Some(symbol) = symbol else { return };
        let Some(mut chase) = self.chase_states.get(&symbol).cloned() else {
            return;
        };

        let first_filled_leg = match chase.phase {
            ChasePhase::LegFilledWaiting(leg) => leg,
            _ => return,
        };

        if !chase.terminal_for(first_filled_leg) {
            self.handle_partial_fill_deadline(symbol, chase).await;
            return;
        }

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
        let initial_remaining_quantity =
            (chase.target_for(unfilled_leg) - chase.cumulative_for(unfilled_leg)).max(0.0);
        let quantity_tolerance = chase.target_for(unfilled_leg).abs().mul_add(1e-9, 1e-12);
        if initial_remaining_quantity <= quantity_tolerance {
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
            .map(|order| is_terminal_internal_status(&order.status))
            .unwrap_or(false);
        let cancel_result = if already_terminal || submission_not_started {
            Ok(None)
        } else {
            match unfilled_leg {
                Leg::Spot => self
                    .cancel_order_pumped(LegVenue::Spot, &unfilled_sym, &unfilled_cid)
                    .await
                    .map(Some),
                Leg::Futures => self
                    .cancel_order_pumped(LegVenue::UsdtFutures, &unfilled_sym, &unfilled_cid)
                    .await
                    .map(Some),
            }
        };
        let cancel_body = match cancel_result {
            Ok(body) => body,
            Err(err) => {
                let latest = self.chase_states.get(&symbol).cloned().unwrap_or(chase);
                error!(
                    "Cannot replace hedge order {} for {} because cancel was not confirmed: {}",
                    unfilled_cid, unfilled_sym, err
                );
                self.require_chase_reconciliation(
                    &symbol,
                    latest,
                    &unfilled_cid,
                    "HEDGE_CANCEL_UNCONFIRMED",
                );
                return;
            }
        };
        if let Some(body) = cancel_body {
            // The REST wait pumps private fills and even other hedge-deadline
            // events. Rebase onto the latest durable cycle before aggregating
            // this concrete generation's terminal snapshot.
            let Some(latest) = self.chase_states.get(&symbol).cloned() else {
                return;
            };
            if latest.leg_for_client_order_id(&unfilled_cid).is_none() {
                self.require_chase_reconciliation(
                    &symbol,
                    latest,
                    &unfilled_cid,
                    "HEDGE_CANCEL_LINEAGE_CHANGED",
                );
                return;
            }
            chase = latest;
            let snapshot_result = if self.trading_mode == "paper" {
                Ok(TerminalOrderSnapshot {
                    status: ExchangeOrderStatus::Canceled,
                    cumulative_filled_qty: self
                        .order_cumulative_fills
                        .get(&unfilled_cid)
                        .copied()
                        .unwrap_or(0.0),
                    average_fill_price: None,
                })
            } else {
                Self::parse_terminal_order_snapshot(&body, &unfilled_cid)
            };
            let snapshot = match snapshot_result {
                Ok(snapshot) => snapshot,
                Err(err) => {
                    error!(
                        "Cannot replace hedge order {} because cancel response was not authoritative: {}",
                        unfilled_cid, err
                    );
                    self.require_chase_reconciliation(
                        &symbol,
                        chase,
                        &unfilled_cid,
                        "HEDGE_CANCEL_SNAPSHOT_INVALID",
                    );
                    return;
                }
            };
            if let Err(err) = self.apply_terminal_order_snapshot(
                &mut chase,
                unfilled_leg,
                &unfilled_cid,
                snapshot,
            ) {
                error!(
                    "Cannot aggregate cancel/fill race for {}: {}",
                    unfilled_cid, err
                );
                self.require_chase_reconciliation(
                    &symbol,
                    chase,
                    &unfilled_cid,
                    "HEDGE_CANCEL_AGGREGATION_FAILED",
                );
                return;
            }
            if !self.store_chase_state(
                symbol.clone(),
                chase.clone(),
                "legging maker cancel snapshot aggregated",
            ) {
                self.require_chase_reconciliation(
                    &symbol,
                    chase,
                    &unfilled_cid,
                    "HEDGE_CANCEL_NOT_DURABLE",
                );
                return;
            }
            // A nested timeout may already have installed a residual order.
            // The outer timeout owns only the generation it canceled and must
            // not submit a second taker hedge.
            if chase.active_client_order_id(unfilled_leg) != unfilled_cid
                || chase.phase == ChasePhase::LeggingDefenseTakerPlaced
            {
                return;
            }
        } else if submission_not_started
            && let Some(order) = self.internal_orders.get_mut(&unfilled_cid)
        {
            order.status = "NOT_SUBMITTED".to_string();
        }
        if !self.persist_execution_state_for_symbol(&symbol, "legging maker cancel confirmed") {
            self.require_chase_reconciliation(
                &symbol,
                chase,
                &unfilled_cid,
                "HEDGE_CANCEL_NOT_DURABLE",
            );
            return;
        }

        let remaining_quantity =
            (chase.target_for(unfilled_leg) - chase.cumulative_for(unfilled_leg)).max(0.0);
        if remaining_quantity <= quantity_tolerance {
            if chase.both_legs_terminal() {
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
                        &symbol,
                        chase,
                        &unfilled_cid,
                        "TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&symbol, "cancel/fill race completed both legs");
            } else {
                self.require_chase_reconciliation(
                    &symbol,
                    chase,
                    &unfilled_cid,
                    "TARGET_REACHED_WITHOUT_TERMINAL_ORDER_STATE",
                );
            }
            return;
        }

        let new_taker_cid = self.replacement_client_order_id(&chase, unfilled_leg);
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
        chase.set_active_client_order_id(unfilled_leg, new_taker_cid.clone());
        if let Some(lineage) = self.order_lineage.get(&unfilled_cid).cloned() {
            self.order_lineage.insert(new_taker_cid.clone(), lineage);
        }
        self.order_cumulative_fills
            .insert(new_taker_cid.clone(), 0.0);
        chase.phase = ChasePhase::LeggingDefenseTakerPlaced;
        // This write-ahead snapshot closes the crash window between exchange
        // acceptance and recording the replacement client order id.
        if !self.store_chase_state(
            symbol.clone(),
            chase.clone(),
            "legging defense before exchange submission",
        ) {
            return;
        }
        if let Some(order) = self.internal_orders.get_mut(&new_taker_cid) {
            order.status = "SUBMITTING".to_string();
        }
        if !self.persist_execution_state_for_symbol(&symbol, "legging defense submission started") {
            return;
        }

        let quantity_market = match unfilled_leg {
            Leg::Spot => MarketType::Spot,
            Leg::Futures => MarketType::Perp,
        };
        let Some(quantity) = self.format_market_quantity_with_price(
            &unfilled_sym,
            quantity_market,
            remaining_quantity,
            expected_fill_price,
        ) else {
            self.require_chase_reconciliation(
                &symbol,
                chase,
                &new_taker_cid,
                "RESIDUAL_BELOW_MARKET_LOT_FILTER",
            );
            return;
        };

        let market_res = match unfilled_leg {
            Leg::Spot => {
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

        let Some(latest) = self.chase_states.get(&symbol).cloned() else {
            return;
        };
        chase = latest;
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
            if let Some(order) = self.internal_orders.get_mut(&new_taker_cid)
                && matches!(order.status.as_str(), "PENDING_SUBMIT" | "SUBMITTING")
            {
                order.status = if self.trading_mode == "paper" {
                    "FILLED_PENDING".to_string()
                } else {
                    "NEW".to_string()
                };
            }
            if !self.persist_execution_state_for_symbol(
                &symbol,
                "legging defense submission acknowledged",
            ) {
                self.require_chase_reconciliation(
                    &symbol,
                    chase,
                    &new_taker_cid,
                    "LEGGING_DEFENSE_ACK_NOT_DURABLE",
                );
                return;
            }
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
            if self.binance_rest.trading_mode != "paper" {
                let rest = self.binance_rest.clone();
                let dash_tx = self.dash_tx.clone();
                tokio::spawn(async move {
                    if let Err(error) = rest.refresh_rate_limit_telemetry().await {
                        warn!("Exchange rate-limit telemetry refresh failed: {}", error);
                    }
                    OrderManager::emit_exchange_quota_snapshot(&rest, &dash_tx);
                });
            }
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

        if intent == AlphaIntent::SubscribeMarketData {
            let sym_upper = match instruction.symbol.as_deref() {
                Some(symbol)
                    if !symbol.trim().is_empty()
                        && symbol
                            .trim()
                            .chars()
                            .all(|character| character.is_ascii_alphanumeric()) =>
                {
                    symbol.trim().to_uppercase()
                }
                _ => {
                    warn!("Received SUBSCRIBE_MARKET_DATA with an invalid symbol; ignoring.");
                    return;
                }
            };
            if let Err(err) = self.subscription_tx.send(sym_upper.clone()).await {
                warn!(
                    "Could not request side-effect-free market-data subscription for {}: {}",
                    sym_upper, err
                );
                return;
            }
            info!(
                "Requested side-effect-free market-data subscription for {}",
                sym_upper
            );
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
                let capacity_exhausted = err.contains("byte budget exceeded");
                if !capacity_exhausted {
                    self.intent_journal_error = Some(err);
                    self.intent_journal = None;
                } else if intent.is_exit() {
                    self.state = SystemState::Reconciling;
                }
                let symbol = instruction.symbol.clone();
                self.reject_instruction(
                    &instruction,
                    symbol.as_deref(),
                    if capacity_exhausted {
                        if intent.is_exit() {
                            "intent_journal_survival_reserve_exhausted"
                        } else {
                            "intent_journal_entry_budget_exhausted"
                        }
                    } else {
                        "intent_journal_unavailable"
                    },
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
        if let Err(err) = self.subscription_tx.send(sym_upper.clone()).await {
            warn!(
                "Could not request dynamic market-data subscription for {}: {}",
                sym_upper, err
            );
        }
        if intent == AlphaIntent::EnterShort {
            // ENTER_SHORT is short cash spot / long perpetual. This engine has
            // no authoritative margin-borrow, interest, recall, or repayment
            // ledger, so allowing it would silently sell account inventory.
            warn!(
                "Rejecting reverse funding entry for {}: short-spot borrow lifecycle is disabled",
                sym_upper
            );
            self.reject_instruction(
                &instruction,
                Some(&sym_upper),
                "short_spot_borrow_lifecycle_disabled",
                true,
            );
            return;
        }
        if !is_exit_intent && self.is_symbol_persistence_latched(&sym_upper) {
            self.reject_instruction(
                &instruction,
                Some(&sym_upper),
                "symbol_persistence_latched",
                true,
            );
            return;
        }
        if !is_exit_intent && self.continuous_risk_state != ContinuousRiskState::Normal {
            self.reject_instruction(
                &instruction,
                Some(&sym_upper),
                "continuous_risk_actor_entry_frozen",
                true,
            );
            return;
        }
        if !is_exit_intent && !self.market_data_quorum_ready(&sym_upper) {
            self.reject_instruction(
                &instruction,
                Some(&sym_upper),
                "market_data_quorum_not_ready",
                true,
            );
            return;
        }
        if !is_exit_intent {
            if self.storage_control_error.is_some() {
                self.reject_instruction(
                    &instruction,
                    Some(&sym_upper),
                    "storage_control_unavailable",
                    true,
                );
                return;
            }
            if self.storage_emergency_latched {
                self.reject_instruction(
                    &instruction,
                    Some(&sym_upper),
                    "storage_emergency_latched",
                    true,
                );
                return;
            }
        }
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
        if !is_exit_intent && let Err(err) = self.execution_state_storage_allows_new_risk() {
            warn!("Rejecting new risk because durable storage is degraded: {err}");
            self.reject_instruction(
                &instruction,
                Some(&sym_upper),
                "execution_state_storage_budget_exhausted",
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
        if !is_exit_intent && let Some(reason) = self.entry_quota_block_reason() {
            self.reject_instruction(&instruction, Some(&sym_upper), reason, true);
            return;
        }
        if !is_exit_intent && self.check_circuit_breakers().await {
            self.reject_instruction(&instruction, Some(&sym_upper), "circuit_breaker", true);
            return;
        }

        if instruction.route_policy.as_deref() == Some("emergency_reduce_only") {
            let intent_id = match self.begin_emergency_exit(&instruction, &sym_upper) {
                Ok(intent_id) => intent_id,
                Err(reason) => {
                    self.reject_instruction(&instruction, Some(&sym_upper), reason, true);
                    return;
                }
            };
            if !self.transition_intent_ack(&intent_id, "SUBMITTED", "emergency_route_detected") {
                self.fail_emergency_exit(&intent_id, "emergency intent ACK was not durable");
                return;
            }
            self.drive_emergency_exit(intent_id).await;
            return;
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
                let mut authoritative = existing.clone();
                for leg in [Leg::Spot, Leg::Futures] {
                    if (leg == Leg::Spot && !authoritative.has_spot_leg())
                        || (leg == Leg::Futures && !authoritative.has_futures_leg())
                    {
                        continue;
                    }
                    let client_order_id = authoritative.active_client_order_id(leg).to_string();
                    let already_terminal = self
                        .internal_orders
                        .get(&client_order_id)
                        .is_some_and(|order| is_terminal_internal_status(&order.status));
                    if already_terminal {
                        continue;
                    }
                    let venue = match leg {
                        Leg::Spot => LegVenue::Spot,
                        Leg::Futures => LegVenue::UsdtFutures,
                    };
                    let cancel_body = match self
                        .cancel_order_pumped(venue, &sym_upper, &client_order_id)
                        .await
                    {
                        Ok(body) => body,
                        Err(err) => {
                            let latest = self
                                .chase_states
                                .get(&sym_upper)
                                .cloned()
                                .unwrap_or(authoritative);
                            warn!(
                                "Preemption cancel for {} was not confirmed: {}",
                                client_order_id, err
                            );
                            self.require_chase_reconciliation(
                                &sym_upper,
                                latest,
                                &client_order_id,
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
                    };

                    // The pumped REST wait may have processed a private fill or
                    // a hedge-deadline event. Always merge the authoritative
                    // cancel snapshot into that latest logical cycle, never the
                    // stale clone captured before the request.
                    let Some(mut latest) = self.chase_states.get(&sym_upper).cloned() else {
                        self.state = SystemState::Reconciling;
                        self.emit_execution_readiness(
                            "BLOCKED",
                            "preempted cycle changed while cancel was in flight",
                        );
                        self.emit_execution_recovery_required(
                            "preempt_cycle_disappeared_during_cancel",
                        );
                        self.reject_instruction(
                            &instruction,
                            Some(&sym_upper),
                            "preempt_cycle_changed_during_cancel",
                            true,
                        );
                        return;
                    };
                    if !matches!(latest.phase, ChasePhase::Idle | ChasePhase::DualMakerPlaced) {
                        self.require_chase_reconciliation(
                            &sym_upper,
                            latest,
                            &client_order_id,
                            "PREEMPT_CYCLE_ADVANCED_DURING_CANCEL",
                        );
                        self.reject_instruction(
                            &instruction,
                            Some(&sym_upper),
                            "preempt_cycle_changed_during_cancel",
                            true,
                        );
                        return;
                    }
                    let snapshot = if self.trading_mode == "paper" {
                        TerminalOrderSnapshot {
                            status: ExchangeOrderStatus::Canceled,
                            cumulative_filled_qty: self
                                .order_cumulative_fills
                                .get(&client_order_id)
                                .copied()
                                .unwrap_or(0.0),
                            average_fill_price: None,
                        }
                    } else {
                        match Self::parse_terminal_order_snapshot(&cancel_body, &client_order_id) {
                            Ok(snapshot) => snapshot,
                            Err(err) => {
                                warn!(
                                    "Preemption cancel for {} lacked an authoritative terminal snapshot: {}",
                                    client_order_id, err
                                );
                                self.require_chase_reconciliation(
                                    &sym_upper,
                                    latest,
                                    &client_order_id,
                                    "PREEMPT_CANCEL_SNAPSHOT_INVALID",
                                );
                                self.reject_instruction(
                                    &instruction,
                                    Some(&sym_upper),
                                    "preempt_cancel_snapshot_invalid",
                                    true,
                                );
                                return;
                            }
                        }
                    };
                    if let Err(err) = self.apply_terminal_order_snapshot(
                        &mut latest,
                        leg,
                        &client_order_id,
                        snapshot,
                    ) {
                        warn!(
                            "Preemption cancel/fill aggregation failed for {}: {}",
                            client_order_id, err
                        );
                        self.require_chase_reconciliation(
                            &sym_upper,
                            latest,
                            &client_order_id,
                            "PREEMPT_CANCEL_AGGREGATION_FAILED",
                        );
                        self.reject_instruction(
                            &instruction,
                            Some(&sym_upper),
                            "preempt_cancel_aggregation_failed",
                            true,
                        );
                        return;
                    }
                    authoritative = latest;
                    let _ = self.store_chase_state(
                        sym_upper.clone(),
                        authoritative.clone(),
                        "authoritative preemption cancel snapshot",
                    );
                }

                let fill_tolerance = authoritative
                    .spot_quantity
                    .max(authoritative.perp_quantity)
                    .mul_add(1e-9, 1e-12);
                let any_fill = authoritative.spot_cumulative_filled > fill_tolerance
                    || authoritative.futures_cumulative_filled > fill_tolerance;
                let all_generations_terminal = [Leg::Spot, Leg::Futures]
                    .into_iter()
                    .filter(|leg| match leg {
                        Leg::Spot => authoritative.has_spot_leg(),
                        Leg::Futures => authoritative.has_futures_leg(),
                    })
                    .all(|leg| {
                        self.internal_orders
                            .get(authoritative.active_client_order_id(leg))
                            .is_some_and(|order| is_terminal_internal_status(&order.status))
                    });
                if any_fill || !all_generations_terminal {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        authoritative,
                        "",
                        if any_fill {
                            "PREEMPT_CANCEL_OBSERVED_FILL"
                        } else {
                            "PREEMPT_ORDER_NOT_TERMINAL"
                        },
                    );
                    self.reject_instruction(
                        &instruction,
                        Some(&sym_upper),
                        if any_fill {
                            "preempt_cancel_observed_fill"
                        } else {
                            "preempt_order_not_terminal"
                        },
                        true,
                    );
                    return;
                }
                let _ = self.store_chase_state(
                    sym_upper.clone(),
                    authoritative,
                    "confirmed fill-free preemption cancels",
                );
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
                let _ = self.persist_execution_state_for_symbol(
                    &sym_upper,
                    "preempted intent lineage removed",
                );
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
        let requested_exact =
            Self::canonical_exact(instruction.requested_quantity_decimal.as_deref())
                .expect("protocol-v3 exact requested quantity was validated");
        let requested_quantity_decimal = requested_exact.to_string();
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

            let spot_q = Self::canonical_exact(instruction.exit_spot_quantity_decimal.as_deref())
                .and_then(ExactDecimal::to_f64)
                .unwrap_or(0.0);

            let perp_q =
                Self::canonical_exact(instruction.exit_futures_quantity_decimal.as_deref())
                    .and_then(ExactDecimal::to_f64)
                    .unwrap_or(0.0);

            (spot_q.min(spot_tracked), perp_q.min(perp_tracked))
        } else {
            let q = requested_exact.to_f64().unwrap_or(0.0) * instruction.exposure_scale;
            (q, q)
        };

        let sym_info = self.symbol_info(&sym_upper);
        let max_allowed = sym_info
            .spot_max_qty
            .min(sym_info.spot_market_max_qty)
            .min(sym_info.futures_max_qty)
            .min(sym_info.futures_market_max_qty);
        let max_allowed_f64 = max_allowed.to_f64().unwrap_or(0.0);
        resolved_spot_qty = resolved_spot_qty.min(max_allowed_f64);
        resolved_perp_qty = resolved_perp_qty.min(max_allowed_f64);

        let common_entry_qty = if is_exit {
            None
        } else {
            match self.normalize_common_entry_quantity(&sym_upper, resolved_spot_qty) {
                Some(quantity) => Some(quantity),
                None => {
                    warn!(
                        "Instruction {} for {} has no common executable spot/futures quantity at or below {:.8}",
                        instruction.intent, sym_upper, resolved_spot_qty
                    );
                    self.reject_instruction(
                        &instruction,
                        Some(&sym_upper),
                        "no_common_entry_quantity",
                        true,
                    );
                    return;
                }
            }
        };

        let normalized_spot_qty = if let Some(quantity) = common_entry_qty {
            quantity
        } else if skip_spot_leg {
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

        let normalized_perp_qty = if let Some(quantity) = common_entry_qty {
            quantity
        } else if skip_perp_leg {
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
            let spot_notional = if skip_spot_leg {
                None
            } else {
                Self::exact_notional(normalized_spot_qty, mid_price)
            };
            let perp_notional = if skip_perp_leg {
                None
            } else {
                Self::exact_notional(normalized_perp_qty, mid_price)
            };
            if (!skip_spot_leg && spot_notional.is_none())
                || (!skip_perp_leg && perp_notional.is_none())
            {
                self.reject_instruction(
                    &instruction,
                    Some(&sym_upper),
                    "invalid_notional_arithmetic",
                    true,
                );
                return;
            }
            if spot_notional.is_some_and(|notional| notional < sym_info.spot_min_notional) {
                let spot_notional = spot_notional.expect("checked above");
                if is_exit {
                    warn!(
                        "Instruction {} for {} spot leg notional ${} is below minimum ${}; rejecting the paired exit",
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
                        "Instruction {} for {} rejected: spot notional ${} is below minimum ${} (qty={:.8}, price={:.4})",
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
            if perp_notional.is_some_and(|notional| notional < sym_info.futures_min_notional) {
                let perp_notional = perp_notional.expect("checked above");
                if is_exit {
                    warn!(
                        "Instruction {} for {} perp leg notional ${} is below minimum ${}; rejecting the paired exit",
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
                        "Instruction {} for {} rejected: perp notional ${} is below minimum ${} (qty={:.8}, price={:.4})",
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

        let per_symbol_cap = self.active_per_symbol_notional_cap_usd();
        let fallback_spot_price = per_symbol_cap / normalized_spot_qty.max(1e-12);
        let fallback_perp_price = per_symbol_cap / normalized_perp_qty.max(1e-12);
        let reservation_spot_price = self
            .spot_top_cache
            .get(&sym_upper)
            .map(|top| if is_buy { top.ask_price } else { top.bid_price })
            .or_else(|| self.spot_mid_cache.get(&sym_upper).copied())
            .filter(|price| price.is_finite() && *price > 0.0)
            .unwrap_or(fallback_spot_price);
        let reservation_perp_price = self
            .perp_top_cache
            .get(&sym_upper)
            .map(|top| if is_buy { top.bid_price } else { top.ask_price })
            .or_else(|| self.perp_mid_cache.get(&sym_upper).copied())
            .filter(|price| price.is_finite() && *price > 0.0)
            .unwrap_or(fallback_perp_price);
        if !is_exit {
            let spot_notional = if skip_spot_leg {
                0.0
            } else {
                normalized_spot_qty * reservation_spot_price
            };
            let perp_notional = if skip_perp_leg {
                0.0
            } else {
                normalized_perp_qty * reservation_perp_price
            };
            let candidate_per_symbol = spot_notional.max(perp_notional);
            let candidate_pair_gross = spot_notional + perp_notional;
            let (tracked_spot_notional, tracked_perp_notional) =
                self.tracked_symbol_leg_notionals_usd(&sym_upper);
            let projected_per_symbol =
                (tracked_spot_notional + spot_notional).max(tracked_perp_notional + perp_notional);
            let reserved_other = self.pending_entry_reserved_gross_usd(Some(&sym_upper));
            let projected_gross =
                self.current_gross_exposure_usd + reserved_other + candidate_pair_gross;
            let rejection = if self.risk_bearing_symbol_count(Some(&sym_upper))
                >= COMPILED_MAX_CONCURRENT_SYMBOLS
            {
                Some("MAX_CONCURRENT_SYMBOLS")
            } else if !candidate_per_symbol.is_finite()
                || !candidate_pair_gross.is_finite()
                || candidate_per_symbol <= 0.0
            {
                Some("INVALID_ENTRY_RESERVATION")
            } else if !projected_per_symbol.is_finite()
                || projected_per_symbol > per_symbol_cap + 1e-9
            {
                Some("PER_SYMBOL_NOTIONAL_CAP")
            } else if projected_gross > self.max_gross_exposure_usd + 1e-9 {
                Some("MAX_GROSS_EXPOSURE_RESERVED")
            } else if self.trading_mode != "paper"
                && !skip_spot_leg
                && is_buy
                && !self.spot_collateral_available_for_entry(
                    &sym_upper,
                    spot_notional,
                    Some(&sym_upper),
                )
            {
                Some("INSUFFICIENT_SPOT_COLLATERAL_RESERVED")
            } else {
                None
            };
            if let Some(reason) = rejection {
                warn!(
                    "Rejecting {} before chase acceptance: {} (candidate_pair=${:.2}, projected_symbol=${:.2}/${:.2}, pending_reserved=${:.2}, projected_gross=${:.2}/${:.2})",
                    sym_upper,
                    reason,
                    candidate_pair_gross,
                    projected_per_symbol,
                    per_symbol_cap,
                    reserved_other,
                    projected_gross,
                    self.max_gross_exposure_usd,
                );
                self.reject_instruction(&instruction, Some(&sym_upper), reason, true);
                return;
            }
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
            requested_quantity_decimal: Some(requested_quantity_decimal.clone()),
            risk_adjusted_requested_quantity_decimal: ExactDecimal::from_f64(resolved_spot_qty)
                .map(|value| value.to_string()),
            normalized_common_entry_quantity_decimal: if is_exit {
                None
            } else {
                ExactDecimal::from_f64(normalized_spot_qty).map(|value| value.to_string())
            },
            actual_spot_inventory_decimal: instruction.actual_spot_inventory_decimal.clone(),
            actual_futures_inventory_decimal: instruction.actual_futures_inventory_decimal.clone(),
            exit_spot_quantity_decimal: instruction.exit_spot_quantity_decimal.clone(),
            exit_futures_quantity_decimal: instruction.exit_futures_quantity_decimal.clone(),
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
            let Some(estimated_notional) =
                Self::exact_notional(normalized_perp_qty.max(normalized_spot_qty), mid_price)
            else {
                self.reject_instruction(
                    &instruction,
                    Some(&sym_upper),
                    "invalid_notional_arithmetic",
                    true,
                );
                return;
            };
            let min_n = sym_info
                .spot_min_notional
                .max(sym_info.futures_min_notional);

            if !is_exit && estimated_notional < min_n {
                warn!(
                    "Instruction {} for {} rejected: estimated notional ${} is below minimum ${} (qty_spot={:.8}, qty_perp={:.8}, price={:.4})",
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
            spot_order_aliases: Vec::new(),
            futures_order_aliases: Vec::new(),
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
            expected_spot_price: reservation_spot_price,
            expected_fut_price: reservation_perp_price,
            spot_fill_price: None,
            futures_fill_price: None,
            spot_cumulative_filled: 0.0,
            futures_cumulative_filled: 0.0,
            spot_terminal: false,
            futures_terminal: false,
            requested_quantity_decimal,
            normalized_common_entry_quantity_decimal: common_entry_qty
                .map(|value| value.to_string()),
            actual_spot_inventory_decimal: instruction
                .actual_spot_inventory_decimal
                .clone()
                .unwrap_or_else(|| "0".to_string()),
            actual_futures_inventory_decimal: instruction
                .actual_futures_inventory_decimal
                .clone()
                .unwrap_or_else(|| "0".to_string()),
            exit_spot_quantity_decimal: if is_exit {
                ExactDecimal::from_f64(normalized_spot_qty)
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "0".to_string())
            } else {
                "0".to_string()
            },
            exit_futures_quantity_decimal: if is_exit {
                ExactDecimal::from_f64(normalized_perp_qty)
                    .map(|value| value.to_string())
                    .unwrap_or_else(|| "0".to_string())
            } else {
                "0".to_string()
            },
            route_policy: instruction.route_policy.clone().unwrap_or_default(),
            last_exchange_event_time_ms: None,
            last_receive_time_ms: None,
            last_persist_time_ms: None,
        };
        let mut chase = chase;
        chase.ensure_active_aliases();
        self.chase_states.insert(sym_upper.clone(), chase);
        self.chase_intent_ids
            .insert(sym_upper.clone(), intent_id.clone());
        self.chase_unhedged_budgets.insert(
            sym_upper.clone(),
            instruction
                .max_unhedged_notional_ms
                .unwrap_or(crate::ipc::DEFAULT_MAX_UNHEDGED_NOTIONAL_MS),
        );
        if !self
            .persist_execution_state_for_symbol(&sym_upper, "accepted chase before SUBMITTED ACK")
        {
            if let Some(chase) = self.chase_states.get_mut(&sym_upper) {
                chase.phase = ChasePhase::ReconciliationRequired;
            }
            if !is_exit {
                self.reject_instruction(
                    &instruction,
                    Some(&sym_upper),
                    "symbol_persistence_latched",
                    true,
                );
            }
            return;
        }
        if !self.transition_intent_ack(&intent_id, "SUBMITTED", "") {
            self.remove_chase_state(&sym_upper, "intent submission ACK failed");
            self.chase_intent_ids.remove(&sym_upper);
            let _ = self
                .persist_execution_state_for_symbol(&sym_upper, "failed intent lineage removed");
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
        let mut durable_order_event =
            matches!(&event, WsEvent::OrderUpdate { .. }).then(|| event.clone());
        match event {
            WsEvent::TerminalPublicationPersisted { publication_id } => {
                self.complete_terminal_publication(&publication_id);
            }
            WsEvent::Connected {
                symbol,
                stream_type,
                connection_role,
                ..
            } => {
                info!(
                    "OrderManager received WebSocket Connected event for {} ({:?}).",
                    symbol, stream_type
                );
                let market_quorum_ready = if stream_type == WsStreamType::MarketData {
                    let Some(role) = connection_role.as_deref() else {
                        warn!("MarketData Connected event has no connection role");
                        return;
                    };
                    self.market_stream_ready_roles
                        .insert(Self::market_stream_role_key(&symbol, role));
                    self.market_data_quorum_ready(&symbol)
                } else {
                    false
                };
                if market_quorum_ready
                    && self
                        .public_stream_recovery_symbols
                        .remove(&symbol.to_uppercase())
                {
                    let _ = self.persist_execution_state_for_symbol(
                        &symbol,
                        "public market-data quorum recovery observed",
                    );
                }
                if self.state != SystemState::Trading
                    && stream_type == WsStreamType::MarketData
                    && market_quorum_ready
                    && self.public_stream_recovery_symbols.is_empty()
                    && (self.trading_mode == "paper" || self.private_stream_quorum_ready())
                {
                    if matches!(
                        self.continuous_risk_state,
                        ContinuousRiskState::EntryFrozen | ContinuousRiskState::CancelingEntries
                    ) {
                        let _ = self.advance_continuous_risk(
                            ContinuousRiskState::Reconciling,
                            "public market-data quorum restored; signed reconciliation required",
                        );
                    }
                    self.execute_reconciliation_sequence().await;
                } else if self.state == SystemState::Trading
                    && stream_type == WsStreamType::MarketData
                    && connection_role.as_deref() == Some("futures-public")
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
                            if let Some(order_sym) = order.get("symbol").and_then(|v| v.as_str())
                                && order_sym.eq_ignore_ascii_case(&symbol)
                                && let Some(client_id) = order
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
            WsEvent::Disconnected {
                symbol,
                stream_type,
                connection_role,
                ..
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

                if stream_type == WsStreamType::MarketData {
                    self.public_stream_recovery_symbols
                        .insert(symbol.to_uppercase());
                    if let Some(role) = connection_role.as_deref() {
                        self.market_stream_ready_roles
                            .remove(&Self::market_stream_role_key(&symbol, role));
                    }
                    self.emit_execution_readiness(
                        "BLOCKED",
                        "public market-data quorum disconnected",
                    );
                }

                // Never discard an execution state because a feed disconnected:
                // the exchange-side effect may still exist. Clear only stale
                // market data and keep the chase durable for reconciliation.
                let symbol_upper = symbol.to_uppercase();
                if self.chase_states.contains_key(&symbol_upper) {
                    self.state = SystemState::Reconciling;
                    let _ = self.persist_execution_state_for_symbol(
                        &symbol_upper,
                        "market stream disconnected during chase",
                    );
                    self.emit_execution_recovery_required(
                        "market_stream_disconnected_during_chase",
                    );
                }
                match connection_role.as_deref() {
                    Some("spot-public") => {
                        self.spot_top_cache.remove(&symbol_upper);
                        self.spot_depth_capacity.remove(&symbol_upper);
                        self.depth_sequences
                            .remove(&format!("{}:spot", symbol_upper));
                    }
                    Some("futures-public") => {
                        self.perp_top_cache.remove(&symbol_upper);
                        self.perp_depth_capacity.remove(&symbol_upper);
                        self.depth_sequences
                            .remove(&format!("{}:perp", symbol_upper));
                    }
                    Some("futures-market") => {
                        self.perp_mid_cache.remove(&symbol_upper);
                    }
                    _ if stream_type == WsStreamType::MarketData => {
                        self.spot_top_cache.remove(&symbol_upper);
                        self.perp_top_cache.remove(&symbol_upper);
                        self.spot_depth_capacity.remove(&symbol_upper);
                        self.perp_depth_capacity.remove(&symbol_upper);
                        self.depth_sequences
                            .retain(|key, _| !key.starts_with(&format!("{}:", symbol_upper)));
                    }
                    _ => {}
                }
                if stream_type == WsStreamType::MarketData {
                    self.activate_continuous_risk(
                        format!(
                            "public_market_data_disconnected:{}:{}",
                            symbol,
                            connection_role.as_deref().unwrap_or("unknown")
                        ),
                        ContinuousRiskState::Reconciling,
                    )
                    .await;
                } else if stream_type == WsStreamType::UserData {
                    self.activate_continuous_risk(
                        "private_user_data_disconnected".to_string(),
                        ContinuousRiskState::Reconciling,
                    )
                    .await;
                }
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
                    self.activate_continuous_risk(
                        format!("private_stream_not_ready:{market:?}:{status}"),
                        ContinuousRiskState::Reconciling,
                    )
                    .await;
                    return;
                }
                if quorum_ready
                    && self.trading_mode != "paper"
                    && self.public_stream_recovery_symbols.is_empty()
                {
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
                self.activate_continuous_risk(
                    "telemetry_gap_private_replay_required".to_string(),
                    ContinuousRiskState::Reconciling,
                )
                .await;
            }
            WsEvent::BookTicker {
                symbol,
                bid_price,
                ask_price,
                exchange_event_time_ms,
                receive_time_ms,
                ..
            } => {
                if !self.market_processing_allowed(&symbol)
                    || !Self::market_event_time_is_current(
                        exchange_event_time_ms,
                        receive_time_ms,
                        false,
                    )
                    || !bid_price.is_finite()
                    || !ask_price.is_finite()
                    || bid_price <= 0.0
                    || ask_price < bid_price
                {
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
                sequence_contiguous,
                exchange_event_time_ms,
                receive_time_ms,
                ..
            } => {
                if !self.market_processing_allowed(&symbol)
                    || !Self::market_event_time_is_current(
                        exchange_event_time_ms,
                        receive_time_ms,
                        market == MarketType::Spot,
                    )
                    || bids.is_empty()
                    || asks.is_empty()
                    || bids.iter().chain(&asks).any(|level| {
                        !level[0].is_finite()
                            || !level[1].is_finite()
                            || level[0] <= 0.0
                            || level[1] <= 0.0
                    })
                {
                    return;
                }

                let sym_upper = symbol.to_uppercase();
                let market_name = match market {
                    MarketType::Spot => "spot",
                    MarketType::Perp => "perp",
                };
                let sequence_key = format!("{}:{}", sym_upper, market_name);
                if !sequence_contiguous {
                    warn!("Source-declared depth sequence gap for {}", sequence_key);
                    self.spot_top_cache.remove(&sym_upper);
                    self.perp_top_cache.remove(&sym_upper);
                    self.spot_depth_capacity.remove(&sym_upper);
                    self.perp_depth_capacity.remove(&sym_upper);
                    let gap_event = serde_json::json!({
                        "event": "FeedGap",
                        "symbol": sym_upper,
                        "market": market_name,
                        "first_update_id": first_update_id,
                        "previous_final_update_id": previous_final_update_id,
                        "final_update_id": final_update_id,
                        "reason": "source_depth_sequence_gap",
                    });
                    if let Ok(encoded) = rmp_serde::to_vec_named(&gap_event) {
                        let _ = self.dash_tx.send(encoded);
                    }
                    if let Some(final_id) = final_update_id {
                        self.depth_sequences.insert(sequence_key, final_id);
                    }
                    return;
                }
                if let Some(final_id) = final_update_id {
                    if let Some(last_id) = self.depth_sequences.get(&sequence_key).copied() {
                        if final_id <= last_id {
                            debug!(
                                "Ignoring duplicate/stale depth update {} <= {} for {}",
                                final_id, last_id, sequence_key
                            );
                            return;
                        }
                        let previous_mismatch = !is_snapshot
                            && previous_final_update_id
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
                            // Advance the rejected cursor so one missing range
                            // produces one incident instead of an unbounded warning
                            // storm. The configured partial-book stream supplies a
                            // fresh authoritative snapshot on the next message.
                            self.depth_sequences.insert(sequence_key, final_id);
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

                // Per-symbol toxicity gate (see BookTicker handler above).
                if !self.toxic_symbols.contains_key(&sym_upper)
                    || self
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
                commission,
                commission_asset,
                realized_pnl: _realized_pnl,
                maker,
                execution_type,
                event_time_ms,
                exchange_event_time_ms,
                receive_time_ms,
                persist_time_ms,
                market: event_market,
                order_id,
                trade_id,
                ..
            } => {
                let parsed_status = ExchangeOrderStatus::parse(&status);
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
                if parsed_status.is_some_and(|value| value.is_filled() || value.is_partial())
                    && let Some(internal) = self.internal_orders.get(&client_order_id)
                    && let Some(expected_price) = internal.limit_price
                    && let Some(actual_fill_price) = observed_fill_price
                {
                    let slippage_bps =
                        ((actual_fill_price - expected_price) / expected_price) * 10_000.0;
                    info!(
                        "Fill monitoring: {} status={} expected_price={:.2} actual_fill={:.2} slippage={:.2}bps",
                        client_order_id, status, expected_price, actual_fill_price, slippage_bps
                    );
                }

                let sym_clone = symbol.to_uppercase();
                let mut recovered_from_terminal_tombstone = false;
                let mut chase_snapshot = self.chase_states.get(&sym_clone).cloned();
                if chase_snapshot.is_none()
                    && client_order_id.starts_with("bngs_")
                    && let Some(recovered) =
                        self.recover_chase_from_terminal_tombstone(&sym_clone, &client_order_id)
                {
                    recovered_from_terminal_tombstone = true;
                    chase_snapshot = Some(recovered);
                }
                let mut matched_leg_was_terminal = false;
                let mut matched_effective_fill_qty = 0.0;
                let mut cycle_imbalance = 0.0;
                let mut reconciliation_reason: Option<&'static str> = None;
                let mut unknown_bot_fill_delta = 0.0;
                let mut matched_market = None;

                if chase_snapshot.is_none() && client_order_id.starts_with("bngs_") {
                    let previous = self
                        .order_cumulative_fills
                        .get(&client_order_id)
                        .copied()
                        .unwrap_or(0.0);
                    let reported = cumulative_filled_qty.unwrap_or_else(|| {
                        if parsed_status
                            .is_some_and(|value| value.is_filled() || value.is_partial())
                        {
                            previous + reported_last_fill_qty.max(0.0)
                        } else {
                            previous
                        }
                    });
                    if reported.is_finite() && reported + 1e-12 >= previous {
                        unknown_bot_fill_delta = (reported - previous).max(0.0);
                        self.order_cumulative_fills
                            .insert(client_order_id.clone(), reported);
                    } else {
                        reconciliation_reason = Some("UNKNOWN_ORDER_INVALID_FILL_PROGRESS");
                    }
                }

                if let Some(mut chase) = chase_snapshot.take() {
                    chase.last_exchange_event_time_ms = exchange_event_time_ms.or(event_time_ms);
                    chase.last_receive_time_ms = receive_time_ms.or(event_time_ms);
                    chase.last_persist_time_ms = persist_time_ms;
                    let matched_leg = chase.leg_for_client_order_id(&client_order_id);

                    if let Some(matched_leg) = matched_leg {
                        matched_market = Some(match matched_leg {
                            Leg::Spot => MarketType::Spot,
                            Leg::Futures => MarketType::Perp,
                        });
                        let target = chase.target_for(matched_leg);
                        let tolerance = target.abs().mul_add(1e-9, 1e-12);
                        let previous_order_cumulative = self
                            .order_cumulative_fills
                            .get(&client_order_id)
                            .copied()
                            .unwrap_or(0.0);
                        let reported_order_cumulative =
                            cumulative_filled_qty.unwrap_or_else(|| {
                                if parsed_status
                                    .is_some_and(|value| value.is_filled() || value.is_partial())
                                {
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
                            matched_effective_fill_qty = effective_filled_qty;
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
                            let terminal = parsed_status.is_some_and(|value| value.is_filled())
                                && target_reached;
                            chase.set_progress(matched_leg, updated_leg_cumulative, terminal);

                            let imbalance = (chase.spot_cumulative_filled
                                - chase.futures_cumulative_filled)
                                .abs();
                            cycle_imbalance = imbalance;
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
                            } else if parsed_status.is_some_and(|value| value.is_filled())
                                && !target_reached
                            {
                                reconciliation_reason = Some("TERMINAL_FILL_QUANTITY_MISMATCH");
                            }
                        }
                    }
                    chase_snapshot = Some(chase);
                }

                if commission.is_some_and(|amount| amount != 0.0) {
                    let commission_amount = commission.expect("non-zero commission was checked");
                    let commission_market = matched_market.or(event_market).or_else(|| {
                        self.order_lineage
                            .get(&client_order_id)
                            .and_then(|lineage| lineage.market)
                    });
                    let commission_result = commission_market
                        .ok_or("COMMISSION_MARKET_UNKNOWN")
                        .and_then(|market| {
                            self.apply_commission_once(CommissionObservation {
                                symbol: &sym_clone,
                                client_order_id: &client_order_id,
                                market,
                                amount: commission_amount,
                                asset: commission_asset.as_deref().unwrap_or(""),
                                order_id,
                                trade_id,
                            })
                        });
                    if let Err(reason) = commission_result {
                        reconciliation_reason.get_or_insert(reason);
                    }
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
                // A fill for a strategy-owned client id without recovered
                // cycle lineage is an unresolved exchange effect, even when
                // Binance reports a terminal order status. Keep a deliberately
                // non-terminal local marker so compaction cannot discard the
                // cumulative fill before authoritative reconciliation has
                // attributed it.
                if unknown_bot_fill_delta > 1e-12
                    && let Some(internal_order) = self.internal_orders.get_mut(&client_order_id)
                {
                    internal_order.status = "UNATTRIBUTED_FILLED".to_string();
                }

                if let Some(chase) = chase_snapshot.as_ref() {
                    self.chase_states.insert(sym_clone.clone(), chase.clone());
                    if recovered_from_terminal_tombstone {
                        self.ensure_terminal_tombstone(
                            chase,
                            "EXCHANGE_FLAT_AWAITING_TERMINAL",
                            "LATE_FILL_REPAIR_REQUIRED",
                            "late private fill applied to retained terminal lineage",
                        );
                    }
                }
                // This symbol-scoped durability point is the authoritative equivalent of
                // persist_execution_state("order update and cumulative fill progress").
                if !self.persist_execution_state_for_symbol(
                    &sym_clone,
                    "order update and cumulative fill progress",
                ) {
                    if let Some(chase) = chase_snapshot.clone() {
                        self.require_chase_reconciliation(
                            &sym_clone,
                            chase,
                            &client_order_id,
                            "ORDER_UPDATE_NOT_DURABLE",
                        );
                    } else {
                        self.state = SystemState::Reconciling;
                        self.emit_execution_readiness("BLOCKED", "order_update_not_durable");
                        self.emit_execution_recovery_required("order_update_not_durable");
                    }
                    return;
                }
                if let Some(WsEvent::OrderUpdate {
                    persist_time_ms, ..
                }) = durable_order_event.as_mut()
                {
                    *persist_time_ms = Some(Self::current_time_ms());
                }
                if let Some(durable_event) = durable_order_event.as_ref()
                    && let Ok(encoded) = rmp_serde::to_vec_named(durable_event)
                {
                    let _ = self.dash_tx.send(encoded);
                }

                if let (Some(reason), Some(chase)) = (reconciliation_reason, chase_snapshot.clone())
                {
                    self.require_chase_reconciliation(&sym_clone, chase, &client_order_id, reason);
                    return;
                }

                if reconciliation_reason.is_some() && chase_snapshot.is_none() {
                    self.state = SystemState::Reconciling;
                    self.emit_execution_readiness("BLOCKED", "unknown_bot_order_invalid_progress");
                    self.emit_execution_recovery_required("unknown_bot_order_invalid_progress");
                    return;
                }
                if unknown_bot_fill_delta > 1e-12 {
                    warn!(
                        "Unattributed fill delta {:.12} arrived for bot-owned order {}",
                        unknown_bot_fill_delta, client_order_id
                    );
                    self.state = SystemState::Reconciling;
                    self.emit_execution_readiness("BLOCKED", "unknown_bot_order_fill");
                    self.emit_execution_recovery_required("unknown_bot_order_fill");
                    return;
                }

                if parsed_status.is_none()
                    && let Some(chase) = chase_snapshot.clone()
                    && chase.leg_for_client_order_id(&client_order_id).is_some()
                {
                    self.require_chase_reconciliation(
                        &sym_clone,
                        chase,
                        &client_order_id,
                        "UNKNOWN_EXCHANGE_ORDER_STATUS",
                    );
                    return;
                }

                // Handle chase state logic
                if let Some(mut chase) = chase_snapshot {
                    let matched_leg = chase.leg_for_client_order_id(&client_order_id);
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

                    if parsed_status.is_some_and(ExchangeOrderStatus::is_filled) {
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
                    } else if parsed_status.is_some_and(ExchangeOrderStatus::is_partial) {
                        let tolerance = chase
                            .spot_quantity
                            .max(chase.perp_quantity)
                            .mul_add(1e-9, 1e-12);
                        let schedule_deadline = matched_effective_fill_qty > tolerance
                            && cycle_imbalance > tolerance
                            && chase.phase != ChasePhase::ReconciliationRequired;
                        if schedule_deadline {
                            let ahead_leg = if chase.spot_cumulative_filled
                                >= chase.futures_cumulative_filled
                            {
                                Leg::Spot
                            } else {
                                Leg::Futures
                            };
                            chase.phase = ChasePhase::LegFilledWaiting(ahead_leg);
                            let timeout_ms = self.bounded_legging_timeout_ms(&chase);
                            let _ = self.store_chase_state(
                                sym_clone.clone(),
                                chase,
                                "first partial fill armed hedge deadline",
                            );
                            info!(
                                "Partial fill created naked exposure for {}; arming {}ms hedge deadline",
                                sym_clone, timeout_ms
                            );
                            self.schedule_legging_timeout(client_order_id, timeout_ms);
                            return;
                        }
                        let _ = self.store_chase_state(
                            sym_clone.clone(),
                            chase,
                            "partial fill progress",
                        );
                    } else if parsed_status
                        .is_some_and(ExchangeOrderStatus::is_terminal_without_full_fill)
                    {
                        if chase.phase == ChasePhase::DeadlineFreezing {
                            let _ = self.store_chase_state(
                                sym_clone.clone(),
                                chase,
                                "deadline cancellation update retained for signed readback",
                            );
                            return;
                        }
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
                                let Some(mut latest) = self.chase_states.get(&sym_clone).cloned()
                                else {
                                    return;
                                };
                                let cancel_body = match cancel_result {
                                    Ok(body) => body,
                                    Err(err) => {
                                        error!(
                                            "Fail-closed cancel failed for {} after {} on {}: {}",
                                            chase.symbol, status, client_order_id, err
                                        );
                                        self.require_chase_reconciliation(
                                            &sym_clone,
                                            latest,
                                            &client_order_id,
                                            "PEER_CANCEL_UNCONFIRMED",
                                        );
                                        return;
                                    }
                                };
                                // The cancel wait can process a private fill.
                                // A successful DELETE can also report a fill
                                // before its private event is delivered. Rebase
                                // both sources before declaring a zero-fill exit.
                                let peer_client_id =
                                    chase.active_client_order_id(other_leg).to_string();
                                let snapshot = Self::parse_terminal_order_snapshot(
                                    &cancel_body,
                                    &peer_client_id,
                                );
                                let merge = snapshot.and_then(|snapshot| {
                                    self.apply_terminal_order_snapshot(
                                        &mut latest,
                                        other_leg,
                                        &peer_client_id,
                                        snapshot,
                                    )
                                });
                                if let Err(error) = merge {
                                    warn!("Peer cancel lacks complete terminal evidence: {error}");
                                    self.require_chase_reconciliation(
                                        &sym_clone,
                                        latest,
                                        &peer_client_id,
                                        "PEER_CANCEL_SNAPSHOT_INVALID",
                                    );
                                    return;
                                }
                                if latest.spot_cumulative_filled > 1e-12
                                    || latest.futures_cumulative_filled > 1e-12
                                    || latest.phase == ChasePhase::ReconciliationRequired
                                {
                                    self.require_chase_reconciliation(
                                        &sym_clone,
                                        latest,
                                        &peer_client_id,
                                        "PEER_CANCEL_OBSERVED_FILL",
                                    );
                                    return;
                                }
                                chase = latest;
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
                            ChasePhase::DeadlineFreezing => {
                                let _ = self.store_chase_state(
                                    sym_clone.clone(),
                                    chase,
                                    "deadline cancellation terminal update retained for signed readback",
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
                exchange_event_time_ms,
                receive_time_ms,
                ..
            } => {
                if self.market_processing_allowed(&symbol)
                    && mark_price.is_finite()
                    && mark_price > 0.0
                    && next_funding_rate.is_finite()
                    && Self::market_event_time_is_current(
                        exchange_event_time_ms,
                        receive_time_ms,
                        false,
                    )
                {
                    self.apply_mark_price(&symbol, MarketType::Perp, mark_price);
                }
            }
            WsEvent::VolumeBar {
                symbol: _,
                minute_start_ms: _,
                notional_usd: _,
                ..
            } => {}
            WsEvent::AccountUpdate {
                balances,
                available_balances,
                positions,
                source,
            } => {
                if source == "spot" {
                    for (asset, balance) in balances {
                        self.spot_balances.insert(asset, balance);
                    }
                    for (asset, balance) in available_balances {
                        self.spot_available_balances.insert(asset, balance);
                    }
                    // Private balance messages do not include open orders or a
                    // complete account surface. Revoke signed truth until the
                    // next bounded REST audit; exits remain available.
                    self.standard_spot_account_truth = None;
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
                    let _private_positions_changed = !positions.is_empty();
                    let _available_balance_evidence = available_balances;
                    // ACCOUNT_UPDATE lacks leverage, liquidation price,
                    // maintenance margin, position mode, and open-order truth.
                    // Any change invalidates the signed REST topology.
                    self.usdm_account_truth = None;
                }
            }
            WsEvent::PositionDivergence { .. } => {
                // Emitted internally, no need to handle here
            }
            WsEvent::ExecutionReadiness { .. } | WsEvent::ExchangeQuota { .. } => {
                // Emitted by this actor directly to telemetry after reconciliation.
            }
        }
    }

    async fn try_place_dual_maker(&mut self, symbol: String) {
        let sym_upper = symbol.to_uppercase();
        let Some(chase_snapshot) = self.chase_states.get(&sym_upper).cloned() else {
            return;
        };

        if chase_snapshot.phase != ChasePhase::Idle {
            return;
        }

        if !chase_snapshot.is_exit
            && let Some((reason, target)) = self.continuous_risk_assessment()
        {
            self.activate_continuous_risk(reason, target).await;
            return;
        }

        if !chase_snapshot.is_exit && self.continuous_risk_state != ContinuousRiskState::Normal {
            warn!(
                "Continuous risk state {} blocked entry chase placement for {}",
                self.continuous_risk_state.as_str(),
                chase_snapshot.symbol
            );
            return;
        }

        if !chase_snapshot.is_exit
            && (self.storage_emergency_latched || self.storage_control_error.is_some())
        {
            warn!(
                "Storage-control latch blocked entry chase placement for {}",
                chase_snapshot.symbol
            );
            return;
        }
        if !chase_snapshot.is_exit
            && let Some(reason) = self.entry_quota_block_reason()
        {
            warn!(
                "Exchange quota blocked entry chase placement for {}: {}",
                chase_snapshot.symbol, reason
            );
            return;
        }

        // Toxicity is an entry-readiness gate, never an exit/repair gate.
        // Exits must proceed through their exposure-clamped route even when
        // the entry book is classified as toxic.
        if !chase_snapshot.is_exit && self.toxic_symbols.contains_key(&sym_upper) {
            return;
        }

        let symbol_info = self.symbol_info(&chase_snapshot.symbol);
        let spot_tick_size = symbol_info.spot_tick_size;
        let futures_tick_size = symbol_info.futures_tick_size;
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
            let (tracked_spot_notional, tracked_perp_notional) =
                self.tracked_symbol_leg_notionals_usd(&sym_upper);
            let projected_per_symbol =
                (tracked_spot_notional + spot_notional).max(tracked_perp_notional + perp_notional);
            let reserved_other = self.pending_entry_reserved_gross_usd(Some(&sym_upper));
            let projected_gross =
                self.current_gross_exposure_usd + reserved_other + spot_notional + perp_notional;
            let risk_rejection = if !per_symbol_notional.is_finite()
                || !projected_gross.is_finite()
                || per_symbol_notional <= 0.0
            {
                Some("INVALID_ENTRY_NOTIONAL")
            } else if !projected_per_symbol.is_finite()
                || projected_per_symbol > per_symbol_cap + 1e-9
            {
                Some("PER_SYMBOL_NOTIONAL_CAP")
            } else if projected_gross > self.max_gross_exposure_usd + 1e-9 {
                Some("MAX_GROSS_EXPOSURE")
            } else {
                None
            };
            if let Some(reason) = risk_rejection {
                warn!(
                    "Rejecting {} entry before placement: {} (candidate_symbol=${:.2}, projected_symbol=${:.2}/${:.2}, pending_reserved=${:.2}, projected_gross=${:.2}/${:.2})",
                    sym_upper,
                    reason,
                    per_symbol_notional,
                    projected_per_symbol,
                    per_symbol_cap,
                    reserved_other,
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

            let durable_before_submit = self.persist_execution_state_for_symbol(
                &sym_upper,
                "single-leg unwind before exchange submission",
            );
            if !durable_before_submit {
                if let Some(chase) = self.chase_states.get_mut(&sym_upper) {
                    chase.phase = ChasePhase::ReconciliationRequired;
                }
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
                if !self
                    .persist_execution_state_for_symbol(&sym_upper, "paper single-leg fill queued")
                {
                    if let Some(chase) = self.chase_states.get_mut(&sym_upper) {
                        chase.phase = ChasePhase::ReconciliationRequired;
                    }
                    return;
                }
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

            let Some(quantity) = self.format_market_quantity_with_price(
                &chase_snapshot.symbol,
                market,
                match active_leg {
                    Leg::Spot => chase_snapshot.spot_quantity,
                    Leg::Futures => chase_snapshot.perp_quantity,
                },
                target_price,
            ) else {
                self.require_chase_reconciliation(
                    &sym_upper,
                    chase_snapshot,
                    &client_order_id,
                    "SINGLE_LEG_MARKET_LOT_FILTER_REJECTED",
                );
                return;
            };
            if let Some(order) = self.internal_orders.get_mut(&client_order_id) {
                order.status = "SUBMITTING".to_string();
            }
            if !self.persist_execution_state_for_symbol(&sym_upper, "single-leg submission started")
            {
                if let Some(chase) = self.chase_states.get_mut(&sym_upper) {
                    chase.phase = ChasePhase::ReconciliationRequired;
                }
                return;
            }
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

            if !self.chase_states.contains_key(&sym_upper) {
                return;
            }
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
                self.acknowledge_submitted_order(&client_order_id);
                if !self.chase_states.contains_key(&sym_upper) {
                    return;
                }
                if !self.persist_execution_state_for_symbol(
                    &sym_upper,
                    "single-leg submission acknowledged",
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        chase_snapshot,
                        &client_order_id,
                        "SINGLE_LEG_ACK_NOT_DURABLE",
                    );
                    return;
                }

                // Spot FULL responses can include exchange trade IDs for every
                // fill. Replay those exact economics. Futures RESULT responses
                // do not carry trade IDs, so a terminal response without a
                // complete `fills` array remains reconciliation-only until the
                // private stream/history supplies authoritative trades.
                if let Ok(parsed) = serde_json::from_str::<serde_json::Value>(&receipt.body)
                    && parsed.get("status").and_then(|s| s.as_str()) == Some("FILLED")
                {
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
                            connection_id: Some("rest-trade-readback".to_string()),
                            exchange_event_time_ms: parsed
                                .get("transactTime")
                                .and_then(|value| value.as_i64()),
                            receive_time_ms: Some(Self::current_time_ms()),
                            process_time_ms: Some(Self::current_time_ms()),
                            persist_time_ms: None,
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
                        // These fills are already authoritative REST evidence.
                        // Apply them through the actor before returning instead
                        // of dropping economics when its own queue is full.
                        Box::pin(self.process_ws_event(evt)).await;
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

        let raw_spot_target = match chase_snapshot.spot_side {
            TradeSide::Buy => spot_top.bid_price,
            TradeSide::Sell => spot_top.ask_price,
        };

        let raw_fut_target = match chase_snapshot.futures_side {
            TradeSide::Buy => perp_top.bid_price,
            TradeSide::Sell => perp_top.ask_price,
        };

        // Cross into base-10 before applying the one-tick OBI skew. Adding a
        // tiny tick to a large binary float can otherwise be a no-op.
        let shift_for_obi = |value: f64, tick: ExactDecimal| {
            let value = Self::exact_live_value(value)?;
            if current_obi > 0.3 {
                value.checked_add(tick)
            } else if current_obi < -0.3 {
                value.checked_sub(tick)
            } else {
                Some(value)
            }
        };
        let spot_target = shift_for_obi(raw_spot_target, spot_tick_size).and_then(|value| {
            Self::quantize_price(value, spot_tick_size, chase_snapshot.spot_side)
        });
        let fut_target = shift_for_obi(raw_fut_target, futures_tick_size).and_then(|value| {
            Self::quantize_price(value, futures_tick_size, chase_snapshot.futures_side)
        });
        let prices_within_filters = spot_target.is_some_and(|price| {
            price.is_positive()
                && price >= symbol_info.spot_min_price
                && price <= symbol_info.spot_max_price
        }) && fut_target.is_some_and(|price| {
            price.is_positive()
                && price >= symbol_info.futures_min_price
                && price <= symbol_info.futures_max_price
        });
        if !prices_within_filters {
            error!(
                "Normalized maker prices are invalid for {}: spot_target={:?} fut_target={:?}",
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

        let spot_target_exact = spot_target.expect("filter check proved exact spot target");
        let fut_target_exact = fut_target.expect("filter check proved exact futures target");
        let Some(spot_price_str) = spot_target_exact.format_to_scale(spot_tick_size.scale()) else {
            self.require_chase_reconciliation(
                &sym_upper,
                chase_snapshot,
                "",
                "SPOT_PRICE_FORMAT_OVERFLOW",
            );
            return;
        };
        let Some(fut_price_str) = fut_target_exact.format_to_scale(futures_tick_size.scale())
        else {
            self.require_chase_reconciliation(
                &sym_upper,
                chase_snapshot,
                "",
                "FUTURES_PRICE_FORMAT_OVERFLOW",
            );
            return;
        };
        let (Some(spot_target), Some(fut_target)) =
            (spot_target_exact.to_f64(), fut_target_exact.to_f64())
        else {
            self.require_chase_reconciliation(
                &sym_upper,
                chase_snapshot,
                "",
                "PRICE_CONVERSION_OVERFLOW",
            );
            return;
        };
        let spot_qty_str = self.format_quantity_for_market(
            &chase_snapshot.symbol,
            MarketType::Spot,
            chase_snapshot.spot_quantity,
            false,
        );
        let fut_qty_str = self.format_quantity_for_market(
            &chase_snapshot.symbol,
            MarketType::Perp,
            chase_snapshot.perp_quantity,
            false,
        );
        let (Some(spot_qty_str), Some(fut_qty_str)) = (spot_qty_str, fut_qty_str) else {
            if chase_snapshot.is_exit {
                self.require_chase_reconciliation(
                    &sym_upper,
                    chase_snapshot,
                    "",
                    "PAIRED_EXIT_LIMIT_LOT_FILTER_REJECTED",
                );
            } else {
                if !self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    chase_snapshot.cycle_client_order_id(),
                    0.0,
                    false,
                    "LIMIT_LOT_FILTER_REJECTED",
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        chase_snapshot,
                        "",
                        "TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&sym_upper, "limit lot filter rejected entry");
            }
            return;
        };

        if !chase_snapshot.is_exit {
            let equal_common_quantity = spot_qty_str
                .parse::<ExactDecimal>()
                .ok()
                .zip(fut_qty_str.parse::<ExactDecimal>().ok())
                .is_some_and(|(spot, futures)| spot == futures);
            if !equal_common_quantity {
                error!(
                    "Refusing unequal entry quantities immediately before submission for {}: spot={} futures={}",
                    chase_snapshot.symbol, spot_qty_str, fut_qty_str
                );
                if !self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    chase_snapshot.cycle_client_order_id(),
                    0.0,
                    false,
                    "UNEQUAL_ENTRY_QUANTITIES",
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        chase_snapshot,
                        "",
                        "TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&sym_upper, "unequal entry quantities rejected");
                return;
            }
        }

        let spot_maker_notional = spot_qty_str
            .parse::<ExactDecimal>()
            .ok()
            .and_then(|quantity| quantity.checked_mul(spot_target_exact));
        let futures_maker_notional = fut_qty_str
            .parse::<ExactDecimal>()
            .ok()
            .and_then(|quantity| quantity.checked_mul(fut_target_exact));
        let maker_notionals_valid = spot_maker_notional.is_some_and(|notional| {
            notional >= symbol_info.spot_min_notional
                && symbol_info
                    .spot_max_notional
                    .is_none_or(|maximum| notional <= maximum)
        }) && futures_maker_notional.is_some_and(|notional| {
            notional >= symbol_info.futures_min_notional
                && symbol_info
                    .futures_max_notional
                    .is_none_or(|maximum| notional <= maximum)
        });
        if !maker_notionals_valid {
            if chase_snapshot.is_exit {
                self.require_chase_reconciliation(
                    &sym_upper,
                    chase_snapshot,
                    "",
                    "PAIRED_EXIT_LIMIT_NOTIONAL_FILTER_REJECTED",
                );
            } else {
                if !self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    chase_snapshot.cycle_client_order_id(),
                    0.0,
                    false,
                    "LIMIT_NOTIONAL_FILTER_REJECTED",
                ) {
                    self.require_chase_reconciliation(
                        &sym_upper,
                        chase_snapshot,
                        "",
                        "TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&sym_upper, "limit notional filter rejected entry");
            }
            return;
        }

        if self.trading_mode != "paper" && chase_snapshot.spot_side == TradeSide::Buy {
            let required_quote = spot_target * chase_snapshot.spot_quantity;
            let quote_asset =
                Self::quote_asset_for_symbol(&chase_snapshot.symbol).unwrap_or("UNKNOWN_QUOTE");
            let available_quote = self
                .spot_available_balances
                .get(quote_asset)
                .copied()
                .unwrap_or(0.0);
            let reserved_other =
                self.pending_spot_collateral_reserved_usd(quote_asset, Some(&sym_upper));
            if !self.spot_collateral_available_for_entry(
                &chase_snapshot.symbol,
                required_quote,
                Some(&sym_upper),
            ) {
                error!(
                    "Insufficient spot {} collateral for {}. Required: {}, reserved by other entries: {}, available: {}",
                    quote_asset,
                    chase_snapshot.symbol,
                    required_quote,
                    reserved_other,
                    available_quote
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

        let durable_before_submit = self.persist_execution_state_for_symbol(
            &sym_upper,
            "dual-maker orders before exchange submission",
        );
        if !durable_before_submit {
            if let Some(chase) = self.chase_states.get_mut(&sym_upper) {
                chase.phase = ChasePhase::ReconciliationRequired;
            }
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
            if let Err(err) = self.arm_cycle_deadline(&sym_upper) {
                let durable_chase = self
                    .chase_states
                    .get(&sym_upper)
                    .cloned()
                    .unwrap_or_else(|| chase_snapshot.clone());
                error!("Paper cycle deadline could not be armed for {sym_upper}: {err}");
                self.require_chase_reconciliation(
                    &sym_upper,
                    durable_chase,
                    "",
                    "CYCLE_DEADLINE_NOT_DURABLE",
                );
            }
            return;
        }

        if let Some(order) = self
            .internal_orders
            .get_mut(&chase_snapshot.spot_client_order_id)
        {
            order.status = "SUBMITTING".to_string();
        }
        if !self.persist_execution_state_for_symbol(&sym_upper, "spot maker submission started") {
            if let Some(chase) = self.chase_states.get_mut(&sym_upper) {
                chase.phase = ChasePhase::ReconciliationRequired;
            }
            return;
        }
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
        if !self.chase_states.contains_key(&sym_upper) {
            return;
        }
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
                self.acknowledge_submitted_order(&chase_snapshot.spot_client_order_id);
                if !self.chase_states.contains_key(&sym_upper) {
                    return;
                }
                if let Err(err) = self.arm_cycle_deadline(&sym_upper) {
                    let durable_chase = self
                        .chase_states
                        .get(&sym_upper)
                        .cloned()
                        .unwrap_or_else(|| chase_snapshot.clone());
                    error!("Absolute cycle deadline could not be armed for {sym_upper}: {err}");
                    self.require_chase_reconciliation(
                        &sym_upper,
                        durable_chase,
                        &chase_snapshot.spot_client_order_id,
                        "CYCLE_DEADLINE_NOT_DURABLE",
                    );
                    return;
                }
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
        if !self.persist_execution_state_for_symbol(&sym_upper, "futures maker submission started")
        {
            if let Some(chase) = self.chase_states.get_mut(&sym_upper) {
                chase.phase = ChasePhase::ReconciliationRequired;
            }
            return;
        }

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

        if !self.chase_states.contains_key(&sym_upper) {
            return;
        }
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
                self.acknowledge_submitted_order(&chase_snapshot.futures_client_order_id);
                if let Some(c) = self.chase_states.get_mut(&sym_upper)
                    && c.phase == ChasePhase::Idle
                {
                    c.phase = ChasePhase::DualMakerPlaced;
                }
                if !self.chase_states.contains_key(&sym_upper) {
                    return;
                }
                if !self.persist_execution_state_for_symbol(
                    &sym_upper,
                    "dual-maker submissions acknowledged",
                ) {
                    let durable_chase = self
                        .chase_states
                        .get(&sym_upper)
                        .cloned()
                        .unwrap_or_else(|| chase_snapshot.clone());
                    self.require_chase_reconciliation(
                        &sym_upper,
                        durable_chase,
                        &chase_snapshot.futures_client_order_id,
                        "DUAL_MAKER_ACK_NOT_DURABLE",
                    );
                }
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
                let Some(mut durable_chase) = self.chase_states.get(&sym_upper).cloned() else {
                    return;
                };
                let cancel_snapshot = spot_cancel.and_then(|body| {
                    Self::parse_terminal_order_snapshot(&body, &chase_snapshot.spot_client_order_id)
                });
                if let Err(cancel_err) = cancel_snapshot.and_then(|snapshot| {
                    self.apply_terminal_order_snapshot(
                        &mut durable_chase,
                        Leg::Spot,
                        &chase_snapshot.spot_client_order_id,
                        snapshot,
                    )
                }) {
                    error!(
                        "Spot cancel unresolved during futures failure cleanup for {}: {}",
                        chase_snapshot.symbol, cancel_err
                    );
                }
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
        let (spot_account_body, exchange_spot_account) = match self.get_spot_account_pumped().await
        {
            Ok(json_str) => match Self::parse_spot_account_balances(&json_str) {
                Ok(balances) => (json_str, balances),
                Err(err) => {
                    error!("Position audit could not parse spot account: {}", err);
                    self.state = SystemState::Reconciling;
                    self.emit_execution_readiness("BLOCKED", "runtime_spot_account_invalid_json");
                    return;
                }
            },
            Err(e) => {
                error!("Position audit failed to fetch spot account: {}", e);
                self.state = SystemState::Reconciling;
                self.emit_execution_readiness("BLOCKED", "runtime_spot_account_unavailable");
                return;
            }
        };
        self.spot_balances = exchange_spot_account.total.clone();
        self.spot_available_balances = exchange_spot_account.available;

        // 2. Fetch futures positions
        let (futures_positions_body, exchange_positions) =
            match self.get_futures_positions_pumped().await {
                Ok(json_str) => match Self::parse_futures_positions(&json_str) {
                    Ok(positions) => (json_str, positions),
                    Err(err) => {
                        error!("Position audit could not parse futures positions: {}", err);
                        self.state = SystemState::Reconciling;
                        self.emit_execution_readiness("BLOCKED", "runtime_positions_invalid_json");
                        return;
                    }
                },
                Err(e) => {
                    error!("Position audit failed to fetch futures positions: {}", e);
                    self.state = SystemState::Reconciling;
                    self.emit_execution_readiness("BLOCKED", "runtime_positions_unavailable");
                    return;
                }
            };

        let futures_account_body = match self.get_futures_account_pumped().await {
            Ok(body) => body,
            Err(error) => {
                error!("Position audit failed to fetch USD-M account truth: {error}");
                self.state = SystemState::Reconciling;
                self.emit_execution_readiness("BLOCKED", "runtime_usdm_account_unavailable");
                return;
            }
        };
        let futures_position_mode_body = match self.get_futures_position_mode_pumped().await {
            Ok(body) => body,
            Err(error) => {
                error!("Position audit failed to fetch USD-M position mode: {error}");
                self.state = SystemState::Reconciling;
                self.emit_execution_readiness("BLOCKED", "runtime_usdm_position_mode_unavailable");
                return;
            }
        };
        let futures_open_orders = match self.get_open_orders_pumped(LegVenue::UsdtFutures).await {
            Ok(body) => match serde_json::from_str::<Vec<Value>>(&body) {
                Ok(rows) => rows.len(),
                Err(error) => {
                    error!("Position audit USD-M open orders were invalid: {error}");
                    self.state = SystemState::Reconciling;
                    self.emit_execution_readiness("BLOCKED", "runtime_usdm_open_orders_invalid");
                    return;
                }
            },
            Err(error) => {
                error!("Position audit failed to fetch USD-M open orders: {error}");
                self.state = SystemState::Reconciling;
                self.emit_execution_readiness("BLOCKED", "runtime_usdm_open_orders_unavailable");
                return;
            }
        };
        let spot_open_orders = match self.get_open_orders_pumped(LegVenue::Spot).await {
            Ok(body) => match serde_json::from_str::<Vec<Value>>(&body) {
                Ok(rows) => rows.len(),
                Err(error) => {
                    error!("Position audit Spot open orders were invalid: {error}");
                    self.state = SystemState::Reconciling;
                    self.emit_execution_readiness("BLOCKED", "runtime_spot_open_orders_invalid");
                    return;
                }
            },
            Err(error) => {
                error!("Position audit failed to fetch Spot open orders: {error}");
                self.state = SystemState::Reconciling;
                self.emit_execution_readiness("BLOCKED", "runtime_spot_open_orders_unavailable");
                return;
            }
        };
        if let Err(error) = self.apply_signed_account_truth(
            &spot_account_body,
            &futures_account_body,
            &futures_positions_body,
            &futures_position_mode_body,
            spot_open_orders,
            futures_open_orders,
        ) {
            error!("Position audit account truth failed closed: {error}");
            self.standard_spot_account_truth = None;
            self.usdm_account_truth = None;
            self.state = SystemState::Reconciling;
            self.emit_execution_readiness("BLOCKED", "runtime_account_truth_invalid");
            return;
        }

        // Exchange-only, local-only, side, quantity, and unpaired-leg
        // discrepancies are all risk. Never rewrite or delete a local pair
        // from one futures snapshot; retain evidence and revoke readiness.
        let mut divergences = self.futures_position_divergences(&exchange_positions);
        divergences.extend(self.spot_inventory_divergences(&self.spot_balances));
        for (symbol, divergence_type, local_qty, exchange_qty) in &divergences {
            if let Ok(vec) = rmp_serde::to_vec_named(&WsEvent::PositionDivergence {
                symbol: symbol.clone(),
                divergence_type: (*divergence_type).to_string(),
                local_qty: *local_qty,
                exchange_qty: *exchange_qty,
            }) {
                let _ = self.dash_tx.send(vec);
            }
            warn!(
                "Position Audit: {} divergence for {} (local_qty={}, exchange_qty={})",
                divergence_type, symbol, local_qty, exchange_qty
            );
        }

        if !divergences.is_empty() {
            self.state = SystemState::Reconciling;
            let _ = self.persist_execution_state("runtime position divergence");
            self.emit_execution_readiness("BLOCKED", "runtime_position_divergence");
            self.emit_execution_recovery_required("runtime_position_divergence");
            return;
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
            if let Some((reason, target)) = self.continuous_risk_assessment() {
                if matches!(
                    target,
                    ContinuousRiskState::Derisking | ContinuousRiskState::ManualReview
                ) {
                    let _ = self.advance_continuous_risk(target, &reason);
                }
                self.state = SystemState::Reconciling;
                self.emit_execution_readiness("BLOCKED", &reason);
                return;
            }
            self.state = SystemState::Trading;
            if !self.clear_continuous_risk_after_proof(
                "paper execution state authoritatively reconciled",
            ) {
                self.state = SystemState::Reconciling;
                self.emit_execution_readiness(
                    "BLOCKED",
                    "continuous risk terminal state requires operator action",
                );
                return;
            }
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
            ) && let Ok(bal) = wallet_balance.parse::<f64>()
                && bal.is_finite()
            {
                balances_map.insert(asset_name.to_string(), serde_json::json!(bal));
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

        let funding_end_ms = Self::current_time_ms().saturating_add(
            self.binance_rest
                .time_offset
                .load(std::sync::atomic::Ordering::Relaxed),
        );
        let funding_start_ms = funding_end_ms.saturating_sub(7 * 24 * 60 * 60 * 1000);
        let funding_income_body = match self
            .get_futures_funding_income_pumped(funding_start_ms, funding_end_ms)
            .await
        {
            Ok(body) => body,
            Err(err) => {
                warn!("Failed to fetch funding-income history during reconciliation: {err}");
                self.emit_execution_readiness("BLOCKED", "funding_income_history_unavailable");
                return;
            }
        };
        let funding_income_count = match Self::validate_funding_income_history(&funding_income_body)
        {
            Ok(count) => count,
            Err(err) => {
                warn!("Failed to validate funding-income history: {err}");
                self.emit_execution_readiness("BLOCKED", "funding_income_history_invalid");
                return;
            }
        };
        let funding_backfill_event = serde_json::json!({
            "event": "FundingIncomeBackfill",
            "start_time_ms": funding_start_ms,
            "end_time_ms": funding_end_ms,
            "records": funding_income_count,
        });
        if let Ok(payload) = rmp_serde::to_vec_named(&funding_backfill_event) {
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
        let parsed_spot_account = match Self::parse_spot_account_balances(&spot_json) {
            Ok(value) => value,
            Err(err) => {
                warn!("Failed to parse spot account during reconciliation: {err}");
                self.emit_execution_readiness("BLOCKED", "spot_account_invalid_json");
                return;
            }
        };
        self.spot_balances = parsed_spot_account.total;
        self.spot_available_balances = parsed_spot_account.available;

        info!("[Step 3/4] Mapping internal orders to exchange truth and searching for orphans.");

        let mut exchange_known_client_ids: std::collections::HashSet<String> =
            std::collections::HashSet::new();
        let mut bot_open_orders = Vec::<(String, String, LegVenue)>::new();
        let mut unexplained_open_orders = Vec::<String>::new();

        for order in &exchange_open_orders {
            if let Some(client_id) = order.get("clientOrderId").and_then(|v| v.as_str()) {
                exchange_known_client_ids.insert(client_id.to_string());
                let Some(symbol) = order.get("symbol").and_then(Value::as_str) else {
                    self.emit_execution_readiness("BLOCKED", "open_order_missing_symbol");
                    return;
                };
                if client_id.starts_with("bngs_") {
                    let venue =
                        if order.get("_bongus_market").and_then(Value::as_str) == Some("spot") {
                            LegVenue::Spot
                        } else {
                            LegVenue::UsdtFutures
                        };
                    bot_open_orders.push((client_id.to_string(), symbol.to_uppercase(), venue));
                } else {
                    unexplained_open_orders.push(client_id.to_string());
                }
            }
        }

        // A recovered known maker is just as capable of filling as an orphan.
        // Freeze every strategy-owned generation, then merge the cancel/fill
        // snapshot into its logical cycle before considering READY.
        for (client_id, symbol, venue) in &bot_open_orders {
            let cancel_body = match self.cancel_order_pumped(*venue, symbol, client_id).await {
                Ok(body) => body,
                Err(err) => {
                    warn!("Failed to cancel bot-owned startup order {client_id}: {err}");
                    self.emit_execution_readiness("BLOCKED", "bot_owned_order_cancel_failed");
                    return;
                }
            };
            let snapshot = match Self::parse_terminal_order_snapshot(&cancel_body, client_id) {
                Ok(snapshot) => snapshot,
                Err(err) => {
                    warn!(
                        "Startup cancel for {client_id} lacked an authoritative terminal snapshot: {err}"
                    );
                    self.emit_execution_readiness("BLOCKED", "startup_cancel_snapshot_invalid");
                    return;
                }
            };
            let matching_symbol = self
                .chase_states
                .iter()
                .find(|(_, chase)| chase.leg_for_client_order_id(client_id).is_some())
                .map(|(key, _)| key.clone());
            if let Some(chase_symbol) = matching_symbol {
                let Some(mut latest) = self.chase_states.get(&chase_symbol).cloned() else {
                    self.emit_execution_readiness("BLOCKED", "startup_chase_changed_during_cancel");
                    return;
                };
                let Some(leg) = latest.leg_for_client_order_id(client_id) else {
                    self.emit_execution_readiness("BLOCKED", "startup_cancel_lineage_changed");
                    return;
                };
                if let Err(err) =
                    self.apply_terminal_order_snapshot(&mut latest, leg, client_id, snapshot)
                {
                    warn!("Startup cancel/fill aggregation failed for {client_id}: {err}");
                    self.require_chase_reconciliation(
                        &chase_symbol,
                        latest,
                        client_id,
                        "STARTUP_CANCEL_AGGREGATION_FAILED",
                    );
                    return;
                }
                if !self.store_chase_state(
                    chase_symbol.clone(),
                    latest,
                    "startup known order cancel snapshot",
                ) {
                    self.emit_execution_readiness("BLOCKED", "startup_cancel_not_durable");
                    return;
                }
            } else {
                let previous = self
                    .order_cumulative_fills
                    .get(client_id)
                    .copied()
                    .unwrap_or(0.0);
                if snapshot.cumulative_filled_qty > previous + 1e-12 {
                    warn!(
                        "Startup found a fill on bot order {client_id} without durable cycle lineage"
                    );
                    self.emit_execution_readiness("BLOCKED", "orphan_order_fill_unattributed");
                    self.emit_execution_recovery_required("orphan_order_fill_unattributed");
                    return;
                }
                self.order_cumulative_fills
                    .insert(client_id.clone(), snapshot.cumulative_filled_qty);
                if let Some(order) = self.internal_orders.get_mut(client_id) {
                    order.status = match snapshot.status {
                        ExchangeOrderStatus::Filled => "FILLED",
                        ExchangeOrderStatus::Canceled => "CANCELED",
                        ExchangeOrderStatus::Rejected => "REJECTED",
                        ExchangeOrderStatus::Expired => "EXPIRED",
                        ExchangeOrderStatus::ExpiredInMatch => "EXPIRED_IN_MATCH",
                        _ => "UNKNOWN",
                    }
                    .to_string();
                }
            }
        }
        if !unexplained_open_orders.is_empty() {
            warn!(
                "Unexplained non-strategy open orders remain on the trading account: {:?}",
                unexplained_open_orders
            );
            self.emit_execution_readiness("BLOCKED", "unexplained_account_open_orders");
            self.emit_execution_recovery_required("unexplained_account_open_orders");
            return;
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
                    let snapshot = match Self::parse_terminal_order_snapshot(&body, &client_id) {
                        Ok(snapshot) => snapshot,
                        Err(err) => {
                            warn!(
                                "Dangling order {client_id} did not resolve to authoritative terminal state: {err}"
                            );
                            self.emit_execution_readiness("BLOCKED", "dangling_order_not_terminal");
                            return;
                        }
                    };
                    let matching_symbol = self
                        .chase_states
                        .iter()
                        .find(|(_, chase)| chase.leg_for_client_order_id(&client_id).is_some())
                        .map(|(key, _)| key.clone());
                    if let Some(chase_symbol) = matching_symbol {
                        let Some(mut latest) = self.chase_states.get(&chase_symbol).cloned() else {
                            self.emit_execution_readiness(
                                "BLOCKED",
                                "dangling_chase_changed_during_query",
                            );
                            return;
                        };
                        let Some(leg) = latest.leg_for_client_order_id(&client_id) else {
                            self.emit_execution_readiness(
                                "BLOCKED",
                                "dangling_order_lineage_changed",
                            );
                            return;
                        };
                        if let Err(err) = self.apply_terminal_order_snapshot(
                            &mut latest,
                            leg,
                            &client_id,
                            snapshot,
                        ) {
                            warn!("Dangling order aggregation failed for {client_id}: {err}");
                            self.require_chase_reconciliation(
                                &chase_symbol,
                                latest,
                                &client_id,
                                "DANGLING_ORDER_AGGREGATION_FAILED",
                            );
                            return;
                        }
                        if !self.store_chase_state(
                            chase_symbol,
                            latest,
                            "dangling order terminal snapshot",
                        ) {
                            self.emit_execution_readiness(
                                "BLOCKED",
                                "dangling_order_snapshot_not_durable",
                            );
                            return;
                        }
                    } else if let Some(order) = self.internal_orders.get_mut(&client_id) {
                        order.status = match snapshot.status {
                            ExchangeOrderStatus::Filled => "FILLED",
                            ExchangeOrderStatus::Canceled => "CANCELED",
                            ExchangeOrderStatus::Rejected => "REJECTED",
                            ExchangeOrderStatus::Expired => "EXPIRED",
                            ExchangeOrderStatus::ExpiredInMatch => "EXPIRED_IN_MATCH",
                            _ => "UNKNOWN",
                        }
                        .to_string();
                    }
                }
                Err(e) => {
                    warn!(
                        "Failed to query dangling order {}: {}. Marking stale.",
                        client_id, e
                    );
                    self.emit_execution_readiness("BLOCKED", "dangling_order_query_failed");
                    return;
                }
            }
        }

        // A crash with resting but entirely unfilled makers can now converge:
        // both concrete generations have authoritative terminal state and no
        // economic effect. Filled/partial cycles retain their original targets
        // and remain blocked for explicit exposure repair.
        let recovered_symbols: Vec<String> = self.chase_states.keys().cloned().collect();
        for symbol in recovered_symbols {
            let Some(mut chase) = self.chase_states.get(&symbol).cloned() else {
                continue;
            };
            let tolerance = chase
                .spot_quantity
                .max(chase.perp_quantity)
                .mul_add(1e-9, 1e-12);
            let no_fill = chase.spot_cumulative_filled <= tolerance
                && chase.futures_cumulative_filled <= tolerance;
            let all_active_terminal = [Leg::Spot, Leg::Futures]
                .into_iter()
                .filter(|leg| match leg {
                    Leg::Spot => chase.has_spot_leg(),
                    Leg::Futures => chase.has_futures_leg(),
                })
                .all(|leg| {
                    self.internal_orders
                        .get(chase.active_client_order_id(leg))
                        .is_some_and(|order| is_terminal_internal_status(&order.status))
                });
            if no_fill && all_active_terminal {
                if !self.emit_cycle_order_update(
                    &chase,
                    "CANCELED",
                    chase.cycle_client_order_id(),
                    0.0,
                    false,
                    "STARTUP_RECONCILE_CANCELED_UNFILLED",
                ) {
                    self.require_chase_reconciliation(
                        &symbol,
                        chase,
                        "",
                        "STARTUP_TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&symbol, "startup unfilled chase canceled");
                continue;
            }
            if chase.both_legs_terminal() {
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
                        &symbol,
                        chase,
                        "",
                        "STARTUP_TERMINAL_ACK_NOT_DURABLE",
                    );
                    return;
                }
                self.remove_chase_state(&symbol, "startup fully filled chase rebuilt");
                continue;
            }
            if all_active_terminal {
                self.require_chase_reconciliation(
                    &symbol,
                    chase,
                    "",
                    "STARTUP_PARTIAL_CYCLE_REQUIRES_REPAIR",
                );
                return;
            }
        }

        // Re-query after orphan cancellation and dangling-order resolution.
        // A successful DELETE response is not itself proof that the final
        // account surface is clear, especially across cancel/fill races.
        for venue in [LegVenue::Spot, LegVenue::UsdtFutures] {
            let final_open_orders = match self.get_open_orders_pumped(venue).await {
                Ok(body) => body,
                Err(err) => {
                    warn!("Final open-order reconciliation query failed: {err}");
                    self.emit_execution_readiness(
                        "BLOCKED",
                        "final_open_orders_requery_unavailable",
                    );
                    return;
                }
            };
            let rows: Vec<Value> = match serde_json::from_str(&final_open_orders) {
                Ok(rows) => rows,
                Err(err) => {
                    warn!("Final open-order reconciliation JSON was invalid: {err}");
                    self.emit_execution_readiness("BLOCKED", "final_open_orders_invalid_json");
                    return;
                }
            };
            if !rows.is_empty() {
                let reason = if rows.iter().any(|row| {
                    row.get("clientOrderId")
                        .and_then(Value::as_str)
                        .is_some_and(|client_id| client_id.starts_with("bngs_"))
                }) {
                    "bot_open_orders_remain_after_reconcile"
                } else {
                    "unexplained_open_orders_appeared_during_reconcile"
                };
                self.emit_execution_readiness("BLOCKED", reason);
                self.emit_execution_recovery_required(reason);
                return;
            }
        }

        let final_positions_body = match self.get_futures_positions_pumped().await {
            Ok(body) => body,
            Err(err) => {
                warn!("Final futures-position reconciliation query failed: {err}");
                self.emit_execution_readiness("BLOCKED", "futures_positions_unavailable");
                return;
            }
        };
        let final_positions = match Self::parse_futures_positions(&final_positions_body) {
            Ok(positions) => positions,
            Err(err) => {
                warn!("Final futures-position reconciliation parse failed: {err}");
                self.emit_execution_readiness("BLOCKED", "futures_positions_invalid_json");
                return;
            }
        };
        let divergences = self.futures_position_divergences(&final_positions);
        if !divergences.is_empty() {
            for (symbol, divergence_type, local_qty, exchange_qty) in divergences {
                if let Ok(payload) = rmp_serde::to_vec_named(&WsEvent::PositionDivergence {
                    symbol,
                    divergence_type: divergence_type.to_string(),
                    local_qty,
                    exchange_qty,
                }) {
                    let _ = self.dash_tx.send(payload);
                }
            }
            self.emit_execution_readiness("BLOCKED", "startup_position_divergence");
            self.emit_execution_recovery_required("startup_position_divergence");
            return;
        }

        let final_spot_account = match self.get_spot_account_pumped().await {
            Ok(body) => body,
            Err(err) => {
                warn!("Final spot-account reconciliation query failed: {err}");
                self.emit_execution_readiness("BLOCKED", "final_spot_account_unavailable");
                return;
            }
        };
        let final_spot_balances = match Self::parse_spot_account_balances(&final_spot_account) {
            Ok(balances) => balances,
            Err(err) => {
                warn!("Final spot-account reconciliation parse failed: {err}");
                self.emit_execution_readiness("BLOCKED", "final_spot_account_invalid_json");
                return;
            }
        };
        self.spot_balances = final_spot_balances.total;
        self.spot_available_balances = final_spot_balances.available;
        let spot_divergences = self.spot_inventory_divergences(&self.spot_balances);
        if !spot_divergences.is_empty() {
            for (symbol, divergence_type, local_qty, exchange_qty) in spot_divergences {
                if let Ok(payload) = rmp_serde::to_vec_named(&WsEvent::PositionDivergence {
                    symbol,
                    divergence_type: divergence_type.to_string(),
                    local_qty,
                    exchange_qty,
                }) {
                    let _ = self.dash_tx.send(payload);
                }
            }
            self.emit_execution_readiness("BLOCKED", "startup_spot_inventory_divergence");
            self.emit_execution_recovery_required("startup_spot_inventory_divergence");
            return;
        }

        for (symbol, position) in &self.tracked_positions {
            let Some(spot) = position.spot.as_ref() else {
                continue;
            };
            if Self::side_is_long(&spot.side) != Some(true) {
                self.emit_execution_readiness("BLOCKED", "short_spot_liability_unreconciled");
                self.emit_execution_recovery_required("short_spot_liability_unreconciled");
                return;
            }
            if Self::base_asset_for_symbol(symbol).is_none() {
                self.emit_execution_readiness("BLOCKED", "spot_base_asset_unresolved");
                return;
            }
        }

        let final_futures_account = match self.get_futures_account_pumped().await {
            Ok(body) => body,
            Err(error) => {
                warn!("Final USD-M account-truth query failed: {error}");
                self.emit_execution_readiness("BLOCKED", "final_usdm_account_unavailable");
                return;
            }
        };
        let final_futures_position_mode = match self.get_futures_position_mode_pumped().await {
            Ok(body) => body,
            Err(error) => {
                warn!("Final USD-M position-mode query failed: {error}");
                self.emit_execution_readiness("BLOCKED", "final_usdm_position_mode_unavailable");
                return;
            }
        };
        if let Err(error) = self.apply_signed_account_truth(
            &final_spot_account,
            &final_futures_account,
            &final_positions_body,
            &final_futures_position_mode,
            0,
            0,
        ) {
            warn!("Final signed account topology is incomplete: {error}");
            self.standard_spot_account_truth = None;
            self.usdm_account_truth = None;
            self.emit_execution_readiness("BLOCKED", "final_account_truth_invalid");
            return;
        }

        if !self.private_stream_quorum_ready() {
            self.emit_execution_readiness("BLOCKED", "private_stream_quorum_lost_during_reconcile");
            return;
        }

        if !self.clear_symbol_persistence_latches_after_reconciliation() {
            self.state = SystemState::Reconciling;
            self.emit_execution_readiness("BLOCKED", "symbol_persistence_latch_clear_not_durable");
            self.emit_execution_recovery_required("symbol_persistence_latch_clear_not_durable");
            return;
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
        // Account/open-order proof is necessary but not sufficient for entry
        // readiness. Clock, quota, persistence, storage, margin, and price
        // breakers must also be clear at the exact point READY is emitted.
        if let Some((reason, target)) = self.continuous_risk_assessment() {
            if matches!(
                target,
                ContinuousRiskState::Derisking | ContinuousRiskState::ManualReview
            ) {
                let _ = self.advance_continuous_risk(target, &reason);
            }
            self.state = SystemState::Reconciling;
            self.emit_execution_readiness("BLOCKED", &reason);
            return;
        }
        self.state = SystemState::Trading;
        if !self.clear_continuous_risk_after_proof(
            "signed spot/futures account and open-order truth reconciled",
        ) {
            self.state = SystemState::Reconciling;
            self.emit_execution_readiness(
                "BLOCKED",
                "continuous risk terminal state requires operator action",
            );
            return;
        }
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

    fn decimal(value: &str) -> ExactDecimal {
        value.parse().expect("valid test decimal")
    }

    fn paper_test_manager() -> OrderManager {
        paper_test_manager_with_dashboard().0
    }

    fn paper_test_manager_with_dashboard()
    -> (OrderManager, tokio::sync::broadcast::Receiver<Vec<u8>>) {
        let (_event_tx, event_rx) = mpsc::channel(8);
        let (engine_tx, _engine_rx) = mpsc::channel(16);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, dash_rx) = broadcast::channel::<Vec<u8>>(128);
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
        for symbol in [
            "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT", "TONUSDT",
        ] {
            for role in ["spot-public", "futures-public", "futures-market"] {
                manager
                    .market_stream_ready_roles
                    .insert(OrderManager::market_stream_role_key(symbol, role));
            }
        }
        (manager, dash_rx)
    }

    fn install_fresh_nonpaper_account_truth(manager: &mut OrderManager) {
        let observed_at_ms = OrderManager::current_time_ms();
        manager.standard_spot_account_truth = Some(StandardSpotAccountTruth {
            wallet_balance: HashMap::new(),
            available_balance: HashMap::new(),
            open_orders: 0,
            borrow_state: "NOT_APPLICABLE_STANDARD_SPOT".to_string(),
            observed_at_ms,
        });
        manager.usdm_account_truth = Some(UsdmAccountRiskTruth {
            wallet_balance: "10000".to_string(),
            available_balance: "10000".to_string(),
            positions: HashMap::new(),
            maintenance_margin: "0".to_string(),
            margin_ratio: 0.0,
            liquidation_price: HashMap::new(),
            position_mode: "ONE_WAY".to_string(),
            open_orders: 0,
            observed_at_ms,
        });
    }

    fn prepare_emergency_market(manager: &mut OrderManager, symbol: &str) {
        manager
            .exchange_info
            .insert(symbol.to_string(), test_exchange_symbol(symbol));
        manager.spot_mid_cache.insert(symbol.to_string(), 100.0);
        manager.perp_mid_cache.insert(symbol.to_string(), 100.0);
        let top = TopOfBook {
            bid_price: 100.0,
            ask_price: 100.0,
            bid_qty: 100.0,
            ask_qty: 100.0,
        };
        manager.spot_top_cache.insert(symbol.to_string(), top);
        manager.perp_top_cache.insert(symbol.to_string(), top);
    }

    fn emergency_exit_instruction(
        symbol: &str,
        actual_spot: &str,
        actual_futures: &str,
        exit_spot: &str,
        exit_futures: &str,
    ) -> crate::ipc::AlphaInstruction {
        let requested = decimal(actual_spot).max(decimal(actual_futures));
        let spot_exit = decimal(exit_spot);
        let futures_exit = decimal(exit_futures);
        crate::ipc::AlphaInstruction {
            symbol: Some(symbol.to_string()),
            intent: "EXIT_LONG".to_string(),
            quantity: requested.to_f64().expect("test request fits f64"),
            urgency: 1.0,
            max_slippage_bps: 50.0,
            exposure_scale: 1.0,
            intent_id: Some("emergency-test-intent".to_string()),
            direction: Some("long".to_string()),
            spot_quantity: Some(spot_exit.to_f64().expect("test spot exit fits f64")),
            perp_quantity: Some(futures_exit.to_f64().expect("test futures exit fits f64")),
            requested_quantity_decimal: Some(requested.to_string()),
            actual_spot_inventory_decimal: Some(actual_spot.to_string()),
            actual_futures_inventory_decimal: Some(actual_futures.to_string()),
            exit_spot_quantity_decimal: Some(exit_spot.to_string()),
            exit_futures_quantity_decimal: Some(exit_futures.to_string()),
            skip_spot_leg: spot_exit == ExactDecimal::ZERO,
            skip_perp_leg: futures_exit == ExactDecimal::ZERO,
            route_policy: Some("emergency_reduce_only".to_string()),
            route_model_version: Some("emergency-v1".to_string()),
            ..crate::ipc::AlphaInstruction::default()
        }
        .seal_internal()
    }

    #[test]
    fn signed_emergency_readback_preserves_precision_and_rejects_noncanonical_json() {
        let balances = OrderManager::parse_exact_spot_account_balances(
            r#"{"balances":[{"asset":"BTC","free":"0.1234567890123456789012345678","locked":"0.0000000000000000000000000001"}]}"#,
        )
        .expect("canonical strings preserve all supported decimal digits");
        assert_eq!(
            balances.total["BTC"].to_string(),
            "0.1234567890123456789012345679"
        );
        let futures = OrderManager::parse_exact_futures_positions(
            r#"[{"symbol":"BTCUSDT","positionAmt":"-0.1234567890123456789012345678","positionSide":"BOTH"}]"#,
        )
        .expect("signed futures strings remain exact");
        assert_eq!(
            futures["BTCUSDT"].to_string(),
            "-0.1234567890123456789012345678"
        );
        assert!(
            OrderManager::parse_exact_spot_account_balances(
                r#"{"balances":[{"asset":"BTC","free":0.1,"locked":"0"}]}"#
            )
            .is_err()
        );
        assert!(
            OrderManager::parse_exact_futures_positions(
                r#"[{"symbol":"BTCUSDT","positionAmt":"-0.1000","positionSide":"BOTH"}]"#
            )
            .is_err()
        );

        let mut manager = paper_test_manager();
        let mut filters = test_exchange_symbol("BTCUSDT");
        let increment = decimal("0.000000000000000003");
        filters.spot_step_size = increment;
        filters.spot_market_step_size = increment;
        filters.spot_min_qty = increment;
        filters.spot_market_min_qty = increment;
        manager.exchange_info.insert("BTCUSDT".to_string(), filters);
        assert_eq!(
            manager
                .normalize_exact_quantity_for_market(
                    "BTCUSDT",
                    MarketType::Spot,
                    decimal("0.123456789012345679"),
                )
                .expect("precision-sensitive quantity remains executable")
                .to_string(),
            "0.123456789012345678"
        );
    }

    #[tokio::test]
    async fn emergency_exit_preserves_unrelated_account_inventory_and_exact_lifetime_budget() {
        let mut manager = paper_test_manager();
        prepare_emergency_market(&mut manager, "BTCUSDT");
        manager.spot_balances.insert("BTC".to_string(), 2.0);
        manager
            .spot_available_balances
            .insert("BTC".to_string(), 2.0);
        let instruction = emergency_exit_instruction("BTCUSDT", "1", "1", "1", "1");
        let intent_id = instruction.intent_id.clone().unwrap();

        manager.handle_alpha_instruction(instruction).await;

        let record = manager
            .emergency_exits
            .get(&intent_id)
            .expect("durable emergency record remains as terminal evidence");
        assert_eq!(record.state, EmergencyExitState::Flat);
        assert_eq!(record.initial_signed_spot_total_decimal, "2");
        assert_eq!(record.cumulative_spot_emergency_filled_decimal, "1");
        assert_eq!(record.cumulative_futures_emergency_filled_decimal, "1");
        assert_eq!(record.signed_spot_total_decimal, "1");
        assert_eq!(record.signed_futures_position_decimal, "0");
        assert_eq!(record.spot_generations.len(), 1);
        assert_eq!(record.spot_generations[0].requested_quantity_decimal, "1");
        assert_eq!(record.spot_generations[0].cumulative_filled_decimal, "1");
        assert_eq!(
            record
                .transitions
                .iter()
                .map(|transition| transition.state)
                .collect::<Vec<_>>(),
            vec![
                EmergencyExitState::Detected,
                EmergencyExitState::CancelingCurrentOrders,
                EmergencyExitState::SignedReadback,
                EmergencyExitState::InventoryClassified,
                EmergencyExitState::ReduceOnlyDerisking,
                EmergencyExitState::VerifyingFlat,
                EmergencyExitState::Flat,
            ]
        );
    }

    #[tokio::test]
    async fn emergency_cancel_includes_equal_and_divergent_terminal_leg_fills() {
        for (spot_fill, futures_fill, expected_futures) in
            [(0.4_f64, 0.4_f64, "0.4"), (0.4_f64, 0.3_f64, "0.3")]
        {
            let mut manager = paper_test_manager();
            prepare_emergency_market(&mut manager, "BTCUSDT");
            let mut chase = dual_test_chase(1.0);
            chase.spot_cumulative_filled = spot_fill;
            chase.futures_cumulative_filled = futures_fill;
            manager
                .chase_states
                .insert("BTCUSDT".to_string(), chase.clone());
            for (client_id, cumulative) in [
                (chase.spot_client_order_id.clone(), spot_fill),
                (chase.futures_client_order_id.clone(), futures_fill),
            ] {
                manager.internal_orders.insert(
                    client_id.clone(),
                    InternalOrder {
                        client_order_id: client_id.clone(),
                        symbol: "BTCUSDT".to_string(),
                        status: "PARTIALLY_FILLED".to_string(),
                        limit_price: Some(100.0),
                    },
                );
                manager.order_cumulative_fills.insert(client_id, cumulative);
            }
            manager.tracked_positions.insert(
                "BTCUSDT".to_string(),
                TrackedPosition {
                    symbol: "BTCUSDT".to_string(),
                    spot: Some(TrackedLegPosition {
                        side: "LONG".to_string(),
                        entry_price: 100.0,
                        quantity: spot_fill,
                        unrealized_pnl: 0.0,
                        last_mark_price: 100.0,
                    }),
                    perp: Some(TrackedLegPosition {
                        side: "SHORT".to_string(),
                        entry_price: 100.0,
                        quantity: futures_fill,
                        unrealized_pnl: 0.0,
                        last_mark_price: 100.0,
                    }),
                },
            );
            let instruction = emergency_exit_instruction(
                "BTCUSDT",
                "0.4",
                expected_futures,
                "0.4",
                expected_futures,
            );
            let intent_id = instruction.intent_id.clone().unwrap();

            manager.handle_alpha_instruction(instruction).await;

            let record = &manager.emergency_exits[&intent_id];
            assert_eq!(record.state, EmergencyExitState::Flat);
            assert_eq!(record.initial_signed_spot_total_decimal, "0.4");
            assert_eq!(
                record.initial_signed_futures_position_decimal,
                format!("-{expected_futures}")
            );
            assert_eq!(record.spot_generations[0].requested_quantity_decimal, "0.4");
            assert_eq!(
                record.futures_generations[0].requested_quantity_decimal,
                expected_futures
            );
            assert_eq!(record.cumulative_spot_emergency_filled_decimal, "0.4");
            assert_eq!(
                record.cumulative_futures_emergency_filled_decimal,
                expected_futures
            );
        }
    }

    #[test]
    fn emergency_transition_checkpoint_recovers_after_every_state_boundary() {
        let mut manager = paper_test_manager();
        prepare_emergency_market(&mut manager, "BTCUSDT");
        let mut chase = dual_test_chase(1.0);
        chase.is_exit = true;
        chase
            .spot_order_aliases
            .push("earlier-exit-spot".to_string());
        manager.chase_states.insert("BTCUSDT".to_string(), chase);
        let mut instruction = emergency_exit_instruction("BTCUSDT", "1", "1", "1", "1");
        instruction.account_id = Some("account-fixture".to_string());
        instruction.environment = Some("paper".to_string());
        instruction.strategy_id = Some("carry-fixture".to_string());
        instruction.cycle_id = Some("exit-cycle-fixture".to_string());
        let intent_id = manager
            .begin_emergency_exit(&instruction, "BTCUSDT")
            .expect("DETECTED is durable");
        let journal = manager.execution_state_journal_path.clone();

        for next in [
            EmergencyExitState::CancelingCurrentOrders,
            EmergencyExitState::SignedReadback,
            EmergencyExitState::InventoryClassified,
            EmergencyExitState::ReduceOnlyDerisking,
            EmergencyExitState::VerifyingFlat,
            EmergencyExitState::Flat,
        ] {
            let mut recovered = paper_test_manager();
            recovered
                .load_execution_state_from_path(journal.clone())
                .expect("every pre-effect transition checkpoint is restartable");
            assert_eq!(
                recovered.emergency_exits[&intent_id].state,
                manager.emergency_exits[&intent_id].state
            );
            assert!(manager.transition_emergency_exit(
                &intent_id,
                next,
                "crash-boundary regression"
            ));
        }
        let (mut recovered, mut dashboard_rx) = paper_test_manager_with_dashboard();
        recovered
            .load_execution_state_from_path(journal.clone())
            .expect("terminal FLAT checkpoint is restartable");
        assert_eq!(
            recovered.emergency_exits[&intent_id].state,
            EmergencyExitState::Flat
        );
        let publication_id = format!("emergency:{intent_id}:FLAT");
        let payload = recovered.terminal_publications[&publication_id].clone();
        assert_eq!(payload["flat_proof"], true);
        assert_eq!(payload["accounting_status"], "RECONCILIATION_REQUIRED");
        assert_eq!(payload["account_id"], "account-fixture");
        assert_eq!(payload["cycle_id"], "exit-cycle-fixture");
        assert_eq!(payload["verified_spot_inventory_decimal"], "0");
        assert_eq!(
            payload["original_exit_spot_client_order_ids"],
            serde_json::json!(["earlier-exit-spot", "spot-cid"])
        );
        assert_eq!(
            payload["original_exit_futures_client_order_ids"],
            serde_json::json!(["fut-cid"])
        );
        assert!(
            payload.get("spot_vwap_decimal").is_none(),
            "flat proof must not fabricate economic fills"
        );
        recovered.replay_terminal_publications();
        let replayed: Value =
            rmp_serde::from_slice(&dashboard_rx.try_recv().expect("exact durable FLAT replay"))
                .unwrap();
        assert_eq!(replayed, payload);
        recovered.complete_terminal_publication(&publication_id);
        let mut handed_off = paper_test_manager();
        handed_off.load_execution_state_from_path(journal).unwrap();
        assert!(handed_off.terminal_publications.is_empty());
    }

    #[tokio::test]
    async fn emergency_partial_generation_restart_submits_only_exact_residual() {
        let mut manager = paper_test_manager();
        prepare_emergency_market(&mut manager, "BTCUSDT");
        let instruction = emergency_exit_instruction("BTCUSDT", "1", "0", "1", "0");
        let intent_id = manager
            .begin_emergency_exit(&instruction, "BTCUSDT")
            .expect("DETECTED is durable");
        assert!(manager.update_emergency_exit(
            &intent_id,
            "seed terminal partial generation",
            |record| {
                record.initial_inventory_captured = true;
                record.initial_signed_spot_total_decimal = "1".to_string();
                record.initial_signed_futures_position_decimal = "0".to_string();
                record.signed_spot_total_decimal = "0.6".to_string();
                record.signed_spot_available_decimal = "0.6".to_string();
                record.signed_futures_position_decimal = "0".to_string();
                record.classified_spot_exit_quantity_decimal = "0.6".to_string();
                record.classified_futures_exit_quantity_decimal = "0".to_string();
                record.cumulative_spot_emergency_filled_decimal = "0.4".to_string();
                record.spot_generations.push(EmergencyRepairGeneration {
                    leg: Leg::Spot,
                    generation: 0,
                    client_order_id: OrderManager::emergency_repair_client_order_id(
                        &intent_id,
                        Leg::Spot,
                        0,
                    ),
                    requested_quantity_decimal: "1".to_string(),
                    cumulative_filled_decimal: "0.4".to_string(),
                    final_status: "CANCELED".to_string(),
                });
            }
        ));
        for next in [
            EmergencyExitState::CancelingCurrentOrders,
            EmergencyExitState::SignedReadback,
            EmergencyExitState::InventoryClassified,
            EmergencyExitState::ReduceOnlyDerisking,
        ] {
            assert!(manager.transition_emergency_exit(
                &intent_id,
                next,
                "partial-generation restart fixture"
            ));
        }
        let journal = manager.execution_state_journal_path.clone();
        let mut recovered = paper_test_manager();
        recovered
            .load_execution_state_from_path(journal)
            .expect("partial generation survives restart");
        prepare_emergency_market(&mut recovered, "BTCUSDT");

        recovered
            .submit_emergency_derisk(&intent_id)
            .await
            .expect("terminal partial generation may create one residual generation");

        let record = &recovered.emergency_exits[&intent_id];
        assert_eq!(record.spot_generations.len(), 2);
        assert_eq!(record.spot_generations[1].generation, 1);
        assert_eq!(record.spot_generations[1].requested_quantity_decimal, "0.6");
        assert_eq!(
            record.spot_generations[1].client_order_id,
            OrderManager::emergency_repair_client_order_id(&intent_id, Leg::Spot, 1)
        );
        assert_eq!(record.cumulative_spot_emergency_filled_decimal, "1");
        let effective = record
            .spot_generations
            .iter()
            .map(|generation| decimal(&generation.cumulative_filled_decimal))
            .try_fold(ExactDecimal::ZERO, |total, fill| total.checked_add(fill))
            .unwrap();
        assert_eq!(effective, decimal("1"));
    }

    #[tokio::test]
    async fn exhausted_emergency_retry_budget_is_durable_manual_review() {
        let mut manager = paper_test_manager();
        let instruction = emergency_exit_instruction("BTCUSDT", "1", "0", "1", "0");
        let intent_id = manager
            .begin_emergency_exit(&instruction, "BTCUSDT")
            .expect("DETECTED is durable");
        assert!(manager.update_emergency_exit(
            &intent_id,
            "seed exhausted verification",
            |record| {
                record.initial_inventory_captured = true;
                record.initial_signed_spot_total_decimal = "1".to_string();
                record.initial_signed_futures_position_decimal = "0".to_string();
                record.signed_spot_total_decimal = "1".to_string();
                record.signed_spot_available_decimal = "1".to_string();
                record.signed_futures_position_decimal = "0".to_string();
                record.derisk_attempts = record.max_retries.saturating_add(1);
            }
        ));
        for next in [
            EmergencyExitState::CancelingCurrentOrders,
            EmergencyExitState::SignedReadback,
            EmergencyExitState::InventoryClassified,
            EmergencyExitState::ReduceOnlyDerisking,
            EmergencyExitState::VerifyingFlat,
        ] {
            assert!(manager.transition_emergency_exit(&intent_id, next, "exhausted retry fixture"));
        }

        manager.drive_emergency_exit(intent_id.clone()).await;
        assert_eq!(
            manager.emergency_exits[&intent_id].state,
            EmergencyExitState::ManualReview
        );
        assert!(
            manager.emergency_exits[&intent_id]
                .last_error
                .contains("exhausted")
        );
        let mut recovered = paper_test_manager();
        recovered
            .load_execution_state_from_path(manager.execution_state_journal_path.clone())
            .expect("MANUAL_REVIEW remains durable after restart");
        assert_eq!(
            recovered.emergency_exits[&intent_id].state,
            EmergencyExitState::ManualReview
        );
    }

    #[test]
    fn nonpaper_entry_quota_gate_requires_fresh_two_venue_capacity() {
        let mut manager = paper_test_manager();
        manager.binance_rest = BinanceRest::new(
            "test-key".to_string(),
            "test-secret".to_string(),
            "testnet".to_string(),
        );
        assert_eq!(
            manager.entry_quota_block_reason(),
            Some("exchange_rate_limit_telemetry_unavailable")
        );

        let now_ms = OrderManager::current_time_ms();
        manager
            .binance_rest
            .set_rate_limit_observations_for_test(6_000, 100, 2_400, 100, now_ms);
        assert_eq!(manager.entry_quota_block_reason(), None);
        manager.binance_rest.set_order_count_observations_for_test(
            LegVenue::UsdtFutures,
            100,
            85,
            1_200,
            100,
            now_ms,
        );
        assert_eq!(
            manager.entry_quota_block_reason(),
            Some("insufficient_exchange_rate_limit_budget")
        );
        assert!(manager.critical_quota_guard().is_ok());
        manager.binance_rest.set_order_count_observations_for_test(
            LegVenue::UsdtFutures,
            100,
            100,
            1_200,
            100,
            now_ms,
        );
        assert_eq!(
            manager.critical_quota_guard().unwrap_err(),
            "exchange_retry_after_or_capacity_exhausted"
        );

        let mut exhausted = paper_test_manager();
        exhausted.binance_rest = BinanceRest::new(
            "test-key".to_string(),
            "test-secret".to_string(),
            "testnet".to_string(),
        );
        exhausted
            .binance_rest
            .set_rate_limit_observations_for_test(6_000, 5_997, 2_400, 2_397, now_ms);
        assert_eq!(
            exhausted.entry_quota_block_reason(),
            Some("insufficient_exchange_rate_limit_budget")
        );
    }

    #[tokio::test]
    async fn strategy_refresh_is_shed_at_seventy_percent_without_network_work() {
        let mut manager = paper_test_manager();
        manager.binance_rest = BinanceRest::new(
            "test-key".to_string(),
            "test-secret".to_string(),
            "testnet".to_string(),
        );
        let now_ms = OrderManager::current_time_ms();
        manager
            .binance_rest
            .set_rate_limit_observations_for_test(6_000, 4_200, 2_400, 1_680, now_ms);
        manager
            .binance_rest
            .set_clock_health_for_test(0, 20, now_ms);

        manager.tick_strategy().await;

        assert!(manager.ranking_engine.last_refresh.is_none());
        assert_eq!(
            manager
                .binance_rest
                .quota_block_reason(RestWorkClass::Nonessential),
            Some("nonessential_exchange_work_shed")
        );
    }

    #[test]
    fn decimal_exchange_quantization_does_not_drop_a_lot_on_binary_noise() {
        let residual = 1.0_f64 - 0.4_f64;
        assert_eq!(
            OrderManager::round_down_to_step(residual, decimal("0.1"))
                .unwrap()
                .to_string(),
            "0.6"
        );
        assert_eq!(
            OrderManager::round_down_to_step(0.657, decimal("0.01"))
                .unwrap()
                .to_string(),
            "0.65"
        );
        assert_eq!(
            OrderManager::quantize_price(
                decimal("100.24999999999999"),
                decimal("0.1"),
                TradeSide::Buy,
            )
            .unwrap()
            .to_string(),
            "100.2"
        );
        assert_eq!(
            OrderManager::quantize_price(
                decimal("100.20000000000002"),
                decimal("0.1"),
                TradeSide::Sell,
            )
            .unwrap()
            .to_string(),
            "100.3"
        );

        let mut manager = paper_test_manager();
        let mut filters = test_exchange_symbol("BTCUSDT");
        filters.futures_market_min_qty = decimal("0.01");
        filters.futures_market_step_size = decimal("0.01");
        filters.futures_market_max_qty = decimal("50");
        manager.exchange_info.insert("BTCUSDT".to_string(), filters);
        assert_eq!(
            manager.normalize_quantity_for_market("BTCUSDT", MarketType::Perp, 0.657),
            Some(0.65)
        );
        assert_eq!(
            manager.format_quantity_for_market("BTCUSDT", MarketType::Perp, 0.657, true),
            Some("0.65".to_string())
        );
        assert_eq!(
            manager.format_quantity_for_market("BTCUSDT", MarketType::Perp, 0.657, false),
            Some("0.657".to_string())
        );
    }

    #[test]
    fn common_entry_quantity_intersects_spot_and_futures_grids_exactly() {
        let mut manager = paper_test_manager();
        let mut filters = test_exchange_symbol("BTCUSDT");
        filters.spot_min_qty = decimal("0.001");
        filters.spot_step_size = decimal("0.001");
        filters.spot_market_min_qty = decimal("0.001");
        filters.spot_market_step_size = decimal("0.001");
        filters.futures_min_qty = decimal("0.01");
        filters.futures_step_size = decimal("0.01");
        filters.futures_market_min_qty = decimal("0.01");
        filters.futures_market_step_size = decimal("0.01");
        manager.exchange_info.insert("BTCUSDT".to_string(), filters);

        let common = manager
            .normalize_common_entry_quantity("BTCUSDT", 0.657)
            .expect("a common quantity exists");
        assert_eq!(common, 0.65);
        let spot = manager
            .format_quantity_for_market("BTCUSDT", MarketType::Spot, common, false)
            .unwrap();
        let futures = manager
            .format_quantity_for_market("BTCUSDT", MarketType::Perp, common, false)
            .unwrap();
        assert_eq!(
            spot.parse::<ExactDecimal>(),
            futures.parse::<ExactDecimal>()
        );
        assert_eq!(spot, "0.650");
        assert_eq!(futures, "0.65");
        assert_eq!(
            manager.normalize_common_entry_quantity("BTCUSDT", 0.009),
            None
        );
    }

    #[test]
    fn common_entry_quantity_uses_lcm_for_non_nested_decimal_grids() {
        let mut manager = paper_test_manager();
        let mut filters = test_exchange_symbol("BTCUSDT");
        filters.spot_min_qty = decimal("0.002");
        filters.spot_step_size = decimal("0.002");
        filters.spot_market_min_qty = decimal("0.002");
        filters.spot_market_step_size = decimal("0.002");
        filters.futures_min_qty = decimal("0.003");
        filters.futures_step_size = decimal("0.003");
        filters.futures_market_min_qty = decimal("0.003");
        filters.futures_market_step_size = decimal("0.003");
        manager.exchange_info.insert("BTCUSDT".to_string(), filters);

        assert_eq!(
            manager.normalize_common_entry_quantity("BTCUSDT", 0.011),
            Some(0.006)
        );
    }

    #[test]
    fn base_asset_commission_reduces_sellable_spot_inventory_exactly_once() {
        let mut manager = paper_test_manager();
        manager.apply_fill_to_position(
            "BTCUSDT",
            MarketType::Spot,
            TradeSide::Buy,
            1.0,
            50_000.0,
            false,
        );

        assert_eq!(
            manager.apply_commission_once(CommissionObservation {
                symbol: "BTCUSDT",
                client_order_id: "spot-cid",
                market: MarketType::Spot,
                amount: 0.001,
                asset: "BTC",
                order_id: Some(1001),
                trade_id: Some(2002),
            }),
            Ok(())
        );
        let inventory = manager
            .tracked_positions
            .get("BTCUSDT")
            .and_then(|position| position.spot.as_ref())
            .map(|leg| leg.quantity)
            .unwrap();
        assert_eq!(
            ExactDecimal::from_f64(inventory).unwrap().to_string(),
            "0.999"
        );

        assert_eq!(
            manager.apply_commission_once(CommissionObservation {
                symbol: "BTCUSDT",
                client_order_id: "spot-cid",
                market: MarketType::Spot,
                amount: 0.001,
                asset: "BTC",
                order_id: Some(1001),
                trade_id: Some(2002),
            }),
            Ok(())
        );
        let duplicate_inventory = manager
            .tracked_positions
            .get("BTCUSDT")
            .and_then(|position| position.spot.as_ref())
            .map(|leg| leg.quantity)
            .unwrap();
        assert_eq!(duplicate_inventory, inventory);
        assert_eq!(
            manager.apply_commission_once(CommissionObservation {
                symbol: "BTCUSDT",
                client_order_id: "spot-cid",
                market: MarketType::Spot,
                amount: 0.002,
                asset: "BTC",
                order_id: Some(1001),
                trade_id: Some(2002),
            }),
            Err("COMMISSION_IDENTITY_CONFLICT")
        );
    }

    #[test]
    fn unknown_commission_asset_is_retained_and_blocks_readiness_after_restart() {
        let journal_path = unique_test_path("unknown-commission", "jsonl");
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = journal_path.clone();
        manager.apply_fill_to_position(
            "BTCUSDT",
            MarketType::Spot,
            TradeSide::Buy,
            1.0,
            50_000.0,
            false,
        );

        assert_eq!(
            manager.apply_commission_once(CommissionObservation {
                symbol: "BTCUSDT",
                client_order_id: "spot-cid",
                market: MarketType::Spot,
                amount: 0.5,
                asset: "MYSTERY",
                order_id: Some(3003),
                trade_id: Some(4004),
            }),
            Err("UNKNOWN_COMMISSION_ASSET")
        );
        assert!(manager.unvalued_commission_assets.contains("MYSTERY"));
        assert!(manager.has_unresolved_execution_effects());
        assert!(manager.persist_execution_state("unknown fee retained"));

        let mut restarted = paper_test_manager();
        restarted
            .load_execution_state_from_path(journal_path)
            .unwrap();
        assert!(restarted.unvalued_commission_assets.contains("MYSTERY"));
        assert!(restarted.has_unresolved_execution_effects());
    }

    #[tokio::test]
    async fn terminal_summary_is_durable_and_carries_exact_net_inventory_and_commissions() {
        let journal_path = unique_test_path("terminal-summary", "jsonl");
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = journal_path.clone();
        let mut dashboard = manager.dash_tx.subscribe();
        let mut chase = dual_test_chase(1.0);
        chase.requested_quantity_decimal = "1.001".to_string();
        chase.normalized_common_entry_quantity_decimal = Some("1".to_string());
        chase.spot_cumulative_filled = 1.0;
        chase.futures_cumulative_filled = 1.0;
        chase.spot_terminal = true;
        chase.futures_terminal = true;
        chase.spot_fill_price = Some(50_000.0);
        chase.futures_fill_price = Some(50_010.0);
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), chase.clone());
        manager.order_lineage.insert(
            "spot-cid".to_string(),
            OrderLineage {
                requested_quantity_decimal: Some("1.001".to_string()),
                risk_adjusted_requested_quantity_decimal: Some("1".to_string()),
                normalized_common_entry_quantity_decimal: Some("1".to_string()),
                ..OrderLineage::default()
            },
        );
        for client_id in ["spot-cid", "fut-cid"] {
            manager.internal_orders.insert(
                client_id.to_string(),
                InternalOrder {
                    client_order_id: client_id.to_string(),
                    symbol: "BTCUSDT".to_string(),
                    status: "FILLED".to_string(),
                    limit_price: Some(50_000.0),
                },
            );
        }
        manager.apply_fill_to_position(
            "BTCUSDT",
            MarketType::Spot,
            TradeSide::Buy,
            1.0,
            50_000.0,
            false,
        );
        manager.apply_fill_to_position(
            "BTCUSDT",
            MarketType::Perp,
            TradeSide::Sell,
            1.0,
            50_010.0,
            false,
        );
        assert_eq!(
            manager.apply_commission_once(CommissionObservation {
                symbol: "BTCUSDT",
                client_order_id: "spot-cid",
                market: MarketType::Spot,
                amount: 0.001,
                asset: "BTC",
                order_id: Some(1001),
                trade_id: Some(2002),
            }),
            Ok(())
        );

        assert!(manager.emit_cycle_order_update(
            &chase,
            "FILLED",
            "spot-cid",
            1.0,
            true,
            "FILLED_CYCLE",
        ));

        let mut terminal = None;
        for _ in 0..8 {
            let bytes = timeout(Duration::from_millis(250), dashboard.recv())
                .await
                .expect("terminal telemetry")
                .expect("broadcast remains open");
            let value: Value = rmp_serde::from_slice(&bytes).unwrap();
            if value
                .get("terminal_summary_version")
                .and_then(Value::as_u64)
                == Some(crate::ipc::EXECUTION_PROTOCOL_VERSION as u64)
            {
                terminal = Some(value);
                break;
            }
        }
        let terminal = terminal.expect("rich terminal summary was published");
        assert_eq!(
            terminal
                .get("requested_quantity_decimal")
                .and_then(Value::as_str),
            Some("1.001")
        );
        assert_eq!(
            terminal
                .get("spot_cumulative_filled_quantity_decimal")
                .and_then(Value::as_str),
            Some("1")
        );
        assert_eq!(
            terminal
                .get("actual_spot_inventory_decimal")
                .and_then(Value::as_str),
            Some("0.999")
        );
        assert_eq!(
            terminal
                .get("actual_futures_inventory_decimal")
                .and_then(Value::as_str),
            Some("1")
        );
        assert_eq!(
            terminal
                .get("commissions")
                .and_then(Value::as_array)
                .and_then(|rows| rows.first())
                .and_then(|row| row.get("amount"))
                .and_then(Value::as_str),
            Some("0.001")
        );

        let mut restarted = paper_test_manager();
        restarted
            .load_execution_state_from_path(journal_path.clone())
            .unwrap();
        assert_eq!(
            restarted.chase_states.get("BTCUSDT").map(|item| item.phase),
            Some(ChasePhase::Completed)
        );
        // Model a crash after chase removal but before the asynchronous relay
        // ever committed its broadcast. The complete payload must still replay.
        let publication_id = terminal["publication_id"].as_str().unwrap().to_string();
        assert_eq!(
            restarted.terminal_publications.get(&publication_id),
            Some(&terminal)
        );
        restarted.remove_chase_state("BTCUSDT", "test terminal removal before relay fsync");
        drop(restarted);
        let mut recovered = paper_test_manager();
        recovered
            .load_execution_state_from_path(journal_path.clone())
            .unwrap();
        assert!(recovered.chase_states.is_empty());
        let mut replay_receiver = recovered.dash_tx.subscribe();
        recovered.replay_terminal_publications();
        let replay: Value = rmp_serde::from_slice(&replay_receiver.recv().await.unwrap()).unwrap();
        assert_eq!(replay, terminal);
        // Only an internal fsync handoff can retire the persisted obligation.
        recovered.complete_terminal_publication(&publication_id);
        let mut after_handoff = paper_test_manager();
        after_handoff
            .load_execution_state_from_path(journal_path.clone())
            .unwrap();
        assert!(after_handoff.terminal_publications.is_empty());
        let _ = std::fs::remove_file(journal_path);
    }

    #[test]
    fn quantity_boundaries_use_the_canonical_decimal_value_of_each_float() {
        let mut manager = paper_test_manager();
        let mut filters = test_exchange_symbol("BTCUSDT");
        filters.futures_market_min_qty = decimal("0.01");
        filters.futures_market_step_size = decimal("0.01");
        filters.futures_market_max_qty = decimal("0.02");
        manager.exchange_info.insert("BTCUSDT".to_string(), filters);

        let immediately_below_minimum = f64::from_bits(0.01_f64.to_bits() - 1);
        let immediately_above_maximum = f64::from_bits(0.02_f64.to_bits() + 1);
        assert_eq!(
            manager.format_quantity_for_market(
                "BTCUSDT",
                MarketType::Perp,
                immediately_below_minimum,
                true,
            ),
            None
        );
        // A request just above the maximum is safely floored onto the maximum
        // market lot rather than compared using a fuzzy epsilon.
        assert_eq!(
            manager.format_quantity_for_market(
                "BTCUSDT",
                MarketType::Perp,
                immediately_above_maximum,
                true,
            ),
            Some("0.02".to_string())
        );
        assert_eq!(
            manager.format_quantity_for_market("BTCUSDT", MarketType::Perp, 0.03, true),
            None
        );
    }

    #[test]
    fn market_notional_boundaries_honor_exact_values_and_applicability_flags() {
        let mut manager = paper_test_manager();
        let mut filters = test_exchange_symbol("BTCUSDT");
        filters.futures_market_min_qty = decimal("0.1");
        filters.futures_market_step_size = decimal("0.1");
        filters.futures_min_notional = decimal("5");
        filters.futures_max_notional = Some(decimal("5"));
        filters.futures_min_notional_apply_to_market = true;
        filters.futures_max_notional_apply_to_market = true;
        manager
            .exchange_info
            .insert("BTCUSDT".to_string(), filters.clone());

        assert_eq!(
            manager.format_market_quantity_with_price("BTCUSDT", MarketType::Perp, 0.1, 50.0),
            Some("0.1".to_string())
        );
        assert_eq!(
            manager.format_market_quantity_with_price(
                "BTCUSDT",
                MarketType::Perp,
                0.1,
                f64::from_bits(50.0_f64.to_bits() - 1),
            ),
            None
        );
        assert_eq!(
            manager.format_market_quantity_with_price(
                "BTCUSDT",
                MarketType::Perp,
                0.1,
                f64::from_bits(50.0_f64.to_bits() + 1),
            ),
            None
        );

        filters.futures_min_notional_apply_to_market = false;
        filters.futures_max_notional_apply_to_market = false;
        manager.exchange_info.insert("BTCUSDT".to_string(), filters);
        assert_eq!(
            manager.format_market_quantity_with_price("BTCUSDT", MarketType::Perp, 0.1, 49.0),
            Some("0.1".to_string())
        );
        assert_eq!(
            manager.format_market_quantity_with_price("BTCUSDT", MarketType::Perp, 0.1, 51.0),
            Some("0.1".to_string())
        );
    }

    fn config_sync_instruction(
        intent_id: &str,
        sequence: u64,
        pause_new_entries: bool,
        per_symbol_cap: u64,
        max_gross: u64,
    ) -> crate::ipc::AlphaInstruction {
        let canonical_json = format!(
            "{{\"emergency_exit_max_retries\":2,\"emergency_exit_max_slippage_bps\":50.0,\"emergency_exit_readback_attempts\":3,\"max_gross_exposure_usd\":{max_gross},\"pause_new_entries\":{pause_new_entries},\"per_symbol_notional_cap_usd\":{per_symbol_cap}}}"
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

    fn storage_control_config_sync_instruction(
        intent_id: &str,
        sequence: u64,
        generation: u64,
        emergency_latched: bool,
        recovery_acknowledged: bool,
    ) -> crate::ipc::AlphaInstruction {
        let canonical_json = format!(
            "{{\"emergency_exit_max_retries\":2,\"emergency_exit_max_slippage_bps\":50.0,\"emergency_exit_readback_attempts\":3,\"max_gross_exposure_usd\":9000,\"pause_new_entries\":true,\"per_symbol_notional_cap_usd\":2000,\"storage_control_generation\":{generation},\"storage_emergency_latched\":{emergency_latched},\"storage_recovery_acknowledged\":{recovery_acknowledged}}}"
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

    fn unique_test_path(label: &str, extension: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "bongus-{label}-{}-{}.{}",
            std::process::id(),
            rand::random::<u64>(),
            extension
        ))
    }

    #[test]
    fn recovery_barrier_is_durable_and_success_restores_prior_entry_state() {
        let execution_path = unique_test_path("recovery-barrier-success", "jsonl");
        let intent_path = unique_test_path("recovery-barrier-intents", "jsonl");
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = execution_path.clone();
        manager.intent_journal = Some(IntentJournal::load(&intent_path).unwrap());
        let checkpoint = ContinuousRiskCheckpoint {
            state: manager.continuous_risk_state,
            reason: manager.continuous_risk_reason.clone(),
        };
        let snapshot = manager
            .prepare_recovery_barrier("request-success")
            .expect("enter durable recovery barrier");
        assert_eq!(snapshot.barrier_request_id, "request-success");
        assert_eq!(
            manager.continuous_risk_state,
            ContinuousRiskState::EntryFrozen
        );
        assert_eq!(
            manager.continuous_risk_reason,
            "recovery_generation_barrier_active:request-success"
        );
        let latest: ExecutionStateSnapshot = std::fs::read_to_string(&execution_path)
            .unwrap()
            .lines()
            .last()
            .map(serde_json::from_str)
            .unwrap()
            .unwrap();
        assert_eq!(
            latest.continuous_risk_reason,
            "recovery_generation_barrier_active:request-success"
        );

        manager
            .restore_after_recovery_barrier(checkpoint, "request-success", "5000-aaaaaaaaaaaaaaaa")
            .expect("durably release recovery barrier");
        assert_eq!(manager.continuous_risk_state, ContinuousRiskState::Normal);
        assert_eq!(manager.continuous_risk_reason, "startup");
        std::fs::remove_file(execution_path).ok();
        std::fs::remove_file(intent_path).ok();
    }

    #[test]
    fn restart_during_recovery_barrier_latches_manual_review_durably() {
        let execution_path = unique_test_path("recovery-barrier-restart", "jsonl");
        let intent_path = unique_test_path("recovery-restart-intents", "jsonl");
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = execution_path.clone();
        manager.intent_journal = Some(IntentJournal::load(&intent_path).unwrap());
        manager
            .prepare_recovery_barrier("request-interrupted")
            .expect("enter recovery barrier before crash");
        drop(manager);

        let mut recovered = paper_test_manager();
        recovered
            .load_execution_state_from_path(execution_path.clone())
            .expect("load active recovery barrier after restart");
        assert!(recovered.recover_interrupted_recovery_barrier());
        assert_eq!(recovered.state, SystemState::Reconciling);
        assert_eq!(
            recovered.continuous_risk_state,
            ContinuousRiskState::ManualReview
        );
        assert!(
            recovered
                .continuous_risk_reason
                .starts_with("recovery_generation_barrier_failed:request-interrupted:")
        );
        let latest: ExecutionStateSnapshot = std::fs::read_to_string(&execution_path)
            .unwrap()
            .lines()
            .last()
            .map(serde_json::from_str)
            .unwrap()
            .unwrap();
        assert_eq!(
            latest.continuous_risk_state,
            ContinuousRiskState::ManualReview
        );
        std::fs::remove_file(execution_path).ok();
        std::fs::remove_file(intent_path).ok();
    }

    #[tokio::test]
    async fn ambiguous_recovery_resume_ack_latches_manual_review() {
        let execution_path = unique_test_path("recovery-resume-ambiguity", "jsonl");
        let intent_path = unique_test_path("recovery-resume-intents", "jsonl");
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = execution_path.clone();
        manager.intent_journal = Some(IntentJournal::load(&intent_path).unwrap());
        let (reply_tx, reply_rx) = oneshot::channel();
        let (release_tx, release_rx) = oneshot::channel();
        let (resumed_tx, resumed_rx) = oneshot::channel();
        drop(resumed_rx);
        let actor = tokio::spawn(async move {
            manager
                .handle_recovery_barrier_event(
                    "request-ambiguous-resume".to_string(),
                    reply_tx,
                    release_rx,
                    resumed_tx,
                )
                .await;
            manager
        });
        reply_rx
            .await
            .expect("barrier reply")
            .expect("barrier prepared");
        release_tx
            .send(RecoveryBarrierRelease::Published {
                generation_id: "6000-bbbbbbbbbbbbbbbb".to_string(),
            })
            .unwrap();
        let manager = actor.await.unwrap();
        assert_eq!(
            manager.continuous_risk_state,
            ContinuousRiskState::ManualReview
        );
        assert!(
            manager
                .continuous_risk_reason
                .contains("did not receive the order-actor resume acknowledgement")
        );
        let latest: ExecutionStateSnapshot = std::fs::read_to_string(&execution_path)
            .unwrap()
            .lines()
            .last()
            .map(serde_json::from_str)
            .unwrap()
            .unwrap();
        assert_eq!(
            latest.continuous_risk_state,
            ContinuousRiskState::ManualReview
        );
        std::fs::remove_file(execution_path).ok();
        std::fs::remove_file(intent_path).ok();
    }

    fn next_config_ack(
        receiver: &mut tokio::sync::broadcast::Receiver<Vec<u8>>,
    ) -> serde_json::Value {
        for _ in 0..32 {
            let payload = receiver.try_recv().expect("expected dashboard event");
            let event: serde_json::Value = rmp_serde::from_slice(&payload).unwrap();
            if event.get("event").and_then(Value::as_str) == Some("ConfigAck") {
                return event;
            }
        }
        panic!("matching ConfigAck was not emitted")
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

    #[test]
    fn config_sync_durably_latches_storage_emergency_and_restart_recovers_it() {
        let checkpoint = unique_test_path("storage-latch", "json");
        let mut manager = paper_test_manager();
        manager.storage_control_path = checkpoint.clone();
        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "storage-emergency",
            1,
            7,
            true,
            false,
        ));

        assert!(manager.storage_emergency_latched);
        assert_eq!(manager.storage_control_generation, 7);
        assert!(checkpoint.is_file());

        let mut restarted = paper_test_manager();
        restarted.storage_control_path = checkpoint;
        restarted.storage_control_generation = 0;
        restarted.storage_emergency_latched = false;
        restarted.storage_control_error = None;
        restarted.load_storage_control().unwrap();
        assert!(restarted.storage_emergency_latched);
        assert_eq!(restarted.storage_control_generation, 7);
    }

    #[tokio::test]
    async fn checkpoint_failure_emits_volatile_fifo_barrier_and_blocks_all_entry_progression() {
        let checkpoint = unique_test_path("storage-volatile", "json");
        let mut manager = paper_test_manager();
        manager.trading_mode = "live".to_string();
        manager.state = SystemState::Trading;
        manager.storage_control_path = checkpoint.clone();
        manager.storage_control_persist_failure = Some("ENOSPC at checkpoint fsync".to_string());
        let mut dashboard = manager.dash_tx.subscribe();

        let mut idle = dual_test_chase(1.0);
        idle.phase = ChasePhase::Idle;
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), idle.clone());
        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "volatile-storage-emergency",
            1,
            40,
            true,
            false,
        ));

        let volatile_ack = next_config_ack(&mut dashboard);
        assert_eq!(
            volatile_ack.get("config_status").and_then(Value::as_str),
            Some("VOLATILE_LATCHED")
        );
        assert_eq!(
            volatile_ack.get("ack_status").and_then(Value::as_str),
            Some("TERMINAL")
        );
        assert!(
            volatile_ack
                .get("reason")
                .and_then(Value::as_str)
                .is_some_and(|reason| reason.contains("not_durable"))
        );
        assert!(manager.storage_emergency_latched);
        assert!(manager.storage_control_volatile_latched);
        assert!(manager.storage_control_error.is_some());
        assert_eq!(manager.storage_control_generation, 40);
        assert_eq!(manager.state, SystemState::Reconciling);
        assert!(
            !manager.chase_states.contains_key("BTCUSDT"),
            "an unsubmitted entry chase must be removed at the FIFO barrier"
        );
        assert!(!checkpoint.exists());

        // Simulate a stale actor callback trying to place a chase after the
        // storage-control event was processed. The independent latch must win
        // before any book, metadata, or REST path is consulted.
        manager.chase_states.insert("BTCUSDT".to_string(), idle);
        manager.try_place_dual_maker("BTCUSDT".to_string()).await;
        assert!(manager.internal_orders.is_empty());
        assert!(
            !manager.chase_states.contains_key("BTCUSDT"),
            "the continuous risk actor must remove a stale unsubmitted entry callback"
        );

        manager
            .handle_alpha_instruction(
                crate::ipc::AlphaInstruction {
                    symbol: Some("ETHUSDT".to_string()),
                    intent: "ENTER_LONG".to_string(),
                    quantity: 1.0,
                    urgency: 0.5,
                    max_slippage_bps: 5.0,
                    exposure_scale: 1.0,
                    intent_id: Some("entry-after-volatile-latch".to_string()),
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;
        assert!(!manager.chase_states.contains_key("ETHUSDT"));

        // A recovery clear cannot be acknowledged while its own checkpoint
        // remains non-durable.
        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "recovery-still-enospc",
            2,
            41,
            false,
            true,
        ));
        let rejected_clear = next_config_ack(&mut dashboard);
        assert_eq!(
            rejected_clear.get("config_status").and_then(Value::as_str),
            Some("REJECTED")
        );
        assert!(manager.storage_emergency_latched);
        assert!(manager.storage_control_error.is_some());

        manager.storage_control_persist_failure = None;
        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "durable-recovery",
            3,
            42,
            false,
            true,
        ));
        let applied_clear = next_config_ack(&mut dashboard);
        assert_eq!(
            applied_clear.get("config_status").and_then(Value::as_str),
            Some("APPLIED")
        );
        assert!(!manager.storage_emergency_latched);
        assert!(!manager.storage_control_volatile_latched);
        assert!(manager.storage_control_error.is_none());
        assert!(checkpoint.is_file());

        let _ = std::fs::remove_file(checkpoint);
    }

    #[test]
    fn unavailable_intent_journal_still_emits_volatile_emergency_fifo_barrier() {
        let checkpoint = unique_test_path("storage-no-intent-journal", "json");
        let mut manager = paper_test_manager();
        manager.trading_mode = "live".to_string();
        manager.state = SystemState::Trading;
        manager.storage_control_path = checkpoint.clone();
        manager.intent_journal = None;
        manager.intent_journal_error = Some("injected ENOSPC".to_string());
        let mut dashboard = manager.dash_tx.subscribe();

        let mut idle = dual_test_chase(1.0);
        idle.phase = ChasePhase::Idle;
        manager.chase_states.insert("BTCUSDT".to_string(), idle);
        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "volatile-without-intent-journal",
            1,
            50,
            true,
            false,
        ));

        let volatile_ack = next_config_ack(&mut dashboard);
        assert_eq!(
            volatile_ack.get("config_status").and_then(Value::as_str),
            Some("VOLATILE_LATCHED")
        );
        assert_eq!(
            volatile_ack
                .get("applied_config_hash")
                .and_then(Value::as_str),
            volatile_ack
                .get("declared_config_hash")
                .and_then(Value::as_str)
        );
        assert_eq!(
            volatile_ack.get("reason").and_then(Value::as_str),
            Some("intent_journal_unavailable")
        );
        assert!(manager.storage_emergency_latched);
        assert!(manager.storage_control_volatile_latched);
        assert!(manager.storage_control_error.is_some());
        assert_eq!(manager.storage_control_generation, 50);
        assert_eq!(manager.state, SystemState::Reconciling);
        assert!(!manager.chase_states.contains_key("BTCUSDT"));

        let _ = std::fs::remove_file(checkpoint);
    }

    #[test]
    fn missing_or_stale_storage_control_cannot_clear_latch() {
        let mut manager = paper_test_manager();
        manager.storage_control_path = unique_test_path("storage-stale", "json");
        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "storage-emergency",
            1,
            10,
            true,
            false,
        ));
        assert!(manager.storage_emergency_latched);

        // Legacy/ordinary config snapshots remain accepted, but absence of a
        // control transition is explicitly a no-op for the durable latch.
        let missing = config_sync_instruction("ordinary-config", 2, false, 2_000, 9_000);
        let missing_hash = missing.config_version_hash.clone().unwrap();
        manager.handle_config_sync_instruction(missing);
        assert!(manager.storage_emergency_latched);
        assert_eq!(
            manager.config_consensus.applied_hash(),
            Some(missing_hash.as_str())
        );

        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "stale-clear",
            3,
            9,
            false,
            true,
        ));
        assert!(manager.storage_emergency_latched);
        assert_eq!(manager.storage_control_generation, 10);
        assert_eq!(
            manager.config_consensus.applied_hash(),
            Some(missing_hash.as_str())
        );
    }

    #[test]
    fn newer_operator_acknowledged_storage_recovery_clears_latch() {
        let mut manager = paper_test_manager();
        manager.storage_control_path = unique_test_path("storage-clear", "json");
        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "storage-emergency",
            1,
            20,
            true,
            false,
        ));
        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "storage-recovery",
            2,
            21,
            false,
            true,
        ));

        assert!(!manager.storage_emergency_latched);
        assert_eq!(manager.storage_control_generation, 21);
        let persisted: StorageControlRecord =
            serde_json::from_slice(&std::fs::read(&manager.storage_control_path).unwrap()).unwrap();
        persisted.validate().unwrap();
        assert!(!persisted.emergency_latched);

        let ordinary =
            storage_control_config_sync_instruction("ordinary-after-recovery", 3, 0, false, false);
        let ordinary_hash = ordinary.config_version_hash.clone().unwrap();
        manager.handle_config_sync_instruction(ordinary);
        assert_eq!(
            manager.config_consensus.applied_hash(),
            Some(ordinary_hash.as_str())
        );
        assert_eq!(manager.storage_control_generation, 21);
        assert!(!manager.storage_emergency_latched);
    }

    #[tokio::test]
    async fn storage_latch_rejects_entry_but_preserves_reduce_only_exit() {
        let mut manager = paper_test_manager();
        manager.storage_control_path = unique_test_path("storage-entry-gate", "json");
        manager.handle_config_sync_instruction(storage_control_config_sync_instruction(
            "storage-emergency",
            1,
            30,
            true,
            false,
        ));
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
                    intent_id: Some("blocked-storage-entry".to_string()),
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;
        assert!(!manager.chase_states.contains_key("BTCUSDT"));
        let mut rejection_reason = None;
        for _ in 0..8 {
            let payload = timeout(Duration::from_secs(1), dashboard.recv())
                .await
                .expect("entry rejection event")
                .expect("dashboard broadcast");
            let event: serde_json::Value = rmp_serde::from_slice(&payload).unwrap();
            if event.get("event").and_then(Value::as_str) == Some("OrderRejected") {
                rejection_reason = event
                    .get("reason")
                    .and_then(Value::as_str)
                    .map(str::to_string);
                break;
            }
        }
        assert_eq!(
            rejection_reason.as_deref(),
            Some("storage_emergency_latched")
        );

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
                    intent_id: Some("allowed-storage-exit".to_string()),
                    direction: Some("long".to_string()),
                    skip_spot_leg: true,
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;
        assert!(
            manager
                .chase_states
                .get("BTCUSDT")
                .is_some_and(|chase| chase.is_exit),
            "storage emergency must not block a verified reduce-only exit"
        );
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

    #[tokio::test]
    async fn same_symbol_entry_reservation_includes_existing_leg_exposure() {
        let mut manager = paper_test_manager();
        manager.trading_mode = "live".to_string();
        manager.handle_config_sync_instruction(config_sync_instruction(
            "config-same-symbol-cap",
            1,
            false,
            2_500,
            10_000,
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
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: Some(TrackedLegPosition {
                    side: "LONG".to_string(),
                    entry_price: 100.0,
                    quantity: 20.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 100.0,
                    quantity: 20.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
            },
        );
        manager.recompute_gross_exposure();
        let mut candidate = dual_test_chase(6.0);
        candidate.phase = ChasePhase::Idle;
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), candidate);

        manager.try_place_dual_maker("BTCUSDT".to_string()).await;

        assert!(
            !manager.chase_states.contains_key("BTCUSDT"),
            "the candidate must include the existing $2,000 per-leg exposure and breach the $2,500 cap"
        );
        assert!(manager.internal_orders.is_empty());
    }

    #[test]
    fn execution_state_entry_gate_accounts_for_next_fsync_and_transition_reserve() {
        let manager = paper_test_manager();
        let next_snapshot_bytes = serde_json::to_vec(&manager.execution_snapshot())
            .unwrap()
            .len() as u64
            + 1;
        let exact_required = next_snapshot_bytes + EXECUTION_STATE_TRANSITION_RESERVE_BYTES;
        assert!(
            manager
                .execution_state_storage_allows_new_risk_at_limit(exact_required)
                .is_ok()
        );
        let error = manager
            .execution_state_storage_allows_new_risk_at_limit(exact_required - 1)
            .unwrap_err();
        assert!(error.contains("projected="));
        assert!(error.contains("transition_reserve="));
    }

    #[test]
    fn execution_state_journal_compacts_without_losing_terminal_lineage() {
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = unique_test_path("execution-compact", "jsonl");
        let mut completed = dual_test_chase(1.0);
        completed.phase = ChasePhase::Completed;
        completed.spot_client_order_id = "resolved-order".to_string();
        completed.spot_order_aliases = vec!["resolved-order".to_string()];
        completed.futures_client_order_id = "resolved-futures-order".to_string();
        completed.futures_order_aliases = vec!["resolved-futures-order".to_string()];
        manager.internal_orders.insert(
            "resolved-order".to_string(),
            InternalOrder {
                client_order_id: "resolved-order".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "FILLED".to_string(),
                limit_price: Some(100.0),
            },
        );
        manager
            .order_cumulative_fills
            .insert("resolved-order".to_string(), 1.0);
        manager
            .order_lineage
            .insert("resolved-order".to_string(), OrderLineage::default());
        manager.ensure_terminal_tombstone(
            &completed,
            "TERMINAL_RECONCILED",
            "TERMINAL_EVENT_PERSISTED",
            "test terminal lifecycle",
        );
        manager.prune_resolved_execution_artifacts();
        assert!(manager.internal_orders.contains_key("resolved-order"));
        assert!(
            manager
                .order_cumulative_fills
                .contains_key("resolved-order")
        );
        assert!(manager.order_lineage.contains_key("resolved-order"));
        manager.append_execution_snapshot().unwrap();

        let mut before_retention_expiry = paper_test_manager();
        before_retention_expiry
            .load_execution_state_from_path(manager.execution_state_journal_path.clone())
            .unwrap();
        assert!(
            before_retention_expiry
                .internal_orders
                .contains_key("resolved-order")
        );
        let tombstone = before_retention_expiry
            .terminal_tombstones
            .get("resolved-order")
            .expect("terminal tombstone survived restart");
        assert_eq!(tombstone.reconciliation_status, "TERMINAL_EVENT_PERSISTED");
        assert!(tombstone.terminal_sequence_watermark > 0);
        assert!(tombstone.retention_deadline_ms > tombstone.tombstoned_at_ms);

        let now_ms = OrderManager::current_time_ms();
        let expiring = manager
            .terminal_tombstones
            .get_mut("resolved-order")
            .unwrap();
        expiring.tombstoned_at_ms = now_ms - TERMINAL_TOMBSTONE_RETENTION_MS - 1;
        expiring.retention_deadline_ms = now_ms - 1;
        manager.prune_resolved_execution_artifacts();
        assert!(!manager.internal_orders.contains_key("resolved-order"));
        assert!(
            !manager
                .order_cumulative_fills
                .contains_key("resolved-order")
        );
        assert!(!manager.order_lineage.contains_key("resolved-order"));
        let retained = manager
            .terminal_tombstones
            .get("resolved-order")
            .expect("expired tombstone retains embedded evidence");
        assert_eq!(retained.lifecycle_state, "RETAINED_PRUNED");
        assert_eq!(retained.reconciliation_status, "RETENTION_EXPIRED");
        assert!(retained.internal_orders.contains_key("resolved-order"));
        assert_eq!(
            retained.order_cumulative_fills.get("resolved-order"),
            Some(&1.0)
        );
        assert!(retained.order_lineage.contains_key("resolved-order"));

        let max_bytes = 160_000;
        for _ in 0..600 {
            manager
                .append_execution_snapshot_at_limit(max_bytes)
                .unwrap();
        }
        let encoded = std::fs::read_to_string(&manager.execution_state_journal_path).unwrap();
        assert!(
            encoded.lines().count() < 600,
            "history should have compacted"
        );
        assert!(
            manager
                .execution_state_journal_path
                .metadata()
                .unwrap()
                .len()
                <= max_bytes
        );

        let mut restarted = paper_test_manager();
        restarted
            .load_execution_state_from_path(manager.execution_state_journal_path.clone())
            .unwrap();
        let restarted_tombstone = restarted
            .terminal_tombstones
            .get("resolved-order")
            .expect("compaction retained terminal tombstone");
        assert_eq!(restarted_tombstone.lifecycle_state, "RETAINED_PRUNED");
        assert!(
            restarted_tombstone
                .order_lineage
                .contains_key("resolved-order")
        );
    }

    #[test]
    fn failed_chase_removal_restores_state_and_latches_only_its_symbol() {
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = unique_test_path("remove-fsync-failure", "jsonl");
        let chase = dual_test_chase(1.0);
        let cycle_id = chase.cycle_client_order_id().to_string();
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), chase.clone());
        manager
            .chase_unhedged_budgets
            .insert("BTCUSDT".to_string(), 0.25);
        manager
            .chase_unhedged_started_at_ms
            .insert("BTCUSDT".to_string(), 42);
        manager.cycle_deadlines.insert(cycle_id.clone(), 84);
        assert!(manager.persist_execution_state_for_symbol("BTCUSDT", "test baseline"));

        manager.execution_state_persist_failure = Some("injected fsync failure".to_string());
        assert!(
            manager
                .remove_chase_state("BTCUSDT", "test checked removal")
                .is_none(),
            "a failed removal fsync must not report success"
        );
        assert_eq!(manager.state, SystemState::Trading);
        assert!(manager.chase_states.contains_key("BTCUSDT"));
        assert_eq!(manager.chase_unhedged_budgets.get("BTCUSDT"), Some(&0.25));
        assert_eq!(
            manager.chase_unhedged_started_at_ms.get("BTCUSDT"),
            Some(&42)
        );
        assert_eq!(manager.cycle_deadlines.get(&cycle_id), Some(&84));
        assert!(manager.is_symbol_persistence_latched("BTCUSDT"));
        assert!(!manager.is_symbol_persistence_latched("ETHUSDT"));
        assert!(manager.terminal_tombstones.contains_key(&cycle_id));

        manager.execution_state_persist_failure = None;
        assert!(
            manager.persist_execution_state_for_symbol(
                "BTCUSDT",
                "durably retain failed-removal latch"
            )
        );
        let mut restarted = paper_test_manager();
        restarted
            .load_execution_state_from_path(manager.execution_state_journal_path.clone())
            .unwrap();
        assert!(restarted.chase_states.contains_key("BTCUSDT"));
        assert!(restarted.is_symbol_persistence_latched("BTCUSDT"));
        assert!(restarted.terminal_tombstones.contains_key(&cycle_id));
    }

    #[tokio::test]
    async fn symbol_persistence_latch_blocks_its_entries_but_preserves_reduce_only_exit() {
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = unique_test_path("symbol-latch-gate", "jsonl");
        manager.latch_symbol_persistence_failure(
            "BTCUSDT",
            "test exposure mutation",
            "injected fsync failure",
        );
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
                    intent_id: Some("blocked-symbol-latch-entry".to_string()),
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;
        assert!(!manager.chase_states.contains_key("BTCUSDT"));
        let mut rejection_reason = None;
        for _ in 0..8 {
            let payload = timeout(Duration::from_secs(1), dashboard.recv())
                .await
                .expect("entry rejection event")
                .expect("dashboard broadcast");
            let event: serde_json::Value = rmp_serde::from_slice(&payload).unwrap();
            if event.get("event").and_then(Value::as_str) == Some("OrderRejected") {
                rejection_reason = event
                    .get("reason")
                    .and_then(Value::as_str)
                    .map(str::to_string);
                break;
            }
        }
        assert_eq!(
            rejection_reason.as_deref(),
            Some("symbol_persistence_latched")
        );

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
                    intent_id: Some("allowed-symbol-latch-exit".to_string()),
                    direction: Some("long".to_string()),
                    skip_spot_leg: true,
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;
        assert!(
            manager
                .chase_states
                .get("BTCUSDT")
                .is_some_and(|chase| chase.is_exit),
            "a symbol persistence latch is entry-only and must preserve verified exits"
        );
    }

    #[tokio::test]
    async fn late_private_fill_recovers_retained_terminal_lineage_after_exchange_flat() {
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = unique_test_path("late-terminal-fill", "jsonl");
        let mut chase = dual_test_chase(1.0);
        chase.spot_client_order_id = "bngs_late_spot".to_string();
        chase.spot_order_aliases = vec![chase.spot_client_order_id.clone()];
        chase.futures_client_order_id = "bngs_late_futures".to_string();
        chase.futures_order_aliases = vec![chase.futures_client_order_id.clone()];
        manager.internal_orders.insert(
            chase.spot_client_order_id.clone(),
            InternalOrder {
                client_order_id: chase.spot_client_order_id.clone(),
                symbol: chase.symbol.clone(),
                status: "CANCELED".to_string(),
                limit_price: Some(100.0),
            },
        );
        manager.order_lineage.insert(
            chase.spot_client_order_id.clone(),
            OrderLineage {
                cycle_id: Some("late-cycle".to_string()),
                market: Some(MarketType::Spot),
                ..OrderLineage::default()
            },
        );
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), chase.clone());
        assert!(
            manager
                .remove_chase_state("BTCUSDT", "exchange reported flat before late fill")
                .is_some()
        );
        assert!(!manager.chase_states.contains_key("BTCUSDT"));

        manager
            .handle_ws_event(test_order_update(
                "bngs_late_spot",
                "PARTIALLY_FILLED",
                0.4,
                0.4,
                MarketType::Spot,
                100.0,
            ))
            .await;

        let recovered = manager
            .chase_states
            .get("BTCUSDT")
            .expect("late fill restored its retained chase lineage");
        assert_eq!(recovered.phase, ChasePhase::ReconciliationRequired);
        assert!((recovered.spot_cumulative_filled - 0.4).abs() < 1e-12);
        assert_eq!(
            manager.order_cumulative_fills.get("bngs_late_spot"),
            Some(&0.4)
        );
        let position = manager
            .tracked_positions
            .get("BTCUSDT")
            .and_then(|position| position.spot.as_ref())
            .expect("late fill exposure was reconstructed");
        assert!((position.quantity - 0.4).abs() < 1e-12);
        let tombstone = manager
            .terminal_tombstones
            .get("bngs_late_spot")
            .expect("late fill kept the terminal tombstone");
        assert_eq!(tombstone.reconciliation_status, "LATE_FILL_REPAIR_REQUIRED");
        assert_eq!(
            tombstone.order_cumulative_fills.get("bngs_late_spot"),
            Some(&0.4)
        );

        let mut restarted = paper_test_manager();
        restarted
            .load_execution_state_from_path(manager.execution_state_journal_path.clone())
            .unwrap();
        assert_eq!(
            restarted
                .chase_states
                .get("BTCUSDT")
                .map(|state| state.phase),
            Some(ChasePhase::ReconciliationRequired)
        );
        assert_eq!(
            restarted
                .terminal_tombstones
                .get("bngs_late_spot")
                .map(|value| value.reconciliation_status.as_str()),
            Some("LATE_FILL_REPAIR_REQUIRED")
        );
    }

    fn dual_test_chase(quantity: f64) -> ChaseState {
        let exact_quantity = ExactDecimal::from_f64(quantity)
            .expect("test chase quantity is finite")
            .to_string();
        ChaseState {
            symbol: "BTCUSDT".to_string(),
            quantity,
            spot_quantity: quantity,
            perp_quantity: quantity,
            spot_client_order_id: "spot-cid".to_string(),
            futures_client_order_id: "fut-cid".to_string(),
            spot_order_aliases: vec!["spot-cid".to_string()],
            futures_order_aliases: vec!["fut-cid".to_string()],
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
            requested_quantity_decimal: exact_quantity.clone(),
            normalized_common_entry_quantity_decimal: Some(exact_quantity),
            actual_spot_inventory_decimal: "0".to_string(),
            actual_futures_inventory_decimal: "0".to_string(),
            exit_spot_quantity_decimal: "0".to_string(),
            exit_futures_quantity_decimal: "0".to_string(),
            ..ChaseState::default()
        }
    }

    fn tracked_test_leg(side: &str, quantity: f64) -> TrackedLegPosition {
        TrackedLegPosition {
            side: side.to_string(),
            entry_price: 100.0,
            quantity,
            unrealized_pnl: 0.0,
            last_mark_price: 100.0,
        }
    }

    #[tokio::test]
    async fn continuous_risk_cancels_entries_preserves_exits_and_recovers_persisted_state() {
        let mut manager = paper_test_manager();
        let mut entry = dual_test_chase(1.0);
        entry.symbol = "BTCUSDT".to_string();
        let mut exit = dual_test_chase(1.0);
        exit.symbol = "ETHUSDT".to_string();
        exit.spot_client_order_id = "exit-spot-cid".to_string();
        exit.futures_client_order_id = "exit-fut-cid".to_string();
        exit.spot_order_aliases = vec![exit.spot_client_order_id.clone()];
        exit.futures_order_aliases = vec![exit.futures_client_order_id.clone()];
        exit.is_exit = true;
        exit.spot_side = TradeSide::Sell;
        exit.futures_side = TradeSide::Buy;
        manager
            .chase_states
            .insert(entry.symbol.clone(), entry.clone());
        manager
            .chase_states
            .insert(exit.symbol.clone(), exit.clone());
        for (client_id, symbol) in [
            (&entry.spot_client_order_id, &entry.symbol),
            (&entry.futures_client_order_id, &entry.symbol),
            (&exit.spot_client_order_id, &exit.symbol),
            (&exit.futures_client_order_id, &exit.symbol),
        ] {
            manager.internal_orders.insert(
                client_id.clone(),
                InternalOrder {
                    client_order_id: client_id.clone(),
                    symbol: symbol.clone(),
                    status: "NEW".to_string(),
                    limit_price: Some(100.0),
                },
            );
        }

        manager
            .activate_continuous_risk(
                "test_public_disconnect".to_string(),
                ContinuousRiskState::Reconciling,
            )
            .await;

        assert_eq!(
            manager.continuous_risk_state,
            ContinuousRiskState::Reconciling
        );
        assert_eq!(manager.state, SystemState::Reconciling);
        assert_eq!(
            manager.chase_states["BTCUSDT"].phase,
            ChasePhase::ReconciliationRequired
        );
        assert_eq!(manager.internal_orders["spot-cid"].status, "CANCELED");
        assert_eq!(manager.internal_orders["fut-cid"].status, "CANCELED");
        assert_eq!(
            manager.chase_states["ETHUSDT"].phase,
            ChasePhase::DualMakerPlaced,
            "exit chase must remain active"
        );
        assert_eq!(manager.internal_orders["exit-spot-cid"].status, "NEW");
        assert_eq!(manager.internal_orders["exit-fut-cid"].status, "NEW");
        assert!(manager.continuous_risk_sequence >= 3);

        let journal_path = manager.execution_state_journal_path.clone();
        let mut recovered = paper_test_manager();
        recovered
            .load_execution_state_from_path(journal_path)
            .expect("persisted risk state must recover");
        assert_eq!(
            recovered.continuous_risk_state,
            ContinuousRiskState::Reconciling
        );
        assert_eq!(
            recovered.chase_states["BTCUSDT"].phase,
            ChasePhase::ReconciliationRequired
        );
        assert!(recovered.chase_states["ETHUSDT"].is_exit);
    }

    #[tokio::test]
    async fn stale_python_brain_autonomously_flattens_once_and_restart_replay_is_idempotent() {
        let (mut manager, mut dashboard_rx) = paper_test_manager_with_dashboard();
        prepare_emergency_market(&mut manager, "BTCUSDT");
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: Some(tracked_test_leg("LONG", 1.0)),
                perp: Some(tracked_test_leg("SHORT", 1.0)),
            },
        );
        manager.brain_ping_age_override = Some(PYTHON_BRAIN_STALE_AFTER + Duration::from_secs(1));

        assert_eq!(
            manager.continuous_risk_assessment(),
            Some((
                "python_brain_stale".to_string(),
                ContinuousRiskState::Derisking
            ))
        );
        manager.reevaluate_continuous_risk("position_audit").await;

        assert_eq!(
            manager.continuous_risk_state,
            ContinuousRiskState::Derisking
        );
        assert_eq!(manager.emergency_exits.len(), 1);
        let (intent_id, record) = manager.emergency_exits.iter().next().unwrap();
        let intent_id = intent_id.clone();
        assert_eq!(record.state, EmergencyExitState::Flat);
        assert_eq!(
            record.autonomous_risk_sequence,
            Some(manager.continuous_risk_sequence)
        );
        assert_eq!(
            record.trigger_reason,
            "continuous risk autonomous emergency: python_brain_stale"
        );
        assert_eq!(record.direction, "EXIT_LONG");
        assert_eq!(record.spot_generations.len(), 1);
        assert_eq!(record.futures_generations.len(), 1);
        assert_eq!(record.derisk_attempts, 1);

        let mut saw_autonomous_state = false;
        let mut saw_intent_ack = false;
        loop {
            match dashboard_rx.try_recv() {
                Ok(payload) => {
                    let event: Value = rmp_serde::from_slice(&payload).unwrap();
                    saw_intent_ack |=
                        event.get("event").and_then(Value::as_str) == Some("IntentAck");
                    if event.get("event").and_then(Value::as_str) == Some("EmergencyExitState")
                        && event.get("intent_id").and_then(Value::as_str)
                            == Some(intent_id.as_str())
                    {
                        saw_autonomous_state = event
                            .get("autonomous")
                            .and_then(Value::as_bool)
                            .unwrap_or(false);
                    }
                }
                Err(broadcast::error::TryRecvError::Lagged(_)) => continue,
                Err(broadcast::error::TryRecvError::Empty)
                | Err(broadcast::error::TryRecvError::Closed) => break,
            }
        }
        assert!(saw_autonomous_state);
        assert!(
            !saw_intent_ack,
            "Rust-originated risk exits are state-machine records, not synthetic Alpha receipts"
        );

        manager
            .reevaluate_continuous_risk("position_audit_repeat")
            .await;
        assert_eq!(manager.emergency_exits.len(), 1);
        assert_eq!(manager.emergency_exits[&intent_id].derisk_attempts, 1);

        let journal_path = manager.execution_state_journal_path.clone();
        let expected_risk_sequence = manager.continuous_risk_sequence;
        let mut recovered = paper_test_manager();
        recovered
            .load_execution_state_from_path(journal_path)
            .expect("autonomous emergency evidence must recover");
        recovered.brain_ping_age_override = Some(PYTHON_BRAIN_STALE_AFTER + Duration::from_secs(1));
        recovered
            .reevaluate_continuous_risk("post_restart_position_audit")
            .await;

        assert_eq!(recovered.continuous_risk_sequence, expected_risk_sequence);
        assert_eq!(recovered.emergency_exits.len(), 1);
        assert_eq!(
            recovered.emergency_exits[&intent_id].state,
            EmergencyExitState::Flat
        );
        assert_eq!(recovered.emergency_exits[&intent_id].derisk_attempts, 1);
        assert_eq!(
            recovered.emergency_exits[&intent_id].spot_generations.len(),
            1
        );
        assert_eq!(
            recovered.emergency_exits[&intent_id]
                .futures_generations
                .len(),
            1
        );
    }

    #[tokio::test]
    async fn entry_quota_pressure_cannot_mask_stale_brain_autonomous_derisk() {
        let mut manager = paper_test_manager();
        prepare_emergency_market(&mut manager, "BTCUSDT");
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: Some(tracked_test_leg("LONG", 1.0)),
                perp: Some(tracked_test_leg("SHORT", 1.0)),
            },
        );
        manager.binance_rest = BinanceRest::new(
            "test-key".to_string(),
            "test-secret".to_string(),
            "testnet".to_string(),
        );
        let now_ms = OrderManager::current_time_ms();
        manager
            .binance_rest
            .set_rate_limit_observations_for_test(6_000, 100, 2_400, 100, now_ms);
        manager.binance_rest.set_order_count_observations_for_test(
            LegVenue::UsdtFutures,
            100,
            85,
            1_200,
            100,
            now_ms,
        );
        assert_eq!(
            manager.entry_quota_block_reason(),
            Some("insufficient_exchange_rate_limit_budget")
        );
        assert!(manager.critical_quota_guard().is_ok());
        manager.brain_ping_age_override = Some(PYTHON_BRAIN_STALE_AFTER + Duration::from_secs(1));

        assert_eq!(
            manager.continuous_risk_assessment(),
            Some((
                "python_brain_stale".to_string(),
                ContinuousRiskState::Derisking
            ))
        );
        manager
            .reevaluate_continuous_risk("quota_pressure_position_audit")
            .await;

        let record = manager.emergency_exits.values().next().unwrap();
        assert_eq!(record.state, EmergencyExitState::Flat);
        assert!(record.autonomous_risk_sequence.is_some());
    }

    #[tokio::test]
    async fn autonomous_derisk_attempts_every_symbol_before_global_manual_review() {
        let mut manager = paper_test_manager();
        // BTC sorts first but deliberately has no slippage reference. ETH is
        // fully executable and must still flatten after BTC fails closed.
        prepare_emergency_market(&mut manager, "ETHUSDT");
        for symbol in ["BTCUSDT", "ETHUSDT"] {
            manager.tracked_positions.insert(
                symbol.to_string(),
                TrackedPosition {
                    symbol: symbol.to_string(),
                    spot: Some(tracked_test_leg("LONG", 1.0)),
                    perp: Some(tracked_test_leg("SHORT", 1.0)),
                },
            );
        }

        manager
            .activate_continuous_risk(
                "test_portfolio_best_effort".to_string(),
                ContinuousRiskState::Derisking,
            )
            .await;

        assert_eq!(manager.emergency_exits.len(), 2);
        let btc = manager
            .emergency_exits
            .values()
            .find(|record| record.symbol == "BTCUSDT")
            .unwrap();
        let eth = manager
            .emergency_exits
            .values()
            .find(|record| record.symbol == "ETHUSDT")
            .unwrap();
        assert_eq!(btc.state, EmergencyExitState::ManualReview);
        assert!(btc.last_error.contains("slippage reference is unavailable"));
        assert_eq!(eth.state, EmergencyExitState::Flat);
        assert_eq!(eth.spot_generations.len(), 1);
        assert_eq!(eth.futures_generations.len(), 1);
        assert_eq!(
            manager.continuous_risk_state,
            ContinuousRiskState::ManualReview
        );
    }

    #[tokio::test]
    async fn derisking_without_attributable_inventory_requires_manual_review() {
        let mut manager = paper_test_manager();
        manager.current_gross_exposure_usd = manager.max_gross_exposure_usd + 1.0;
        assert_eq!(
            manager.continuous_risk_assessment().unwrap().1,
            ContinuousRiskState::Derisking
        );

        manager
            .reevaluate_continuous_risk("unattributed_gross_exposure")
            .await;

        assert_eq!(
            manager.continuous_risk_state,
            ContinuousRiskState::ManualReview
        );
        assert!(manager.emergency_exits.is_empty());
        assert!(
            manager
                .continuous_risk_reason
                .contains("DERISKING has no attributable positive tracked inventory")
        );
    }

    #[tokio::test]
    async fn autonomous_derisk_supports_each_safe_single_leg_topology() {
        for (symbol, spot, perp, expected_direction, expected_spot, expected_futures) in [
            (
                "BTCUSDT",
                Some(tracked_test_leg("LONG", 1.0)),
                None,
                "EXIT_LONG",
                1,
                0,
            ),
            (
                "ETHUSDT",
                None,
                Some(tracked_test_leg("SHORT", 1.0)),
                "EXIT_LONG",
                0,
                1,
            ),
            (
                "SOLUSDT",
                None,
                Some(tracked_test_leg("LONG", 1.0)),
                "EXIT_SHORT",
                0,
                1,
            ),
        ] {
            let mut manager = paper_test_manager();
            prepare_emergency_market(&mut manager, symbol);
            manager.tracked_positions.insert(
                symbol.to_string(),
                TrackedPosition {
                    symbol: symbol.to_string(),
                    spot,
                    perp,
                },
            );

            manager
                .activate_continuous_risk(
                    "test_safe_single_leg".to_string(),
                    ContinuousRiskState::Derisking,
                )
                .await;

            let record = manager.emergency_exits.values().next().unwrap();
            assert_eq!(record.state, EmergencyExitState::Flat);
            assert_eq!(record.direction, expected_direction);
            assert_eq!(record.spot_generations.len(), expected_spot);
            assert_eq!(record.futures_generations.len(), expected_futures);
        }
    }

    #[tokio::test]
    async fn autonomous_derisk_refuses_unproven_short_spot_liability() {
        let mut manager = paper_test_manager();
        prepare_emergency_market(&mut manager, "BTCUSDT");
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: Some(tracked_test_leg("SHORT", 1.0)),
                perp: Some(tracked_test_leg("LONG", 1.0)),
            },
        );

        manager
            .activate_continuous_risk(
                "test_inverse_topology".to_string(),
                ContinuousRiskState::Derisking,
            )
            .await;

        assert_eq!(
            manager.continuous_risk_state,
            ContinuousRiskState::ManualReview
        );
        assert!(manager.emergency_exits.is_empty());
        assert!(manager.internal_orders.is_empty());
        assert!(
            manager
                .continuous_risk_reason
                .contains("short-spot liability cannot be autonomously repaid")
        );
    }

    #[tokio::test]
    async fn reconciliation_proof_clears_nonterminal_risk_but_not_derisking() {
        let mut recoverable = paper_test_manager();
        assert!(
            recoverable.advance_continuous_risk(ContinuousRiskState::Reconciling, "test_reconcile")
        );
        recoverable.execute_reconciliation_sequence().await;
        assert_eq!(recoverable.state, SystemState::Trading);
        assert_eq!(
            recoverable.continuous_risk_state,
            ContinuousRiskState::Normal
        );

        let mut derisking = paper_test_manager();
        assert!(
            derisking.advance_continuous_risk(ContinuousRiskState::Derisking, "test_derisking")
        );
        derisking.execute_reconciliation_sequence().await;
        assert_eq!(derisking.state, SystemState::Reconciling);
        assert_eq!(
            derisking.continuous_risk_state,
            ContinuousRiskState::Derisking
        );
    }

    fn test_exchange_symbol(symbol: &str) -> crate::binance_rest::ExchangeSymbolInfo {
        crate::binance_rest::ExchangeSymbolInfo {
            symbol: symbol.to_string(),
            spot_tick_size: decimal("0.01"),
            spot_min_price: decimal("0.01"),
            spot_max_price: decimal("1000000"),
            spot_min_qty: decimal("0.001"),
            spot_step_size: decimal("0.001"),
            spot_max_qty: decimal("100"),
            spot_market_min_qty: decimal("0.001"),
            spot_market_step_size: decimal("0.001"),
            spot_market_max_qty: decimal("100"),
            spot_min_notional: decimal("5"),
            spot_max_notional: None,
            spot_min_notional_apply_to_market: true,
            spot_max_notional_apply_to_market: false,
            futures_tick_size: decimal("0.01"),
            futures_min_price: decimal("0.01"),
            futures_max_price: decimal("1000000"),
            futures_min_qty: decimal("0.001"),
            futures_step_size: decimal("0.001"),
            futures_max_qty: decimal("100"),
            futures_market_min_qty: decimal("0.001"),
            futures_market_step_size: decimal("0.001"),
            futures_market_max_qty: decimal("100"),
            futures_min_notional: decimal("5"),
            futures_max_notional: None,
            futures_min_notional_apply_to_market: true,
            futures_max_notional_apply_to_market: false,
        }
    }

    #[test]
    fn active_cycle_filter_or_status_change_preserves_chase_and_revokes_readiness() {
        let baseline = test_exchange_symbol("BTCUSDT");
        let eth = test_exchange_symbol("ETHUSDT");
        let mut variants = Vec::new();

        let mut tick_changed = baseline.clone();
        tick_changed.spot_tick_size = decimal("0.1");
        variants.push(Some(tick_changed));
        let mut lot_changed = baseline.clone();
        lot_changed.futures_step_size = decimal("0.01");
        variants.push(Some(lot_changed));
        let mut minimum_changed = baseline.clone();
        minimum_changed.spot_min_notional = decimal("10");
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
            connection_id: Some("test-private".to_string()),
            exchange_event_time_ms: Some(1),
            receive_time_ms: Some(2),
            process_time_ms: Some(3),
            persist_time_ms: None,
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
            let payload = timeout(Duration::from_secs(1), async {
                loop {
                    let bytes = dash_rx
                        .recv()
                        .await
                        .expect("dashboard channel must remain open");
                    let payload: serde_json::Value = rmp_serde::from_slice(&bytes).unwrap();
                    if payload["event"] == "OrderUpdate" {
                        break payload;
                    }
                }
            })
            .await
            .expect("fill must be broadcast while REST is pending");
            assert_eq!(payload["event"], "OrderUpdate");
            assert_eq!(payload["client_order_id"], "spot-cid");
            let _ = release_tx.send(());
        });
        event_tx
            .send(EngineEvent::Ws(Box::new(test_order_update(
                "spot-cid",
                "PARTIALLY_FILLED",
                0.4,
                0.4,
                MarketType::Spot,
                100.0,
            ))))
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
    async fn fill_during_cancel_is_rebased_before_residual_hedge_submission() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Arc::new(Mutex::new(Vec::<String>::new()));
        let server_requests = requests.clone();
        let server = tokio::spawn(async move {
            for _ in 0..2 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = vec![0u8; 8192];
                let read = socket.read(&mut request).await.unwrap();
                let first_line = String::from_utf8_lossy(&request[..read])
                    .lines()
                    .next()
                    .unwrap_or("")
                    .to_string();
                server_requests.lock().unwrap().push(first_line.clone());
                let body = if first_line.starts_with("DELETE ") {
                    tokio::time::sleep(Duration::from_millis(100)).await;
                    r#"{"symbol":"BTCUSDT","clientOrderId":"fut-cid","status":"CANCELED","executedQty":"0.4","avgPrice":"101"}"#.to_string()
                } else {
                    r#"{"symbol":"BTCUSDT","clientOrderId":"repair","status":"NEW","executedQty":"0"}"#.to_string()
                };
                let response = format!(
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let (event_tx, event_rx) = mpsc::channel(8);
        let (engine_tx, _engine_rx) = mpsc::channel(16);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(16);
        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            "test-key".to_string(),
            "test-secret".to_string(),
            dash_tx,
            "live".to_string(),
        );
        install_fresh_nonpaper_account_truth(&mut manager);
        manager.binance_rest.fut_base_url = format!("http://{address}");
        manager.binance_rest.client = reqwest::Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .unwrap();
        let now_ms = OrderManager::current_time_ms();
        manager
            .binance_rest
            .set_rate_limit_observations_for_test(6_000, 100, 2_400, 100, now_ms);
        manager
            .binance_rest
            .set_clock_health_for_test(0, 20, now_ms);
        let mut chase = dual_test_chase(1.0);
        chase.phase = ChasePhase::LegFilledWaiting(Leg::Spot);
        chase.spot_cumulative_filled = 1.0;
        chase.spot_terminal = true;
        chase.spot_fill_price = Some(100.0);
        chase.max_slippage_bps = 0.0;
        manager.chase_states.insert("BTCUSDT".to_string(), chase);
        manager.internal_orders.insert(
            "fut-cid".to_string(),
            InternalOrder {
                client_order_id: "fut-cid".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "NEW".to_string(),
                limit_price: Some(101.0),
            },
        );
        manager
            .order_cumulative_fills
            .insert("fut-cid".to_string(), 0.0);
        event_tx
            .send(EngineEvent::Ws(Box::new(test_order_update(
                "fut-cid",
                "PARTIALLY_FILLED",
                0.4,
                0.4,
                MarketType::Perp,
                101.0,
            ))))
            .await
            .unwrap();

        manager.handle_legging_timeout("spot-cid".to_string()).await;
        server.await.unwrap();

        let chase = manager.chase_states.get("BTCUSDT").unwrap();
        assert_eq!(chase.futures_cumulative_filled, 0.4);
        assert_eq!(chase.perp_quantity, 1.0);
        assert_eq!(chase.phase, ChasePhase::LeggingDefenseTakerPlaced);
        let requests = requests.lock().unwrap();
        assert!(
            requests.iter().any(|line| {
                line.starts_with("POST /fapi/v1/order?") && line.contains("quantity=0.6")
            }),
            "observed requests: {requests:?}"
        );
    }

    #[tokio::test]
    async fn partial_exit_deadline_keeps_original_target_and_requires_reconciliation() {
        let mut manager = paper_test_manager();
        let mut chase = dual_test_chase(1.0);
        chase.is_exit = true;
        chase.spot_side = TradeSide::Sell;
        chase.futures_side = TradeSide::Buy;
        chase.phase = ChasePhase::LegFilledWaiting(Leg::Spot);
        chase.spot_cumulative_filled = 0.6;
        chase.futures_cumulative_filled = 0.4;
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), chase.clone());
        for client_id in ["spot-cid", "fut-cid"] {
            manager.internal_orders.insert(
                client_id.to_string(),
                InternalOrder {
                    client_order_id: client_id.to_string(),
                    symbol: "BTCUSDT".to_string(),
                    status: "CANCELED".to_string(),
                    limit_price: Some(100.0),
                },
            );
        }

        manager
            .handle_partial_fill_deadline("BTCUSDT".to_string(), chase)
            .await;

        let retained = manager.chase_states.get("BTCUSDT").unwrap();
        assert_eq!(retained.phase, ChasePhase::ReconciliationRequired);
        assert_eq!(retained.spot_quantity, 1.0);
        assert_eq!(retained.perp_quantity, 1.0);
        assert_eq!(retained.spot_cumulative_filled, 0.6);
        assert_eq!(retained.futures_cumulative_filled, 0.4);
    }

    #[tokio::test]
    async fn zero_fill_absolute_deadline_classifies_flat_and_terminates_entry() {
        let mut manager = paper_test_manager();
        let chase = dual_test_chase(1.0);
        let cycle_id = chase.cycle_client_order_id().to_string();
        manager.chase_states.insert("BTCUSDT".to_string(), chase);
        for client_id in ["spot-cid", "fut-cid"] {
            manager.internal_orders.insert(
                client_id.to_string(),
                InternalOrder {
                    client_order_id: client_id.to_string(),
                    symbol: "BTCUSDT".to_string(),
                    status: "CANCELED".to_string(),
                    limit_price: Some(100.0),
                },
            );
        }
        let deadline_at_ms = OrderManager::current_time_ms();
        manager
            .cycle_deadlines
            .insert(cycle_id.clone(), deadline_at_ms);

        manager
            .handle_cycle_deadline(cycle_id.clone(), deadline_at_ms)
            .await;

        assert!(!manager.chase_states.contains_key("BTCUSDT"));
        assert!(!manager.cycle_deadlines.contains_key(&cycle_id));
        let record = manager
            .cycle_deadline_records
            .get(&cycle_id)
            .expect("flat deadline classification remains durable");
        assert_eq!(record.classification, CycleDeadlineClassification::Flat);
        assert_eq!(record.spot_cumulative_filled, 0.0);
        assert_eq!(record.futures_cumulative_filled, 0.0);
    }

    #[tokio::test]
    async fn equal_partial_deadline_becomes_smaller_neutral_entry() {
        let mut manager = paper_test_manager();
        let mut chase = dual_test_chase(1.0);
        chase.spot_cumulative_filled = 0.4;
        chase.futures_cumulative_filled = 0.4;
        let cycle_id = chase.cycle_client_order_id().to_string();
        manager.chase_states.insert("BTCUSDT".to_string(), chase);
        for client_id in ["spot-cid", "fut-cid"] {
            manager.internal_orders.insert(
                client_id.to_string(),
                InternalOrder {
                    client_order_id: client_id.to_string(),
                    symbol: "BTCUSDT".to_string(),
                    status: "CANCELED".to_string(),
                    limit_price: Some(100.0),
                },
            );
        }
        let deadline_at_ms = OrderManager::current_time_ms();
        manager
            .cycle_deadlines
            .insert(cycle_id.clone(), deadline_at_ms);

        manager
            .handle_cycle_deadline(cycle_id.clone(), deadline_at_ms)
            .await;

        assert!(!manager.chase_states.contains_key("BTCUSDT"));
        assert_eq!(
            manager
                .cycle_deadline_records
                .get(&cycle_id)
                .map(|record| record.classification),
            Some(CycleDeadlineClassification::EqualPartial)
        );
    }

    #[tokio::test]
    async fn first_ack_deadline_is_durable_and_cannot_be_reset() {
        let journal_path = unique_test_path("absolute-cycle-deadline", "jsonl");
        let mut manager = paper_test_manager();
        manager.execution_state_journal_path = journal_path.clone();
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(1.0));

        let first = manager.arm_cycle_deadline("BTCUSDT").unwrap();
        let second = manager.arm_cycle_deadline("BTCUSDT").unwrap();
        assert_eq!(first, second, "repricing/re-ACK must not extend the TTL");

        let mut restarted = paper_test_manager();
        restarted
            .load_execution_state_from_path(journal_path.clone())
            .unwrap();
        assert_eq!(
            restarted.cycle_deadlines.get("spot-cid").copied(),
            Some(first)
        );
        let _ = std::fs::remove_file(journal_path);
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
        install_fresh_nonpaper_account_truth(&mut manager);
        manager.state = SystemState::Trading;
        manager.binance_rest.spot_base_url = format!("http://{address}");
        manager.binance_rest.fut_base_url = format!("http://{address}");
        manager.binance_rest.client = reqwest::Client::builder()
            .connect_timeout(Duration::from_millis(100))
            .timeout(Duration::from_secs(2))
            .build()
            .unwrap();
        let now_ms = OrderManager::current_time_ms();
        manager
            .binance_rest
            .set_rate_limit_observations_for_test(6_000, 100, 2_400, 100, now_ms);
        manager
            .binance_rest
            .set_clock_health_for_test(0, 20, now_ms);
        manager
            .exchange_info
            .insert("BTCUSDT".to_string(), test_exchange_symbol("BTCUSDT"));
        manager.exchange_info_updated_at = Some(Instant::now());
        manager.spot_balances.insert("USDT".to_string(), 10_000.0);
        manager
            .spot_available_balances
            .insert("USDT".to_string(), 10_000.0);
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
                .send(EngineEvent::Ws(Box::new(test_order_update(
                    "spot-cid",
                    "FILLED",
                    1.0,
                    1.0,
                    MarketType::Spot,
                    100.0,
                ))))
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
            observed.iter().all(|line| {
                !line.contains("/fapi/v1/order?")
                    || !line.contains("type=MARKET")
                    || !line.contains("reduceOnly=true")
            }),
            "an entry-leg hedge must not be mislabeled reduce-only: {observed:?}"
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
        assert_eq!(manager.internal_orders["spot-cid"].status, "FILLED");
        server.abort();
    }

    #[tokio::test]
    async fn futures_rest_ack_preserves_partial_and_completed_private_progress() {
        for complete in [false, true] {
            let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap();
            let (engine_tx, event_rx) = mpsc::channel(32);
            let inject_tx = engine_tx.clone();
            let server = tokio::spawn(async move {
                for leg in ["spot-cid", "fut-cid"] {
                    let (mut socket, _) = listener.accept().await.unwrap();
                    let mut request = vec![0u8; 8192];
                    let read = socket.read(&mut request).await.unwrap();
                    assert!(String::from_utf8_lossy(&request[..read]).starts_with("POST "));
                    if leg == "fut-cid" {
                        let fresh_update =
                            |client_id: &str, status: &str, quantity: f64, market| {
                                let mut event = test_order_update(
                                    client_id, status, quantity, quantity, market, 100.0,
                                );
                                if let WsEvent::OrderUpdate { event_time_ms, .. } = &mut event {
                                    *event_time_ms = Some(OrderManager::current_time_ms());
                                }
                                event
                            };
                        if complete {
                            inject_tx
                                .send(EngineEvent::Ws(Box::new(fresh_update(
                                    "spot-cid",
                                    "FILLED",
                                    1.0,
                                    MarketType::Spot,
                                ))))
                                .await
                                .unwrap();
                        }
                        inject_tx
                            .send(EngineEvent::Ws(Box::new(fresh_update(
                                "fut-cid",
                                if complete {
                                    "FILLED"
                                } else {
                                    "PARTIALLY_FILLED"
                                },
                                if complete { 1.0 } else { 0.4 },
                                MarketType::Perp,
                            ))))
                            .await
                            .unwrap();
                        tokio::time::sleep(Duration::from_millis(100)).await;
                    }
                    let body = format!(
                        r#"{{"symbol":"BTCUSDT","clientOrderId":"{leg}","status":"NEW","executedQty":"0"}}"#
                    );
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                        body.len()
                    );
                    socket.write_all(response.as_bytes()).await.unwrap();
                }
            });
            let mut manager = paper_test_manager();
            manager.event_receiver = event_rx;
            manager.engine_tx = engine_tx;
            manager.trading_mode = "testnet".into();
            manager.binance_rest.trading_mode = "testnet".into();
            manager.binance_rest.spot_base_url = format!("http://{address}");
            manager.binance_rest.fut_base_url = format!("http://{address}");
            install_fresh_nonpaper_account_truth(&mut manager);
            prepare_emergency_market(&mut manager, "BTCUSDT");
            let now_ms = OrderManager::current_time_ms();
            manager
                .binance_rest
                .set_rate_limit_observations_for_test(6000, 100, 2400, 100, now_ms);
            manager
                .binance_rest
                .set_clock_health_for_test(0, 20, now_ms);
            manager.exchange_info_updated_at = Some(Instant::now());
            manager
                .spot_available_balances
                .insert("USDT".into(), 10_000.0);
            let capacity = ExecutableDepth {
                bid_notional_usd: 100_000.0,
                ask_notional_usd: 100_000.0,
                observed_at: Instant::now(),
            };
            manager
                .spot_depth_capacity
                .insert("BTCUSDT".into(), capacity);
            manager
                .perp_depth_capacity
                .insert("BTCUSDT".into(), capacity);
            let mut chase = dual_test_chase(1.0);
            chase.phase = ChasePhase::Idle;
            manager.chase_states.insert("BTCUSDT".into(), chase);
            let mut evidence_rx = manager.dash_tx.subscribe();
            timeout(
                Duration::from_secs(3),
                manager.try_place_dual_maker("BTCUSDT".into()),
            )
            .await
            .unwrap();
            server.await.unwrap();
            let mut evidence = Vec::<Value>::new();
            while let Ok(bytes) = evidence_rx.try_recv() {
                evidence.push(rmp_serde::from_slice(&bytes).unwrap());
            }
            if complete {
                assert!(
                    manager.chase_states.is_empty(),
                    "late REST ACK resurrected completed chase"
                );
                assert_eq!(manager.internal_orders["fut-cid"].status, "FILLED");
                assert_eq!(manager.terminal_publications.len(), 1);
            } else {
                assert_eq!(
                    manager.chase_states["BTCUSDT"].phase,
                    ChasePhase::LegFilledWaiting(Leg::Futures),
                    "events={evidence:?}"
                );
                assert_eq!(
                    manager.internal_orders["fut-cid"].status,
                    "PARTIALLY_FILLED"
                );
            }
        }
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

    #[tokio::test]
    async fn delta_neutral_pair_does_not_net_away_usdm_margin_requirement() {
        let mut manager = paper_test_manager();
        manager.account_equity_usd = 1_000.0;
        manager.max_gross_exposure_usd = 1_000_000_000.0;
        manager.basis_deviation_stop_bps = 10_000.0;
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: Some(TrackedLegPosition {
                    side: "LONG".to_string(),
                    entry_price: 100.0,
                    quantity: 2_500.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 100.0,
                    quantity: 2_500.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
            },
        );

        assert!(
            manager.check_circuit_breakers().await,
            "equal spot/perp notionals must still retain the gross USD-M maintenance requirement"
        );
    }

    #[test]
    fn private_recovery_symbols_union_positions_chases_and_unresolved_orders() {
        let mut manager = paper_test_manager();
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: None,
                perp: None,
            },
        );
        manager
            .chase_states
            .insert("ETHUSDT".to_string(), dual_test_chase(1.0));
        manager.internal_orders.insert(
            "open-order".to_string(),
            InternalOrder {
                client_order_id: "open-order".to_string(),
                symbol: "SOLUSDT".to_string(),
                status: "NEW".to_string(),
                limit_price: Some(100.0),
            },
        );
        manager.internal_orders.insert(
            "resolved-order".to_string(),
            InternalOrder {
                client_order_id: "resolved-order".to_string(),
                symbol: "XRPUSDT".to_string(),
                status: "FILLED".to_string(),
                limit_price: Some(1.0),
            },
        );

        assert_eq!(
            manager.private_recovery_symbols(),
            HashSet::from([
                "BTCUSDT".to_string(),
                "ETHUSDT".to_string(),
                "SOLUSDT".to_string(),
            ])
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
        first.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: Some(TrackedLegPosition {
                    side: "LONG".to_string(),
                    entry_price: 100.0,
                    quantity: 2.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
                perp: None,
            },
        );
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
        assert_eq!(
            recovered.tracked_positions["BTCUSDT"]
                .spot
                .as_ref()
                .map(|leg| leg.quantity),
            Some(2.0)
        );
        assert_eq!(recovered.current_gross_exposure_usd, 200.0);
        assert_eq!(
            recovered.pending_spot_collateral_reserved_usd("USDT", None),
            200.0,
            "the durable chase must restore its full quote-collateral reservation"
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
        let now_ms = OrderManager::current_time_ms();
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
                sequence_contiguous: true,
                connection_id: "test-perp-1".to_string(),
                exchange_event_time_ms: Some(now_ms),
                receive_time_ms: now_ms,
                process_time_ms: now_ms,
                persist_time_ms: None,
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
                sequence_contiguous: true,
                connection_id: "test-perp-1".to_string(),
                exchange_event_time_ms: Some(now_ms),
                receive_time_ms: now_ms,
                process_time_ms: now_ms,
                persist_time_ms: None,
            })
            .await;

        assert_eq!(
            manager.depth_sequences.get("BTCUSDT:perp"),
            Some(&12),
            "the rejected cursor advances so one gap cannot storm forever"
        );
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
    async fn stale_exchange_event_cannot_refresh_executable_depth() {
        let mut manager = paper_test_manager();
        let now_ms = OrderManager::current_time_ms();
        manager
            .handle_ws_event(WsEvent::L2Depth {
                symbol: "BTCUSDT".to_string(),
                market: MarketType::Perp,
                bids: vec![[100.0, 1.0]],
                asks: vec![[101.0, 1.0]],
                first_update_id: Some(10),
                final_update_id: Some(10),
                previous_final_update_id: None,
                is_snapshot: true,
                sequence_contiguous: true,
                connection_id: "test-perp-stale".to_string(),
                exchange_event_time_ms: Some(now_ms - 5_000),
                receive_time_ms: now_ms,
                process_time_ms: now_ms,
                persist_time_ms: None,
            })
            .await;

        assert!(!manager.perp_top_cache.contains_key("BTCUSDT"));
        assert!(!manager.perp_depth_capacity.contains_key("BTCUSDT"));
        assert!(!manager.depth_sequences.contains_key("BTCUSDT:perp"));
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
    async fn zero_fill_terminal_rebases_peer_cancel_execution_before_rejection() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = vec![0u8; 8192];
            let read = socket.read(&mut request).await.unwrap();
            assert!(String::from_utf8_lossy(&request[..read]).starts_with("DELETE "));
            let body = r#"{"symbol":"BTCUSDT","clientOrderId":"fut-cid","status":"CANCELED","executedQty":"0.4","avgPrice":"101"}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            socket.write_all(response.as_bytes()).await.unwrap();
        });
        let mut manager = paper_test_manager();
        manager.binance_rest.trading_mode = "live".into();
        manager.binance_rest.fut_base_url = format!("http://{address}");
        manager
            .chase_states
            .insert("BTCUSDT".into(), dual_test_chase(1.0));
        for client_order_id in ["spot-cid", "fut-cid"] {
            manager.internal_orders.insert(
                client_order_id.into(),
                InternalOrder {
                    client_order_id: client_order_id.into(),
                    symbol: "BTCUSDT".into(),
                    status: "NEW".into(),
                    limit_price: Some(100.0),
                },
            );
        }
        manager
            .handle_ws_event(test_order_update(
                "spot-cid",
                "EXPIRED",
                0.0,
                0.0,
                MarketType::Spot,
                100.0,
            ))
            .await;
        server.await.unwrap();
        let retained = manager
            .chase_states
            .get("BTCUSDT")
            .expect("fill exposure retained");
        assert_eq!(retained.phase, ChasePhase::ReconciliationRequired);
        assert_eq!(retained.futures_cumulative_filled, 0.4);
        assert_eq!(
            manager.tracked_positions["BTCUSDT"]
                .perp
                .as_ref()
                .unwrap()
                .quantity,
            0.4
        );
        assert!(
            manager
                .terminal_publications
                .values()
                .all(|event| event["execution_type"] != "DUAL_SUBMISSION_FAILED")
        );
    }

    #[test]
    fn late_submission_ack_never_regresses_private_order_progress() {
        let mut manager = paper_test_manager();
        for status in [
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELED",
            "EXPIRED",
            "REJECTED",
        ] {
            manager.internal_orders.insert(
                "late".into(),
                InternalOrder {
                    client_order_id: "late".into(),
                    symbol: "BTCUSDT".into(),
                    status: status.into(),
                    limit_price: Some(100.0),
                },
            );
            manager.acknowledge_submitted_order("late");
            assert_eq!(manager.internal_orders["late"].status, status);
        }
        manager.internal_orders.clear();
        manager.acknowledge_submitted_order("late");
        assert!(manager.internal_orders.is_empty());
    }

    #[tokio::test]
    async fn spot_full_response_applies_every_fill_even_when_actor_send_queue_is_closed() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = vec![0u8; 8192];
            let read = socket.read(&mut request).await.unwrap();
            assert!(String::from_utf8_lossy(&request[..read]).starts_with("POST "));
            let body = serde_json::json!({
                "symbol":"BTCUSDT", "clientOrderId":"spot-cid", "orderId":42,
                "status":"FILLED", "executedQty":"1", "transactTime":OrderManager::current_time_ms(),
                "fills":[
                    {"tradeId":1, "price":"100", "qty":"0.4", "commission":"0.04", "commissionAsset":"USDT"},
                    {"tradeId":2, "price":"100", "qty":"0.6", "commission":"0.06", "commissionAsset":"USDT"}
                ]
            }).to_string();
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            socket.write_all(response.as_bytes()).await.unwrap();
        });
        let mut manager = paper_test_manager();
        prepare_emergency_market(&mut manager, "BTCUSDT");
        manager.trading_mode = "testnet".into();
        manager.binance_rest.trading_mode = "testnet".into();
        manager.binance_rest.spot_base_url = format!("http://{address}");
        let (closed_tx, closed_rx) = mpsc::channel(1);
        drop(closed_rx);
        manager.engine_tx = closed_tx;
        let mut chase = dual_test_chase(1.0);
        chase.is_exit = true;
        chase.skip_perp_leg = true;
        chase.perp_quantity = 0.0;
        chase.futures_terminal = true;
        chase.futures_client_order_id.clear();
        chase.futures_order_aliases.clear();
        chase.spot_side = TradeSide::Sell;
        chase.phase = ChasePhase::Idle;
        manager.chase_states.insert("BTCUSDT".into(), chase);
        manager.tracked_positions.insert(
            "BTCUSDT".into(),
            TrackedPosition {
                symbol: "BTCUSDT".into(),
                spot: Some(tracked_test_leg("LONG", 1.0)),
                perp: None,
            },
        );
        manager.try_place_dual_maker("BTCUSDT".into()).await;
        server.await.unwrap();
        assert_eq!(manager.order_cumulative_fills["spot-cid"], 1.0);
        assert_eq!(manager.internal_orders["spot-cid"].status, "FILLED");
        assert!(!manager.chase_states.contains_key("BTCUSDT"));
        let summary = manager
            .terminal_publications
            .values()
            .find(|event| event["execution_type"] == "FILLED_CYCLE")
            .expect("complete economic terminal summary");
        assert_eq!(summary["spot_cumulative_filled_quantity_decimal"], "1");
        let commissions = summary["commissions"].as_array().unwrap();
        assert_eq!(commissions.len(), 2);
        let total = commissions.iter().fold(ExactDecimal::ZERO, |sum, row| {
            assert_eq!(row["asset"], "USDT");
            sum.checked_add(decimal(row["amount"].as_str().unwrap()))
                .unwrap()
        });
        assert_eq!(total.to_string(), "0.1");
    }

    #[tokio::test]
    async fn private_fill_commission_reduces_spot_inventory_once_end_to_end() {
        let mut manager = paper_test_manager();
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(1.0));

        let mut event =
            test_order_update("spot-cid", "FILLED", 1.0, 1.0, MarketType::Spot, 50_000.0);
        if let WsEvent::OrderUpdate {
            commission,
            commission_asset,
            order_id,
            trade_id,
            ..
        } = &mut event
        {
            *commission = Some(0.001);
            *commission_asset = Some("BTC".to_string());
            *order_id = Some(101);
            *trade_id = Some(202);
        }
        manager.handle_ws_event(event.clone()).await;

        let inventory = manager
            .tracked_positions
            .get("BTCUSDT")
            .and_then(|position| position.spot.as_ref())
            .map(|leg| ExactDecimal::from_f64(leg.quantity).unwrap().to_string());
        assert_eq!(inventory.as_deref(), Some("0.999"));
        assert!(
            manager
                .applied_commission_keys
                .contains_key("spot:BTCUSDT:101:202")
        );

        manager.handle_ws_event(event).await;
        let duplicate_inventory = manager
            .tracked_positions
            .get("BTCUSDT")
            .and_then(|position| position.spot.as_ref())
            .map(|leg| ExactDecimal::from_f64(leg.quantity).unwrap().to_string());
        assert_eq!(duplicate_inventory.as_deref(), Some("0.999"));
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
                spot_order_aliases: vec!["spot-cid".to_string()],
                futures_order_aliases: vec!["fut-cid".to_string()],
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
                ..ChaseState::default()
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

        let now_ms = OrderManager::current_time_ms();
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
                sequence_contiguous: true,
                connection_id: "test-spot-1".to_string(),
                exchange_event_time_ms: None,
                receive_time_ms: now_ms,
                process_time_ms: now_ms,
                persist_time_ms: None,
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
                sequence_contiguous: true,
                connection_id: "test-perp-1".to_string(),
                exchange_event_time_ms: Some(now_ms),
                receive_time_ms: now_ms,
                process_time_ms: now_ms,
                persist_time_ms: None,
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

        let WsEvent::OrderUpdate {
            client_order_id,
            avg_fill_price,
            execution_type,
            ..
        } = expect_ws_event(first)
        else {
            panic!("unexpected first websocket event")
        };
        assert_eq!(client_order_id, "spot-cid");
        assert_eq!(avg_fill_price, Some(100.0));
        assert_eq!(execution_type.as_deref(), Some("PAPER_RESTING_CROSS_FILL"));

        let WsEvent::OrderUpdate {
            client_order_id,
            avg_fill_price,
            execution_type,
            ..
        } = expect_ws_event(second)
        else {
            panic!("unexpected second websocket event")
        };
        assert_eq!(client_order_id, "fut-cid");
        assert_eq!(avg_fill_price, Some(104.0));
        assert_eq!(execution_type.as_deref(), Some("PAPER_RESTING_CROSS_FILL"));
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
                spot_order_aliases: Vec::new(),
                futures_order_aliases: vec!["fut-cid".to_string()],
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
                ..ChaseState::default()
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
                connection_id: Some("test-private".to_string()),
                exchange_event_time_ms: Some(0),
                receive_time_ms: Some(1),
                process_time_ms: Some(2),
                persist_time_ms: None,
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
                spot_order_aliases: Vec::new(),
                futures_order_aliases: vec!["fut-cid".to_string()],
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
                ..ChaseState::default()
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
    async fn market_data_subscription_intent_cannot_create_orders_or_chases() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, _engine_rx) = mpsc::channel(8);
        let (subscription_tx, mut subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel::<Vec<u8>>(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "testnet".to_string(),
        );
        manager.state = SystemState::Disconnected;

        manager
            .handle_alpha_instruction(crate::ipc::AlphaInstruction {
                symbol: Some("dogeusdt".to_string()),
                intent: "SUBSCRIBE_MARKET_DATA".to_string(),
                ..crate::ipc::AlphaInstruction::default()
            })
            .await;

        assert_eq!(subscription_rx.try_recv().unwrap(), "DOGEUSDT");
        assert!(manager.chase_states.is_empty());
        assert!(manager.internal_orders.is_empty());
        assert!(manager.tracked_positions.is_empty());
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
                spot_order_aliases: vec!["spot-cid".to_string()],
                futures_order_aliases: vec!["fut-cid".to_string()],
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
                ..ChaseState::default()
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

        let WsEvent::OrderUpdate {
            symbol,
            filled_qty,
            avg_fill_price,
            maker,
            execution_type,
            ..
        } = expect_ws_event(taker_fill)
        else {
            panic!("unexpected taker websocket event")
        };
        assert_eq!(symbol, "BTCUSDT");
        assert!((filled_qty - 0.6).abs() < 1e-12);
        assert_eq!(avg_fill_price, Some(103.0));
        assert_eq!(maker, Some(false));
        assert_eq!(execution_type.as_deref(), Some("PAPER_TAKER_FILL"));

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
                spot_order_aliases: vec!["spot-cid".to_string()],
                futures_order_aliases: vec!["fut-cid".to_string()],
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
                ..ChaseState::default()
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
                spot_order_aliases: vec!["spot-cid".to_string()],
                futures_order_aliases: vec!["fut-cid".to_string()],
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
                ..ChaseState::default()
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

    fn expect_ws_event(event: EngineEvent) -> WsEvent {
        match event {
            EngineEvent::Ws(event) => *event,
            other => panic!("expected websocket event, got {}", other_type_name(&other)),
        }
    }

    fn other_type_name(event: &EngineEvent) -> &'static str {
        match event {
            EngineEvent::Ws(_) => "ws",
            EngineEvent::Alpha(_) => "alpha",
            EngineEvent::LeggingTimeout(_) => "legging_timeout",
            EngineEvent::CycleDeadline { .. } => "cycle_deadline",
            EngineEvent::StrategyTick => "strategy_tick",
            EngineEvent::PositionAuditTick => "position_audit_tick",
            EngineEvent::ExchangeInfoRefreshResult(_) => "exchange_info_refresh_result",
            EngineEvent::RecoveryBarrier { .. } => "recovery_barrier",
            EngineEvent::RecoveryBarrierFailed { .. } => "recovery_barrier_failed",
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
                spot_order_aliases: vec!["spot-cid".to_string()],
                futures_order_aliases: vec!["fut-cid".to_string()],
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
                ..ChaseState::default()
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
        let now_ms = OrderManager::current_time_ms();
        manager
            .handle_ws_event(WsEvent::BookTicker {
                symbol: "BTCUSDT".to_string(),
                bid_price: 100.02,
                ask_price: 100.03,
                connection_id: "test-perp-1".to_string(),
                exchange_event_time_ms: Some(now_ms),
                receive_time_ms: now_ms,
                process_time_ms: now_ms,
                persist_time_ms: None,
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
                spot_order_aliases: Vec::new(),
                futures_order_aliases: vec!["fut-cid".to_string()],
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
                ..ChaseState::default()
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
        for role in ["spot-public", "futures-public", "futures-market"] {
            manager
                .market_stream_ready_roles
                .insert(OrderManager::market_stream_role_key("REPLAYUSDT", role));
        }
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

    #[test]
    fn public_market_data_quorum_requires_all_three_role_specific_sessions() {
        let mut manager = paper_test_manager();
        manager
            .market_stream_ready_roles
            .retain(|key| !key.starts_with("BTCUSDT:"));
        assert!(!manager.market_data_quorum_ready("BTCUSDT"));

        for role in ["spot-public", "futures-public"] {
            manager
                .market_stream_ready_roles
                .insert(OrderManager::market_stream_role_key("BTCUSDT", role));
        }
        assert!(!manager.market_data_quorum_ready("BTCUSDT"));

        manager
            .market_stream_ready_roles
            .insert(OrderManager::market_stream_role_key(
                "BTCUSDT",
                "futures-market",
            ));
        assert!(manager.market_data_quorum_ready("BTCUSDT"));
    }

    #[tokio::test]
    async fn feed_scoped_disconnect_revokes_quorum_without_clearing_other_market() {
        let mut manager = paper_test_manager();
        manager.spot_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 99.0,
                ask_price: 100.0,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 100.0,
                ask_price: 101.0,
                bid_qty: 1.0,
                ask_qty: 1.0,
            },
        );

        manager
            .handle_ws_event(WsEvent::Disconnected {
                symbol: "BTCUSDT".to_string(),
                stream_type: WsStreamType::MarketData,
                connection_id: Some("spot-public-test".to_string()),
                connection_role: Some("spot-public".to_string()),
            })
            .await;

        assert!(!manager.market_data_quorum_ready("BTCUSDT"));
        assert!(!manager.spot_top_cache.contains_key("BTCUSDT"));
        assert!(manager.perp_top_cache.contains_key("BTCUSDT"));
    }

    #[tokio::test]
    async fn public_disconnect_latch_survives_restart_and_requires_three_role_recovery() {
        let mut manager = paper_test_manager();
        manager
            .handle_ws_event(WsEvent::Disconnected {
                symbol: "BTCUSDT".to_string(),
                stream_type: WsStreamType::MarketData,
                connection_id: Some("spot-public-test".to_string()),
                connection_role: Some("spot-public".to_string()),
            })
            .await;

        assert_eq!(manager.state, SystemState::Reconciling);
        assert_eq!(
            manager.continuous_risk_state,
            ContinuousRiskState::Reconciling
        );
        assert!(manager.public_stream_recovery_symbols.contains("BTCUSDT"));
        let mut recovered = paper_test_manager();
        recovered
            .load_execution_state_from_path(manager.execution_state_journal_path.clone())
            .expect("public-disconnect latch must be durable");
        assert!(recovered.public_stream_recovery_symbols.contains("BTCUSDT"));

        manager.execute_reconciliation_sequence().await;
        assert_eq!(manager.state, SystemState::Reconciling);
        assert_ne!(
            manager.continuous_risk_state,
            ContinuousRiskState::Normal,
            "account proof alone cannot replace public semantic readiness"
        );

        manager
            .handle_ws_event(WsEvent::Connected {
                symbol: "BTCUSDT".to_string(),
                stream_type: WsStreamType::MarketData,
                connection_id: Some("spot-public-recovered".to_string()),
                connection_role: Some("spot-public".to_string()),
            })
            .await;
        assert!(manager.public_stream_recovery_symbols.is_empty());
        assert!(manager.market_data_quorum_ready("BTCUSDT"));
        assert_eq!(manager.state, SystemState::Trading);
        assert_eq!(manager.continuous_risk_state, ContinuousRiskState::Normal);
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

    #[tokio::test]
    async fn first_partial_fill_arms_hard_hedge_deadline() {
        let (_event_tx, event_rx) = mpsc::channel(8);
        let (engine_tx, mut engine_rx) = mpsc::channel(16);
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
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(1.0));
        for client_id in ["spot-cid", "fut-cid"] {
            manager.internal_orders.insert(
                client_id.to_string(),
                InternalOrder {
                    client_order_id: client_id.to_string(),
                    symbol: "BTCUSDT".to_string(),
                    status: "NEW".to_string(),
                    limit_price: Some(100.0),
                },
            );
            manager
                .order_cumulative_fills
                .insert(client_id.to_string(), 0.0);
        }

        manager
            .process_ws_event(test_order_update(
                "spot-cid",
                "PARTIALLY_FILLED",
                0.4,
                0.4,
                MarketType::Spot,
                100.0,
            ))
            .await;

        let chase = manager.chase_states.get("BTCUSDT").unwrap();
        assert_eq!(chase.phase, ChasePhase::LegFilledWaiting(Leg::Spot));
        assert_eq!(chase.spot_cumulative_filled, 0.4);
        let deadline = timeout(Duration::from_millis(600), engine_rx.recv())
            .await
            .expect("partial fill must arm a bounded deadline")
            .expect("engine channel should remain open");
        assert!(matches!(
            deadline,
            EngineEvent::LeggingTimeout(client_id) if client_id == "spot-cid"
        ));
    }

    #[test]
    fn generation_aliases_aggregate_late_fills_and_replacement_ids_are_deterministic() {
        let mut manager = paper_test_manager();
        let mut chase = dual_test_chase(1.0);
        chase.futures_order_aliases.push("old-fut-cid".to_string());
        chase.futures_client_order_id = "new-fut-cid".to_string();
        chase.futures_order_aliases.push("new-fut-cid".to_string());
        chase.futures_cumulative_filled = 0.25;
        manager
            .order_cumulative_fills
            .insert("old-fut-cid".to_string(), 0.25);
        manager.internal_orders.insert(
            "old-fut-cid".to_string(),
            InternalOrder {
                client_order_id: "old-fut-cid".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "PARTIALLY_FILLED".to_string(),
                limit_price: Some(101.0),
            },
        );

        let snapshot = TerminalOrderSnapshot {
            status: ExchangeOrderStatus::Canceled,
            cumulative_filled_qty: 0.4,
            average_fill_price: Some(101.0),
        };
        manager
            .apply_terminal_order_snapshot(&mut chase, Leg::Futures, "old-fut-cid", snapshot)
            .unwrap();
        assert_eq!(chase.futures_cumulative_filled, 0.4);
        assert_eq!(
            chase.leg_for_client_order_id("old-fut-cid"),
            Some(Leg::Futures)
        );
        let first = manager.replacement_client_order_id(&chase, Leg::Futures);
        let replay = manager.replacement_client_order_id(&chase, Leg::Futures);
        assert_eq!(first, replay);
        chase.set_active_client_order_id(Leg::Futures, first.clone());
        assert!(chase.futures_order_aliases.contains(&first));
        assert!(
            chase
                .futures_order_aliases
                .contains(&"old-fut-cid".to_string())
        );
    }

    #[test]
    fn expired_in_match_is_a_typed_terminal_status() {
        let parsed = ExchangeOrderStatus::parse("EXPIRED_IN_MATCH").unwrap();
        assert!(parsed.is_terminal_without_full_fill());
        assert!(is_terminal_internal_status("EXPIRED_IN_MATCH"));
        let snapshot = OrderManager::parse_terminal_order_snapshot(
            r#"{"clientOrderId":"cid","status":"EXPIRED_IN_MATCH","executedQty":"0.25","avgPrice":"100"}"#,
            "cid",
        )
        .unwrap();
        assert_eq!(snapshot.status, ExchangeOrderStatus::ExpiredInMatch);
        assert_eq!(snapshot.cumulative_filled_qty, 0.25);
    }

    #[tokio::test]
    async fn reverse_short_spot_entry_is_rejected_before_any_order_state() {
        let mut manager = paper_test_manager();
        let initial_balance = 3.0;
        manager
            .spot_balances
            .insert("BTC".to_string(), initial_balance);
        manager
            .handle_alpha_instruction(
                crate::ipc::AlphaInstruction {
                    symbol: Some("BTCUSDT".to_string()),
                    intent: "ENTER_SHORT".to_string(),
                    quantity: 0.1,
                    exposure_scale: 1.0,
                    intent_id: Some("reverse-disabled".to_string()),
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;
        assert!(manager.chase_states.is_empty());
        assert!(manager.internal_orders.is_empty());
        assert_eq!(manager.spot_balances.get("BTC"), Some(&initial_balance));
    }

    #[test]
    fn pending_entries_reserve_spendable_spot_collateral() {
        let mut manager = paper_test_manager();
        manager
            .spot_available_balances
            .insert("USDT".to_string(), 150.0);
        manager
            .chase_states
            .insert("BTCUSDT".to_string(), dual_test_chase(1.0));

        assert_eq!(
            manager.pending_spot_collateral_reserved_usd("USDT", None),
            100.0
        );
        assert!(manager.spot_collateral_available_for_entry("ETHUSDT", 50.0, None));
        assert!(
            !manager.spot_collateral_available_for_entry("ETHUSDT", 50.01, None),
            "a second entry cannot spend quote collateral already reserved by a durable chase"
        );
        assert!(
            !manager.spot_collateral_available_for_entry("ETHBTC", 1.0, None),
            "unknown quote assets fail closed"
        );
    }

    #[tokio::test]
    async fn fifth_risk_bearing_symbol_is_rejected_while_pending_slots_are_reserved() {
        let mut manager = paper_test_manager();
        for symbol in ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"] {
            manager.tracked_positions.insert(
                symbol.to_string(),
                TrackedPosition {
                    symbol: symbol.to_string(),
                    spot: None,
                    perp: Some(TrackedLegPosition {
                        side: "SHORT".to_string(),
                        entry_price: 100.0,
                        quantity: 0.1,
                        unrealized_pnl: 0.0,
                        last_mark_price: 100.0,
                    }),
                },
            );
        }
        manager.recompute_gross_exposure();
        manager
            .handle_alpha_instruction(
                crate::ipc::AlphaInstruction {
                    symbol: Some("XRPUSDT".to_string()),
                    intent: "ENTER_LONG".to_string(),
                    quantity: 0.1,
                    exposure_scale: 1.0,
                    intent_id: Some("fifth-slot".to_string()),
                    ..crate::ipc::AlphaInstruction::default()
                }
                .seal_internal(),
            )
            .await;
        assert!(!manager.chase_states.contains_key("XRPUSDT"));
        assert!(manager.internal_orders.is_empty());
    }

    #[test]
    fn signed_standard_spot_and_usdm_truth_is_separate_complete_and_exact() {
        let observed_at_ms = OrderManager::current_time_ms();
        let spot = OrderManager::parse_standard_spot_account_truth(
            r#"{"canTrade":true,"permissions":["SPOT"],"balances":[{"asset":"BTC","free":"0.99900000","locked":"0.00100000"},{"asset":"USDT","free":"1000.00000000","locked":"0.00000000"}]}"#,
            2,
            observed_at_ms,
        )
        .unwrap();
        assert_eq!(
            spot.wallet_balance.get("BTC").map(String::as_str),
            Some("1")
        );
        assert_eq!(
            spot.available_balance.get("BTC").map(String::as_str),
            Some("0.999")
        );
        assert_eq!(spot.open_orders, 2);
        assert_eq!(spot.borrow_state, "NOT_APPLICABLE_STANDARD_SPOT");

        let (usdm, margin_balance) = OrderManager::parse_usdm_account_risk_truth(
            r#"{"totalWalletBalance":"1000.00000000","availableBalance":"800.00000000","totalMaintMargin":"50.00000000","totalMarginBalance":"1000.00000000","positions":[{"symbol":"BTCUSDT","positionSide":"BOTH","positionAmt":"-0.25000000","leverage":"2"},{"symbol":"ETHUSDT","positionSide":"BOTH","positionAmt":"0.00000000","leverage":"2"}]}"#,
            r#"[{"symbol":"BTCUSDT","positionSide":"BOTH","positionAmt":"-0.25000000","liquidationPrice":"25000.12500000"},{"symbol":"ETHUSDT","positionSide":"BOTH","positionAmt":"0.00000000","liquidationPrice":"0.00000000"}]"#,
            r#"{"dualSidePosition":false}"#,
            3,
            observed_at_ms,
        )
        .unwrap();
        assert_eq!(margin_balance, 1000.0);
        assert_eq!(usdm.wallet_balance, "1000");
        assert_eq!(usdm.available_balance, "800");
        assert_eq!(usdm.maintenance_margin, "50");
        assert_eq!(usdm.margin_ratio, 0.05);
        assert_eq!(usdm.position_mode, "ONE_WAY");
        assert_eq!(usdm.open_orders, 3);
        let position = usdm.positions.get("BTCUSDT:BOTH").unwrap();
        assert_eq!(position.position_amount, "-0.25");
        assert_eq!(position.leverage, 2);
        assert_eq!(position.liquidation_price, "25000.125");
    }

    #[test]
    fn signed_usdm_truth_rejects_missing_liquidation_or_leverage_and_drives_risk() {
        let account_without_leverage = r#"{"totalWalletBalance":"1000","availableBalance":"800","totalMaintMargin":"50","totalMarginBalance":"1000","positions":[{"symbol":"BTCUSDT","positionSide":"BOTH","positionAmt":"-0.25"}]}"#;
        let position_risk = r#"[{"symbol":"BTCUSDT","positionSide":"BOTH","positionAmt":"-0.25","liquidationPrice":"25000"}]"#;
        assert!(
            OrderManager::parse_usdm_account_risk_truth(
                account_without_leverage,
                position_risk,
                r#"{"dualSidePosition":false}"#,
                0,
                OrderManager::current_time_ms(),
            )
            .unwrap_err()
            .contains("leverage")
        );
        assert!(
            OrderManager::parse_usdm_account_risk_truth(
                r#"{"totalWalletBalance":"1000","availableBalance":"800","totalMaintMargin":"0","totalMarginBalance":"1000","positions":[{"symbol":"BTCUSDT","positionSide":"BOTH","positionAmt":"0","leverage":"2"}]}"#,
                r#"[{"symbol":"BTCUSDT","positionSide":"BOTH","positionAmt":"0","liquidationPrice":"0"}]"#,
                r#"{"dualSidePosition":true}"#,
                0,
                OrderManager::current_time_ms(),
            )
            .unwrap_err()
            .contains("contradicts")
        );

        let mut manager = paper_test_manager();
        manager.trading_mode = "live".to_string();
        install_fresh_nonpaper_account_truth(&mut manager);
        manager.usdm_account_truth.as_mut().unwrap().margin_ratio = 0.81;
        assert_eq!(
            manager.circuit_breaker_assessment().unwrap().1,
            ContinuousRiskState::Derisking
        );
        manager.usdm_account_truth.as_mut().unwrap().margin_ratio = 0.0;
        manager.usdm_account_truth.as_mut().unwrap().position_mode = "HEDGE".to_string();
        assert_eq!(
            manager.circuit_breaker_assessment().unwrap(),
            (
                "unsupported_usdm_position_mode".to_string(),
                ContinuousRiskState::EntryFrozen
            )
        );
        manager.usdm_account_truth.as_mut().unwrap().position_mode = "ONE_WAY".to_string();
        manager
            .standard_spot_account_truth
            .as_mut()
            .unwrap()
            .observed_at_ms -= ACCOUNT_TRUTH_MAX_AGE_MS + 1;
        assert_eq!(
            manager.circuit_breaker_assessment().unwrap().1,
            ContinuousRiskState::EntryFrozen
        );
    }

    #[test]
    fn signed_usdm_truth_accepts_flat_demo_account_with_explicit_position_mode() {
        let (truth, margin_balance) = OrderManager::parse_usdm_account_risk_truth(
            r#"{"totalWalletBalance":"10000","availableBalance":"10000","totalMaintMargin":"0","totalMarginBalance":"10000","positions":[]}"#,
            "[]",
            r#"{"dualSidePosition":false}"#,
            0,
            OrderManager::current_time_ms(),
        )
        .expect("a signed flat account plus explicit position mode is complete truth");

        assert_eq!(truth.position_mode, "ONE_WAY");
        assert!(truth.positions.is_empty());
        assert_eq!(truth.open_orders, 0);
        assert_eq!(truth.margin_ratio, 0.0);
        assert_eq!(margin_balance, 10_000.0);
    }

    #[test]
    fn futures_reconciliation_accepts_long_short_labels_and_never_hides_exchange_only_risk() {
        let mut manager = paper_test_manager();
        manager.tracked_positions.insert(
            "BTCUSDT".to_string(),
            TrackedPosition {
                symbol: "BTCUSDT".to_string(),
                spot: Some(TrackedLegPosition {
                    side: "LONG".to_string(),
                    entry_price: 100.0,
                    quantity: 1.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
                perp: Some(TrackedLegPosition {
                    side: "SHORT".to_string(),
                    entry_price: 101.0,
                    quantity: 1.0,
                    unrealized_pnl: 0.0,
                    last_mark_price: 101.0,
                }),
            },
        );
        let positions = OrderManager::parse_futures_positions(
            r#"[{"symbol":"BTCUSDT","positionAmt":"-1"},{"symbol":"ETHUSDT","positionAmt":"0.5"}]"#,
        )
        .unwrap();
        let divergences = manager.futures_position_divergences(&positions);
        assert_eq!(divergences.len(), 1);
        assert_eq!(divergences[0].0, "ETHUSDT");
        assert_eq!(divergences[0].1, "exchange_only");
        assert!(manager.tracked_positions["BTCUSDT"].spot.is_some());

        let dual_sided = OrderManager::parse_futures_positions(
            r#"[{"symbol":"SOLUSDT","positionSide":"LONG","positionAmt":"2"},{"symbol":"SOLUSDT","positionSide":"SHORT","positionAmt":"-2"}]"#,
        )
        .unwrap_err();
        assert!(dual_sided.contains("simultaneous LONG and SHORT"));
        let inconsistent = OrderManager::parse_futures_positions(
            r#"[{"symbol":"SOLUSDT","positionSide":"SHORT","positionAmt":"2"}]"#,
        )
        .unwrap_err();
        assert!(inconsistent.contains("SHORT row has positive"));
    }

    #[test]
    fn spot_account_parsing_and_inventory_reconciliation_fail_closed() {
        assert!(OrderManager::parse_spot_balances("{}").is_err());
        assert!(
            OrderManager::parse_spot_balances(
                r#"{"balances":[{"asset":"BTC","free":"nan","locked":"0"}]}"#
            )
            .is_err()
        );
        let balances = OrderManager::parse_spot_balances(
            r#"{"balances":[{"asset":"BTC","free":"0.8","locked":"0.2"},{"asset":"USDT","free":"100","locked":"0"}]}"#,
        )
        .unwrap();
        assert_eq!(balances.get("BTC"), Some(&1.0));
        let account = OrderManager::parse_spot_account_balances(
            r#"{"balances":[{"asset":"BTC","free":"0.8","locked":"0.2"},{"asset":"USDT","free":"75","locked":"25"}]}"#,
        )
        .unwrap();
        assert_eq!(account.total.get("USDT"), Some(&100.0));
        assert_eq!(account.available.get("USDT"), Some(&75.0));

        let mut manager = paper_test_manager();
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
                    quantity: 0.9,
                    unrealized_pnl: 0.0,
                    last_mark_price: 100.0,
                }),
                perp: None,
            },
        );
        let divergences = manager.spot_inventory_divergences(&balances);
        assert_eq!(divergences.len(), 1);
        assert_eq!(divergences[0].1, "spot_quantity_mismatch");
        assert_eq!(divergences[0].2, 0.9);
        assert_eq!(divergences[0].3, 1.0);
    }

    #[test]
    fn spot_inventory_ignores_only_exchange_only_dust_below_orderable_quantity() {
        let mut manager = paper_test_manager();
        manager
            .exchange_info
            .insert("BTCUSDT".to_string(), test_exchange_symbol("BTCUSDT"));

        let dust = HashMap::from([("BTC".to_string(), 0.0009)]);
        assert!(manager.spot_inventory_divergences(&dust).is_empty());

        let actionable = HashMap::from([("BTC".to_string(), 0.001)]);
        let divergences = manager.spot_inventory_divergences(&actionable);
        assert_eq!(divergences.len(), 1);
        assert_eq!(divergences[0].0, "BTCUSDT");
        assert_eq!(divergences[0].1, "spot_exchange_only");
    }

    #[tokio::test]
    async fn unknown_bot_fill_revokes_readiness_without_guessing_position_lineage() {
        let mut manager = paper_test_manager();
        manager
            .process_ws_event(test_order_update(
                "bngs_s_orphan",
                "FILLED",
                0.25,
                0.25,
                MarketType::Spot,
                100.0,
            ))
            .await;
        assert_eq!(manager.state, SystemState::Reconciling);
        assert!(manager.tracked_positions.is_empty());
        assert_eq!(
            manager.order_cumulative_fills.get("bngs_s_orphan"),
            Some(&0.25)
        );
    }
}
