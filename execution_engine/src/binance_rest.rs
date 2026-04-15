#![allow(dead_code)]
use hmac::{Hmac, Mac};
use reqwest::{Client, Method, RequestBuilder};
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

use std::sync::atomic::{AtomicI64, Ordering};

const MAINNET_FUTURES_BASE_URL: &str = "https://fapi.binance.com";
const TESTNET_FUTURES_BASE_URL: &str = "https://demo-fapi.binance.com";
const MAINNET_SPOT_BASE_URL: &str = "https://api.binance.com";
const TESTNET_SPOT_BASE_URL: &str = "https://demo-api.binance.com";

pub struct BinanceRest {
    client: Client,
    api_key: String,
    secret_key: String,
    spot_api_key: String,
    spot_secret_key: String,
    pub fut_base_url: String,
    pub spot_base_url: String,
    pub time_offset: std::sync::Arc<AtomicI64>,
    pub trading_mode: String,
}

#[derive(Debug, Clone)]
pub struct ExchangeSymbolInfo {
    pub symbol: String,
    pub spot_tick_size: f64,
    pub spot_step_size: f64,
    pub futures_tick_size: f64,
    pub futures_step_size: f64,
}

#[derive(Debug, Clone, Copy)]
pub enum TradeSide {
    Buy,
    Sell,
}

impl TradeSide {
    fn as_str(&self) -> &'static str {
        match self {
            TradeSide::Buy => "BUY",
            TradeSide::Sell => "SELL",
        }
    }
}

#[derive(Debug, Clone, Copy)]
pub enum LegVenue {
    Spot,
    UsdtFutures,
}

impl BinanceRest {
    pub fn new(api_key: String, secret_key: String, trading_mode: String) -> Self {
        let mut futures_api_key = api_key.trim().to_string();
        let mut futures_secret_key = secret_key.trim().to_string();
        let raw_spot_api_key = std::env::var("BINANCE_SPOT_API_KEY")
            .unwrap_or_default()
            .trim()
            .to_string();
        let raw_spot_secret_key = std::env::var("BINANCE_SPOT_API_SECRET")
            .unwrap_or_default()
            .trim()
            .to_string();

        if futures_api_key.is_empty() {
            futures_api_key = raw_spot_api_key.clone();
        }
        if futures_secret_key.is_empty() {
            futures_secret_key = raw_spot_secret_key.clone();
        }

        let spot_api_key = if raw_spot_api_key.is_empty() {
            futures_api_key.clone()
        } else {
            raw_spot_api_key
        };
        let spot_secret_key = if raw_spot_secret_key.is_empty() {
            futures_secret_key.clone()
        } else {
            raw_spot_secret_key
        };

        let (fut_base_url, spot_base_url) = match trading_mode.as_str() {
            "testnet" => (
                TESTNET_FUTURES_BASE_URL.to_string(),
                TESTNET_SPOT_BASE_URL.to_string(),
            ),
            _ => (
                MAINNET_FUTURES_BASE_URL.to_string(),
                MAINNET_SPOT_BASE_URL.to_string(),
            ),
        };

        Self {
            client: Client::new(),
            api_key: futures_api_key,
            secret_key: futures_secret_key,
            spot_api_key,
            spot_secret_key,
            fut_base_url,
            spot_base_url,
            time_offset: std::sync::Arc::new(AtomicI64::new(0)),
            trading_mode,
        }
    }

    pub async fn sync_time(&self) -> Result<(), String> {
        let url = format!("{}/fapi/v1/time", self.fut_base_url);
        let resp = self
            .client
            .get(&url)
            .send()
            .await
            .map_err(|e| e.to_string())?;
        let text = resp.text().await.map_err(|e| e.to_string())?;
        let json: serde_json::Value = serde_json::from_str(&text).map_err(|e| e.to_string())?;
        if let Some(server_time) = json.get("serverTime").and_then(|v| v.as_i64()) {
            let local_time = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("Time")
                .as_millis() as i64;
            self.time_offset
                .store(server_time - local_time, Ordering::Relaxed);
        }
        Ok(())
    }

