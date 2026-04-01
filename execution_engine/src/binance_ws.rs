use futures_util::{StreamExt, SinkExt};
use rand::Rng;
use serde::{Deserialize, Serialize};
use std::time::Duration;
use tokio::net::TcpStream;
use tokio::sync::mpsc::Sender;
use tokio::time::sleep;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use tokio_tungstenite::tungstenite::Message;
use tracing::{info, warn, error};

use crate::order_manager::{WsEvent, MarketType};

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

pub struct WsConnectionManager {
    url: String,
    symbol: String,
    state: WsState,
    reconnect_delay_ms: u64,
    event_sender: Sender<WsEvent>,
    consecutive_failures: u32,
    market: MarketType,
    current_volume_minute_ms: Option<i64>,
    current_volume_notional_usd: f64,
}

impl WsConnectionManager {
    pub fn new(url: &str, symbol: &str, event_sender: Sender<WsEvent>, market: MarketType) -> Self {
        Self {
            url: url.to_string(),
            symbol: symbol.to_lowercase(),
            state: WsState::Disconnected,
            reconnect_delay_ms: 1000,
            event_sender,
            consecutive_failures: 0,
            market,
            current_volume_minute_ms: None,
            current_volume_notional_usd: 0.0,
        }
    }

    pub async fn run(&mut self) {
        loop {
            self.state = WsState::Connecting;
            info!("Attempting to connect to {}", self.url);

            match connect_async(&self.url).await {
                Ok((mut ws_stream, _)) => {
                    info!("Successfully connected to Binance WebSocket.");
                    self.state = WsState::Connected;
                    self.consecutive_failures = 0;
                    let _ = self.event_sender.send(WsEvent::Connected { symbol: self.symbol.clone() }).await;
                    self.reconnect_delay_ms = 1000; // reset backoff

                    self.handle_connection(&mut ws_stream).await;
                }
                Err(e) => {
                    self.consecutive_failures += 1;
                    error!("Failed to connect: {}. Retrying in {}ms (attempt #{})",
                        e, self.reconnect_delay_ms, self.consecutive_failures);
                    if self.consecutive_failures > 10 {
                        warn!("SUSPECTED MAINTENANCE: {} consecutive WS failures for {}. Consider extending brain staleness timeout.",
                            self.consecutive_failures, self.symbol);
                    }
                    self.state = WsState::Disconnected;
                    let _ = self.event_sender.send(WsEvent::Disconnected { symbol: self.symbol.clone() }).await;
                }
            }

            // Exponential backoff with jitter (0-50% of delay)
            let jitter = rand::thread_rng().gen_range(0..=(self.reconnect_delay_ms / 2));
            sleep(Duration::from_millis(self.reconnect_delay_ms + jitter)).await;
            self.reconnect_delay_ms = std::cmp::min(self.reconnect_delay_ms * 2, 60_000);
        }
    }

