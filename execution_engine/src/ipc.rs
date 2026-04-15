use serde::{Deserialize, Serialize};
use std::time::Duration;
use tokio::sync::mpsc::Sender;
use tracing::{debug, error, info};
use zeromq::{PullSocket, Socket, SocketRecv};

#[derive(Debug, Deserialize, Serialize)]
pub struct AlphaInstruction {
    #[serde(default)]
    pub symbol: Option<String>,
    pub intent: String, // e.g. "ENTER_LONG", "EXIT_LONG"
    #[serde(default)]
    pub quantity: f64,
    #[serde(default)]
    pub urgency: f64,
    #[serde(default)]
    pub max_slippage_bps: f64,
    #[serde(default)]
    pub exposure_scale: f64,
    pub heartbeat_id: Option<String>,
    #[serde(default)]
    pub intent_id: Option<String>,
    #[serde(default)]
    pub direction: Option<String>,
    #[serde(default)]
    pub skip_spot_leg: bool,
    #[serde(default)]
    pub skip_perp_leg: bool,
    #[serde(default)]
    pub spot_entry_price: Option<f64>,
    #[serde(default)]
    pub perp_entry_price: Option<f64>,
    #[serde(default)]
    pub spot_mark_price: Option<f64>,
    #[serde(default)]
    pub perp_mark_price: Option<f64>,
    #[serde(default)]
    pub spot_quantity: Option<f64>,
    #[serde(default)]
    pub perp_quantity: Option<f64>,
}

pub struct IpcServer {
    endpoint: String,
    tx: Sender<AlphaInstruction>,
}

impl IpcServer {
    pub fn new(endpoint: &str, tx: Sender<AlphaInstruction>) -> Self {
        Self {
            endpoint: endpoint.to_string(),
            tx,
        }
    }

    pub async fn run(&mut self) {
        info!("Starting IPC ZeroMQ Receiver on {}", self.endpoint);
        let mut socket = PullSocket::new();

        match socket.bind(&self.endpoint).await {
            Ok(_) => info!("Listening for alpha instructions on {}", self.endpoint),
            Err(e) => {
                error!("Failed to bind ZeroMQ socket: {}", e);
                return;
            }
        }

        loop {
            match socket.recv().await {
                Ok(msg) => {
                    // msg is a valid ZMQ message. Assuming multipart message with 1 part.
                    if let Some(bytes) = msg.get(0) {
                        match rmp_serde::from_slice::<AlphaInstruction>(bytes) {
                            Ok(instruction) => {
                                debug!("Received Alpha Instruction: {:?}", instruction);
                                let _ = self.tx.send(instruction).await;
                            }
                            Err(e) => {
                                error!("Failed to deserialize IPC message: {}", e);
                            }
                        }
                    }
                }
                Err(e) => {
                    error!("Error receiving from ZMQ socket: {}", e);
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            }
        }
    }
}