    pub async fn get_exchange_info(
        &self,
    ) -> Result<std::collections::HashMap<String, ExchangeSymbolInfo>, String> {
        let futures_primary_url = format!("{}/fapi/v1/exchangeInfo", self.fut_base_url);
        let spot_primary_url = format!("{}/api/v3/exchangeInfo", self.spot_base_url);

        let futures_json = self
            .fetch_exchange_info_json_with_fallback(
                &futures_primary_url,
                None,
                "futures exchange info",
            )
            .await?;
        let spot_json = self
            .fetch_exchange_info_json_with_fallback(&spot_primary_url, None, "spot exchange info")
            .await?;

        let futures_filters = Self::parse_symbol_filters(&futures_json);
        let spot_filters = Self::parse_symbol_filters(&spot_json);

        let mut info_map = std::collections::HashMap::new();
        let mut symbols: std::collections::HashSet<String> = std::collections::HashSet::new();
        symbols.extend(futures_filters.keys().cloned());
        symbols.extend(spot_filters.keys().cloned());

        for symbol in symbols {
            let (spot_tick_size, spot_step_size) =
                spot_filters.get(&symbol).copied().unwrap_or((0.1, 0.1));
            let (futures_tick_size, futures_step_size) =
                futures_filters.get(&symbol).copied().unwrap_or((0.1, 0.1));
            info_map.insert(
                symbol.clone(),
                ExchangeSymbolInfo {
                    symbol,
                    spot_tick_size,
                    spot_step_size,
                    futures_tick_size,
                    futures_step_size,
                },
            );
        }

        Ok(info_map)
    }

    async fn fetch_exchange_info_json_with_fallback(
        &self,
        primary_url: &str,
        fallback_url: Option<&str>,
        label: &str,
    ) -> Result<serde_json::Value, String> {
        match self.fetch_exchange_info_json(primary_url, label).await {
            Ok(json) => Ok(json),
            Err(primary_err) => {
                let Some(fallback_url) = fallback_url else {
                    return Err(primary_err);
                };
                if fallback_url == primary_url {
                    return Err(primary_err);
                }

                tracing::warn!(
                    "Primary {} endpoint failed in {} mode: {}. Falling back to {}",
                    label,
                    self.trading_mode,
                    primary_err,
                    fallback_url,
                );
                self.fetch_exchange_info_json(fallback_url, label).await
            }
        }
    }

    async fn fetch_exchange_info_json(
        &self,
        url: &str,
        label: &str,
    ) -> Result<serde_json::Value, String> {
        let response = self
            .client
            .get(url)
            .send()
            .await
            .map_err(|e| format!("Failed to fetch {} from {}: {}", label, url, e))?;
        let status = response.status();
        let text = response
            .text()
            .await
            .map_err(|e| format!("Failed to read {} text from {}: {}", label, url, e))?;

        if !status.is_success() {
            return Err(format!(
                "{} from {} returned HTTP {} ({})",
                label,
                url,
                status.as_u16(),
                Self::preview_body(&text),
            ));
        }

        serde_json::from_str(&text).map_err(|e| {
            format!(
                "Failed to parse {} JSON from {}: {} (body starts with: {})",
                label,
                url,
                e,
                Self::preview_body(&text),
            )
        })
    }

    fn preview_body(text: &str) -> String {
        let mut preview = text.replace('\n', " ");
        if preview.len() > 120 {
            preview.truncate(120);
        }
        preview
    }

    fn exchange_error(text: &str) -> Option<String> {
        let json = serde_json::from_str::<serde_json::Value>(text).ok()?;
        let code = json.get("code").and_then(|value| value.as_i64())?;
        if code >= 0 {
            return None;
        }
        let msg = json
            .get("msg")
            .and_then(|value| value.as_str())
            .unwrap_or("unknown exchange error");
        Some(format!("exchange error {} ({})", code, msg))
    }

