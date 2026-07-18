use futures_util::{SinkExt, StreamExt};
use std::collections::{HashMap, HashSet};
use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc::{Receiver, Sender};
use tokio::time::sleep;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::{error, info, warn};

use crate::binance_rest::{BinanceRest, LegVenue};
use crate::order_manager::{MarketType, WsEvent, WsStreamType};

const BACKFILL_PAGE_LIMIT: usize = 1000;
const BACKFILL_WINDOW_MS: i64 = 24 * 60 * 60 * 1000;
const BACKFILL_CURSOR_OVERLAP_MS: i64 = 24 * 60 * 60 * 1000;
const MAX_RECOVERABLE_GAP_MS: i64 = 7 * 24 * 60 * 60 * 1000;
const BOT_ORDER_PREFIX: &str = "bngs_";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UserDataStreamKind {
    Spot,
    Futures,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PrivateStreamControl {
    ReplayFromCursor { reason: String },
}

impl UserDataStreamKind {
    fn as_str(&self) -> &'static str {
        match self {
            UserDataStreamKind::Spot => "spot",
            UserDataStreamKind::Futures => "futures",
        }
    }

    fn venue(&self) -> LegVenue {
        match self {
            UserDataStreamKind::Spot => LegVenue::Spot,
            UserDataStreamKind::Futures => LegVenue::UsdtFutures,
        }
    }

    fn market(&self) -> MarketType {
        match self {
            UserDataStreamKind::Spot => MarketType::Spot,
            UserDataStreamKind::Futures => MarketType::Perp,
        }
    }
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
struct PrivateBackfillStats {
    start_time_ms: i64,
    end_time_ms: i64,
    order_count: usize,
    trade_count: usize,
}

#[derive(Debug, Clone)]
struct BackfillOrder {
    order_id: i64,
    client_order_id: String,
    symbol: String,
    status: String,
    side: String,
    executed_qty: f64,
    avg_price: Option<f64>,
    update_time_ms: Option<i64>,
}

#[derive(Debug, Clone)]
struct BackfillTrade {
    order_id: i64,
    trade_id: i64,
    price: f64,
    qty: f64,
    quote_qty: Option<f64>,
    commission: Option<f64>,
    commission_asset: Option<String>,
    realized_pnl: Option<f64>,
    maker: Option<bool>,
    buyer: Option<bool>,
    event_time_ms: Option<i64>,
}

fn value_i64(value: Option<&serde_json::Value>) -> Option<i64> {
    value.and_then(|node| {
        node.as_i64()
            .or_else(|| node.as_u64().and_then(|raw| i64::try_from(raw).ok()))
            .or_else(|| node.as_str().and_then(|raw| raw.parse::<i64>().ok()))
    })
}

fn value_f64(value: Option<&serde_json::Value>) -> Option<f64> {
    value
        .and_then(|node| node.as_f64().or_else(|| node.as_str()?.parse::<f64>().ok()))
        .filter(|number| number.is_finite())
}

fn parse_json_rows(body: &str, label: &str) -> Result<Vec<serde_json::Value>, String> {
    let rows = serde_json::from_str::<Vec<serde_json::Value>>(body)
        .map_err(|err| format!("invalid {label} JSON: {err}"))?;
    if rows.len() >= BACKFILL_PAGE_LIMIT {
        return Err(format!(
            "{label} returned {} rows at the page limit; refusing truncated private-stream recovery",
            rows.len()
        ));
    }
    Ok(rows)
}

fn safe_reqwest_error(err: &reqwest::Error) -> &'static str {
    if err.is_timeout() {
        "request timed out"
    } else if err.is_connect() {
        "connection failed"
    } else if err.is_body() {
        "response body failed"
    } else if err.is_decode() {
        "response decode failed"
    } else {
        "HTTP transport failed"
    }
}

fn parse_backfill_order(row: &serde_json::Value, symbol: &str) -> Result<BackfillOrder, String> {
    let order_id = value_i64(row.get("orderId"))
        .ok_or_else(|| "backfill order is missing orderId".to_string())?;
    let client_order_id = row
        .get("clientOrderId")
        .or_else(|| row.get("origClientOrderId"))
        .and_then(|value| value.as_str())
        .unwrap_or("")
        .to_string();
    let status = row
        .get("status")
        .and_then(|value| value.as_str())
        .unwrap_or("UNKNOWN")
        .to_uppercase();
    let side = row
        .get("side")
        .and_then(|value| value.as_str())
        .unwrap_or("")
        .to_uppercase();
    let executed_qty = value_f64(row.get("executedQty")).unwrap_or(0.0);
    if executed_qty < 0.0 {
        return Err(format!(
            "backfill order {order_id} has negative executedQty"
        ));
    }
    let cumulative_quote = value_f64(
        row.get("cumQuote")
            .or_else(|| row.get("cumQuoteQty"))
            .or_else(|| row.get("cummulativeQuoteQty")),
    );
    let avg_price = value_f64(row.get("avgPrice"))
        .filter(|price| *price > 0.0)
        .or_else(|| {
            cumulative_quote.and_then(|quote| {
                if executed_qty > 0.0 {
                    Some(quote / executed_qty)
                } else {
                    None
                }
            })
        });
    Ok(BackfillOrder {
        order_id,
        client_order_id,
        symbol: row
            .get("symbol")
            .and_then(|value| value.as_str())
            .unwrap_or(symbol)
            .to_uppercase(),
        status,
        side,
        executed_qty,
        avg_price,
        update_time_ms: value_i64(row.get("updateTime").or_else(|| row.get("time"))),
    })
}

