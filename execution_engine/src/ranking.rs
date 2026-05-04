use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::time::Instant;
use tracing::{info, warn};
use crate::binance_rest::BinanceRest;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PremiumIndex {
    pub symbol: String,
    #[serde(rename = "lastFundingRate")]
    pub last_funding_rate: String,
    #[serde(rename = "nextFundingRate")]
    pub next_funding_rate: String,
    #[serde(rename = "markPrice")]
    pub mark_price: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BybitTicker {
    pub symbol: String,
    #[serde(rename = "fundingRate")]
    pub funding_rate: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BybitResponse {
    pub result: BybitResult,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BybitResult {
    pub list: Vec<BybitTicker>,
}

pub struct RankingEngine {
    pub rates: HashMap<String, f64>,
    pub bybit_rates: HashMap<String, f64>,
    pub mark_prices: HashMap<String, f64>,
    pub last_refresh: Option<Instant>,
    pub binance_rest: std::sync::Arc<BinanceRest>,
}

impl RankingEngine {
    pub fn new(binance_rest: std::sync::Arc<BinanceRest>) -> Self {
        Self {
            rates: HashMap::new(),
            bybit_rates: HashMap::new(),
            mark_prices: HashMap::new(),
            last_refresh: None,
            binance_rest,
        }
    }

    pub async fn refresh(&mut self) -> Result<(), String> {
        self.refresh_binance().await?;
        let _ = self.refresh_bybit().await; // Non-critical
        self.last_refresh = Some(Instant::now());
        Ok(())
    }

    async fn refresh_binance(&mut self) -> Result<(), String> {
        let url = format!("{}/fapi/v1/premiumIndex", self.binance_rest.fut_base_url);
        let resp = self.binance_rest.client.get(&url).send().await.map_err(|e: reqwest::Error| e.to_string())?;
        
        let status = resp.status();
        let text = resp.text().await.map_err(|e: reqwest::Error| e.to_string())?;

        if !status.is_success() {
            return Err(format!("Binance PremiumIndex API failed with status {}: {}", status, text));
        }

        let data: Vec<serde_json::Value> = serde_json::from_str(&text).map_err(|e| {
            format!("Failed to parse PremiumIndex JSON: {}. Body starts with: {}", e, &text[..text.len().min(100)])
        })?;
        
        for item in data {
            let symbol = item.get("symbol").and_then(|v| v.as_str()).unwrap_or("").to_uppercase();
            if symbol.is_empty() { continue; }

            let rate_str = item.get("nextFundingRate")
                .or_else(|| item.get("lastFundingRate"))
                .and_then(|v| v.as_str())
                .unwrap_or("0");
            
            let mark_str = item.get("markPrice")
                .and_then(|v| v.as_str())
                .unwrap_or("0");

            let rate: f64 = rate_str.parse().unwrap_or(0.0);
            let mark: f64 = mark_str.parse().unwrap_or(0.0);
            
            self.rates.insert(symbol.clone(), rate * 1095.0);
            self.mark_prices.insert(symbol, mark);
        }

        info!("Refreshed Binance funding rates for {} symbols", self.rates.len());
        Ok(())
    }

    async fn refresh_bybit(&mut self) -> Result<(), String> {
        let url = "https://api.bybit.com/v5/market/tickers?category=linear";
        let resp = self.binance_rest.client.get(url).send().await.map_err(|e: reqwest::Error| e.to_string())?;
        
        let status = resp.status();
        let text = resp.text().await.map_err(|e: reqwest::Error| e.to_string())?;

        if !status.is_success() {
            return Err(format!("Bybit Tickers API failed with status {}: {}", status, text));
        }

        let data: serde_json::Value = serde_json::from_str(&text).map_err(|e| {
            format!("Failed to parse Bybit JSON: {}. Body starts with: {}", e, &text[..text.len().min(100)])
        })?;
        
        if let Some(list) = data.get("result").and_then(|r| r.get("list")).and_then(|l| l.as_array()) {
            for item in list {
                let symbol = item.get("symbol").and_then(|v| v.as_str()).unwrap_or("").to_uppercase();
                if symbol.is_empty() { continue; }

                let rate_str = item.get("fundingRate").and_then(|v| v.as_str()).unwrap_or("0");
                let rate: f64 = rate_str.parse().unwrap_or(0.0);
                self.bybit_rates.insert(symbol, rate * 1095.0);
            }
            info!("Refreshed Bybit funding rates for {} symbols", self.bybit_rates.len());
        } else {
            warn!("Bybit API response missing expected list: {}", text);
        }

        Ok(())
    }

    pub fn get_ranked_funding(&self) -> Vec<(String, f64)> {
        let mut ranked: Vec<(String, f64)> = self.rates.iter().map(|(k, v)| (k.clone(), *v)).collect();
        ranked.sort_by(|a, b| b.1.partial_cmp(&a.1).unwrap_or(std::cmp::Ordering::Equal));
        ranked
    }

    pub fn get_rate(&self, symbol: &str) -> f64 {
        *self.rates.get(symbol).unwrap_or(&0.0)
    }

    pub fn get_bybit_rate(&self, symbol: &str) -> Option<f64> {
        self.bybit_rates.get(symbol).copied()
    }

    pub fn get_mark_price(&self, symbol: &str) -> f64 {
        *self.mark_prices.get(symbol).unwrap_or(&0.0)
    }
}
