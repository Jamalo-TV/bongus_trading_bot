use futures_util::{SinkExt, StreamExt};
use std::collections::HashMap;
use std::time::Duration;
use tokio::sync::mpsc::Sender;
use tokio::time::sleep;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::{error, info, warn};

use crate::binance_rest::BinanceRest;
use crate::order_manager::{WsEvent, WsStreamType};

#[derive(Debug, Clone, Copy)]
pub enum UserDataStreamKind {
    Spot,
    Futures,
}

impl UserDataStreamKind {
    fn as_str(&self) -> &'static str {
        match self {
            UserDataStreamKind::Spot => "spot",
            UserDataStreamKind::Futures => "futures",
        }
    }
}

pub struct UserDataWsManager {
    rest_client: BinanceRest,
    event_sender: Sender<WsEvent>,
    stream_kind: UserDataStreamKind,
    listen_key: Option<String>,
}

impl UserDataWsManager {
    pub fn new(
        rest_client: BinanceRest,
        event_sender: Sender<WsEvent>,
        stream_kind: UserDataStreamKind,
    ) -> Self {
        Self {
            rest_client,
            event_sender,
            stream_kind,
            listen_key: None,
        }
    }

    pub async fn run(&mut self) {
        loop {
            // Step 1: Get Listen Key
            if self.listen_key.is_none() {
                let create_result = match self.stream_kind {
                    UserDataStreamKind::Spot => self.rest_client.create_spot_listen_key().await,
                    UserDataStreamKind::Futures => self.rest_client.create_listen_key().await,
                };
                match create_result {
                    Ok(res) => {
                        if let Ok(json) = serde_json::from_str::<serde_json::Value>(&res) {
                            if let Some(key) = json.get("listenKey").and_then(|v| v.as_str()) {
                                info!(
                                    "Obtained new listen key for {} User Data Stream",
                                    self.stream_kind.as_str()
                                );
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

            let use_testnet = self.rest_client.trading_mode == "testnet";
            let ws_base = match (self.stream_kind, use_testnet) {
                (UserDataStreamKind::Futures, true) => "wss://fstream.binancefuture.com",
                (UserDataStreamKind::Futures, false) => "wss://fstream.binance.com",
                (UserDataStreamKind::Spot, true) => "wss://demo-stream.binance.com",
                (UserDataStreamKind::Spot, false) => "wss://stream.binance.com:9443",
            };
            let ws_url = format!("{}/ws/{}", ws_base, listen_key);
            info!(
                "Connecting to {} User Data Stream at {}",
                self.stream_kind.as_str(),
                ws_url
            );

            let mut heartbeat_interval = tokio::time::interval(Duration::from_secs(30 * 60)); // 30 minutes

            let connect_result =
                tokio::time::timeout(Duration::from_secs(15), connect_async(&ws_url)).await;
            match connect_result {
                Ok(Ok((mut ws_stream, _))) => {
                    info!(
                        "Successfully connected to Binance {} User Data Stream.",
                        self.stream_kind.as_str()
                    );

                    let _ = self
                        .event_sender
                        .send(WsEvent::Connected {
                            symbol: "USER_DATA".to_string(),
                            stream_type: WsStreamType::UserData,
                        })
                        .await;

                    let mut last_message_time = std::time::Instant::now();
                    let mut ping_interval = tokio::time::interval(Duration::from_secs(30));
                    ping_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
                    let mut check_interval = tokio::time::interval(Duration::from_secs(10));
                    check_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);

                    loop {
                        tokio::select! {
                            _ = heartbeat_interval.tick() => {
                                // Keep-alive the listen key via REST
                                if let Some(key) = &self.listen_key {
                                    let keepalive_result = match self.stream_kind {
                                        UserDataStreamKind::Spot => self.rest_client.keepalive_spot_listen_key(key).await,
                                        UserDataStreamKind::Futures => self.rest_client.keepalive_listen_key(key).await,
                                    };
                                    if let Err(e) = keepalive_result {
                                        warn!("Failed to keep-alive listen key: {}", e);
                                    } else {
                                        info!("Successfully kept listen key alive.");
                                    }
                                }
                            }
                            _ = ping_interval.tick() => {
                                if let Err(e) = ws_stream.send(Message::Ping(vec![])).await {
                                    error!("Failed to send Ping on User Data Stream: {}", e);
                                    break;
                                }
                            }
                            _ = check_interval.tick() => {
                                if last_message_time.elapsed() > Duration::from_secs(60) {
                                    warn!(
                                        "Binance {} User Data Stream read timeout (no message for 60s). Reconnecting.",
                                        self.stream_kind.as_str()
                                    );
                                    break;
                                }
                            }
                            msg_opt = ws_stream.next() => {
                                last_message_time = std::time::Instant::now();
                                let msg_result = match msg_opt {
                                    Some(res) => res,
                                    None => {
                                        warn!("User Data WebSocket stream ended");
                                        break;
                                    }
                                };

                                match msg_result {
                                    Ok(Message::Text(text)) => self.handle_message(&text).await,
                                    Ok(Message::Ping(ping_data)) => {
                                        let _ = ws_stream.send(Message::Pong(ping_data)).await;
                                    }
                                    Ok(Message::Close(_)) => {
                                        warn!("User Data WebSocket closed by server");
                                        break;
                                    }
                                    Err(e) => {
                                        error!("User Data WebSocket error: {}", e);
                                        break;
                                    }
                                    _ => {}
                                }
                            }
                        }
                    }

                    let _ = self
                        .event_sender
                        .send(WsEvent::Disconnected {
                            symbol: "USER_DATA".to_string(),
                            stream_type: WsStreamType::UserData,
                        })
                        .await;
                }
                other => {
                    let err_msg = match other {
                        Ok(Err(e)) => e.to_string(),
                        Err(_) => "Connection attempt timed out (15s)".to_string(),
                        _ => unreachable!(),
                    };
                    error!("Failed to connect User Data Stream: {}", err_msg);
                }
            }

            if let Some(key) = self.listen_key.take() {
                let close_result = match self.stream_kind {
                    UserDataStreamKind::Spot => self.rest_client.close_spot_listen_key(&key).await,
                    UserDataStreamKind::Futures => self.rest_client.close_listen_key(&key).await,
                };
                if let Err(e) = close_result {
                    warn!(
                        "Failed to close {} listen key on reconnect: {}",
                        self.stream_kind.as_str(),
                        e
                    );
                }
            }
            sleep(Duration::from_secs(5)).await;
        }
    }

    async fn handle_message(&self, text: &str) {
        tracing::info!("WS Msg: {}", text);
        let Ok(value) = serde_json::from_str::<serde_json::Value>(text) else {
            return;
        };
        let Some(event_type) = value.get("e").and_then(|v| v.as_str()) else {
            return;
        };
        let parse_f64 = |node: Option<&serde_json::Value>| -> Option<f64> {
            node.and_then(|v| v.as_str())
                .and_then(|s| s.parse::<f64>().ok())
        };
        let parse_i64 =
            |node: Option<&serde_json::Value>| -> Option<i64> { node.and_then(|v| v.as_i64()) };
        let parse_bool =
            |node: Option<&serde_json::Value>| -> Option<bool> { node.and_then(|v| v.as_bool()) };

        match event_type {
            "ORDER_TRADE_UPDATE" => {
                if let Some(order) = value.get("o") {
                    let client_order_id = order
                        .get("c")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let symbol = order
                        .get("s")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let status = order
                        .get("X")
                        .and_then(|v| v.as_str())
                        .unwrap_or("")
                        .to_string();
                    let filled_qty_str = order.get("l").and_then(|v| v.as_str()).unwrap_or("0");
                    let filled_qty = filled_qty_str.parse::<f64>().unwrap_or(0.0);
                    let avg_fill_price = parse_f64(order.get("ap"));
                    let last_fill_price = parse_f64(order.get("L"));
                    let cumulative_quote_qty = parse_f64(order.get("Z"));
                    let commission = parse_f64(order.get("n"));
                    let commission_asset = order
                        .get("N")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string());
                    let realized_pnl = parse_f64(order.get("rp"));
                    let maker = parse_bool(order.get("m"));
                    let execution_type = order
                        .get("x")
                        .and_then(|v| v.as_str())
                        .map(|s| s.to_string());
                    let event_time_ms = parse_i64(order.get("T"))
                        .or_else(|| parse_i64(value.get("T")))
                        .or_else(|| parse_i64(value.get("E")));

                    let _ = self
                        .event_sender
                        .send(WsEvent::OrderUpdate {
                            client_order_id,
                            symbol,
                            status,
                            filled_qty,
                            avg_fill_price,
                            last_fill_price,
                            cumulative_quote_qty,
                            commission,
                            commission_asset,
                            realized_pnl,
                            maker,
                            execution_type,
                            event_time_ms,
                            maker_fills: None,
                            taker_fills: None,
                        })
                        .await;
                }
            }
            "executionReport" => {
                let client_order_id = value
                    .get("c")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let symbol = value
                    .get("s")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let status = value
                    .get("X")
                    .and_then(|v| v.as_str())
                    .unwrap_or("")
                    .to_string();
                let filled_qty_str = value.get("l").and_then(|v| v.as_str()).unwrap_or("0");
                let filled_qty = filled_qty_str.parse::<f64>().unwrap_or(0.0);
                let cumulative_filled_qty = parse_f64(value.get("z")).unwrap_or(0.0);
                let avg_fill_price = parse_f64(value.get("Z")).and_then(|quote_qty| {
                    if cumulative_filled_qty > 0.0 {
                        Some(quote_qty / cumulative_filled_qty)
                    } else {
                        None
                    }
                });
                let last_fill_price = parse_f64(value.get("L"));
                let cumulative_quote_qty = parse_f64(value.get("Z"));
                let commission = parse_f64(value.get("n"));
                let commission_asset = value
                    .get("N")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                let realized_pnl = None;
                let maker = parse_bool(value.get("m"));
                let execution_type = value
                    .get("x")
                    .and_then(|v| v.as_str())
                    .map(|s| s.to_string());
                let event_time_ms = parse_i64(value.get("T")).or_else(|| parse_i64(value.get("E")));

                let _ = self
                    .event_sender
                    .send(WsEvent::OrderUpdate {
                        client_order_id,
                        symbol,
                        status,
                        filled_qty,
                        avg_fill_price,
                        last_fill_price,
                        cumulative_quote_qty,
                        commission,
                        commission_asset,
                        realized_pnl,
                        maker,
                        execution_type,
                        event_time_ms,
                        maker_fills: None,
                        taker_fills: None,
                    })
                    .await;
            }
            "ACCOUNT_UPDATE" => {
                if let Some(update_data) = value.get("a")
                    && let Some(balances_arr) = update_data.get("B").and_then(|v| v.as_array())
                {
                    let mut parsed_balances = HashMap::new();
                    for b in balances_arr {
                        if let (Some(asset), Some(wb)) = (
                            b.get("a").and_then(|v| v.as_str()),
                            b.get("wb").and_then(|v| v.as_str()),
                        ) && let Ok(wallet_balance) = wb.parse::<f64>()
                        {
                            parsed_balances.insert(asset.to_string(), wallet_balance);
                        }
                    }
                    let _ = self
                        .event_sender
                        .send(WsEvent::AccountUpdate {
                            balances: parsed_balances,
                            source: "futures".to_string(),
                        })
                        .await;
                }
            }
            "outboundAccountPosition" => {
                if let Some(balances_arr) = value.get("B").and_then(|v| v.as_array()) {
                    let mut parsed_balances = HashMap::new();
                    for b in balances_arr {
                        if let (Some(asset), Some(f)) = (
                            b.get("a").and_then(|v| v.as_str()),
                            b.get("f").and_then(|v| v.as_str()),
                        ) && let Ok(free_balance) = f.parse::<f64>()
                        {
                            parsed_balances.insert(asset.to_string(), free_balance);
                        }
                    }
                    let _ = self
                        .event_sender
                        .send(WsEvent::AccountUpdate {
                            balances: parsed_balances,
                            source: "spot".to_string(),
                        })
                        .await;
                }
            }
            "listenKeyExpired" => {
                info!("Listen key expired event received");
            }
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::binance_rest::BinanceRest;
    use tokio::sync::mpsc;

    fn test_manager() -> (UserDataWsManager, mpsc::Receiver<WsEvent>) {
        let (tx, rx) = mpsc::channel(4);
        let rest = BinanceRest::new("".to_string(), "".to_string(), "paper".to_string());
        (
            UserDataWsManager::new(rest, tx, UserDataStreamKind::Futures),
            rx,
        )
    }

    #[tokio::test]
    async fn futures_order_trade_update_emits_last_fill_quantity_not_cumulative() {
        let (manager, mut rx) = test_manager();
        let message = r#"{
            "e":"ORDER_TRADE_UPDATE",
            "E":1710000000000,
            "T":1710000000100,
            "o":{
                "s":"TONUSDT",
                "c":"cid-fut",
                "X":"PARTIALLY_FILLED",
                "x":"TRADE",
                "l":"12.5",
                "z":"37.5",
                "L":"3.21",
                "ap":"3.20",
                "Z":"120.0",
                "m":true
            }
        }"#;

        manager.handle_message(message).await;

        match rx.recv().await.expect("order update") {
            WsEvent::OrderUpdate {
                client_order_id,
                symbol,
                status,
                filled_qty,
                avg_fill_price,
                last_fill_price,
                ..
            } => {
                assert_eq!(client_order_id, "cid-fut");
                assert_eq!(symbol, "TONUSDT");
                assert_eq!(status, "PARTIALLY_FILLED");
                assert!((filled_qty - 12.5).abs() < 1e-9);
                assert_eq!(avg_fill_price, Some(3.20));
                assert_eq!(last_fill_price, Some(3.21));
            }
            event => panic!("unexpected event: {:?}", event),
        }
    }

    #[tokio::test]
    async fn spot_execution_report_uses_last_fill_quantity_and_cumulative_avg_price() {
        let (manager, mut rx) = test_manager();
        let message = r#"{
            "e":"executionReport",
            "E":1710000000000,
            "T":1710000000100,
            "s":"TONUSDT",
            "c":"cid-spot",
            "X":"FILLED",
            "x":"TRADE",
            "l":"2.0",
            "z":"10.0",
            "L":"4.10",
            "Z":"41.0",
            "n":"0.01",
            "N":"USDT",
            "m":false
        }"#;

        manager.handle_message(message).await;

        match rx.recv().await.expect("order update") {
            WsEvent::OrderUpdate {
                client_order_id,
                symbol,
                status,
                filled_qty,
                avg_fill_price,
                last_fill_price,
                ..
            } => {
                assert_eq!(client_order_id, "cid-spot");
                assert_eq!(symbol, "TONUSDT");
                assert_eq!(status, "FILLED");
                assert!((filled_qty - 2.0).abs() < 1e-9);
                assert_eq!(avg_fill_price, Some(4.10));
                assert_eq!(last_fill_price, Some(4.10));
            }
            event => panic!("unexpected event: {:?}", event),
        }
    }
}
