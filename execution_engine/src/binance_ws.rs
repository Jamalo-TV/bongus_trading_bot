use futures_util::{StreamExt, SinkExt};
use serde::{Deserialize, Serialize};
use std::time::Duration;
use tokio::net::TcpStream;
use tokio::sync::mpsc::Sender;
use tokio::time::sleep;
use tokio_tungstenite::{connect_async, MaybeTlsStream, WebSocketStream};
use tokio_tungstenite::tungstenite::Message;
use tracing::{info, warn, error};

use crate::order_manager::WsEvent;

const BINANCE_WS_URL: &str = "wss://stream.binance.com:9443/ws";

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
}

impl WsConnectionManager {
    pub fn new(url: &str, symbol: &str, event_sender: Sender<WsEvent>) -> Self {
        Self {
            url: url.to_string(),
            symbol: symbol.to_lowercase(),
            state: WsState::Disconnected,
            reconnect_delay_ms: 1000,
            event_sender,
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
                    let _ = self.event_sender.send(WsEvent::Connected { symbol: self.symbol.clone() }).await;
                    self.reconnect_delay_ms = 1000; // reset backoff

                    self.handle_connection(&mut ws_stream).await;
                }
                Err(e) => {
                    error!("Failed to connect: {}. Retrying in {}ms", e, self.reconnect_delay_ms);
                    self.state = WsState::Disconnected;
                    let _ = self.event_sender.send(WsEvent::Disconnected { symbol: self.symbol.clone() }).await;
                }
            }

            // Exponential backoff
            sleep(Duration::from_millis(self.reconnect_delay_ms)).await;
            self.reconnect_delay_ms = std::cmp::min(self.reconnect_delay_ms * 2, 60_000);
        }
    }

    async fn handle_connection(&mut self, ws_stream: &mut tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>) {
        // Subscribe to markPrice, bookTicker AND depth@100ms for Level 2 Maker execution & OBI
        let streams = vec![
            format!("{}@markPrice", self.symbol),
            format!("{}@bookTicker", self.symbol),
            format!("{}@depth5@100ms", self.symbol),
        ];
        
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
                            let mark_price = payload.get("p").and_then(|v| v.as_str()).unwrap_or("0");
                            // info!("markPrice update received: symbol={} mark_price={}", symbol, mark_price);
                        } else if value.get("stream").and_then(|s| s.as_str()).unwrap_or("").contains("@depth") {
                            // Parse partial depth stream
                            let bids_arr = payload.get("bids").and_then(|v| v.as_array());
                            let asks_arr = payload.get("asks").and_then(|v| v.as_array());
                            
                            if let (Some(b_arr), Some(a_arr)) = (bids_arr, asks_arr) {
                                let mut raw_bids = Vec::new();
                                for b in b_arr {
                                    if let (Some(price_str), Some(qty_str)) = (b.get(0).and_then(|v| v.as_str()), b.get(1).and_then(|v| v.as_str())) {
                                        if let (Ok(p), Ok(q)) = (price_str.parse::<f64>(), qty_str.parse::<f64>()) {
                                            raw_bids.push((p, q));
                                        }
                                    }
                                }

                                let mut raw_asks = Vec::new();
                                for a in a_arr {
                                    if let (Some(price_str), Some(qty_str)) = (a.get(0).and_then(|v| v.as_str()), a.get(1).and_then(|v| v.as_str())) {
                                        if let (Ok(p), Ok(q)) = (price_str.parse::<f64>(), qty_str.parse::<f64>()) {
                                            raw_asks.push((p, q));
                                        }
                                    }
                                }

                                let _ = self.event_sender.send(WsEvent::L2Depth {
                                    symbol: self.symbol.to_uppercase(),
                                    bids: raw_bids,
                                    asks: raw_asks,
                                }).await;
                            }
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

    async fn handle_server_shutdown(&mut self, ws_stream: &mut WebSocketStream<MaybeTlsStream<TcpStream>>) {
        info!("Executing emergency shutdown sequence...");
        // 1. Halt new order submissions (broadcast to other parts of system)
        // 2. Dispatch emergency cancelation requests via WS if possible
        
        let cancel_all_req = serde_json::json!({
            "method": "SERVER_SHUTDOWN_EMERGENCY_CANCEL",
            "params": []
        });

        info!("Sending emergency order cancelations...");
        let _ = ws_stream.send(Message::Text(cancel_all_req.to_string())).await;
        
        // Brief delay to allow cancelations to hopefully transmit before socket completely dies
        sleep(Duration::from_millis(100)).await;
        
        let _ = ws_stream.close(None).await;
        info!("Emergency shutdown sequence complete. Socket closed.");
    }
}