    async fn send_checked_text(
        &self,
        req: RequestBuilder,
        context: &str,
    ) -> Result<String, String> {
        let response = req
            .send()
            .await
            .map_err(|err| format!("{} request failed: {}", context, err))?;
        let status = response.status();
        let text = response
            .text()
            .await
            .map_err(|err| format!("{} response read failed: {}", context, err))?;

        if !status.is_success() {
            let details = Self::exchange_error(&text).unwrap_or_else(|| Self::preview_body(&text));
            return Err(format!(
                "{} returned HTTP {} ({})",
                context,
                status.as_u16(),
                details,
            ));
        }

        if let Some(details) = Self::exchange_error(&text) {
            return Err(format!("{} {}", context, details));
        }

        Ok(text)
    }

    fn parse_symbol_filters(
        json: &serde_json::Value,
    ) -> std::collections::HashMap<String, (f64, f64)> {
        let mut parsed = std::collections::HashMap::new();
        if let Some(symbols) = json.get("symbols").and_then(|s| s.as_array()) {
            for sym in symbols {
                let symbol = sym
                    .get("symbol")
                    .and_then(|s| s.as_str())
                    .unwrap_or("")
                    .to_string();
                if symbol.is_empty() {
                    continue;
                }

                let mut tick_size = 0.1;
                let mut step_size = 0.1;
                if let Some(filters) = sym.get("filters").and_then(|f| f.as_array()) {
                    for filter in filters {
                        if let Some(filter_type) = filter.get("filterType").and_then(|t| t.as_str())
                        {
                            if filter_type == "PRICE_FILTER" {
                                if let Some(ts) = filter.get("tickSize").and_then(|t| t.as_str()) {
                                    tick_size = ts.parse().unwrap_or(0.1);
                                }
                            } else if filter_type == "LOT_SIZE" {
                                if let Some(ss) = filter.get("stepSize").and_then(|s| s.as_str()) {
                                    step_size = ss.parse().unwrap_or(0.1);
                                }
                            }
                        }
                    }
                }

                parsed.insert(symbol, (tick_size, step_size));
            }
        }

        parsed
    }

    fn current_timestamp(&self) -> u64 {
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("Time went backwards")
            .as_millis() as i64;
        let offset = self.time_offset.load(Ordering::Relaxed);
        (ts + offset) as u64
    }

    fn sign(&self, secret_key: &str, query_string: &str) -> String {
        let mut mac = HmacSha256::new_from_slice(secret_key.as_bytes())
            .expect("HMAC can take key of any size");
        mac.update(query_string.as_bytes());
        let result = mac.finalize();
        hex::encode(result.into_bytes())
    }

    fn build_signed_request_with_base(
        &self,
        method: Method,
        base_url: &str,
        endpoint: &str,
        mut params: Vec<(&str, String)>,
    ) -> RequestBuilder {
        params.push(("recvWindow", "60000".to_string()));
        params.push(("timestamp", self.current_timestamp().to_string()));

        let query_string = params
            .iter()
            .map(|(k, v)| format!("{}={}", k, v))
            .collect::<Vec<String>>()
            .join("&");

        let (api_key_to_use, secret_key_to_use) = if base_url == self.spot_base_url {
            (&self.spot_api_key, &self.spot_secret_key)
        } else {
            (&self.api_key, &self.secret_key)
        };

        let signature = self.sign(secret_key_to_use, &query_string);
        let final_query = format!("{}&signature={}", query_string, signature);
        let url = format!("{}{}?{}", base_url, endpoint, final_query);

        self.client
            .request(method, &url)
            .header("X-MBX-APIKEY", api_key_to_use)
    }

    pub async fn get_open_orders(&self) -> Result<String, String> {
        let req = self.build_signed_request_with_base(
            Method::GET,
            &self.fut_base_url,
            "/fapi/v1/openOrders",
            vec![],
        );
        self.send_checked_text(req, "open orders").await
    }

    pub async fn get_account(&self) -> Result<String, String> {
        let req = self.build_signed_request_with_base(
            Method::GET,
            &self.spot_base_url,
            "/api/v3/account",
            vec![],
        );
        self.send_checked_text(req, "spot account").await
    }