fn parse_backfill_trade(row: &serde_json::Value) -> Result<BackfillTrade, String> {
    let order_id = value_i64(row.get("orderId"))
        .ok_or_else(|| "backfill trade is missing orderId".to_string())?;
    let trade_id =
        value_i64(row.get("id")).ok_or_else(|| "backfill trade is missing id".to_string())?;
    let price = value_f64(row.get("price"))
        .filter(|value| *value > 0.0)
        .ok_or_else(|| format!("backfill trade {trade_id} has invalid price"))?;
    let qty = value_f64(row.get("qty"))
        .filter(|value| *value > 0.0)
        .ok_or_else(|| format!("backfill trade {trade_id} has invalid qty"))?;
    Ok(BackfillTrade {
        order_id,
        trade_id,
        price,
        qty,
        quote_qty: value_f64(row.get("quoteQty")),
        commission: value_f64(row.get("commission")),
        commission_asset: row
            .get("commissionAsset")
            .and_then(|value| value.as_str())
            .map(str::to_string),
        realized_pnl: value_f64(row.get("realizedPnl")),
        maker: row
            .get("maker")
            .or_else(|| row.get("isMaker"))
            .and_then(|value| value.as_bool()),
        buyer: row
            .get("buyer")
            .or_else(|| row.get("isBuyer"))
            .and_then(|value| value.as_bool()),
        event_time_ms: value_i64(row.get("time")),
    })
}

