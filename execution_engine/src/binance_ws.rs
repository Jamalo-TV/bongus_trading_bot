use futures_util::{SinkExt, StreamExt};
use rand::Rng;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::net::TcpStream;
use tokio::sync::mpsc::Sender;
use tokio::time::sleep;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::{MaybeTlsStream, WebSocketStream, connect_async};
use tracing::{error, info, warn};

use crate::order_manager::{MarketType, WsEvent, WsStreamType};

static CONNECTION_SEQUENCE: AtomicU64 = AtomicU64::new(1);
const MAX_EXECUTION_EVENT_AGE_MS: i64 = 3_000;
const MAX_FUTURE_CLOCK_SKEW_MS: i64 = 1_000;

#[allow(dead_code)]
#[derive(Debug, Deserialize, Serialize)]
pub struct ServerShutdownEvent {
    pub e: String, // "serverShutdown"
}

#[derive(Debug)]
pub enum WsState {
    Disconnected,
    Connecting,
    Connected,
    ShuttingDown,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum WsFeedKind {
    SpotPublic,
    FuturesPublic,
    FuturesMarket,
}

impl WsFeedKind {
    fn market(self) -> MarketType {
        match self {
            Self::SpotPublic => MarketType::Spot,
            Self::FuturesPublic | Self::FuturesMarket => MarketType::Perp,
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::SpotPublic => "spot-public",
            Self::FuturesPublic => "futures-public",
            Self::FuturesMarket => "futures-market",
        }
    }

    fn subscription_streams(self, symbol: &str) -> Vec<String> {
        match self {
            Self::SpotPublic => vec![format!("{symbol}@depth20@100ms")],
            Self::FuturesPublic => vec![
                format!("{symbol}@bookTicker"),
                format!("{symbol}@depth20@100ms"),
            ],
            Self::FuturesMarket => {
                vec![format!("{symbol}@markPrice"), format!("{symbol}@aggTrade")]
            }
        }
    }

    fn semantic_requirements(self) -> &'static [&'static str] {
        match self {
            Self::SpotPublic => &["depth"],
            Self::FuturesPublic => &["bookTicker", "depth"],
            Self::FuturesMarket => &["markPrice", "aggTrade"],
        }
    }
}

pub struct WsConnectionManager {
    url: String,
    symbol: String,
    state: WsState,
    reconnect_delay_ms: u64,
    event_sender: Sender<WsEvent>,
    consecutive_failures: u32,
    feed_kind: WsFeedKind,
    current_volume_minute_ms: Option<i64>,
    current_volume_notional_usd: f64,
    planned_connection_max_age: Duration,
    last_depth_update_id: Option<u64>,
}

impl WsConnectionManager {
    pub fn new(
        url: &str,
        symbol: &str,
        event_sender: Sender<WsEvent>,
        feed_kind: WsFeedKind,
        planned_connection_max_age_seconds: u64,
    ) -> Self {
        Self {
            url: url.to_string(),
            symbol: symbol.to_lowercase(),
            state: WsState::Disconnected,
            reconnect_delay_ms: 1000,
            event_sender,
            consecutive_failures: 0,
            feed_kind,
            current_volume_minute_ms: None,
            current_volume_notional_usd: 0.0,
            planned_connection_max_age: Duration::from_secs(planned_connection_max_age_seconds),
            last_depth_update_id: None,
        }
    }

