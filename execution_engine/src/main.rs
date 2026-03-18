mod binance_rest;
mod binance_ws;
mod collateral_engine;
mod order_manager;

use binance_ws::WsConnectionManager;
use order_manager::OrderManager;
use tokio::sync::mpsc;
use tokio::sync::broadcast;
use tracing_subscriber::FmtSubscriber;
use tokio::net::TcpListener;
use tokio::io::AsyncWriteExt;
use std::time::Duration;

#[tokio::main]
async fn main() {
    let subscriber = FmtSubscriber::builder()
        .with_max_level(tracing::Level::INFO)
        .finish();
    tracing::subscriber::set_global_default(subscriber)
        .expect("setting default subscriber failed");

    tracing::info!("Starting Binance Execution Engine (Rust)...");

    // Channels for primary execution
    let (tx, rx) = mpsc::channel(10000);
    
    // Broadcast channel for Python Dashboard IPC
    let (dash_tx, _) = broadcast::channel(10000);

    let mut order_manager = OrderManager::new(
        rx, 
        "DUMMY_API_KEY".to_string(), 
        "DUMMY_SECRET_KEY".to_string(),
        dash_tx.clone()
    );

    // Spawn Order Manager
    tokio::spawn(async move {
        order_manager.run().await;
    });

    let top_assets = vec![
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT",
        "TRXUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT", "MATICUSDT", "LTCUSDT",
        "BCHUSDT", "UNIUSDT", "NEARUSDT", "APTUSDT", "XLMUSDT", "ATOMUSDT", "ARBUSDT"
    ];

    let binance_ws_url = "wss://fstream.binance.com/ws";

    // Spawn WsConnectionManager for each asset
    for symbol in top_assets {
        let sym = symbol.to_string();
        let tx_clone = tx.clone();
        let dash_tx_clone = dash_tx.clone();
        let url = binance_ws_url.to_string();
        
        tokio::spawn(async move {
            let mut ws_manager = WsConnectionManager::new(&url, &sym, tx_clone);
            // Optionally, the WsConnectionManager could also send to dash_tx
            // For now, we'll let it run.
            ws_manager.run().await;
        });
        // Pace connection initialization to avoid rate limits
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
                while let Ok(msg) = rx.recv().await {
                    let _ = socket.write_all(format!("{}\\n", msg).as_bytes()).await;
                }
            });
        }
    });

    // Keep main thread alive
    tokio::signal::ctrl_c().await.unwrap();
    tracing::info!("Shutting down engine.");
}
