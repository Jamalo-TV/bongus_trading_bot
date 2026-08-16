use serde::Deserialize;
use std::collections::HashMap;

const ENDPOINT_MATRIX_JSON: &str = include_str!("../../config/binance_endpoints_v1.json");

#[derive(Debug, Clone, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct VenueEndpoints {
    pub rest_base_url: String,
    pub public_stream_ws_base_url: String,
    pub market_stream_ws_base_url: String,
    pub private_ws_base_url: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct EnvironmentEndpoints {
    futures: VenueEndpoints,
    spot: VenueEndpoints,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
struct EndpointMatrix {
    schema_version: u32,
    planned_connection_max_age_seconds: u64,
    environments: HashMap<String, EnvironmentEndpoints>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SelectedEndpoints {
    pub futures: VenueEndpoints,
    pub spot: VenueEndpoints,
    pub planned_connection_max_age_seconds: u64,
}

fn load_matrix() -> Result<EndpointMatrix, String> {
    let matrix: EndpointMatrix = serde_json::from_str(ENDPOINT_MATRIX_JSON)
        .map_err(|error| format!("parse shared Binance endpoint matrix: {error}"))?;
    if matrix.schema_version != 1 {
        return Err(format!(
            "unsupported Binance endpoint schema {}",
            matrix.schema_version
        ));
    }
    if matrix.planned_connection_max_age_seconds == 0
        || matrix.planned_connection_max_age_seconds >= 24 * 60 * 60
    {
        return Err("Binance connection renewal must be planned before 24 hours".to_string());
    }
    if matrix.environments.len() != 2
        || !matrix.environments.contains_key("mainnet")
        || !matrix.environments.contains_key("testnet")
    {
        return Err("Binance endpoint matrix must define mainnet and testnet".to_string());
    }
    for (environment, endpoints) in &matrix.environments {
        for (venue, values) in [("futures", &endpoints.futures), ("spot", &endpoints.spot)] {
            if !values.rest_base_url.starts_with("https://")
                || !values.public_stream_ws_base_url.starts_with("wss://")
                || !values.market_stream_ws_base_url.starts_with("wss://")
                || !values.private_ws_base_url.starts_with("wss://")
            {
                return Err(format!(
                    "invalid secure Binance endpoint for {environment}/{venue}"
                ));
            }
        }
    }
    Ok(matrix)
}

pub fn endpoints_for_mode(trading_mode: &str) -> Result<SelectedEndpoints, String> {
    let matrix = load_matrix()?;
    let environment = if trading_mode.eq_ignore_ascii_case("testnet") {
        "testnet"
    } else {
        "mainnet"
    };
    let endpoints = matrix
        .environments
        .get(environment)
        .ok_or_else(|| format!("missing Binance endpoint environment {environment}"))?;
    Ok(SelectedEndpoints {
        futures: endpoints.futures.clone(),
        spot: endpoints.spot.clone(),
        planned_connection_max_age_seconds: matrix.planned_connection_max_age_seconds,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_matrix_uses_official_testnet_endpoints_and_planned_renewal() {
        let endpoints = endpoints_for_mode("testnet").unwrap();
        assert_eq!(
            endpoints.futures.rest_base_url,
            "https://demo-fapi.binance.com"
        );
        assert_eq!(
            endpoints.futures.public_stream_ws_base_url,
            "wss://demo-fstream.binance.com/public"
        );
        assert_eq!(
            endpoints.futures.market_stream_ws_base_url,
            "wss://demo-fstream.binance.com/market"
        );
        assert_eq!(
            endpoints.spot.rest_base_url,
            "https://testnet.binance.vision"
        );
        assert_eq!(
            endpoints.spot.private_ws_base_url,
            "wss://ws-api.testnet.binance.vision/ws-api/v3"
        );
        assert_eq!(endpoints.planned_connection_max_age_seconds, 23 * 60 * 60);
    }

    #[test]
    fn paper_uses_mainnet_market_data_matrix() {
        assert_eq!(
            endpoints_for_mode("paper")
                .unwrap()
                .futures
                .market_stream_ws_base_url,
            "wss://fstream.binance.com/market"
        );
    }
}
