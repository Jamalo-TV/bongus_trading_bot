mod binance_rest;
mod binance_ws;
mod collateral_engine;
mod ipc;
mod order_manager;
mod ranking;
mod strategy;
mod user_data_ws;

use binance_rest::BinanceRest;
use binance_ws::WsConnectionManager;
use order_manager::{EngineEvent, MarketType, OrderManager, WsEvent};
use std::collections::HashSet;
use std::path::PathBuf;
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncWrite, AsyncWriteExt};
use tokio::net::TcpListener;
use tokio::sync::broadcast;
use tokio::sync::mpsc;
use tracing::info;
use tracing_subscriber::FmtSubscriber;
use user_data_ws::{PrivateStreamControl, UserDataStreamKind, UserDataWsManager};

fn current_time_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| i64::try_from(duration.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or_default()
}

async fn handle_telemetry_lag<W: AsyncWrite + Unpin>(
    socket: &mut W,
    skipped: u64,
    ws_sender: &mpsc::Sender<WsEvent>,
    futures_control: &mpsc::Sender<PrivateStreamControl>,
    spot_control: &mpsc::Sender<PrivateStreamControl>,
) {
    let gap = WsEvent::TelemetryGap {
        skipped_messages: skipped,
        reason: "broadcast_receiver_overflow".to_string(),
        event_time_ms: current_time_ms(),
    };
    // Deliver directly because putting the marker only back onto an already
    // lagged broadcast cannot prove that this client observed the loss.
    if let Ok(payload) = rmp_serde::to_vec_named(&gap) {
        let _ = socket.write_all(&payload).await;
    }
    let _ = ws_sender.send(gap).await;
    let replay = PrivateStreamControl::ReplayFromCursor {
        reason: "telemetry receiver overflow".to_string(),
    };
    let _ = futures_control.try_send(replay.clone());
    let _ = spot_control.try_send(replay);
}

/// Local-only stream campaign boundary. It uses the production msgpack/TCP
/// framing and deliberately closes four independent connections so the Python
/// subscriber must reconnect while consuming market, private-stream replay,
/// listen-key-expiry, and telemetry-overflow markers.
async fn run_stream_recovery_harness(bind: &str) -> Result<(), String> {
    let listener = TcpListener::bind(bind)
        .await
        .map_err(|error| format!("bind_stream_harness:{error}"))?;
    let batches = vec![
        vec![serde_json::json!({
            "event": "L2Depth",
            "symbol": "BTCUSDT",
            "market": "perp",
            "bids": [[60000.0, 1.0]],
            "asks": [[60001.0, 1.0]],
            "first_update_id": 1,
            "final_update_id": 1,
            "previous_final_update_id": 0,
            "sequence_contiguous": true,
            "diagnostic_connection": 1,
        })],
        vec![serde_json::json!({
            "event": "PrivateStreamStatus",
            "stream_kind": "futures",
            "status": "GAP",
            "cursor": 100,
            "reason": "private_stream_disconnect",
            "diagnostic_connection": 2,
        })],
        vec![
            serde_json::json!({
                "event": "PrivateStreamStatus",
                "stream_kind": "futures",
                "status": "BACKFILLED",
                "cursor": 101,
                "reason": "durable_cursor_replay_complete",
                "diagnostic_connection": 3,
            }),
            serde_json::json!({
                "event": "PrivateStreamStatus",
                "stream_kind": "spot",
                "status": "GAP",
                "cursor": 200,
                "reason": "listen_key_expired",
                "diagnostic_connection": 3,
            }),
        ],
        vec![
            serde_json::json!({
                "event": "PrivateStreamStatus",
                "stream_kind": "spot",
                "status": "BACKFILLED",
                "cursor": 201,
                "reason": "durable_cursor_replay_complete",
                "diagnostic_connection": 4,
            }),
            serde_json::json!({
                "event": "TelemetryGap",
                "skipped_messages": 37,
                "reason": "broadcast_receiver_overflow",
                "event_time_ms": current_time_ms(),
                "diagnostic_connection": 4,
            }),
        ],
    ];

    for batch in batches {
        let (mut socket, _) = listener
            .accept()
            .await
            .map_err(|error| format!("accept_stream_harness:{error}"))?;
        for event in batch {
            let payload = rmp_serde::to_vec_named(&event)
                .map_err(|error| format!("encode_stream_harness:{error}"))?;
            socket
                .write_all(&payload)
                .await
                .map_err(|error| format!("write_stream_harness:{error}"))?;
        }
        socket
            .shutdown()
            .await
            .map_err(|error| format!("shutdown_stream_harness:{error}"))?;
    }
    Ok(())
}