    fn current_time_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis() as i64
    }

    fn next_connection_id(&self) -> String {
        format!(
            "{}-{}-{}-{}",
            self.feed_kind.as_str(),
            self.symbol,
            Self::current_time_ms(),
            CONNECTION_SEQUENCE.fetch_add(1, Ordering::Relaxed)
        )
    }

    pub async fn run(&mut self) {
        loop {
            self.state = WsState::Connecting;
            info!("Attempting to connect to {}", self.url);

            let connect_result =
                tokio::time::timeout(Duration::from_secs(15), connect_async(&self.url)).await;
            match connect_result {
                Ok(Ok((mut ws_stream, _))) => {
                    info!("Successfully connected to Binance WebSocket.");
                    let connection_id = self.next_connection_id();
                    self.last_depth_update_id = None;
                    let became_semantically_ready =
                        self.handle_connection(&mut ws_stream, &connection_id).await;
                    if became_semantically_ready {
                        self.consecutive_failures = 0;
                        self.reconnect_delay_ms = 1000;
                    } else {
                        self.consecutive_failures = self.consecutive_failures.saturating_add(1);
                    }
                }
                other => {
                    self.consecutive_failures += 1;
                    let err_msg = match other {
                        Ok(Err(e)) => e.to_string(),
                        Err(_) => "Connection attempt timed out (15s)".to_string(),
                        _ => unreachable!(),
                    };
                    error!(
                        "Failed to connect: {}. Retrying in {}ms (attempt #{})",
                        err_msg, self.reconnect_delay_ms, self.consecutive_failures
                    );
                    if self.consecutive_failures > 10 {
                        warn!(
                            "SUSPECTED MAINTENANCE: {} consecutive WS failures for {}. Consider extending brain staleness timeout.",
                            self.consecutive_failures, self.symbol
                        );
                    }
                    self.state = WsState::Disconnected;
                    let _ = self
                        .event_sender
                        .send(WsEvent::Disconnected {
                            symbol: self.symbol.clone(),
                            stream_type: WsStreamType::MarketData,
                            connection_id: None,
                            connection_role: Some(self.feed_kind.as_str().to_string()),
                        })
                        .await;
                }
            }

            // Exponential backoff with jitter (0-50% of delay)
            let jitter = rand::thread_rng().gen_range(0..=(self.reconnect_delay_ms / 2));
            sleep(Duration::from_millis(self.reconnect_delay_ms + jitter)).await;
            self.reconnect_delay_ms = std::cmp::min(self.reconnect_delay_ms * 2, 60_000);
        }
    }

    async fn handle_connection(
        &mut self,
        ws_stream: &mut tokio_tungstenite::WebSocketStream<
            tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
        >,
        connection_id: &str,
    ) -> bool {
        // Binance USD-M routes high-frequency public streams and regular
        // market streams through distinct sessions. Mixing them on one
        // unrouted connection silently drops market-category events.
        let streams = self.feed_kind.subscription_streams(&self.symbol);

        let sub_req = serde_json::json!({
            "method": "SUBSCRIBE",
            "params": streams,
            "id": 1
        });

        if let Err(e) = ws_stream.send(Message::Text(sub_req.to_string())).await {
            error!("Error sending subscription: {}", e);
            return false;
        }

        let connection_started_at = std::time::Instant::now();
        let mut semantic_message_times = HashMap::<&'static str, std::time::Instant>::new();
        let mut subscription_acked = false;
        let mut readiness_emitted = false;
        let mut ping_interval = tokio::time::interval(Duration::from_secs(30));
        ping_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let mut check_interval = tokio::time::interval(Duration::from_secs(10));
        check_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
        let planned_renewal = tokio::time::sleep(self.planned_connection_max_age);
        tokio::pin!(planned_renewal);

        loop {
            tokio::select! {
                _ = &mut planned_renewal => {
                    info!(
                        "Planned public WebSocket renewal for {} before Binance's 24-hour limit",
                        self.symbol
                    );
                    break;
                }
                _ = ping_interval.tick() => {
                    if let Err(e) = ws_stream.send(Message::Ping(vec![])).await {
                        error!("Failed to send Ping: {}", e);
                        break;
                    }
                }
                _ = check_interval.tick() => {
                    let semantic_stale = connection_started_at.elapsed() > Duration::from_secs(60)
                        && self.feed_kind.semantic_requirements().iter().any(|requirement| {
                            semantic_message_times
                                .get(requirement)
                                .map(|last| last.elapsed() > Duration::from_secs(60))
                                .unwrap_or(true)
                        });
                    if semantic_stale {
                        warn!("WebSocket semantic-data timeout (required market message missing/stale for 60s) for {} ({}). Reconnecting.", self.symbol, self.feed_kind.as_str());
                        break;
                    }
                }
                msg_opt = ws_stream.next() => {
                    let msg_result = match msg_opt {
                        Some(res) => res,
                        None => {
                            warn!("WebSocket stream ended for {}", self.symbol);
                            break;
                        }
                    };

                    match msg_result {
                        Ok(Message::Text(text)) => {
                            let receive_time_ms = Self::current_time_ms();
                            // Fast check for serverShutdown
                            if text.contains(r#""e":"serverShutdown""#) {
                                warn!("CRITICAL: Received serverShutdown event from Binance!");
                                self.state = WsState::ShuttingDown;
                                self.handle_server_shutdown(ws_stream).await;
                                break; // Exit connection loop to trigger reconnect
                            }

                            if let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) {
                                match Self::subscription_acknowledged(&value) {
                                    Ok(true) => subscription_acked = true,
                                    Ok(false) => {}
                                    Err(()) => {
                                        warn!(
                                            "Binance rejected or malformed the public subscription for {}",
                                            self.symbol
                                        );
                                        break;
                                    }
                                }
                                let mut valid_semantic_kind: Option<&'static str> = None;
                                let event = value
                                    .get("data")
                                    .and_then(|d| d.get("e"))
                                    .or_else(|| value.get("e"))
                                    .and_then(|v| v.as_str())
                                    .unwrap_or("");

                                let payload = value.get("data").unwrap_or(&value);

                                if event == "bookTicker"
                                    && self.feed_kind == WsFeedKind::FuturesPublic
                                {
                                    let symbol = payload.get("s").and_then(|v| v.as_str()).unwrap_or("");
                                    let bid_price_str =
                                        payload.get("b").and_then(|v| v.as_str()).unwrap_or("0");
                                    let ask_price_str =
                                        payload.get("a").and_then(|v| v.as_str()).unwrap_or("0");
                                    let bid_price = bid_price_str.parse::<f64>().unwrap_or(0.0);
                                    let ask_price = ask_price_str.parse::<f64>().unwrap_or(0.0);

                                    let exchange_event_time_ms = payload
                                        .get("E")
                                        .and_then(|node| node.as_i64());
                                    if symbol.eq_ignore_ascii_case(&self.symbol)
                                        && bid_price.is_finite()
                                        && ask_price.is_finite()
                                        && bid_price > 0.0
                                        && ask_price >= bid_price
                                        && Self::event_time_is_current(
                                            exchange_event_time_ms,
                                            receive_time_ms,
                                            false,
                                        )
                                    {
                                        let process_time_ms = Self::current_time_ms();
                                        let _ = self
                                            .event_sender
                                            .send(WsEvent::BookTicker {
                                                symbol: symbol.to_string(),
                                                bid_price,
                                                ask_price,
                                                connection_id: connection_id.to_string(),
                                                exchange_event_time_ms,
                                                receive_time_ms,
                                                process_time_ms,
                                                persist_time_ms: None,
                                            })
                                            .await;
                                        valid_semantic_kind = Some("bookTicker");
                                    }
                                } else if event == "markPriceUpdate"
                                    && self.feed_kind == WsFeedKind::FuturesMarket
                                {
                                    let symbol = payload.get("s").and_then(|v| v.as_str()).unwrap_or("");
                                    let mark_price = payload
                                        .get("p")
                                        .and_then(|v| v.as_str())
                                        .and_then(|s| s.parse::<f64>().ok())
                                        .unwrap_or(0.0);
                                    // "r" is nextFundingRate — the predicted rate for the upcoming settlement.
                                    // Empty string is sent between settlements; treat as 0.0.
                                    let next_funding_rate = payload
                                        .get("r")
                                        .and_then(|v| v.as_str())
                                        .and_then(|s| s.parse::<f64>().ok())
                                        .unwrap_or(0.0);
                                    // "T" is the exact next funding settlement
                                    // time in epoch milliseconds.
                                    let next_funding_time_ms = payload
                                        .get("T")
                                        .and_then(|value| value.as_i64())
                                        .unwrap_or(0);

                                    let exchange_event_time_ms = payload
                                        .get("E")
                                        .and_then(|node| node.as_i64());
                                    if symbol.eq_ignore_ascii_case(&self.symbol)
                                        && mark_price.is_finite()
                                        && mark_price > 0.0
                                        && next_funding_rate.is_finite()
                                        && next_funding_time_ms > 0
                                        && Self::event_time_is_current(
                                            exchange_event_time_ms,
                                            receive_time_ms,
                                            false,
                                        )
                                    {
                                        let process_time_ms = Self::current_time_ms();
                                        let _ = self
                                            .event_sender
                                            .send(WsEvent::MarkPrice {
                                                symbol: symbol.to_uppercase(),
                                                mark_price,
                                                next_funding_rate,
                                                next_funding_time_ms,
                                                connection_id: connection_id.to_string(),
                                                exchange_event_time_ms,
                                                receive_time_ms,
                                                process_time_ms,
                                                persist_time_ms: None,
                                            })
                                            .await;
                                        valid_semantic_kind = Some("markPrice");
                                    }
                                } else if event == "aggTrade"
                                    && self.feed_kind == WsFeedKind::FuturesMarket
                                {
                                    let symbol = payload
                                        .get("s")
                                        .and_then(|v| v.as_str())
                                        .unwrap_or("")
                                        .to_uppercase();
                                    let trade_time_ms =
                                        payload.get("T").and_then(|v| v.as_i64()).unwrap_or(0);
                                    let price = payload
                                        .get("p")
                                        .and_then(|v| v.as_str())
                                        .and_then(|s| s.parse::<f64>().ok())
                                        .unwrap_or(0.0);
                                    let qty = payload
                                        .get("q")
                                        .and_then(|v| v.as_str())
                                        .and_then(|s| s.parse::<f64>().ok())
                                        .unwrap_or(0.0);
                                    let exchange_event_time_ms = payload
                                        .get("E")
                                        .and_then(|node| node.as_i64())
                                        .or(Some(trade_time_ms));
                                    if symbol.eq_ignore_ascii_case(&self.symbol)
                                        && trade_time_ms > 0
                                        && price.is_finite()
                                        && price > 0.0
                                        && qty.is_finite()
                                        && qty > 0.0
                                        && Self::event_time_is_current(
                                            exchange_event_time_ms,
                                            receive_time_ms,
                                            false,
                                        )
                                    {
                                        valid_semantic_kind = Some("aggTrade");
                                        let minute_start_ms = trade_time_ms - (trade_time_ms % 60_000);
                                        if let Some(current_minute_ms) = self.current_volume_minute_ms {
                                            if minute_start_ms != current_minute_ms {
                                                let process_time_ms = Self::current_time_ms();
                                                let _ = self
                                                    .event_sender
                                                    .send(WsEvent::VolumeBar {
                                                        symbol: symbol.clone(),
                                                        minute_start_ms: current_minute_ms,
                                                        notional_usd: self.current_volume_notional_usd,
                                                        connection_id: connection_id.to_string(),
                                                        exchange_event_time_ms,
                                                        receive_time_ms,
                                                        process_time_ms,
                                                        persist_time_ms: None,
                                                    })
                                                    .await;
                                                self.current_volume_minute_ms = Some(minute_start_ms);
                                                self.current_volume_notional_usd = 0.0;
                                            }
                                        } else {
                                            self.current_volume_minute_ms = Some(minute_start_ms);
                                        }
                                        self.current_volume_notional_usd += price * qty;
                                    }
                                } else if let Some((bids_arr, asks_arr)) =
                                    Self::extract_depth_arrays(payload)
                                {
                                    // Parse partial depth snapshots for both spot and futures:
                                    // - Spot raw /ws SUBSCRIBE sends {"lastUpdateId":..., "bids":[...], "asks":[...]}
                                    // - Futures partial depth sends {"e":"depthUpdate", ..., "b":[...], "a":[...]}
                                    let raw_bids = Self::parse_depth_levels(bids_arr);
                                    let raw_asks = Self::parse_depth_levels(asks_arr);
                                    let snapshot_update_id = payload
                                        .get("lastUpdateId")
                                        .and_then(|value| value.as_u64());
                                    let first_update_id = payload
                                        .get("U")
                                        .and_then(|value| value.as_u64())
                                        .or(snapshot_update_id);
                                    let final_update_id = payload
                                        .get("u")
                                        .and_then(|value| value.as_u64())
                                        .or(snapshot_update_id);
                                    let previous_final_update_id = payload
                                        .get("pu")
                                        .and_then(|value| value.as_u64());
                                    let payload_symbol = payload.get("s").and_then(|v| v.as_str());
                                    let symbol_matches = match self.feed_kind {
                                        WsFeedKind::SpotPublic => payload_symbol
                                            .map(|symbol| symbol.eq_ignore_ascii_case(&self.symbol))
                                            .unwrap_or(true),
                                        WsFeedKind::FuturesPublic => payload_symbol
                                            .is_some_and(|symbol| symbol.eq_ignore_ascii_case(&self.symbol)),
                                        WsFeedKind::FuturesMarket => false,
                                    };
                                    let exchange_event_time_ms = payload
                                        .get("E")
                                        .and_then(|node| node.as_i64());
                                    let event_time_current = Self::event_time_is_current(
                                        exchange_event_time_ms,
                                        receive_time_ms,
                                        self.feed_kind == WsFeedKind::SpotPublic,
                                    );

                                    // Both subscriptions created by this manager use
                                    // `<symbol>@depth20`, Binance's authoritative
                                    // partial-book snapshot stream. Futures labels the
                                    // payload `depthUpdate`, but each message still
                                    // replaces the complete top-20 view; it must not be
                                    // validated as a diff-book sequence.
                                    let is_partial_book_snapshot = true;
                                    if symbol_matches
                                        && event_time_current
                                        && !raw_bids.is_empty()
                                        && !raw_asks.is_empty()
                                        && final_update_id.is_some()
                                    {
                                        let sequence_contiguous = if snapshot_update_id.is_some() {
                                            self.last_depth_update_id
                                                .map(|last| {
                                                    final_update_id
                                                        .is_some_and(|final_id| final_id > last)
                                                })
                                                .unwrap_or(true)
                                        } else {
                                            Self::depth_sequence_contiguous(
                                                self.last_depth_update_id,
                                                first_update_id,
                                                final_update_id,
                                                previous_final_update_id,
                                            )
                                        };
                                        self.last_depth_update_id = final_update_id;
                                        let process_time_ms = Self::current_time_ms();
                                        let _ = self
                                            .event_sender
                                            .send(WsEvent::L2Depth {
                                                symbol: self.symbol.to_uppercase(),
                                                market: self.feed_kind.market(),
                                                bids: raw_bids,
                                                asks: raw_asks,
                                                first_update_id,
                                                final_update_id,
                                                previous_final_update_id,
                                                is_snapshot: is_partial_book_snapshot,
                                                sequence_contiguous,
                                                connection_id: connection_id.to_string(),
                                                exchange_event_time_ms,
                                                receive_time_ms,
                                                process_time_ms,
                                                persist_time_ms: None,
                                            })
                                            .await;
                                        valid_semantic_kind = Some("depth");
                                    }
                                }

                                if let Some(kind) = valid_semantic_kind {
                                    semantic_message_times.insert(kind, std::time::Instant::now());
                                }
                                if !readiness_emitted
                                    && subscription_acked
                                    && self.feed_kind.semantic_requirements().iter().all(|kind| {
                                        semantic_message_times.contains_key(kind)
                                    })
                                {
                                    self.state = WsState::Connected;
                                    let _ = self
                                        .event_sender
                                        .send(WsEvent::Connected {
                                            symbol: self.symbol.clone(),
                                            stream_type: WsStreamType::MarketData,
                                            connection_id: Some(connection_id.to_string()),
                                            connection_role: Some(self.feed_kind.as_str().to_string()),
                                        })
                                        .await;
                                    readiness_emitted = true;
                                }
                            }
                        }
                        Ok(Message::Ping(ping_data)) => {
                            // Auto-reply with Pong
                            if let Err(e) = ws_stream.send(Message::Pong(ping_data)).await {
                                error!("Failed to send Pong: {}", e);
                                break;
                            }
                        }
                        Ok(Message::Close(frame)) => {
                            warn!("WebSocket closed by server for {}: {:?}", self.symbol, frame);
                            break;
                        }
                        Err(e) => {
                            error!("WebSocket error for {}: {}", self.symbol, e);
                            break;
                        }
                        _ => {}
                    }
                }
            }
        }

        info!(
            "Connection loop exited for {}. Preparing to reconnect.",
            self.symbol
        );
        let _ = self
            .event_sender
            .send(WsEvent::Disconnected {
                symbol: self.symbol.clone(),
                stream_type: WsStreamType::MarketData,
                connection_id: Some(connection_id.to_string()),
                connection_role: Some(self.feed_kind.as_str().to_string()),
            })
            .await;
        self.state = WsState::Disconnected;
        readiness_emitted
    }

    fn depth_sequence_contiguous(
        last_final_update_id: Option<u64>,
        first_update_id: Option<u64>,
        final_update_id: Option<u64>,
        previous_final_update_id: Option<u64>,
    ) -> bool {
        let Some(final_update_id) = final_update_id else {
            return false;
        };
        let Some(last_final_update_id) = last_final_update_id else {
            return first_update_id
                .map(|first| first <= final_update_id)
                .unwrap_or(true);
        };
        if final_update_id <= last_final_update_id {
            return false;
        }
        if let Some(previous_final_update_id) = previous_final_update_id {
            return previous_final_update_id == last_final_update_id;
        }
        first_update_id
            .map(|first| first <= last_final_update_id.saturating_add(1))
            // Spot partial-book snapshots carry only lastUpdateId. Each frame
            // replaces the complete top-N view, so monotonicity is sufficient.
            .unwrap_or(true)
    }

    fn event_time_is_current(
        exchange_event_time_ms: Option<i64>,
        receive_time_ms: i64,
        allow_missing: bool,
    ) -> bool {
        let Some(exchange_event_time_ms) = exchange_event_time_ms else {
            return allow_missing;
        };
        exchange_event_time_ms > 0
            && exchange_event_time_ms >= receive_time_ms - MAX_EXECUTION_EVENT_AGE_MS
            && exchange_event_time_ms <= receive_time_ms + MAX_FUTURE_CLOCK_SKEW_MS
    }

    fn subscription_acknowledged(value: &serde_json::Value) -> Result<bool, ()> {
        if value.get("id").and_then(|node| node.as_u64()) != Some(1) {
            return Ok(false);
        }
        if value.get("result").is_some_and(serde_json::Value::is_null) {
            Ok(true)
        } else {
            Err(())
        }
    }

    fn extract_depth_arrays(
        payload: &serde_json::Value,
    ) -> Option<(&Vec<serde_json::Value>, &Vec<serde_json::Value>)> {
        if let (Some(bids), Some(asks)) = (
            payload.get("bids").and_then(|v| v.as_array()),
            payload.get("asks").and_then(|v| v.as_array()),
        ) {
            return Some((bids, asks));
        }

        if let (Some(bids), Some(asks)) = (
            payload.get("b").and_then(|v| v.as_array()),
            payload.get("a").and_then(|v| v.as_array()),
        ) {
            return Some((bids, asks));
        }

        None
    }

    fn parse_depth_levels(levels: &[serde_json::Value]) -> Vec<[f64; 2]> {
        let mut parsed = Vec::new();
        for level in levels {
            if let (Some(price_str), Some(qty_str)) = (
                level.get(0).and_then(|v| v.as_str()),
                level.get(1).and_then(|v| v.as_str()),
            ) && let (Ok(price), Ok(qty)) = (price_str.parse::<f64>(), qty_str.parse::<f64>())
                && price.is_finite()
                && qty.is_finite()
                && price > 0.0
                && qty > 0.0
            {
                parsed.push([price, qty]);
            }
        }
        parsed
    }

    async fn handle_server_shutdown(
        &mut self,
        ws_stream: &mut WebSocketStream<MaybeTlsStream<TcpStream>>,
    ) {
        info!("Executing emergency shutdown sequence...");

        // Close the WebSocket gracefully
        let _ = ws_stream.close(None).await;
        info!(
            "Emergency shutdown: WebSocket closed; the connection loop will emit one feed-scoped disconnect."
        );
    }
}

