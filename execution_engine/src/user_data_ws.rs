use futures_util::{StreamExt, SinkExt};
use std::time::Duration;
use tokio::sync::mpsc::Sender;
use tokio::time::sleep;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::{info, warn, error};
use std::collections::HashMap;

use crate::order_manager::WsEvent;
use crate::binance_rest::BinanceRest;

pub struct UserDataWsManager {
    rest_client: BinanceRest,
    event_sender: Sender<WsEvent>,
    listen_key: Option<String>,
}

impl UserDataWsManager {
    pub fn new(rest_client: BinanceRest, event_sender: Sender<WsEvent>) -> Self {
        Self {
            rest_client,
            event_sender,
            listen_key: None,
        }
    }

    pub async fn run(&mut self) {
        loop {
            // Step 1: Get Listen Key
            if self.listen_key.is_none() {
                match self.rest_client.create_listen_key().await {
                    Ok(res) => {
                        if let Ok(json) = serde_json::from_str::<serde_json::Value>(&res) {
                            if let Some(key) = json.get("listenKey").and_then(|v| v.as_str()) {
                                info!("Obtained new listen key for User Data Stream");
                                self.listen_key = Some(key.to_string());
                            } else {
                                error!("Failed to extract listenKey: {}", res);
                            }
                        }
                    }
                    Err(e) => error!("Failed to create listen key: {}", e),
                }
            }

            let Some(listen_key) = &self.listen_key else {
                sleep(Duration::from_secs(5)).await;
                continue;
            };

            let use_testnet = std::env::var("USE_TESTNET")
                .unwrap_or_else(|_| "true".to_string())
                .to_lowercase() == "true";
            let ws_base = if use_testnet {
                "wss://stream.binancefuture.com"
            } else {
                "wss://fstream.binance.com"
            };
            let ws_url = format!("{}/ws/{}", ws_base, listen_key);
            info!("Connecting to User Data Stream at {}", ws_url);

            let mut heartbeat_interval = tokio::time::interval(Duration::from_secs(30 * 60)); // 30 minutes

            match connect_async(&ws_url).await {
                Ok((mut ws_stream, _)) => {
                    info!("Successfully connected to Binance User Data Stream.");
                    
                    loop {
                        tokio::select! {
                            _ = heartbeat_interval.tick() => {
                                // Keep-alive the listen key via REST
                                if let Some(key) = &self.listen_key {
                                    if let Err(e) = self.rest_client.keepalive_listen_key(key).await {
                                        warn!("Failed to keep-alive listen key: {}", e);
                                    } else {
                                        info!("Successfully kept listen key alive.");
                                    }
                                }
                            }
                            msg_result = ws_stream.next() => {
                                match msg_result {
                                    Some(Ok(Message::Text(text))) => self.handle_message(&text).await,
                                    Some(Ok(Message::Ping(ping_data))) => {
                                        let _ = ws_stream.send(Message::Pong(ping_data)).await;
                                    }
                                    Some(Ok(Message::Close(_))) => {
                                        warn!("User Data WebSocket closed by server");
                                        break;
                                    }
                                    Some(Err(e)) => {
                                        error!("User Data WebSocket error: {}", e);
                                        break;
                                    }
                                    None => break,
                                    _ => {}
                                }
                            }
                        }
                    }
                }
                Err(e) => {
                    error!("Failed to connect User Data Stream: {}", e);
                }
            }
            
            // On disconnect/error, clear listen key to fetch a new one and wait
            self.listen_key = None;
            sleep(Duration::from_secs(5)).await;
        }
    }
    
    async fn handle_message(&self, text: &str) {
        let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else { return; };
        let Some(event_type) = value.get("e").and_then(|v| v.as_str()) else { return; };

        match event_type {
            "ORDER_TRADE_UPDATE" => {
                if let Some(order) = value.get("o") {
                    let client_order_id = order.get("c").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    let symbol = order.get("s").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    let status = order.get("X").and_then(|v| v.as_str()).unwrap_or("").to_string();
                    let filled_qty_str = order.get("z").and_then(|v| v.as_str()).unwrap_or("0");
                    let filled_qty = filled_qty_str.parse::<f64>().unwrap_or(0.0);

                    let _ = self.event_sender.send(WsEvent::OrderUpdate {
                        client_order_id,
                        symbol,
                        status,
                        filled_qty,
                    }).await;
                }
            }
            "executionReport" => {
                let client_order_id = value.get("c").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let symbol = value.get("s").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let status = value.get("X").and_then(|v| v.as_str()).unwrap_or("").to_string();
                let filled_qty_str = value.get("z").and_then(|v| v.as_str()).unwrap_or("0");
                let filled_qty = filled_qty_str.parse::<f64>().unwrap_or(0.0);

                let _ = self.event_sender.send(WsEvent::OrderUpdate {
                    client_order_id,
                    symbol,
                    status,
                    filled_qty,
                }).await;
            }
            "ACCOUNT_UPDATE" => {
                if let Some(update_data) = value.get("a") {
                    if let Some(balances_arr) = update_data.get("B").and_then(|v| v.as_array()) {
                        let mut parsed_balances = HashMap::new();
                        for b in balances_arr {
                            if let (Some(asset), Some(wb)) = (b.get("a").and_then(|v| v.as_str()), b.get("wb").and_then(|v| v.as_str())) {
                                if let Ok(wallet_balance) = wb.parse::<f64>() {
                                    parsed_balances.insert(asset.to_string(), wallet_balance);
                                }
                            }
                        }
                        let _ = self.event_sender.send(WsEvent::AccountUpdate {
                            balances: parsed_balances,
                        }).await;
                    }
                }
            }
            "outboundAccountPosition" => {
                if let Some(balances_arr) = value.get("B").and_then(|v| v.as_array()) {
                    let mut parsed_balances = HashMap::new();
                    for b in balances_arr {
                        if let (Some(asset), Some(f)) = (b.get("a").and_then(|v| v.as_str()), b.get("f").and_then(|v| v.as_str())) {
                            if let Ok(free_balance) = f.parse::<f64>() {
                                parsed_balances.insert(asset.to_string(), free_balance);
                            }
                        }
                    }
                    let _ = self.event_sender.send(WsEvent::AccountUpdate {
                        balances: parsed_balances,
                    }).await;
                }
            }
            "listenKeyExpired" => {
                info!("Listen key expired event received");
            }
            _ => {}
        }
    }
}
