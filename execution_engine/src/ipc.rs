use serde::{Deserialize, Serialize};
use std::time::Duration;
use tokio::sync::mpsc::Sender;
use tracing::{error, info};
use zeromq::{Socket, SocketRecv, PullSocket};

use crate::order_manager::WsEvent; // we will add an IPC event here later or create an alpha event channel

#[derive(Debug, Deserialize, Serialize)]
pub struct AlphaInstruction {
    pub symbol: String,
    pub intent: String, // e.g. "ENTER_LONG", "EXIT_LONG"
    pub quantity: f64,
    pub urgency: f64,
    pub max_slippage_bps: f64,
    pub exposure_scale: f64,
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
                                info!("Received Alpha Instruction: {:?}", instruction);
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
