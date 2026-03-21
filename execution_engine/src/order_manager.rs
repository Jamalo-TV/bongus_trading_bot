use rand::Rng;
use serde_json::Value;
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH, Duration, Instant};
use tokio::sync::mpsc::Receiver;
use tokio::time::sleep;
use tracing::{error, info, warn};
use tokio::sync::broadcast;

use crate::binance_rest::{BinanceRest, LegVenue, TradeSide};
use crate::collateral_engine::{UnifiedPortfolioMarginCalculator, Position, PositionSide};

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
    L2Depth {
        symbol: String,
        bids: Vec<(f64, f64)>,
        asks: Vec<(f64, f64)>,
    },
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
    pub status: String,
    pub limit_price: Option<f64>,
}

#[derive(Debug, Clone)]
pub struct TrackedPosition {
    pub symbol: String,
    pub side: String,
    pub entry_price: f64,
    pub quantity: f64,
    pub unrealized_pnl: f64,
    pub last_mark_price: f64,
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
    pub max_gross_exposure_usd: f64,
    pub account_equity_usd: f64,
    pub tracked_positions: HashMap<String, TrackedPosition>,
    pub collateral_calc: UnifiedPortfolioMarginCalculator,
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
    LegFilledWaiting(Leg),
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
    expected_spot_price: f64,
    expected_fut_price: f64,
}