fn build_backfill_events(
    stream_kind: UserDataStreamKind,
    symbol: &str,
    order_rows: &[serde_json::Value],
    trade_rows: &[serde_json::Value],
) -> Result<(Vec<WsEvent>, usize, usize), String> {
    let mut orders = HashMap::<i64, BackfillOrder>::new();
    for row in order_rows {
        let order = parse_backfill_order(row, symbol)?;
        orders.insert(order.order_id, order);
    }

    let mut trades = Vec::<BackfillTrade>::new();
    let mut seen_trades = HashSet::<(i64, i64)>::new();
    for row in trade_rows {
        let trade = parse_backfill_trade(row)?;
        let Some(order) = orders.get(&trade.order_id) else {
            continue;
        };
        if !order.client_order_id.starts_with(BOT_ORDER_PREFIX) {
            continue;
        }
        if seen_trades.insert((trade.order_id, trade.trade_id)) {
            trades.push(trade);
        }
    }
    trades.sort_by_key(|trade| {
        (
            trade.event_time_ms.unwrap_or_default(),
            trade.order_id,
            trade.trade_id,
        )
    });

    let mut fetched_qty_by_order = HashMap::<i64, f64>::new();
    for trade in &trades {
        *fetched_qty_by_order.entry(trade.order_id).or_default() += trade.qty;
    }
    let mut cumulative_qty_by_order = HashMap::<i64, f64>::new();
    let mut cumulative_quote_by_order = HashMap::<i64, f64>::new();
    for (order_id, fetched_qty) in &fetched_qty_by_order {
        let order = orders
            .get(order_id)
            .ok_or_else(|| format!("backfill trade references unknown order {order_id}"))?;
        let tolerance = order.executed_qty.abs().mul_add(1e-9, 1e-12);
        if *fetched_qty > order.executed_qty + tolerance {
            return Err(format!(
                "backfill trades exceed exchange cumulative quantity for order {order_id}"
            ));
        }
        let baseline_qty = (order.executed_qty - fetched_qty).max(0.0);
        cumulative_qty_by_order.insert(*order_id, baseline_qty);
        cumulative_quote_by_order.insert(
            *order_id,
            order.avg_price.unwrap_or_default() * baseline_qty,
        );
    }

    let mut events = Vec::new();
    for trade in &trades {
        let order = orders
            .get(&trade.order_id)
            .ok_or_else(|| format!("backfill trade references unknown order {}", trade.order_id))?;
        let cumulative_qty = cumulative_qty_by_order.entry(trade.order_id).or_default();
        *cumulative_qty += trade.qty;
        let cumulative_quote = cumulative_quote_by_order.entry(trade.order_id).or_default();
        *cumulative_quote += trade.quote_qty.unwrap_or(trade.price * trade.qty);
        let tolerance = order.executed_qty.abs().mul_add(1e-9, 1e-12);
        let status = if order.status == "FILLED"
            && (*cumulative_qty - order.executed_qty).abs() <= tolerance
        {
            "FILLED"
        } else {
            "PARTIALLY_FILLED"
        };
        let side = if matches!(order.side.as_str(), "BUY" | "SELL") {
            Some(order.side.clone())
        } else {
            trade
                .buyer
                .map(|buyer| if buyer { "BUY" } else { "SELL" }.to_string())
        };
        events.push(WsEvent::OrderUpdate {
            client_order_id: order.client_order_id.clone(),
            symbol: order.symbol.clone(),
            status: status.to_string(),
            filled_qty: trade.qty,
            cumulative_filled_qty: Some(*cumulative_qty),
            avg_fill_price: order.avg_price,
            last_fill_price: Some(trade.price),
            cumulative_quote_qty: Some(*cumulative_quote),
            commission: trade.commission,
            commission_asset: trade.commission_asset.clone(),
            realized_pnl: trade.realized_pnl,
            maker: trade.maker,
            execution_type: Some("TRADE".to_string()),
            event_time_ms: trade.event_time_ms,
            maker_fills: None,
            taker_fills: None,
            market: Some(stream_kind.market()),
            side,
            order_id: Some(trade.order_id),
            trade_id: Some(trade.trade_id),
            account_id: None,
            environment: None,
            strategy_id: None,
            cycle_id: None,
            intent_id: None,
            leg_id: None,
            config_version_hash: None,
        });
    }

    let mut bot_orders: Vec<&BackfillOrder> = orders
        .values()
        .filter(|order| order.client_order_id.starts_with(BOT_ORDER_PREFIX))
        .collect();
    bot_orders.sort_by_key(|order| order.order_id);
    for order in &bot_orders {
        events.push(WsEvent::OrderUpdate {
            client_order_id: order.client_order_id.clone(),
            symbol: order.symbol.clone(),
            status: order.status.clone(),
            filled_qty: 0.0,
            cumulative_filled_qty: Some(order.executed_qty),
            avg_fill_price: order.avg_price,
            last_fill_price: None,
            cumulative_quote_qty: order
                .avg_price
                .map(|avg_price| avg_price * order.executed_qty),
            commission: None,
            commission_asset: None,
            realized_pnl: None,
            maker: None,
            execution_type: Some("REST_ORDER_BACKFILL".to_string()),
            event_time_ms: order.update_time_ms,
            maker_fills: None,
            taker_fills: None,
            market: Some(stream_kind.market()),
            side: if order.side.is_empty() {
                None
            } else {
                Some(order.side.clone())
            },
            order_id: Some(order.order_id),
            trade_id: None,
            account_id: None,
            environment: None,
            strategy_id: None,
            cycle_id: None,
            intent_id: None,
            leg_id: None,
            config_version_hash: None,
        });
    }

    Ok((events, bot_orders.len(), trades.len()))
}

pub struct UserDataWsManager {
    rest_client: BinanceRest,
    event_sender: Sender<WsEvent>,
    stream_kind: UserDataStreamKind,
    listen_key: Option<String>,
    monitored_symbols: Vec<String>,
    cursor_path: PathBuf,
    control_receiver: Option<Receiver<PrivateStreamControl>>,
}