#[cfg(test)]
mod tests {
    use super::{WsConnectionManager, WsFeedKind};
    use crate::order_manager::{MarketType, WsEvent};

    #[test]
    fn extract_depth_arrays_supports_spot_shape() {
        let payload = serde_json::json!({
            "lastUpdateId": 1,
            "bids": [["100.1", "2.0"]],
            "asks": [["100.2", "3.0"]],
        });

        let (bids, asks) = WsConnectionManager::extract_depth_arrays(&payload).expect("spot depth");
        let parsed_bids = WsConnectionManager::parse_depth_levels(bids);
        let parsed_asks = WsConnectionManager::parse_depth_levels(asks);

        assert_eq!(parsed_bids, vec![[100.1, 2.0]]);
        assert_eq!(parsed_asks, vec![[100.2, 3.0]]);
    }

    #[test]
    fn extract_depth_arrays_supports_futures_shape() {
        let payload = serde_json::json!({
            "e": "depthUpdate",
            "b": [["200.1", "4.0"]],
            "a": [["200.2", "5.0"]],
        });

        let (bids, asks) =
            WsConnectionManager::extract_depth_arrays(&payload).expect("futures depth");
        let parsed_bids = WsConnectionManager::parse_depth_levels(bids);
        let parsed_asks = WsConnectionManager::parse_depth_levels(asks);

        assert_eq!(parsed_bids, vec![[200.1, 4.0]]);
        assert_eq!(parsed_asks, vec![[200.2, 5.0]]);
    }

