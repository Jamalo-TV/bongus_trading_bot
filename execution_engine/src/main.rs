#![recursion_limit = "256"]

mod binance_endpoints;
mod binance_rest;
mod binance_ws;
mod collateral_engine;
mod exact_decimal;
mod ipc;
mod order_manager;
mod ranking;
#[cfg_attr(not(unix), allow(dead_code, unused_variables))]
mod recovery_generation;
mod storage;
mod strategy;
mod telemetry;
mod user_data_ws;

use binance_rest::BinanceRest;
use binance_ws::{WsConnectionManager, WsFeedKind};
use futures_util::FutureExt;
use order_manager::{EngineEvent, OrderManager, WsEvent};
use std::collections::HashSet;
use std::future::Future;
use std::panic::AssertUnwindSafe;
use std::path::{Path, PathBuf};
use std::sync::{Arc, RwLock};
use std::time::Duration;
use std::time::{SystemTime, UNIX_EPOCH};
use telemetry::{TelemetryFrame, TelemetryJournal, TelemetryRelayControl};
use tokio::io::AsyncWriteExt;
use tokio::net::TcpListener;
use tokio::sync::mpsc;
use tracing::info;
use tracing_subscriber::FmtSubscriber;
use user_data_ws::{
    PrivateCursorRecoveryHandle, PrivateStreamControl, UserDataStreamKind, UserDataWsManager,
};

