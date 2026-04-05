use rand::Rng;
use serde_json::Value;
use std::collections::{HashMap, VecDeque};
use std::time::{SystemTime, UNIX_EPOCH, Duration, Instant};
use tokio::sync::mpsc::{Receiver, Sender};
use tokio::time::sleep;
use tracing::{debug, error, info, warn};
use tokio::sync::broadcast;

use crate::binance_rest::{BinanceRest, LegVenue, TradeSide};
use crate::collateral_engine::UnifiedPortfolioMarginCalculator;

#[derive(Debug, PartialEq, Eq, Clone)]
pub enum SystemState {
    Disconnected,
    Reconciling,
    Trading,
}

#[derive(Debug, Clone, Copy, serde::Serialize, serde::Deserialize, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum MarketType {
    Spot,
    Perp,
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
        market: MarketType,
        bids: Vec<[f64; 2]>,
        asks: Vec<[f64; 2]>,
    },
    /// Emitted on every markPriceUpdate from Binance perp streams (~1s cadence).
    /// `next_funding_rate` is the predicted rate for the upcoming settlement —
    /// more actionable than lastFundingRate for entry/exit decisions.
    MarkPrice {
        symbol: String,
        mark_price: f64,
        next_funding_rate: f64,
    },
    VolumeBar {
        symbol: String,
        minute_start_ms: i64,
        notional_usd: f64,
    },
    OrderUpdate {
        client_order_id: String,
        symbol: String,
        status: String,
        filled_qty: f64,
        avg_fill_price: Option<f64>,
        last_fill_price: Option<f64>,
        cumulative_quote_qty: Option<f64>,
        commission: Option<f64>,
        commission_asset: Option<String>,
        realized_pnl: Option<f64>,
        maker: Option<bool>,
        execution_type: Option<String>,
        event_time_ms: Option<i64>,
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
pub struct TrackedLegPosition {
    pub side: String,
    pub entry_price: f64,
    pub quantity: f64,
    pub unrealized_pnl: f64,
    pub last_mark_price: f64,
}

#[allow(dead_code)]
#[derive(Debug, Clone, Default)]
pub struct TrackedPosition {
    pub symbol: String,
    pub spot: Option<TrackedLegPosition>,
    pub perp: Option<TrackedLegPosition>,
}

#[derive(Debug, Clone, Copy)]
struct TopOfBook {
    bid_price: f64,
    ask_price: f64,
}

pub struct OrderManager {
    pub state: SystemState,
    pub internal_orders: HashMap<String, InternalOrder>,
    pub obi_cache: HashMap<String, f64>,
    pub obi_alert_at: HashMap<String, Instant>,
    pub exchange_info: HashMap<String, crate::binance_rest::ExchangeSymbolInfo>,
    pub event_receiver: Receiver<EngineEvent>,
    pub engine_tx: tokio::sync::mpsc::Sender<EngineEvent>,
    pub subscription_tx: Sender<String>,
    pub binance_rest: BinanceRest,
    chase_states: HashMap<String, ChaseState>,  // key: symbol (uppercase)
    pub dash_tx: broadcast::Sender<String>,
    pub is_toxic: bool,
    pub last_brain_ping: Instant,
    pub current_gross_exposure_usd: f64,
    pub max_gross_exposure_usd: f64,
    pub account_equity_usd: f64,
    pub tracked_positions: HashMap<String, TrackedPosition>,
    #[allow(dead_code)]
    pub collateral_calc: UnifiedPortfolioMarginCalculator,
    pub basis_deviation_stop_bps: f64,
    pub maker_fills: u64,
    pub taker_fills: u64,
    pub mid_price_history: VecDeque<f64>,
    spot_mid_cache: HashMap<String, f64>,
    perp_mid_cache: HashMap<String, f64>,
    spot_top_cache: HashMap<String, TopOfBook>,
    perp_top_cache: HashMap<String, TopOfBook>,
    pub trading_mode: String,
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
    quantity: f64,
    spot_client_order_id: String,
    futures_client_order_id: String,
    spot_side: TradeSide,
    futures_side: TradeSide,
    is_exit: bool,
    phase: ChasePhase,
    #[allow(dead_code)]
    start_time: Instant,
    expected_spot_price: f64,
    expected_fut_price: f64,
    spot_fill_price: Option<f64>,
    futures_fill_price: Option<f64>,
}

impl OrderManager {
    pub fn new(
        event_receiver: Receiver<EngineEvent>,
        engine_tx: tokio::sync::mpsc::Sender<EngineEvent>,
        subscription_tx: Sender<String>,
        api_key: String,
        secret_key: String,
        dash_tx: broadcast::Sender<String>,
        trading_mode: String,
    ) -> Self {
        let max_gross_exposure = std::env::var("MAX_GROSS_EXPOSURE_USD")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(50_000.0);

        let account_equity = std::env::var("ACCOUNT_EQUITY_USD")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(10_000.0);

        let basis_deviation_stop_bps = std::env::var("BASIS_DEVIATION_STOP_BPS")
            .ok()
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(30.0);

        info!("OrderManager config: max_gross_exposure=${}, account_equity=${}, basis_stop={:.0}bps, trading_mode={}",
            max_gross_exposure, account_equity, basis_deviation_stop_bps, trading_mode);

        let collateral_calc = UnifiedPortfolioMarginCalculator::new(
            account_equity,
            0.004,  // 0.4% maintenance margin rate
            0.8,    // 80% danger threshold
        );

        Self {
            state: SystemState::Disconnected,
            internal_orders: HashMap::new(),
            obi_cache: HashMap::new(),
            obi_alert_at: HashMap::new(),
            exchange_info: HashMap::new(),
            event_receiver,
            engine_tx,
            subscription_tx,
            binance_rest: BinanceRest::new(api_key, secret_key, trading_mode.clone()),
            chase_states: HashMap::new(),
            dash_tx,
            is_toxic: false,
            last_brain_ping: Instant::now(),
            current_gross_exposure_usd: 0.0,
            max_gross_exposure_usd: max_gross_exposure,
            account_equity_usd: account_equity,
            tracked_positions: HashMap::new(),
            collateral_calc,
            basis_deviation_stop_bps,
            maker_fills: 0,
            taker_fills: 0,
            mid_price_history: VecDeque::with_capacity(64),
            spot_mid_cache: HashMap::new(),
            perp_mid_cache: HashMap::new(),
            spot_top_cache: HashMap::new(),
            perp_top_cache: HashMap::new(),
            trading_mode,
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
                .filter_map(|position| position.spot.as_ref())
                .filter(|leg| leg.side == "LONG")
                .map(|leg| leg.last_mark_price * leg.quantity)
                .sum();
            let total_perp_notional: f64 = self.tracked_positions.values()
                .filter_map(|position| position.perp.as_ref())
                .filter(|leg| leg.side == "SHORT")
                .map(|leg| leg.last_mark_price * leg.quantity)
                .sum();
            let total_upnl: f64 = self.tracked_positions.values()
                .map(|position| {
                    position.spot.as_ref().map(|leg| leg.unrealized_pnl).unwrap_or(0.0)
                        + position.perp.as_ref().map(|leg| leg.unrealized_pnl).unwrap_or(0.0)
                })
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

        // Circuit breaker 4: basis deviation stop
        // Compare current spot vs perp mark prices against entry prices
        for (symbol, position) in &self.tracked_positions {
            if let (Some(spot_pos), Some(perp_pos)) = (
                position.spot.as_ref(),
                position.perp.as_ref(),
            ) {
                if spot_pos.entry_price <= 0.0 || spot_pos.last_mark_price <= 0.0 {
                    continue;
                }
                let entry_basis = (perp_pos.entry_price - spot_pos.entry_price) / spot_pos.entry_price;
                let current_basis = (perp_pos.last_mark_price - spot_pos.last_mark_price) / spot_pos.last_mark_price;
                let deviation_bps = ((current_basis - entry_basis).abs()) * 10_000.0;

                if deviation_bps > self.basis_deviation_stop_bps {
                    warn!("CRITICAL: Basis deviation {:.1}bps exceeds stop {:.0}bps for {}! Emergency flatten.",
                        deviation_bps, self.basis_deviation_stop_bps, symbol);
                    return true;
                }
            }
        }

        false
    }

    pub fn maker_fill_rate(&self) -> f64 {
        let total = self.maker_fills + self.taker_fills;
        if total == 0 { return 0.0; }
        self.maker_fills as f64 / total as f64
    }

    fn update_mid_price(&mut self, mid_price: f64) {
        if self.mid_price_history.len() >= 64 {
            self.mid_price_history.pop_front();
        }
        self.mid_price_history.push_back(mid_price);
    }

    fn recent_volatility_bps(&self) -> f64 {
        if self.mid_price_history.len() < 2 {
            return 0.0;
        }
        let returns: Vec<f64> = self.mid_price_history.iter()
            .zip(self.mid_price_history.iter().skip(1))
            .map(|(prev, curr)| ((curr - prev) / prev) * 10_000.0)
            .collect();
        let n = returns.len() as f64;
        let mean = returns.iter().sum::<f64>() / n;
        let variance = returns.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / n;
        variance.sqrt()
    }

    fn adaptive_legging_timeout_ms(&self) -> u64 {
        let vol = self.recent_volatility_bps();
        let raw = 300.0 - vol * 20.0;
        raw.clamp(50.0, 500.0) as u64
    }

    fn current_time_ms() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as i64)
            .unwrap_or(0)
    }

    fn paper_limit_crossed(side: TradeSide, limit_price: f64, top: TopOfBook) -> bool {
        match side {
            TradeSide::Buy => top.ask_price > 0.0 && top.ask_price <= limit_price,
            TradeSide::Sell => top.bid_price > 0.0 && top.bid_price >= limit_price,
        }
    }

    fn paper_market_fill_price(
        &self,
        symbol: &str,
        market: MarketType,
        side: TradeSide,
        fallback: f64,
    ) -> f64 {
        let sym_upper = symbol.to_uppercase();
        let top = match market {
            MarketType::Spot => self.spot_top_cache.get(&sym_upper).copied(),
            MarketType::Perp => self.perp_top_cache.get(&sym_upper).copied(),
        };
        match (top, side) {
            (Some(top), TradeSide::Buy) if top.ask_price > 0.0 => top.ask_price,
            (Some(top), TradeSide::Sell) if top.bid_price > 0.0 => top.bid_price,
            _ => fallback,
        }
    }

    async fn emit_paper_order_fill(
        &mut self,
        client_order_id: String,
        symbol: String,
        filled_qty: f64,
        fill_price: f64,
        maker: bool,
        execution_type: &str,
    ) {
        if fill_price <= 0.0 {
            warn!(
                "Skipping synthetic paper fill for {} {} because fill_price is invalid ({})",
                symbol, client_order_id, fill_price
            );
            return;
        }

        let _ = self
            .engine_tx
            .send(EngineEvent::Ws(WsEvent::OrderUpdate {
                client_order_id,
                symbol,
                status: "FILLED".to_string(),
                filled_qty,
                avg_fill_price: Some(fill_price),
                last_fill_price: Some(fill_price),
                cumulative_quote_qty: Some(fill_price * filled_qty),
                commission: None,
                commission_asset: None,
                realized_pnl: None,
                maker: Some(maker),
                execution_type: Some(execution_type.to_string()),
                event_time_ms: Some(Self::current_time_ms()),
            }))
            .await;
    }

    async fn maybe_fill_paper_resting_leg(&mut self, symbol: &str, market: MarketType) {
        if self.trading_mode != "paper" {
            return;
        }

        let sym_upper = symbol.to_uppercase();
        let Some(chase) = self.chase_states.get(&sym_upper).cloned() else {
            return;
        };
        if !matches!(
            chase.phase,
            ChasePhase::DualMakerPlaced | ChasePhase::LegFilledWaiting(_)
        ) {
            return;
        }

        let top = match market {
            MarketType::Spot => self.spot_top_cache.get(&sym_upper).copied(),
            MarketType::Perp => self.perp_top_cache.get(&sym_upper).copied(),
        };
        let Some(top) = top else {
            return;
        };

        let (client_order_id, side, fallback_price) = match market {
            MarketType::Spot => (
                chase.spot_client_order_id.clone(),
                chase.spot_side,
                chase.expected_spot_price,
            ),
            MarketType::Perp => (
                chase.futures_client_order_id.clone(),
                chase.futures_side,
                chase.expected_fut_price,
            ),
        };

        let maybe_fill = {
            let Some(order) = self.internal_orders.get_mut(&client_order_id) else {
                return;
            };
            if order.status != "NEW" {
                return;
            }

            let limit_price = order.limit_price.unwrap_or(fallback_price);
            if limit_price <= 0.0 || !Self::paper_limit_crossed(side, limit_price, top) {
                return;
            }

            order.status = "FILLED_PENDING".to_string();
            Some((client_order_id.clone(), chase.symbol.clone(), chase.quantity, limit_price))
        };

        let Some((client_order_id, symbol, quantity, fill_price)) = maybe_fill else {
            return;
        };

        self.emit_paper_order_fill(
            client_order_id,
            symbol,
            quantity,
            fill_price,
            true,
            "PAPER_RESTING_CROSS_FILL",
        )
        .await;
    }

    fn trade_side_label(side: TradeSide) -> String {
        match side {
            TradeSide::Buy => "LONG".to_string(),
            TradeSide::Sell => "SHORT".to_string(),
        }
    }

    fn precision_for_increment(increment: f64) -> usize {
        let rendered = format!("{:.16}", increment.abs());
        rendered
            .trim_end_matches('0')
            .split('.')
            .nth(1)
            .map(|fractional| fractional.len())
            .unwrap_or(0)
    }

    fn round_down_to_step(value: f64, step: f64) -> f64 {
        if value <= 0.0 || step <= 0.0 {
            return value.max(0.0);
        }

        let rounded = (value / step).floor() * step;
        let precision = Self::precision_for_increment(step);
        let scale = 10f64.powi(precision as i32);
        (rounded * scale).round() / scale
    }

    fn quantize_price(value: f64, tick_size: f64, side: TradeSide) -> f64 {
        if value <= 0.0 || tick_size <= 0.0 {
            return value.max(0.0);
        }

        let scaled = value / tick_size;
        let rounded = match side {
            TradeSide::Buy => scaled.floor(),
            TradeSide::Sell => scaled.ceil(),
        } * tick_size;
        let precision = Self::precision_for_increment(tick_size);
        let scale = 10f64.powi(precision as i32);
        (rounded * scale).round() / scale
    }

    fn format_with_increment(value: f64, increment: f64) -> String {
        let precision = Self::precision_for_increment(increment);
        format!("{:.*}", precision, value)
    }

    fn symbol_info(&self, symbol: &str) -> crate::binance_rest::ExchangeSymbolInfo {
        self.exchange_info
            .get(symbol)
            .cloned()
            .unwrap_or(crate::binance_rest::ExchangeSymbolInfo {
                symbol: symbol.to_string(),
                spot_tick_size: 0.1,
                spot_step_size: 0.1,
                futures_tick_size: 0.1,
                futures_step_size: 0.1,
            })
    }

    fn normalize_common_quantity(&self, symbol: &str, requested_quantity: f64) -> Option<f64> {
        let info = self.symbol_info(symbol);
        let common_step = info.spot_step_size.max(info.futures_step_size);
        let normalized = Self::round_down_to_step(requested_quantity, common_step.max(0.00000001));
        if normalized > 0.0 {
            Some(normalized)
        } else {
            None
        }
    }

    fn format_quantity_for_market(&self, symbol: &str, market: MarketType, quantity: f64) -> String {
        let info = self.symbol_info(symbol);
        let step_size = match market {
            MarketType::Spot => info.spot_step_size,
            MarketType::Perp => info.futures_step_size,
        };
        let normalized = Self::round_down_to_step(quantity, step_size.max(0.00000001));
        Self::format_with_increment(normalized, step_size.max(0.00000001))
    }

    fn refresh_leg_pnl(leg: &mut TrackedLegPosition) {
        leg.unrealized_pnl = match leg.side.as_str() {
            "LONG" => (leg.last_mark_price - leg.entry_price) * leg.quantity,
            "SHORT" => (leg.entry_price - leg.last_mark_price) * leg.quantity,
            _ => 0.0,
        };
    }

    fn recompute_gross_exposure(&mut self) {
        self.current_gross_exposure_usd = self
            .tracked_positions
            .values()
            .map(|position| {
                let spot_notional = position
                    .spot
                    .as_ref()
                    .map(|leg| leg.last_mark_price * leg.quantity)
                    .unwrap_or(0.0);
                let perp_notional = position
                    .perp
                    .as_ref()
                    .map(|leg| leg.last_mark_price * leg.quantity)
                    .unwrap_or(0.0);
                spot_notional + perp_notional
            })
            .sum();
    }

    fn emit_position_snapshot(&self, symbol: &str) {
        let Some(position) = self.tracked_positions.get(symbol) else {
            return;
        };

        let unrealized_pnl = position
            .spot
            .as_ref()
            .map(|leg| leg.unrealized_pnl)
            .unwrap_or(0.0)
            + position
                .perp
                .as_ref()
                .map(|leg| leg.unrealized_pnl)
                .unwrap_or(0.0);
        let basis_bps = match (position.spot.as_ref(), position.perp.as_ref()) {
            (Some(spot), Some(perp)) if spot.last_mark_price > 0.0 => {
                Some(((perp.last_mark_price - spot.last_mark_price) / spot.last_mark_price) * 10_000.0)
            }
            _ => None,
        };

        let pnl_event = serde_json::json!({
            "event": "PositionPnL",
            "symbol": symbol,
            "unrealized_pnl": unrealized_pnl,
            "spot_mark_price": position.spot.as_ref().map(|leg| leg.last_mark_price),
            "perp_mark_price": position.perp.as_ref().map(|leg| leg.last_mark_price),
            "basis_bps": basis_bps,
        });
        let _ = self.dash_tx.send(pnl_event.to_string());
    }

    fn apply_mark_price(&mut self, symbol: &str, market: MarketType, mark_price: f64) {
        let sym_upper = symbol.to_uppercase();
        match market {
            MarketType::Spot => {
                self.spot_mid_cache.insert(sym_upper.clone(), mark_price);
            }
            MarketType::Perp => {
                self.perp_mid_cache.insert(sym_upper.clone(), mark_price);
            }
        }

        if let Some(position) = self.tracked_positions.get_mut(&sym_upper) {
            match market {
                MarketType::Spot => {
                    if let Some(leg) = position.spot.as_mut() {
                        leg.last_mark_price = mark_price;
                        Self::refresh_leg_pnl(leg);
                    }
                }
                MarketType::Perp => {
                    if let Some(leg) = position.perp.as_mut() {
                        leg.last_mark_price = mark_price;
                        Self::refresh_leg_pnl(leg);
                    }
                }
            }
        }

        self.recompute_gross_exposure();
        self.emit_position_snapshot(&sym_upper);
    }

    fn apply_fill_to_position(
        &mut self,
        symbol: &str,
        market: MarketType,
        side: TradeSide,
        filled_qty: f64,
        fill_price: f64,
        is_exit: bool,
    ) {
        if filled_qty <= 0.0 {
            return;
        }

        let sym_upper = symbol.to_uppercase();
        let last_mark_price = match market {
            MarketType::Spot => self.spot_mid_cache.get(&sym_upper).copied().unwrap_or(fill_price),
            MarketType::Perp => self.perp_mid_cache.get(&sym_upper).copied().unwrap_or(fill_price),
        };
        let side_label = Self::trade_side_label(side);
        let remove_symbol = {
            let position = self
                .tracked_positions
                .entry(sym_upper.clone())
                .or_insert_with(|| TrackedPosition {
                    symbol: sym_upper.clone(),
                    ..TrackedPosition::default()
                });
            let leg_slot = match market {
                MarketType::Spot => &mut position.spot,
                MarketType::Perp => &mut position.perp,
            };

            if is_exit {
                if let Some(existing) = leg_slot.as_mut() {
                    if filled_qty >= existing.quantity - 0.00000001 {
                        *leg_slot = None;
                    } else {
                        existing.quantity -= filled_qty;
                        existing.last_mark_price = last_mark_price;
                        Self::refresh_leg_pnl(existing);
                    }
                } else {
                    warn!(
                        "Received exit fill for {} {:?} with no tracked leg to reduce",
                        sym_upper, market
                    );
                }
            } else if let Some(existing) = leg_slot.as_mut() {
                let new_total_qty = existing.quantity + filled_qty;
                if new_total_qty > 0.0 {
                    existing.entry_price = ((existing.entry_price * existing.quantity)
                        + (fill_price * filled_qty))
                        / new_total_qty;
                    existing.quantity = new_total_qty;
                } else {
                    existing.entry_price = fill_price;
                    existing.quantity = filled_qty;
                }
                existing.side = side_label;
                existing.last_mark_price = last_mark_price;
                Self::refresh_leg_pnl(existing);
            } else {
                *leg_slot = Some(TrackedLegPosition {
                    side: side_label,
                    entry_price: fill_price,
                    quantity: filled_qty,
                    unrealized_pnl: 0.0,
                    last_mark_price,
                });
                if let Some(new_leg) = leg_slot.as_mut() {
                    Self::refresh_leg_pnl(new_leg);
                }
            }

            position.spot.is_none() && position.perp.is_none()
        };

        if remove_symbol {
            self.tracked_positions.remove(&sym_upper);
        }

        self.recompute_gross_exposure();
        self.emit_position_snapshot(&sym_upper);
    }

    fn tracked_exit_quantity(&self, symbol: &str) -> Option<f64> {
        let sym_upper = symbol.to_uppercase();
        let position = self.tracked_positions.get(&sym_upper)?;
        let spot_qty = position.spot.as_ref().map(|leg| leg.quantity).unwrap_or(0.0);
        let perp_qty = position.perp.as_ref().map(|leg| leg.quantity).unwrap_or(0.0);
        let resolved_qty = spot_qty.max(perp_qty);
        if resolved_qty > 0.0 {
            Some(resolved_qty)
        } else {
            None
        }
    }

    fn emit_maker_fill_rate(&self) {
        let fill_event = serde_json::json!({
            "event": "MakerFillRate",
            "maker_fills": self.maker_fills,
            "taker_fills": self.taker_fills,
            "rate": self.maker_fill_rate(),
        });
        let _ = self.dash_tx.send(fill_event.to_string());
    }

    fn emit_cycle_order_update(
        &self,
        chase: &ChaseState,
        status: &str,
        client_order_id: &str,
        filled_qty: f64,
        maker: bool,
        execution_type: &str,
    ) {
        let cycle_event = serde_json::json!({
            "event": "OrderUpdate",
            "symbol": &chase.symbol,
            "status": status,
            "filled_qty": filled_qty,
            "client_order_id": client_order_id,
            "avg_fill_price": chase.spot_fill_price.unwrap_or(chase.expected_spot_price),
            "last_fill_price": chase.spot_fill_price.unwrap_or(chase.expected_spot_price),
            "cumulative_quote_qty": serde_json::Value::Null,
            "commission": serde_json::Value::Null,
            "commission_asset": serde_json::Value::Null,
            "realized_pnl": serde_json::Value::Null,
            "maker": maker,
            "execution_type": execution_type,
            "event_time_ms": SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_millis() as i64)
                .unwrap_or(0),
            "spot_fill_price": chase.spot_fill_price.unwrap_or(chase.expected_spot_price),
            "perp_fill_price": chase.futures_fill_price.unwrap_or(chase.expected_fut_price),
        });
        if let Ok(msg) = serde_json::to_string(&cycle_event) {
            let _ = self.dash_tx.send(msg);
        }
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
        let symbol = self.chase_states.iter()
            .find(|(_, s)| s.spot_client_order_id == trigger_client_id
                       || s.futures_client_order_id == trigger_client_id)
            .map(|(k, _)| k.clone());
        let Some(symbol) = symbol else { return };
        let Some(mut chase) = self.chase_states.get(&symbol).cloned() else { return };

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
        if self.trading_mode == "paper" {
            if let Some(order) = self.internal_orders.get_mut(&unfilled_cid) {
                order.status = "CANCELED".to_string();
            }
        }

        let new_taker_cid = Self::generate_client_order_id("legging");
        info!("Placing legging defense MARKET order for {:?} cid={}", unfilled_leg, new_taker_cid);

        let market_res = match unfilled_leg {
            Leg::Spot => {
                let quantity = self.format_quantity_for_market(&unfilled_sym, MarketType::Spot, chase.quantity);
                self.binance_rest
                    .place_spot_market_order(&unfilled_sym, unfilled_side, &quantity, &new_taker_cid)
                    .await
            }
            Leg::Futures => {
                let quantity = self.format_quantity_for_market(&unfilled_sym, MarketType::Perp, chase.quantity);
                self.binance_rest
                    .place_futures_market_order(&unfilled_sym, unfilled_side, &quantity, &new_taker_cid)
                    .await
            }
        };

        if let Ok(body) = market_res {
            info!("Taker hedge submission response: {}", body);
            chase.phase = ChasePhase::LeggingDefenseTakerPlaced;
            self.chase_states.insert(symbol.clone(), chase.clone());
            if self.trading_mode == "paper" {
                let market = match unfilled_leg {
                    Leg::Spot => MarketType::Spot,
                    Leg::Futures => MarketType::Perp,
                };
                let fallback_fill_price = match unfilled_leg {
                    Leg::Spot => chase.expected_spot_price,
                    Leg::Futures => chase.expected_fut_price,
                };
                let fill_price =
                    self.paper_market_fill_price(&unfilled_sym, market, unfilled_side, fallback_fill_price);
                self.internal_orders.insert(
                    new_taker_cid.clone(),
                    InternalOrder {
                        client_order_id: new_taker_cid.clone(),
                        symbol: unfilled_sym.clone(),
                        status: "FILLED_PENDING".to_string(),
                        limit_price: Some(fill_price),
                    },
                );
                self.emit_paper_order_fill(
                    new_taker_cid,
                    unfilled_sym,
                    chase.quantity,
                    fill_price,
                    false,
                    "PAPER_TAKER_FILL",
                )
                .await;
            }
        } else {
            error!("Failed to submit legging defense taker order: {:?}", market_res.err());
            self.chase_states.remove(&symbol);
        }
    }

    async fn handle_alpha_instruction(&mut self, instruction: crate::ipc::AlphaInstruction) {
        if instruction.intent != "HEARTBEAT" {
            info!("Handling Alpha Instruction: {:?}", instruction);
        } else {
            debug!("Handling Alpha Instruction: {:?}", instruction);
        }
        self.last_brain_ping = Instant::now();

        if instruction.intent == "HEARTBEAT" {
            let ack_event = serde_json::json!({
                "event": "HeartbeatAck",
                "heartbeat_id": instruction.heartbeat_id,
                "status": "ok",
                "ts_ms": SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_millis())
                    .unwrap_or(0),
            });
            let _ = self.dash_tx.send(ack_event.to_string());
            return;
        }

        if self.state != SystemState::Trading {
            warn!("System not currently trading; ignoring alpha instruction.");
            return;
        }

        let is_exit_intent = matches!(instruction.intent.as_str(), "EXIT_LONG" | "EXIT_SHORT");
        if !is_exit_intent && self.check_circuit_breakers().await {
            return;
        }

        let sym_upper = match instruction.symbol.as_deref() {
            Some(s) => s.to_uppercase(),
            None => {
                warn!("Received non-heartbeat instruction with no symbol; ignoring.");
                return;
            }
        };
        if let Err(err) = self.subscription_tx.send(sym_upper.clone()).await {
            warn!(
                "Could not request dynamic market-data subscription for {}: {}",
                sym_upper, err
            );
        }
        if self.chase_states.contains_key(&sym_upper) {
            if !is_exit_intent {
                warn!("Currently executing a Chase for {}, skipping new alpha instruction.", sym_upper);
                let rejected_event = serde_json::json!({
                    "event": "OrderRejected",
                    "symbol": sym_upper,
                    "intent": instruction.intent,
                    "intent_id": instruction.intent_id,
                    "reason": "chase_active",
                });
                let _ = self.dash_tx.send(rejected_event.to_string());
                return;
            }
            // Exit preempts active chase
            if let Some(existing) = self.chase_states.remove(&sym_upper) {
                warn!("EXIT received for {} while chase active (phase: {:?}) — preempting chase",
                    sym_upper, existing.phase);
            }
        }

        let spot_client_order_id = Self::generate_client_order_id("spot");
        let futures_client_order_id = Self::generate_client_order_id("fut");

        let is_buy = instruction.intent == "ENTER_LONG" || instruction.intent == "EXIT_SHORT";
        let is_exit = instruction.intent == "EXIT_LONG" || instruction.intent == "EXIT_SHORT";
        let scaled_quantity = instruction.quantity * instruction.exposure_scale;
        let resolved_quantity = if is_exit {
            let tracked_quantity = self.tracked_exit_quantity(&sym_upper);
            let requested_quantity = if scaled_quantity > 0.0 {
                scaled_quantity
            } else {
                tracked_quantity.unwrap_or(0.0)
            };
            tracked_quantity
                .map(|tracked| requested_quantity.min(tracked))
                .unwrap_or(requested_quantity)
        } else {
            scaled_quantity
        };
        let Some(normalized_quantity) = self.normalize_common_quantity(&sym_upper, resolved_quantity) else {
            warn!(
                "Instruction {} for {} resolved to invalid quantity {:.8} after exchange normalization",
                instruction.intent, sym_upper, resolved_quantity
            );
            return;
        };

        self.chase_states.insert(sym_upper.clone(), ChaseState {
            symbol: sym_upper.clone(),
            quantity: normalized_quantity,
            spot_client_order_id,
            futures_client_order_id,
            spot_side: if is_buy { TradeSide::Buy } else { TradeSide::Sell },
            futures_side: if is_buy { TradeSide::Sell } else { TradeSide::Buy },
            is_exit,
            phase: ChasePhase::Idle,
            start_time: Instant::now(),
            expected_spot_price: 0.0,
            expected_fut_price: 0.0,
            spot_fill_price: None,
            futures_fill_price: None,
        });

        info!("Dynamic chase state initialized from AlphaInstruction for {}.", sym_upper);
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
                    // Do not reset state to Disconnected if ONE out of many WS streams drops.
                    // This causes the entire engine to go blind to all other streams.
                    // Instead, just clear chase state or rely on reconnection logic.
                    // self.state = SystemState::Disconnected;
                    self.chase_states.clear();
                }
                WsEvent::BookTicker {
                    symbol,
                    bid_price,
                    ask_price,
                } => {
                    if self.state != SystemState::Trading {
                        return;
                    }

                    // Update mid-price history for adaptive volatility
                    let mid_price = (bid_price + ask_price) / 2.0;
                    let sym_upper = symbol.to_uppercase();
                    self.perp_top_cache.insert(
                        sym_upper.clone(),
                        TopOfBook {
                            bid_price,
                            ask_price,
                        },
                    );
                    self.update_mid_price(mid_price);
                    self.apply_mark_price(&sym_upper, MarketType::Perp, mid_price);

                    // Spread toxicity protection
                    let spread_bps = (ask_price - bid_price) / ((ask_price + bid_price) / 2.0) * 10000.0;
                    if spread_bps > 50.0 {
                        if !self.is_toxic {
                            warn!("Spread toxicity detected for {}! ({:.1} bps). Pausing maker operations.", sym_upper, spread_bps);
                            self.is_toxic = true;
                        }
                    } else if self.is_toxic {
                        info!("Toxicity resolved for {}. Resuming operations.", sym_upper);
                        self.is_toxic = false;
                    }

                    if !self.is_toxic {
                        self.try_place_dual_maker(sym_upper).await;
                    }
                    self.maybe_fill_paper_resting_leg(&symbol, MarketType::Perp).await;
                }
                WsEvent::L2Depth { symbol, market, bids, asks } => {
                    if self.state != SystemState::Trading {
                        return;
                    }

                    let total_bid_vol: f64 = bids.iter().map(|item| item[1]).sum();
                    let total_ask_vol: f64 = asks.iter().map(|item| item[1]).sum();

                    let obi = if total_bid_vol + total_ask_vol > 0.0 {
                        (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol)
                    } else {
                        0.0
                    };

                    let sym_upper = symbol.to_uppercase();
                    if let (Some(best_bid), Some(best_ask)) = (bids.first(), asks.first()) {
                        let mid_price = (best_bid[0] + best_ask[0]) / 2.0;
                        let top = TopOfBook {
                            bid_price: best_bid[0],
                            ask_price: best_ask[0],
                        };
                        match market {
                            MarketType::Spot => {
                                self.spot_top_cache.insert(sym_upper.clone(), top);
                            }
                            MarketType::Perp => {
                                self.perp_top_cache.insert(sym_upper.clone(), top);
                            }
                        }
                        self.apply_mark_price(&sym_upper, market, mid_price);
                    }

                    if market == MarketType::Perp {
                        let previous_obi = self.obi_cache.get(&sym_upper).copied().unwrap_or(0.0);
                        self.obi_cache.insert(sym_upper.clone(), obi);
                        let now = Instant::now();
                        let should_log_obi = obi.abs() > 0.4
                            && (previous_obi.abs() <= 0.4
                                || self
                                    .obi_alert_at
                                    .get(&sym_upper)
                                    .map(|last| now.duration_since(*last) >= Duration::from_secs(30))
                                    .unwrap_or(true));

                        if should_log_obi {
                            debug!("High OBI detected for {}: {:.2}. Skewing resting limits.", sym_upper, obi);
                            self.obi_alert_at.insert(sym_upper.clone(), now);
                        } else if obi.abs() <= 0.4 {
                            self.obi_alert_at.remove(&sym_upper);
                        }
                    }

                    // Broadcast depth data to Python (for DepthTracker)
                    let dash = self.dash_tx.clone();
                    let sym = symbol.clone();
                    // Force lowercase market string serialization so Python's rust_data_subscriber matches "spot"
                    let mkt_str = match market {
                        MarketType::Spot => "spot",
                        MarketType::Perp => "perp",
                    };
                    let bids_json: Vec<Vec<serde_json::Value>> = bids.iter().map(|item| vec![serde_json::json!(item[0]), serde_json::json!(item[1])]).collect();
                    let asks_json: Vec<Vec<serde_json::Value>> = asks.iter().map(|item| vec![serde_json::json!(item[0]), serde_json::json!(item[1])]).collect();
                    tokio::spawn(async move {
                        let depth_event = serde_json::json!({
                            "event": "L2Depth",
                            "symbol": sym,
                            "market": mkt_str,
                            "bids": bids_json,
                            "asks": asks_json,
                        });
                        if let Ok(msg) = serde_json::to_string(&depth_event) {
                            let _ = dash.send(msg);
                        }
                    });

                    if !self.is_toxic {
                        self.try_place_dual_maker(sym_upper).await;
                    }
                    self.maybe_fill_paper_resting_leg(&symbol, market).await;
                }
                WsEvent::OrderUpdate {
                    client_order_id,
                    symbol,
                    status,
                    filled_qty,
                    avg_fill_price,
                    last_fill_price,
                    cumulative_quote_qty: _cumulative_quote_qty,
                    commission: _commission,
                    commission_asset: _commission_asset,
                    realized_pnl: _realized_pnl,
                    maker,
                    execution_type,
                    event_time_ms: _event_time_ms,
                } => {
                    info!(
                        "Order Update: {} {} {} filled={} avg={:?} last={:?} maker={:?} exec={:?}",
                        symbol,
                        client_order_id,
                        status,
                        filled_qty,
                        avg_fill_price,
                        last_fill_price,
                        maker,
                        execution_type
                    );
                    let observed_fill_price = avg_fill_price.or(last_fill_price);
                    let previous_status = self
                        .internal_orders
                        .get(&client_order_id)
                        .map(|order| order.status.clone());
                    if status == "FILLED" && previous_status.as_deref() == Some("FILLED") {
                        warn!(
                            "Duplicate FILLED update ignored for {} {}",
                            symbol, client_order_id
                        );
                        return;
                    }

                    // Slippage monitoring on fills
                    if status == "FILLED" {
                        if let Some(internal) = self.internal_orders.get(&client_order_id) {
                            if let Some(expected_price) = internal.limit_price {
                                if let Some(actual_fill_price) = observed_fill_price {
                                    let slippage_bps =
                                        ((actual_fill_price - expected_price) / expected_price) * 10_000.0;
                                    info!(
                                        "Fill monitoring: {} expected_price={:.2} actual_fill={:.2} slippage={:.2}bps",
                                        client_order_id, expected_price, actual_fill_price, slippage_bps
                                    );
                                } else {
                                    info!("Fill monitoring: {} expected_price={:.2}", client_order_id, expected_price);
                                }
                            }
                        }
                    }

                    let sym_clone = symbol.to_uppercase();
                    let chase_snapshot = self.chase_states.get(&sym_clone).cloned();
                    if status == "FILLED" {
                        if let Some(chase) = chase_snapshot.as_ref() {
                            if client_order_id == chase.spot_client_order_id {
                                let spot_fill_price =
                                    observed_fill_price.unwrap_or(chase.expected_spot_price);
                                self.apply_fill_to_position(
                                    &sym_clone,
                                    MarketType::Spot,
                                    chase.spot_side,
                                    filled_qty,
                                    spot_fill_price,
                                    chase.is_exit,
                                );
                            } else if client_order_id == chase.futures_client_order_id {
                                let fut_fill_price =
                                    observed_fill_price.unwrap_or(chase.expected_fut_price);
                                self.apply_fill_to_position(
                                    &sym_clone,
                                    MarketType::Perp,
                                    chase.futures_side,
                                    filled_qty,
                                    fut_fill_price,
                                    chase.is_exit,
                                );
                            }
                        }
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
                    if let Some(mut chase) = chase_snapshot {
                        let matched_leg = if client_order_id == chase.spot_client_order_id {
                            Some(Leg::Spot)
                        } else if client_order_id == chase.futures_client_order_id {
                            Some(Leg::Futures)
                        } else {
                            None
                        };
                        let Some(matched_leg) = matched_leg else {
                            return;
                        };

                        if status == "FILLED" {
                            let mut trigger_timeout = false;
                            let spot_fill_price = observed_fill_price.unwrap_or(chase.expected_spot_price);
                            let fut_fill_price = observed_fill_price.unwrap_or(chase.expected_fut_price);
                            if client_order_id == chase.spot_client_order_id {
                                chase.spot_fill_price = Some(spot_fill_price);
                            } else if client_order_id == chase.futures_client_order_id {
                                chase.futures_fill_price = Some(fut_fill_price);
                            }

                            match chase.phase {
                                ChasePhase::Idle => {
                                    self.maker_fills += 1;
                                    info!(
                                        "Leg '{:?}' FILLED before maker phase transition completed. Waiting for the other leg...",
                                        matched_leg
                                    );
                                    chase.phase = ChasePhase::LegFilledWaiting(matched_leg);
                                    self.chase_states.insert(sym_clone.clone(), chase.clone());
                                    trigger_timeout = true;
                                }
                                ChasePhase::DualMakerPlaced => {
                                    let first_filled = matched_leg;
                                    self.maker_fills += 1;
                                    info!("Leg '{:?}' FILLED (maker). Waiting for the other leg...", first_filled);
                                    chase.phase = ChasePhase::LegFilledWaiting(first_filled);
                                    self.chase_states.insert(sym_clone.clone(), chase.clone());
                                    trigger_timeout = true;
                                },
                                ChasePhase::LegFilledWaiting(first_filled) => {
                                    if first_filled != matched_leg {
                                        self.maker_fills += 1;
                                        info!("Chase cycle completed (both legs filled as maker). Rate: {:.1}%",
                                            self.maker_fill_rate() * 100.0);
                                        self.emit_maker_fill_rate();
                                        chase.phase = ChasePhase::Completed;
                                        self.emit_cycle_order_update(
                                            &chase,
                                            "FILLED",
                                            &chase.spot_client_order_id,
                                            chase.quantity,
                                            true,
                                            "FILLED_CYCLE",
                                        );
                                        self.chase_states.remove(&sym_clone);
                                    }
                                },
                                ChasePhase::LeggingDefenseTakerPlaced => {
                                    self.taker_fills += 1;
                                    info!("Chase cycle completed (legging defense taker). Maker rate: {:.1}%",
                                        self.maker_fill_rate() * 100.0);
                                    self.emit_maker_fill_rate();
                                    chase.phase = ChasePhase::Completed;
                                    self.emit_cycle_order_update(
                                        &chase,
                                        "FILLED",
                                        &chase.spot_client_order_id,
                                        chase.quantity,
                                        false,
                                        "FILLED_CYCLE",
                                    );
                                    self.chase_states.remove(&sym_clone);
                                },
                                _ => {}
                            }

                            if trigger_timeout {
                                let tx = self.engine_tx.clone();
                                let cid = client_order_id.clone();
                                let timeout_ms = self.adaptive_legging_timeout_ms();
                                info!("Adaptive legging timeout: {}ms (vol={:.1}bps)", timeout_ms, self.recent_volatility_bps());
                                tokio::spawn(async move {
                                    sleep(Duration::from_millis(timeout_ms)).await;
                                    let _ = tx.send(EngineEvent::LeggingTimeout(cid)).await;
                                });
                            }
                        } else if matches!(
                            status.as_str(),
                            "REJECTED" | "EXPIRED" | "CANCELED" | "CANCELLED"
                        ) {
                            match chase.phase {
                                ChasePhase::LegFilledWaiting(first_filled) => {
                                    let expected_failed_leg = match first_filled {
                                        Leg::Spot => Leg::Futures,
                                        Leg::Futures => Leg::Spot,
                                    };
                                    if matched_leg == expected_failed_leg {
                                        let filled_leg_client_id = match first_filled {
                                            Leg::Spot => chase.spot_client_order_id.clone(),
                                            Leg::Futures => chase.futures_client_order_id.clone(),
                                        };
                                        warn!(
                                            "Leg {:?} failed for {} while {:?} was already filled. Triggering taker hedge.",
                                            matched_leg, sym_clone, first_filled
                                        );
                                        self.handle_legging_timeout(filled_leg_client_id).await;
                                    }
                                }
                                ChasePhase::Idle | ChasePhase::DualMakerPlaced => {
                                    let other_leg = match matched_leg {
                                        Leg::Spot => Leg::Futures,
                                        Leg::Futures => Leg::Spot,
                                    };
                                    let cancel_result = match other_leg {
                                        Leg::Spot => self
                                            .binance_rest
                                            .cancel_order(&chase.symbol, &chase.spot_client_order_id)
                                            .await,
                                        Leg::Futures => self
                                            .binance_rest
                                            .cancel_futures_order(&chase.symbol, &chase.futures_client_order_id)
                                            .await,
                                    };
                                    if let Err(err) = cancel_result {
                                        error!(
                                            "Fail-closed cancel failed for {} after {} on {}: {}",
                                            chase.symbol, status, client_order_id, err
                                        );
                                    }
                                    self.emit_cycle_order_update(
                                        &chase,
                                        "REJECTED",
                                        &client_order_id,
                                        0.0,
                                        false,
                                        "DUAL_SUBMISSION_FAILED",
                                    );
                                    self.chase_states.remove(&sym_clone);
                                }
                                ChasePhase::LeggingDefenseTakerPlaced => {
                                    error!(
                                        "Legging defense failed for {} on client id {}",
                                        chase.symbol, client_order_id
                                    );
                                    self.emit_cycle_order_update(
                                        &chase,
                                        "REJECTED",
                                        &client_order_id,
                                        0.0,
                                        false,
                                        "LEGGING_DEFENSE_FAILED",
                                    );
                                    self.chase_states.remove(&sym_clone);
                                }
                                ChasePhase::Completed => {}
                            }
                        }
                    }
                }
                WsEvent::MarkPrice { symbol, mark_price, next_funding_rate } => {
                    self.apply_mark_price(&symbol, MarketType::Perp, mark_price);
                    let dash = self.dash_tx.clone();
                    let sym = symbol.clone();
                    let mp = mark_price;
                    let nfr = next_funding_rate;
                    tokio::spawn(async move {
                        let mark_event = serde_json::json!({
                            "event": "MarkPrice",
                            "symbol": sym,
                            "mark_price": mp,
                            "next_funding_rate": nfr,
                        });
                        if let Ok(msg) = serde_json::to_string(&mark_event) {
                            let _ = dash.send(msg);
                        }
                    });
                }
                WsEvent::VolumeBar { symbol, minute_start_ms, notional_usd } => {
                    let dash = self.dash_tx.clone();
                    let sym = symbol.clone();
                    let ms = minute_start_ms;
                    let notional = notional_usd;
                    tokio::spawn(async move {
                        let vol_event = serde_json::json!({
                            "event": "VolumeBar",
                            "symbol": sym,
                            "minute_start_ms": ms,
                            "notional_usd": notional,
                        });
                        if let Ok(msg) = serde_json::to_string(&vol_event) {
                            let _ = dash.send(msg);
                        }
                    });
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

    async fn try_place_dual_maker(&mut self, symbol: String) {
        let sym_upper = symbol.to_uppercase();
        let Some(chase_snapshot) = self.chase_states.get(&sym_upper).cloned() else {
            return;
        };

        if chase_snapshot.phase != ChasePhase::Idle {
            return;
        }

        let current_obi = self.obi_cache.get(&sym_upper).copied().unwrap_or(0.0);
        let symbol_info = self.symbol_info(&chase_snapshot.symbol);
        let spot_tick_size = symbol_info.spot_tick_size.max(0.00000001);
        let futures_tick_size = symbol_info.futures_tick_size.max(0.00000001);
        let Some(spot_top) = self.spot_top_cache.get(&sym_upper).copied() else {
            return;
        };
        let Some(perp_top) = self.perp_top_cache.get(&sym_upper).copied() else {
            return;
        };

        let mut spot_target = match chase_snapshot.spot_side {
            TradeSide::Buy => spot_top.bid_price,
            TradeSide::Sell => spot_top.ask_price,
        };

        let mut fut_target = match chase_snapshot.futures_side {
            TradeSide::Buy => perp_top.bid_price,
            TradeSide::Sell => perp_top.ask_price,
        };

        // OBI-based price skewing
        if current_obi > 0.3 {
            spot_target += spot_tick_size;
            fut_target += futures_tick_size;
        } else if current_obi < -0.3 {
            spot_target -= spot_tick_size;
            fut_target -= futures_tick_size;
        }

        let spot_target = Self::quantize_price(spot_target, spot_tick_size, chase_snapshot.spot_side);
        let fut_target = Self::quantize_price(fut_target, futures_tick_size, chase_snapshot.futures_side);
        if spot_target <= 0.0 || fut_target <= 0.0 {
            error!(
                "Normalized maker prices are invalid for {}: spot_target={} fut_target={}",
                chase_snapshot.symbol, spot_target, fut_target
            );
            self.emit_cycle_order_update(
                &chase_snapshot,
                "REJECTED",
                &chase_snapshot.spot_client_order_id,
                0.0,
                false,
                "INVALID_PRICE_NORMALIZATION",
            );
            self.chase_states.remove(&sym_upper);
            return;
        }

        let spot_price_str = Self::format_with_increment(spot_target, spot_tick_size);
        let fut_price_str = Self::format_with_increment(fut_target, futures_tick_size);
        let spot_qty_str =
            self.format_quantity_for_market(&chase_snapshot.symbol, MarketType::Spot, chase_snapshot.quantity);
        let fut_qty_str =
            self.format_quantity_for_market(&chase_snapshot.symbol, MarketType::Perp, chase_snapshot.quantity);

        // Store expected prices for slippage monitoring
        if let Some(c) = self.chase_states.get_mut(&sym_upper) {
            c.expected_spot_price = spot_target;
            c.expected_fut_price = fut_target;
        }

        info!("Placing DUAL maker LIMIT orders. OBI: {:.2}", current_obi);

        self.internal_orders.insert(chase_snapshot.spot_client_order_id.clone(), InternalOrder {
            client_order_id: chase_snapshot.spot_client_order_id.clone(),
            symbol: chase_snapshot.symbol.clone(),
            status: "PENDING_SUBMIT".to_string(),
            limit_price: Some(spot_target),
        });
        self.internal_orders.insert(chase_snapshot.futures_client_order_id.clone(), InternalOrder {
            client_order_id: chase_snapshot.futures_client_order_id.clone(),
            symbol: chase_snapshot.symbol.clone(),
            status: "PENDING_SUBMIT".to_string(),
            limit_price: Some(fut_target),
        });

        if self.trading_mode == "paper" {
            if let Some(c) = self.chase_states.get_mut(&sym_upper) {
                c.phase = ChasePhase::DualMakerPlaced;
            }
            if let Some(order) = self.internal_orders.get_mut(&chase_snapshot.spot_client_order_id) {
                order.status = "NEW".to_string();
            }
            if let Some(order) = self.internal_orders.get_mut(&chase_snapshot.futures_client_order_id) {
                order.status = "NEW".to_string();
            }
            return;
        }

        let spot_res = self
            .binance_rest
            .place_spot_limit_order(
                &chase_snapshot.symbol,
                chase_snapshot.spot_side,
                &spot_qty_str,
                &spot_price_str,
                &chase_snapshot.spot_client_order_id,
            )
            .await;

        let spot_placed = if let Ok(body) = spot_res {
            info!("Spot Maker order placed: {}", body);
            if let Some(order) = self.internal_orders.get_mut(&chase_snapshot.spot_client_order_id) {
                order.status = "NEW".to_string();
            }
            true
        } else {
            error!("Failed Spot Maker: {:?}", spot_res.err());
            if let Some(order) = self.internal_orders.get_mut(&chase_snapshot.spot_client_order_id) {
                order.status = "REJECTED".to_string();
            }
            false
        };

        let fut_res = self
            .binance_rest
            .place_futures_limit_order(
                &chase_snapshot.symbol,
                chase_snapshot.futures_side,
                &fut_qty_str,
                &fut_price_str,
                &chase_snapshot.futures_client_order_id,
            )
            .await;

        let fut_placed = if let Ok(body) = fut_res {
            info!("Futures Maker order placed: {}", body);
            if let Some(order) = self.internal_orders.get_mut(&chase_snapshot.futures_client_order_id) {
                order.status = "NEW".to_string();
            }
            true
        } else {
            error!("Failed Futures Maker: {:?}", fut_res.err());
            if let Some(order) = self.internal_orders.get_mut(&chase_snapshot.futures_client_order_id) {
                order.status = "REJECTED".to_string();
            }
            false
        };

        match (spot_placed, fut_placed) {
            (true, true) => {
                if let Some(c) = self.chase_states.get_mut(&sym_upper) {
                    c.phase = ChasePhase::DualMakerPlaced;
                }
            }
            (true, false) => {
                warn!(
                    "Fail-closing {} after futures order submission failed; cancelling resting spot leg",
                    chase_snapshot.symbol
                );
                if let Err(err) = self
                    .binance_rest
                    .cancel_order(&chase_snapshot.symbol, &chase_snapshot.spot_client_order_id)
                    .await
                {
                    error!("Spot cancel failed during fail-closed cleanup for {}: {}", chase_snapshot.symbol, err);
                }
                if let Some(order) = self.internal_orders.get_mut(&chase_snapshot.spot_client_order_id) {
                    order.status = "CANCELED".to_string();
                }
                self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    &chase_snapshot.futures_client_order_id,
                    0.0,
                    false,
                    "DUAL_SUBMISSION_FAILED",
                );
                self.chase_states.remove(&sym_upper);
            }
            (false, true) => {
                warn!(
                    "Fail-closing {} after spot order submission failed; cancelling resting futures leg",
                    chase_snapshot.symbol
                );
                if let Err(err) = self
                    .binance_rest
                    .cancel_futures_order(&chase_snapshot.symbol, &chase_snapshot.futures_client_order_id)
                    .await
                {
                    error!(
                        "Futures cancel failed during fail-closed cleanup for {}: {}",
                        chase_snapshot.symbol, err
                    );
                }
                if let Some(order) = self.internal_orders.get_mut(&chase_snapshot.futures_client_order_id) {
                    order.status = "CANCELED".to_string();
                }
                self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    &chase_snapshot.spot_client_order_id,
                    0.0,
                    false,
                    "DUAL_SUBMISSION_FAILED",
                );
                self.chase_states.remove(&sym_upper);
            }
            (false, false) => {
                self.emit_cycle_order_update(
                    &chase_snapshot,
                    "REJECTED",
                    &chase_snapshot.spot_client_order_id,
                    0.0,
                    false,
                    "DUAL_SUBMISSION_FAILED",
                );
                self.chase_states.remove(&sym_upper);
            }
        }
    }

    async fn execute_reconciliation_sequence(&mut self) {
        // Skip reconciliation in paper mode — no real account to reconcile
        if self.trading_mode == "paper" {
            info!("Paper mode: skipping reconciliation, transitioning directly to Trading state.");
            self.state = SystemState::Trading;
            return;
        }

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

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::sync::{broadcast, mpsc};
    use tokio::time::timeout;

    #[tokio::test]
    async fn paper_dual_maker_waits_for_cross_before_filling() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, mut engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );
        manager.state = SystemState::Trading;

        manager.chase_states.insert(
            "BTCUSDT".to_string(),
            ChaseState {
                symbol: "BTCUSDT".to_string(),
                quantity: 1.0,
                spot_client_order_id: "spot-cid".to_string(),
                futures_client_order_id: "fut-cid".to_string(),
                spot_side: TradeSide::Buy,
                futures_side: TradeSide::Sell,
                is_exit: false,
                phase: ChasePhase::Idle,
                start_time: Instant::now(),
                expected_spot_price: 0.0,
                expected_fut_price: 0.0,
                spot_fill_price: None,
                futures_fill_price: None,
            },
        );
        manager.spot_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 100.0,
                ask_price: 101.0,
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 103.0,
                ask_price: 104.0,
            },
        );

        manager.try_place_dual_maker("BTCUSDT".to_string()).await;

        let no_fill = timeout(Duration::from_millis(150), engine_rx.recv()).await;
        assert!(no_fill.is_err(), "paper maker orders should rest until the book crosses");

        manager
            .handle_ws_event(WsEvent::L2Depth {
                symbol: "BTCUSDT".to_string(),
                market: MarketType::Spot,
                bids: vec![[99.0, 1.0]],
                asks: vec![[100.0, 1.0]],
            })
            .await;
        manager
            .handle_ws_event(WsEvent::L2Depth {
                symbol: "BTCUSDT".to_string(),
                market: MarketType::Perp,
                bids: vec![[104.0, 1.0]],
                asks: vec![[104.5, 1.0]],
            })
            .await;

        let first = timeout(Duration::from_millis(400), engine_rx.recv())
            .await
            .expect("first paper fill event should arrive")
            .expect("first paper fill event should be present");
        let second = timeout(Duration::from_millis(400), engine_rx.recv())
            .await
            .expect("second paper fill event should arrive")
            .expect("second paper fill event should be present");

        match first {
            EngineEvent::Ws(WsEvent::OrderUpdate {
                client_order_id,
                avg_fill_price,
                execution_type,
                ..
            }) => {
                assert_eq!(client_order_id, "spot-cid");
                assert_eq!(avg_fill_price, Some(100.0));
                assert_eq!(execution_type.as_deref(), Some("PAPER_RESTING_CROSS_FILL"));
            }
            other => panic!("unexpected first engine event: {:?}", other_type_name(&other)),
        }

        match second {
            EngineEvent::Ws(WsEvent::OrderUpdate {
                client_order_id,
                avg_fill_price,
                execution_type,
                ..
            }) => {
                assert_eq!(client_order_id, "fut-cid");
                assert_eq!(avg_fill_price, Some(104.0));
                assert_eq!(execution_type.as_deref(), Some("PAPER_RESTING_CROSS_FILL"));
            }
            other => panic!("unexpected second engine event: {:?}", other_type_name(&other)),
        }
    }

    #[tokio::test]
    async fn paper_legging_timeout_uses_current_book_for_taker_fill() {
        let (_event_tx, event_rx) = mpsc::channel(4);
        let (engine_tx, mut engine_rx) = mpsc::channel(8);
        let (subscription_tx, _subscription_rx) = mpsc::channel(4);
        let (dash_tx, _dash_rx) = broadcast::channel(8);

        let mut manager = OrderManager::new(
            event_rx,
            engine_tx,
            subscription_tx,
            String::new(),
            String::new(),
            dash_tx,
            "paper".to_string(),
        );

        manager.chase_states.insert(
            "BTCUSDT".to_string(),
            ChaseState {
                symbol: "BTCUSDT".to_string(),
                quantity: 1.0,
                spot_client_order_id: "spot-cid".to_string(),
                futures_client_order_id: "fut-cid".to_string(),
                spot_side: TradeSide::Buy,
                futures_side: TradeSide::Sell,
                is_exit: false,
                phase: ChasePhase::LegFilledWaiting(Leg::Spot),
                start_time: Instant::now(),
                expected_spot_price: 100.0,
                expected_fut_price: 104.0,
                spot_fill_price: Some(100.0),
                futures_fill_price: None,
            },
        );
        manager.internal_orders.insert(
            "fut-cid".to_string(),
            InternalOrder {
                client_order_id: "fut-cid".to_string(),
                symbol: "BTCUSDT".to_string(),
                status: "NEW".to_string(),
                limit_price: Some(104.0),
            },
        );
        manager.perp_top_cache.insert(
            "BTCUSDT".to_string(),
            TopOfBook {
                bid_price: 103.0,
                ask_price: 103.5,
            },
        );

        manager.handle_legging_timeout("spot-cid".to_string()).await;

        let taker_fill = timeout(Duration::from_millis(400), engine_rx.recv())
            .await
            .expect("paper taker fill should arrive")
            .expect("paper taker fill should be present");

        match taker_fill {
            EngineEvent::Ws(WsEvent::OrderUpdate {
                symbol,
                avg_fill_price,
                maker,
                execution_type,
                ..
            }) => {
                assert_eq!(symbol, "BTCUSDT");
                assert_eq!(avg_fill_price, Some(103.0));
                assert_eq!(maker, Some(false));
                assert_eq!(execution_type.as_deref(), Some("PAPER_TAKER_FILL"));
            }
            other => panic!("unexpected taker engine event: {:?}", other_type_name(&other)),
        }

        let canceled = manager
            .internal_orders
            .get("fut-cid")
            .expect("unfilled maker leg should still be tracked");
        assert_eq!(canceled.status, "CANCELED");
    }

    fn other_type_name(event: &EngineEvent) -> &'static str {
        match event {
            EngineEvent::Ws(_) => "ws",
            EngineEvent::Alpha(_) => "alpha",
            EngineEvent::LeggingTimeout(_) => "legging_timeout",
        }
    }
}