fn resolve_shared_api_credential(
    primary_name: &str,
    fallback_name: &str,
    default_value: &str,
) -> String {
    let primary = std::env::var(primary_name)
        .unwrap_or_default()
        .trim()
        .to_string();
    if !primary.is_empty() {
        return primary;
    }

    let fallback = std::env::var(fallback_name)
        .unwrap_or_default()
        .trim()
        .to_string();
    if !fallback.is_empty() {
        return fallback;
    }

    default_value.to_string()
}

fn env_flag(name: &str, default_value: bool) -> bool {
    match std::env::var(name) {
        Ok(value) => match value.trim().to_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => true,
            "0" | "false" | "no" | "off" => false,
            _ => default_value,
        },
        Err(_) => default_value,
    }
}

fn spawn_symbol_streams(
    symbol: String,
    ws_tx: mpsc::Sender<WsEvent>,
    futures_url: String,
    spot_url: String,
) {
    let perp_symbol = symbol.clone();
    let perp_tx = ws_tx.clone();
    tokio::spawn(async move {
        let mut ws_manager =
            WsConnectionManager::new(&futures_url, &perp_symbol, perp_tx, MarketType::Perp);
        ws_manager.run().await;
    });

    tokio::spawn(async move {
        let mut ws_manager = WsConnectionManager::new(&spot_url, &symbol, ws_tx, MarketType::Spot);
        ws_manager.run().await;
    });
}

