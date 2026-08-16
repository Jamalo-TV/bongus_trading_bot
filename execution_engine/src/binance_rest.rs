#![allow(dead_code)]
use crate::binance_endpoints::endpoints_for_mode;
use crate::exact_decimal::ExactDecimal;
use hmac::{Hmac, Mac};
use reqwest::{
    Client, Method, RequestBuilder,
    header::{HeaderMap, RETRY_AFTER},
};
use sha2::Sha256;
#[cfg(not(test))]
use std::sync::OnceLock;
use std::sync::{Arc as SharedArc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

use std::sync::atomic::{AtomicI64, Ordering};

#[derive(Debug, Clone)]
pub struct BinanceRest {
    pub client: Client,
    api_key: String,
    secret_key: String,
    spot_api_key: String,
    spot_secret_key: String,
    pub fut_base_url: String,
    pub spot_base_url: String,
    pub time_offset: std::sync::Arc<AtomicI64>,
    last_clock_sync_at_ms: std::sync::Arc<AtomicI64>,
    last_clock_sync_rtt_ms: std::sync::Arc<AtomicI64>,
    pub trading_mode: String,
    rate_limit_telemetry: ExchangeRateLimitTelemetry,
}

const RATE_LIMIT_WINDOW_MS: i64 = 60_000;
const ORDER_COUNT_TEN_SECOND_WINDOW_MS: i64 = 10_000;
const RATE_LIMIT_FRESHNESS_MS: i64 = 30_000;
const DEFAULT_RATE_LIMIT_RETRY_MS: i64 = 60_000;
const DEFAULT_IP_BAN_RETRY_MS: i64 = 300_000;
const AMBIGUOUS_SERVER_HOLD_MS: i64 = 60_000;
const CLOCK_SYNC_FRESHNESS_MS: i64 = 5 * 60_000;
pub const CLOCK_WARN_OFFSET_MS: i64 = 100;
pub const CLOCK_BLOCK_OFFSET_MS: i64 = 250;
pub const NONESSENTIAL_SHED_UTILIZATION_BPS: u64 = 7_000;
pub const ENTRY_BLOCK_UTILIZATION_BPS: u64 = 8_500;

#[derive(Debug, Clone, Default)]
struct VenueRateLimitState {
    limit: u64,
    used: u64,
    observed_at_ms: i64,
    order_limit_10s: u64,
    order_used_10s: u64,
    order_observed_at_10s_ms: i64,
    order_limit_1m: u64,
    order_used_1m: u64,
    order_observed_at_1m_ms: i64,
    blocked_until_ms: i64,
    ambiguous_until_ms: i64,
    last_failure_class: Option<String>,
}

#[derive(Debug, Clone, Default)]
struct ExchangeRateLimitState {
    spot: VenueRateLimitState,
    futures: VenueRateLimitState,
}

#[derive(Debug, Clone, Default)]
struct ExchangeRateLimitTelemetry {
    inner: SharedArc<Mutex<ExchangeRateLimitState>>,
}

#[derive(Debug, Clone, Default)]
struct SharedRuntimeTelemetry {
    time_offset: std::sync::Arc<AtomicI64>,
    last_clock_sync_at_ms: std::sync::Arc<AtomicI64>,
    last_clock_sync_rtt_ms: std::sync::Arc<AtomicI64>,
    rate_limits: ExchangeRateLimitTelemetry,
}

#[derive(Debug, Clone, serde::Serialize, PartialEq, Eq)]
pub struct ExchangeRateLimitSnapshot {
    pub status: String,
    pub reason: String,
    pub spot_limit_weight: u64,
    pub spot_used_weight: u64,
    pub spot_remaining_weight: u64,
    pub spot_observed_at_ms: i64,
    pub futures_limit_weight: u64,
    pub futures_used_weight: u64,
    pub futures_remaining_weight: u64,
    pub futures_observed_at_ms: i64,
    pub combined_remaining_weight: u64,
    pub spot_order_limit_10s: u64,
    pub spot_order_used_10s: u64,
    pub spot_order_remaining_10s: u64,
    pub spot_order_limit_1m: u64,
    pub spot_order_used_1m: u64,
    pub spot_order_remaining_1m: u64,
    pub futures_order_limit_10s: u64,
    pub futures_order_used_10s: u64,
    pub futures_order_remaining_10s: u64,
    pub futures_order_limit_1m: u64,
    pub futures_order_used_1m: u64,
    pub futures_order_remaining_1m: u64,
    pub max_utilization_bps: u64,
    pub nonessential_allowed: bool,
    pub entry_allowed: bool,
    pub critical_allowed: bool,
    pub reserved_request_weight: u64,
    pub reserved_order_count: u64,
    pub ambiguous_until_ms: i64,
    pub last_failure_class: Option<String>,
    pub blocked_until_ms: i64,
    pub event_time_ms: i64,
}

#[derive(Debug, Clone, serde::Serialize, PartialEq, Eq)]
pub struct ExchangeClockHealth {
    pub status: String,
    pub reason: String,
    pub synchronized: bool,
    pub warning: bool,
    pub entry_allowed: bool,
    pub offset_ms: i64,
    pub round_trip_ms: i64,
    pub observed_at_ms: i64,
    pub event_time_ms: i64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RestWorkClass {
    Nonessential,
    Entry,
    Critical,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
struct RateLimitDefinitions {
    request_weight_1m: u64,
    orders_10s: u64,
    orders_1m: u64,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ExchangeSymbolInfo {
    pub symbol: String,
    pub spot_tick_size: ExactDecimal,
    pub spot_min_price: ExactDecimal,
    pub spot_max_price: ExactDecimal,
    pub spot_min_qty: ExactDecimal,
    pub spot_step_size: ExactDecimal,
    pub spot_max_qty: ExactDecimal,
    pub spot_market_min_qty: ExactDecimal,
    pub spot_market_step_size: ExactDecimal,
    pub spot_market_max_qty: ExactDecimal,
    pub spot_min_notional: ExactDecimal,
    pub spot_max_notional: Option<ExactDecimal>,
    pub spot_min_notional_apply_to_market: bool,
    pub spot_max_notional_apply_to_market: bool,
    pub futures_tick_size: ExactDecimal,
    pub futures_min_price: ExactDecimal,
    pub futures_max_price: ExactDecimal,
    pub futures_min_qty: ExactDecimal,
    pub futures_step_size: ExactDecimal,
    pub futures_max_qty: ExactDecimal,
    pub futures_market_min_qty: ExactDecimal,
    pub futures_market_step_size: ExactDecimal,
    pub futures_market_max_qty: ExactDecimal,
    pub futures_min_notional: ExactDecimal,
    pub futures_max_notional: Option<ExactDecimal>,
    pub futures_min_notional_apply_to_market: bool,
    pub futures_max_notional_apply_to_market: bool,
}

#[derive(Debug, Clone, Copy, PartialEq)]
struct ParsedSymbolFilters {
    tick_size: ExactDecimal,
    min_price: ExactDecimal,
    max_price: ExactDecimal,
    min_qty: ExactDecimal,
    step_size: ExactDecimal,
    max_qty: ExactDecimal,
    market_min_qty: ExactDecimal,
    market_step_size: ExactDecimal,
    market_max_qty: ExactDecimal,
    min_notional: ExactDecimal,
    max_notional: Option<ExactDecimal>,
    min_notional_apply_to_market: bool,
    max_notional_apply_to_market: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
pub enum TradeSide {
    Buy,
    Sell,
}

impl TradeSide {
    fn as_str(&self) -> &'static str {
        match self {
            TradeSide::Buy => "BUY",
            TradeSide::Sell => "SELL",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub enum LegVenue {
    Spot,
    UsdtFutures,
}

impl ExchangeRateLimitTelemetry {
    fn lock_state(&self) -> std::sync::MutexGuard<'_, ExchangeRateLimitState> {
        self.inner
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    fn venue_state_mut(
        state: &mut ExchangeRateLimitState,
        venue: LegVenue,
    ) -> &mut VenueRateLimitState {
        match venue {
            LegVenue::Spot => &mut state.spot,
            LegVenue::UsdtFutures => &mut state.futures,
        }
    }

    fn set_limit(&self, venue: LegVenue, limit: u64) {
        if limit == 0 {
            return;
        }
        let mut state = self.lock_state();
        Self::venue_state_mut(&mut state, venue).limit = limit;
    }

    fn set_limits(&self, venue: LegVenue, limits: RateLimitDefinitions) {
        let mut state = self.lock_state();
        let venue_state = Self::venue_state_mut(&mut state, venue);
        if limits.request_weight_1m > 0 {
            venue_state.limit = limits.request_weight_1m;
        }
        if limits.orders_10s > 0 {
            venue_state.order_limit_10s = limits.orders_10s;
        }
        if limits.orders_1m > 0 {
            venue_state.order_limit_1m = limits.orders_1m;
        }
    }

    fn parse_used_weight(headers: &HeaderMap) -> Option<u64> {
        [
            "x-mbx-used-weight-1m",
            "x-sapi-used-ip-weight-1m",
            "x-mbx-used-weight",
        ]
        .into_iter()
        .find_map(|name| {
            headers
                .get(name)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.trim().parse::<u64>().ok())
        })
    }

    fn parse_counter(headers: &HeaderMap, names: &[&str]) -> Option<u64> {
        names.iter().find_map(|name| {
            headers
                .get(*name)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.trim().parse::<u64>().ok())
        })
    }

    fn update_monotonic_window(
        used: &mut u64,
        observed_at_ms: &mut i64,
        observed_used: u64,
        now_ms: i64,
        window_ms: i64,
    ) {
        if *observed_at_ms <= 0
            || now_ms.saturating_sub(*observed_at_ms) >= window_ms
            || observed_used >= *used
        {
            *used = observed_used;
            *observed_at_ms = now_ms;
        }
    }

    fn retry_after_ms(headers: &HeaderMap, status: u16) -> i64 {
        let default_ms = if status == 418 {
            DEFAULT_IP_BAN_RETRY_MS
        } else {
            DEFAULT_RATE_LIMIT_RETRY_MS
        };
        headers
            .get(RETRY_AFTER)
            .and_then(|value| value.to_str().ok())
            .and_then(|value| value.trim().parse::<f64>().ok())
            .filter(|seconds| seconds.is_finite() && *seconds >= 0.0)
            .map(|seconds| (seconds * 1_000.0).ceil() as i64)
            .unwrap_or(default_ms)
    }

    fn record_response(
        &self,
        venue: LegVenue,
        headers: &HeaderMap,
        status: u16,
        observed_at_ms: i64,
    ) {
        let mut state = self.lock_state();
        let venue_state = Self::venue_state_mut(&mut state, venue);
        if let Some(used) = Self::parse_used_weight(headers) {
            // Concurrent responses can complete out of request order. Never
            // accept a lower counter inside one local minute, because that
            // would manufacture capacity. A genuine exchange-window reset is
            // accepted only after the prior observation ages out.
            if venue_state.observed_at_ms <= 0
                || observed_at_ms.saturating_sub(venue_state.observed_at_ms) >= RATE_LIMIT_WINDOW_MS
                || used >= venue_state.used
            {
                venue_state.used = used;
                venue_state.observed_at_ms = observed_at_ms;
            }
        }
        if let Some(used) = Self::parse_counter(headers, &["x-mbx-order-count-10s"]) {
            Self::update_monotonic_window(
                &mut venue_state.order_used_10s,
                &mut venue_state.order_observed_at_10s_ms,
                used,
                observed_at_ms,
                ORDER_COUNT_TEN_SECOND_WINDOW_MS,
            );
        }
        if let Some(used) =
            Self::parse_counter(headers, &["x-mbx-order-count-1m", "x-mbx-order-count-1min"])
        {
            Self::update_monotonic_window(
                &mut venue_state.order_used_1m,
                &mut venue_state.order_observed_at_1m_ms,
                used,
                observed_at_ms,
                RATE_LIMIT_WINDOW_MS,
            );
        }
        if matches!(status, 418 | 429) {
            venue_state.blocked_until_ms = venue_state
                .blocked_until_ms
                .max(observed_at_ms.saturating_add(Self::retry_after_ms(headers, status)));
        }
    }

    fn record_failure_class(&self, venue: LegVenue, class: ApiFailureClass, observed_at_ms: i64) {
        let mut state = self.lock_state();
        let venue_state = Self::venue_state_mut(&mut state, venue);
        venue_state.last_failure_class = Some(class.as_str().to_string());
        if class == ApiFailureClass::AmbiguousServerResult {
            venue_state.ambiguous_until_ms = venue_state
                .ambiguous_until_ms
                .max(observed_at_ms.saturating_add(AMBIGUOUS_SERVER_HOLD_MS));
        }
    }

    fn utilization_bps(used: u64, limit: u64) -> u64 {
        if limit == 0 {
            return 0;
        }
        used.saturating_mul(10_000)
            .saturating_add(limit.saturating_sub(1))
            / limit
    }

    fn venue_max_utilization_bps(state: &VenueRateLimitState) -> u64 {
        [
            Self::utilization_bps(state.used, state.limit),
            Self::utilization_bps(state.order_used_10s, state.order_limit_10s),
            Self::utilization_bps(state.order_used_1m, state.order_limit_1m),
        ]
        .into_iter()
        .max()
        .unwrap_or(0)
    }

    fn reserve_at_entry_threshold(limit: u64) -> u64 {
        if limit == 0 {
            return 0;
        }
        let entry_budget = limit.saturating_mul(ENTRY_BLOCK_UTILIZATION_BPS) / 10_000;
        limit.saturating_sub(entry_budget)
    }

    fn effective_window_used(
        used: u64,
        observed_at_ms: i64,
        event_time_ms: i64,
        window_ms: i64,
    ) -> u64 {
        if observed_at_ms > 0
            && event_time_ms >= observed_at_ms
            && event_time_ms.saturating_sub(observed_at_ms) >= window_ms
        {
            0
        } else {
            used
        }
    }

    fn snapshot(&self, event_time_ms: i64) -> ExchangeRateLimitSnapshot {
        let state = self.lock_state().clone();
        let effective = |venue: &VenueRateLimitState| VenueRateLimitState {
            used: Self::effective_window_used(
                venue.used,
                venue.observed_at_ms,
                event_time_ms,
                RATE_LIMIT_WINDOW_MS,
            ),
            order_used_10s: Self::effective_window_used(
                venue.order_used_10s,
                venue.order_observed_at_10s_ms,
                event_time_ms,
                ORDER_COUNT_TEN_SECOND_WINDOW_MS,
            ),
            order_used_1m: Self::effective_window_used(
                venue.order_used_1m,
                venue.order_observed_at_1m_ms,
                event_time_ms,
                RATE_LIMIT_WINDOW_MS,
            ),
            ..venue.clone()
        };
        let effective_spot = effective(&state.spot);
        let effective_futures = effective(&state.futures);
        let spot_remaining = effective_spot.limit.saturating_sub(effective_spot.used);
        let futures_remaining = effective_futures
            .limit
            .saturating_sub(effective_futures.used);
        let blocked_until_ms = state
            .spot
            .blocked_until_ms
            .max(state.futures.blocked_until_ms);
        let ambiguous_until_ms = state
            .spot
            .ambiguous_until_ms
            .max(state.futures.ambiguous_until_ms);
        let last_failure_class = [
            state.spot.last_failure_class.clone(),
            state.futures.last_failure_class.clone(),
        ]
        .into_iter()
        .flatten()
        .last();
        let max_utilization_bps = Self::venue_max_utilization_bps(&effective_spot)
            .max(Self::venue_max_utilization_bps(&effective_futures));
        let limits_known = state.spot.limit > 0 && state.futures.limit > 0;
        let observations_known = state.spot.observed_at_ms > 0 && state.futures.observed_at_ms > 0;
        let observations_fresh = observations_known
            && event_time_ms >= state.spot.observed_at_ms
            && event_time_ms >= state.futures.observed_at_ms
            && event_time_ms.saturating_sub(state.spot.observed_at_ms) <= RATE_LIMIT_FRESHNESS_MS
            && event_time_ms.saturating_sub(state.futures.observed_at_ms)
                <= RATE_LIMIT_FRESHNESS_MS;
        let blocked = blocked_until_ms > event_time_ms;
        let ambiguous = ambiguous_until_ms > event_time_ms;
        let nonessential_allowed = !blocked
            && !ambiguous
            && limits_known
            && observations_fresh
            && max_utilization_bps < NONESSENTIAL_SHED_UTILIZATION_BPS;
        let entry_allowed = !blocked
            && !ambiguous
            && limits_known
            && observations_fresh
            && max_utilization_bps < ENTRY_BLOCK_UTILIZATION_BPS;
        let critical_allowed = !blocked && max_utilization_bps < 10_000;
        let (status, reason) = if blocked {
            ("BLOCKED", "exchange_retry_after_active")
        } else if ambiguous {
            ("ENTRY_BLOCKED", "ambiguous_exchange_result_reserve_active")
        } else if !limits_known {
            ("STALE", "exchange_rate_limit_limits_unknown")
        } else if !observations_known {
            ("STALE", "exchange_rate_limit_headers_missing")
        } else if !observations_fresh {
            ("STALE", "exchange_rate_limit_observation_stale")
        } else if max_utilization_bps >= ENTRY_BLOCK_UTILIZATION_BPS {
            ("ENTRY_BLOCKED", "emergency_quota_reserve_active")
        } else if max_utilization_bps >= NONESSENTIAL_SHED_UTILIZATION_BPS {
            ("SHEDDING", "nonessential_work_shed")
        } else {
            ("READY", "authoritative_exchange_headers")
        };
        let spot_order_remaining_10s = effective_spot
            .order_limit_10s
            .saturating_sub(effective_spot.order_used_10s);
        let spot_order_remaining_1m = effective_spot
            .order_limit_1m
            .saturating_sub(effective_spot.order_used_1m);
        let futures_order_remaining_10s = effective_futures
            .order_limit_10s
            .saturating_sub(effective_futures.order_used_10s);
        let futures_order_remaining_1m = effective_futures
            .order_limit_1m
            .saturating_sub(effective_futures.order_used_1m);
        let reserved_request_weight = Self::reserve_at_entry_threshold(state.spot.limit)
            .min(Self::reserve_at_entry_threshold(state.futures.limit));
        let reserved_order_count = [
            Self::reserve_at_entry_threshold(state.spot.order_limit_10s),
            Self::reserve_at_entry_threshold(state.spot.order_limit_1m),
            Self::reserve_at_entry_threshold(state.futures.order_limit_10s),
            Self::reserve_at_entry_threshold(state.futures.order_limit_1m),
        ]
        .into_iter()
        .filter(|reserve| *reserve > 0)
        .min()
        .unwrap_or(0);
        ExchangeRateLimitSnapshot {
            status: status.to_string(),
            reason: reason.to_string(),
            spot_limit_weight: state.spot.limit,
            spot_used_weight: effective_spot.used,
            spot_remaining_weight: spot_remaining,
            spot_observed_at_ms: state.spot.observed_at_ms,
            futures_limit_weight: state.futures.limit,
            futures_used_weight: effective_futures.used,
            futures_remaining_weight: futures_remaining,
            futures_observed_at_ms: state.futures.observed_at_ms,
            combined_remaining_weight: spot_remaining.min(futures_remaining),
            spot_order_limit_10s: state.spot.order_limit_10s,
            spot_order_used_10s: effective_spot.order_used_10s,
            spot_order_remaining_10s,
            spot_order_limit_1m: state.spot.order_limit_1m,
            spot_order_used_1m: effective_spot.order_used_1m,
            spot_order_remaining_1m,
            futures_order_limit_10s: state.futures.order_limit_10s,
            futures_order_used_10s: effective_futures.order_used_10s,
            futures_order_remaining_10s,
            futures_order_limit_1m: state.futures.order_limit_1m,
            futures_order_used_1m: effective_futures.order_used_1m,
            futures_order_remaining_1m,
            max_utilization_bps,
            nonessential_allowed,
            entry_allowed,
            critical_allowed,
            reserved_request_weight,
            reserved_order_count,
            ambiguous_until_ms,
            last_failure_class,
            blocked_until_ms,
            event_time_ms,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReconciledSubmission {
    pub body: String,
    pub recovered_after_ambiguous_submit: bool,
    pub retried_after_negative_proof: bool,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum ApiFailureClass {
    TransportAmbiguous,
    AmbiguousTimeout,
    ClockSkew,
    Authentication,
    ClientRejected,
    IpBanned,
    RateLimited,
    AmbiguousServerResult,
    ServerTransient,
    UnexpectedStatus,
}

impl ApiFailureClass {
    fn as_str(self) -> &'static str {
        match self {
            Self::TransportAmbiguous => "transport_ambiguous",
            Self::AmbiguousTimeout => "ambiguous_timeout",
            Self::ClockSkew => "clock_skew",
            Self::Authentication => "authentication",
            Self::ClientRejected => "client_rejected",
            Self::IpBanned => "ip_banned",
            Self::RateLimited => "rate_limited",
            Self::AmbiguousServerResult => "ambiguous_server_result",
            Self::ServerTransient => "server_transient",
            Self::UnexpectedStatus => "unexpected_status",
        }
    }
}

/// Diagnostic process boundary for the accepted-timeout campaign. The caller
/// supplies a local exchange-shaped HTTP endpoint; no Binance host or account
/// is contacted. Every production placement path must recover the accepted
/// POST through an authoritative same-client-ID GET without a second POST.
pub async fn run_rest_timeout_harness(base_url: &str) -> Result<(), String> {
    let mut rest = BinanceRest::new(
        "diagnostic-key".to_string(),
        "diagnostic-secret".to_string(),
        "live".to_string(),
    );
    rest.spot_base_url = base_url.to_string();
    rest.fut_base_url = base_url.to_string();
    rest.client = Client::builder()
        .connect_timeout(Duration::from_millis(50))
        .timeout(Duration::from_millis(75))
        .build()
        .map_err(|error| format!("build_client:{error}"))?;

    let mut outcomes = Vec::new();
    macro_rules! record {
        ($name:expr, $future:expr) => {{
            let receipt = $future.await.map_err(|error| format!("{}:{error}", $name))?;
            outcomes.push(serde_json::json!({
                "name": $name,
                "recovered_after_ambiguous_submit": receipt.recovered_after_ambiguous_submit,
                "retried_after_negative_proof": receipt.retried_after_negative_proof,
            }));
        }};
    }

    record!(
        "spot_limit",
        rest.place_spot_limit_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Buy,
            "0.01",
            "60000",
            "bngs_timeout_spot_limit",
        )
    );
    record!(
        "futures_limit_entry",
        rest.place_futures_limit_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Sell,
            "0.01",
            "60000",
            "bngs_timeout_futures_limit_entry",
            false,
        )
    );
    record!(
        "futures_limit_exit",
        rest.place_futures_limit_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Buy,
            "0.01",
            "60000",
            "bngs_timeout_futures_limit_exit",
            true,
        )
    );
    record!(
        "spot_market",
        rest.place_spot_market_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Sell,
            "0.01",
            "bngs_timeout_spot_market",
        )
    );
    record!(
        "futures_market_entry",
        rest.place_futures_market_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Sell,
            "0.01",
            "bngs_timeout_futures_market_entry",
            false,
        )
    );
    record!(
        "futures_market_exit",
        rest.place_futures_market_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Buy,
            "0.01",
            "bngs_timeout_futures_market_exit",
            true,
        )
    );

    println!(
        "{}",
        serde_json::to_string(&serde_json::json!({
            "schema_version": 1,
            "outcomes": outcomes,
        }))
        .map_err(|error| format!("encode_json:{error}"))?
    );
    Ok(())
}

/// Fetch a sequence of paired spot/perpetual exchange-info snapshots from a
/// local exchange emulator through the production REST/parser path.
pub async fn run_metadata_change_harness(base_url: &str) -> Result<(), String> {
    let mut rest = BinanceRest::new(
        "diagnostic-key".to_string(),
        "diagnostic-secret".to_string(),
        "live".to_string(),
    );
    rest.spot_base_url = base_url.to_string();
    rest.fut_base_url = base_url.to_string();
    rest.client = Client::builder()
        .connect_timeout(Duration::from_millis(250))
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|error| format!("build_client:{error}"))?;
    // This process-boundary harness points only at a local emulator and tests
    // metadata transitions, not quota discovery. Seed a synthetic, fresh
    // budget so the production 70% nonessential-work gate does not mask the
    // six intended exchangeInfo stages.
    let diagnostic_limits = RateLimitDefinitions {
        request_weight_1m: 10_000,
        orders_10s: 1_000,
        orders_1m: 10_000,
    };
    let observed_at_ms = BinanceRest::current_time_ms();
    for venue in [LegVenue::Spot, LegVenue::UsdtFutures] {
        rest.rate_limit_telemetry
            .set_limits(venue, diagnostic_limits);
        let mut headers = HeaderMap::new();
        headers.insert("x-mbx-used-weight-1m", "0".parse().expect("header"));
        rest.rate_limit_telemetry
            .record_response(venue, &headers, 200, observed_at_ms);
    }

    let mut stages = Vec::new();
    for index in 0..6 {
        let exchange_info = rest
            .get_exchange_info()
            .await
            .map_err(|error| format!("stage_{index}:{error}"))?;
        let payload = match exchange_info.get("BTCUSDT") {
            Some(info) => serde_json::json!({
                "stage": index,
                "available": true,
                "spot_tick_size": info.spot_tick_size.to_f64(),
                "spot_step_size": info.spot_step_size.to_f64(),
                "spot_market_step_size": info.spot_market_step_size.to_f64(),
                "spot_min_notional": info.spot_min_notional.to_f64(),
                "futures_tick_size": info.futures_tick_size.to_f64(),
                "futures_step_size": info.futures_step_size.to_f64(),
                "futures_market_step_size": info.futures_market_step_size.to_f64(),
                "futures_min_notional": info.futures_min_notional.to_f64(),
            }),
            None => serde_json::json!({
                "stage": index,
                "available": false,
            }),
        };
        stages.push(payload);
    }
    println!(
        "{}",
        serde_json::to_string(&serde_json::json!({
            "schema_version": 1,
            "stages": stages,
        }))
        .map_err(|error| format!("encode_json:{error}"))?
    );
    Ok(())
}

impl BinanceRest {
    fn shared_runtime_telemetry() -> SharedRuntimeTelemetry {
        #[cfg(test)]
        {
            SharedRuntimeTelemetry::default()
        }
        #[cfg(not(test))]
        {
            static SHARED: OnceLock<SharedRuntimeTelemetry> = OnceLock::new();
            SHARED.get_or_init(SharedRuntimeTelemetry::default).clone()
        }
    }

    pub fn new(api_key: String, secret_key: String, trading_mode: String) -> Self {
        let mut futures_api_key = api_key.trim().to_string();
        let mut futures_secret_key = secret_key.trim().to_string();
        let raw_spot_api_key = std::env::var("BINANCE_SPOT_API_KEY")
            .unwrap_or_default()
            .trim()
            .to_string();
        let raw_spot_secret_key = std::env::var("BINANCE_SPOT_API_SECRET")
            .unwrap_or_default()
            .trim()
            .to_string();

        if futures_api_key.is_empty() {
            futures_api_key = raw_spot_api_key.clone();
        }
        if futures_secret_key.is_empty() {
            futures_secret_key = raw_spot_secret_key.clone();
        }

        let spot_api_key = if raw_spot_api_key.is_empty() {
            futures_api_key.clone()
        } else {
            raw_spot_api_key
        };
        let spot_secret_key = if raw_spot_secret_key.is_empty() {
            futures_secret_key.clone()
        } else {
            raw_spot_secret_key
        };

        let endpoints = endpoints_for_mode(&trading_mode)
            .expect("embedded Binance endpoint matrix must be valid");
        let fut_base_url = endpoints.futures.rest_base_url;
        let spot_base_url = endpoints.spot.rest_base_url;

        // Every REST operation runs on execution-critical paths.  A bounded
        // client prevents an exchange/network stall from freezing the order
        // actor indefinitely; ambiguous order outcomes are reconciled by the
        // durable state machine instead of being blindly retried.
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(15))
            .pool_idle_timeout(Duration::from_secs(30))
            .build()
            .expect("bounded Binance HTTP client must build");
        // The order actor and the two private-stream managers create separate
        // BinanceRest handles, but Binance enforces request weight by IP and
        // order counts by account. Production handles therefore share one
        // process-wide telemetry/clock surface; unit-test instances remain
        // isolated so parallel tests cannot manufacture cross-test state.
        let shared_telemetry = Self::shared_runtime_telemetry();

        Self {
            client,
            api_key: futures_api_key,
            secret_key: futures_secret_key,
            spot_api_key,
            spot_secret_key,
            fut_base_url,
            spot_base_url,
            time_offset: shared_telemetry.time_offset,
            last_clock_sync_at_ms: shared_telemetry.last_clock_sync_at_ms,
            last_clock_sync_rtt_ms: shared_telemetry.last_clock_sync_rtt_ms,
            trading_mode,
            rate_limit_telemetry: shared_telemetry.rate_limits,
        }
    }

    fn current_time_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_millis() as i64)
            .unwrap_or(0)
    }

    fn venue_from_url(url: &reqwest::Url) -> LegVenue {
        if url.path().starts_with("/api/") || url.path().starts_with("/sapi/") {
            LegVenue::Spot
        } else {
            // Futures and portfolio-margin requests share the conservative
            // execution-side budget. Unknown Binance-shaped paths never get
            // credited to the less-used venue.
            LegVenue::UsdtFutures
        }
    }

    fn rate_limit_definitions(json: &serde_json::Value) -> RateLimitDefinitions {
        let mut definitions = RateLimitDefinitions::default();
        let Some(items) = json.get("rateLimits").and_then(serde_json::Value::as_array) else {
            return definitions;
        };
        for item in items {
            let Some(kind) = item
                .get("rateLimitType")
                .and_then(serde_json::Value::as_str)
            else {
                continue;
            };
            let Some(interval) = item.get("interval").and_then(serde_json::Value::as_str) else {
                continue;
            };
            let interval_num = item
                .get("intervalNum")
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(0);
            let limit = item
                .get("limit")
                .and_then(serde_json::Value::as_u64)
                .unwrap_or(0);
            if limit == 0 {
                continue;
            }
            match (kind, interval, interval_num) {
                ("REQUEST_WEIGHT", "MINUTE", 1) => definitions.request_weight_1m = limit,
                ("ORDERS", "SECOND", 10) => definitions.orders_10s = limit,
                ("ORDERS", "MINUTE", 1) => definitions.orders_1m = limit,
                _ => {}
            }
        }
        definitions
    }

    fn request_weight_limit(json: &serde_json::Value) -> Option<u64> {
        let limit = Self::rate_limit_definitions(json).request_weight_1m;
        (limit > 0).then_some(limit)
    }

    pub fn rate_limit_snapshot(&self) -> ExchangeRateLimitSnapshot {
        self.rate_limit_telemetry.snapshot(Self::current_time_ms())
    }

    pub fn clock_health_snapshot(&self) -> ExchangeClockHealth {
        let event_time_ms = Self::current_time_ms();
        let observed_at_ms = self.last_clock_sync_at_ms.load(Ordering::Relaxed);
        let offset_ms = self.time_offset.load(Ordering::Relaxed);
        let round_trip_ms = self.last_clock_sync_rtt_ms.load(Ordering::Relaxed);
        let age_ms = event_time_ms.saturating_sub(observed_at_ms);
        let synchronized = observed_at_ms > 0
            && event_time_ms >= observed_at_ms
            && age_ms <= CLOCK_SYNC_FRESHNESS_MS;
        // Midpoint sampling bounds the clock estimate's network uncertainty
        // by half the round trip. Treat an RTT above 500 ms as incapable of
        // proving the required 250 ms entry bound.
        let offset_abs_ms = offset_ms.saturating_abs();
        let uncertainty_safe = round_trip_ms <= CLOCK_BLOCK_OFFSET_MS.saturating_mul(2);
        let entry_allowed =
            synchronized && uncertainty_safe && offset_abs_ms <= CLOCK_BLOCK_OFFSET_MS;
        let warning = synchronized
            && (offset_abs_ms > CLOCK_WARN_OFFSET_MS
                || round_trip_ms > CLOCK_WARN_OFFSET_MS.saturating_mul(2));
        let (status, reason) = if !synchronized {
            ("UNSYNCHRONIZED", "exchange_clock_sample_missing_or_stale")
        } else if !uncertainty_safe {
            ("BLOCKED", "exchange_clock_network_uncertainty")
        } else if offset_abs_ms > CLOCK_BLOCK_OFFSET_MS {
            ("BLOCKED", "exchange_clock_offset_above_250ms")
        } else if offset_abs_ms > CLOCK_WARN_OFFSET_MS {
            ("WARNING", "exchange_clock_offset_above_100ms")
        } else if warning {
            ("WARNING", "exchange_clock_round_trip_above_200ms")
        } else {
            ("READY", "fresh_exchange_midpoint_sample")
        };
        ExchangeClockHealth {
            status: status.to_string(),
            reason: reason.to_string(),
            synchronized,
            warning,
            entry_allowed,
            offset_ms,
            round_trip_ms,
            observed_at_ms,
            event_time_ms,
        }
    }

    fn record_server_time_sample(
        &self,
        response_body: &str,
        request_started_at_ms: i64,
        request_completed_at_ms: i64,
    ) -> Result<(), String> {
        if request_started_at_ms <= 0 || request_completed_at_ms < request_started_at_ms {
            return Err("local clock moved backwards during exchange time sample".to_string());
        }
        let json: serde_json::Value =
            serde_json::from_str(response_body).map_err(|error| error.to_string())?;
        let server_time = json
            .get("serverTime")
            .and_then(serde_json::Value::as_i64)
            .filter(|value| *value > 0)
            .ok_or_else(|| {
                "futures server time response is missing a positive serverTime".to_string()
            })?;
        let round_trip_ms = request_completed_at_ms.saturating_sub(request_started_at_ms);
        let midpoint_ms = request_started_at_ms.saturating_add(round_trip_ms / 2);
        self.time_offset
            .store(server_time.saturating_sub(midpoint_ms), Ordering::Relaxed);
        self.last_clock_sync_rtt_ms
            .store(round_trip_ms, Ordering::Relaxed);
        self.last_clock_sync_at_ms
            .store(request_completed_at_ms, Ordering::Relaxed);
        Ok(())
    }

    pub fn quota_block_reason(&self, work: RestWorkClass) -> Option<&'static str> {
        if self.trading_mode == "paper" {
            return None;
        }
        let snapshot = self.rate_limit_snapshot();
        match work {
            RestWorkClass::Nonessential if !snapshot.nonessential_allowed => {
                Some("nonessential_exchange_work_shed")
            }
            RestWorkClass::Entry if snapshot.status == "STALE" => {
                Some("exchange_rate_limit_telemetry_unavailable")
            }
            RestWorkClass::Entry if snapshot.blocked_until_ms > snapshot.event_time_ms => {
                Some("exchange_rate_limit_retry_after_active")
            }
            RestWorkClass::Entry if snapshot.ambiguous_until_ms > snapshot.event_time_ms => {
                Some("ambiguous_exchange_result_reserve_active")
            }
            RestWorkClass::Entry if !snapshot.entry_allowed => {
                Some("insufficient_exchange_rate_limit_budget")
            }
            RestWorkClass::Critical if !snapshot.critical_allowed => {
                Some("exchange_retry_after_or_capacity_exhausted")
            }
            _ => None,
        }
    }

    #[cfg(test)]
    pub(crate) fn set_rate_limit_observations_for_test(
        &self,
        spot_limit: u64,
        spot_used: u64,
        futures_limit: u64,
        futures_used: u64,
        observed_at_ms: i64,
    ) {
        self.rate_limit_telemetry.set_limits(
            LegVenue::Spot,
            RateLimitDefinitions {
                request_weight_1m: spot_limit,
                orders_10s: 100,
                orders_1m: 1_200,
            },
        );
        self.rate_limit_telemetry.set_limits(
            LegVenue::UsdtFutures,
            RateLimitDefinitions {
                request_weight_1m: futures_limit,
                orders_10s: 300,
                orders_1m: 1_200,
            },
        );
        for (venue, used) in [
            (LegVenue::Spot, spot_used),
            (LegVenue::UsdtFutures, futures_used),
        ] {
            let mut headers = HeaderMap::new();
            if let Ok(value) = used.to_string().parse() {
                headers.insert("x-mbx-used-weight-1m", value);
                self.rate_limit_telemetry
                    .record_response(venue, &headers, 200, observed_at_ms);
            }
        }
    }

    #[cfg(test)]
    pub(crate) fn set_order_count_observations_for_test(
        &self,
        venue: LegVenue,
        limit_10s: u64,
        used_10s: u64,
        limit_1m: u64,
        used_1m: u64,
        observed_at_ms: i64,
    ) {
        self.rate_limit_telemetry.set_limits(
            venue,
            RateLimitDefinitions {
                request_weight_1m: 0,
                orders_10s: limit_10s,
                orders_1m: limit_1m,
            },
        );
        let mut headers = HeaderMap::new();
        headers.insert(
            "x-mbx-order-count-10s",
            used_10s.to_string().parse().expect("test header"),
        );
        headers.insert(
            "x-mbx-order-count-1m",
            used_1m.to_string().parse().expect("test header"),
        );
        self.rate_limit_telemetry
            .record_response(venue, &headers, 200, observed_at_ms);
    }

    #[cfg(test)]
    pub(crate) fn set_clock_health_for_test(
        &self,
        offset_ms: i64,
        round_trip_ms: i64,
        observed_at_ms: i64,
    ) {
        self.time_offset.store(offset_ms, Ordering::Relaxed);
        self.last_clock_sync_rtt_ms
            .store(round_trip_ms, Ordering::Relaxed);
        self.last_clock_sync_at_ms
            .store(observed_at_ms, Ordering::Relaxed);
    }

    pub async fn refresh_rate_limit_telemetry(&self) -> Result<(), String> {
        if self.trading_mode == "paper" {
            return Ok(());
        }
        if let Some(reason) = self.quota_block_reason(RestWorkClass::Critical) {
            return Err(reason.to_string());
        }
        let futures_url = format!("{}/fapi/v1/time", self.fut_base_url);
        let spot_url = format!("{}/api/v3/time", self.spot_base_url);
        let futures_probe = async {
            let started_at_ms = Self::current_time_ms();
            let result = self
                .send_checked_text(self.client.get(&futures_url), "futures rate-limit probe")
                .await;
            (result, started_at_ms, Self::current_time_ms())
        };
        let ((futures_result, request_started_at_ms, request_completed_at_ms), spot_result) = tokio::join!(
            futures_probe,
            self.send_checked_text(self.client.get(&spot_url), "spot rate-limit probe"),
        );
        let mut errors = Vec::new();
        match futures_result {
            Ok(body) => {
                if let Err(error) = self.record_server_time_sample(
                    &body,
                    request_started_at_ms,
                    request_completed_at_ms,
                ) {
                    errors.push(format!("futures clock sample invalid: {error}"));
                }
            }
            Err(error) => errors.push(error),
        }
        if let Err(error) = spot_result {
            errors.push(error);
        }
        if errors.is_empty() {
            Ok(())
        } else {
            Err(errors.join("; "))
        }
    }

    pub async fn sync_time(&self) -> Result<(), String> {
        let url = format!("{}/fapi/v1/time", self.fut_base_url);
        let request_started_at_ms = Self::current_time_ms();
        let text = self
            .send_checked_text(self.client.get(&url), "futures server time")
            .await?;
        self.record_server_time_sample(&text, request_started_at_ms, Self::current_time_ms())
    }

    pub async fn get_exchange_info(
        &self,
    ) -> Result<std::collections::HashMap<String, ExchangeSymbolInfo>, String> {
        if let Some(reason) = self.quota_block_reason(RestWorkClass::Nonessential) {
            let snapshot = self.rate_limit_snapshot();
            let bootstrap_limits = (snapshot.spot_limit_weight == 0
                || snapshot.futures_limit_weight == 0)
                && snapshot.blocked_until_ms <= snapshot.event_time_ms
                && snapshot.ambiguous_until_ms <= snapshot.event_time_ms;
            if !bootstrap_limits {
                return Err(reason.to_string());
            }
        }
        let futures_primary_url = format!("{}/fapi/v1/exchangeInfo", self.fut_base_url);
        let spot_primary_url = format!("{}/api/v3/exchangeInfo", self.spot_base_url);

        let futures_json = self
            .fetch_exchange_info_json_with_fallback(
                &futures_primary_url,
                None,
                "futures exchange info",
            )
            .await?;
        let spot_json = self
            .fetch_exchange_info_json_with_fallback(&spot_primary_url, None, "spot exchange info")
            .await?;

        self.rate_limit_telemetry.set_limits(
            LegVenue::UsdtFutures,
            Self::rate_limit_definitions(&futures_json),
        );
        self.rate_limit_telemetry
            .set_limits(LegVenue::Spot, Self::rate_limit_definitions(&spot_json));

        let futures_filters = Self::parse_symbol_filters(&futures_json);
        let spot_filters = Self::parse_symbol_filters(&spot_json);

        let mut info_map = std::collections::HashMap::new();
        // A paired trade is executable only when both venues independently
        // report complete, positive filters and TRADING status.  Never invent
        // fallback filters for a missing leg.
        for symbol in spot_filters.keys() {
            let Some(futures) = futures_filters.get(symbol).copied() else {
                continue;
            };
            let Some(spot) = spot_filters.get(symbol).copied() else {
                continue;
            };
            info_map.insert(
                symbol.clone(),
                ExchangeSymbolInfo {
                    symbol: symbol.clone(),
                    spot_tick_size: spot.tick_size,
                    spot_min_price: spot.min_price,
                    spot_max_price: spot.max_price,
                    spot_min_qty: spot.min_qty,
                    spot_step_size: spot.step_size,
                    spot_max_qty: spot.max_qty,
                    spot_market_min_qty: spot.market_min_qty,
                    spot_market_step_size: spot.market_step_size,
                    spot_market_max_qty: spot.market_max_qty,
                    spot_min_notional: spot.min_notional,
                    spot_max_notional: spot.max_notional,
                    spot_min_notional_apply_to_market: spot.min_notional_apply_to_market,
                    spot_max_notional_apply_to_market: spot.max_notional_apply_to_market,
                    futures_tick_size: futures.tick_size,
                    futures_min_price: futures.min_price,
                    futures_max_price: futures.max_price,
                    futures_min_qty: futures.min_qty,
                    futures_step_size: futures.step_size,
                    futures_max_qty: futures.max_qty,
                    futures_market_min_qty: futures.market_min_qty,
                    futures_market_step_size: futures.market_step_size,
                    futures_market_max_qty: futures.market_max_qty,
                    futures_min_notional: futures.min_notional,
                    futures_max_notional: futures.max_notional,
                    futures_min_notional_apply_to_market: futures.min_notional_apply_to_market,
                    futures_max_notional_apply_to_market: futures.max_notional_apply_to_market,
                },
            );
        }

        Ok(info_map)
    }

    async fn fetch_exchange_info_json_with_fallback(
        &self,
        primary_url: &str,
        fallback_url: Option<&str>,
        label: &str,
    ) -> Result<serde_json::Value, String> {
        match self.fetch_exchange_info_json(primary_url, label).await {
            Ok(json) => Ok(json),
            Err(primary_err) => {
                let Some(fallback_url) = fallback_url else {
                    return Err(primary_err);
                };
                if fallback_url == primary_url {
                    return Err(primary_err);
                }

                tracing::warn!(
                    "Primary {} endpoint failed in {} mode: {}. Falling back to {}",
                    label,
                    self.trading_mode,
                    primary_err,
                    fallback_url,
                );
                self.fetch_exchange_info_json(fallback_url, label).await
            }
        }
    }

    async fn fetch_exchange_info_json(
        &self,
        url: &str,
        label: &str,
    ) -> Result<serde_json::Value, String> {
        let response = self
            .client
            .get(url)
            .send()
            .await
            .map_err(|e| format!("Failed to fetch {} from {}: {}", label, url, e))?;
        let status = response.status();
        let venue = Self::venue_from_url(response.url());
        self.rate_limit_telemetry.record_response(
            venue,
            response.headers(),
            status.as_u16(),
            Self::current_time_ms(),
        );
        let text = response
            .text()
            .await
            .map_err(|e| format!("Failed to read {} text from {}: {}", label, url, e))?;

        if !status.is_success() {
            return Err(format!(
                "{} from {} returned HTTP {} ({})",
                label,
                url,
                status.as_u16(),
                Self::preview_body(&text),
            ));
        }

        serde_json::from_str(&text).map_err(|e| {
            format!(
                "Failed to parse {} JSON from {}: {} (body starts with: {})",
                label,
                url,
                e,
                Self::preview_body(&text),
            )
        })
    }

    fn preview_body(text: &str) -> String {
        let mut preview = text.replace('\n', " ");
        if preview.len() > 120 {
            preview.truncate(120);
        }
        preview
    }

    fn exchange_error(text: &str) -> Option<String> {
        let json = serde_json::from_str::<serde_json::Value>(text).ok()?;
        let code = json.get("code").and_then(|value| value.as_i64())?;
        if code >= 0 {
            return None;
        }
        let msg = json
            .get("msg")
            .and_then(|value| value.as_str())
            .unwrap_or("unknown exchange error");
        Some(format!("exchange error {} ({})", code, msg))
    }

    fn exchange_error_code(text: &str) -> Option<i64> {
        serde_json::from_str::<serde_json::Value>(text)
            .ok()?
            .get("code")?
            .as_i64()
            .filter(|code| *code < 0)
    }

    fn classify_api_failure(status: u16, text: &str) -> ApiFailureClass {
        match Self::exchange_error_code(text) {
            Some(-1007) => return ApiFailureClass::AmbiguousTimeout,
            Some(-1021) => return ApiFailureClass::ClockSkew,
            _ => {}
        }
        match status {
            401 | 403 => ApiFailureClass::Authentication,
            418 => ApiFailureClass::IpBanned,
            429 => ApiFailureClass::RateLimited,
            503 => ApiFailureClass::AmbiguousServerResult,
            500..=599 => ApiFailureClass::ServerTransient,
            400..=499 => ApiFailureClass::ClientRejected,
            _ => ApiFailureClass::UnexpectedStatus,
        }
    }

    fn safe_transport_error(err: &reqwest::Error) -> &'static str {
        if err.is_timeout() {
            "request timed out"
        } else if err.is_connect() {
            "connection failed"
        } else if err.is_body() {
            "response body failed"
        } else if err.is_decode() {
            "response decode failed"
        } else if err.is_request() {
            "request construction or transport failed"
        } else {
            "HTTP transport failed"
        }
    }

    async fn send_checked_text(
        &self,
        req: RequestBuilder,
        context: &str,
    ) -> Result<String, String> {
        let response = req.send().await.map_err(|err| {
            format!(
                "{} class={} {}",
                context,
                ApiFailureClass::TransportAmbiguous.as_str(),
                Self::safe_transport_error(&err)
            )
        })?;
        let status = response.status();
        let venue = Self::venue_from_url(response.url());
        self.rate_limit_telemetry.record_response(
            venue,
            response.headers(),
            status.as_u16(),
            Self::current_time_ms(),
        );
        let retry_after = response
            .headers()
            .get(RETRY_AFTER)
            .and_then(|value| value.to_str().ok())
            .map(str::to_string);
        let text = response.text().await.map_err(|err| {
            format!(
                "{} class={} {}",
                context,
                ApiFailureClass::TransportAmbiguous.as_str(),
                Self::safe_transport_error(&err)
            )
        })?;

        if !status.is_success() {
            let details = Self::exchange_error(&text).unwrap_or_else(|| Self::preview_body(&text));
            let class = Self::classify_api_failure(status.as_u16(), &text);
            self.rate_limit_telemetry
                .record_failure_class(venue, class, Self::current_time_ms());
            let retry_hint = retry_after
                .as_deref()
                .map(|value| format!(", retry_after={value}"))
                .unwrap_or_default();
            return Err(format!(
                "{} returned HTTP {} (class={}{}; {})",
                context,
                status.as_u16(),
                class.as_str(),
                retry_hint,
                details,
            ));
        }

        if let Some(details) = Self::exchange_error(&text) {
            let class = Self::classify_api_failure(status.as_u16(), &text);
            self.rate_limit_telemetry
                .record_failure_class(venue, class, Self::current_time_ms());
            return Err(format!("{} class={} {}", context, class.as_str(), details));
        }

        Ok(text)
    }

    fn parse_symbol_filters(
        json: &serde_json::Value,
    ) -> std::collections::HashMap<String, ParsedSymbolFilters> {
        let mut parsed = std::collections::HashMap::new();
        if let Some(symbols) = json.get("symbols").and_then(|s| s.as_array()) {
            for sym in symbols {
                let symbol = sym
                    .get("symbol")
                    .and_then(|s| s.as_str())
                    .unwrap_or("")
                    .to_string();
                if symbol.is_empty() {
                    continue;
                }

                if sym.get("status").and_then(|value| value.as_str()) != Some("TRADING") {
                    continue;
                }

                let mut tick_size: Option<ExactDecimal> = None;
                let mut min_price = ExactDecimal::ZERO;
                let mut max_price = ExactDecimal::MAX;
                let mut min_qty: Option<ExactDecimal> = None;
                let mut step_size: Option<ExactDecimal> = None;
                let mut max_qty: Option<ExactDecimal> = None;
                let mut market_min_qty: Option<ExactDecimal> = None;
                let mut market_step_size: Option<ExactDecimal> = None;
                let mut market_max_qty: Option<ExactDecimal> = None;
                let mut min_notional: Option<ExactDecimal> = None;
                let mut max_notional: Option<ExactDecimal> = None;
                let mut min_notional_apply_to_market = true;
                let mut max_notional_apply_to_market = true;
                let mut saw_price_filter = false;
                let mut saw_lot_size_filter = false;
                let mut saw_market_lot_size_filter = false;
                let mut saw_notional_filter = false;
                let mut filters_valid = true;

                if let Some(filters) = sym.get("filters").and_then(|f| f.as_array()) {
                    for filter in filters {
                        let Some(filter_type) =
                            filter.get("filterType").and_then(|value| value.as_str())
                        else {
                            filters_valid = false;
                            break;
                        };
                        let decimal_field = |key: &str| -> Result<Option<ExactDecimal>, ()> {
                            let Some(node) = filter.get(key) else {
                                return Ok(None);
                            };
                            let raw = node.as_str().ok_or(())?;
                            let value = raw.parse::<ExactDecimal>().map_err(|_| ())?;
                            (value >= ExactDecimal::ZERO)
                                .then_some(Some(value))
                                .ok_or(())
                        };
                        let bool_field = |key: &str, default: bool| -> Result<bool, ()> {
                            match filter.get(key) {
                                None => Ok(default),
                                Some(node) => node.as_bool().ok_or(()),
                            }
                        };

                        let result = match filter_type {
                            "PRICE_FILTER" => (|| {
                                if saw_price_filter {
                                    return Err(());
                                }
                                saw_price_filter = true;
                                tick_size = decimal_field("tickSize")?.filter(|v| v.is_positive());
                                if tick_size.is_none() {
                                    return Err(());
                                }
                                min_price = decimal_field("minPrice")?
                                    .filter(|v| v.is_positive())
                                    .unwrap_or(ExactDecimal::ZERO);
                                max_price = decimal_field("maxPrice")?
                                    .filter(|v| v.is_positive())
                                    .unwrap_or(ExactDecimal::MAX);
                                Ok(())
                            })(),
                            "LOT_SIZE" => (|| {
                                if saw_lot_size_filter {
                                    return Err(());
                                }
                                saw_lot_size_filter = true;
                                min_qty = decimal_field("minQty")?.filter(|v| v.is_positive());
                                step_size = decimal_field("stepSize")?.filter(|v| v.is_positive());
                                max_qty = decimal_field("maxQty")?.filter(|v| v.is_positive());
                                (min_qty.is_some() && step_size.is_some() && max_qty.is_some())
                                    .then_some(())
                                    .ok_or(())
                            })(),
                            "MARKET_LOT_SIZE" => (|| {
                                if saw_market_lot_size_filter {
                                    return Err(());
                                }
                                saw_market_lot_size_filter = true;
                                market_min_qty =
                                    decimal_field("minQty")?.filter(|v| v.is_positive());
                                market_step_size =
                                    decimal_field("stepSize")?.filter(|v| v.is_positive());
                                market_max_qty =
                                    decimal_field("maxQty")?.filter(|v| v.is_positive());
                                Ok(())
                            })(),
                            "NOTIONAL" | "MIN_NOTIONAL" => (|| {
                                if saw_notional_filter {
                                    return Err(());
                                }
                                saw_notional_filter = true;
                                min_notional = filter
                                    .get("minNotional")
                                    .or_else(|| filter.get("notional"))
                                    .and_then(|node| node.as_str())
                                    .and_then(|raw| raw.parse::<ExactDecimal>().ok())
                                    .filter(|value| value.is_positive());
                                if min_notional.is_none() {
                                    return Err(());
                                }
                                if filter_type == "NOTIONAL" {
                                    max_notional =
                                        decimal_field("maxNotional")?.filter(|v| v.is_positive());
                                    min_notional_apply_to_market =
                                        bool_field("applyMinToMarket", true)?;
                                    max_notional_apply_to_market =
                                        bool_field("applyMaxToMarket", true)?;
                                } else {
                                    min_notional_apply_to_market =
                                        bool_field("applyToMarket", true)?;
                                    max_notional_apply_to_market = false;
                                }
                                Ok(())
                            })(),
                            _ => Ok(()),
                        };
                        if result.is_err() {
                            filters_valid = false;
                            break;
                        }
                    }
                } else {
                    filters_valid = false;
                }
                if !filters_valid {
                    continue;
                }

                let Some(tick_size) = tick_size else {
                    continue;
                };
                let Some(step_size) = step_size else {
                    continue;
                };
                let Some(min_qty) = min_qty else {
                    continue;
                };
                let Some(max_qty) = max_qty else {
                    continue;
                };
                let Some(min_notional) = min_notional else {
                    continue;
                };
                let market_min_qty = market_min_qty.unwrap_or(min_qty).max(min_qty);
                let Some(market_step_size) = market_step_size
                    .map(|market| step_size.checked_common_increment(market))
                    .unwrap_or(Some(step_size))
                else {
                    continue;
                };
                let market_max_qty = market_max_qty.unwrap_or(max_qty).min(max_qty);
                if min_price > max_price
                    || min_qty > max_qty
                    || market_min_qty > market_max_qty
                    || max_notional.is_some_and(|maximum| maximum < min_notional)
                {
                    continue;
                }
                parsed.insert(
                    symbol,
                    ParsedSymbolFilters {
                        tick_size,
                        min_price,
                        max_price,
                        min_qty,
                        step_size,
                        max_qty,
                        market_min_qty,
                        market_step_size,
                        market_max_qty,
                        min_notional,
                        max_notional,
                        min_notional_apply_to_market,
                        max_notional_apply_to_market,
                    },
                );
            }
        }

        parsed
    }

    fn current_timestamp(&self) -> u64 {
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("Time went backwards")
            .as_millis() as i64;
        let offset = self.time_offset.load(Ordering::Relaxed);
        (ts + offset) as u64
    }

    fn sign(&self, secret_key: &str, query_string: &str) -> String {
        let mut mac = HmacSha256::new_from_slice(secret_key.as_bytes())
            .expect("HMAC can take key of any size");
        mac.update(query_string.as_bytes());
        let result = mac.finalize();
        hex::encode(result.into_bytes())
    }

    pub fn spot_user_stream_subscription_request(&self, request_id: &str) -> serde_json::Value {
        let timestamp = self.current_timestamp();
        let recv_window = 5_000_u64;
        // WebSocket API signatures sort all params (except signature) by name.
        let payload = format!(
            "apiKey={}&recvWindow={}&timestamp={}",
            self.spot_api_key, recv_window, timestamp
        );
        let signature = self.sign(&self.spot_secret_key, &payload);
        serde_json::json!({
            "id": request_id,
            "method": "userDataStream.subscribe.signature",
            "params": {
                "apiKey": self.spot_api_key,
                "recvWindow": recv_window,
                "timestamp": timestamp,
                "signature": signature,
            }
        })
    }

    fn build_signed_request_with_base(
        &self,
        method: Method,
        base_url: &str,
        endpoint: &str,
        mut params: Vec<(&str, String)>,
    ) -> RequestBuilder {
        params.push(("recvWindow", "60000".to_string()));
        params.push(("timestamp", self.current_timestamp().to_string()));

        let query_string = params
            .iter()
            .map(|(k, v)| format!("{}={}", k, v))
            .collect::<Vec<String>>()
            .join("&");

        let (api_key_to_use, secret_key_to_use) = if base_url == self.spot_base_url {
            (&self.spot_api_key, &self.spot_secret_key)
        } else {
            (&self.api_key, &self.secret_key)
        };

        let signature = self.sign(secret_key_to_use, &query_string);
        let final_query = format!("{}&signature={}", query_string, signature);
        let url = format!("{}{}?{}", base_url, endpoint, final_query);

        self.client
            .request(method, &url)
            .header("X-MBX-APIKEY", api_key_to_use)
    }

    pub async fn get_open_orders(&self) -> Result<String, String> {
        self.get_open_orders_for_venue(LegVenue::UsdtFutures).await
    }

    pub async fn get_open_orders_for_venue(&self, venue: LegVenue) -> Result<String, String> {
        let (base_url, endpoint, label) = match venue {
            LegVenue::Spot => (
                &self.spot_base_url,
                "/api/v3/openOrders",
                "spot open orders",
            ),
            LegVenue::UsdtFutures => (
                &self.fut_base_url,
                "/fapi/v1/openOrders",
                "futures open orders",
            ),
        };
        let req = self.build_signed_request_with_base(Method::GET, base_url, endpoint, vec![]);
        self.send_checked_text(req, label).await
    }

    pub async fn get_account(&self) -> Result<String, String> {
        let req = self.build_signed_request_with_base(
            Method::GET,
            &self.spot_base_url,
            "/api/v3/account",
            vec![],
        );
        self.send_checked_text(req, "spot account").await
    }

    pub async fn get_fapi_account(&self) -> Result<String, String> {
        let primary = self.build_signed_request_with_base(
            Method::GET,
            &self.fut_base_url,
            "/fapi/v3/account",
            vec![],
        );
        match self.send_checked_text(primary, "futures account V3").await {
            Ok(body) => Ok(body),
            Err(error)
                if error.contains("returned HTTP 400") || error.contains("returned HTTP 404") =>
            {
                let fallback = self.build_signed_request_with_base(
                    Method::GET,
                    &self.fut_base_url,
                    "/fapi/v2/account",
                    vec![],
                );
                self.send_checked_text(fallback, "futures account V2 fallback")
                    .await
            }
            Err(error) => Err(error),
        }
    }

    pub async fn get_fapi_position_risk(&self) -> Result<String, String> {
        let primary = self.build_signed_request_with_base(
            Method::GET,
            &self.fut_base_url,
            "/fapi/v3/positionRisk",
            vec![],
        );
        match self
            .send_checked_text(primary, "futures position risk V3")
            .await
        {
            Ok(body) => Ok(body),
            Err(error)
                if error.contains("returned HTTP 400") || error.contains("returned HTTP 404") =>
            {
                let fallback = self.build_signed_request_with_base(
                    Method::GET,
                    &self.fut_base_url,
                    "/fapi/v2/positionRisk",
                    vec![],
                );
                self.send_checked_text(fallback, "futures position risk V2 fallback")
                    .await
            }
            Err(error) => Err(error),
        }
    }

    pub async fn get_fapi_position_mode(&self) -> Result<String, String> {
        let request = self.build_signed_request_with_base(
            Method::GET,
            &self.fut_base_url,
            "/fapi/v1/positionSide/dual",
            vec![],
        );
        self.send_checked_text(request, "futures position mode")
            .await
    }

    pub async fn cancel_order(&self, symbol: &str, order_id: &str) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok(serde_json::json!({
                "orderId": 999998,
                "clientOrderId": order_id,
                "status": "CANCELED",
                "executedQty": "0",
            })
            .to_string());
        }

        let params = vec![
            ("symbol", symbol.to_string()),
            ("origClientOrderId", order_id.to_string()),
        ];
        let req = self.build_signed_request_with_base(
            Method::DELETE,
            &self.spot_base_url,
            "/api/v3/order",
            params,
        );
        self.send_checked_text(req, "spot cancel order").await
    }

    pub async fn cancel_futures_order(
        &self,
        symbol: &str,
        order_id: &str,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok(serde_json::json!({
                "orderId": 999997,
                "clientOrderId": order_id,
                "status": "CANCELED",
                "executedQty": "0",
            })
            .to_string());
        }

        let params = vec![
            ("symbol", symbol.to_string()),
            ("origClientOrderId", order_id.to_string()),
        ];
        let req = self.build_signed_request_with_base(
            Method::DELETE,
            &self.fut_base_url,
            "/fapi/v1/order",
            params,
        );
        self.send_checked_text(req, "futures cancel order").await
    }

    pub async fn place_spot_market_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999999,\"status\":\"FILLED\"}".to_string());
        }

        let params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "MARKET".to_string()),
            ("quantity", quantity.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];

        let req = self.build_signed_request_with_base(
            Method::POST,
            &self.spot_base_url,
            "/api/v3/order",
            params,
        );
        self.send_checked_text(req, "spot market order").await
    }

    pub async fn place_spot_limit_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999998,\"status\":\"NEW\"}".to_string());
        }

        let params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "LIMIT_MAKER".to_string()),
            ("quantity", quantity.to_string()),
            ("price", price.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];

        let req = self.build_signed_request_with_base(
            Method::POST,
            &self.spot_base_url,
            "/api/v3/order",
            params,
        );
        self.send_checked_text(req, "spot limit order").await
    }

    pub async fn place_futures_limit_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999997,\"status\":\"NEW\"}".to_string());
        }

        let params = Self::futures_limit_order_params(
            symbol,
            side,
            quantity,
            price,
            client_order_id,
            reduce_only,
        );

        let req = self.build_signed_request_with_base(
            Method::POST,
            &self.fut_base_url,
            "/fapi/v1/order",
            params,
        );
        self.send_checked_text(req, "futures limit order").await
    }

    fn futures_limit_order_params(
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Vec<(&'static str, String)> {
        let mut params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "LIMIT".to_string()),
            ("timeInForce", "GTX".to_string()),
            ("quantity", quantity.to_string()),
            ("price", price.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];
        // Futures orders in this engine use one-way mode semantics (there is no
        // positionSide parameter). Close orders must be reduce-only so a stale
        // quantity or position race cannot increase or reverse the exposure.
        if reduce_only {
            params.push(("reduceOnly", "true".to_string()));
        }
        params
    }

    pub async fn place_futures_market_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999996,\"status\":\"FILLED\"}".to_string());
        }

        let mut params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "MARKET".to_string()),
            ("quantity", quantity.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];
        if reduce_only {
            params.push(("reduceOnly", "true".to_string()));
        }

        let req = self.build_signed_request_with_base(
            Method::POST,
            &self.fut_base_url,
            "/fapi/v1/order",
            params,
        );
        self.send_checked_text(req, "futures market order").await
    }

    pub async fn get_order_by_client_id(
        &self,
        venue: LegVenue,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<String, String> {
        let params = match venue {
            LegVenue::Spot => vec![
                ("symbol", symbol.to_string()),
                ("origClientOrderId", client_order_id.to_string()),
            ],
            LegVenue::UsdtFutures => vec![
                ("symbol", symbol.to_string()),
                ("origClientOrderId", client_order_id.to_string()),
            ],
        };

        let req = match venue {
            LegVenue::Spot => self.build_signed_request_with_base(
                Method::GET,
                &self.spot_base_url,
                "/api/v3/order",
                params,
            ),
            LegVenue::UsdtFutures => self.build_signed_request_with_base(
                Method::GET,
                &self.fut_base_url,
                "/fapi/v1/order",
                params,
            ),
        };

        self.send_checked_text(req, "query order by client id")
            .await
    }

    pub async fn get_order_by_id(
        &self,
        venue: LegVenue,
        symbol: &str,
        order_id: i64,
    ) -> Result<String, String> {
        let params = vec![
            ("symbol", symbol.to_uppercase()),
            ("orderId", order_id.to_string()),
        ];
        let req = match venue {
            LegVenue::Spot => self.build_signed_request_with_base(
                Method::GET,
                &self.spot_base_url,
                "/api/v3/order",
                params,
            ),
            LegVenue::UsdtFutures => self.build_signed_request_with_base(
                Method::GET,
                &self.fut_base_url,
                "/fapi/v1/order",
                params,
            ),
        };
        self.send_checked_text(req, "query order by exchange id")
            .await
    }

    fn is_authoritative_order_not_found(error: &str) -> bool {
        error.contains("exchange error -2013") || error.contains("Order does not exist")
    }

    fn submission_failure_may_have_reached_exchange(error: &str) -> bool {
        error.contains("class=transport_ambiguous")
            || error.contains("class=ambiguous_timeout")
            || error.contains("class=ambiguous_server_result")
            || error.contains("class=server_transient")
            || error.to_ascii_lowercase().contains("duplicate")
            || error.contains("exchange error -4116")
    }

    async fn probe_order_after_ambiguous_submit(
        &self,
        venue: LegVenue,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<Option<String>, String> {
        self.probe_order_after_ambiguous_submit_with_attempts(venue, symbol, client_order_id, 2)
            .await
    }

    async fn probe_order_after_ambiguous_submit_with_attempts(
        &self,
        venue: LegVenue,
        symbol: &str,
        client_order_id: &str,
        readback_attempts: u16,
    ) -> Result<Option<String>, String> {
        if readback_attempts == 0 {
            return Err("ambiguous submission readback budget is zero".to_string());
        }
        let mut last_unavailable = None;
        for attempt in 0..readback_attempts {
            match self
                .get_order_by_client_id(venue, symbol, client_order_id)
                .await
            {
                Ok(body) => return Ok(Some(body)),
                Err(err) if Self::is_authoritative_order_not_found(&err) => {
                    last_unavailable = None;
                }
                Err(err) => {
                    last_unavailable = Some(err);
                }
            }
            if attempt + 1 < readback_attempts {
                tokio::time::sleep(Duration::from_millis(250)).await;
            }
        }
        if let Some(error) = last_unavailable {
            return Err(format!(
                "ambiguous submission and authoritative order lookup unavailable after {readback_attempts} attempt(s): {error}"
            ));
        }
        Ok(None)
    }

    /// Submit a deterministic spot limit order. Any ambiguous POST outcome is
    /// queried twice before one same-ID retry is permitted. A duplicate error
    /// on that retry is queried again, so the caller never creates a second
    /// exchange identity.
    pub async fn place_spot_limit_order_read_before_retry(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
    ) -> Result<ReconciledSubmission, String> {
        let first_error = match self
            .place_spot_limit_order(symbol, side, quantity, price, client_order_id)
            .await
        {
            Ok(body) => {
                return Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: false,
                    retried_after_negative_proof: false,
                });
            }
            Err(error) => error,
        };
        if !Self::submission_failure_may_have_reached_exchange(&first_error) {
            return Err(first_error);
        }
        if let Some(body) = self
            .probe_order_after_ambiguous_submit(LegVenue::Spot, symbol, client_order_id)
            .await?
        {
            return Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: false,
            });
        }

        match self
            .place_spot_limit_order(symbol, side, quantity, price, client_order_id)
            .await
        {
            Ok(body) => Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: true,
            }),
            Err(error) if Self::submission_failure_may_have_reached_exchange(&error) => {
                let body = self
                    .get_order_by_client_id(LegVenue::Spot, symbol, client_order_id)
                    .await
                    .map_err(|_| {
                        "ambiguous spot submission remained unresolved after same-ID retry"
                            .to_string()
                    })?;
                Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: true,
                    retried_after_negative_proof: true,
                })
            }
            Err(error) => Err(error),
        }
    }

    pub async fn place_futures_limit_order_read_before_retry(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<ReconciledSubmission, String> {
        let first_error = match self
            .place_futures_limit_order(symbol, side, quantity, price, client_order_id, reduce_only)
            .await
        {
            Ok(body) => {
                return Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: false,
                    retried_after_negative_proof: false,
                });
            }
            Err(error) => error,
        };
        if !Self::submission_failure_may_have_reached_exchange(&first_error) {
            return Err(first_error);
        }
        if let Some(body) = self
            .probe_order_after_ambiguous_submit(LegVenue::UsdtFutures, symbol, client_order_id)
            .await?
        {
            return Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: false,
            });
        }

        match self
            .place_futures_limit_order(symbol, side, quantity, price, client_order_id, reduce_only)
            .await
        {
            Ok(body) => Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: true,
            }),
            Err(error) if Self::submission_failure_may_have_reached_exchange(&error) => {
                let body = self
                    .get_order_by_client_id(LegVenue::UsdtFutures, symbol, client_order_id)
                    .await
                    .map_err(|_| {
                        "ambiguous futures submission remained unresolved after same-ID retry"
                            .to_string()
                    })?;
                Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: true,
                    retried_after_negative_proof: true,
                })
            }
            Err(error) => Err(error),
        }
    }

    pub async fn place_spot_market_order_read_before_retry(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
    ) -> Result<ReconciledSubmission, String> {
        self.place_spot_market_order_read_before_retry_with_budget(
            symbol,
            side,
            quantity,
            client_order_id,
            1,
            2,
        )
        .await
    }

    /// Emergency-safe deterministic submission. Every ambiguous POST is read
    /// back under the caller's explicit budget before the same client id may be
    /// retried. Exhaustion is ambiguous and therefore never reported as a
    /// proven rejection.
    #[allow(clippy::too_many_arguments)]
    pub async fn place_spot_market_order_read_before_retry_with_budget(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
        max_retries: u16,
        readback_attempts: u16,
    ) -> Result<ReconciledSubmission, String> {
        for submit_attempt in 0..=max_retries {
            match self
                .place_spot_market_order(symbol, side, quantity, client_order_id)
                .await
            {
                Ok(body) => {
                    return Ok(ReconciledSubmission {
                        body,
                        recovered_after_ambiguous_submit: submit_attempt > 0,
                        retried_after_negative_proof: submit_attempt > 0,
                    });
                }
                Err(error) if Self::submission_failure_may_have_reached_exchange(&error) => {
                    if let Some(body) = self
                        .probe_order_after_ambiguous_submit_with_attempts(
                            LegVenue::Spot,
                            symbol,
                            client_order_id,
                            readback_attempts,
                        )
                        .await?
                    {
                        return Ok(ReconciledSubmission {
                            body,
                            recovered_after_ambiguous_submit: true,
                            retried_after_negative_proof: submit_attempt > 0,
                        });
                    }
                    if submit_attempt == max_retries {
                        return Err(format!(
                            "ambiguous spot market submission unresolved after {} same-ID submit attempt(s) and {} readback(s) each",
                            u32::from(max_retries) + 1,
                            readback_attempts
                        ));
                    }
                }
                Err(error) => return Err(error),
            }
        }
        Err("spot emergency submit budget exhausted".to_string())
    }

    pub async fn place_futures_market_order_read_before_retry(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<ReconciledSubmission, String> {
        self.place_futures_market_order_read_before_retry_with_budget(
            symbol,
            side,
            quantity,
            client_order_id,
            reduce_only,
            1,
            2,
        )
        .await
    }

    #[allow(clippy::too_many_arguments)]
    pub async fn place_futures_market_order_read_before_retry_with_budget(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
        reduce_only: bool,
        max_retries: u16,
        readback_attempts: u16,
    ) -> Result<ReconciledSubmission, String> {
        // This primitive is shared by ordinary legging defense and the
        // emergency actor.  The latter forces `reduce_only=true` at its typed
        // call boundary; routine entry repair must retain its non-reduce-only
        // semantics while still using the same read-before-retry algorithm.
        for submit_attempt in 0..=max_retries {
            match self
                .place_futures_market_order(symbol, side, quantity, client_order_id, reduce_only)
                .await
            {
                Ok(body) => {
                    return Ok(ReconciledSubmission {
                        body,
                        recovered_after_ambiguous_submit: submit_attempt > 0,
                        retried_after_negative_proof: submit_attempt > 0,
                    });
                }
                Err(error) if Self::submission_failure_may_have_reached_exchange(&error) => {
                    if let Some(body) = self
                        .probe_order_after_ambiguous_submit_with_attempts(
                            LegVenue::UsdtFutures,
                            symbol,
                            client_order_id,
                            readback_attempts,
                        )
                        .await?
                    {
                        return Ok(ReconciledSubmission {
                            body,
                            recovered_after_ambiguous_submit: true,
                            retried_after_negative_proof: submit_attempt > 0,
                        });
                    }
                    if submit_attempt == max_retries {
                        return Err(format!(
                            "ambiguous futures market submission unresolved after {} same-ID submit attempt(s) and {} readback(s) each",
                            u32::from(max_retries) + 1,
                            readback_attempts
                        ));
                    }
                }
                Err(error) => return Err(error),
            }
        }
        Err("futures emergency submit budget exhausted".to_string())
    }

    /// Return one bounded, authoritative order-history page for private-stream
    /// recovery.  Callers must fail closed when Binance returns `limit` rows:
    /// silently accepting a truncated page could lose a fill during a stream
    /// outage.
    pub async fn get_order_history(
        &self,
        venue: LegVenue,
        symbol: &str,
        start_time_ms: i64,
        end_time_ms: i64,
        limit: u16,
    ) -> Result<String, String> {
        let params = vec![
            ("symbol", symbol.to_uppercase()),
            ("startTime", start_time_ms.max(0).to_string()),
            ("endTime", end_time_ms.max(start_time_ms).to_string()),
            ("limit", limit.clamp(1, 1000).to_string()),
        ];
        let req = match venue {
            LegVenue::Spot => self.build_signed_request_with_base(
                Method::GET,
                &self.spot_base_url,
                "/api/v3/allOrders",
                params,
            ),
            LegVenue::UsdtFutures => self.build_signed_request_with_base(
                Method::GET,
                &self.fut_base_url,
                "/fapi/v1/allOrders",
                params,
            ),
        };
        self.send_checked_text(req, "private-stream order-history backfill")
            .await
    }

    /// Return one bounded, authoritative trade-history page for private-stream
    /// recovery.  Both Binance APIs require a symbol, so the stream manager
    /// calls this for every monitored symbol before restoring readiness.
    pub async fn get_user_trade_history(
        &self,
        venue: LegVenue,
        symbol: &str,
        start_time_ms: i64,
        end_time_ms: i64,
        limit: u16,
    ) -> Result<String, String> {
        let params = vec![
            ("symbol", symbol.to_uppercase()),
            ("startTime", start_time_ms.max(0).to_string()),
            ("endTime", end_time_ms.max(start_time_ms).to_string()),
            ("limit", limit.clamp(1, 1000).to_string()),
        ];
        let req = match venue {
            LegVenue::Spot => self.build_signed_request_with_base(
                Method::GET,
                &self.spot_base_url,
                "/api/v3/myTrades",
                params,
            ),
            LegVenue::UsdtFutures => self.build_signed_request_with_base(
                Method::GET,
                &self.fut_base_url,
                "/fapi/v1/userTrades",
                params,
            ),
        };
        self.send_checked_text(req, "private-stream trade-history backfill")
            .await
    }

    /// Fetch recent realized funding income as an independent startup
    /// reconciliation surface. User-data replay reconstructs orders/trades,
    /// while funding payments are account-ledger effects and need their own
    /// authoritative REST query before READY.
    pub async fn get_futures_funding_income_history(
        &self,
        start_time_ms: i64,
        end_time_ms: i64,
        limit: u16,
    ) -> Result<String, String> {
        let params = vec![
            ("incomeType", "FUNDING_FEE".to_string()),
            ("startTime", start_time_ms.max(0).to_string()),
            ("endTime", end_time_ms.max(start_time_ms).to_string()),
            ("limit", limit.clamp(1, 1000).to_string()),
        ];
        let req = self.build_signed_request_with_base(
            Method::GET,
            &self.fut_base_url,
            "/fapi/v1/income",
            params,
        );
        self.send_checked_text(req, "futures funding-income history")
            .await
    }

    pub async fn create_listen_key(&self) -> Result<String, String> {
        let url = format!("{}/fapi/v1/listenKey", self.fut_base_url);
        let req = self.client.post(&url).header("X-MBX-APIKEY", &self.api_key);
        self.send_checked_text(req, "create futures listen key")
            .await
    }

    pub async fn create_spot_listen_key(&self) -> Result<String, String> {
        let url = format!("{}/api/v3/userDataStream", self.spot_base_url);
        let req = self
            .client
            .post(&url)
            .header("X-MBX-APIKEY", &self.spot_api_key);
        self.send_checked_text(req, "create spot listen key").await
    }

    pub async fn keepalive_listen_key(&self, listen_key: &str) -> Result<String, String> {
        let url = format!(
            "{}/fapi/v1/listenKey?listenKey={}",
            self.fut_base_url, listen_key
        );
        let req = self.client.put(&url).header("X-MBX-APIKEY", &self.api_key);
        self.send_checked_text(req, "keepalive futures listen key")
            .await
    }

    pub async fn keepalive_spot_listen_key(&self, listen_key: &str) -> Result<String, String> {
        let url = format!(
            "{}/api/v3/userDataStream?listenKey={}",
            self.spot_base_url, listen_key
        );
        let req = self
            .client
            .put(&url)
            .header("X-MBX-APIKEY", &self.spot_api_key);
        self.send_checked_text(req, "keepalive spot listen key")
            .await
    }

    pub async fn close_listen_key(&self, listen_key: &str) -> Result<String, String> {
        let url = format!(
            "{}/fapi/v1/listenKey?listenKey={}",
            self.fut_base_url, listen_key
        );
        let req = self
            .client
            .delete(&url)
            .header("X-MBX-APIKEY", &self.api_key);
        self.send_checked_text(req, "close futures listen key")
            .await
    }

    pub async fn close_spot_listen_key(&self, listen_key: &str) -> Result<String, String> {
        let url = format!(
            "{}/api/v3/userDataStream?listenKey={}",
            self.spot_base_url, listen_key
        );
        let req = self
            .client
            .delete(&url)
            .header("X-MBX-APIKEY", &self.spot_api_key);
        self.send_checked_text(req, "close spot listen key").await
    }

    /// Cancel ALL open futures orders for a symbol (emergency shutdown).
    pub async fn cancel_all_open_futures_orders(&self, symbol: &str) -> Result<String, String> {
        let params = vec![("symbol", symbol.to_string())];
        let req = self.build_signed_request_with_base(
            Method::DELETE,
            &self.fut_base_url,
            "/fapi/v1/allOpenOrders",
            params,
        );
        self.send_checked_text(req, "cancel all futures orders")
            .await
    }
}

