#![allow(dead_code)]
use hmac::{Hmac, Mac};
use reqwest::{Client, Method, RequestBuilder};
use sha2::Sha256;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

use std::sync::atomic::{AtomicI64, Ordering};

const MAINNET_FUTURES_BASE_URL: &str = "https://fapi.binance.com";
const TESTNET_FUTURES_BASE_URL: &str = "https://demo-fapi.binance.com";
const MAINNET_SPOT_BASE_URL: &str = "https://api.binance.com";
const TESTNET_SPOT_BASE_URL: &str = "https://demo-api.binance.com";

#[derive(Debug, Clone)]
pub struct BinanceRest {
    pub client: Client,
    api_key: String,
    secret_key: String,
    spot_api_key: String,
    spot_secret_key: String,
    pub fut_base_url: String,
    pub spot_base_url: String,
    pub time_offset: std::sync::Arc<AtomicI64>,
    pub trading_mode: String,
}

#[derive(Debug, Clone, PartialEq)]
pub struct ExchangeSymbolInfo {
    pub symbol: String,
    pub spot_tick_size: f64,
    pub spot_step_size: f64,
    pub spot_max_qty: f64,
    pub spot_min_notional: f64,
    pub futures_tick_size: f64,
    pub futures_step_size: f64,
    pub futures_max_qty: f64,
    pub futures_min_notional: f64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, serde::Serialize, serde::Deserialize)]
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

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ReconciledSubmission {
    pub body: String,
    pub recovered_after_ambiguous_submit: bool,
    pub retried_after_negative_proof: bool,
}

/// Diagnostic process boundary for the accepted-timeout campaign. The caller
/// supplies a local exchange-shaped HTTP endpoint; no Binance host or account
/// is contacted. Every production placement path must recover the accepted
/// POST through an authoritative same-client-ID GET without a second POST.
pub async fn run_rest_timeout_harness(base_url: &str) -> Result<(), String> {
    let mut rest = BinanceRest::new(
        "diagnostic-key".to_string(),
        "diagnostic-secret".to_string(),
        "live".to_string(),
    );
    rest.spot_base_url = base_url.to_string();
    rest.fut_base_url = base_url.to_string();
    rest.client = Client::builder()
        .connect_timeout(Duration::from_millis(50))
        .timeout(Duration::from_millis(75))
        .build()
        .map_err(|error| format!("build_client:{error}"))?;

    let mut outcomes = Vec::new();
    macro_rules! record {
        ($name:expr, $future:expr) => {{
            let receipt = $future.await.map_err(|error| format!("{}:{error}", $name))?;
            outcomes.push(serde_json::json!({
                "name": $name,
                "recovered_after_ambiguous_submit": receipt.recovered_after_ambiguous_submit,
                "retried_after_negative_proof": receipt.retried_after_negative_proof,
            }));
        }};
    }

    record!(
        "spot_limit",
        rest.place_spot_limit_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Buy,
            "0.01",
            "60000",
            "bngs_timeout_spot_limit",
        )
    );
    record!(
        "futures_limit_entry",
        rest.place_futures_limit_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Sell,
            "0.01",
            "60000",
            "bngs_timeout_futures_limit_entry",
            false,
        )
    );
    record!(
        "futures_limit_exit",
        rest.place_futures_limit_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Buy,
            "0.01",
            "60000",
            "bngs_timeout_futures_limit_exit",
            true,
        )
    );
    record!(
        "spot_market",
        rest.place_spot_market_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Sell,
            "0.01",
            "bngs_timeout_spot_market",
        )
    );
    record!(
        "futures_market_entry",
        rest.place_futures_market_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Sell,
            "0.01",
            "bngs_timeout_futures_market_entry",
            false,
        )
    );
    record!(
        "futures_market_exit",
        rest.place_futures_market_order_read_before_retry(
            "BTCUSDT",
            TradeSide::Buy,
            "0.01",
            "bngs_timeout_futures_market_exit",
            true,
        )
    );

    println!(
        "{}",
        serde_json::to_string(&serde_json::json!({
            "schema_version": 1,
            "outcomes": outcomes,
        }))
        .map_err(|error| format!("encode_json:{error}"))?
    );
    Ok(())
}

