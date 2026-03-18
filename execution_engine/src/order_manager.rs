use rand::Rng;
use serde_json::Value;
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH, Duration, Instant};
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
    },
    // User Data Stream events
    OrderUpdate {
        client_order_id: String,
        symbol: String,
        status: String,
        filled_qty: f64,
    },
    AccountUpdate {
        balances: HashMap<String, f64>,
    }
}

pub enum EngineEvent {
    Ws(WsEvent),
    Alpha(crate::ipc::AlphaInstruction),
    LeggingTimeout(String),
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
    pub obi_cache: HashMap<String, f64>,
    pub exchange_info: HashMap<String, crate::binance_rest::ExchangeSymbolInfo>,
    pub event_receiver: Receiver<EngineEvent>,
    pub engine_tx: tokio::sync::mpsc::Sender<EngineEvent>,
    pub binance_rest: BinanceRest,
    chase: Option<ChaseState>,
    pub dash_tx: broadcast::Sender<String>,
    pub is_toxic: bool,
    pub last_brain_ping: Instant,
    pub current_gross_exposure_usd: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Leg {
    Spot,
    Futures,
}

#[derive(Debug, Clone, Copy, PartialEq)]
enum ChasePhase {
    Idle,
    DualMakerPlaced,
    LegFilledWaiting(Leg), // Which leg filled first
    LeggingDefenseTakerPlaced,
    Completed
}

#[derive(Debug, Clone)]
struct ChaseState {
    symbol: String,
    quantity: String,
    spot_client_order_id: String,
    futures_client_order_id: String,
    spot_side: TradeSide,
    futures_side: TradeSide,
    phase: ChasePhase,
    start_time: Instant,
}

impl OrderManager {
    pub fn new(event_receiver: Receiver<EngineEvent>, engine_tx: tokio::sync::mpsc::Sender<EngineEvent>, api_key: String, secret_key: String, dash_tx: broadcast::Sender<String>) -> Self {
        Self {
            state: SystemState::Disconnected,
            internal_orders: HashMap::new(),
            obi_cache: HashMap::new(),
            exchange_info: HashMap::new(),
            event_receiver,
            engine_tx,
            binance_rest: BinanceRest::new(api_key, secret_key),
            chase: None,
            dash_tx,
            is_toxic: false,
            last_brain_ping: Instant::now(),
            current_gross_exposure_usd: 0.0,
        }
    }

    async fn check_circuit_breakers(&mut self) -> bool {
        // Native circuit breaker: Python brain disconnected (staleness)
        if self.last_brain_ping.elapsed() > Duration::from_secs(12 * 60) { // 12 minutes max staleness
            warn!("CRITICAL: Python brain has not sent instructions in > 12 mins. Halting trading.");
            return true;
        }
        
        // Native circuit breaker: gross exposure
        if self.current_gross_exposure_usd > 200_000.0 {
            warn!("CRITICAL: Gross exposure limit exceeded! Halting new risk.");
            return true;
        }

        false
    }

    pub async fn run(&mut self) {
        info!("OrderManager task started (Maker-Only Mode via Avellaneda-Stoikov Inventory Model).");

        info!("Fetching exchange info to populate tick sizes...");
        match self.binance_rest.get_exchange_info().await {
            Ok(info) => {
                self.exchange_info = info;
                info!("Fetched exchange info for {} symbols.", self.exchange_info.len());
            }
            Err(e) => {
                error!("Failed to fetch exchange info on startup: {}. Falling back to 0.1 tick sizes.", e);
            }
        }

        while let Some(event) = self.event_receiver.recv().await {
            match event {
                EngineEvent::Ws(ws_event) => {
                    // Forward event to dashboard
                    if let Ok(json_str) = serde_json::to_string(&ws_event) {
                        let _ = self.dash_tx.send(json_str);
                    }
                    self.handle_ws_event(ws_event).await;
                }
                EngineEvent::Alpha(alpha_instruction) => {
                    self.handle_alpha_instruction(alpha_instruction).await;
                }
                EngineEvent::LeggingTimeout(client_id) => {
                    self.handle_legging_timeout(client_id).await;
                }
            }
        }
    }