/// Retry wrapper with exponential backoff and jitter for REST API calls.
pub async fn with_retry<F, Fut>(
    operation: F,
    max_retries: u32,
    base_delay_ms: u64,
) -> Result<String, String>
where
    F: Fn() -> Fut,
    Fut: std::future::Future<Output = Result<String, reqwest::Error>>,
{
    let mut last_err = String::new();
    for attempt in 0..=max_retries {
        match operation().await {
            Ok(result) => return Ok(result),
            Err(e) => {
                last_err = BinanceRest::safe_transport_error(&e).to_string();
                if attempt < max_retries {
                    let delay = base_delay_ms * 2u64.pow(attempt);
                    let jitter = rand::random::<u64>() % (delay / 2 + 1);
                    tokio::time::sleep(tokio::time::Duration::from_millis(delay + jitter)).await;
                    tracing::warn!("REST retry {}/{}: {}", attempt + 1, max_retries, last_err);
                }
            }
        }
    }
    Err(format!(
        "All {} retries exhausted: {}",
        max_retries, last_err
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering as AtomicOrdering};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    fn decimal(value: &str) -> ExactDecimal {
        value.parse().expect("valid test decimal")
    }

    fn param_value<'a>(params: &'a [(&str, String)], key: &str) -> Option<&'a str> {
        params
            .iter()
            .find_map(|(name, value)| (*name == key).then_some(value.as_str()))
    }

    #[test]
    fn exchange_rate_limit_snapshot_requires_two_fresh_authoritative_venues() {
        let telemetry = ExchangeRateLimitTelemetry::default();
        telemetry.set_limit(LegVenue::Spot, 6_000);
        telemetry.set_limit(LegVenue::UsdtFutures, 2_400);
        let mut spot_headers = HeaderMap::new();
        spot_headers.insert("x-mbx-used-weight-1m", "120".parse().unwrap());
        let mut futures_headers = HeaderMap::new();
        futures_headers.insert("x-mbx-used-weight-1m", "80".parse().unwrap());
        telemetry.record_response(LegVenue::Spot, &spot_headers, 200, 1_000_000);

        let one_venue = telemetry.snapshot(1_000_001);
        assert_eq!(one_venue.status, "STALE");
        assert_eq!(one_venue.combined_remaining_weight, 2_400);

        telemetry.record_response(LegVenue::UsdtFutures, &futures_headers, 200, 1_000_002);
        let ready = telemetry.snapshot(1_000_003);
        assert_eq!(ready.status, "READY");
        assert_eq!(ready.spot_remaining_weight, 5_880);
        assert_eq!(ready.futures_remaining_weight, 2_320);
        assert_eq!(ready.combined_remaining_weight, 2_320);

        assert_eq!(telemetry.snapshot(1_031_000).status, "STALE");
    }

    #[test]
    fn exchange_rate_limit_counter_never_regresses_inside_window_and_429_blocks() {
        let telemetry = ExchangeRateLimitTelemetry::default();
        telemetry.set_limit(LegVenue::Spot, 100);
        telemetry.set_limit(LegVenue::UsdtFutures, 100);
        let headers = |used: &'static str| {
            let mut values = HeaderMap::new();
            values.insert("x-mbx-used-weight-1m", used.parse().unwrap());
            values
        };
        telemetry.record_response(LegVenue::Spot, &headers("90"), 200, 2_000_000);
        telemetry.record_response(LegVenue::UsdtFutures, &headers("80"), 200, 2_000_000);
        telemetry.record_response(LegVenue::Spot, &headers("10"), 200, 2_000_100);
        assert_eq!(telemetry.snapshot(2_000_101).spot_used_weight, 90);

        let mut limited = headers("95");
        limited.insert(RETRY_AFTER, "7".parse().unwrap());
        telemetry.record_response(LegVenue::Spot, &limited, 429, 2_000_200);
        let blocked = telemetry.snapshot(2_000_201);
        assert_eq!(blocked.status, "BLOCKED");
        assert_eq!(blocked.blocked_until_ms, 2_007_200);
        assert_eq!(blocked.combined_remaining_weight, 5);

        telemetry.record_response(LegVenue::Spot, &headers("3"), 200, 2_060_201);
        let reset = telemetry.snapshot(2_060_202);
        assert_eq!(reset.spot_used_weight, 3);
        assert_eq!(reset.status, "STALE");

        telemetry.record_response(LegVenue::UsdtFutures, &headers("4"), 418, 2_060_300);
        let banned = telemetry.snapshot(2_060_301);
        assert_eq!(banned.status, "BLOCKED");
        assert_eq!(banned.blocked_until_ms, 2_360_300);
        assert!(!banned.critical_allowed);
    }

    #[test]
    fn exchange_info_rate_limit_parser_uses_one_minute_request_weight_only() {
        let payload = serde_json::json!({
            "rateLimits": [
                {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 50},
                {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 100},
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "SECOND", "intervalNum": 10, "limit": 50},
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 2400}
            ]
        });
        assert_eq!(BinanceRest::request_weight_limit(&payload), Some(2_400));
        assert_eq!(
            BinanceRest::rate_limit_definitions(&payload),
            RateLimitDefinitions {
                request_weight_1m: 2_400,
                orders_10s: 50,
                orders_1m: 100,
            }
        );
    }

    #[test]
    fn quota_reserve_tracks_weight_and_order_windows_independently() {
        let telemetry = ExchangeRateLimitTelemetry::default();
        let limits = RateLimitDefinitions {
            request_weight_1m: 100,
            orders_10s: 100,
            orders_1m: 1_000,
        };
        telemetry.set_limits(LegVenue::Spot, limits);
        telemetry.set_limits(LegVenue::UsdtFutures, limits);
        let headers = |weight: u64, orders_10s: u64, orders_1m: u64| {
            let mut values = HeaderMap::new();
            values.insert("x-mbx-used-weight-1m", weight.to_string().parse().unwrap());
            values.insert(
                "x-mbx-order-count-10s",
                orders_10s.to_string().parse().unwrap(),
            );
            values.insert(
                "x-mbx-order-count-1m",
                orders_1m.to_string().parse().unwrap(),
            );
            values
        };
        telemetry.record_response(LegVenue::Spot, &headers(69, 10, 100), 200, 3_000_000);
        telemetry.record_response(LegVenue::UsdtFutures, &headers(69, 10, 100), 200, 3_000_000);
        let ready = telemetry.snapshot(3_000_001);
        assert_eq!(ready.status, "READY");
        assert!(ready.nonessential_allowed && ready.entry_allowed && ready.critical_allowed);

        telemetry.record_response(LegVenue::Spot, &headers(70, 10, 100), 200, 3_000_002);
        let shedding = telemetry.snapshot(3_000_003);
        assert_eq!(shedding.status, "SHEDDING");
        assert!(!shedding.nonessential_allowed);
        assert!(shedding.entry_allowed && shedding.critical_allowed);

        // The order-count window alone can consume the emergency reserve even
        // while request weight remains below the entry threshold.
        telemetry.record_response(LegVenue::UsdtFutures, &headers(69, 85, 100), 200, 3_000_004);
        let reserved = telemetry.snapshot(3_000_005);
        assert_eq!(reserved.status, "ENTRY_BLOCKED");
        assert_eq!(reserved.max_utilization_bps, 8_500);
        assert!(!reserved.entry_allowed);
        assert!(reserved.critical_allowed);
        assert_eq!(reserved.reserved_request_weight, 15);
        assert_eq!(reserved.reserved_order_count, 15);

        telemetry.record_response(
            LegVenue::UsdtFutures,
            &headers(69, 100, 100),
            200,
            3_000_006,
        );
        assert!(!telemetry.snapshot(3_000_007).critical_allowed);

        // Local counters cannot remain latched forever when no request can be
        // sent to observe a lower header. After a complete authoritative
        // window has elapsed, critical probes may resume, while stale
        // telemetry still keeps entries blocked until fresh headers arrive.
        let expired = telemetry.snapshot(3_010_006);
        assert_eq!(expired.futures_order_used_10s, 0);
        assert!(expired.critical_allowed);
        assert!(!telemetry.snapshot(3_060_007).entry_allowed);
    }

    #[test]
    fn ambiguous_503_holds_entries_but_preserves_critical_capacity() {
        let telemetry = ExchangeRateLimitTelemetry::default();
        for venue in [LegVenue::Spot, LegVenue::UsdtFutures] {
            telemetry.set_limit(venue, 100);
            let mut headers = HeaderMap::new();
            headers.insert("x-mbx-used-weight-1m", "10".parse().unwrap());
            telemetry.record_response(venue, &headers, 200, 4_000_000);
        }
        telemetry.record_failure_class(
            LegVenue::UsdtFutures,
            ApiFailureClass::AmbiguousServerResult,
            4_000_001,
        );
        let snapshot = telemetry.snapshot(4_000_002);
        assert_eq!(snapshot.status, "ENTRY_BLOCKED");
        assert!(!snapshot.entry_allowed);
        assert!(snapshot.critical_allowed);
        assert_eq!(
            snapshot.last_failure_class.as_deref(),
            Some("ambiguous_server_result")
        );
        assert_eq!(snapshot.ambiguous_until_ms, 4_060_001);
    }

    #[test]
    fn exchange_clock_warns_above_100ms_and_blocks_above_250ms_or_stale() {
        let rest = BinanceRest::new(String::new(), String::new(), "testnet".to_string());
        assert_eq!(rest.clock_health_snapshot().status, "UNSYNCHRONIZED");

        let now_ms = BinanceRest::current_time_ms();
        rest.set_clock_health_for_test(101, 20, now_ms);
        let warning = rest.clock_health_snapshot();
        assert_eq!(warning.status, "WARNING");
        assert!(warning.warning && warning.entry_allowed);

        rest.set_clock_health_for_test(-251, 20, now_ms);
        let blocked = rest.clock_health_snapshot();
        assert_eq!(blocked.status, "BLOCKED");
        assert!(!blocked.entry_allowed);

        rest.set_clock_health_for_test(0, 20, now_ms - CLOCK_SYNC_FRESHNESS_MS - 1);
        assert_eq!(rest.clock_health_snapshot().status, "UNSYNCHRONIZED");
    }

    #[test]
    fn api_failures_have_stable_safety_classifications() {
        assert_eq!(
            BinanceRest::classify_api_failure(400, r#"{"code":-1007,"msg":"timeout"}"#),
            ApiFailureClass::AmbiguousTimeout
        );
        assert_eq!(
            BinanceRest::classify_api_failure(400, r#"{"code":-1021,"msg":"clock"}"#),
            ApiFailureClass::ClockSkew
        );
        assert_eq!(
            BinanceRest::classify_api_failure(418, "{}"),
            ApiFailureClass::IpBanned
        );
        assert_eq!(
            BinanceRest::classify_api_failure(429, "{}"),
            ApiFailureClass::RateLimited
        );
        assert_eq!(
            BinanceRest::classify_api_failure(503, "{}"),
            ApiFailureClass::AmbiguousServerResult
        );
        for ambiguous in [
            "order class=transport_ambiguous HTTP transport failed",
            "order class=ambiguous_timeout exchange error -1007",
            "order class=ambiguous_server_result returned HTTP 503",
            "order class=server_transient returned HTTP 503",
            "order class=client_rejected Duplicate order sent",
        ] {
            assert!(BinanceRest::submission_failure_may_have_reached_exchange(
                ambiguous
            ));
        }
        for proven_rejection in [
            "order class=clock_skew exchange error -1021",
            "order class=authentication returned HTTP 401",
            "order class=client_rejected returned HTTP 400",
            "order class=ip_banned returned HTTP 418",
            "order class=rate_limited returned HTTP 429",
        ] {
            assert!(
                !BinanceRest::submission_failure_may_have_reached_exchange(proven_rejection),
                "a typed exchange rejection must not trigger query/retry traffic: {proven_rejection}"
            );
        }
    }

    #[test]
    fn futures_limit_exit_params_are_reduce_only() {
        let params = BinanceRest::futures_limit_order_params(
            "BTCUSDT",
            TradeSide::Buy,
            "0.01",
            "60000.0",
            "fut-exit-1",
            true,
        );

        assert_eq!(param_value(&params, "reduceOnly"), Some("true"));
        assert_eq!(param_value(&params, "timeInForce"), Some("GTX"));
    }

    #[test]
    fn futures_limit_entry_params_are_not_reduce_only() {
        let params = BinanceRest::futures_limit_order_params(
            "BTCUSDT",
            TradeSide::Sell,
            "0.01",
            "60000.0",
            "fut-entry-1",
            false,
        );

        assert_eq!(param_value(&params, "reduceOnly"), None);
    }

    #[test]
    fn exchange_filters_fail_closed_on_missing_fields_or_non_trading_status() {
        let payload = serde_json::json!({
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "minPrice": "0.10", "maxPrice": "1000000", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001", "maxQty": "100"},
                        {"filterType": "MARKET_LOT_SIZE", "minQty": "0.01", "stepSize": "0.01", "maxQty": "50"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"}
                    ]
                },
                {
                    "symbol": "MISSINGUSDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001", "maxQty": "100"}
                    ]
                },
                {
                    "symbol": "HALTEDUSDT",
                    "status": "BREAK",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001", "maxQty": "100"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"}
                    ]
                }
            ]
        });

        let filters = BinanceRest::parse_symbol_filters(&payload);
        assert_eq!(
            filters.get("BTCUSDT"),
            Some(&ParsedSymbolFilters {
                tick_size: decimal("0.1"),
                min_price: decimal("0.1"),
                max_price: decimal("1000000"),
                min_qty: decimal("0.001"),
                step_size: decimal("0.001"),
                max_qty: decimal("100"),
                market_min_qty: decimal("0.01"),
                market_step_size: decimal("0.01"),
                market_max_qty: decimal("50"),
                min_notional: decimal("5"),
                max_notional: None,
                min_notional_apply_to_market: true,
                max_notional_apply_to_market: false,
            })
        );
        assert!(!filters.contains_key("MISSINGUSDT"));
        assert!(!filters.contains_key("HALTEDUSDT"));
    }

    #[test]
    fn exchange_filters_preserve_exact_notional_rules_and_intersect_market_grids() {
        let payload = serde_json::json!({
            "symbols": [{
                "symbol": "HOSTILEUSDT",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "minPrice": "0.00000001", "maxPrice": "10000000000000000.00000001", "tickSize": "0.00000001"},
                    {"filterType": "LOT_SIZE", "minQty": "0.003", "stepSize": "0.003", "maxQty": "999999.999"},
                    {"filterType": "MARKET_LOT_SIZE", "minQty": "0", "stepSize": "0.002", "maxQty": "0"},
                    {"filterType": "NOTIONAL", "minNotional": "5.0000000000000001", "applyMinToMarket": false, "maxNotional": "1000.0000000000000001", "applyMaxToMarket": true}
                ]
            }]
        });

        let filters = BinanceRest::parse_symbol_filters(&payload);
        let filters = filters.get("HOSTILEUSDT").expect("valid exact filters");
        assert_eq!(filters.tick_size.to_string(), "0.00000001");
        assert_eq!(filters.max_price.to_string(), "10000000000000000.00000001");
        assert_eq!(filters.market_min_qty.to_string(), "0.003");
        assert_eq!(filters.market_step_size.to_string(), "0.006");
        assert_eq!(filters.market_max_qty.to_string(), "999999.999");
        assert_eq!(filters.min_notional.to_string(), "5.0000000000000001");
        assert_eq!(
            filters.max_notional.expect("maximum notional").to_string(),
            "1000.0000000000000001"
        );
        assert!(!filters.min_notional_apply_to_market);
        assert!(filters.max_notional_apply_to_market);
    }

    #[test]
    fn malformed_or_contradictory_decimal_filters_fail_closed() {
        let valid_base = |symbol: &str,
                          price: serde_json::Value,
                          market: serde_json::Value,
                          notional: serde_json::Value| {
            serde_json::json!({
                "symbol": symbol,
                "status": "TRADING",
                "filters": [
                    price,
                    {"filterType": "LOT_SIZE", "minQty": "0.001", "stepSize": "0.001", "maxQty": "100"},
                    market,
                    notional
                ]
            })
        };
        let payload = serde_json::json!({
            "symbols": [
                valid_base(
                    "BADPRICE",
                    serde_json::json!({"filterType": "PRICE_FILTER", "minPrice": "not-a-number", "maxPrice": "10", "tickSize": "0.01"}),
                    serde_json::json!({"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001", "maxQty": "100"}),
                    serde_json::json!({"filterType": "MIN_NOTIONAL", "notional": "5"}),
                ),
                valid_base(
                    "BADMARKETGRID",
                    serde_json::json!({"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "10", "tickSize": "0.01"}),
                    serde_json::json!({"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "oops", "maxQty": "100"}),
                    serde_json::json!({"filterType": "MIN_NOTIONAL", "notional": "5"}),
                ),
                valid_base(
                    "BADNOTIONAL",
                    serde_json::json!({"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "10", "tickSize": "0.01"}),
                    serde_json::json!({"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001", "maxQty": "100"}),
                    serde_json::json!({"filterType": "NOTIONAL", "minNotional": "10", "maxNotional": "9", "applyMinToMarket": true, "applyMaxToMarket": true}),
                ),
                valid_base(
                    "BADFLAG",
                    serde_json::json!({"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "10", "tickSize": "0.01"}),
                    serde_json::json!({"filterType": "MARKET_LOT_SIZE", "minQty": "0.001", "stepSize": "0.001", "maxQty": "100"}),
                    serde_json::json!({"filterType": "MIN_NOTIONAL", "notional": "5", "applyToMarket": "yes"}),
                )
            ]
        });

        assert!(BinanceRest::parse_symbol_filters(&payload).is_empty());
    }

    #[tokio::test]
    async fn accepted_rest_timeout_is_read_before_retry_with_one_exchange_effect() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let post_count = Arc::new(AtomicUsize::new(0));
        let get_count = Arc::new(AtomicUsize::new(0));
        let post_count_server = post_count.clone();
        let get_count_server = get_count.clone();
        let server = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };
                let posts = post_count_server.clone();
                let gets = get_count_server.clone();
                tokio::spawn(async move {
                    let mut request = vec![0u8; 8192];
                    let Ok(read) = socket.read(&mut request).await else {
                        return;
                    };
                    let request = String::from_utf8_lossy(&request[..read]);
                    let is_post = request.starts_with("POST ");
                    let body = if is_post {
                        posts.fetch_add(1, AtomicOrdering::SeqCst);
                        // Model an exchange that committed the deterministic
                        // order but whose HTTP response arrives after the
                        // client's bounded timeout.
                        tokio::time::sleep(Duration::from_millis(200)).await;
                        r#"{"symbol":"BTCUSDT","orderId":77,"clientOrderId":"bngs_timeout_1","status":"NEW","executedQty":"0"}"#
                    } else {
                        gets.fetch_add(1, AtomicOrdering::SeqCst);
                        r#"{"symbol":"BTCUSDT","orderId":77,"clientOrderId":"bngs_timeout_1","status":"NEW","executedQty":"0"}"#
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

        let mut rest = BinanceRest::new(
            "test-key".to_string(),
            "test-secret".to_string(),
            "live".to_string(),
        );
        rest.spot_base_url = format!("http://{address}");
        rest.client = Client::builder()
            .connect_timeout(Duration::from_millis(50))
            .timeout(Duration::from_millis(75))
            .build()
            .unwrap();

        let receipt = rest
            .place_spot_limit_order_read_before_retry(
                "BTCUSDT",
                TradeSide::Buy,
                "0.01",
                "60000",
                "bngs_timeout_1",
            )
            .await
            .expect("authoritative GET must recover the accepted timeout");
        assert!(receipt.recovered_after_ambiguous_submit);
        assert!(!receipt.retried_after_negative_proof);
        assert_eq!(post_count.load(AtomicOrdering::SeqCst), 1);
        assert_eq!(get_count.load(AtomicOrdering::SeqCst), 1);
        server.abort();
    }

    #[tokio::test]
    async fn emergency_futures_ambiguous_503_is_read_back_once_with_reduce_only_identity() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let server_requests = requests.clone();
        let server = tokio::spawn(async move {
            for _ in 0..2 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = vec![0_u8; 8192];
                let read = socket.read(&mut request).await.unwrap();
                let first_line = String::from_utf8_lossy(&request[..read])
                    .lines()
                    .next()
                    .unwrap_or("")
                    .to_string();
                server_requests.lock().unwrap().push(first_line.clone());
                let (status, body) = if first_line.starts_with("POST ") {
                    (
                        "503 Service Unavailable",
                        r#"{"code":-1007,"msg":"Timeout waiting for response from backend server. Send status unknown; execution status unknown."}"#,
                    )
                } else {
                    (
                        "200 OK",
                        r#"{"symbol":"BTCUSDT","orderId":77,"clientOrderId":"bngs_er_f_ambiguous","status":"FILLED","executedQty":"0.123456789012345678"}"#,
                    )
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let mut rest = BinanceRest::new(
            "test-key".to_string(),
            "test-secret".to_string(),
            "live".to_string(),
        );
        rest.fut_base_url = format!("http://{address}");
        let receipt = rest
            .place_futures_market_order_read_before_retry_with_budget(
                "BTCUSDT",
                TradeSide::Buy,
                "0.123456789012345678",
                "bngs_er_f_ambiguous",
                true,
                2,
                3,
            )
            .await
            .expect("signed GET must recover an ambiguous emergency submission");
        server.await.unwrap();

        assert!(receipt.recovered_after_ambiguous_submit);
        assert!(!receipt.retried_after_negative_proof);
        let observed = requests.lock().unwrap();
        assert_eq!(observed.len(), 2);
        assert!(observed[0].starts_with("POST /fapi/v1/order?"));
        assert!(observed[0].contains("reduceOnly=true"));
        assert!(observed[0].contains("quantity=0.123456789012345678"));
        assert!(observed[0].contains("newClientOrderId=bngs_er_f_ambiguous"));
        assert!(observed[1].starts_with("GET /fapi/v1/order?"));
        assert!(observed[1].contains("origClientOrderId=bngs_er_f_ambiguous"));
    }

    #[tokio::test]
    async fn signed_usdm_account_truth_uses_current_endpoints_and_bounded_v2_fallback() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let requests = Arc::new(std::sync::Mutex::new(Vec::<String>::new()));
        let server_requests = requests.clone();
        let server = tokio::spawn(async move {
            for _ in 0..4 {
                let (mut socket, _) = listener.accept().await.unwrap();
                let mut request = vec![0_u8; 8192];
                let read = socket.read(&mut request).await.unwrap();
                let first_line = String::from_utf8_lossy(&request[..read])
                    .lines()
                    .next()
                    .unwrap_or("")
                    .to_string();
                server_requests.lock().unwrap().push(first_line.clone());
                let (status, body) = if first_line.starts_with("GET /fapi/v3/account?") {
                    ("404 Not Found", r#"{"code":-404,"msg":"not found"}"#)
                } else if first_line.starts_with("GET /fapi/v2/account?") {
                    ("200 OK", r#"{"totalWalletBalance":"1"}"#)
                } else if first_line.starts_with("GET /fapi/v3/positionRisk?") {
                    ("200 OK", "[]")
                } else if first_line.starts_with("GET /fapi/v1/positionSide/dual?") {
                    ("200 OK", r#"{"dualSidePosition":false}"#)
                } else {
                    (
                        "500 Internal Server Error",
                        r#"{"code":-1,"msg":"unexpected path"}"#,
                    )
                };
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                socket.write_all(response.as_bytes()).await.unwrap();
            }
        });

        let mut rest = BinanceRest::new(
            "test-key".to_string(),
            "test-secret".to_string(),
            "live".to_string(),
        );
        rest.fut_base_url = format!("http://{address}");
        assert_eq!(
            rest.get_fapi_account().await.unwrap(),
            r#"{"totalWalletBalance":"1"}"#
        );
        assert_eq!(rest.get_fapi_position_risk().await.unwrap(), "[]");
        assert_eq!(
            rest.get_fapi_position_mode().await.unwrap(),
            r#"{"dualSidePosition":false}"#
        );
        server.await.unwrap();

        let observed = requests.lock().unwrap();
        assert_eq!(observed.len(), 4);
        assert!(observed[0].starts_with("GET /fapi/v3/account?"));
        assert!(observed[1].starts_with("GET /fapi/v2/account?"));
        assert!(observed[2].starts_with("GET /fapi/v3/positionRisk?"));
        assert!(observed[3].starts_with("GET /fapi/v1/positionSide/dual?"));
    }

    #[tokio::test]
    async fn listen_key_http_failure_is_not_masqueraded_as_a_success_body() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = vec![0u8; 2048];
            let _ = socket.read(&mut request).await.unwrap();
            let body = r#"{"code":-2015,"msg":"Invalid API-key"}"#;
            let response = format!(
                "HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            socket.write_all(response.as_bytes()).await.unwrap();
        });

        let mut rest = BinanceRest::new(
            "test-key".to_string(),
            "test-secret".to_string(),
            "live".to_string(),
        );
        rest.fut_base_url = format!("http://{address}");
        let error = rest
            .create_listen_key()
            .await
            .expect_err("HTTP 401 must fail listen-key creation");
        assert!(error.contains("HTTP 401"));
        assert!(error.contains("-2015"));
        server.await.unwrap();
    }
}
