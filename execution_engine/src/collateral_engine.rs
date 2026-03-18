//! A dedicated, pure-math engine for simulating Binance Unified Portfolio Margin (PM) locally.
//! Validates margin usage against the unified account rather than isolated accounts.

#[derive(Debug, Clone, Copy, PartialEq)]
pub enum PositionSide {
    Long,
    Short,
}

#[derive(Debug, Clone)]
pub struct Position {
    pub symbol: String,
    pub side: PositionSide,
    pub entry_price: f64,
    pub quantity: f64,
    pub is_spot: bool,
}

impl Position {
    /// Calculate current Notional Value based on Mark Price
    pub fn notional_value(&self, mark_price: f64) -> f64 {
        mark_price * self.quantity
    }

    /// Calculate Unrealized PnL based on Mark Price
    pub fn unrealized_pnl(&self, mark_price: f64) -> f64 {
        match self.side {
            PositionSide::Long => (mark_price - self.entry_price) * self.quantity,
            PositionSide::Short => (self.entry_price - mark_price) * self.quantity,
        }
    }
}

pub struct UnifiedPortfolioMarginCalculator {
    /// Total baseline USD equity before PNL
    pub base_equity_usd: f64,
    
    /// Universal Maintenance Margin Rate (assumed flat for simplicity, usually tiered)
    pub maintenance_margin_rate: f64,

    /// Danger threshold for uniMMR.
    pub danger_threshold: f64,
}

impl UnifiedPortfolioMarginCalculator {
    pub fn new(base_equity_usd: f64, mmr: f64, danger_threshold: f64) -> Self {
        Self {
            base_equity_usd,
            maintenance_margin_rate: mmr,
            danger_threshold,
        }
    }

    /// Computes the uniMMR (Unified Maintenance Margin Ratio) 
    /// accounting for perfectly hedged spot long + perp short.
    pub fn calculate_uni_mmr(&self, spot_leg: &Position, perp_leg: &Position, spot_mark: f64, perp_mark: f64) -> f64 {
        let spot_upnl = spot_leg.unrealized_pnl(spot_mark);
        let perp_upnl = perp_leg.unrealized_pnl(perp_mark);
        
        // Unified Equity = Base + Spot UPNL + Perp UPNL
        let unified_equity = self.base_equity_usd + spot_upnl + perp_upnl;

        if unified_equity <= 0.0 {
            return f64::INFINITY; 
        }

        // In PM, fully hedged spot + perp often have heavily offset Maintenance Margin requirements.
        let spot_notional = spot_leg.notional_value(spot_mark);
        let perp_notional = perp_leg.notional_value(perp_mark);
        
        // Total MM is sum of legs (Binance PM provides offsets, but worst case is strictly additive without taking PM correlations. 
        // We'll mimic the offset by taking max leg or hedged risk).
        // For accurate PM, uniMM = abs(spot_notional - perp_notional) * mmr + (hedged notional * lower_mmr)
        // Simplification for alpha audit:
        let directional_risk = (spot_notional - perp_notional).abs();
        let uni_mm = directional_risk * self.maintenance_margin_rate;

        // Margin Ratio = uniMM / Unified Equity
        uni_mm / unified_equity
    }

    pub fn requires_defense(&self, spot_leg: &Position, perp_leg: &Position, spot_mark: f64, perp_mark: f64) -> bool {
        let ratio = self.calculate_uni_mmr(spot_leg, perp_leg, spot_mark, perp_mark);
        ratio >= self.danger_threshold
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn setup_pm_test() -> (UnifiedPortfolioMarginCalculator, Position, Position) {
        let calc = UnifiedPortfolioMarginCalculator::new(
            1000.0,  
            0.004,   // 0.4% baseline risk weight
            0.8      // 80% danger threshold
        );

        let spot_pos = Position {
            symbol: "BTCUSDT".to_string(),
            side: PositionSide::Long,
            entry_price: 100_000.0,
            quantity: 0.1,
            is_spot: true,
        };

        let perp_pos = Position {
            symbol: "BTCUSDT".to_string(),
            side: PositionSide::Short,
            entry_price: 100_000.0,
            quantity: 0.1,
            is_spot: false,
        };

        (calc, spot_pos, perp_pos)
    }

    #[test]
    fn test_pm_perfect_hedge_ratio() {
        let (calc, spot, perp) = setup_pm_test();
        let mark_price = 105_000.0;
        
        let ratio = calc.calculate_uni_mmr(&spot, &perp, mark_price, mark_price);
        // Hedged positions have 0 directional risk and thus ~0 uni_mm
        // UPNL balances out, Equity remains 1000.
        assert!((ratio - 0.0).abs() < 1e-6);
        assert!(!calc.requires_defense(&spot, &perp, mark_price, mark_price));
    }

    #[test]
    fn test_pm_skewed_basis_blowout() {
        let (calc, spot, perp) = setup_pm_test();
        // Severe basis blowout: perp trades at $110,000, spot at $105,000
        let spot_mark = 105_000.0;
        let perp_mark = 110_000.0;
        
        let ratio = calc.calculate_uni_mmr(&spot, &perp, spot_mark, perp_mark);
        
        // Spot UPNL: (105k - 100k) * 0.1 = +$500
        // Perp UPNL: (100k - 110k) * 0.1 = -$1000
        // Total Equity: 1000 + 500 - 1000 = 500
        
        // Directional Risk = |(105k * 0.1) - (110k * 0.1)| = |10500 - 11000| = 500
        // uniMM = 500 * 0.004 = 2.0
        
        // Ratio = 2.0 / 500 = 0.004
        assert!((ratio - 0.004).abs() < 1e-6);
        assert!(!calc.requires_defense(&spot, &perp, spot_mark, perp_mark));
    }
}