    #[test]
    fn depth_levels_reject_nonfinite_and_nonpositive_values() {
        let levels = serde_json::json!([
            ["100.0", "2.0"],
            ["NaN", "1.0"],
            ["Infinity", "1.0"],
            ["99.0", "0"],
            ["98.0", "-1.0"],
            ["0", "1.0"]
        ]);
        let parsed =
            WsConnectionManager::parse_depth_levels(levels.as_array().expect("depth-level array"));
        assert_eq!(parsed, vec![[100.0, 2.0]]);
    }

    #[test]
    fn execution_event_age_rejects_replays_and_excessive_future_skew() {
        assert!(WsConnectionManager::event_time_is_current(
            Some(99_000),
            100_000,
            false
        ));
        assert!(!WsConnectionManager::event_time_is_current(
            Some(95_000),
            100_000,
            false
        ));
        assert!(!WsConnectionManager::event_time_is_current(
            Some(102_000),
            100_000,
            false
        ));
        assert!(WsConnectionManager::event_time_is_current(
            None, 100_000, true
        ));
        assert!(!WsConnectionManager::event_time_is_current(
            None, 100_000, false
        ));
    }

    #[test]
    fn each_feed_requires_every_subscribed_semantic_category() {
        assert_eq!(WsFeedKind::SpotPublic.semantic_requirements(), &["depth"]);
        assert_eq!(
            WsFeedKind::FuturesPublic.semantic_requirements(),
            &["bookTicker", "depth"]
        );
        assert_eq!(
            WsFeedKind::FuturesMarket.semantic_requirements(),
            &["markPrice", "aggTrade"]
        );
    }

