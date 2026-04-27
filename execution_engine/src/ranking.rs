use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::time::Instant;
use tracing::info;
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
        
        if !resp.status().is_success() {
            return Err(format!("Binance PremiumIndex API failed with status {}", resp.status()));
        }

        let data: Vec<PremiumIndex> = resp.json().await.map_err(|e: reqwest::Error| e.to_string())?;
        
        for item in data {
            let symbol = item.symbol.to_uppercase();
            let rate: f64 = item.next_funding_rate.parse().unwrap_or(0.0);
            let mark: f64 = item.mark_price.parse().unwrap_or(0.0);
            
            self.rates.insert(symbol.clone(), rate * 1095.0);
            self.mark_prices.insert(symbol, mark);
        }

        info!("Refreshed Binance funding rates for {} symbols", self.rates.len());
        Ok(())
    }

    async fn refresh_bybit(&mut self) -> Result<(), String> {
        let url = "https://api.bybit.com/v5/market/tickers?category=linear";
        let resp = self.binance_rest.client.get(url).send().await.map_err(|e: reqwest::Error| e.to_string())?;
        
        if !resp.status().is_success() {
            return Err(format!("Bybit Tickers API failed with status {}", resp.status()));
        }

        let data: BybitResponse = resp.json().await.map_err(|e: reqwest::Error| e.to_string())?;
        
        for item in data.result.list {
            let symbol = item.symbol.to_uppercase();
            let rate: f64 = item.funding_rate.parse().unwrap_or(0.0);
            self.bybit_rates.insert(symbol, rate * 1095.0);
        }

        info!("Refreshed Bybit funding rates for {} symbols", self.bybit_rates.len());
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