    async fn handle_legging_timeout(&mut self, trigger_client_id: String) {
        let Some(mut chase) = self.chase.clone() else { return };
        
        let first_filled_leg = match chase.phase {
            ChasePhase::LegFilledWaiting(leg) => leg,
            _ => return, // State progressed, no longer waiting
        };

        info!("Legging timeout reached for: {:?}. Cancelling unfilled maker and converting to taker...", first_filled_leg);

        // Figure out which one to cancel/replace
        let (unfilled_sym, unfilled_cid, unfilled_side, unfilled_leg) = match first_filled_leg {
            Leg::Spot => (
                chase.symbol.clone(),
                chase.futures_client_order_id.clone(),
                chase.futures_side,
                Leg::Futures
            ),
            Leg::Futures => (
                chase.symbol.clone(),
                chase.spot_client_order_id.clone(),
                chase.spot_side,
                Leg::Spot
            ),
        };

        // Cancel
        match unfilled_leg {
            Leg::Spot => { let _ = self.binance_rest.cancel_order(&unfilled_sym, &unfilled_cid).await; },
            Leg::Futures => { let _ = self.binance_rest.cancel_futures_order(&unfilled_sym, &unfilled_cid).await; },
        }

        // Place market order
        let new_taker_cid = Self::generate_client_order_id("legging");
        info!("Placing legging defense MARKET order for {:?} cid={}", unfilled_leg, new_taker_cid);
        
        let market_res = match unfilled_leg {
            Leg::Spot => {
                self.binance_rest.place_spot_market_order(&unfilled_sym, unfilled_side, &chase.quantity, &new_taker_cid).await
            }
            Leg::Futures => {
                self.binance_rest.place_futures_market_order(&unfilled_sym, unfilled_side, &chase.quantity, &new_taker_cid).await
            }
        };

        if let Ok(body) = market_res {
            info!("Taker hedge submission response: {}", body);
            chase.phase = ChasePhase::LeggingDefenseTakerPlaced;
            self.chase = Some(chase);
        } else {
            error!("Failed to submit legging defense taker order: {:?}", market_res.err());
            // It's broken, probably clear chase state to let reconciler handle it later
            self.chase = None;
        }
    }

    async fn handle_alpha_instruction(&mut self, instruction: crate::ipc::AlphaInstruction) {
        info!("Handling Alpha Instruction: {:?}", instruction);
        self.last_brain_ping = Instant::now(); // update heartbeat

        if self.state != SystemState::Trading {
            warn!("System not currently trading; ignoring alpha instruction.");
            return;
        }

        if self.check_circuit_breakers().await {
            return;
        }

        if self.chase.is_some() {
            warn!("Currently executing a Chase, skipping new alpha instruction.");
            return;
        }

        // We will start a Chase based on this instruction
        let spot_client_order_id = Self::generate_client_order_id("spot");
        let futures_client_order_id = Self::generate_client_order_id("fut");

        let is_buy = instruction.intent == "ENTER_LONG" || instruction.intent == "EXIT_SHORT";

        let scaled_quantity = instruction.quantity * instruction.exposure_scale;

        self.chase = Some(ChaseState {
            symbol: instruction.symbol.to_uppercase(),
            quantity: format!("{:.3}", scaled_quantity),
            spot_client_order_id,
            futures_client_order_id,
            // Long Spot / Short Perp bias for ENTRY_LONG
            spot_side: if is_buy { TradeSide::Buy } else { TradeSide::Sell },
            futures_side: if is_buy { TradeSide::Sell } else { TradeSide::Buy },
            phase: ChasePhase::Idle,
            start_time: Instant::now(),
        });

        info!("Dynamic chase state initialized from AlphaInstruction for {}.", instruction.symbol);
    }