impl UserDataWsManager {
    pub fn new(
        rest_client: BinanceRest,
        event_sender: Sender<WsEvent>,
        stream_kind: UserDataStreamKind,
    ) -> Self {
        let monitored_symbols = std::env::var("MONITORED_SYMBOLS")
            .unwrap_or_else(|_| "BTCUSDT,ETHUSDT".to_string())
            .split(',')
            .map(|symbol| symbol.trim().to_uppercase())
            .filter(|symbol| !symbol.is_empty())
            .collect();
        let cursor_dir = std::env::var("PRIVATE_STREAM_CURSOR_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("data/private_stream_cursors"));
        let cursor_path = cursor_dir.join(format!("{}.jsonl", stream_kind.as_str()));
        Self {
            rest_client,
            event_sender,
            stream_kind,
            listen_key: None,
            monitored_symbols,
            cursor_path,
            control_receiver: None,
        }
    }

    pub fn with_control_receiver(
        mut self,
        control_receiver: Receiver<PrivateStreamControl>,
    ) -> Self {
        self.control_receiver = Some(control_receiver);
        self
    }

    fn current_time_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| i64::try_from(duration.as_millis()).unwrap_or(i64::MAX))
            .unwrap_or_default()
    }

    fn load_cursor(&self) -> Result<Option<i64>, String> {
        if !self.cursor_path.exists() {
            return Ok(None);
        }
        let file = std::fs::File::open(&self.cursor_path)
            .map_err(|err| format!("open private-stream cursor: {err}"))?;
        let mut latest = None;
        for (index, line) in BufReader::new(file).lines().enumerate() {
            let line = line.map_err(|err| format!("read private-stream cursor: {err}"))?;
            if line.trim().is_empty() {
                continue;
            }
            let row = serde_json::from_str::<serde_json::Value>(&line).map_err(|err| {
                format!("invalid private-stream cursor line {}: {err}", index + 1)
            })?;
            let stream = row
                .get("stream")
                .and_then(|value| value.as_str())
                .unwrap_or("");
            if stream != self.stream_kind.as_str() {
                return Err(format!(
                    "private-stream cursor contains mismatched stream {stream}"
                ));
            }
            let through_ms = value_i64(row.get("through_ms"))
                .ok_or_else(|| "private-stream cursor is missing through_ms".to_string())?;
            if latest.is_some_and(|previous| through_ms < previous) {
                return Err("private-stream cursor regressed".to_string());
            }
            latest = Some(through_ms);
        }
        Ok(latest)
    }

    fn append_cursor(&self, through_ms: i64) -> Result<(), String> {
        if let Some(parent) = self
            .cursor_path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            std::fs::create_dir_all(parent)
                .map_err(|err| format!("create private-stream cursor directory: {err}"))?;
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.cursor_path)
            .map_err(|err| format!("append private-stream cursor: {err}"))?;
        let row = serde_json::json!({
            "schema_version": 1,
            "stream": self.stream_kind.as_str(),
            "through_ms": through_ms,
            "recorded_at_ms": Self::current_time_ms(),
        });
        serde_json::to_writer(&mut file, &row)
            .map_err(|err| format!("encode private-stream cursor: {err}"))?;
        file.write_all(b"\n")
            .map_err(|err| format!("write private-stream cursor: {err}"))?;
        file.sync_data()
            .map_err(|err| format!("sync private-stream cursor: {err}"))
    }

    async fn send_private_status(
        &self,
        status: &str,
        stats: Option<&PrivateBackfillStats>,
        error: Option<String>,
    ) {
        let _ = self
            .event_sender
            .send(WsEvent::PrivateStreamStatus {
                market: self.stream_kind.market(),
                status: status.to_string(),
                start_time_ms: stats.map(|value| value.start_time_ms),
                end_time_ms: stats.map(|value| value.end_time_ms),
                orders_replayed: stats.map(|value| value.order_count as u64).unwrap_or(0),
                trades_replayed: stats.map(|value| value.trade_count as u64).unwrap_or(0),
                error,
            })
            .await;
    }

    async fn backfill_private_stream(&self) -> Result<PrivateBackfillStats, String> {
        if self.monitored_symbols.is_empty() {
            return Err("private-stream recovery has no monitored symbols".to_string());
        }
        self.rest_client
            .sync_time()
            .await
            .map_err(|err| format!("private-stream time sync failed: {err}"))?;
        let end_time_ms = Self::current_time_ms()
            .saturating_add(self.rest_client.time_offset.load(Ordering::Relaxed));
        let cursor = self.load_cursor()?;
        let start_time_ms = cursor
            .map(|value| value.saturating_sub(BACKFILL_CURSOR_OVERLAP_MS))
            .unwrap_or_else(|| end_time_ms.saturating_sub(MAX_RECOVERABLE_GAP_MS))
            .max(0);
        if end_time_ms.saturating_sub(start_time_ms) > MAX_RECOVERABLE_GAP_MS {
            return Err(format!(
                "{} private-stream cursor is older than Binance's bounded recovery window",
                self.stream_kind.as_str()
            ));
        }

        let mut stats = PrivateBackfillStats {
            start_time_ms,
            end_time_ms,
            ..PrivateBackfillStats::default()
        };
        for symbol in &self.monitored_symbols {
            let mut order_rows = Vec::new();
            let mut trade_rows = Vec::new();
            let mut window_start = start_time_ms;
            while window_start <= end_time_ms {
                let window_end = window_start
                    .saturating_add(BACKFILL_WINDOW_MS - 1)
                    .min(end_time_ms);
                let order_body = self
                    .rest_client
                    .get_order_history(
                        self.stream_kind.venue(),
                        symbol,
                        window_start,
                        window_end,
                        BACKFILL_PAGE_LIMIT as u16,
                    )
                    .await?;
                let trade_body = self
                    .rest_client
                    .get_user_trade_history(
                        self.stream_kind.venue(),
                        symbol,
                        window_start,
                        window_end,
                        BACKFILL_PAGE_LIMIT as u16,
                    )
                    .await?;
                order_rows.extend(parse_json_rows(&order_body, "order history")?);
                trade_rows.extend(parse_json_rows(&trade_body, "trade history")?);
                if window_end == end_time_ms {
                    break;
                }
                window_start = window_end.saturating_add(1);
            }

            let known_order_ids: HashSet<i64> = order_rows
                .iter()
                .filter_map(|row| value_i64(row.get("orderId")))
                .collect();
            let missing_order_ids: HashSet<i64> = trade_rows
                .iter()
                .filter_map(|row| value_i64(row.get("orderId")))
                .filter(|order_id| !known_order_ids.contains(order_id))
                .collect();
            if missing_order_ids.len() > 100 {
                return Err(format!(
                    "{} {} trade history has too many orders outside the bounded order page",
                    self.stream_kind.as_str(),
                    symbol
                ));
            }
            for order_id in missing_order_ids {
                let body = self
                    .rest_client
                    .get_order_by_id(self.stream_kind.venue(), symbol, order_id)
                    .await?;
                let row = serde_json::from_str::<serde_json::Value>(&body)
                    .map_err(|err| format!("invalid order lookup JSON: {err}"))?;
                order_rows.push(row);
            }

            let (events, order_count, trade_count) =
                build_backfill_events(self.stream_kind, symbol, &order_rows, &trade_rows)?;
            for event in events {
                self.event_sender.send(event).await.map_err(|_| {
                    "execution actor closed during private-stream backfill".to_string()
                })?;
            }
            stats.order_count += order_count;
            stats.trade_count += trade_count;
        }
        // The next recovery deliberately rewinds by 24 hours.  That makes a
        // crash after this append but before in-memory delivery idempotently
        // replay the potentially lost events instead of skipping them.
        self.append_cursor(end_time_ms)?;
        Ok(stats)
    }

    pub async fn run(&mut self) {
        // Keep the receiver local so the connected-stream select can await it
        // without mutably borrowing the full manager alongside REST/WS work.
        let mut control_receiver = self.control_receiver.take();
        loop {
            // Futures still uses a listen key. Spot listen-key REST endpoints
            // were retired in 2026 and spot now subscribes through WebSocket API.
            if self.stream_kind == UserDataStreamKind::Futures && self.listen_key.is_none() {
                let create_result = self.rest_client.create_listen_key().await;
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
                                error!("Listen-key response did not contain the required field");
                            }
                        }
                    }
                    Err(e) => error!("Failed to create listen key: {}", safe_reqwest_error(&e)),
                }
            }

            let use_testnet = self.rest_client.trading_mode == "testnet";
            let ws_url = match self.stream_kind {
                UserDataStreamKind::Spot => std::env::var("BINANCE_SPOT_WS_API_URL")
                    .unwrap_or_else(|_| {
                        if use_testnet {
                            // This project uses Binance Spot Demo Mode
                            // (demo-api.binance.com), whose credentials are
                            // distinct from classic Spot Testnet credentials.
                            "wss://demo-ws-api.binance.com/ws-api/v3".to_string()
                        } else {
                            "wss://ws-api.binance.com/ws-api/v3".to_string()
                        }
                    }),
                UserDataStreamKind::Futures => {
                    let Some(listen_key) = &self.listen_key else {
                        sleep(Duration::from_secs(5)).await;
                        continue;
                    };
                    let ws_base = if use_testnet {
                        "wss://fstream.binancefuture.com"
                    } else {
                        "wss://fstream.binance.com"
                    };
                    format!("{}/ws/{}", ws_base, listen_key)
                }
            };
            // The futures URL embeds the account listen key. Never write either
            // private stream URL or the spot subscription request to logs.
            info!(
                "Connecting to {} User Data Stream",
                self.stream_kind.as_str()
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

                    if self.stream_kind == UserDataStreamKind::Spot {
                        let request_id = format!("bongus-spot-{}", Self::current_time_ms());
                        let request = self
                            .rest_client
                            .spot_user_stream_subscription_request(&request_id);
                        let subscribe_result: Result<(), String> = async {
                            ws_stream
                                .send(Message::Text(request.to_string()))
                                .await
                                .map_err(|_| "spot subscription send failed".to_string())?;
                            let response =
                                tokio::time::timeout(Duration::from_secs(10), ws_stream.next())
                                    .await
                                    .map_err(|_| {
                                        "spot subscription response timed out".to_string()
                                    })?
                                    .ok_or_else(|| "spot subscription stream closed".to_string())?
                                    .map_err(|_| {
                                        "spot subscription transport failed".to_string()
                                    })?;
                            let Message::Text(text) = response else {
                                return Err(
                                    "spot subscription returned a non-text response".to_string()
                                );
                            };
                            let response_json = serde_json::from_str::<serde_json::Value>(&text)
                                .map_err(|_| {
                                    "spot subscription returned invalid JSON".to_string()
                                })?;
                            let accepted = response_json.get("id").and_then(|v| v.as_str())
                                == Some(request_id.as_str())
                                && response_json.get("status").and_then(|v| v.as_u64())
                                    == Some(200)
                                && response_json
                                    .get("result")
                                    .and_then(|v| v.get("subscriptionId"))
                                    .and_then(|v| v.as_u64())
                                    .is_some();
                            if !accepted {
                                return Err("spot user-data subscription was rejected".to_string());
                            }
                            Ok(())
                        }
                        .await;
                        if let Err(err) = subscribe_result {
                            error!("{}; readiness remains revoked", err);
                            self.send_private_status("BACKFILL_FAILED", None, Some(err))
                                .await;
                            let _ = self
                                .event_sender
                                .send(WsEvent::Disconnected {
                                    symbol: "USER_DATA".to_string(),
                                    stream_type: WsStreamType::UserData,
                                })
                                .await;
                            sleep(Duration::from_secs(5)).await;
                            continue;
                        }
                        info!("Spot User Data Stream subscription accepted.");
                    }

                    self.send_private_status("BACKFILLING", None, None).await;
                    let backfill_stats = match self.backfill_private_stream().await {
                        Ok(stats) => stats,
                        Err(err) => {
                            error!(
                                "{} private-stream backfill failed; readiness remains revoked: {}",
                                self.stream_kind.as_str(),
                                err
                            );
                            self.send_private_status("BACKFILL_FAILED", None, Some(err))
                                .await;
                            let _ = self
                                .event_sender
                                .send(WsEvent::Disconnected {
                                    symbol: "USER_DATA".to_string(),
                                    stream_type: WsStreamType::UserData,
                                })
                                .await;
                            if let Some(key) = self.listen_key.take() {
                                let close_result = match self.stream_kind {
                                    UserDataStreamKind::Spot => {
                                        self.rest_client.close_spot_listen_key(&key).await
                                    }
                                    UserDataStreamKind::Futures => {
                                        self.rest_client.close_listen_key(&key).await
                                    }
                                };
                                if let Err(close_err) = close_result {
                                    warn!(
                                        "Failed to close {} listen key after backfill failure: {}",
                                        self.stream_kind.as_str(),
                                        safe_reqwest_error(&close_err)
                                    );
                                }
                            }
                            sleep(Duration::from_secs(5)).await;
                            continue;
                        }
                    };
                    self.send_private_status("READY", Some(&backfill_stats), None)
                        .await;

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
                            control = async {
                                match control_receiver.as_mut() {
                                    Some(receiver) => receiver.recv().await,
                                    None => std::future::pending::<Option<PrivateStreamControl>>().await,
                                }
                            } => {
                                match control {
                                    Some(PrivateStreamControl::ReplayFromCursor { reason }) => {
                                        warn!(
                                            "{} private stream replay requested: {}",
                                            self.stream_kind.as_str(),
                                            reason
                                        );
                                        break;
                                    }
                                    None => {
                                        // A closed control channel is not itself an exchange gap.
                                        control_receiver = None;
                                    }
                                }
                            }
                            _ = heartbeat_interval.tick() => {
                                // Keep-alive the listen key via REST
                                if let Some(key) = &self.listen_key {
                                    let keepalive_result = match self.stream_kind {
                                        UserDataStreamKind::Spot => self.rest_client.keepalive_spot_listen_key(key).await,
                                        UserDataStreamKind::Futures => self.rest_client.keepalive_listen_key(key).await,
                                    };
                                    if let Err(e) = keepalive_result {
                                        warn!(
                                            "Failed to keep-alive listen key: {}",
                                            safe_reqwest_error(&e)
                                        );
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
                                    Ok(Message::Text(text)) => {
                                        if self.handle_message(&text).await {
                                            warn!(
                                                "{} listen key expired; reconnecting with a fresh key",
                                                self.stream_kind.as_str()
                                            );
                                            break;
                                        }
                                    },
                                    Ok(Message::Ping(ping_data)) => {
                                        let _ = ws_stream.send(Message::Pong(ping_data)).await;
                                    }
                                    Ok(Message::Close(_)) => {
                                        warn!("User Data WebSocket closed by server");
                                        break;
                                    }
                                    Err(_) => {
                                        error!("User Data WebSocket transport error; reconnecting");
                                        break;
                                    }
                                    _ => {}
                                }
                            }
                        }
                    }

                    self.send_private_status("GAP_DETECTED", Some(&backfill_stats), None)
                        .await;
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
                        Ok(Err(_)) => "WebSocket handshake or transport failed",
                        Err(_) => "Connection attempt timed out (15s)",
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
                        safe_reqwest_error(&e)
                    );
                }
            }
            sleep(Duration::from_secs(5)).await;
        }
    }

    /// Parse one private event. Returns true when the listen key expired and
    /// the owning connection loop must reconnect/backfill before readiness.
    async fn handle_message(&self, text: &str) -> bool {
        let Ok(envelope) = serde_json::from_str::<serde_json::Value>(text) else {
            warn!("Ignoring malformed private WebSocket JSON");
            return false;
        };
        // Spot WebSocket API wraps user events in {subscriptionId, event};
        // futures private-stream messages remain top-level event objects.
        let value = envelope.get("event").unwrap_or(&envelope);
        let Some(event_type) = value.get("e").and_then(|v| v.as_str()) else {
            return false;
        };
        tracing::debug!("Private WebSocket event type={}", event_type);
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
                    let cumulative_filled_qty = parse_f64(order.get("z"));
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
                            cumulative_filled_qty,
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
                            market: Some(MarketType::Perp),
                            side: order.get("S").and_then(|v| v.as_str()).map(str::to_string),
                            order_id: parse_i64(order.get("i")),
                            trade_id: parse_i64(order.get("t")),
                            account_id: None,
                            environment: None,
                            strategy_id: None,
                            cycle_id: None,
                            intent_id: None,
                            leg_id: None,
                            config_version_hash: None,
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
                        cumulative_filled_qty: Some(cumulative_filled_qty),
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
                        market: Some(MarketType::Spot),
                        side: value.get("S").and_then(|v| v.as_str()).map(str::to_string),
                        order_id: parse_i64(value.get("i")),
                        trade_id: parse_i64(value.get("t")),
                        account_id: None,
                        environment: None,
                        strategy_id: None,
                        cycle_id: None,
                        intent_id: None,
                        leg_id: None,
                        config_version_hash: None,
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
            "listenKeyExpired" | "eventStreamTerminated" | "serverShutdown" => {
                return true;
            }
            _ => {}
        }
        false
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
                "S":"SELL",
                "i":12345,
                "t":67890,
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

        assert!(!manager.handle_message(message).await);

        match rx.recv().await.expect("order update") {
            WsEvent::OrderUpdate {
                client_order_id,
                symbol,
                status,
                filled_qty,
                avg_fill_price,
                last_fill_price,
                cumulative_filled_qty,
                market,
                side,
                order_id,
                trade_id,
                ..
            } => {
                assert_eq!(client_order_id, "cid-fut");
                assert_eq!(symbol, "TONUSDT");
                assert_eq!(status, "PARTIALLY_FILLED");
                assert!((filled_qty - 12.5).abs() < 1e-9);
                assert_eq!(avg_fill_price, Some(3.20));
                assert_eq!(last_fill_price, Some(3.21));
                assert_eq!(cumulative_filled_qty, Some(37.5));
                assert_eq!(market, Some(MarketType::Perp));
                assert_eq!(side.as_deref(), Some("SELL"));
                assert_eq!(order_id, Some(12345));
                assert_eq!(trade_id, Some(67890));
            }
            event => panic!("unexpected event: {:?}", event),
        }
    }

    #[tokio::test]
    async fn spot_execution_report_uses_last_fill_quantity_and_cumulative_avg_price() {
        let (manager, mut rx) = test_manager();
        let message = r#"{
            "subscriptionId": 0,
            "event": {
                "e":"executionReport",
                "E":1710000000000,
                "T":1710000000100,
                "s":"TONUSDT",
                "c":"cid-spot",
                "S":"BUY",
                "i":54321,
                "t":9876,
                "X":"FILLED",
                "x":"TRADE",
                "l":"2.0",
                "z":"10.0",
                "L":"4.10",
                "Z":"41.0",
                "n":"0.01",
                "N":"USDT",
                "m":false
            }
        }"#;

        assert!(!manager.handle_message(message).await);

        match rx.recv().await.expect("order update") {
            WsEvent::OrderUpdate {
                client_order_id,
                symbol,
                status,
                filled_qty,
                avg_fill_price,
                last_fill_price,
                cumulative_filled_qty,
                market,
                side,
                order_id,
                trade_id,
                ..
            } => {
                assert_eq!(client_order_id, "cid-spot");
                assert_eq!(symbol, "TONUSDT");
                assert_eq!(status, "FILLED");
                assert!((filled_qty - 2.0).abs() < 1e-9);
                assert_eq!(avg_fill_price, Some(4.10));
                assert_eq!(last_fill_price, Some(4.10));
                assert_eq!(cumulative_filled_qty, Some(10.0));
                assert_eq!(market, Some(MarketType::Spot));
                assert_eq!(side.as_deref(), Some("BUY"));
                assert_eq!(order_id, Some(54321));
                assert_eq!(trade_id, Some(9876));
            }
            event => panic!("unexpected event: {:?}", event),
        }
    }

    #[tokio::test]
    async fn listen_key_expiry_requests_immediate_reconnect() {
        let (manager, _rx) = test_manager();

        assert!(
            manager
                .handle_message(r#"{"e":"listenKeyExpired","E":1710000000000}"#)
                .await
        );
        assert!(
            !manager
                .handle_message(r#"{"e":"unknownPrivateEvent"}"#)
                .await
        );
        assert!(!manager.handle_message("not-json").await);
    }

    #[test]
    fn futures_backfill_reconstructs_cumulative_tail_and_preserves_trade_identity() {
        let orders = serde_json::json!([{
            "symbol": "BTCUSDT",
            "orderId": 99,
            "clientOrderId": "bngs_fut_99",
            "status": "FILLED",
            "side": "SELL",
            "executedQty": "3",
            "avgPrice": "101",
            "updateTime": 1710000000300i64
        }]);
        // Only the tail trade is in the rewind window. The exchange order's
        // cumulative executedQty supplies the missing one-unit baseline.
        let trades = serde_json::json!([{
            "symbol": "BTCUSDT",
            "id": 501,
            "orderId": 99,
            "price": "102",
            "qty": "2",
            "quoteQty": "204",
            "commission": "0.0204",
            "commissionAsset": "USDT",
            "realizedPnl": "1.25",
            "maker": false,
            "buyer": false,
            "time": 1710000000200i64
        }]);

        let (events, order_count, trade_count) = build_backfill_events(
            UserDataStreamKind::Futures,
            "BTCUSDT",
            orders.as_array().unwrap(),
            trades.as_array().unwrap(),
        )
        .expect("backfill events");
        assert_eq!(order_count, 1);
        assert_eq!(trade_count, 1);
        assert_eq!(events.len(), 2);
        match &events[0] {
            WsEvent::OrderUpdate {
                status,
                filled_qty,
                cumulative_filled_qty,
                commission,
                trade_id,
                market,
                execution_type,
                ..
            } => {
                assert_eq!(status, "FILLED");
                assert_eq!(*filled_qty, 2.0);
                assert_eq!(*cumulative_filled_qty, Some(3.0));
                assert_eq!(*commission, Some(0.0204));
                assert_eq!(*trade_id, Some(501));
                assert_eq!(*market, Some(MarketType::Perp));
                assert_eq!(execution_type.as_deref(), Some("TRADE"));
            }
            event => panic!("unexpected event: {event:?}"),
        }
        match &events[1] {
            WsEvent::OrderUpdate {
                status,
                filled_qty,
                cumulative_filled_qty,
                trade_id,
                execution_type,
                ..
            } => {
                assert_eq!(status, "FILLED");
                assert_eq!(*filled_qty, 0.0);
                assert_eq!(*cumulative_filled_qty, Some(3.0));
                assert_eq!(*trade_id, None);
                assert_eq!(execution_type.as_deref(), Some("REST_ORDER_BACKFILL"));
            }
            event => panic!("unexpected event: {event:?}"),
        }
    }

    #[test]
    fn private_backfill_excludes_non_bot_orders_and_fails_on_truncated_pages() {
        let orders = serde_json::json!([{
            "symbol": "ETHUSDT",
            "orderId": 7,
            "clientOrderId": "manual-order",
            "status": "FILLED",
            "side": "BUY",
            "executedQty": "1",
            "cummulativeQuoteQty": "2000"
        }]);
        let trades = serde_json::json!([{
            "id": 8,
            "orderId": 7,
            "price": "2000",
            "qty": "1",
            "isMaker": true,
            "isBuyer": true
        }]);
        let (events, order_count, trade_count) = build_backfill_events(
            UserDataStreamKind::Spot,
            "ETHUSDT",
            orders.as_array().unwrap(),
            trades.as_array().unwrap(),
        )
        .expect("external rows are valid but not owned");
        assert!(events.is_empty());
        assert_eq!(order_count, 0);
        assert_eq!(trade_count, 0);

        let full_page = serde_json::to_string(&vec![serde_json::json!({}); 1000]).unwrap();
        assert!(
            parse_json_rows(&full_page, "trade history")
                .unwrap_err()
                .contains("page limit")
        );
    }

    #[test]
    fn private_stream_cursor_is_append_only_and_rejects_regression() {
        let (mut manager, _rx) = test_manager();
        manager.cursor_path = std::env::temp_dir().join(format!(
            "bongus-private-cursor-{}-{}.jsonl",
            std::process::id(),
            rand::random::<u64>()
        ));
        manager.append_cursor(1_000).unwrap();
        manager.append_cursor(2_000).unwrap();
        assert_eq!(manager.load_cursor().unwrap(), Some(2_000));
        manager.append_cursor(1_500).unwrap();
        assert!(manager.load_cursor().unwrap_err().contains("regressed"));
        std::fs::remove_file(&manager.cursor_path).ok();
    }
}