/// Fetch a sequence of paired spot/perpetual exchange-info snapshots from a
/// local exchange emulator through the production REST/parser path.
pub async fn run_metadata_change_harness(base_url: &str) -> Result<(), String> {
    let mut rest = BinanceRest::new(
        "diagnostic-key".to_string(),
        "diagnostic-secret".to_string(),
        "live".to_string(),
    );
    rest.spot_base_url = base_url.to_string();
    rest.fut_base_url = base_url.to_string();
    rest.client = Client::builder()
        .connect_timeout(Duration::from_millis(250))
        .timeout(Duration::from_secs(2))
        .build()
        .map_err(|error| format!("build_client:{error}"))?;

    let mut stages = Vec::new();
    for index in 0..6 {
        let exchange_info = rest
            .get_exchange_info()
            .await
            .map_err(|error| format!("stage_{index}:{error}"))?;
        let payload = match exchange_info.get("BTCUSDT") {
            Some(info) => serde_json::json!({
                "stage": index,
                "available": true,
                "spot_tick_size": info.spot_tick_size,
                "spot_step_size": info.spot_step_size,
                "spot_min_notional": info.spot_min_notional,
                "futures_tick_size": info.futures_tick_size,
                "futures_step_size": info.futures_step_size,
                "futures_min_notional": info.futures_min_notional,
            }),
            None => serde_json::json!({
                "stage": index,
                "available": false,
            }),
        };
        stages.push(payload);
    }
    println!(
        "{}",
        serde_json::to_string(&serde_json::json!({
            "schema_version": 1,
            "stages": stages,
        }))
        .map_err(|error| format!("encode_json:{error}"))?
    );
    Ok(())
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

        // Every REST operation runs on execution-critical paths.  A bounded
        // client prevents an exchange/network stall from freezing the order
        // actor indefinitely; ambiguous order outcomes are reconciled by the
        // durable state machine instead of being blindly retried.
        let client = Client::builder()
            .connect_timeout(Duration::from_secs(5))
            .timeout(Duration::from_secs(15))
            .pool_idle_timeout(Duration::from_secs(30))
            .build()
            .expect("bounded Binance HTTP client must build");

        Self {
            client,
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
        // A paired trade is executable only when both venues independently
        // report complete, positive filters and TRADING status.  Never invent
        // fallback filters for a missing leg.
        for symbol in spot_filters.keys() {
            let Some((futures_tick_size, futures_step_size, futures_max_qty, futures_min_notional)) =
                futures_filters.get(symbol).copied()
            else {
                continue;
            };
            let Some((spot_tick_size, spot_step_size, spot_max_qty, spot_min_notional)) =
                spot_filters.get(symbol).copied()
            else {
                continue;
            };
            info_map.insert(
                symbol.clone(),
                ExchangeSymbolInfo {
                    symbol: symbol.clone(),
                    spot_tick_size,
                    spot_step_size,
                    spot_max_qty,
                    spot_min_notional,
                    futures_tick_size,
                    futures_step_size,
                    futures_max_qty,
                    futures_min_notional,
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

    fn safe_transport_error(err: &reqwest::Error) -> &'static str {
        if err.is_timeout() {
            "request timed out"
        } else if err.is_connect() {
            "connection failed"
        } else if err.is_body() {
            "response body failed"
        } else if err.is_decode() {
            "response decode failed"
        } else if err.is_request() {
            "request construction or transport failed"
        } else {
            "HTTP transport failed"
        }
    }

    async fn send_checked_text(
        &self,
        req: RequestBuilder,
        context: &str,
    ) -> Result<String, String> {
        let response = req
            .send()
            .await
            .map_err(|err| format!("{}: {}", context, Self::safe_transport_error(&err)))?;
        let status = response.status();
        let text = response
            .text()
            .await
            .map_err(|err| format!("{}: {}", context, Self::safe_transport_error(&err)))?;

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
    ) -> std::collections::HashMap<String, (f64, f64, f64, f64)> {
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

                if sym.get("status").and_then(|value| value.as_str()) != Some("TRADING") {
                    continue;
                }

                let mut tick_size: Option<f64> = None;
                let mut step_size: Option<f64> = None;
                let mut max_qty: Option<f64> = None;
                let mut market_max_qty: Option<f64> = None;
                let mut min_notional: Option<f64> = None;

                if let Some(filters) = sym.get("filters").and_then(|f| f.as_array()) {
                    for filter in filters {
                        if let Some(filter_type) = filter.get("filterType").and_then(|t| t.as_str())
                        {
                            if filter_type == "PRICE_FILTER" {
                                if let Some(ts) = filter.get("tickSize").and_then(|t| t.as_str()) {
                                    tick_size = ts.parse().ok().filter(|value: &f64| *value > 0.0);
                                }
                            } else if filter_type == "LOT_SIZE" {
                                if let Some(ss) = filter.get("stepSize").and_then(|s| s.as_str()) {
                                    step_size = ss.parse().ok().filter(|value: &f64| *value > 0.0);
                                }
                                if let Some(mq) = filter.get("maxQty").and_then(|m| m.as_str()) {
                                    max_qty = mq.parse().ok().filter(|value: &f64| *value > 0.0);
                                }
                            } else if filter_type == "MARKET_LOT_SIZE" {
                                if let Some(mq) = filter.get("maxQty").and_then(|m| m.as_str()) {
                                    market_max_qty =
                                        mq.parse().ok().filter(|value: &f64| *value > 0.0);
                                }
                            } else if filter_type == "NOTIONAL" || filter_type == "MIN_NOTIONAL" {
                                if let Some(mn) = filter
                                    .get("minNotional")
                                    .or_else(|| filter.get("notional"))
                                    .and_then(|n| n.as_str())
                                {
                                    min_notional =
                                        mn.parse().ok().filter(|value: &f64| *value > 0.0);
                                }
                            }
                        }
                    }
                }

                let Some(tick_size) = tick_size else {
                    continue;
                };
                let Some(step_size) = step_size else {
                    continue;
                };
                let Some(max_qty) = max_qty else {
                    continue;
                };
                let Some(min_notional) = min_notional else {
                    continue;
                };
                let final_max_qty = market_max_qty.map_or(max_qty, |market| market.min(max_qty));
                parsed.insert(symbol, (tick_size, step_size, final_max_qty, min_notional));
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

    pub fn spot_user_stream_subscription_request(&self, request_id: &str) -> serde_json::Value {
        let timestamp = self.current_timestamp();
        let recv_window = 5_000_u64;
        // WebSocket API signatures sort all params (except signature) by name.
        let payload = format!(
            "apiKey={}&recvWindow={}&timestamp={}",
            self.spot_api_key, recv_window, timestamp
        );
        let signature = self.sign(&self.spot_secret_key, &payload);
        serde_json::json!({
            "id": request_id,
            "method": "userDataStream.subscribe.signature",
            "params": {
                "apiKey": self.spot_api_key,
                "recvWindow": recv_window,
                "timestamp": timestamp,
                "signature": signature,
            }
        })
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
        self.get_open_orders_for_venue(LegVenue::UsdtFutures).await
    }

    pub async fn get_open_orders_for_venue(&self, venue: LegVenue) -> Result<String, String> {
        let (base_url, endpoint, label) = match venue {
            LegVenue::Spot => (
                &self.spot_base_url,
                "/api/v3/openOrders",
                "spot open orders",
            ),
            LegVenue::UsdtFutures => (
                &self.fut_base_url,
                "/fapi/v1/openOrders",
                "futures open orders",
            ),
        };
        let req = self.build_signed_request_with_base(Method::GET, base_url, endpoint, vec![]);
        self.send_checked_text(req, label).await
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

    pub async fn get_fapi_position_risk(&self) -> Result<String, String> {
        let req = self.build_signed_request_with_base(
            Method::GET,
            &self.fut_base_url,
            "/fapi/v2/positionRisk",
            vec![],
        );
        self.send_checked_text(req, "futures position risk").await
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
            ("type", "LIMIT_MAKER".to_string()),
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
        reduce_only: bool,
    ) -> Result<String, String> {
        if self.trading_mode == "paper" {
            tokio::time::sleep(tokio::time::Duration::from_millis(50)).await;
            return Ok("{\"orderId\":999997,\"status\":\"NEW\"}".to_string());
        }

        let params = Self::futures_limit_order_params(
            symbol,
            side,
            quantity,
            price,
            client_order_id,
            reduce_only,
        );

        let req = self.build_signed_request_with_base(
            Method::POST,
            &self.fut_base_url,
            "/fapi/v1/order",
            params,
        );
        self.send_checked_text(req, "futures limit order").await
    }

    fn futures_limit_order_params(
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Vec<(&'static str, String)> {
        let mut params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "LIMIT".to_string()),
            ("timeInForce", "GTX".to_string()),
            ("quantity", quantity.to_string()),
            ("price", price.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];
        // Futures orders in this engine use one-way mode semantics (there is no
        // positionSide parameter). Close orders must be reduce-only so a stale
        // quantity or position race cannot increase or reverse the exposure.
        if reduce_only {
            params.push(("reduceOnly", "true".to_string()));
        }
        params
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

    pub async fn get_order_by_id(
        &self,
        venue: LegVenue,
        symbol: &str,
        order_id: i64,
    ) -> Result<String, String> {
        let params = vec![
            ("symbol", symbol.to_uppercase()),
            ("orderId", order_id.to_string()),
        ];
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
        self.send_checked_text(req, "query order by exchange id")
            .await
    }

    fn is_authoritative_order_not_found(error: &str) -> bool {
        error.contains("exchange error -2013") || error.contains("Order does not exist")
    }

    async fn probe_order_after_ambiguous_submit(
        &self,
        venue: LegVenue,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<Option<String>, String> {
        for attempt in 0..2 {
            match self
                .get_order_by_client_id(venue, symbol, client_order_id)
                .await
            {
                Ok(body) => return Ok(Some(body)),
                Err(err) if Self::is_authoritative_order_not_found(&err) => {
                    if attempt == 0 {
                        tokio::time::sleep(Duration::from_millis(250)).await;
                    }
                }
                Err(_) => {
                    return Err(
                        "ambiguous submission and authoritative order lookup unavailable"
                            .to_string(),
                    );
                }
            }
        }
        Ok(None)
    }

    /// Submit a deterministic spot limit order. Any ambiguous POST outcome is
    /// queried twice before one same-ID retry is permitted. A duplicate error
    /// on that retry is queried again, so the caller never creates a second
    /// exchange identity.
    pub async fn place_spot_limit_order_read_before_retry(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
    ) -> Result<ReconciledSubmission, String> {
        match self
            .place_spot_limit_order(symbol, side, quantity, price, client_order_id)
            .await
        {
            Ok(body) => {
                return Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: false,
                    retried_after_negative_proof: false,
                });
            }
            Err(_) => {}
        }
        if let Some(body) = self
            .probe_order_after_ambiguous_submit(LegVenue::Spot, symbol, client_order_id)
            .await?
        {
            return Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: false,
            });
        }

        match self
            .place_spot_limit_order(symbol, side, quantity, price, client_order_id)
            .await
        {
            Ok(body) => Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: true,
            }),
            Err(_) => {
                let body = self
                    .get_order_by_client_id(LegVenue::Spot, symbol, client_order_id)
                    .await
                    .map_err(|_| {
                        "ambiguous spot submission remained unresolved after same-ID retry"
                            .to_string()
                    })?;
                Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: true,
                    retried_after_negative_proof: true,
                })
            }
        }
    }

    pub async fn place_futures_limit_order_read_before_retry(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<ReconciledSubmission, String> {
        match self
            .place_futures_limit_order(symbol, side, quantity, price, client_order_id, reduce_only)
            .await
        {
            Ok(body) => {
                return Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: false,
                    retried_after_negative_proof: false,
                });
            }
            Err(_) => {}
        }
        if let Some(body) = self
            .probe_order_after_ambiguous_submit(LegVenue::UsdtFutures, symbol, client_order_id)
            .await?
        {
            return Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: false,
            });
        }

        match self
            .place_futures_limit_order(symbol, side, quantity, price, client_order_id, reduce_only)
            .await
        {
            Ok(body) => Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: true,
            }),
            Err(_) => {
                let body = self
                    .get_order_by_client_id(LegVenue::UsdtFutures, symbol, client_order_id)
                    .await
                    .map_err(|_| {
                        "ambiguous futures submission remained unresolved after same-ID retry"
                            .to_string()
                    })?;
                Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: true,
                    retried_after_negative_proof: true,
                })
            }
        }
    }

    pub async fn place_spot_market_order_read_before_retry(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
    ) -> Result<ReconciledSubmission, String> {
        match self
            .place_spot_market_order(symbol, side, quantity, client_order_id)
            .await
        {
            Ok(body) => {
                return Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: false,
                    retried_after_negative_proof: false,
                });
            }
            Err(_) => {}
        }
        if let Some(body) = self
            .probe_order_after_ambiguous_submit(LegVenue::Spot, symbol, client_order_id)
            .await?
        {
            return Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: false,
            });
        }
        match self
            .place_spot_market_order(symbol, side, quantity, client_order_id)
            .await
        {
            Ok(body) => Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: true,
            }),
            Err(_) => {
                let body = self
                    .get_order_by_client_id(LegVenue::Spot, symbol, client_order_id)
                    .await
                    .map_err(|_| {
                        "ambiguous spot market submission remained unresolved after same-ID retry"
                            .to_string()
                    })?;
                Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: true,
                    retried_after_negative_proof: true,
                })
            }
        }
    }

    pub async fn place_futures_market_order_read_before_retry(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
        reduce_only: bool,
    ) -> Result<ReconciledSubmission, String> {
        match self
            .place_futures_market_order(symbol, side, quantity, client_order_id, reduce_only)
            .await
        {
            Ok(body) => {
                return Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: false,
                    retried_after_negative_proof: false,
                });
            }
            Err(_) => {}
        }
        if let Some(body) = self
            .probe_order_after_ambiguous_submit(LegVenue::UsdtFutures, symbol, client_order_id)
            .await?
        {
            return Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: false,
            });
        }
        match self
            .place_futures_market_order(symbol, side, quantity, client_order_id, reduce_only)
            .await
        {
            Ok(body) => Ok(ReconciledSubmission {
                body,
                recovered_after_ambiguous_submit: true,
                retried_after_negative_proof: true,
            }),
            Err(_) => {
                let body = self
                    .get_order_by_client_id(
                        LegVenue::UsdtFutures,
                        symbol,
                        client_order_id,
                    )
                    .await
                    .map_err(|_| {
                        "ambiguous futures market submission remained unresolved after same-ID retry"
                            .to_string()
                    })?;
                Ok(ReconciledSubmission {
                    body,
                    recovered_after_ambiguous_submit: true,
                    retried_after_negative_proof: true,
                })
            }
        }
    }

    /// Return one bounded, authoritative order-history page for private-stream
    /// recovery.  Callers must fail closed when Binance returns `limit` rows:
    /// silently accepting a truncated page could lose a fill during a stream
    /// outage.
    pub async fn get_order_history(
        &self,
        venue: LegVenue,
        symbol: &str,
        start_time_ms: i64,
        end_time_ms: i64,
        limit: u16,
    ) -> Result<String, String> {
        let params = vec![
            ("symbol", symbol.to_uppercase()),
            ("startTime", start_time_ms.max(0).to_string()),
            ("endTime", end_time_ms.max(start_time_ms).to_string()),
            ("limit", limit.clamp(1, 1000).to_string()),
        ];
        let req = match venue {
            LegVenue::Spot => self.build_signed_request_with_base(
                Method::GET,
                &self.spot_base_url,
                "/api/v3/allOrders",
                params,
            ),
            LegVenue::UsdtFutures => self.build_signed_request_with_base(
                Method::GET,
                &self.fut_base_url,
                "/fapi/v1/allOrders",
                params,
            ),
        };
        self.send_checked_text(req, "private-stream order-history backfill")
            .await
    }

    /// Return one bounded, authoritative trade-history page for private-stream
    /// recovery.  Both Binance APIs require a symbol, so the stream manager
    /// calls this for every monitored symbol before restoring readiness.
    pub async fn get_user_trade_history(
        &self,
        venue: LegVenue,
        symbol: &str,
        start_time_ms: i64,
        end_time_ms: i64,
        limit: u16,
    ) -> Result<String, String> {
        let params = vec![
            ("symbol", symbol.to_uppercase()),
            ("startTime", start_time_ms.max(0).to_string()),
            ("endTime", end_time_ms.max(start_time_ms).to_string()),
            ("limit", limit.clamp(1, 1000).to_string()),
        ];
        let req = match venue {
            LegVenue::Spot => self.build_signed_request_with_base(
                Method::GET,
                &self.spot_base_url,
                "/api/v3/myTrades",
                params,
            ),
            LegVenue::UsdtFutures => self.build_signed_request_with_base(
                Method::GET,
                &self.fut_base_url,
                "/fapi/v1/userTrades",
                params,
            ),
        };
        self.send_checked_text(req, "private-stream trade-history backfill")
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
                last_err = BinanceRest::safe_transport_error(&e).to_string();
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

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering as AtomicOrdering};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpListener;

    fn param_value<'a>(params: &'a [(&str, String)], key: &str) -> Option<&'a str> {
        params
            .iter()
            .find_map(|(name, value)| (*name == key).then_some(value.as_str()))
    }

    #[test]
    fn futures_limit_exit_params_are_reduce_only() {
        let params = BinanceRest::futures_limit_order_params(
            "BTCUSDT",
            TradeSide::Buy,
            "0.01",
            "60000.0",
            "fut-exit-1",
            true,
        );

        assert_eq!(param_value(&params, "reduceOnly"), Some("true"));
        assert_eq!(param_value(&params, "timeInForce"), Some("GTX"));
    }

    #[test]
    fn futures_limit_entry_params_are_not_reduce_only() {
        let params = BinanceRest::futures_limit_order_params(
            "BTCUSDT",
            TradeSide::Sell,
            "0.01",
            "60000.0",
            "fut-entry-1",
            false,
        );

        assert_eq!(param_value(&params, "reduceOnly"), None);
    }

    #[test]
    fn exchange_filters_fail_closed_on_missing_fields_or_non_trading_status() {
        let payload = serde_json::json!({
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "maxQty": "100"},
                        {"filterType": "MARKET_LOT_SIZE", "maxQty": "50"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"}
                    ]
                },
                {
                    "symbol": "MISSINGUSDT",
                    "status": "TRADING",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "maxQty": "100"}
                    ]
                },
                {
                    "symbol": "HALTEDUSDT",
                    "status": "BREAK",
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001", "maxQty": "100"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"}
                    ]
                }
            ]
        });

        let filters = BinanceRest::parse_symbol_filters(&payload);
        assert_eq!(filters.get("BTCUSDT"), Some(&(0.1, 0.001, 50.0, 5.0)));
        assert!(!filters.contains_key("MISSINGUSDT"));
        assert!(!filters.contains_key("HALTEDUSDT"));
    }

    #[tokio::test]
    async fn accepted_rest_timeout_is_read_before_retry_with_one_exchange_effect() {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let post_count = Arc::new(AtomicUsize::new(0));
        let get_count = Arc::new(AtomicUsize::new(0));
        let post_count_server = post_count.clone();
        let get_count_server = get_count.clone();
        let server = tokio::spawn(async move {
            loop {
                let Ok((mut socket, _)) = listener.accept().await else {
                    break;
                };
                let posts = post_count_server.clone();
                let gets = get_count_server.clone();
                tokio::spawn(async move {
                    let mut request = vec![0u8; 8192];
                    let Ok(read) = socket.read(&mut request).await else {
                        return;
                    };
                    let request = String::from_utf8_lossy(&request[..read]);
                    let is_post = request.starts_with("POST ");
                    let body = if is_post {
                        posts.fetch_add(1, AtomicOrdering::SeqCst);
                        // Model an exchange that committed the deterministic
                        // order but whose HTTP response arrives after the
                        // client's bounded timeout.
                        tokio::time::sleep(Duration::from_millis(200)).await;
                        r#"{"symbol":"BTCUSDT","orderId":77,"clientOrderId":"bngs_timeout_1","status":"NEW","executedQty":"0"}"#
                    } else {
                        gets.fetch_add(1, AtomicOrdering::SeqCst);
                        r#"{"symbol":"BTCUSDT","orderId":77,"clientOrderId":"bngs_timeout_1","status":"NEW","executedQty":"0"}"#
                    };
                    let response = format!(
                        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                        body.len(),
                        body
                    );
                    let _ = socket.write_all(response.as_bytes()).await;
                });
            }
        });

        let mut rest = BinanceRest::new(
            "test-key".to_string(),
            "test-secret".to_string(),
            "live".to_string(),
        );
        rest.spot_base_url = format!("http://{address}");
        rest.client = Client::builder()
            .connect_timeout(Duration::from_millis(50))
            .timeout(Duration::from_millis(75))
            .build()
            .unwrap();

        let receipt = rest
            .place_spot_limit_order_read_before_retry(
                "BTCUSDT",
                TradeSide::Buy,
                "0.01",
                "60000",
                "bngs_timeout_1",
            )
            .await
            .expect("authoritative GET must recover the accepted timeout");
        assert!(receipt.recovered_after_ambiguous_submit);
        assert!(!receipt.retried_after_negative_proof);
        assert_eq!(post_count.load(AtomicOrdering::SeqCst), 1);
        assert_eq!(get_count.load(AtomicOrdering::SeqCst), 1);
        server.abort();
    }
}
