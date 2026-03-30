mod binance_rest;
mod binance_ws;
mod collateral_engine;
mod order_manager;
mod user_data_ws;
mod ipc;

use binance_ws::WsConnectionManager;
use user_data_ws::{UserDataStreamKind, UserDataWsManager};
use binance_rest::BinanceRest;
use order_manager::{OrderManager, EngineEvent, MarketType};
use tokio::sync::mpsc;
use tokio::sync::broadcast;
use tracing_subscriber::FmtSubscriber;
use tokio::net::TcpListener;
use tokio::io::AsyncWriteExt;
use std::time::Duration;

#[tokio::main]
async fn main() {
    dotenvy::dotenv().ok();
    
    let subscriber = FmtSubscriber::builder()
        .with_max_level(tracing::Level::INFO)
        .finish();
    tracing::subscriber::set_global_default(subscriber)
        .expect("setting default subscriber failed");

    tracing::info!("Starting Binance Execution Engine (Rust)...");

    // Channels for primary execution
    let (engine_tx, engine_rx) = mpsc::channel(10000);
    
    // Bridge WS Events -> Engine Events
    let (ws_tx, mut ws_rx) = mpsc::channel(10000);
    let engine_tx_for_ws = engine_tx.clone();
    tokio::spawn(async move {
        while let Some(evt) = ws_rx.recv().await {
            let _ = engine_tx_for_ws.send(EngineEvent::Ws(evt)).await;
        }
    });

    // Bridge Alpha IPC -> Engine Events
    let (alpha_tx, mut alpha_rx) = mpsc::channel(10000);
    let engine_tx_for_alpha = engine_tx.clone();
    tokio::spawn(async move {
        while let Some(evt) = alpha_rx.recv().await {
            let _ = engine_tx_for_alpha.send(EngineEvent::Alpha(evt)).await;
        }
    });

    // Broadcast channel for Python Dashboard IPC
    let (dash_tx, _) = broadcast::channel(10000);

    let api_key = std::env::var("BINANCE_API_KEY")
        .unwrap_or_else(|_| "DUMMY_API_KEY".to_string())
        .trim()
        .to_string();
    let secret_key = std::env::var("BINANCE_API_SECRET")
        .unwrap_or_else(|_| "DUMMY_SECRET_KEY".to_string())
        .trim()
        .to_string();

    let trading_mode = std::env::var("TRADING_MODE")
        .unwrap_or_else(|_| "paper".to_string())
        .to_lowercase();
    let trading_mode = match trading_mode.as_str() {
        "live" | "testnet" | "paper" => trading_mode,
        _ => {
            tracing::warn!("Unknown TRADING_MODE '{}', defaulting to 'paper'", trading_mode);
            "paper".to_string()
        }
    };
    tracing::info!("TRADING_MODE = {}", trading_mode);

    let mut order_manager = OrderManager::new(
        engine_rx,
        engine_tx,
        api_key.clone(),
        secret_key.clone(),
        dash_tx.clone(),
        trading_mode.clone(),
    );

    // Spawn Order Manager
    tokio::spawn(async move {
        order_manager.run().await;
    });

    // Spawn ZeroMQ IPC Server using TCP for cross-platform compatibility
    let zmq_endpoint = "tcp://127.0.0.1:5555";
    let mut ipc_server = ipc::IpcServer::new(zmq_endpoint, alpha_tx);
    tokio::spawn(async move {
        ipc_server.run().await;
    });

    // Spawn private User Data WebSocket Managers (skip in paper mode — no real API key needed)
    if trading_mode != "paper" {
        let futures_user_data_rest_client = BinanceRest::new(
            api_key.clone(),
            secret_key.clone(),
            trading_mode.clone(),
        );
        let spot_user_data_rest_client = BinanceRest::new(
            api_key.clone(),
            secret_key.clone(),
            trading_mode.clone(),
        );
        let fut_ud_tx = ws_tx.clone();
        tokio::spawn(async move {
            let mut ud_ws_manager = UserDataWsManager::new(
                futures_user_data_rest_client,
                fut_ud_tx,
                UserDataStreamKind::Futures,
            );
            ud_ws_manager.run().await;
        });

        let spot_ud_tx = ws_tx.clone();
        tokio::spawn(async move {
            let mut ud_ws_manager = UserDataWsManager::new(
                spot_user_data_rest_client,
                spot_ud_tx,
                UserDataStreamKind::Spot,
            );
            ud_ws_manager.run().await;
        });
    }

    // Read monitored symbols from env — must match Python's MONITORED_SYMBOLS
    let symbols_env = std::env::var("MONITORED_SYMBOLS")
        .unwrap_or_else(|_| "BTCUSDT,ETHUSDT,SOLUSDT,DOGEUSDT,PEPEUSDT,BNBUSDT,ARBUSDT,SUIUSDT".to_string());
    let monitored_symbols: Vec<String> = symbols_env
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();

    tracing::info!("Monitoring {} symbols: {:?}", monitored_symbols.len(), monitored_symbols);

    // Only use testnet WS endpoints when TRADING_MODE=testnet.
    // Paper mode uses mainnet WS for real market data (but places no real orders).
    let use_testnet = trading_mode == "testnet";
    let binance_ws_url = if use_testnet {
        "wss://stream.binancefuture.com/ws"
    } else {
        "wss://fstream.binance.com/ws"
    };
    let default_spot_ws_url = if use_testnet {
        "wss://testnet.binance.vision/ws".to_string()
    } else {
        "wss://stream.binance.com:9443/ws".to_string()
    };
    let spot_ws_url = std::env::var("BINANCE_SPOT_WS_URL")
        .unwrap_or(default_spot_ws_url);

    // Spawn perp + spot WsConnectionManager for each symbol
    for symbol in &monitored_symbols {
        // Perp: markPrice + bookTicker + depth5@100ms
        let sym = symbol.clone();
        let tx_clone = ws_tx.clone();
        let perp_url = binance_ws_url.to_string();
        tokio::spawn(async move {
            let mut ws_manager = WsConnectionManager::new(&perp_url, &sym, tx_clone, MarketType::Perp);
            ws_manager.run().await;
        });
        tokio::time::sleep(Duration::from_millis(50)).await;

        // Spot: depth5@100ms only
        let sym = symbol.clone();
        let tx_clone = ws_tx.clone();
        let s_url = spot_ws_url.clone();
        tokio::spawn(async move {
            let mut ws_manager = WsConnectionManager::new(&s_url, &sym, tx_clone, MarketType::Spot);
            ws_manager.run().await;
        });
        tokio::time::sleep(Duration::from_millis(50)).await;
    }

    // Spawn IPC Server
    let dash_tx_ipc = dash_tx.clone();
    tokio::spawn(async move {
        let listener = TcpListener::bind("127.0.0.1:9000").await.unwrap();
        tracing::info!("Dashboard IPC Server listening on 127.0.0.1:9000");

        while let Ok((mut socket, _)) = listener.accept().await {
            let mut rx = dash_tx_ipc.subscribe();
            tokio::spawn(async move {
                loop {
                    match rx.recv().await {
                        Ok(msg) => {
                            if socket.write_all(format!("{}\n", msg).as_bytes()).await.is_err() {
                                tracing::warn!("Dashboard IPC client disconnected, closing socket task.");
                                break;
                            }
                        }
                        Err(broadcast::error::RecvError::Lagged(skipped)) => {
                            tracing::warn!("Dashboard IPC client lagged, skipped {} messages", skipped);
                            continue;
                        }
                        Err(broadcast::error::RecvError::Closed) => {
                            tracing::info!("Dashboard IPC channel closed");
                            break;
                        }
                    }
                }
            });
        }
    });

    // Keep main thread alive
    tokio::signal::ctrl_c().await.unwrap();
    tracing::info!("Shutting down engine.");
}