    #[test]
    fn subscription_readiness_requires_the_exact_ack_shape() {
        assert_eq!(
            WsConnectionManager::subscription_acknowledged(&serde_json::json!({
                "result": null,
                "id": 1
            })),
            Ok(true)
        );
        assert_eq!(
            WsConnectionManager::subscription_acknowledged(&serde_json::json!({
                "event": "bookTicker",
                "id": 99
            })),
            Ok(false)
        );
        assert_eq!(
            WsConnectionManager::subscription_acknowledged(&serde_json::json!({
                "code": 2,
                "msg": "Invalid request",
                "id": 1
            })),
            Err(())
        );
    }

    #[test]
    fn futures_depth_continuity_uses_previous_final_update_id() {
        assert!(WsConnectionManager::depth_sequence_contiguous(
            None,
            Some(10),
            Some(12),
            None
        ));
        assert!(WsConnectionManager::depth_sequence_contiguous(
            Some(12),
            Some(13),
            Some(15),
            Some(12)
        ));
        assert!(!WsConnectionManager::depth_sequence_contiguous(
            Some(12),
            Some(14),
            Some(15),
            Some(13)
        ));
    }

    #[test]
    fn public_depth_messagepack_contains_the_timing_and_continuity_envelope() {
        let event = WsEvent::L2Depth {
            symbol: "BTCUSDT".to_string(),
            market: MarketType::Perp,
            bids: vec![[100.0, 1.0]],
            asks: vec![[101.0, 1.0]],
            first_update_id: Some(10),
            final_update_id: Some(12),
            previous_final_update_id: Some(9),
            is_snapshot: true,
            sequence_contiguous: true,
            connection_id: "perp-btcusdt-1".to_string(),
            exchange_event_time_ms: Some(1000),
            receive_time_ms: 1001,
            process_time_ms: 1002,
            persist_time_ms: None,
        };
        let encoded = rmp_serde::to_vec_named(&event).unwrap();
        let decoded: serde_json::Value = rmp_serde::from_slice(&encoded).unwrap();
        assert_eq!(decoded["connection_id"], "perp-btcusdt-1");
        assert_eq!(decoded["exchange_event_time_ms"], 1000);
        assert_eq!(decoded["receive_time_ms"], 1001);
        assert_eq!(decoded["process_time_ms"], 1002);
        assert_eq!(decoded["sequence_contiguous"], true);
    }

    #[test]
    fn usd_m_public_and_market_streams_never_share_a_session() {
        let public = WsFeedKind::FuturesPublic.subscription_streams("btcusdt");
        let market = WsFeedKind::FuturesMarket.subscription_streams("btcusdt");
        assert_eq!(public, vec!["btcusdt@bookTicker", "btcusdt@depth20@100ms"]);
        assert_eq!(market, vec!["btcusdt@markPrice", "btcusdt@aggTrade"]);
        assert!(public.iter().all(|stream| !market.contains(stream)));
    }
}