fn current_time_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| i64::try_from(duration.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or_default()
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

fn discover_runtime_dotenv(current_dir: &Path) -> Option<PathBuf> {
    let local = current_dir.join(".env");
    if local.is_file() {
        return Some(local);
    }

    // The watchdog launches a packaged binary with cwd=<release>/bin, while
    // `cargo run` is commonly invoked from the source execution_engine folder.
    // Search exactly those runtime layouts; never embed the build checkout.
    let directory_name = current_dir.file_name().and_then(|name| name.to_str());
    if matches!(directory_name, Some("bin") | Some("execution_engine")) {
        let parent = current_dir.parent()?.join(".env");
        if parent.is_file() {
            return Some(parent);
        }
    }
    None
}

fn spawn_critical_task<F>(name: String, fatal_tx: mpsc::Sender<String>, future: F)
where
    F: Future<Output = ()> + Send + 'static,
{
    tokio::spawn(async move {
        let outcome = AssertUnwindSafe(future).catch_unwind().await;
        let reason = match outcome {
            Ok(()) => format!("critical task {name} exited"),
            Err(_) => format!("critical task {name} panicked"),
        };
        let _ = fatal_tx.send(reason).await;
    });
}

fn spawn_symbol_streams(
    symbol: String,
    ws_tx: mpsc::Sender<WsEvent>,
    futures_public_url: String,
    futures_market_url: String,
    spot_public_url: String,
    planned_connection_max_age_seconds: u64,
    fatal_tx: mpsc::Sender<String>,
) {
    let futures_public_symbol = symbol.clone();
    let futures_public_tx = ws_tx.clone();
    let futures_public_name = format!("futures-public-stream-{futures_public_symbol}");
    spawn_critical_task(futures_public_name, fatal_tx.clone(), async move {
        let mut ws_manager = WsConnectionManager::new(
            &futures_public_url,
            &futures_public_symbol,
            futures_public_tx,
            WsFeedKind::FuturesPublic,
            planned_connection_max_age_seconds,
        );
        ws_manager.run().await;
    });

    let futures_market_symbol = symbol.clone();
    let futures_market_tx = ws_tx.clone();
    let futures_market_name = format!("futures-market-stream-{futures_market_symbol}");
    spawn_critical_task(futures_market_name, fatal_tx.clone(), async move {
        let mut ws_manager = WsConnectionManager::new(
            &futures_market_url,
            &futures_market_symbol,
            futures_market_tx,
            WsFeedKind::FuturesMarket,
            planned_connection_max_age_seconds,
        );
        ws_manager.run().await;
    });

    let spot_name = format!("spot-public-stream-{symbol}");
    spawn_critical_task(spot_name, fatal_tx, async move {
        let mut ws_manager = WsConnectionManager::new(
            &spot_public_url,
            &symbol,
            ws_tx,
            WsFeedKind::SpotPublic,
            planned_connection_max_age_seconds,
        );
        ws_manager.run().await;
    });
}

#[tokio::main]
async fn main() {
    let arguments: Vec<String> = std::env::args().collect();
    if arguments
        .iter()
        .any(|argument| argument == "--create-recovery-generation")
    {
        if let Err(error) = recovery_generation::run_recovery_generation_cli(&arguments).await {
            eprintln!("{error}");
            std::process::exit(2);
        }
        return;
    }
    if let Some(index) = arguments
        .iter()
        .position(|argument| argument == "--verify-recovery-generation")
    {
        let Some(manifest_path) = arguments.get(index + 1) else {
            eprintln!("missing recovery generation manifest path");
            std::process::exit(2);
        };
        match recovery_generation::verify_recovery_generation(Path::new(manifest_path)) {
            Ok(result) => {
                println!(
                    "{}",
                    serde_json::to_string(&result)
                        .expect("recovery verification response is serializable")
                );
            }
            Err(error) => {
                eprintln!("{error}");
                std::process::exit(2);
            }
        }
        return;
    }
    if arguments
        .iter()
        .any(|argument| argument == "--execution-protocol-harness")
    {
        if let Err(error) = ipc::run_execution_protocol_harness() {
            eprintln!("{error}");
            std::process::exit(2);
        }
        return;
    }
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
    let dotenv_status = match std::env::current_dir() {
        Ok(current_dir) => match discover_runtime_dotenv(&current_dir) {
            Some(dotenv_path) => match dotenvy::from_path(&dotenv_path) {
                Ok(_) => format!("Loaded runtime .env from {}", dotenv_path.display()),
                Err(err) => format!(
                    "Failed to load runtime .env from {}: {}",
                    dotenv_path.display(),
                    err
                ),
            },
            None => format!(
                "Runtime .env not found relative to cwd {}",
                current_dir.display()
            ),
        },
        Err(error) => format!("Unable to resolve runtime cwd for .env discovery: {error}"),
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
    let (critical_failure_tx, mut critical_failure_rx) = mpsc::channel::<String>(64);

    // Bridge WS Events -> Engine Events
    let (ws_tx, mut ws_rx) = mpsc::channel(2048);
    let engine_tx_for_ws = engine_tx.clone();
    spawn_critical_task(
        "websocket-engine-bridge".to_string(),
        critical_failure_tx.clone(),
        async move {
            while let Some(evt) = ws_rx.recv().await {
                if engine_tx_for_ws
                    .send(EngineEvent::Ws(Box::new(evt)))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        },
    );

    // Bridge Alpha IPC -> Engine Events
    let (alpha_tx, mut alpha_rx) = mpsc::channel(2048);
    let engine_tx_for_alpha = engine_tx.clone();
    spawn_critical_task(
        "alpha-engine-bridge".to_string(),
        critical_failure_tx.clone(),
        async move {
            while let Some(evt) = alpha_rx.recv().await {
                if engine_tx_for_alpha
                    .send(EngineEvent::Alpha(Box::new(evt)))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        },
    );

    // Broadcast channel for Python Dashboard IPC
    let (dash_tx, _) = tokio::sync::broadcast::channel(2048);
    // Subscribe before any producer can emit. The relay is the single durable
    // sequencing boundary; client connections consume its separate stream.
    let telemetry_source_rx = dash_tx.subscribe();
    let (telemetry_clients_tx, _) = tokio::sync::broadcast::channel::<TelemetryFrame>(2048);
    let (telemetry_recovery_tx, telemetry_recovery_rx) = mpsc::channel::<TelemetryRelayControl>(1);

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

    // Required ports are acquired before any component can announce trading
    // readiness. An occupied telemetry port is a startup failure, not a
    // detached-task warning.
    let telemetry_listener = match TcpListener::bind("127.0.0.1:9000").await {
        Ok(listener) => listener,
        Err(error) => {
            tracing::error!("Failed to bind required telemetry port: {}", error);
            std::process::exit(1);
        }
    };
    let telemetry_journal = match TelemetryJournal::from_env() {
        Ok(journal) => std::sync::Arc::new(tokio::sync::Mutex::new(journal)),
        Err(error) => {
            tracing::error!("Durable telemetry journal is unavailable: {}", error);
            std::process::exit(1);
        }
    };

    let telemetry_clients_for_relay = telemetry_clients_tx.clone();
    let telemetry_journal_for_relay = telemetry_journal.clone();
    let ws_tx_for_relay = ws_tx.clone();
    let futures_control_for_relay = futures_private_control_tx.clone();
    let spot_control_for_relay = spot_private_control_tx.clone();
    let mut telemetry_relay_task = tokio::spawn(async move {
        telemetry::run_telemetry_relay(
            telemetry_source_rx,
            telemetry_clients_for_relay,
            telemetry_journal_for_relay,
            ws_tx_for_relay,
            futures_control_for_relay,
            spot_control_for_relay,
            telemetry_recovery_rx,
        )
        .await
    });

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
    let monitored_symbols: Vec<String> = std::env::var("MONITORED_SYMBOLS")
        .unwrap_or_else(|_| "BTCUSDT,ETHUSDT".to_string())
        .split(',')
        .map(|symbol| symbol.trim().to_uppercase())
        .filter(|symbol| !symbol.is_empty())
        .collect();
    let mut initial_private_recovery_universe: HashSet<String> =
        monitored_symbols.iter().cloned().collect();
    initial_private_recovery_universe.extend(order_manager.private_recovery_symbols());
    let private_recovery_universe = Arc::new(RwLock::new(initial_private_recovery_universe));
    let futures_cursor_recovery =
        PrivateCursorRecoveryHandle::from_env(UserDataStreamKind::Futures);
    let spot_cursor_recovery = PrivateCursorRecoveryHandle::from_env(UserDataStreamKind::Spot);
    let recovery_coordinator = match recovery_generation::RecoveryCoordinator::from_env(
        engine_tx.clone(),
        telemetry_recovery_tx,
        spot_cursor_recovery.clone(),
        futures_cursor_recovery.clone(),
    ) {
        Ok(coordinator) => coordinator,
        Err(error) => {
            tracing::error!("Recovery generation coordinator is unavailable: {}", error);
            std::process::exit(1);
        }
    };

    let audit_interval_s = std::env::var("RUST_POSITION_AUDIT_INTERVAL_S")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(120)
        .max(1);
    let engine_tx_for_audit = engine_tx.clone();
    spawn_critical_task(
        "position-audit-timer".to_string(),
        critical_failure_tx.clone(),
        async move {
            info!(
                "Position Audit Timer started ({}s interval)",
                audit_interval_s
            );
            loop {
                tokio::time::sleep(Duration::from_secs(audit_interval_s)).await;
                if engine_tx_for_audit
                    .send(EngineEvent::PositionAuditTick)
                    .await
                    .is_err()
                {
                    break;
                }
            }
        },
    );

    // Metadata HTTP work stays outside the order actor so a slow refresh can
    // never starve private fills. The timer itself is supervised like every
    // other execution-critical producer.
    let metadata_interval_s = std::env::var("EXCHANGE_INFO_REFRESH_INTERVAL_S")
        .ok()
        .and_then(|value| value.parse::<u64>().ok())
        .unwrap_or(300)
        .max(30);
    let metadata_rest = order_manager.binance_rest.clone();
    let engine_tx_for_metadata = engine_tx.clone();
    spawn_critical_task(
        "exchange-metadata-refresh".to_string(),
        critical_failure_tx.clone(),
        async move {
            loop {
                tokio::time::sleep(Duration::from_secs(metadata_interval_s)).await;
                let result = metadata_rest.get_exchange_info().await;
                if engine_tx_for_metadata
                    .send(EngineEvent::ExchangeInfoRefreshResult(result))
                    .await
                    .is_err()
                {
                    break;
                }
            }
        },
    );

    // Spawn Order Manager
    let mut order_manager_task = tokio::spawn(async move {
        order_manager.run().await;
    });
    let (recovery_control_ready_tx, recovery_control_ready_rx) = tokio::sync::oneshot::channel();
    spawn_critical_task(
        "recovery-generation-control".to_string(),
        critical_failure_tx.clone(),
        async move {
            if let Err(error) = recovery_generation::run_recovery_control_server(
                recovery_coordinator,
                Some(recovery_control_ready_tx),
            )
            .await
            {
                tracing::error!("Recovery generation control failed: {}", error);
            }
        },
    );
    if recovery_control_ready_rx.await.is_err() {
        tracing::error!("Recovery generation control exited before binding its required endpoint");
        std::process::exit(1);
    }

    // Python live_trader_v2 is the orchestrator in live/testnet. Keep the Rust
    // strategy opt-in so it cannot bypass operator pause_new_entries or recovery
    // review state by default.
    if env_flag("ENABLE_RUST_STRATEGY", false) || env_flag("RUST_STRATEGY_ENABLED", false) {
        let engine_tx_for_strategy = engine_tx.clone();
        spawn_critical_task(
            "rust-strategy-timer".to_string(),
            critical_failure_tx.clone(),
            async move {
                info!("Strategy Tick Timer started (60s interval)");
                loop {
                    tokio::time::sleep(Duration::from_secs(60)).await;
                    if engine_tx_for_strategy
                        .send(EngineEvent::StrategyTick)
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            },
        );
    } else {
        info!("Rust autonomous strategy timer disabled; awaiting Python alpha instructions.");
    }

    // Spawn ZeroMQ IPC Server using TCP for cross-platform compatibility
    let zmq_endpoint = "tcp://127.0.0.1:5555";
    let mut ipc_server = ipc::IpcServer::new(zmq_endpoint, alpha_tx);
    let (ipc_ready_tx, ipc_ready_rx) = tokio::sync::oneshot::channel();
    let mut ipc_task = tokio::spawn(async move { ipc_server.run(ipc_ready_tx).await });
    match ipc_ready_rx.await {
        Ok(Ok(())) => {}
        Ok(Err(error)) => {
            tracing::error!("Required alpha IPC endpoint is unavailable: {}", error);
            std::process::exit(1);
        }
        Err(_) => {
            tracing::error!("Alpha IPC task exited before reporting bind readiness");
            std::process::exit(1);
        }
    }

    // Spawn private User Data WebSocket Managers (skip in paper mode — no real API key needed)
    if trading_mode != "paper" {
        let futures_user_data_rest_client =
            BinanceRest::new(api_key.clone(), secret_key.clone(), trading_mode.clone());
        let spot_user_data_rest_client =
            BinanceRest::new(api_key.clone(), secret_key.clone(), trading_mode.clone());
        let fut_ud_tx = ws_tx.clone();
        let futures_recovery_universe = private_recovery_universe.clone();
        spawn_critical_task(
            "futures-private-user-stream".to_string(),
            critical_failure_tx.clone(),
            async move {
                let mut ud_ws_manager = UserDataWsManager::new(
                    futures_user_data_rest_client,
                    fut_ud_tx,
                    UserDataStreamKind::Futures,
                )
                .with_recovery_cursor_handle(futures_cursor_recovery)
                .with_recovery_universe(futures_recovery_universe)
                .with_control_receiver(futures_private_control_rx);
                ud_ws_manager.run().await;
            },
        );

        let spot_ud_tx = ws_tx.clone();
        let spot_recovery_universe = private_recovery_universe.clone();
        spawn_critical_task(
            "spot-private-user-stream".to_string(),
            critical_failure_tx.clone(),
            async move {
                let mut ud_ws_manager = UserDataWsManager::new(
                    spot_user_data_rest_client,
                    spot_ud_tx,
                    UserDataStreamKind::Spot,
                )
                .with_recovery_cursor_handle(spot_cursor_recovery)
                .with_recovery_universe(spot_recovery_universe)
                .with_control_receiver(spot_private_control_rx);
                ud_ws_manager.run().await;
            },
        );
    }

    // Read monitored symbols from env — must match Python's MONITORED_SYMBOLS
    tracing::info!(
        "Monitoring {} symbols: {:?}",
        monitored_symbols.len(),
        monitored_symbols
    );

    // Python and Rust consume one versioned endpoint matrix. Paper mode selects
    // mainnet public market data while its signed order path remains disabled.
    let binance_endpoints = match binance_endpoints::endpoints_for_mode(&trading_mode) {
        Ok(endpoints) => endpoints,
        Err(error) => {
            tracing::error!("Invalid shared Binance endpoint matrix: {}", error);
            std::process::exit(1);
        }
    };
    let futures_public_ws_url = format!(
        "{}/ws",
        binance_endpoints
            .futures
            .public_stream_ws_base_url
            .trim_end_matches('/')
    );
    let futures_market_ws_url = format!(
        "{}/ws",
        binance_endpoints
            .futures
            .market_stream_ws_base_url
            .trim_end_matches('/')
    );
    let spot_public_ws_url = format!(
        "{}/ws",
        binance_endpoints
            .spot
            .public_stream_ws_base_url
            .trim_end_matches('/')
    );

    let mut subscribed_symbols: HashSet<String> = monitored_symbols
        .iter()
        .map(|symbol| symbol.to_uppercase())
        .collect();

    // Spawn separate USD-M public/market sessions plus Spot public depth.
    for symbol in &monitored_symbols {
        spawn_symbol_streams(
            symbol.clone(),
            ws_tx.clone(),
            futures_public_ws_url.clone(),
            futures_market_ws_url.clone(),
            spot_public_ws_url.clone(),
            binance_endpoints.planned_connection_max_age_seconds,
            critical_failure_tx.clone(),
        );
        tokio::time::sleep(Duration::from_millis(50)).await;
    }

    let ws_tx_dynamic = ws_tx.clone();
    let futures_public_url_dynamic = futures_public_ws_url;
    let futures_market_url_dynamic = futures_market_ws_url;
    let spot_public_url_dynamic = spot_public_ws_url;
    let public_connection_max_age_seconds = binance_endpoints.planned_connection_max_age_seconds;
    let dynamic_private_recovery_universe = private_recovery_universe.clone();
    let stream_failure_tx = critical_failure_tx.clone();
    spawn_critical_task(
        "dynamic-market-subscription-manager".to_string(),
        critical_failure_tx.clone(),
        async move {
            while let Some(symbol) = subscription_rx.recv().await {
                let normalized = symbol.trim().to_uppercase();
                if normalized.is_empty() {
                    continue;
                }
                match dynamic_private_recovery_universe.write() {
                    Ok(mut universe) => {
                        universe.insert(normalized.clone());
                    }
                    Err(_) => {
                        tracing::error!("Private recovery universe lock is poisoned");
                        return;
                    }
                }
                if !subscribed_symbols.insert(normalized.clone()) {
                    continue;
                }
                tracing::info!("Dynamically subscribing market data for {}", normalized);
                spawn_symbol_streams(
                    normalized,
                    ws_tx_dynamic.clone(),
                    futures_public_url_dynamic.clone(),
                    futures_market_url_dynamic.clone(),
                    spot_public_url_dynamic.clone(),
                    public_connection_max_age_seconds,
                    stream_failure_tx.clone(),
                );
                tokio::time::sleep(Duration::from_millis(50)).await;
            }
        },
    );

    // Spawn the already-bound telemetry server.
    let telemetry_clients_for_server = telemetry_clients_tx.clone();
    let telemetry_journal_for_server = telemetry_journal.clone();
    let ws_tx_telemetry = ws_tx.clone();
    let mut telemetry_task = tokio::spawn(async move {
        telemetry::run_telemetry_server(
            telemetry_listener,
            telemetry_clients_for_server,
            telemetry_journal_for_server,
            ws_tx_telemetry,
            futures_private_control_tx,
            spot_private_control_tx,
        )
        .await
    });

    // Critical execution services are supervised. If any one returns or
    // panics, exit non-zero so the watchdog cannot mistake a half-alive process
    // for a ready execution engine.
    let fatal_reason = tokio::select! {
        signal = tokio::signal::ctrl_c() => {
            match signal {
                Ok(()) => None,
                Err(error) => Some(format!("ctrl-c handler failed: {error}")),
            }
        }
        result = &mut order_manager_task => {
            Some(match result {
                Ok(()) => "critical order-manager task exited".to_string(),
                Err(error) => format!("critical order-manager task panicked: {error}"),
            })
        }
        result = &mut ipc_task => {
            Some(match result {
                Ok(Ok(())) => "critical alpha IPC task exited".to_string(),
                Ok(Err(error)) => format!("critical alpha IPC task failed: {error}"),
                Err(error) => format!("critical alpha IPC task panicked: {error}"),
            })
        }
        result = &mut telemetry_task => {
            Some(match result {
                Ok(Ok(())) => "critical telemetry task exited".to_string(),
                Ok(Err(error)) => format!("critical telemetry task failed: {error}"),
                Err(error) => format!("critical telemetry task panicked: {error}"),
            })
        }
        result = &mut telemetry_relay_task => {
            Some(match result {
                Ok(Ok(())) => "critical telemetry relay task exited".to_string(),
                Ok(Err(error)) => format!("critical telemetry relay task failed: {error}"),
                Err(error) => format!("critical telemetry relay task panicked: {error}"),
            })
        }
        reason = critical_failure_rx.recv() => {
            Some(reason.unwrap_or_else(|| "critical task supervisor channel closed".to_string()))
        }
    };
    if let Some(reason) = fatal_reason {
        tracing::error!("{}", reason);
        std::process::exit(1);
    }
    tracing::info!("Shutting down engine.");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unique_test_directory(label: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("time after epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "bongus-main-{label}-{}-{nonce}",
            std::process::id()
        ))
    }

    #[test]
    fn dotenv_discovery_uses_runtime_layout_without_ancestor_walk() {
        let root = unique_test_directory("dotenv");
        let bin = root.join("bin");
        let unrelated = root.join("nested");
        std::fs::create_dir_all(&bin).expect("create bin");
        std::fs::create_dir_all(&unrelated).expect("create nested");
        std::fs::write(root.join(".env"), "TRADING_MODE=testnet\n").expect("write dotenv");

        assert_eq!(discover_runtime_dotenv(&root), Some(root.join(".env")));
        assert_eq!(discover_runtime_dotenv(&bin), Some(root.join(".env")));
        assert_eq!(discover_runtime_dotenv(&unrelated), None);

        std::fs::remove_dir_all(root).expect("remove fixture");
    }

    #[tokio::test]
    async fn supervised_task_reports_return_and_panic() {
        let (fatal_tx, mut fatal_rx) = mpsc::channel(2);
        spawn_critical_task("returns".to_string(), fatal_tx.clone(), async {});
        spawn_critical_task("panics".to_string(), fatal_tx, async {
            panic!("supervision fixture");
        });
        let mut reasons = Vec::new();
        for _ in 0..2 {
            reasons.push(
                tokio::time::timeout(Duration::from_secs(1), fatal_rx.recv())
                    .await
                    .expect("supervisor notification")
                    .expect("supervisor channel remains open"),
            );
        }
        assert!(
            reasons
                .iter()
                .any(|reason| reason == "critical task returns exited")
        );
        assert!(
            reasons
                .iter()
                .any(|reason| reason == "critical task panics panicked")
        );
    }
}