    pub async fn get_fapi_account(&self) -> Result<String, String> {
        let req = self.build_signed_request_with_base(
            Method::GET,
            &self.fut_base_url,
            "/fapi/v2/account",
            vec![],
        );
        self.send_checked_text(req, "futures account").await
    }

    pub async fn get_pm_account(&self) -> Result<String, String> {
        // Binance Portfolio Margin Account endpoint (uniMMR)
        let req = self.build_signed_request_with_base(
            Method::GET,
            "https://papi.binance.com",
            "/papi/v1/account",
            vec![],
        );
        self.send_checked_text(req, "portfolio margin account")
            .await
    }

    pub async fn get_pm_um_account(&self) -> Result<String, String> {
        // Binance Portfolio Margin U-margined endpoint
        let req = self.build_signed_request_with_base(
            Method::GET,
            "https://papi.binance.com",
            "/papi/v1/um/account",
            vec![],
        );
        self.send_checked_text(req, "portfolio margin um account")
            .await
    }

    pub async fn cancel_order(&self, symbol: &str, order_id: &str) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999998,\"status\":\"CANCELED\"}".to_string());
        }

        let params = vec![
            ("symbol", symbol.to_string()),
            ("origClientOrderId", order_id.to_string()),
        ];
        let req = self.build_signed_request_with_base(
            Method::DELETE,
            &self.spot_base_url,
            "/api/v3/order",
            params,
        );
        self.send_checked_text(req, "spot cancel order").await
    }

    pub async fn cancel_futures_order(
        &self,
        symbol: &str,
        order_id: &str,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999997,\"status\":\"CANCELED\"}".to_string());
        }

        let params = vec![
            ("symbol", symbol.to_string()),
            ("origClientOrderId", order_id.to_string()),
        ];
        let req = self.build_signed_request_with_base(
            Method::DELETE,
            &self.fut_base_url,
            "/fapi/v1/order",
            params,
        );
        self.send_checked_text(req, "futures cancel order").await
    }

    pub async fn place_spot_market_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999999,\"status\":\"FILLED\"}".to_string());
        }

        let params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "MARKET".to_string()),
            ("quantity", quantity.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];

        let req = self.build_signed_request_with_base(
            Method::POST,
            &self.spot_base_url,
            "/api/v3/order",
            params,
        );
        self.send_checked_text(req, "spot market order").await
    }

    pub async fn place_spot_limit_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999998,\"status\":\"NEW\"}".to_string());
        }

        let params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "LIMIT".to_string()),
            ("timeInForce", "GTC".to_string()),
            ("quantity", quantity.to_string()),
            ("price", price.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];

        let req = self.build_signed_request_with_base(
            Method::POST,
            &self.spot_base_url,
            "/api/v3/order",
            params,
        );
        self.send_checked_text(req, "spot limit order").await
    }

    pub async fn place_futures_limit_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999997,\"status\":\"NEW\"}".to_string());
        }

        let params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "LIMIT".to_string()),
            ("timeInForce", "GTC".to_string()),
            ("quantity", quantity.to_string()),
            ("price", price.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];

        let req = self.build_signed_request_with_base(
            Method::POST,
            &self.fut_base_url,
            "/fapi/v1/order",
            params,
        );
        self.send_checked_text(req, "futures limit order").await
    }

    pub async fn place_futures_market_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999996,\"status\":\"FILLED\"}".to_string());
        }

        let mut params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "MARKET".to_string()),
            ("quantity", quantity.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];
        if reduce_only {
            params.push(("reduceOnly", "true".to_string()));
        }

        let req = self.build_signed_request_with_base(
            Method::POST,
            &self.fut_base_url,
            "/fapi/v1/order",
            params,
        );
        self.send_checked_text(req, "futures market order").await
    }

    pub async fn get_order_by_client_id(
        &self,
        venue: LegVenue,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<String, String> {
        let params = match venue {
            LegVenue::Spot => vec![
                ("symbol", symbol.to_string()),
                ("origClientOrderId", client_order_id.to_string()),
            ],
            LegVenue::UsdtFutures => vec![
                ("symbol", symbol.to_string()),
                ("origClientOrderId", client_order_id.to_string()),
            ],
        };

        let req = match venue {
            LegVenue::Spot => self.build_signed_request_with_base(
                Method::GET,
                &self.spot_base_url,
                "/api/v3/order",
                params,
            ),
            LegVenue::UsdtFutures => self.build_signed_request_with_base(
                Method::GET,
                &self.fut_base_url,
                "/fapi/v1/order",
                params,
            ),
        };

        self.send_checked_text(req, "query order by client id")
            .await
    }

    pub async fn create_listen_key(&self) -> Result<String, reqwest::Error> {
        let url = format!("{}/fapi/v1/listenKey", self.fut_base_url);
        let req = self.client.post(&url).header("X-MBX-APIKEY", &self.api_key);
        req.send().await?.text().await
    }

    pub async fn create_spot_listen_key(&self) -> Result<String, reqwest::Error> {
        let url = format!("{}/api/v3/userDataStream", self.spot_base_url);
        let req = self
            .client
            .post(&url)
            .header("X-MBX-APIKEY", &self.spot_api_key);
        req.send().await?.text().await
    }

    pub async fn keepalive_listen_key(&self, listen_key: &str) -> Result<String, reqwest::Error> {
        let url = format!(
            "{}/fapi/v1/listenKey?listenKey={}",
            self.fut_base_url, listen_key
        );
        let req = self.client.put(&url).header("X-MBX-APIKEY", &self.api_key);
        req.send().await?.text().await
    }

    pub async fn keepalive_spot_listen_key(
        &self,
        listen_key: &str,
    ) -> Result<String, reqwest::Error> {
        let url = format!(
            "{}/api/v3/userDataStream?listenKey={}",
            self.spot_base_url, listen_key
        );
        let req = self
            .client
            .put(&url)
            .header("X-MBX-APIKEY", &self.spot_api_key);
        req.send().await?.text().await
    }

    pub async fn close_listen_key(&self, listen_key: &str) -> Result<String, reqwest::Error> {
        let url = format!(
            "{}/fapi/v1/listenKey?listenKey={}",
            self.fut_base_url, listen_key
        );
        let req = self
            .client
            .delete(&url)
            .header("X-MBX-APIKEY", &self.api_key);
        req.send().await?.text().await
    }

    pub async fn close_spot_listen_key(&self, listen_key: &str) -> Result<String, reqwest::Error> {
        let url = format!(
            "{}/api/v3/userDataStream?listenKey={}",
            self.spot_base_url, listen_key
        );
        let req = self
            .client
            .delete(&url)
            .header("X-MBX-APIKEY", &self.spot_api_key);
        req.send().await?.text().await
    }

    /// Cancel ALL open futures orders for a symbol (emergency shutdown).
    pub async fn cancel_all_open_futures_orders(
        &self,
        symbol: &str,
    ) -> Result<String, reqwest::Error> {
        let params = vec![("symbol", symbol.to_string())];
        let req = self.build_signed_request_with_base(
            Method::DELETE,
            &self.fut_base_url,
            "/fapi/v1/allOpenOrders",
            params,
        );
        req.send().await?.text().await
    }
}

/// Retry wrapper with exponential backoff and jitter for REST API calls.
pub async fn with_retry<F, Fut>(
    operation: F,
    max_retries: u32,
    base_delay_ms: u64,
) -> Result<String, String>
where
    F: Fn() -> Fut,
    Fut: std::future::Future<Output = Result<String, reqwest::Error>>,
{
    let mut last_err = String::new();
    for attempt in 0..=max_retries {
        match operation().await {
            Ok(result) => return Ok(result),
            Err(e) => {
                last_err = e.to_string();
                if attempt < max_retries {
                    let delay = base_delay_ms * 2u64.pow(attempt);
                    let jitter = rand::random::<u64>() % (delay / 2 + 1);
                    tokio::time::sleep(tokio::time::Duration::from_millis(delay + jitter)).await;
                    tracing::warn!("REST retry {}/{}: {}", attempt + 1, max_retries, last_err);
                }
            }
        }
    }
    Err(format!(
        "All {} retries exhausted: {}",
        max_retries, last_err
    ))
}
