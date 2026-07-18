use crate::ipc::AlphaInstruction;
use crate::order_manager::TrackedPosition;
use crate::ranking::RankingEngine;
use std::collections::HashMap;
use tracing::{info, warn};

pub struct StrategyEngine {
    pub max_positions: usize,
    pub capital_per_slot: f64, // Notional per slot fallback
    pub max_leverage: f64,
    pub entry_threshold: f64,
    pub exit_threshold: f64,
    pub rotation_gap: f64,
}

impl StrategyEngine {
    pub fn new() -> Self {
        let max_positions = std::env::var("TARGET_CONCURRENT_POSITIONS")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(4);

        let slot_notional: f64 = std::env::var("SLOT_NOTIONAL_USD")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(5000.0);

        let max_leverage: f64 = std::env::var("MAX_LEVERAGE")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(2.0);

        let capital_per_slot = slot_notional / max_leverage.max(1.0);

        let entry_threshold = std::env::var("ENTRY_ANN_FUNDING_THRESHOLD")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(0.12);

        let exit_threshold = std::env::var("EXIT_ANN_FUNDING_THRESHOLD")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(0.02);

        let rotation_gap = std::env::var("ROTATION_MIN_GAP_ANN")
            .ok()
            .and_then(|v| v.parse().ok())
            .unwrap_or(0.03);

        Self {
            max_positions,
            capital_per_slot,
            max_leverage,
            entry_threshold,
            exit_threshold,
            rotation_gap,
        }
    }

    pub fn generate_instructions(
        &self,
        ranking: &RankingEngine,
        current_positions: &HashMap<String, TrackedPosition>,
        account_equity: f64,
    ) -> Vec<AlphaInstruction> {
        let mut instructions = Vec::new();

        // Calculate dynamic slot size (auto-compounding)
        let slot_equity = if account_equity > 0.0 {
            account_equity / self.max_positions as f64
        } else {
            self.capital_per_slot
        };
        let target_notional = slot_equity * self.max_leverage;

        // 1. Check for Exits (Stale funding)
        for (symbol, pos) in current_positions {
            let current_rate = ranking.get_rate(symbol);
            if current_rate < self.exit_threshold {
                info!(
                    "Strategy: Symbol {} funding {:.4} below exit threshold {:.4}. Exiting.",
                    symbol, current_rate, self.exit_threshold
                );
                let spot_quantity = pos.spot.as_ref().map(|leg| leg.quantity).unwrap_or(0.0);
                let perp_quantity = pos.perp.as_ref().map(|leg| leg.quantity).unwrap_or(0.0);
                instructions.push(
                    AlphaInstruction {
                        symbol: Some(symbol.clone()),
                        intent: "EXIT_LONG".to_string(),
                        quantity: 0.0,
                        urgency: 1.0,
                        max_slippage_bps: 10.0,
                        exposure_scale: 1.0,
                        heartbeat_id: None,
                        intent_id: Some(format!("rust_exit_{}", symbol)),
                        direction: Some("long".to_string()),
                        skip_spot_leg: spot_quantity <= 0.0,
                        skip_perp_leg: perp_quantity <= 0.0,
                        spot_entry_price: None,
                        perp_entry_price: None,
                        spot_mark_price: None,
                        perp_mark_price: None,
                        spot_quantity: Some(spot_quantity),
                        perp_quantity: Some(perp_quantity),
                        ..AlphaInstruction::default()
                    }
                    .seal_internal(),
                );
            }
        }

        // 2. Check for Rotations or New Enters
        let ranked = ranking.get_ranked_funding();
        let open_symbols: Vec<String> = current_positions.keys().cloned().collect();
        let mut free_slots = self.max_positions.saturating_sub(open_symbols.len());

        for (symbol, rate) in ranked {
            if rate < self.entry_threshold {
                break;
            }
            if open_symbols.contains(&symbol) {
                continue;
            }

            let mark_price = ranking.get_mark_price(&symbol);
            if mark_price <= 0.0 {
                continue;
            }

            // Cross-validation with Bybit
            if let Some(bybit_rate) = ranking.get_bybit_rate(&symbol) {
                if rate > self.entry_threshold && bybit_rate < 0.05 {
                    warn!(
                        "Strategy: Binance rate {:.4} for {} but Bybit is {:.4}. Divergence detected, skipping entry.",
                        rate, symbol, bybit_rate
                    );
                    continue;
                }
            }

            if free_slots > 0 {
                info!(
                    "Strategy: High funding {:.4} for {}. Entering with notional ${:.2}.",
                    rate, symbol, target_notional
                );
                let qty = target_notional / mark_price;
                instructions.push(
                    AlphaInstruction {
                        symbol: Some(symbol.clone()),
                        intent: "ENTER_LONG".to_string(),
                        quantity: qty,
                        urgency: 0.5,
                        max_slippage_bps: 10.0,
                        exposure_scale: 1.0,
                        heartbeat_id: None,
                        intent_id: Some(format!("rust_enter_{}", symbol)),
                        direction: Some("long".to_string()),
                        skip_spot_leg: false,
                        skip_perp_leg: false,
                        spot_entry_price: None,
                        perp_entry_price: None,
                        spot_mark_price: None,
                        perp_mark_price: None,
                        spot_quantity: None,
                        perp_quantity: None,
                        ..AlphaInstruction::default()
                    }
                    .seal_internal(),
                );
                free_slots -= 1;
            } else {
                // Check for rotation
                let mut weakest_sym: Option<String> = None;
                let mut weakest_rate = rate - self.rotation_gap;

                for op_sym in &open_symbols {
                    if instructions
                        .iter()
                        .any(|i| i.symbol.as_ref() == Some(op_sym))
                    {
                        continue;
                    }

                    let op_rate = ranking.get_rate(op_sym);
                    if op_rate < weakest_rate {
                        weakest_rate = op_rate;
                        weakest_sym = Some(op_sym.clone());
                    }
                }

                if let Some(ws) = weakest_sym {
                    info!(
                        "Strategy: Rotating {} ({:.4}) -> {} ({:.4})",
                        ws, weakest_rate, symbol, rate
                    );
                    let weakest_position = current_positions.get(&ws);
                    let spot_quantity = weakest_position
                        .and_then(|pos| pos.spot.as_ref())
                        .map(|leg| leg.quantity)
                        .unwrap_or(0.0);
                    let perp_quantity = weakest_position
                        .and_then(|pos| pos.perp.as_ref())
                        .map(|leg| leg.quantity)
                        .unwrap_or(0.0);
                    instructions.push(
                        AlphaInstruction {
                            symbol: Some(ws.clone()),
                            intent: "EXIT_LONG".to_string(),
                            quantity: 0.0,
                            urgency: 1.0,
                            max_slippage_bps: 10.0,
                            exposure_scale: 1.0,
                            heartbeat_id: None,
                            intent_id: Some(format!("rust_rot_exit_{}", ws)),
                            direction: Some("long".to_string()),
                            skip_spot_leg: spot_quantity <= 0.0,
                            skip_perp_leg: perp_quantity <= 0.0,
                            spot_entry_price: None,
                            perp_entry_price: None,
                            spot_mark_price: None,
                            perp_mark_price: None,
                            spot_quantity: Some(spot_quantity),
                            perp_quantity: Some(perp_quantity),
                            ..AlphaInstruction::default()
                        }
                        .seal_internal(),
                    );
                }
                break;
            }
        }

        instructions
    }
}
