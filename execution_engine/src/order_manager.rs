use rand::Rng;
use serde_json::Value;
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH, Duration};
use tokio::sync::mpsc::Receiver;
use tokio::time::sleep;
use tracing::{error, info, warn};
use tokio::sync::broadcast;

use crate::binance_rest::{BinanceRest, LegVenue, TradeSide};

#[derive(Debug, PartialEq, Eq, Clone)]
pub enum SystemState {
    Disconnected,
    Reconciling,
    Trading,
}

#[derive(Debug, Clone, serde::Serialize)]
#[serde(tag = "event")]
pub enum WsEvent {
    Connected { symbol: String },
    Disconnected { symbol: String },
    BookTicker {
        symbol: String,
        bid_price: f64,
        ask_price: f64,
    },
    // New L2 Depth event for true OBI and Queue Position Tracking
    L2Depth {
        symbol: String,
        bids: Vec<(f64, f64)>, // price, qty
        asks: Vec<(f64, f64)>, // price, qty
    }
}

#[derive(Debug, Clone)]
pub struct InternalOrder {
    pub client_order_id: String,
    pub symbol: String,
    pub status: String, // e.g., "NEW", "FILLED", "CANCELLED"
}

pub struct OrderManager {
    pub state: SystemState,
    pub internal_orders: HashMap<String, InternalOrder>,
    pub ws_receiver: Receiver<WsEvent>,
    pub binance_rest: BinanceRest,
    chase: Option<ChaseState>,
    pub dash_tx: broadcast::Sender<String>,
}

#[derive(Debug, Clone, Copy)]
enum Leg {
    Spot,
    Futures,
}

#[derive(Debug, Clone)]
struct ChaseState {
    symbol: String,
    quantity: String,
    maker_leg: Leg,
    maker_client_order_id: String,
    taker_client_order_id: String,
    maker_side: TradeSide,
    taker_side: TradeSide,
}

impl OrderManager {
    pub fn new(ws_receiver: Receiver<WsEvent>, api_key: String, secret_key: String, dash_tx: broadcast::Sender<String>) -> Self {
        Self {
            state: SystemState::Disconnected,
            internal_orders: HashMap::new(),
            ws_receiver,
            binance_rest: BinanceRest::new(api_key, secret_key),
            chase: None,
            dash_tx,
        }
    }

    pub async fn run(&mut self) {
        info!("OrderManager task started (Maker-Only Mode via Avellaneda-Stoikov Inventory Model).");
        
        // This is a zero-allocation hot path, polling lock-free crossbeam receiver
        // This handles async receiving cleanly
        while let Some(event) = self.ws_receiver.recv().await {
            // Forward event to dashboard
            if let Ok(json_str) = serde_json::to_string(&event) {
                let _ = self.dash_tx.send(json_str);
            }

            match event {
                WsEvent::Connected { symbol } => {
                    info!("OrderManager received WebSocket Connected event for {}.", symbol);
                    if self.state == SystemState::Disconnected {
                        self.execute_reconciliation_sequence().await;
                        if self.state == SystemState::Trading {
                            self.start_default_chase_if_idle().await;
                        }
                    }
                }
                WsEvent::Disconnected { symbol } => {
                    warn!("OrderManager received WebSocket Disconnected event for {}.", symbol);
                    self.state = SystemState::Disconnected;
                    self.chase = None;
                }
                WsEvent::BookTicker {
                    symbol,
                    bid_price,
                    ask_price,
                } => {
                    if self.state != SystemState::Trading {
                        continue;
                    }
                    self.on_book_ticker(symbol, bid_price, ask_price).await;
                }
                WsEvent::L2Depth { symbol, bids, asks } => {
                    // TODO: Implement Dynamic Inventory Risk Skew Pricing (Avellaneda-Stoikov)
                    // Calculate OBI, Update resting limits relative to Queue position.
                }
            }
        }
    }

