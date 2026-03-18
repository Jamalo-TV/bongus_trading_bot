use hmac::{Hmac, Mac};
use reqwest::{Client, Method, RequestBuilder};
use sha2::Sha256;
use std::time::{SystemTime, UNIX_EPOCH};

type HmacSha256 = Hmac<Sha256>;

pub struct BinanceRest {
    client: Client,
    api_key: String,
    secret_key: String,
    base_url: String,
}

#[derive(Debug, Clone)]
pub struct ExchangeSymbolInfo {
    pub symbol: String,
    pub tick_size: f64,
    pub step_size: f64,
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
    pub fn new(api_key: String, secret_key: String) -> Self {
        Self {
            client: Client::new(),
            api_key,
            secret_key,
            base_url: "https://api.binance.com".to_string(), // Can be parameterised
        }
    }

    pub async fn get_exchange_info(&self) -> Result<std::collections::HashMap<String, ExchangeSymbolInfo>, String> {
        let url = "https://fapi.binance.com/fapi/v1/exchangeInfo";
        let resp_result = self.client.get(url).send().await;
        let resp = match resp_result {
            Ok(r) => r,
            Err(e) => return Err(format!("Failed to fetch exchange info: {}", e)),
        };
        let text_result = resp.text().await;
        let text = match text_result {
            Ok(t) => t,
            Err(e) => return Err(format!("Failed to read exchange info text: {}", e)),
        };
        let json: serde_json::Value = match serde_json::from_str(&text) {
            Ok(j) => j,
            Err(e) => return Err(format!("Failed to parse exchange info JSON: {}", e)),
        };

        let mut info_map = std::collections::HashMap::new();
        if let Some(symbols) = json.get("symbols").and_then(|s| s.as_array()) {
            for sym in symbols {
                let symbol = sym.get("symbol").and_then(|s| s.as_str()).unwrap_or("").to_string();
                let mut tick_size = 0.1;
                let mut step_size = 0.1;

                if let Some(filters) = sym.get("filters").and_then(|f| f.as_array()) {
                    for filter in filters {
                        if let Some(filter_type) = filter.get("filterType").and_then(|t| t.as_str()) {
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
                info_map.insert(symbol.clone(), ExchangeSymbolInfo {
                    symbol,
                    tick_size,
                    step_size,
                });
            }
        }
        
        Ok(info_map)
    }

    fn current_timestamp() -> u64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("Time went backwards")
            .as_millis() as u64
    }

    fn sign(&self, query_string: &str) -> String {
        let mut mac = HmacSha256::new_from_slice(self.secret_key.as_bytes())
            .expect("HMAC can take key of any size");
        mac.update(query_string.as_bytes());
        let result = mac.finalize();
        hex::encode(result.into_bytes())
    }

    pub fn build_signed_request(
        &self,
        method: Method,
        endpoint: &str,
        mut params: Vec<(&str, String)>,
    ) -> RequestBuilder {
        params.push(("timestamp", Self::current_timestamp().to_string()));
        // Note: In real production, encode URI appropriately. This is simplified.
        let query_string = params
            .iter()
            .map(|(k, v)| format!("{}={}", k, v))
            .collect::<Vec<String>>()
            .join("&");

        let signature = self.sign(&query_string);
        let final_query = format!("{}&signature={}", query_string, signature);
        let url = format!("{}{}?{}", self.base_url, endpoint, final_query);

        self.client
            .request(method, &url)
            .header("X-MBX-APIKEY", &self.api_key)
    }

    fn build_signed_request_with_base(
        &self,
        method: Method,
        base_url: &str,
        endpoint: &str,
        mut params: Vec<(&str, String)>,
    ) -> RequestBuilder {
        params.push(("timestamp", Self::current_timestamp().to_string()));

        let query_string = params
            .iter()
            .map(|(k, v)| format!("{}={}", k, v))
            .collect::<Vec<String>>()
            .join("&");

        let signature = self.sign(&query_string);
        let final_query = format!("{}&signature={}", query_string, signature);
        let url = format!("{}{}?{}", base_url, endpoint, final_query);

        self.client
            .request(method, &url)
            .header("X-MBX-APIKEY", &self.api_key)
    }

    pub async fn get_open_orders(&self) -> Result<String, reqwest::Error> {
        let req = self.build_signed_request(Method::GET, "/api/v3/openOrders", vec![]);
        req.send().await?.text().await
    }

    pub async fn get_account(&self) -> Result<String, reqwest::Error> {
        let req = self.build_signed_request(Method::GET, "/api/v3/account", vec![]);
        req.send().await?.text().await
    }

    pub async fn cancel_order(&self, symbol: &str, order_id: &str) -> Result<String, reqwest::Error> {
        let params = vec![
            ("symbol", symbol.to_string()),
            ("origClientOrderId", order_id.to_string()),
        ];
        let req = self.build_signed_request(Method::DELETE, "/api/v3/order", params);
        req.send().await?.text().await
    }

    pub async fn place_spot_limit_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
    ) -> Result<String, reqwest::Error> {
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
            "https://api.binance.com",
            "/api/v3/order",
            params,
        );
        req.send().await?.text().await
    }

    pub async fn place_futures_limit_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        price: &str,
        client_order_id: &str,
    ) -> Result<String, reqwest::Error> {
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
            "https://fapi.binance.com",
            "/fapi/v1/order",
            params,
        );
        req.send().await?.text().await
    }

    pub async fn place_futures_market_order(
        &self,
        symbol: &str,
        side: TradeSide,
        quantity: &str,
        client_order_id: &str,
    ) -> Result<String, reqwest::Error> {
        let params = vec![
            ("symbol", symbol.to_string()),
            ("side", side.as_str().to_string()),
            ("type", "MARKET".to_string()),
            ("quantity", quantity.to_string()),
            ("newClientOrderId", client_order_id.to_string()),
        ];

        let req = self.build_signed_request_with_base(
            Method::POST,
            "https://fapi.binance.com",
            "/fapi/v1/order",
            params,
        );
        req.send().await?.text().await
    }

    pub async fn get_order_by_client_id(
        &self,
        venue: LegVenue,
        symbol: &str,
        client_order_id: &str,
    ) -> Result<String, reqwest::Error> {
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
                "https://api.binance.com",
                "/api/v3/order",
                params,
            ),
            LegVenue::UsdtFutures => self.build_signed_request_with_base(
                Method::GET,
                "https://fapi.binance.com",
                "/fapi/v1/order",
                params,
            ),
        };

        req.send().await?.text().await
    }

    pub async fn create_listen_key(&self) -> Result<String, reqwest::Error> {
        let url = "https://fapi.binance.com/fapi/v1/listenKey".to_string();
        let req = self.client.post(&url).header("X-MBX-APIKEY", &self.api_key);
        req.send().await?.text().await
    }

    pub async fn keepalive_listen_key(&self, listen_key: &str) -> Result<String, reqwest::Error> {
        let url = format!("https://fapi.binance.com/fapi/v1/listenKey?listenKey={}", listen_key);
        let req = self.client.put(&url).header("X-MBX-APIKEY", &self.api_key);
        req.send().await?.text().await
    }

    pub async fn close_listen_key(&self, listen_key: &str) -> Result<String, reqwest::Error> {
        let url = format!("https://fapi.binance.com/fapi/v1/listenKey?listenKey={}", listen_key);
        let req = self.client.delete(&url).header("X-MBX-APIKEY", &self.api_key);
        req.send().await?.text().await
    }
}