impl OrderManager {
    pub fn new(
        event_receiver: Receiver<EngineEvent>,
        engine_tx: tokio::sync::mpsc::Sender<EngineEvent>,
        api_key: String,
        secret_key: String,
        dash_tx: broadcast::Sender<String>,
    ) -> Self {
        let max_gross_exposure = std::env::var("MAX_GROSS_EXPOSURE_USD")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(50_000.0);

        let account_equity = std::env::var("ACCOUNT_EQUITY_USD")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(10_000.0);

        info!("OrderManager config: max_gross_exposure=${}, account_equity=${}", max_gross_exposure, account_equity);

        let collateral_calc = UnifiedPortfolioMarginCalculator::new(
            account_equity,
            0.004,  // 0.4% maintenance margin rate
            0.8,    // 80% danger threshold
        );

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
            max_gross_exposure_usd: max_gross_exposure,
            account_equity_usd: account_equity,
            tracked_positions: HashMap::new(),
            collateral_calc,
        }
    }

    async fn check_circuit_breakers(&mut self) -> bool {
        // Circuit breaker 1: Python brain disconnected (staleness)
        if self.last_brain_ping.elapsed() > Duration::from_secs(12 * 60) {
            warn!("CRITICAL: Python brain has not sent instructions in > 12 mins. Halting trading.");
            return true;
        }

        // Circuit breaker 2: gross exposure (from env)
        if self.current_gross_exposure_usd > self.max_gross_exposure_usd {
            warn!("CRITICAL: Gross exposure ${:.0} exceeds limit ${:.0}! Halting new risk.",
                self.current_gross_exposure_usd, self.max_gross_exposure_usd);
            return true;
        }

        // Circuit breaker 3: collateral engine margin check
        if !self.tracked_positions.is_empty() {
            let total_spot_notional: f64 = self.tracked_positions.values()
                .filter(|p| p.side == "LONG")
                .map(|p| p.last_mark_price * p.quantity)
                .sum();
            let total_perp_notional: f64 = self.tracked_positions.values()
                .filter(|p| p.side == "SHORT")
                .map(|p| p.last_mark_price * p.quantity)
                .sum();
            let total_upnl: f64 = self.tracked_positions.values()
                .map(|p| p.unrealized_pnl)
                .sum();

            let unified_equity = self.account_equity_usd + total_upnl;
            if unified_equity <= 0.0 {
                warn!("CRITICAL: Unified equity is zero or negative! Kill switch.");
                return true;
            }

            let directional_risk = (total_spot_notional - total_perp_notional).abs();
            let uni_mmr = directional_risk * 0.004 / unified_equity;
            if uni_mmr >= 0.8 {
                warn!("CRITICAL: uniMMR {:.4} exceeds danger threshold 0.8! Halting.", uni_mmr);
                return true;
            }
        }

        false
    }

    pub async fn run(&mut self) {
        info!("OrderManager task started. Max exposure: ${:.0}, Account equity: ${:.0}",
            self.max_gross_exposure_usd, self.account_equity_usd);

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
            _ => return,
        };

        info!("Legging timeout reached for: {:?}. Cancelling unfilled maker and converting to taker...", first_filled_leg);

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

        match unfilled_leg {
            Leg::Spot => { let _ = self.binance_rest.cancel_order(&unfilled_sym, &unfilled_cid).await; },
            Leg::Futures => { let _ = self.binance_rest.cancel_futures_order(&unfilled_sym, &unfilled_cid).await; },
        }

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
            self.chase = None;
        }
    }

    async fn handle_alpha_instruction(&mut self, instruction: crate::ipc::AlphaInstruction) {
        info!("Handling Alpha Instruction: {:?}", instruction);
        self.last_brain_ping = Instant::now();

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

        let spot_client_order_id = Self::generate_client_order_id("spot");
        let futures_client_order_id = Self::generate_client_order_id("fut");

        let is_buy = instruction.intent == "ENTER_LONG" || instruction.intent == "EXIT_SHORT";
        let scaled_quantity = instruction.quantity * instruction.exposure_scale;

        self.chase = Some(ChaseState {
            symbol: instruction.symbol.to_uppercase(),
            quantity: format!("{:.5}", scaled_quantity),
            spot_client_order_id,
            futures_client_order_id,
            spot_side: if is_buy { TradeSide::Buy } else { TradeSide::Sell },
            futures_side: if is_buy { TradeSide::Sell } else { TradeSide::Buy },
            phase: ChasePhase::Idle,
            start_time: Instant::now(),
            expected_spot_price: 0.0,
            expected_fut_price: 0.0,
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

                    // Update tracked position PnL
                    let mid_price = (bid_price + ask_price) / 2.0;
                    if let Some(pos) = self.tracked_positions.get_mut(&symbol) {
                        pos.last_mark_price = mid_price;
                        pos.unrealized_pnl = match pos.side.as_str() {
                            "LONG" => (mid_price - pos.entry_price) * pos.quantity,
                            "SHORT" => (pos.entry_price - mid_price) * pos.quantity,
                            _ => 0.0,
                        };

                        // Broadcast PnL update to dashboard
                        let pnl_event = serde_json::json!({
                            "event": "PositionPnL",
                            "symbol": symbol,
                            "unrealized_pnl": pos.unrealized_pnl,
                            "mark_price": mid_price,
                        });
                        let _ = self.dash_tx.send(pnl_event.to_string());
                    }

                    // Spread toxicity protection
                    let spread_bps = (ask_price - bid_price) / ((ask_price + bid_price) / 2.0) * 10000.0;
                    if spread_bps > 50.0 {
                        if !self.is_toxic {
                            warn!("Spread toxicity detected for {}! ({:.1} bps). Pausing maker operations.", symbol, spread_bps);
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

                    let total_bid_vol: f64 = bids.iter().map(|(_, q)| q).sum();
                    let total_ask_vol: f64 = asks.iter().map(|(_, q)| q).sum();

                    let obi = if total_bid_vol + total_ask_vol > 0.0 {
                        (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol)
                    } else {
                        0.0
                    };

                    self.obi_cache.insert(symbol.clone(), obi);

                    if obi.abs() > 0.4 {
                        info!("High OBI detected for {}: {:.2}. Skewing resting limits.", symbol, obi);
                    }
                }
                WsEvent::OrderUpdate { client_order_id, symbol, status, filled_qty } => {
                    info!("Order Update: {} {} {} filled={}", symbol, client_order_id, status, filled_qty);

                    // Slippage monitoring on fills
                    if status == "FILLED" {
                        if let Some(internal) = self.internal_orders.get(&client_order_id) {
                            if let Some(expected_price) = internal.limit_price {
                                // We can't get actual fill price from this event alone in the
                                // simplified model, but log the expected price for monitoring
                                info!("Fill monitoring: {} expected_price={:.2}", client_order_id, expected_price);
                            }
                        }

                        // Update position tracking
                        if let Some(chase) = &self.chase {
                            if client_order_id == chase.spot_client_order_id {
                                let pos = TrackedPosition {
                                    symbol: symbol.clone(),
                                    side: match chase.spot_side { TradeSide::Buy => "LONG", TradeSide::Sell => "SHORT" }.to_string(),
                                    entry_price: chase.expected_spot_price,
                                    quantity: filled_qty,
                                    unrealized_pnl: 0.0,
                                    last_mark_price: chase.expected_spot_price,
                                };
                                let key = format!("{}_spot", symbol);
                                self.tracked_positions.insert(key, pos);
                                info!("Tracked spot position for {}", symbol);
                            } else if client_order_id == chase.futures_client_order_id {
                                let pos = TrackedPosition {
                                    symbol: symbol.clone(),
                                    side: match chase.futures_side { TradeSide::Buy => "LONG", TradeSide::Sell => "SHORT" }.to_string(),
                                    entry_price: chase.expected_fut_price,
                                    quantity: filled_qty,
                                    unrealized_pnl: 0.0,
                                    last_mark_price: chase.expected_fut_price,
                                };
                                let key = format!("{}_perp", symbol);
                                self.tracked_positions.insert(key, pos);
                                info!("Tracked perp position for {}", symbol);
                            }
                        }

                        // Recalculate gross exposure
                        self.current_gross_exposure_usd = self.tracked_positions.values()
                            .map(|p| p.last_mark_price * p.quantity)
                            .sum();
                    }

                    // Update internal order state
                    if let Some(internal_order) = self.internal_orders.get_mut(&client_order_id) {
                        internal_order.status = status.clone();
                    } else {
                        self.internal_orders.insert(client_order_id.clone(), InternalOrder {
                            client_order_id: client_order_id.clone(),
                            symbol: symbol.clone(),
                            status: status.clone(),
                            limit_price: None,
                        });
                    }

                    // Handle chase state logic
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

        let mut spot_target = match chase_snapshot.spot_side {
            TradeSide::Buy => bid_price,
            TradeSide::Sell => ask_price,
        };

        let mut fut_target = match chase_snapshot.futures_side {
            TradeSide::Buy => bid_price,
            TradeSide::Sell => ask_price,
        };

        // OBI-based price skewing
        if current_obi > 0.3 {
            spot_target += tick_size;
            fut_target += tick_size;
        } else if current_obi < -0.3 {
            spot_target -= tick_size;
            fut_target -= tick_size;
        }

        let spot_price_str = format!("{:.2}", spot_target);
        let fut_price_str = format!("{:.2}", fut_target);

        // Store expected prices for slippage monitoring
        if let Some(ref mut c) = self.chase {
            c.expected_spot_price = spot_target;
            c.expected_fut_price = fut_target;
        }

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

        let mut placed = false;
        if let Ok(body) = spot_res {
            info!("Spot Maker order placed: {}", body);
            self.internal_orders.insert(chase_snapshot.spot_client_order_id.clone(), InternalOrder {
                client_order_id: chase_snapshot.spot_client_order_id.clone(),
                symbol: chase_snapshot.symbol.clone(),
                status: "NEW".to_string(),
                limit_price: Some(spot_target),
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
                limit_price: Some(fut_target),
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

        if let Err(e) = self.binance_rest.sync_time().await {
            warn!("Failed to sync time with Binance: {}", e);
        } else {
            info!("Time synced successfully with Binance");
        }

        info!("[Step 1] Pausing trading signal generation.");

        let jitter_ms = rand::thread_rng().gen_range(500..2500);
        info!("[Step 2] Applying Jittered Backoff of {}ms before REST sync...", jitter_ms);
        sleep(Duration::from_millis(jitter_ms)).await;

        info!("[Step 2b] Fetching Open Orders from Exchange...");
        let open_orders_json = match self.binance_rest.get_open_orders().await {
            Ok(json) => json,
            Err(e) => {
                warn!("Failed to fetch open orders: {}. Will retry reconciliation later.", e);
                return;
            }
        };

        let parsed_orders: Result<Vec<Value>, _> = serde_json::from_str(&open_orders_json);
        if parsed_orders.is_err() {
            warn!("Failed to parse open orders JSON as array: {}", open_orders_json);
            warn!("Failing reconciliation due to API error. State remains Reconciling.");
            return;
        }
        let exchange_open_orders = parsed_orders.unwrap();

        info!("Fetching Account Balances...");
        if let Ok(account_json) = self.binance_rest.get_fapi_account().await {
            if let Ok(parsed_acc) = serde_json::from_str::<Value>(&account_json) {
                let mut balances_map = serde_json::Map::new();
                if let Some(assets) = parsed_acc.get("assets").and_then(|v| v.as_array()) {
                    for asset in assets {
                        if let (Some(asset_name), Some(wallet_balance)) = (
                            asset.get("asset").and_then(|v| v.as_str()),
                            asset.get("walletBalance").and_then(|v| v.as_str()),
                        ) {
                            if let Ok(bal) = wallet_balance.parse::<f64>() {
                                balances_map.insert(asset_name.to_string(), serde_json::json!(bal));
                            }
                        }
                    }
                }

                let update_event = serde_json::json!({
                    "event": "AccountUpdate",
                    "balances": balances_map
                });
                let _ = self.dash_tx.send(update_event.to_string());
            }
        }

        info!("[Step 3/4] Mapping internal orders to exchange truth and searching for orphans.");

        let mut exchange_known_client_ids: std::collections::HashSet<String> = std::collections::HashSet::new();

        for order in &exchange_open_orders {
            if let Some(client_id) = order.get("clientOrderId").and_then(|v| v.as_str()) {
                exchange_known_client_ids.insert(client_id.to_string());

                if client_id.starts_with("bngs_") && !self.internal_orders.contains_key(client_id) {
                    warn!("FOUND ORPHAN: Exchange has active order {}, but internal state does not.", client_id);
                    if let Some(symbol) = order.get("symbol").and_then(|v| v.as_str()) {
                        info!("    -> Issuing REST DELETE for orphan order {} ({})", client_id, symbol);
                        let _ = self.binance_rest.cancel_futures_order(symbol, client_id).await;
                    }
                }
            }
        }

        // Resolve dangling internal orders via REST query
        let dangling: Vec<(String, String)> = self.internal_orders.iter()
            .filter(|(_, o)| o.status == "NEW" && !exchange_known_client_ids.contains(&o.client_order_id))
            .map(|(cid, o)| (cid.clone(), o.symbol.clone()))
            .collect();

        for (client_id, symbol) in dangling {
            warn!("DANGLING INTERNAL ORDER: {} not on exchange. Querying REST...", client_id);
            match self.binance_rest.get_order_by_client_id(LegVenue::UsdtFutures, &symbol, &client_id).await {
                Ok(body) => {
                    if let Ok(json) = serde_json::from_str::<Value>(&body) {
                        let status = json.get("status").and_then(|v| v.as_str()).unwrap_or("UNKNOWN");
                        match status {
                            "FILLED" => {
                                if let Some(order) = self.internal_orders.get_mut(&client_id) {
                                    order.status = "FILLED".to_string();
                                }
                                info!("Dangling order {} was actually FILLED on exchange.", client_id);
                            }
                            "CANCELED" | "EXPIRED" | "REJECTED" => {
                                if let Some(order) = self.internal_orders.get_mut(&client_id) {
                                    order.status = status.to_string();
                                }
                                info!("Dangling order {} resolved as {} on exchange.", client_id, status);
                            }
                            _ => {
                                if let Some(order) = self.internal_orders.get_mut(&client_id) {
                                    order.status = format!("RECONCILED_{}", status);
                                }
                                warn!("Dangling order {} has status: {}", client_id, status);
                            }
                        }
                    }
                }
                Err(e) => {
                    warn!("Failed to query dangling order {}: {}. Marking stale.", client_id, e);
                    if let Some(order) = self.internal_orders.get_mut(&client_id) {
                        order.status = "STALE".to_string();
                    }
                }
            }
        }

        info!("[Step 5] State matrix synchronized (Dangling resolved, Orphans purged).");
        self.state = SystemState::Trading;
        info!("=== System is TRADING ===");
    }
}
