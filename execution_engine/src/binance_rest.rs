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
        let url = format!("{}/api/v3/userDataStream", self.base_url);
        let req = self.client.post(&url).header("X-MBX-APIKEY", &self.api_key);
        req.send().await?.text().await
    }

    pub async fn keepalive_listen_key(&self, listen_key: &str) -> Result<String, reqwest::Error> {
        let url = format!("{}/api/v3/userDataStream?listenKey={}", self.base_url, listen_key);
        let req = self.client.put(&url).header("X-MBX-APIKEY", &self.api_key);
        req.send().await?.text().await
    }

    pub async fn close_listen_key(&self, listen_key: &str) -> Result<String, reqwest::Error> {
        let url = format!("{}/api/v3/userDataStream?listenKey={}", self.base_url, listen_key);
        let req = self.client.delete(&url).header("X-MBX-APIKEY", &self.api_key);
        req.send().await?.text().await
    }
}