    async fn handle_connection(&mut self, ws_stream: &mut tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>) {
        // Spot subscribes to depth only; perp subscribes to markPrice + bookTicker + depth
        let streams = if self.market == MarketType::Spot {
            vec![format!("{}@depth5@100ms", self.symbol)]
        } else {
            vec![
                format!("{}@markPrice", self.symbol),
                format!("{}@bookTicker", self.symbol),
                format!("{}@depth5@100ms", self.symbol),
                format!("{}@aggTrade", self.symbol),
            ]
        };
        
        let sub_req = serde_json::json!({
            "method": "SUBSCRIBE",
            "params": streams,
            "id": 1
        });
        
        if let Err(e) = ws_stream.send(Message::Text(sub_req.to_string())).await {
            error!("Error sending subscription: {}", e);
            return;
        }

        while let Some(msg_result) = ws_stream.next().await {
            match msg_result {
                Ok(Message::Text(text)) => {
                    // Fast check for serverShutdown
                    if text.contains(r#""e":"serverShutdown""#) {
                        warn!("CRITICAL: Received serverShutdown event from Binance!");
                        self.state = WsState::ShuttingDown;
                        self.handle_server_shutdown(ws_stream).await;
                        break; // Exit connection loop to trigger reconnect
                    }

                    if let Ok(value) = serde_json::from_str::<serde_json::Value>(&text) {
                        let event = value
                            .get("data")
                            .and_then(|d| d.get("e"))
                            .or_else(|| value.get("e"))
                            .and_then(|v| v.as_str())
                            .unwrap_or("");

                        let payload = value.get("data").unwrap_or(&value);

                        if event == "bookTicker" {
                            let symbol = payload.get("s").and_then(|v| v.as_str()).unwrap_or("");
                            let bid_price_str = payload.get("b").and_then(|v| v.as_str()).unwrap_or("0");
                            let ask_price_str = payload.get("a").and_then(|v| v.as_str()).unwrap_or("0");
                            let bid_price = bid_price_str.parse::<f64>().unwrap_or(0.0);
                            let ask_price = ask_price_str.parse::<f64>().unwrap_or(0.0);

                            if !symbol.is_empty() {
                                let _ = self
                                    .event_sender
                                    .send(WsEvent::BookTicker {
                                        symbol: symbol.to_string(),
                                        bid_price,
                                        ask_price,
                                    })
                                    .await;
                            }
                        } else if event == "markPriceUpdate" {
                            let symbol = payload.get("s").and_then(|v| v.as_str()).unwrap_or("");
                            let mark_price = payload.get("p").and_then(|v| v.as_str())
                                .and_then(|s| s.parse::<f64>().ok())
                                .unwrap_or(0.0);
                            // "r" is nextFundingRate — the predicted rate for the upcoming settlement.
                            // Empty string is sent between settlements; treat as 0.0.
                            let next_funding_rate = payload.get("r").and_then(|v| v.as_str())
                                .and_then(|s| s.parse::<f64>().ok())
                                .unwrap_or(0.0);

                            if !symbol.is_empty() && mark_price > 0.0 {
                                let _ = self.event_sender.send(WsEvent::MarkPrice {
                                    symbol: symbol.to_uppercase(),
                                    mark_price,
                                    next_funding_rate,
                                }).await;
                            }
                        } else if event == "aggTrade" && self.market == MarketType::Perp {
                            let symbol = payload.get("s").and_then(|v| v.as_str()).unwrap_or("").to_uppercase();
                            let trade_time_ms = payload.get("T").and_then(|v| v.as_i64()).unwrap_or(0);
                            let price = payload.get("p").and_then(|v| v.as_str())
                                .and_then(|s| s.parse::<f64>().ok())
                                .unwrap_or(0.0);
                            let qty = payload.get("q").and_then(|v| v.as_str())
                                .and_then(|s| s.parse::<f64>().ok())
                                .unwrap_or(0.0);
                            if !symbol.is_empty() && trade_time_ms > 0 && price > 0.0 && qty > 0.0 {
                                let minute_start_ms = trade_time_ms - (trade_time_ms % 60_000);
                                if let Some(current_minute_ms) = self.current_volume_minute_ms {
                                    if minute_start_ms != current_minute_ms {
                                        let _ = self.event_sender.send(WsEvent::VolumeBar {
                                            symbol: symbol.clone(),
                                            minute_start_ms: current_minute_ms,
                                            notional_usd: self.current_volume_notional_usd,
                                        }).await;
                                        self.current_volume_minute_ms = Some(minute_start_ms);
                                        self.current_volume_notional_usd = 0.0;
                                    }
                                } else {
                                    self.current_volume_minute_ms = Some(minute_start_ms);
                                }
                                self.current_volume_notional_usd += price * qty;
                            }
                        } else if let Some((bids_arr, asks_arr)) = Self::extract_depth_arrays(payload) {
                            // Parse partial depth snapshots for both spot and futures:
                            // - Spot raw /ws SUBSCRIBE sends {"lastUpdateId":..., "bids":[...], "asks":[...]}
                            // - Futures partial depth sends {"e":"depthUpdate", ..., "b":[...], "a":[...]}
                            let raw_bids = Self::parse_depth_levels(bids_arr);
                            let raw_asks = Self::parse_depth_levels(asks_arr);

                            let _ = self.event_sender.send(WsEvent::L2Depth {
                                symbol: self.symbol.to_uppercase(),
                                market: self.market,
                                bids: raw_bids,
                                asks: raw_asks,
                            }).await;
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
                    warn!("WebSocket closed by server: {:?}", frame);
                    break;
                }
                Err(e) => {
                    error!("WebSocket error: {}", e);
                    break;
                }
                _ => {}
            }
        }
        
        info!("Connection loop exited. Preparing to reconnect.");
        let _ = self.event_sender.send(WsEvent::Disconnected { symbol: self.symbol.clone() }).await;
    }

    fn extract_depth_arrays<'a>(
        payload: &'a serde_json::Value,
    ) -> Option<(&'a Vec<serde_json::Value>, &'a Vec<serde_json::Value>)> {
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
            ) {
                if let (Ok(price), Ok(qty)) = (price_str.parse::<f64>(), qty_str.parse::<f64>()) {
                    parsed.push([price, qty]);
                }
            }
        }
        parsed
    }

    async fn handle_server_shutdown(&mut self, ws_stream: &mut WebSocketStream<MaybeTlsStream<TcpStream>>) {
        info!("Executing emergency shutdown sequence...");

        // Send Disconnected event to engine — OrderManager will cancel orders via REST
        let _ = self.event_sender.send(WsEvent::Disconnected {
            symbol: self.symbol.clone(),
        }).await;

        // Close the WebSocket gracefully
        let _ = ws_stream.close(None).await;
        info!("Emergency shutdown: WebSocket closed. OrderManager will handle REST cancellations.");
    }
}

#[cfg(test)]
mod tests {
    use super::WsConnectionManager;

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

        let (bids, asks) = WsConnectionManager::extract_depth_arrays(&payload).expect("futures depth");
        let parsed_bids = WsConnectionManager::parse_depth_levels(bids);
        let parsed_asks = WsConnectionManager::parse_depth_levels(asks);

        assert_eq!(parsed_bids, vec![[200.1, 4.0]]);
        assert_eq!(parsed_asks, vec![[200.2, 5.0]]);
    }
}