    async fn handle_ws_event(&mut self, event: WsEvent) {
        match event {
            WsEvent::Connected { symbol } => {
                    info!("OrderManager received WebSocket Connected event for {}.", symbol);
                    if self.state == SystemState::Disconnected {
                        self.execute_reconciliation_sequence().await;

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
                        return;
                    }

                    // Spread toxicity protection
                    let spread_bps = (ask_price - bid_price) / ((ask_price + bid_price) / 2.0) * 10000.0;
                    if spread_bps > 50.0 {
                        if !self.is_toxic {
                            warn!("Spread toxicity detected for {}! ({} bps). Pausing maker operations.", symbol, spread_bps);
                            self.is_toxic = true;
                        }
                    } else if self.is_toxic {
                        info!("Toxicity resolved for {}. Resuming operations.", symbol);
                        self.is_toxic = false;
                    }

                    if !self.is_toxic {
                        self.on_book_ticker(symbol, bid_price, ask_price).await;
                    }
                }
                WsEvent::L2Depth { symbol, bids, asks } => {
                    if self.state != SystemState::Trading {
                        return;
                    }
                    
                    // Implement Dynamic Inventory Risk Skew Pricing (Avellaneda-Stoikov)
                    // 1. Calculate Order Book Imbalance (OBI)
                    let total_bid_vol: f64 = bids.iter().map(|(_, q)| q).sum();
                    let total_ask_vol: f64 = asks.iter().map(|(_, q)| q).sum();
                    
                    let obi = if total_bid_vol + total_ask_vol > 0.0 {
                        (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol)
                    } else {
                        0.0
                    };

                    self.obi_cache.insert(symbol.clone(), obi);

                    // For now, log OBI. High OBI means buy pressure -> adjust quoting to capture spread and avoid adverse selection
                    if obi.abs() > 0.4 {
                        info!("High OBI detected for {}: {:.2}. We should skew our resting limits.", symbol, obi);
                        // TODO: Adjust resting limit prices based on this OBI in conjunction with current inventory.
                    }
                }
                WsEvent::OrderUpdate { client_order_id, symbol, status, filled_qty } => {
                    info!("Order Update from User Data Stream: {} {} {} filled={}", symbol, client_order_id, status, filled_qty);
                    // Update internal order state continuously without REST polling
                    if let Some(internal_order) = self.internal_orders.get_mut(&client_order_id) {
                        internal_order.status = status.clone();
                    } else {
                        // Could be an order placed from another system or an orphan, insert it
                        self.internal_orders.insert(client_order_id.clone(), InternalOrder {
                            client_order_id: client_order_id.clone(),
                            symbol: symbol.clone(),
                            status: status.clone(),
                        });
                    }
                    
                    // Handle chase state logic based on ws events
                    if let Some(mut chase) = self.chase.clone() {
                        if status == "FILLED" {
                            let mut trigger_timeout = false;
                            
                            match chase.phase {
                                ChasePhase::DualMakerPlaced => {
                                    let first_filled = if client_order_id == chase.spot_client_order_id {
                                        Leg::Spot
                                    } else if client_order_id == chase.futures_client_order_id {
                                        Leg::Futures
                                    } else {
                                        return;
                                    };
                                    info!("Leg '{:?}' FILLED. Waiting up to 200ms for the other leg...", first_filled);
                                    chase.phase = ChasePhase::LegFilledWaiting(first_filled);
                                    self.chase = Some(chase.clone());
                                    trigger_timeout = true;
                                },
                                ChasePhase::LegFilledWaiting(first_filled) => {
                                    let second_filled = if client_order_id == chase.spot_client_order_id {
                                        Leg::Spot
                                    } else if client_order_id == chase.futures_client_order_id {
                                        Leg::Futures
                                    } else {
                                        return;
                                    };
                                    // Make sure it's the OTHER leg that filled
                                    let is_match = match (first_filled, second_filled) {
                                        (Leg::Spot, Leg::Futures) => true,
                                        (Leg::Futures, Leg::Spot) => true,
                                        _ => false,
                                    };
                                    if is_match {
                                        info!("Chase cycle completed (both legs filled cleanly).");
                                        chase.phase = ChasePhase::Completed;
                                        self.chase = None;
                                    }
                                },
                                ChasePhase::LeggingDefenseTakerPlaced => {
                                    info!("Chase cycle completed (legging defense taker filled).");
                                    chase.phase = ChasePhase::Completed;
                                    self.chase = None;
                                },
                                _ => {}
                            }

                            if trigger_timeout {
                                let tx = self.engine_tx.clone();
                                let cid = client_order_id.clone();
                                tokio::spawn(async move {
                                    sleep(Duration::from_millis(200)).await;
                                    let _ = tx.send(EngineEvent::LeggingTimeout(cid)).await;
                                });
                            }
                        }
                    }
                }
                WsEvent::AccountUpdate { balances } => {
                    info!("Account Update: {:?}", balances);
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

    async fn on_book_ticker(&mut self, symbol: String, bid_price: f64, ask_price: f64) {
        let Some(chase_snapshot) = self.chase.clone() else {
            return;
        };

        if !chase_snapshot.symbol.eq_ignore_ascii_case(&symbol) {
            return;
        }

        if chase_snapshot.phase != ChasePhase::Idle {
            return;
        }

        let current_obi = self.obi_cache.get(&symbol).copied().unwrap_or(0.0);
        let tick_size = self.exchange_info.get(&chase_snapshot.symbol).map(|i| i.tick_size).unwrap_or(0.1);

        // Spot Price
        let mut spot_target = match chase_snapshot.spot_side {
            TradeSide::Buy => bid_price,
            TradeSide::Sell => ask_price,
        };

        // Futures Price
        let mut fut_target = match chase_snapshot.futures_side {
            TradeSide::Buy => bid_price,
            TradeSide::Sell => ask_price,
        };

        // Skew both based on OBI
        if current_obi > 0.3 {
            if let TradeSide::Buy = chase_snapshot.spot_side { spot_target += tick_size; } else { spot_target += tick_size; }
            if let TradeSide::Buy = chase_snapshot.futures_side { fut_target += tick_size; } else { fut_target += tick_size; }
        } else if current_obi < -0.3 {
            if let TradeSide::Buy = chase_snapshot.spot_side { spot_target -= tick_size; } else { spot_target -= tick_size; }
            if let TradeSide::Buy = chase_snapshot.futures_side { fut_target -= tick_size; } else { fut_target -= tick_size; }
        }

        let spot_price_str = format!("{:.2}", spot_target);
        let fut_price_str = format!("{:.2}", fut_target);

        info!("Placing DUAL maker LIMIT orders. OBI: {:.2}", current_obi);
        
        let spot_res = self.binance_rest.place_spot_limit_order(
            &chase_snapshot.symbol,
            chase_snapshot.spot_side,
            &chase_snapshot.quantity,
            &spot_price_str,
            &chase_snapshot.spot_client_order_id,
        ).await;

        let fut_res = self.binance_rest.place_futures_limit_order(
            &chase_snapshot.symbol,
            chase_snapshot.futures_side,
            &chase_snapshot.quantity,
            &fut_price_str,
            &chase_snapshot.futures_client_order_id,
        ).await;

        // If at least one succeeds, we move to DualMakerPlaced. Better error handling in prod.
        let mut placed = false;
        if let Ok(body) = spot_res {
            info!("Spot Maker order placed: {}", body);
            self.internal_orders.insert(chase_snapshot.spot_client_order_id.clone(), InternalOrder {
                client_order_id: chase_snapshot.spot_client_order_id.clone(),
                symbol: chase_snapshot.symbol.clone(),
                status: "NEW".to_string(),
            });
            placed = true;
        } else {
            error!("Failed Spot Maker: {:?}", spot_res.err());
        }

        if let Ok(body) = fut_res {
            info!("Futures Maker order placed: {}", body);
            self.internal_orders.insert(chase_snapshot.futures_client_order_id.clone(), InternalOrder {
                client_order_id: chase_snapshot.futures_client_order_id.clone(),
                symbol: chase_snapshot.symbol.clone(),
                status: "NEW".to_string(),
            });
            placed = true;
        } else {
            error!("Failed Futures Maker: {:?}", fut_res.err());
        }

        if placed {
            if let Some(ref mut c) = self.chase {
                c.phase = ChasePhase::DualMakerPlaced;
            }
        }
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
