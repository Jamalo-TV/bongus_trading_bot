#![allow(dead_code)]
use crate::exact_decimal::ExactDecimal;
use hmac::{Hmac, Mac};
use reqwest::{
    Client, Method, RequestBuilder,
    header::{HeaderMap, RETRY_AFTER},
};
use sha2::Sha256;
use std::sync::{Arc as SharedArc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

use std::sync::atomic::{AtomicI64, Ordering};

const MAINNET_FUTURES_BASE_URL: &str = "https://fapi.binance.com";
const TESTNET_FUTURES_BASE_URL: &str = "https://demo-fapi.binance.com";
const MAINNET_SPOT_BASE_URL: &str = "https://api.binance.com";
const TESTNET_SPOT_BASE_URL: &str = "https://demo-api.binance.com";

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
    pub trading_mode: String,
    rate_limit_telemetry: ExchangeRateLimitTelemetry,
}

const RATE_LIMIT_WINDOW_MS: i64 = 60_000;
const RATE_LIMIT_FRESHNESS_MS: i64 = 30_000;
const DEFAULT_RATE_LIMIT_RETRY_MS: i64 = 60_000;
const DEFAULT_IP_BAN_RETRY_MS: i64 = 300_000;

#[derive(Debug, Clone, Default)]
struct VenueRateLimitState {
    limit: u64,
    used: u64,
    observed_at_ms: i64,
    blocked_until_ms: i64,
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
    pub blocked_until_ms: i64,
    pub event_time_ms: i64,
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
        if matches!(status, 418 | 429) {
            venue_state.blocked_until_ms = venue_state
                .blocked_until_ms
                .max(observed_at_ms.saturating_add(Self::retry_after_ms(headers, status)));
        }
    }

    fn snapshot(&self, event_time_ms: i64) -> ExchangeRateLimitSnapshot {
        let state = self.lock_state().clone();
        let spot_remaining = state.spot.limit.saturating_sub(state.spot.used);
        let futures_remaining = state.futures.limit.saturating_sub(state.futures.used);
        let blocked_until_ms = state
            .spot
            .blocked_until_ms
            .max(state.futures.blocked_until_ms);
        let limits_known = state.spot.limit > 0 && state.futures.limit > 0;
        let observations_known = state.spot.observed_at_ms > 0 && state.futures.observed_at_ms > 0;
        let observations_fresh = observations_known
            && event_time_ms.saturating_sub(state.spot.observed_at_ms) <= RATE_LIMIT_FRESHNESS_MS
            && event_time_ms.saturating_sub(state.futures.observed_at_ms)
                <= RATE_LIMIT_FRESHNESS_MS;
        let blocked = blocked_until_ms > event_time_ms;
        let (status, reason) = if blocked {
            ("BLOCKED", "exchange_retry_after_active")
        } else if !limits_known {
            ("STALE", "exchange_rate_limit_limits_unknown")
        } else if !observations_known {
            ("STALE", "exchange_rate_limit_headers_missing")
        } else if !observations_fresh {
            ("STALE", "exchange_rate_limit_observation_stale")
        } else {
            ("READY", "authoritative_exchange_headers")
        };
        ExchangeRateLimitSnapshot {
            status: status.to_string(),
            reason: reason.to_string(),
            spot_limit_weight: state.spot.limit,
            spot_used_weight: state.spot.used,
            spot_remaining_weight: spot_remaining,
            spot_observed_at_ms: state.spot.observed_at_ms,
            futures_limit_weight: state.futures.limit,
            futures_used_weight: state.futures.used,
            futures_remaining_weight: futures_remaining,
            futures_observed_at_ms: state.futures.observed_at_ms,
            combined_remaining_weight: spot_remaining.min(futures_remaining),
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

        let (fut_base_url, spot_base_url) = match trading_mode.as_str() {
            "testnet" => (
                TESTNET_FUTURES_BASE_URL.to_string(),
                TESTNET_SPOT_BASE_URL.to_string(),
            ),
            _ => (
                MAINNET_FUTURES_BASE_URL.to_string(),
                MAINNET_SPOT_BASE_URL.to_string(),
            ),
        };

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

        Self {
            client,
            api_key: futures_api_key,
            secret_key: futures_secret_key,
            spot_api_key,
            spot_secret_key,
            fut_base_url,
            spot_base_url,
            time_offset: std::sync::Arc::new(AtomicI64::new(0)),
            trading_mode,
            rate_limit_telemetry: ExchangeRateLimitTelemetry::default(),
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

    fn request_weight_limit(json: &serde_json::Value) -> Option<u64> {
        json.get("rateLimits")?
            .as_array()?
            .iter()
            .find_map(|item| {
                let request_weight = item
                    .get("rateLimitType")
                    .and_then(serde_json::Value::as_str)
                    == Some("REQUEST_WEIGHT");
                let minute =
                    item.get("interval").and_then(serde_json::Value::as_str) == Some("MINUTE");
                let interval_num =
                    item.get("intervalNum").and_then(serde_json::Value::as_u64) == Some(1);
                if request_weight && minute && interval_num {
                    item.get("limit").and_then(serde_json::Value::as_u64)
                } else {
                    None
                }
            })
            .filter(|limit| *limit > 0)
    }

    pub fn rate_limit_snapshot(&self) -> ExchangeRateLimitSnapshot {
        self.rate_limit_telemetry.snapshot(Self::current_time_ms())
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
        self.rate_limit_telemetry
            .set_limit(LegVenue::Spot, spot_limit);
        self.rate_limit_telemetry
            .set_limit(LegVenue::UsdtFutures, futures_limit);
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

    pub async fn refresh_rate_limit_telemetry(&self) -> Result<(), String> {
        if self.trading_mode == "paper" {
            return Ok(());
        }
        let futures_url = format!("{}/fapi/v1/time", self.fut_base_url);
        let spot_url = format!("{}/api/v3/time", self.spot_base_url);
        let (futures_result, spot_result) = tokio::join!(
            self.send_checked_text(self.client.get(&futures_url), "futures rate-limit probe",),
            self.send_checked_text(self.client.get(&spot_url), "spot rate-limit probe"),
        );
        let mut errors = Vec::new();
        if let Err(error) = futures_result {
            errors.push(error);
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
        let text = self
            .send_checked_text(self.client.get(&url), "futures server time")
            .await?;
        let json: serde_json::Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
        let server_time = json
            .get("serverTime")
            .and_then(|value| value.as_i64())
            .ok_or_else(|| "futures server time response is missing serverTime".to_string())?;
        let local_time = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|_| "local system clock predates Unix epoch".to_string())?
            .as_millis() as i64;
        self.time_offset
            .store(server_time - local_time, Ordering::Relaxed);
        Ok(())
    }

    pub async fn get_exchange_info(
        &self,
    ) -> Result<std::collections::HashMap<String, ExchangeSymbolInfo>, String> {
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

        if let Some(limit) = Self::request_weight_limit(&futures_json) {
            self.rate_limit_telemetry
                .set_limit(LegVenue::UsdtFutures, limit);
        }
        if let Some(limit) = Self::request_weight_limit(&spot_json) {
            self.rate_limit_telemetry.set_limit(LegVenue::Spot, limit);
        }

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
        let req = self.build_signed_request_with_base(
            Method::GET,
            &self.fut_base_url,
            "/fapi/v2/account",
            vec![],
        );
        self.send_checked_text(req, "futures account").await
    }

    pub async fn get_fapi_position_risk(&self) -> Result<String, String> {
        let req = self.build_signed_request_with_base(
            Method::GET,
            &self.fut_base_url,
            "/fapi/v2/positionRisk",
            vec![],
        );
        self.send_checked_text(req, "futures position risk").await
    }

    pub async fn get_pm_account(&self) -> Result<String, String> {
        // Binance Portfolio Margin Account endpoint (uniMMR)
        let req = self.build_signed_request_with_base(
            Method::GET,
            "https://papi.binance.com",
            "/papi/v1/account",
            vec![],
        );
        self.send_checked_text(req, "portfolio margin account")
            .await
    }

    pub async fn get_pm_um_account(&self) -> Result<String, String> {
        // Binance Portfolio Margin U-margined endpoint
        let req = self.build_signed_request_with_base(
            Method::GET,
            "https://papi.binance.com",
            "/papi/v1/um/account",
            vec![],
        );
        self.send_checked_text(req, "portfolio margin um account")
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
        for attempt in 0..2 {
            match self
                .get_order_by_client_id(venue, symbol, client_order_id)
                .await
            {
                Ok(body) => return Ok(Some(body)),
                Err(err) if Self::is_authoritative_order_not_found(&err) => {
                    if attempt == 0 {
                        tokio::time::sleep(Duration::from_millis(250)).await;
                    }
                }
                Err(_) => {
                    return Err(
                        "ambiguous submission and authoritative order lookup unavailable"
                            .to_string(),
                    );
                }
            }
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
        let first_error = match self
            .place_spot_market_order(symbol, side, quantity, client_order_id)
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
            .place_spot_market_order(symbol, side, quantity, client_order_id)
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
                        "ambiguous spot market submission remained unresolved after same-ID retry"
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

    pub async fn place_futures_market_order_read_before_retry(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<ReconciledSubmission, String> {
        let first_error = match self
            .place_futures_market_order(symbol, side, quantity, client_order_id, reduce_only)
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
            .place_futures_market_order(symbol, side, quantity, client_order_id, reduce_only)
            .await
        {
            Ok(body) => Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: true,
            }),
            Err(error) if Self::submission_failure_may_have_reached_exchange(&error) => {
                let body = self
                    .get_order_by_client_id(
                        LegVenue::UsdtFutures,
                        symbol,
                        client_order_id,
                    )
                    .await
                    .map_err(|_| {
                        "ambiguous futures market submission remained unresolved after same-ID retry"
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
    }

    #[test]
    fn exchange_info_rate_limit_parser_uses_one_minute_request_weight_only() {
        let payload = serde_json::json!({
            "rateLimits": [
                {"rateLimitType": "ORDERS", "interval": "MINUTE", "intervalNum": 1, "limit": 100},
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "SECOND", "intervalNum": 10, "limit": 50},
                {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 2400}
            ]
        });
        assert_eq!(BinanceRest::request_weight_limit(&payload), Some(2_400));
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
            ApiFailureClass::ServerTransient
        );
        for ambiguous in [
            "order class=transport_ambiguous HTTP transport failed",
            "order class=ambiguous_timeout exchange error -1007",
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
