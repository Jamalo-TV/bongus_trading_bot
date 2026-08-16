use futures_util::{FutureExt, SinkExt, Stream, StreamExt};
use rand::Rng;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::fs::OpenOptions;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::sync::atomic::Ordering;
use std::sync::{Arc, RwLock};
use std::time::{Duration, SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc::{Receiver, Sender};
use tokio::sync::{Mutex, OwnedMutexGuard};
use tokio::time::sleep;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;
use tracing::{error, info, warn};

use crate::binance_endpoints::endpoints_for_mode;
use crate::binance_rest::{BinanceRest, LegVenue};
use crate::order_manager::{MarketType, WsEvent, WsStreamType};

const BACKFILL_PAGE_LIMIT: usize = 1000;
const BACKFILL_WINDOW_MS: i64 = 24 * 60 * 60 * 1000;
const BACKFILL_CURSOR_OVERLAP_MS: i64 = 24 * 60 * 60 * 1000;
const MAX_RECOVERABLE_GAP_MS: i64 = 7 * 24 * 60 * 60 * 1000;
const PRIVATE_STREAM_RETRY_INITIAL_MS: u64 = 1_000;
const PRIVATE_STREAM_RETRY_MAX_MS: u64 = 60_000;
const BOT_ORDER_PREFIX: &str = "bngs_";
const MAX_BUFFERED_BACKFILL_MESSAGES: usize = 10_000;
const DEFAULT_PRIVATE_CURSOR_MAX_BYTES: u64 = 1_000_000;
const MIN_PRIVATE_CURSOR_MAX_BYTES: u64 = 16 * 1024;
const MAX_PRIVATE_CURSOR_MAX_BYTES: u64 = 16_000_000;

/// Drain every frame that is ready *at the time this function is polled*.
///
/// This deliberately uses `now_or_never` instead of a zero-duration timeout:
/// the latter is allowed to observe its timer before polling the stream.  A
/// caller can therefore reach a deterministic quiescence barrier by invoking
/// this function, processing the returned frames, and repeating until it
/// returns an empty batch.
fn take_immediately_ready_frames<S, E>(
    stream: &mut S,
    remaining_capacity: usize,
) -> Result<Vec<Message>, String>
where
    S: Stream<Item = Result<Message, E>> + Unpin,
{
    let mut frames = Vec::new();
    loop {
        match stream.next().now_or_never() {
            None => return Ok(frames),
            Some(Some(Ok(Message::Close(_)))) | Some(None) => {
                return Err("private stream closed before readiness".to_string());
            }
            Some(Some(Err(_))) => {
                return Err("private stream transport failed before readiness".to_string());
            }
            Some(Some(Ok(frame))) => {
                if frames.len() >= remaining_capacity {
                    return Err(
                        "private-stream buffer exceeded safety cap before readiness".to_string()
                    );
                }
                frames.push(frame);
            }
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum UserDataStreamKind {
    Spot,
    Futures,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PrivateStreamControl {
    ReplayFromCursor { reason: String },
}

#[derive(Debug, Clone)]
pub(crate) struct PrivateCursorRecoveryHandle {
    stream_kind: UserDataStreamKind,
    cursor_path: PathBuf,
    write_barrier: Arc<Mutex<()>>,
}

#[cfg_attr(not(unix), allow(dead_code))]
pub(crate) struct LockedPrivateCursor {
    stream_kind: UserDataStreamKind,
    cursor_path: PathBuf,
    _guard: OwnedMutexGuard<()>,
}

#[cfg_attr(not(unix), allow(dead_code))]
#[derive(Debug, Clone)]
pub(crate) struct PrivateCursorRecoverySnapshot {
    pub stream_kind: UserDataStreamKind,
    pub cursor_path: PathBuf,
    pub through_ms: Option<i64>,
}

impl UserDataStreamKind {
    pub(crate) fn as_str(&self) -> &'static str {
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

impl PrivateCursorRecoveryHandle {
    pub(crate) fn from_env(stream_kind: UserDataStreamKind) -> Self {
        let cursor_dir = std::env::var("PRIVATE_STREAM_CURSOR_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| PathBuf::from("data/private_stream_cursors"));
        Self {
            stream_kind,
            cursor_path: cursor_dir.join(format!("{}.jsonl", stream_kind.as_str())),
            write_barrier: Arc::new(Mutex::new(())),
        }
    }

    #[cfg_attr(not(unix), allow(dead_code))]
    pub(crate) async fn lock_for_recovery(&self) -> LockedPrivateCursor {
        LockedPrivateCursor {
            stream_kind: self.stream_kind,
            cursor_path: self.cursor_path.clone(),
            _guard: self.write_barrier.clone().lock_owned().await,
        }
    }

    #[cfg(test)]
    pub(crate) async fn write_cursor_for_recovery_test(
        &self,
        through_ms: i64,
    ) -> Result<(), String> {
        let _guard = self.write_barrier.lock().await;
        if let Some(parent) = self.cursor_path.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|error| format!("create private cursor test directory: {error}"))?;
        }
        let row = serde_json::json!({
            "stream": self.stream_kind.as_str(),
            "through_ms": through_ms,
        });
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.cursor_path)
            .map_err(|error| format!("open private cursor test file: {error}"))?;
        writeln!(file, "{row}")
            .map_err(|error| format!("write private cursor test file: {error}"))?;
        file.sync_all()
            .map_err(|error| format!("sync private cursor test file: {error}"))
    }

    #[cfg(test)]
    pub(crate) fn for_test(stream_kind: UserDataStreamKind, cursor_path: PathBuf) -> Self {
        Self {
            stream_kind,
            cursor_path,
            write_barrier: Arc::new(Mutex::new(())),
        }
    }
}

#[cfg_attr(not(unix), allow(dead_code))]
impl LockedPrivateCursor {
    pub(crate) fn prepare_recovery_snapshot(
        &self,
    ) -> Result<PrivateCursorRecoverySnapshot, String> {
        let through_ms = load_cursor_path(&self.cursor_path, self.stream_kind)?;
        if self.cursor_path.exists() {
            let metadata = std::fs::symlink_metadata(&self.cursor_path).map_err(|error| {
                format!("inspect private-stream cursor for recovery barrier: {error}")
            })?;
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return Err("private-stream cursor is not a regular recovery source".to_string());
            }
            OpenOptions::new()
                .read(true)
                .write(true)
                .open(&self.cursor_path)
                .map_err(|error| {
                    format!("open private-stream cursor for recovery barrier: {error}")
                })?
                .sync_all()
                .map_err(|error| {
                    format!("sync private-stream cursor for recovery barrier: {error}")
                })?;
        }
        #[cfg(unix)]
        if let Some(parent) = self.cursor_path.parent().filter(|parent| parent.exists()) {
            std::fs::File::open(parent)
                .map_err(|error| format!("open private cursor directory: {error}"))?
                .sync_all()
                .map_err(|error| format!("sync private cursor directory: {error}"))?;
        }
        Ok(PrivateCursorRecoverySnapshot {
            stream_kind: self.stream_kind,
            cursor_path: self.cursor_path.clone(),
            through_ms,
        })
    }
}

fn load_cursor_path(
    cursor_path: &std::path::Path,
    stream_kind: UserDataStreamKind,
) -> Result<Option<i64>, String> {
    let previous_path = UserDataWsManager::cursor_path_with_suffix(cursor_path, ".previous");
    if !cursor_path.exists() && previous_path.exists() {
        std::fs::rename(&previous_path, cursor_path)
            .map_err(|err| format!("recover private-stream cursor: {err}"))?;
    }
    if !cursor_path.exists() {
        return Ok(None);
    }
    let file = std::fs::File::open(cursor_path)
        .map_err(|err| format!("open private-stream cursor: {err}"))?;
    let mut latest = None;
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line = line.map_err(|err| format!("read private-stream cursor: {err}"))?;
        if line.trim().is_empty() {
            continue;
        }
        let row = serde_json::from_str::<serde_json::Value>(&line)
            .map_err(|err| format!("invalid private-stream cursor line {}: {err}", index + 1))?;
        let stream = row
            .get("stream")
            .and_then(|value| value.as_str())
            .unwrap_or("");
        if stream != stream_kind.as_str() {
            return Err(format!(
                "private-stream cursor contains mismatched stream {stream}"
            ));
        }
        let through_ms = value_i64(row.get("through_ms"))
            .filter(|value| *value >= 0)
            .ok_or_else(|| "private-stream cursor has invalid through_ms".to_string())?;
        if latest.is_some_and(|previous| through_ms < previous) {
            return Err("private-stream cursor regressed".to_string());
        }
        latest = Some(through_ms);
    }
    Ok(latest)
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

fn bounded_backfill_start(
    stream_kind: UserDataStreamKind,
    end_time_ms: i64,
    cursor: Option<i64>,
) -> Result<(i64, bool), String> {
    if cursor.is_some_and(|value| value > end_time_ms.saturating_add(60_000)) {
        return Err(format!(
            "{} private-stream cursor is ahead of synchronized exchange time",
            stream_kind.as_str()
        ));
    }
    let requested_start_time_ms = cursor
        .map(|value| value.saturating_sub(BACKFILL_CURSOR_OVERLAP_MS))
        .unwrap_or_else(|| end_time_ms.saturating_sub(MAX_RECOVERABLE_GAP_MS))
        .max(0);
    let earliest_recoverable_ms = end_time_ms.saturating_sub(MAX_RECOVERABLE_GAP_MS);
    let start_time_ms = requested_start_time_ms.max(earliest_recoverable_ms);
    Ok((start_time_ms, start_time_ms != requested_start_time_ms))
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
            connection_id: Some("rest-backfill".to_string()),
            exchange_event_time_ms: trade.event_time_ms,
            receive_time_ms: None,
            process_time_ms: None,
            persist_time_ms: None,
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
            connection_id: Some("rest-backfill".to_string()),
            exchange_event_time_ms: order.update_time_ms,
            receive_time_ms: None,
            process_time_ms: None,
            persist_time_ms: None,
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
    recovery_universe: Arc<RwLock<HashSet<String>>>,
    cursor_path: PathBuf,
    cursor_write_barrier: Arc<Mutex<()>>,
    cursor_max_bytes: u64,
    control_receiver: Option<Receiver<PrivateStreamControl>>,
}

impl UserDataWsManager {
    async fn sleep_before_retry(retry_delay_ms: &mut u64) {
        let base =
            (*retry_delay_ms).clamp(PRIVATE_STREAM_RETRY_INITIAL_MS, PRIVATE_STREAM_RETRY_MAX_MS);
        let jitter = rand::thread_rng().gen_range(0..=(base / 2));
        sleep(Duration::from_millis(base.saturating_add(jitter))).await;
        *retry_delay_ms = base.saturating_mul(2).min(PRIVATE_STREAM_RETRY_MAX_MS);
    }

    /// Remove the key from reconnect state before attempting the best-effort
    /// exchange-side close.  Even when the close request fails, a subsequent
    /// retry must create a new key rather than reconnecting with a key the
    /// stream has already declared expired.
    async fn retire_current_listen_key(&mut self, context: &str) {
        let Some(key) = self.listen_key.take() else {
            return;
        };
        let close_result = match self.stream_kind {
            UserDataStreamKind::Spot => self.rest_client.close_spot_listen_key(&key).await,
            UserDataStreamKind::Futures => self.rest_client.close_listen_key(&key).await,
        };
        if let Err(err) = close_result {
            warn!(
                "Failed to close {} listen key {}: {}",
                self.stream_kind.as_str(),
                context,
                err
            );
        }
    }

    fn spot_subscription_rejection(response: &serde_json::Value) -> String {
        let status = response
            .get("status")
            .and_then(|value| value.as_u64())
            .map(|value| value.to_string())
            .unwrap_or_else(|| "unknown".to_string());
        let error = response.get("error");
        let code = error
            .and_then(|value| value.get("code"))
            .and_then(|value| value_i64(Some(value)))
            .map(|value| value.to_string())
            .unwrap_or_else(|| "unknown".to_string());
        let message = error
            .and_then(|value| value.get("msg"))
            .and_then(|value| value.as_str())
            .unwrap_or("no exchange error message");
        let safe_message: String = message.chars().take(200).collect();
        format!(
            "spot user-data subscription was rejected (status={status}, code={code}, message={safe_message})"
        )
    }

    fn futures_private_stream_url(base_url: &str, listen_key: &str) -> String {
        format!(
            "{}/ws?listenKey={}&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE",
            base_url.trim_end_matches('/'),
            listen_key
        )
    }

    pub fn new(
        rest_client: BinanceRest,
        event_sender: Sender<WsEvent>,
        stream_kind: UserDataStreamKind,
    ) -> Self {
        let recovery_universe = std::env::var("MONITORED_SYMBOLS")
            .unwrap_or_else(|_| "BTCUSDT,ETHUSDT".to_string())
            .split(',')
            .map(|symbol| symbol.trim().to_uppercase())
            .filter(|symbol| !symbol.is_empty())
            .collect::<HashSet<_>>();
        let cursor_handle = PrivateCursorRecoveryHandle::from_env(stream_kind);
        let cursor_max_bytes = std::env::var("PRIVATE_STREAM_CURSOR_MAX_BYTES")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .filter(|value| *value >= MIN_PRIVATE_CURSOR_MAX_BYTES)
            .unwrap_or(DEFAULT_PRIVATE_CURSOR_MAX_BYTES)
            .min(MAX_PRIVATE_CURSOR_MAX_BYTES);
        Self {
            rest_client,
            event_sender,
            stream_kind,
            listen_key: None,
            recovery_universe: Arc::new(RwLock::new(recovery_universe)),
            cursor_path: cursor_handle.cursor_path,
            cursor_write_barrier: cursor_handle.write_barrier,
            cursor_max_bytes,
            control_receiver: None,
        }
    }

    pub(crate) fn with_recovery_cursor_handle(
        mut self,
        handle: PrivateCursorRecoveryHandle,
    ) -> Self {
        debug_assert_eq!(self.stream_kind, handle.stream_kind);
        self.cursor_path = handle.cursor_path;
        self.cursor_write_barrier = handle.write_barrier;
        self
    }

    pub fn with_control_receiver(
        mut self,
        control_receiver: Receiver<PrivateStreamControl>,
    ) -> Self {
        self.control_receiver = Some(control_receiver);
        self
    }

    pub fn with_recovery_universe(
        mut self,
        recovery_universe: Arc<RwLock<HashSet<String>>>,
    ) -> Self {
        self.recovery_universe = recovery_universe;
        self
    }

    fn recovery_symbols_snapshot(&self) -> Result<Vec<String>, String> {
        let guard = self
            .recovery_universe
            .read()
            .map_err(|_| "private-stream recovery universe lock is poisoned".to_string())?;
        let mut symbols: Vec<String> = guard
            .iter()
            .map(|symbol| symbol.trim().to_uppercase())
            .filter(|symbol| !symbol.is_empty())
            .collect();
        symbols.sort();
        symbols.dedup();
        Ok(symbols)
    }

    fn current_time_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| i64::try_from(duration.as_millis()).unwrap_or(i64::MAX))
            .unwrap_or_default()
    }

    async fn load_cursor(&self) -> Result<Option<i64>, String> {
        let _barrier = self.cursor_write_barrier.lock().await;
        load_cursor_path(&self.cursor_path, self.stream_kind)
    }

    async fn append_cursor(&self, through_ms: i64) -> Result<(), String> {
        let _barrier = self.cursor_write_barrier.lock().await;
        if let Some(previous) = load_cursor_path(&self.cursor_path, self.stream_kind)? {
            if through_ms < previous {
                return Err("private-stream cursor regressed".to_string());
            }
            if through_ms == previous {
                return Ok(());
            }
        }
        if let Some(parent) = self
            .cursor_path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            std::fs::create_dir_all(parent)
                .map_err(|err| format!("create private-stream cursor directory: {err}"))?;
        }
        let row = serde_json::json!({
            "schema_version": 1,
            "stream": self.stream_kind.as_str(),
            "through_ms": through_ms,
            "recorded_at_ms": Self::current_time_ms(),
        });
        let mut encoded = serde_json::to_vec(&row)
            .map_err(|err| format!("encode private-stream cursor: {err}"))?;
        encoded.push(b'\n');
        if encoded.len() as u64 > self.cursor_max_bytes {
            return Err("private-stream cursor record exceeds byte cap".to_string());
        }
        let current_bytes = match self.cursor_path.metadata() {
            Ok(metadata) => metadata.len(),
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => 0,
            Err(err) => return Err(format!("inspect private-stream cursor: {err}")),
        };
        if current_bytes.saturating_add(encoded.len() as u64) > self.cursor_max_bytes {
            return self.install_compacted_cursor(&encoded);
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.cursor_path)
            .map_err(|err| format!("append private-stream cursor: {err}"))?;
        file.write_all(&encoded)
            .map_err(|err| format!("write private-stream cursor: {err}"))?;
        file.sync_data()
            .map_err(|err| format!("sync private-stream cursor: {err}"))
    }

    fn cursor_path_with_suffix(path: &std::path::Path, suffix: &str) -> PathBuf {
        let mut value = path.as_os_str().to_os_string();
        value.push(suffix);
        PathBuf::from(value)
    }

    fn install_compacted_cursor(&self, encoded: &[u8]) -> Result<(), String> {
        let next_path = Self::cursor_path_with_suffix(&self.cursor_path, ".next");
        let previous_path = Self::cursor_path_with_suffix(&self.cursor_path, ".previous");
        {
            let mut next = OpenOptions::new()
                .create(true)
                .truncate(true)
                .write(true)
                .open(&next_path)
                .map_err(|err| format!("create compacted private-stream cursor: {err}"))?;
            next.write_all(encoded)
                .map_err(|err| format!("write compacted private-stream cursor: {err}"))?;
            next.sync_all()
                .map_err(|err| format!("sync compacted private-stream cursor: {err}"))?;
        }
        if previous_path.exists() {
            std::fs::remove_file(&previous_path)
                .map_err(|err| format!("remove stale private-stream cursor: {err}"))?;
        }
        if self.cursor_path.exists() {
            std::fs::rename(&self.cursor_path, &previous_path)
                .map_err(|err| format!("rotate private-stream cursor: {err}"))?;
        }
        if let Err(err) = std::fs::rename(&next_path, &self.cursor_path) {
            if !self.cursor_path.exists() && previous_path.exists() {
                let _ = std::fs::rename(&previous_path, &self.cursor_path);
            }
            return Err(format!("install compacted private-stream cursor: {err}"));
        }
        if previous_path.exists() {
            let _ = std::fs::remove_file(previous_path);
        }
        Ok(())
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
        let recovery_symbols = self.recovery_symbols_snapshot()?;
        if recovery_symbols.is_empty() {
            return Err("private-stream recovery has no monitored symbols".to_string());
        }
        self.rest_client
            .sync_time()
            .await
            .map_err(|err| format!("private-stream time sync failed: {err}"))?;
        let end_time_ms = Self::current_time_ms()
            .saturating_add(self.rest_client.time_offset.load(Ordering::Relaxed));
        let cursor = self.load_cursor().await?;
        let (start_time_ms, cursor_rebased) =
            bounded_backfill_start(self.stream_kind, end_time_ms, cursor)?;
        if cursor_rebased {
            warn!(
                "{} private-stream cursor predates the bounded history window; rebasing to the earliest recoverable checkpoint. Trading readiness still requires the subsequent two-venue exchange reconciliation.",
                self.stream_kind.as_str()
            );
        }

        let mut stats = PrivateBackfillStats {
            start_time_ms,
            end_time_ms,
            ..PrivateBackfillStats::default()
        };
        for symbol in &recovery_symbols {
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
        self.append_cursor(end_time_ms).await?;
        Ok(stats)
    }

    pub async fn run(&mut self) {
        // Keep the receiver local so the connected-stream select can await it
        // without mutably borrowing the full manager alongside REST/WS work.
        let mut control_receiver = self.control_receiver.take();
        let mut retry_delay_ms = PRIVATE_STREAM_RETRY_INITIAL_MS;
        let endpoints = match endpoints_for_mode(&self.rest_client.trading_mode) {
            Ok(endpoints) => endpoints,
            Err(error) => {
                error!("Invalid shared Binance endpoint matrix: {}", error);
                return;
            }
        };
        let planned_connection_max_age =
            Duration::from_secs(endpoints.planned_connection_max_age_seconds);
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
                    Err(e) => error!("Failed to create listen key: {}", e),
                }
            }

            let ws_url = match self.stream_kind {
                UserDataStreamKind::Spot => endpoints.spot.private_ws_base_url.clone(),
                UserDataStreamKind::Futures => {
                    let Some(listen_key) = &self.listen_key else {
                        Self::sleep_before_retry(&mut retry_delay_ms).await;
                        continue;
                    };
                    Self::futures_private_stream_url(
                        &endpoints.futures.private_ws_base_url,
                        listen_key,
                    )
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
                    let connection_deadline =
                        tokio::time::Instant::now() + planned_connection_max_age;
                    let connection_id = format!(
                        "private-{}-{}",
                        self.stream_kind.as_str(),
                        Self::current_time_ms()
                    );
                    info!(
                        "Successfully connected to Binance {} User Data Stream.",
                        self.stream_kind.as_str()
                    );

                    if self.stream_kind == UserDataStreamKind::Spot {
                        if let Err(err) = self.rest_client.sync_time().await {
                            let reason = format!("spot private-stream time sync failed: {err}");
                            error!("{}; readiness remains revoked", reason);
                            self.send_private_status("BACKFILL_FAILED", None, Some(reason))
                                .await;
                            let _ = self
                                .event_sender
                                .send(WsEvent::Disconnected {
                                    symbol: "USER_DATA".to_string(),
                                    stream_type: WsStreamType::UserData,
                                    connection_id: Some(connection_id.clone()),
                                    connection_role: Some(self.stream_kind.as_str().to_string()),
                                })
                                .await;
                            Self::sleep_before_retry(&mut retry_delay_ms).await;
                            continue;
                        }
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
                                return Err(Self::spot_subscription_rejection(&response_json));
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
                                    connection_id: Some(connection_id.clone()),
                                    connection_role: Some(self.stream_kind.as_str().to_string()),
                                })
                                .await;
                            Self::sleep_before_retry(&mut retry_delay_ms).await;
                            continue;
                        }
                        info!("Spot User Data Stream subscription accepted.");
                    }

                    self.send_private_status("BACKFILLING", None, None).await;
                    // The websocket is live before REST backfill begins. Drain
                    // it concurrently so an execution that lands inside the
                    // backfill window cannot sit unread behind a premature
                    // READY notification.
                    let mut buffered_private_messages = Vec::new();
                    let mut backfill = Box::pin(self.backfill_private_stream());
                    let backfill_result = loop {
                        tokio::select! {
                            result = &mut backfill => break result,
                            message = ws_stream.next() => {
                                match message {
                                    Some(Ok(Message::Text(text))) => {
                                        if buffered_private_messages.len() >= MAX_BUFFERED_BACKFILL_MESSAGES {
                                            break Err("private-stream buffer exceeded safety cap during backfill".to_string());
                                        }
                                        buffered_private_messages.push(text);
                                    }
                                    Some(Ok(Message::Ping(payload))) => {
                                        if ws_stream.send(Message::Pong(payload)).await.is_err() {
                                            break Err("private-stream pong failed during backfill".to_string());
                                        }
                                    }
                                    Some(Ok(Message::Close(_))) | None => {
                                        break Err("private stream closed during REST backfill".to_string());
                                    }
                                    Some(Err(_)) => {
                                        break Err("private stream transport failed during REST backfill".to_string());
                                    }
                                    Some(Ok(_)) => {}
                                }
                            }
                        }
                    };
                    drop(backfill);
                    let backfill_stats = match backfill_result {
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
                                    connection_id: Some(connection_id.clone()),
                                    connection_role: Some(self.stream_kind.as_str().to_string()),
                                })
                                .await;
                            self.retire_current_listen_key("after backfill failure")
                                .await;
                            Self::sleep_before_retry(&mut retry_delay_ms).await;
                            continue;
                        }
                    };
                    let mut pre_ready_failure = None;
                    let mut startup_frames_seen = buffered_private_messages.len();
                    for text in buffered_private_messages {
                        if self.handle_message(&text, Some(&connection_id)).await {
                            pre_ready_failure = Some(
                                "listen key expired while REST backfill was in progress"
                                    .to_string(),
                            );
                            break;
                        }
                    }

                    // Backfill completion and the websocket's next frame can
                    // become ready in the same scheduler turn.  The select
                    // above is allowed to pick backfill first, so reach a
                    // deterministic quiescence point before announcing READY.
                    // Repeat after processing each batch because processing
                    // (and replying to pings) can itself yield long enough for
                    // another frame to become immediately available.
                    while pre_ready_failure.is_none() {
                        let remaining_capacity =
                            MAX_BUFFERED_BACKFILL_MESSAGES.saturating_sub(startup_frames_seen);
                        let ready_frames =
                            match take_immediately_ready_frames(&mut ws_stream, remaining_capacity)
                            {
                                Ok(frames) => frames,
                                Err(reason) => {
                                    pre_ready_failure = Some(reason);
                                    break;
                                }
                            };
                        if ready_frames.is_empty() {
                            break;
                        }
                        startup_frames_seen =
                            startup_frames_seen.saturating_add(ready_frames.len());
                        for frame in ready_frames {
                            match frame {
                                Message::Text(text) => {
                                    if self.handle_message(&text, Some(&connection_id)).await {
                                        pre_ready_failure = Some(
                                            "listen key expired before private-stream readiness"
                                                .to_string(),
                                        );
                                        break;
                                    }
                                }
                                Message::Ping(payload) => {
                                    if ws_stream.send(Message::Pong(payload)).await.is_err() {
                                        pre_ready_failure = Some(
                                            "private-stream pong failed before readiness"
                                                .to_string(),
                                        );
                                        break;
                                    }
                                }
                                Message::Close(_) => {
                                    pre_ready_failure =
                                        Some("private stream closed before readiness".to_string());
                                    break;
                                }
                                _ => {}
                            }
                        }
                    }

                    if let Some(reason) = pre_ready_failure {
                        warn!(
                            "{} private stream failed its pre-readiness barrier: {}",
                            self.stream_kind.as_str(),
                            reason
                        );
                        self.send_private_status(
                            "BACKFILL_FAILED",
                            Some(&backfill_stats),
                            Some(reason),
                        )
                        .await;
                        let _ = self
                            .event_sender
                            .send(WsEvent::Disconnected {
                                symbol: "USER_DATA".to_string(),
                                stream_type: WsStreamType::UserData,
                                connection_id: Some(connection_id.clone()),
                                connection_role: Some(self.stream_kind.as_str().to_string()),
                            })
                            .await;
                        self.retire_current_listen_key("after pre-readiness failure")
                            .await;
                        Self::sleep_before_retry(&mut retry_delay_ms).await;
                        continue;
                    }
                    self.send_private_status("READY", Some(&backfill_stats), None)
                        .await;
                    retry_delay_ms = PRIVATE_STREAM_RETRY_INITIAL_MS;

                    let _ = self
                        .event_sender
                        .send(WsEvent::Connected {
                            symbol: "USER_DATA".to_string(),
                            stream_type: WsStreamType::UserData,
                            connection_id: Some(connection_id.clone()),
                            connection_role: Some(self.stream_kind.as_str().to_string()),
                        })
                        .await;

                    let mut last_valid_activity_time = std::time::Instant::now();
                    let mut ping_interval = tokio::time::interval(Duration::from_secs(30));
                    ping_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
                    let mut check_interval = tokio::time::interval(Duration::from_secs(10));
                    check_interval.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
                    let connection_renewal = tokio::time::sleep_until(connection_deadline);
                    tokio::pin!(connection_renewal);

                    loop {
                        tokio::select! {
                            _ = &mut connection_renewal => {
                                info!(
                                    "Planned {} private WebSocket renewal before Binance's 24-hour limit",
                                    self.stream_kind.as_str()
                                );
                                break;
                            }
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
                                            e
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
                                if last_valid_activity_time.elapsed() > Duration::from_secs(60) {
                                    warn!(
                                        "Binance {} User Data Stream timeout (no valid private event or ping for 60s). Reconnecting.",
                                        self.stream_kind.as_str()
                                    );
                                    break;
                                }
                            }
                            msg_opt = ws_stream.next() => {
                                let msg_result = match msg_opt {
                                    Some(res) => res,
                                    None => {
                                        warn!("User Data WebSocket stream ended");
                                        break;
                                    }
                                };

                                match msg_result {
                                    Ok(Message::Text(text)) => {
                                        if Self::private_message_is_semantic(&text) {
                                            last_valid_activity_time = std::time::Instant::now();
                                        }
                                        if self.handle_message(&text, Some(&connection_id)).await {
                                            warn!(
                                                "{} listen key expired; reconnecting with a fresh key",
                                                self.stream_kind.as_str()
                                            );
                                            break;
                                        }
                                    },
                                    Ok(Message::Ping(ping_data)) => {
                                        last_valid_activity_time = std::time::Instant::now();
                                        let _ = ws_stream.send(Message::Pong(ping_data)).await;
                                    }
                                    Ok(Message::Pong(_)) => {
                                        last_valid_activity_time = std::time::Instant::now();
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
                            connection_id: Some(connection_id.clone()),
                            connection_role: Some(self.stream_kind.as_str().to_string()),
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

            self.retire_current_listen_key("on reconnect").await;
            Self::sleep_before_retry(&mut retry_delay_ms).await;
        }
    }

    fn private_message_is_semantic(text: &str) -> bool {
        let Ok(envelope) = serde_json::from_str::<serde_json::Value>(text) else {
            return false;
        };
        let value = envelope.get("event").unwrap_or(&envelope);
        matches!(
            value.get("e").and_then(|node| node.as_str()),
            Some(
                "ORDER_TRADE_UPDATE"
                    | "executionReport"
                    | "ACCOUNT_UPDATE"
                    | "outboundAccountPosition"
                    | "listenKeyExpired"
                    | "eventStreamTerminated"
                    | "serverShutdown"
            )
        )
    }

    /// Parse one private event. Returns true when the listen key expired and
    /// the owning connection loop must reconnect/backfill before readiness.
    async fn handle_message(&self, text: &str, connection_id: Option<&str>) -> bool {
        let receive_time_ms = Self::current_time_ms();
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
                            connection_id: connection_id.map(str::to_string),
                            exchange_event_time_ms: event_time_ms,
                            receive_time_ms: Some(receive_time_ms),
                            process_time_ms: Some(Self::current_time_ms()),
                            persist_time_ms: None,
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
                        connection_id: connection_id.map(str::to_string),
                        exchange_event_time_ms: event_time_ms,
                        receive_time_ms: Some(receive_time_ms),
                        process_time_ms: Some(Self::current_time_ms()),
                        persist_time_ms: None,
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
                    let mut available_balances = HashMap::new();
                    for b in balances_arr {
                        if let (Some(asset), Some(wb)) = (
                            b.get("a").and_then(|v| v.as_str()),
                            b.get("wb").and_then(|v| v.as_str()),
                        ) && let Ok(wallet_balance) = wb.parse::<f64>()
                            && wallet_balance.is_finite()
                        {
                            parsed_balances.insert(asset.to_string(), wallet_balance);
                        }
                        if let (Some(asset), Some(cross_wallet)) = (
                            b.get("a").and_then(|v| v.as_str()),
                            b.get("cw").and_then(|v| v.as_str()),
                        ) && let Ok(available_balance) = cross_wallet.parse::<f64>()
                            && available_balance.is_finite()
                        {
                            available_balances.insert(asset.to_string(), available_balance);
                        }
                    }
                    let mut positions = HashMap::new();
                    if let Some(position_rows) = update_data.get("P").and_then(Value::as_array) {
                        for row in position_rows {
                            if let (Some(symbol), Some(raw_quantity)) = (
                                row.get("s").and_then(Value::as_str),
                                row.get("pa").and_then(Value::as_str),
                            ) && let Ok(quantity) = raw_quantity.parse::<f64>()
                                && quantity.is_finite()
                            {
                                positions.insert(symbol.to_uppercase(), quantity);
                            }
                        }
                    }
                    let _ = self
                        .event_sender
                        .send(WsEvent::AccountUpdate {
                            balances: parsed_balances,
                            available_balances,
                            positions,
                            source: "futures".to_string(),
                        })
                        .await;
                }
            }
            "outboundAccountPosition" => {
                if let Some(balances_arr) = value.get("B").and_then(|v| v.as_array()) {
                    let mut parsed_balances = HashMap::new();
                    let mut available_balances = HashMap::new();
                    for b in balances_arr {
                        if let (Some(asset), Some(free_raw), Some(locked_raw)) = (
                            b.get("a").and_then(|v| v.as_str()),
                            b.get("f").and_then(|v| v.as_str()),
                            b.get("l").and_then(|v| v.as_str()),
                        ) && let (Ok(free_balance), Ok(locked_balance)) =
                            (free_raw.parse::<f64>(), locked_raw.parse::<f64>())
                            && free_balance.is_finite()
                            && free_balance >= 0.0
                            && locked_balance.is_finite()
                            && locked_balance >= 0.0
                        {
                            parsed_balances
                                .insert(asset.to_string(), free_balance + locked_balance);
                            available_balances.insert(asset.to_string(), free_balance);
                        }
                    }
                    let _ = self
                        .event_sender
                        .send(WsEvent::AccountUpdate {
                            balances: parsed_balances,
                            available_balances,
                            positions: HashMap::new(),
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
    use futures_util::stream;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;
    use tokio::sync::mpsc;

    fn test_manager() -> (UserDataWsManager, mpsc::Receiver<WsEvent>) {
        let (tx, rx) = mpsc::channel(4);
        let rest = BinanceRest::new("".to_string(), "".to_string(), "paper".to_string());
        (
            UserDataWsManager::new(rest, tx, UserDataStreamKind::Futures),
            rx,
        )
    }

    #[test]
    fn private_freshness_ignores_arbitrary_json_and_accepts_known_events() {
        assert!(!UserDataWsManager::private_message_is_semantic(
            r#"{"noise":true}"#
        ));
        assert!(!UserDataWsManager::private_message_is_semantic(
            r#"{"e":"unknownEvent"}"#
        ));
        assert!(UserDataWsManager::private_message_is_semantic(
            r#"{"e":"ORDER_TRADE_UPDATE","o":{}}"#
        ));
        assert!(UserDataWsManager::private_message_is_semantic(
            r#"{"subscriptionId":7,"event":{"e":"executionReport"}}"#
        ));
    }

    #[test]
    fn private_recovery_universe_updates_without_restarting_the_manager() {
        let (manager, _rx) = test_manager();
        let shared = Arc::new(RwLock::new(HashSet::from(["BTCUSDT".to_string()])));
        let manager = manager.with_recovery_universe(shared.clone());
        shared
            .write()
            .expect("recovery universe write")
            .insert("SOLUSDT".to_string());

        assert_eq!(
            manager.recovery_symbols_snapshot().unwrap(),
            vec!["BTCUSDT".to_string(), "SOLUSDT".to_string()]
        );
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

        assert!(
            !manager
                .handle_message(message, Some("private-futures-1"))
                .await
        );

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
                connection_id,
                exchange_event_time_ms,
                receive_time_ms,
                process_time_ms,
                persist_time_ms,
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
                assert_eq!(connection_id.as_deref(), Some("private-futures-1"));
                assert_eq!(exchange_event_time_ms, Some(1710000000100));
                assert!(receive_time_ms.is_some());
                assert!(process_time_ms.is_some());
                assert_eq!(persist_time_ms, None);
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

        assert!(!manager.handle_message(message, None).await);

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
                .handle_message(r#"{"e":"listenKeyExpired","E":1710000000000}"#, None)
                .await
        );
        assert!(
            !manager
                .handle_message(r#"{"e":"unknownPrivateEvent"}"#, None)
                .await
        );
        assert!(!manager.handle_message("not-json", None).await);
    }

    #[test]
    fn pre_ready_drain_consumes_every_immediately_ready_frame() {
        let ready = stream::iter(vec![
            Ok::<Message, ()>(Message::Text("first".into())),
            Ok::<Message, ()>(Message::Ping(vec![1, 2, 3])),
            Ok::<Message, ()>(Message::Text("second".into())),
        ]);
        let pending = stream::pending::<Result<Message, ()>>();
        let mut socket = ready.chain(pending);

        let frames = take_immediately_ready_frames(&mut socket, 3).expect("ready batch");
        assert_eq!(frames.len(), 3);
        assert!(matches!(&frames[0], Message::Text(text) if text == "first"));
        assert!(matches!(&frames[1], Message::Ping(payload) if payload == &[1, 2, 3]));
        assert!(matches!(&frames[2], Message::Text(text) if text == "second"));
        assert!(
            take_immediately_ready_frames(&mut socket, 1)
                .expect("quiescent stream")
                .is_empty()
        );
    }

    #[test]
    fn pre_ready_drain_fails_closed_on_close_and_capacity_exhaustion() {
        let pending = stream::pending::<Result<Message, ()>>();
        let mut closed = stream::iter(vec![Ok::<Message, ()>(Message::Close(None))]).chain(pending);
        assert!(
            take_immediately_ready_frames(&mut closed, 1)
                .unwrap_err()
                .contains("closed before readiness")
        );

        let pending = stream::pending::<Result<Message, ()>>();
        let mut over_capacity =
            stream::iter(vec![Ok::<Message, ()>(Message::Text("queued".into()))]).chain(pending);
        assert!(
            take_immediately_ready_frames(&mut over_capacity, 0)
                .unwrap_err()
                .contains("safety cap")
        );
    }

    #[tokio::test]
    async fn retiring_expired_futures_key_closes_it_and_never_reuses_it() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let server = tokio::spawn(async move {
            let (mut socket, _) = listener.accept().await.unwrap();
            let mut request = vec![0_u8; 4096];
            let read = socket.read(&mut request).await.unwrap();
            let request = String::from_utf8_lossy(&request[..read]);
            assert!(
                request.starts_with("DELETE /fapi/v1/listenKey?listenKey=expired-key HTTP/1.1")
            );
            // Even an exchange-side close failure must not put the expired key
            // back into reconnect state.
            let body = r#"{"code":-1125,"msg":"listen key does not exist"}"#;
            let response = format!(
                "HTTP/1.1 400 Bad Request\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            );
            socket.write_all(response.as_bytes()).await.unwrap();
        });

        let (mut manager, _rx) = test_manager();
        manager.rest_client.fut_base_url = format!("http://{address}");
        manager.listen_key = Some("expired-key".to_string());
        manager.retire_current_listen_key("after test expiry").await;
        assert!(manager.listen_key.is_none());
        server.await.unwrap();
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

    #[tokio::test]
    async fn private_stream_cursor_is_monotonic_and_rejects_regression() {
        let (mut manager, _rx) = test_manager();
        manager.cursor_path = std::env::temp_dir().join(format!(
            "bongus-private-cursor-{}-{}.jsonl",
            std::process::id(),
            rand::random::<u64>()
        ));
        manager.append_cursor(1_000).await.unwrap();
        manager.append_cursor(2_000).await.unwrap();
        assert_eq!(manager.load_cursor().await.unwrap(), Some(2_000));
        assert!(
            manager
                .append_cursor(1_500)
                .await
                .unwrap_err()
                .contains("regressed")
        );
        assert_eq!(manager.load_cursor().await.unwrap(), Some(2_000));
        std::fs::remove_file(&manager.cursor_path).ok();
    }

    #[tokio::test]
    async fn private_stream_cursor_compacts_within_its_byte_cap() {
        let (mut manager, _rx) = test_manager();
        manager.cursor_path = std::env::temp_dir().join(format!(
            "bongus-private-cursor-cap-{}-{}.jsonl",
            std::process::id(),
            rand::random::<u64>()
        ));
        manager.cursor_max_bytes = 512;
        for through_ms in 1_000..1_100 {
            manager.append_cursor(through_ms).await.unwrap();
        }
        assert!(manager.cursor_path.metadata().unwrap().len() <= 512);
        assert_eq!(manager.load_cursor().await.unwrap(), Some(1_099));
        std::fs::remove_file(&manager.cursor_path).ok();
    }

    #[test]
    fn expired_private_cursor_rebases_to_bounded_history_without_skipping_reconciliation() {
        let end = 10 * 24 * 60 * 60 * 1000;
        let stale_cursor = end - MAX_RECOVERABLE_GAP_MS;
        let (start, rebased) =
            bounded_backfill_start(UserDataStreamKind::Futures, end, Some(stale_cursor))
                .expect("stale cursor should rebase");
        assert!(rebased);
        assert_eq!(start, end - MAX_RECOVERABLE_GAP_MS);
        assert!(
            bounded_backfill_start(UserDataStreamKind::Futures, end, Some(end + 60_001))
                .unwrap_err()
                .contains("ahead")
        );
    }

    #[test]
    fn futures_private_stream_uses_the_routed_listen_key_contract() {
        assert_eq!(
            UserDataWsManager::futures_private_stream_url(
                "wss://fstream.binance.com/private",
                "redacted-listen-key",
            ),
            "wss://fstream.binance.com/private/ws?listenKey=redacted-listen-key&events=ORDER_TRADE_UPDATE/ACCOUNT_UPDATE"
        );
    }

    #[test]
    fn spot_subscription_rejection_keeps_safe_exchange_diagnostics() {
        let response = serde_json::json!({
            "id": "request-id",
            "status": 400,
            "error": {"code": -1021, "msg": "Timestamp outside recvWindow"}
        });
        let reason = UserDataWsManager::spot_subscription_rejection(&response);
        assert!(reason.contains("status=400"));
        assert!(reason.contains("code=-1021"));
        assert!(reason.contains("Timestamp outside recvWindow"));
        assert!(!reason.contains("apiKey"));
        assert!(!reason.contains("signature"));
    }
}