#[tokio::main]
async fn main() {
    let arguments: Vec<String> = std::env::args().collect();
    if arguments
        .iter()
        .any(|argument| argument == "--config-consensus-harness")
    {
        if let Err(error) = ipc::run_config_consensus_harness() {
            eprintln!("{error}");
            std::process::exit(2);
        }
        return;
    }
    if let Some(index) = arguments
        .iter()
        .position(|argument| argument == "--rest-timeout-harness")
    {
        let Some(base_url) = arguments.get(index + 1) else {
            eprintln!("missing local exchange base URL");
            std::process::exit(2);
        };
        if let Err(error) = binance_rest::run_rest_timeout_harness(base_url).await {
            eprintln!("{error}");
            std::process::exit(2);
        }
        return;
    }
    if let Some(index) = arguments
        .iter()
        .position(|argument| argument == "--stream-recovery-harness")
    {
        let Some(bind) = arguments.get(index + 1) else {
            eprintln!("missing local stream bind address");
            std::process::exit(2);
        };
        if let Err(error) = run_stream_recovery_harness(bind).await {
            eprintln!("{error}");
            std::process::exit(2);
        }
        return;
    }
    if let Some(index) = arguments
        .iter()
        .position(|argument| argument == "--metadata-change-harness")
    {
        let Some(base_url) = arguments.get(index + 1) else {
            eprintln!("missing local metadata base URL");
            std::process::exit(2);
        };
        if let Err(error) = binance_rest::run_metadata_change_harness(base_url).await {
            eprintln!("{error}");
            std::process::exit(2);
        }
        return;
    }
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let dotenv_path = manifest_dir
        .parent()
        .map(|parent| parent.join(".env"))
        .unwrap_or_else(|| manifest_dir.join(".env"));
    let dotenv_status = if dotenv_path.exists() {
        match dotenvy::from_path(&dotenv_path) {
            Ok(_) => format!("Loaded project .env from {}", dotenv_path.display()),
            Err(err) => format!(
                "Failed to load project .env from {}: {}",
                dotenv_path.display(),
                err
            ),
        }
    } else {
        format!("Project .env not found at {}", dotenv_path.display())
    };

    let subscriber = FmtSubscriber::builder()
        .with_max_level(tracing::Level::INFO)
        .finish();
    tracing::subscriber::set_global_default(subscriber).expect("setting default subscriber failed");

    tracing::info!("Starting Binance Execution Engine (Rust)...");
    tracing::info!("{}", dotenv_status);

    // Channels for primary execution.
    // 2048 gives ~80% less pre-allocated memory than 10_000 while still
    // absorbing a generous burst; tighten further only after monitoring
    // channel utilisation under load.
    let (engine_tx, engine_rx) = mpsc::channel(2048);

    // Bridge WS Events -> Engine Events
    let (ws_tx, mut ws_rx) = mpsc::channel(2048);
    let engine_tx_for_ws = engine_tx.clone();
    tokio::spawn(async move {
        while let Some(evt) = ws_rx.recv().await {
            let _ = engine_tx_for_ws.send(EngineEvent::Ws(evt)).await;
        }
    });

    // Bridge Alpha IPC -> Engine Events
    let (alpha_tx, mut alpha_rx) = mpsc::channel(2048);
    let engine_tx_for_alpha = engine_tx.clone();
    tokio::spawn(async move {
        while let Some(evt) = alpha_rx.recv().await {
            let _ = engine_tx_for_alpha.send(EngineEvent::Alpha(evt)).await;
        }
    });

    // Broadcast channel for Python Dashboard IPC
    let (dash_tx, _) = broadcast::channel(2048);

    let api_key =
        resolve_shared_api_credential("BINANCE_API_KEY", "BINANCE_SPOT_API_KEY", "DUMMY_API_KEY");
    let secret_key = resolve_shared_api_credential(
        "BINANCE_API_SECRET",
        "BINANCE_SPOT_API_SECRET",
        "DUMMY_SECRET_KEY",
    );

    let trading_mode = std::env::var("TRADING_MODE")
        .unwrap_or_else(|_| "paper".to_string())
        .to_lowercase();
    let trading_mode = match trading_mode.as_str() {
        "live" | "testnet" | "paper" => trading_mode,
        _ => {
            tracing::warn!(
                "Unknown TRADING_MODE '{}', defaulting to 'paper'",
                trading_mode
            );
            "paper".to_string()
        }
    };
    tracing::info!("TRADING_MODE = {}", trading_mode);

    // Capacity one intentionally coalesces repeated lag notifications while a
    // private stream is already reconnecting and replaying its durable cursor.
    let (futures_private_control_tx, futures_private_control_rx) =
        mpsc::channel::<PrivateStreamControl>(1);
    let (spot_private_control_tx, spot_private_control_rx) =
        mpsc::channel::<PrivateStreamControl>(1);

    let (subscription_tx, mut subscription_rx) = mpsc::channel::<String>(1024);
    let mut order_manager = OrderManager::new(
        engine_rx,
        engine_tx.clone(),
        subscription_tx.clone(),
        api_key.clone(),
        secret_key.clone(),
        dash_tx.clone(),
        trading_mode.clone(),
    );

    // Spawn Order Manager
    tokio::spawn(async move {
        order_manager.run().await;
    });

    // Python live_trader_v2 is the orchestrator in live/testnet. Keep the Rust
    // strategy opt-in so it cannot bypass operator pause_new_entries or recovery
    // review state by default.
    if env_flag("ENABLE_RUST_STRATEGY", false) || env_flag("RUST_STRATEGY_ENABLED", false) {
        let engine_tx_for_strategy = engine_tx.clone();
        tokio::spawn(async move {
            info!("Strategy Tick Timer started (60s interval)");
            loop {
                tokio::time::sleep(Duration::from_secs(60)).await;
                let _ = engine_tx_for_strategy.send(EngineEvent::StrategyTick).await;
            }
        });
    } else {
        info!("Rust autonomous strategy timer disabled; awaiting Python alpha instructions.");
    }

    // Spawn ZeroMQ IPC Server using TCP for cross-platform compatibility
    let zmq_endpoint = "tcp://127.0.0.1:5555";
    let mut ipc_server = ipc::IpcServer::new(zmq_endpoint, alpha_tx);
    tokio::spawn(async move {
        ipc_server.run().await;
    });

    // Spawn private User Data WebSocket Managers (skip in paper mode — no real API key needed)
    if trading_mode != "paper" {
        let futures_user_data_rest_client =
            BinanceRest::new(api_key.clone(), secret_key.clone(), trading_mode.clone());
        let spot_user_data_rest_client =
            BinanceRest::new(api_key.clone(), secret_key.clone(), trading_mode.clone());
        let fut_ud_tx = ws_tx.clone();
        tokio::spawn(async move {
            let mut ud_ws_manager = UserDataWsManager::new(
                futures_user_data_rest_client,
                fut_ud_tx,
                UserDataStreamKind::Futures,
            )
            .with_control_receiver(futures_private_control_rx);
            ud_ws_manager.run().await;
        });

        let spot_ud_tx = ws_tx.clone();
        tokio::spawn(async move {
            let mut ud_ws_manager = UserDataWsManager::new(
                spot_user_data_rest_client,
                spot_ud_tx,
                UserDataStreamKind::Spot,
            )
            .with_control_receiver(spot_private_control_rx);
            ud_ws_manager.run().await;
        });
    }

    // Read monitored symbols from env — must match Python's MONITORED_SYMBOLS
    let symbols_env =
        std::env::var("MONITORED_SYMBOLS").unwrap_or_else(|_| "BTCUSDT,ETHUSDT".to_string());
    let monitored_symbols: Vec<String> = symbols_env
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();

    tracing::info!(
        "Monitoring {} symbols: {:?}",
        monitored_symbols.len(),
        monitored_symbols
    );

    // Only use testnet WS endpoints when TRADING_MODE=testnet.
    // Paper mode uses mainnet WS for real market data (but places no real orders).
    let use_testnet = trading_mode == "testnet";
    let binance_ws_url = if use_testnet {
        "wss://fstream.binancefuture.com/ws"
    } else {
        "wss://fstream.binance.com/ws"
    };
    let default_spot_ws_url = if use_testnet {
        "wss://demo-stream.binance.com/ws".to_string()
    } else {
        "wss://stream.binance.com:9443/ws".to_string()
    };
    let spot_ws_url = std::env::var("BINANCE_SPOT_WS_URL").unwrap_or(default_spot_ws_url);

    let mut subscribed_symbols: HashSet<String> = monitored_symbols
        .iter()
        .map(|symbol| symbol.to_uppercase())
        .collect();

    // Spawn perp + spot WsConnectionManager for each symbol
    for symbol in &monitored_symbols {
        spawn_symbol_streams(
            symbol.clone(),
            ws_tx.clone(),
            binance_ws_url.to_string(),
            spot_ws_url.clone(),
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }

    let ws_tx_dynamic = ws_tx.clone();
    let futures_url_dynamic = binance_ws_url.to_string();
    let spot_url_dynamic = spot_ws_url.clone();
    tokio::spawn(async move {
        while let Some(symbol) = subscription_rx.recv().await {
            let normalized = symbol.trim().to_uppercase();
            if normalized.is_empty() || !subscribed_symbols.insert(normalized.clone()) {
                continue;
            }
            tracing::info!("Dynamically subscribing market data for {}", normalized);
            spawn_symbol_streams(
                normalized,
                ws_tx_dynamic.clone(),
                futures_url_dynamic.clone(),
                spot_url_dynamic.clone(),
            );
            tokio::time::sleep(Duration::from_millis(50)).await;
        }
    });

    // Spawn IPC Server
    let dash_tx_ipc = dash_tx.clone();
    let ws_tx_telemetry = ws_tx.clone();
    tokio::spawn(async move {
        let listener = TcpListener::bind("127.0.0.1:9000").await.unwrap();
        tracing::info!("Dashboard IPC Server listening on 127.0.0.1:9000");

        while let Ok((mut socket, _)) = listener.accept().await {
            let mut rx = dash_tx_ipc.subscribe();
            let ws_tx_client = ws_tx_telemetry.clone();
            let futures_control = futures_private_control_tx.clone();
            let spot_control = spot_private_control_tx.clone();
            tokio::spawn(async move {
                loop {
                    match rx.recv().await {
                        Ok(msg) => {
                            if socket.write_all(&msg).await.is_err() {
                                tracing::warn!(
                                    "Dashboard IPC client disconnected, closing socket task."
                                );
                                break;
                            }
                        }
                        Err(broadcast::error::RecvError::Lagged(skipped)) => {
                            tracing::warn!(
                                "Dashboard IPC client lagged, skipped {} messages",
                                skipped
                            );
                            handle_telemetry_lag(
                                &mut socket,
                                skipped,
                                &ws_tx_client,
                                &futures_control,
                                &spot_control,
                            )
                            .await;
                            // Reconnection establishes a clean transport boundary;
                            // readiness remains blocked until replay/reconciliation.
                            break;
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

#[cfg(test)]
mod tests {
    use super::*;
    use tokio::io::AsyncReadExt;

    #[tokio::test]
    async fn lagged_telemetry_client_gets_marker_and_both_private_replay_requests() {
        let (mut server, mut client) = tokio::io::duplex(4096);
        let (ws_tx, mut ws_rx) = mpsc::channel(2);
        let (futures_tx, mut futures_rx) = mpsc::channel(1);
        let (spot_tx, mut spot_rx) = mpsc::channel(1);

        handle_telemetry_lag(&mut server, 37, &ws_tx, &futures_tx, &spot_tx).await;
        drop(server);

        let mut payload = Vec::new();
        client.read_to_end(&mut payload).await.unwrap();
        let direct: serde_json::Value = rmp_serde::from_slice(&payload).unwrap();
        assert_eq!(direct["event"], "TelemetryGap");
        assert_eq!(direct["skipped_messages"], 37);

        match ws_rx.recv().await.unwrap() {
            WsEvent::TelemetryGap {
                skipped_messages,
                reason,
                ..
            } => {
                assert_eq!(skipped_messages, 37);
                assert_eq!(reason, "broadcast_receiver_overflow");
            }
            other => panic!("unexpected telemetry recovery event: {other:?}"),
        }
        for control in [futures_rx.recv().await, spot_rx.recv().await] {
            assert_eq!(
                control,
                Some(PrivateStreamControl::ReplayFromCursor {
                    reason: "telemetry receiver overflow".to_string(),
                })
            );
        }
    }
}