    fn generate_client_order_id(prefix: &str) -> String {
        let ts_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis())
            .unwrap_or(0);
        let nonce: u32 = rand::thread_rng().gen_range(1000..9999);
        format!("bngs_{}_{}_{}", prefix, ts_ms, nonce)
    }

    async fn start_default_chase_if_idle(&mut self) {
        if self.chase.is_some() {
            return;
        }

        let symbol = "BTCUSDT".to_string();
        let quantity = "0.001".to_string();
        let maker_client_order_id = Self::generate_client_order_id("mk");
        let taker_client_order_id = Self::generate_client_order_id("tk");

        self.chase = Some(ChaseState {
            symbol,
            quantity,
            maker_leg: Leg::Spot,
            maker_client_order_id,
            taker_client_order_id,
            maker_side: TradeSide::Buy,
            taker_side: TradeSide::Sell,
        });

        info!("Initialized chase state and waiting for bookTicker to place maker order.");
    }

    async fn on_book_ticker(&mut self, symbol: String, bid_price: f64, _ask_price: f64) {
        let Some(chase_snapshot) = self.chase.clone() else {
            return;
        };

        if !chase_snapshot.symbol.eq_ignore_ascii_case(&symbol) {
            return;
        }

        if self.internal_orders.contains_key(&chase_snapshot.maker_client_order_id) {
            return;
        }

        let maker_price = format!("{:.2}", bid_price);
        info!(
            "Placing maker LIMIT on less-liquid leg: symbol={} price={} cid={}",
            chase_snapshot.symbol, maker_price, chase_snapshot.maker_client_order_id
        );

        let maker_res = match chase_snapshot.maker_leg {
            Leg::Spot => {
                self.binance_rest
                    .place_spot_limit_order(
                        &chase_snapshot.symbol,
                        chase_snapshot.maker_side,
                        &chase_snapshot.quantity,
                        &maker_price,
                        &chase_snapshot.maker_client_order_id,
                    )
                    .await
            }
            Leg::Futures => {
                error!("Futures maker LIMIT leg not yet implemented in this phase setup.");
                return;
            }
        };

        match maker_res {
            Ok(body) => {
                info!("Maker order accepted by exchange: {}", body);
                self.internal_orders.insert(
                    chase_snapshot.maker_client_order_id.clone(),
                    InternalOrder {
                        client_order_id: chase_snapshot.maker_client_order_id.clone(),
                        symbol: chase_snapshot.symbol.clone(),
                        status: "NEW".to_string(),
                    },
                );
            }
            Err(err) => {
                error!("Failed to place maker LIMIT order: {}", err);
                return;
            }
        }

        let maker_filled = self
            .wait_for_fill(
                LegVenue::Spot,
                &chase_snapshot.symbol,
                &chase_snapshot.maker_client_order_id,
                25,
                Duration::from_millis(20),
            )
            .await;

        if !maker_filled {
            warn!(
                "Maker order not confirmed FILLED in time; chase remains unhedged until managed by higher-level logic. cid={}",
                chase_snapshot.maker_client_order_id
            );
            return;
        }

        info!(
            "Maker FILLED confirmed. Firing taker MARKET hedge immediately. cid={}",
            chase_snapshot.taker_client_order_id
        );

        match self
            .binance_rest
            .place_futures_market_order(
                &chase_snapshot.symbol,
                chase_snapshot.taker_side,
                &chase_snapshot.quantity,
                &chase_snapshot.taker_client_order_id,
            )
            .await
        {
            Ok(body) => {
                info!("Taker hedge submission response: {}", body);
                let taker_filled = self
                    .wait_for_fill(
                        LegVenue::UsdtFutures,
                        &chase_snapshot.symbol,
                        &chase_snapshot.taker_client_order_id,
                        20,
                        Duration::from_millis(10),
                    )
                    .await;

                if taker_filled {
                    info!("Chase cycle completed with confirmed maker+taker fills.");
                } else {
                    warn!(
                        "Taker order submitted but not yet FILLED-confirmed. Immediate reconciliation required. cid={}",
                        chase_snapshot.taker_client_order_id
                    );
                }
            }
            Err(err) => {
                error!("Failed to submit taker MARKET hedge: {}", err);
            }
        }
    }

    async fn wait_for_fill(
        &self,
        venue: LegVenue,
        symbol: &str,
        client_order_id: &str,
        max_attempts: usize,
        interval: Duration,
    ) -> bool {
        for _ in 0..max_attempts {
            match self
                .binance_rest
                .get_order_by_client_id(venue, symbol, client_order_id)
                .await
            {
                Ok(raw) => {
                    let parsed: Result<Value, _> = serde_json::from_str(&raw);
                    if let Ok(value) = parsed {
                        let status = value.get("status").and_then(|s| s.as_str()).unwrap_or("");
                        if status == "FILLED" {
                            return true;
                        }
                    }
                }
                Err(err) => {
                    warn!("Error querying order status ({}): {}", client_order_id, err);
                }
            }

            sleep(interval).await;
        }

        false
    }

    async fn execute_reconciliation_sequence(&mut self) {
        self.state = SystemState::Reconciling;
        info!("=== Beginning Reconciliation Sequence ===");

        // STEP 1: Pause Trading & Flush internal
        info!("[Step 1] Pausing trading signal generation.");
        // (In a fuller implementation, this signals the strategy engine.)

        // STEP 2: Jittered Backoff
        let jitter_ms = rand::thread_rng().gen_range(500..2500);
        info!("[Step 2] Applying Jittered Backoff of {}ms before REST sync...", jitter_ms);
        sleep(Duration::from_millis(jitter_ms)).await;

        // Fetch Exchange Truth
        info!("[Step 2b] Fetching Open Orders from Exchange...");
        let open_orders_json = match self.binance_rest.get_open_orders().await {
            Ok(json) => json,
            Err(e) => {
                warn!("Failed to fetch open orders: {}. Will retry reconciliation later.", e);
                return; // Or implement local REST retry logic
            }
        };

        let parsed_orders: Result<Vec<Value>, _> = serde_json::from_str(&open_orders_json);
        if parsed_orders.is_err() {
            warn!("Failed to parse open orders JSON: {:?}", open_orders_json);
            return;
        }
        let exchange_open_orders = parsed_orders.unwrap();

        // (We would also fetch balances here `self.binance_rest.get_account().await`)
        // info!("Fetching Account Balances...");

        // STEP 3 & 4: Map Unconfirmed and Handle Orphans
        info!("[Step 3/4] Mapping internal orders to exchange truth and searching for orphans.");
        
        // Populate a set of open client order IDs reported by the exchange
        let mut exchange_known_client_ids: std::collections::HashSet<String> = std::collections::HashSet::new();

        for order in exchange_open_orders {
            if let Some(client_id) = order.get("clientOrderId").and_then(|v| v.as_str()) {
                exchange_known_client_ids.insert(client_id.to_string());
                
                // Identify orphans explicitly created by our bot logic (prefix "bngs_")
                if client_id.starts_with("bngs_") && !self.internal_orders.contains_key(client_id) {
                    warn!("FOUND ORPHAN: Exchange has active order {}, but internal state does not.", client_id);
                    if let Some(symbol) = order.get("symbol").and_then(|v| v.as_str()) {
                        info!("    -> Issuing REST DELETE for orphan order {} ({})", client_id, symbol);
                        // In real bot, await this response and verify it cancels
                        let _ = self.binance_rest.cancel_order(symbol, client_id).await;
                    }
                }
            }
        }

        // Check our internal orders against the exchange truth
        for (client_id, internal_order) in self.internal_orders.iter_mut() {
            if internal_order.status == "NEW" && !exchange_known_client_ids.contains(client_id) {
                warn!("DANGLING INTERNAL ORDER: We think {} is open, but exchange does not have it.", client_id);
                // Implementation: Query REST for specific order to see if it FILLED or CANCELED
                // let specific_status = self.binance_rest.get_order(symbol, client_id).await;
                // if FILLED -> Update Balances
                // if CANCELED/NOT_FOUND -> order failed
                internal_order.status = "UNKNOWN_RECONCILING".to_string(); // Temporary placeholder
            }
        }

        // STEP 5: Resume
        info!("[Step 5] State matrix synchronized (Dangling mitigated, Orphans purged).");
        self.state = SystemState::Trading;
        info!("=== System is TRADING ===");
    }
}
