use crate::order_manager::WsEvent;
use crate::user_data_ws::PrivateStreamControl;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader as StdBufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{Mutex, broadcast, mpsc, oneshot};

const TELEMETRY_SCHEMA_VERSION: u16 = 1;
const DEFAULT_TELEMETRY_JOURNAL_MAX_BYTES: u64 = 30_000_000;
const MIN_TELEMETRY_JOURNAL_MAX_BYTES: u64 = 64 * 1024;
const MAX_CURSOR_RECORD_BYTES: usize = 4096;
const COMPACTION_RECORD_SLACK: usize = 1024;
const MAX_ACK_LINE_BYTES: usize = 1024;
const DEFAULT_PRIMARY_CONSUMER_ID: &str = "python-live-trader";

#[derive(Debug, Clone)]
pub struct TelemetryFrame {
    pub sequence: Option<u64>,
    pub payload: Vec<u8>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct StoredTelemetryEvent {
    schema_version: u16,
    sequence: u64,
    payload: Value,
    checksum: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct TelemetryCursor {
    schema_version: u16,
    generation: u64,
    high_water_sequence: u64,
    consumer_id: String,
    checksum: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TelemetryAck {
    event: String,
    schema_version: u16,
    consumer_id: String,
    high_water_sequence: u64,
}

pub struct TelemetryJournal {
    path: PathBuf,
    cursor_path: PathBuf,
    max_bytes: u64,
    events: VecDeque<StoredTelemetryEvent>,
    next_sequence: u64,
    acknowledged_high_water: u64,
    cursor_generation: u64,
    records_on_disk: usize,
    primary_consumer_id: String,
}

#[cfg_attr(not(unix), allow(dead_code))]
#[derive(Debug, Clone)]
pub(crate) struct TelemetryRecoverySnapshot {
    pub journal_path: PathBuf,
    pub active_cursor_path: Option<PathBuf>,
    pub active_cursor_bytes: Vec<u8>,
    pub active_cursor_suffix: String,
    pub published_high_water_sequence: u64,
    pub acknowledged_high_water_sequence: u64,
    pub cursor_generation: u64,
}

#[cfg_attr(not(unix), allow(dead_code))]
pub(crate) enum TelemetryRelayControl {
    RecoveryBarrier {
        request_id: String,
        reply: oneshot::Sender<Result<TelemetryRecoverySnapshot, String>>,
        release: oneshot::Receiver<()>,
        resumed: oneshot::Sender<()>,
    },
}

impl TelemetryJournal {
    pub fn from_env() -> Result<Self, String> {
        let path = std::env::var("EXECUTION_TELEMETRY_JOURNAL_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| crate::ipc::default_rust_runtime_path("execution_telemetry.jsonl"));
        let cursor_path = std::env::var("EXECUTION_TELEMETRY_CURSOR_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| path_with_suffix(&path, ".cursor"));
        let max_bytes = std::env::var("EXECUTION_TELEMETRY_JOURNAL_MAX_BYTES")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .filter(|value| *value >= MIN_TELEMETRY_JOURNAL_MAX_BYTES)
            .unwrap_or(DEFAULT_TELEMETRY_JOURNAL_MAX_BYTES)
            .min(DEFAULT_TELEMETRY_JOURNAL_MAX_BYTES);
        let primary_consumer_id = std::env::var("EXECUTION_TELEMETRY_PRIMARY_CONSUMER_ID")
            .unwrap_or_else(|_| DEFAULT_PRIMARY_CONSUMER_ID.to_string());
        Self::load_with_consumer(path, cursor_path, max_bytes, primary_consumer_id)
    }

    #[cfg(test)]
    fn load(
        path: impl AsRef<Path>,
        cursor_path: impl AsRef<Path>,
        max_bytes: u64,
    ) -> Result<Self, String> {
        Self::load_with_consumer(
            path,
            cursor_path,
            max_bytes,
            DEFAULT_PRIMARY_CONSUMER_ID.to_string(),
        )
    }

    fn load_with_consumer(
        path: impl AsRef<Path>,
        cursor_path: impl AsRef<Path>,
        max_bytes: u64,
        primary_consumer_id: String,
    ) -> Result<Self, String> {
        if max_bytes < MIN_TELEMETRY_JOURNAL_MAX_BYTES {
            return Err(format!(
                "telemetry journal cap must be at least {MIN_TELEMETRY_JOURNAL_MAX_BYTES} bytes"
            ));
        }
        let primary_consumer_id = primary_consumer_id.trim().to_string();
        if primary_consumer_id.is_empty() || primary_consumer_id.len() > 128 {
            return Err("telemetry primary consumer id is invalid".to_string());
        }
        let path = path.as_ref().to_path_buf();
        let cursor_path = cursor_path.as_ref().to_path_buf();
        recover_interrupted_compaction(&path)?;
        let cursor = load_latest_cursor(&cursor_path)?;
        let acknowledged_high_water = cursor
            .as_ref()
            .map(|value| value.high_water_sequence)
            .unwrap_or(0);
        let cursor_generation = cursor.as_ref().map(|value| value.generation).unwrap_or(0);

        let mut events = VecDeque::new();
        let mut last_sequence = 0_u64;
        let mut records_on_disk = 0_usize;
        if path.exists() {
            let file =
                File::open(&path).map_err(|error| format!("open telemetry journal: {error}"))?;
            for (line_number, line) in StdBufReader::new(file).lines().enumerate() {
                let line = line.map_err(|error| format!("read telemetry journal: {error}"))?;
                if line.trim().is_empty() {
                    continue;
                }
                let record: StoredTelemetryEvent =
                    serde_json::from_str(&line).map_err(|error| {
                        format!(
                            "invalid telemetry journal line {}: {error}",
                            line_number + 1
                        )
                    })?;
                validate_stored_event(&record).map_err(|error| {
                    format!(
                        "invalid telemetry journal line {}: {error}",
                        line_number + 1
                    )
                })?;
                if record.sequence <= last_sequence {
                    return Err(format!(
                        "non-monotonic telemetry sequence on line {}: {} <= {}",
                        line_number + 1,
                        record.sequence,
                        last_sequence
                    ));
                }
                last_sequence = record.sequence;
                records_on_disk += 1;
                if record.sequence > acknowledged_high_water {
                    events.push_back(record);
                }
            }
        }
        if acknowledged_high_water > last_sequence && last_sequence > 0 {
            return Err(format!(
                "telemetry ACK cursor {} is ahead of durable journal {}",
                acknowledged_high_water, last_sequence
            ));
        }
        let next_sequence = last_sequence
            .max(acknowledged_high_water)
            .checked_add(1)
            .ok_or_else(|| "telemetry sequence exhausted".to_string())?;
        Ok(Self {
            path,
            cursor_path,
            max_bytes,
            events,
            next_sequence,
            acknowledged_high_water,
            cursor_generation,
            records_on_disk,
            primary_consumer_id,
        })
    }

    pub(crate) fn prepare_recovery_snapshot(&self) -> Result<TelemetryRecoverySnapshot, String> {
        if self.path.exists() {
            let metadata = std::fs::symlink_metadata(&self.path).map_err(|error| {
                format!("inspect telemetry journal for recovery barrier: {error}")
            })?;
            if metadata.file_type().is_symlink() || !metadata.is_file() {
                return Err("telemetry journal is not a regular recovery source".to_string());
            }
            OpenOptions::new()
                .read(true)
                .write(true)
                .open(&self.path)
                .map_err(|error| format!("open telemetry journal for recovery barrier: {error}"))?
                .sync_all()
                .map_err(|error| format!("sync telemetry journal for recovery barrier: {error}"))?;
        }
        let cursor = load_latest_cursor(&self.cursor_path)?;
        let (active_cursor_path, active_cursor_bytes, active_cursor_suffix) = match cursor.as_ref()
        {
            Some(cursor)
                if cursor.generation == self.cursor_generation
                    && cursor.high_water_sequence == self.acknowledged_high_water =>
            {
                let suffix = if cursor.generation.is_multiple_of(2) {
                    ".a"
                } else {
                    ".b"
                };
                let path = path_with_suffix(&self.cursor_path, suffix);
                let metadata = std::fs::symlink_metadata(&path).map_err(|error| {
                    format!("inspect active telemetry cursor for recovery barrier: {error}")
                })?;
                if metadata.file_type().is_symlink() || !metadata.is_file() {
                    return Err(
                        "active telemetry cursor is not a regular recovery source".to_string()
                    );
                }
                OpenOptions::new()
                    .read(true)
                    .write(true)
                    .open(&path)
                    .map_err(|error| {
                        format!("open active telemetry cursor for recovery barrier: {error}")
                    })?
                    .sync_all()
                    .map_err(|error| {
                        format!("sync active telemetry cursor for recovery barrier: {error}")
                    })?;
                let bytes = std::fs::read(&path)
                    .map_err(|error| format!("read active telemetry cursor: {error}"))?;
                (Some(path), bytes, suffix.to_string())
            }
            None if self.cursor_generation == 0 && self.acknowledged_high_water == 0 => {
                let zero_cursor = TelemetryCursor {
                    schema_version: TELEMETRY_SCHEMA_VERSION,
                    generation: 0,
                    high_water_sequence: 0,
                    consumer_id: self.primary_consumer_id.clone(),
                    checksum: telemetry_cursor_checksum(0, 0, &self.primary_consumer_id),
                };
                let bytes = serde_json::to_vec(&zero_cursor)
                    .map_err(|error| format!("encode zero telemetry cursor: {error}"))?;
                (None, bytes, ".a".to_string())
            }
            Some(cursor) => {
                return Err(format!(
                    "telemetry cursor memory/disk mismatch: memory_generation={}, disk_generation={}, memory_ack={}, disk_ack={}",
                    self.cursor_generation,
                    cursor.generation,
                    self.acknowledged_high_water,
                    cursor.high_water_sequence
                ));
            }
            None => {
                return Err("telemetry ACK cursor is missing at recovery barrier".to_string());
            }
        };
        #[cfg(unix)]
        if let Some(parent) = self.path.parent().filter(|parent| parent.exists()) {
            File::open(parent)
                .map_err(|error| format!("open telemetry directory for barrier: {error}"))?
                .sync_all()
                .map_err(|error| format!("sync telemetry directory for barrier: {error}"))?;
        }
        #[cfg(unix)]
        if self.cursor_path.parent() != self.path.parent()
            && let Some(parent) = self.cursor_path.parent().filter(|parent| parent.exists())
        {
            File::open(parent)
                .map_err(|error| format!("open telemetry cursor directory: {error}"))?
                .sync_all()
                .map_err(|error| format!("sync telemetry cursor directory: {error}"))?;
        }
        Ok(TelemetryRecoverySnapshot {
            journal_path: self.path.clone(),
            active_cursor_path,
            active_cursor_bytes,
            active_cursor_suffix,
            published_high_water_sequence: self.published_high_water(),
            acknowledged_high_water_sequence: self.acknowledged_high_water,
            cursor_generation: self.cursor_generation,
        })
    }

    #[cfg(test)]
    pub(crate) fn load_for_recovery_test(
        path: impl AsRef<Path>,
        cursor_path: impl AsRef<Path>,
    ) -> Result<Self, String> {
        Self::load(path, cursor_path, 128 * 1024)
    }

    fn publish(&mut self, raw_payload: &[u8]) -> Result<TelemetryFrame, String> {
        let mut payload: Value = rmp_serde::from_slice(raw_payload)
            .map_err(|error| format!("decode outbound telemetry: {error}"))?;
        if !telemetry_event_is_durable(&payload) {
            return Ok(TelemetryFrame {
                sequence: None,
                payload: raw_payload.to_vec(),
            });
        }
        let sequence = self.next_sequence;
        decorate_payload(&mut payload, sequence, false)?;
        let checksum = telemetry_event_checksum(sequence, &payload)?;
        let record = StoredTelemetryEvent {
            schema_version: TELEMETRY_SCHEMA_VERSION,
            sequence,
            payload: payload.clone(),
            checksum,
        };
        self.append_record(&record)?;
        self.events.push_back(record);
        self.records_on_disk += 1;
        self.next_sequence = sequence
            .checked_add(1)
            .ok_or_else(|| "telemetry sequence exhausted".to_string())?;
        let encoded = rmp_serde::to_vec_named(&payload)
            .map_err(|error| format!("encode sequenced telemetry: {error}"))?;
        Ok(TelemetryFrame {
            sequence: Some(sequence),
            payload: encoded,
        })
    }

    fn replay_frames(&self) -> Result<Vec<TelemetryFrame>, String> {
        self.events
            .iter()
            .map(|record| {
                let mut payload = record.payload.clone();
                decorate_payload(&mut payload, record.sequence, true)?;
                let encoded = rmp_serde::to_vec_named(&payload)
                    .map_err(|error| format!("encode replay telemetry: {error}"))?;
                Ok(TelemetryFrame {
                    sequence: Some(record.sequence),
                    payload: encoded,
                })
            })
            .collect()
    }

    fn published_high_water(&self) -> u64 {
        self.next_sequence.saturating_sub(1)
    }

    fn acknowledge(&mut self, ack: &TelemetryAck) -> Result<(), String> {
        if ack.event != "TelemetryAck" {
            return Err("telemetry ACK event must be TelemetryAck".to_string());
        }
        if ack.schema_version != TELEMETRY_SCHEMA_VERSION {
            return Err(format!(
                "unsupported telemetry ACK schema {}",
                ack.schema_version
            ));
        }
        let consumer_id = ack.consumer_id.trim();
        if consumer_id.is_empty() || consumer_id.len() > 128 {
            return Err("telemetry ACK consumer_id is invalid".to_string());
        }
        if consumer_id != self.primary_consumer_id {
            return Err(
                "telemetry ACK consumer_id is not the configured primary consumer".to_string(),
            );
        }
        if ack.high_water_sequence > self.published_high_water() {
            return Err(format!(
                "telemetry ACK {} is ahead of published high-water {}",
                ack.high_water_sequence,
                self.published_high_water()
            ));
        }
        if ack.high_water_sequence <= self.acknowledged_high_water {
            return Ok(());
        }
        let next_generation = self
            .cursor_generation
            .checked_add(1)
            .ok_or_else(|| "telemetry cursor generation exhausted".to_string())?;
        persist_cursor(
            &self.cursor_path,
            TelemetryCursor {
                schema_version: TELEMETRY_SCHEMA_VERSION,
                generation: next_generation,
                high_water_sequence: ack.high_water_sequence,
                consumer_id: consumer_id.to_string(),
                checksum: telemetry_cursor_checksum(
                    next_generation,
                    ack.high_water_sequence,
                    consumer_id,
                ),
            },
        )?;
        self.cursor_generation = next_generation;
        self.acknowledged_high_water = ack.high_water_sequence;
        while self
            .events
            .front()
            .is_some_and(|record| record.sequence <= ack.high_water_sequence)
        {
            self.events.pop_front();
        }
        if self.records_on_disk > self.events.len().saturating_add(COMPACTION_RECORD_SLACK)
            || self
                .path
                .metadata()
                .map(|metadata| metadata.len() >= self.max_bytes / 2)
                .unwrap_or(false)
        {
            self.compact()?;
        }
        Ok(())
    }

    fn append_record(&mut self, record: &StoredTelemetryEvent) -> Result<(), String> {
        ensure_parent(&self.path)?;
        let encoded = serde_json::to_vec(record)
            .map_err(|error| format!("encode telemetry journal record: {error}"))?;
        let current_bytes = file_len_or_zero(&self.path)?;
        let projected = current_bytes
            .saturating_add(encoded.len() as u64)
            .saturating_add(1);
        if projected > self.max_bytes && self.records_on_disk > self.events.len() {
            self.compact()?;
        }
        let current_bytes = file_len_or_zero(&self.path)?;
        let projected = current_bytes
            .saturating_add(encoded.len() as u64)
            .saturating_add(1);
        if projected > self.max_bytes {
            return Err(format!(
                "telemetry journal byte budget exhausted: projected={projected}, limit={}",
                self.max_bytes
            ));
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|error| format!("append telemetry journal: {error}"))?;
        file.write_all(&encoded)
            .map_err(|error| format!("write telemetry journal: {error}"))?;
        file.write_all(b"\n")
            .map_err(|error| format!("write telemetry journal delimiter: {error}"))?;
        file.sync_data()
            .map_err(|error| format!("sync telemetry journal: {error}"))
    }

    fn compact(&mut self) -> Result<(), String> {
        ensure_parent(&self.path)?;
        let next_path = path_with_suffix(&self.path, ".next");
        let previous_path = path_with_suffix(&self.path, ".previous");
        {
            let mut next = OpenOptions::new()
                .create(true)
                .truncate(true)
                .write(true)
                .open(&next_path)
                .map_err(|error| format!("create telemetry compaction file: {error}"))?;
            for record in &self.events {
                serde_json::to_writer(&mut next, record)
                    .map_err(|error| format!("encode telemetry compaction record: {error}"))?;
                next.write_all(b"\n")
                    .map_err(|error| format!("write telemetry compaction record: {error}"))?;
            }
            next.sync_all()
                .map_err(|error| format!("sync telemetry compaction file: {error}"))?;
        }
        if previous_path.exists() {
            std::fs::remove_file(&previous_path)
                .map_err(|error| format!("remove stale telemetry checkpoint: {error}"))?;
        }
        if self.path.exists() {
            std::fs::rename(&self.path, &previous_path)
                .map_err(|error| format!("rotate telemetry journal: {error}"))?;
        }
        if let Err(error) = std::fs::rename(&next_path, &self.path) {
            if !self.path.exists() && previous_path.exists() {
                let _ = std::fs::rename(&previous_path, &self.path);
            }
            return Err(format!("install compacted telemetry journal: {error}"));
        }
        if previous_path.exists() {
            let _ = std::fs::remove_file(previous_path);
        }
        self.records_on_disk = self.events.len();
        Ok(())
    }
}

#[allow(clippy::too_many_arguments)]
async fn relay_payload(
    raw_payload: Vec<u8>,
    clients: &broadcast::Sender<TelemetryFrame>,
    journal: &Arc<Mutex<TelemetryJournal>>,
    ws_sender: &mpsc::Sender<WsEvent>,
    futures_control: &mpsc::Sender<PrivateStreamControl>,
    spot_control: &mpsc::Sender<PrivateStreamControl>,
    persistence_failure_latched: &mut bool,
) -> Result<(), String> {
    let publish_result = journal.lock().await.publish(&raw_payload);
    match publish_result {
        Ok(frame) => {
            // A broadcast send is not a durable handoff. Only the successful
            // journal append above releases the execution actor's outbox.
            // Lost/overfull internal ACKs are safe: the actor replays the same
            // publication identity and Python deduplicates its business effect.
            if let Ok(payload) = rmp_serde::from_slice::<Value>(&frame.payload)
                && let Some(publication_id) = payload.get("publication_id").and_then(Value::as_str)
                && frame.sequence.is_some()
            {
                let _ = ws_sender.try_send(WsEvent::TerminalPublicationPersisted {
                    publication_id: publication_id.to_string(),
                });
            }
            if *persistence_failure_latched {
                *persistence_failure_latched = false;
                let recovered = serde_json::json!({
                    "event": "TelemetryPersistenceRecovered",
                    "event_time_ms": current_time_ms(),
                });
                if let Ok(encoded) = rmp_serde::to_vec_named(&recovered)
                    && let Ok(frame) = journal.lock().await.publish(&encoded)
                {
                    let _ = clients.send(frame);
                }
            }
            let _ = clients.send(frame);
        }
        Err(error) => {
            tracing::error!("Durable telemetry persistence failed: {}", error);
            if !*persistence_failure_latched {
                *persistence_failure_latched = true;
                report_gap(
                    1,
                    "telemetry_journal_unavailable",
                    ws_sender,
                    futures_control,
                    spot_control,
                )
                .await;
                let failure = serde_json::json!({
                    "event": "TelemetryPersistenceError",
                    "reason": "telemetry_journal_unavailable",
                    "event_time_ms": current_time_ms(),
                });
                if let Ok(encoded) = rmp_serde::to_vec_named(&failure) {
                    let _ = clients.send(TelemetryFrame {
                        sequence: None,
                        payload: encoded,
                    });
                }
            }
            // Preserve live delivery for an already-connected consumer. The
            // actor is simultaneously forced into reconciliation, so an
            // unjournaled event can never coexist with new-risk READY.
            let _ = clients.send(TelemetryFrame {
                sequence: None,
                payload: raw_payload,
            });
        }
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub async fn run_telemetry_relay(
    mut source: broadcast::Receiver<Vec<u8>>,
    clients: broadcast::Sender<TelemetryFrame>,
    journal: Arc<Mutex<TelemetryJournal>>,
    ws_sender: mpsc::Sender<WsEvent>,
    futures_control: mpsc::Sender<PrivateStreamControl>,
    spot_control: mpsc::Sender<PrivateStreamControl>,
    mut recovery_control: mpsc::Receiver<TelemetryRelayControl>,
) -> Result<(), String> {
    let mut persistence_failure_latched = false;
    let mut recovery_control_open = true;
    loop {
        tokio::select! {
            biased;
            control = recovery_control.recv(), if recovery_control_open => {
                let Some(TelemetryRelayControl::RecoveryBarrier {
                    request_id,
                    reply,
                    release,
                    resumed,
                }) = control else {
                    recovery_control_open = false;
                    continue;
                };
                if request_id.is_empty() || request_id.len() > 128 {
                    let _ = reply.send(Err("invalid telemetry recovery barrier request id".to_string()));
                    let _ = resumed.send(());
                    continue;
                }
                // The order actor is already paused before this command is
                // sent. Drain every event it emitted before acknowledging the
                // cut, then retain the journal mutex until publication ends.
                loop {
                    match source.try_recv() {
                        Ok(payload) => relay_payload(
                            payload,
                            &clients,
                            &journal,
                            &ws_sender,
                            &futures_control,
                            &spot_control,
                            &mut persistence_failure_latched,
                        ).await?,
                        Err(broadcast::error::TryRecvError::Empty) => break,
                        Err(broadcast::error::TryRecvError::Lagged(skipped)) => {
                            report_gap(
                                skipped,
                                "telemetry_relay_source_overflow_at_recovery_barrier",
                                &ws_sender,
                                &futures_control,
                                &spot_control,
                            ).await;
                            let marker = direct_gap_frame(
                                skipped,
                                "telemetry_relay_source_overflow_at_recovery_barrier",
                            )?;
                            let _ = clients.send(marker);
                        }
                        Err(broadcast::error::TryRecvError::Closed) => {
                            let _ = reply.send(Err("telemetry source closed at recovery barrier".to_string()));
                            let _ = resumed.send(());
                            return Err("telemetry source broadcast closed".to_string());
                        }
                    }
                }
                let guard = journal.lock().await;
                let snapshot = guard.prepare_recovery_snapshot();
                if reply.send(snapshot).is_err() {
                    drop(guard);
                    let _ = resumed.send(());
                    continue;
                }
                let _ = release.await;
                drop(guard);
                let _ = resumed.send(());
            }
            payload = source.recv() => {
                let raw_payload = match payload {
                    Ok(payload) => payload,
                    Err(broadcast::error::RecvError::Lagged(skipped)) => {
                        report_gap(
                            skipped,
                            "telemetry_relay_source_overflow",
                            &ws_sender,
                            &futures_control,
                            &spot_control,
                        ).await;
                        let marker = direct_gap_frame(skipped, "telemetry_relay_source_overflow")?;
                        let _ = clients.send(marker);
                        continue;
                    }
                    Err(broadcast::error::RecvError::Closed) => {
                        return Err("telemetry source broadcast closed".to_string());
                    }
                };
                relay_payload(
                    raw_payload,
                    &clients,
                    &journal,
                    &ws_sender,
                    &futures_control,
                    &spot_control,
                    &mut persistence_failure_latched,
                ).await?;
            }
        }
    }
}

pub async fn run_telemetry_server(
    listener: TcpListener,
    clients: broadcast::Sender<TelemetryFrame>,
    journal: Arc<Mutex<TelemetryJournal>>,
    ws_sender: mpsc::Sender<WsEvent>,
    futures_control: mpsc::Sender<PrivateStreamControl>,
    spot_control: mpsc::Sender<PrivateStreamControl>,
) -> Result<(), String> {
    tracing::info!("Sequenced telemetry server listening on 127.0.0.1:9000");
    loop {
        let (socket, _) = listener
            .accept()
            .await
            .map_err(|error| format!("telemetry accept failed: {error}"))?;
        let receiver = clients.subscribe();
        let client_journal = journal.clone();
        let ws_sender = ws_sender.clone();
        let futures_control = futures_control.clone();
        let spot_control = spot_control.clone();
        tokio::spawn(async move {
            if let Err(error) = run_client(
                socket,
                receiver,
                client_journal,
                ws_sender,
                futures_control,
                spot_control,
            )
            .await
            {
                tracing::warn!("Telemetry client closed: {}", error);
            }
        });
    }
}

async fn run_client(
    socket: TcpStream,
    mut receiver: broadcast::Receiver<TelemetryFrame>,
    journal: Arc<Mutex<TelemetryJournal>>,
    ws_sender: mpsc::Sender<WsEvent>,
    futures_control: mpsc::Sender<PrivateStreamControl>,
    spot_control: mpsc::Sender<PrivateStreamControl>,
) -> Result<(), String> {
    // Subscribe happens before this snapshot is taken. Any event present in
    // both the replay snapshot and the live receiver is suppressed below by
    // sequence, closing the connect/replay race without dropping either side.
    let replay = journal.lock().await.replay_frames()?;
    let (reader, mut writer) = socket.into_split();
    let mut reader = BufReader::new(reader);
    let mut delivered_high_water = 0_u64;
    for frame in replay {
        writer
            .write_all(&frame.payload)
            .await
            .map_err(|error| format!("write telemetry replay: {error}"))?;
        if let Some(sequence) = frame.sequence {
            delivered_high_water = delivered_high_water.max(sequence);
        }
    }

    let mut ack_line = Vec::with_capacity(256);
    loop {
        tokio::select! {
            frame = receiver.recv() => {
                match frame {
                    Ok(frame) => {
                        if frame.sequence.is_some_and(|sequence| sequence <= delivered_high_water) {
                            continue;
                        }
                        writer.write_all(&frame.payload).await
                            .map_err(|error| format!("write live telemetry: {error}"))?;
                        if let Some(sequence) = frame.sequence {
                            delivered_high_water = sequence;
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(skipped)) => {
                        let marker = direct_gap_frame(skipped, "telemetry_client_overflow")?;
                        writer.write_all(&marker.payload).await
                            .map_err(|error| format!("write telemetry gap: {error}"))?;
                        report_gap(
                            skipped,
                            "telemetry_client_overflow",
                            &ws_sender,
                            &futures_control,
                            &spot_control,
                        ).await;
                        return Err(format!("telemetry client lagged by {skipped} messages"));
                    }
                    Err(broadcast::error::RecvError::Closed) => {
                        return Err("telemetry client broadcast closed".to_string());
                    }
                }
            }
            read = reader.read_until(b'\n', &mut ack_line) => {
                let bytes = read.map_err(|error| format!("read telemetry ACK: {error}"))?;
                if bytes == 0 {
                    return Ok(());
                }
                if ack_line.len() > MAX_ACK_LINE_BYTES {
                    return Err("telemetry ACK line exceeded safety cap".to_string());
                }
                let ack: TelemetryAck = serde_json::from_slice(&ack_line)
                    .map_err(|error| format!("invalid telemetry ACK JSON: {error}"))?;
                if ack.high_water_sequence > delivered_high_water {
                    return Err(format!(
                        "telemetry ACK {} exceeds this connection's delivered high-water {}",
                        ack.high_water_sequence,
                        delivered_high_water
                    ));
                }
                journal.lock().await.acknowledge(&ack)?;
                ack_line.clear();
            }
        }
    }
}

async fn report_gap(
    skipped: u64,
    reason: &str,
    ws_sender: &mpsc::Sender<WsEvent>,
    futures_control: &mpsc::Sender<PrivateStreamControl>,
    spot_control: &mpsc::Sender<PrivateStreamControl>,
) {
    let _ = ws_sender
        .send(WsEvent::TelemetryGap {
            skipped_messages: skipped,
            reason: reason.to_string(),
            event_time_ms: current_time_ms(),
        })
        .await;
    let replay = PrivateStreamControl::ReplayFromCursor {
        reason: reason.to_string(),
    };
    let _ = futures_control.try_send(replay.clone());
    let _ = spot_control.try_send(replay);
}

fn direct_gap_frame(skipped: u64, reason: &str) -> Result<TelemetryFrame, String> {
    let gap = WsEvent::TelemetryGap {
        skipped_messages: skipped,
        reason: reason.to_string(),
        event_time_ms: current_time_ms(),
    };
    let payload = rmp_serde::to_vec_named(&gap)
        .map_err(|error| format!("encode telemetry gap marker: {error}"))?;
    Ok(TelemetryFrame {
        sequence: None,
        payload,
    })
}

fn telemetry_event_is_durable(payload: &Value) -> bool {
    let event = payload
        .get("event")
        .and_then(Value::as_str)
        .unwrap_or_default();
    !matches!(
        event,
        "BookTicker" | "L2Depth" | "MarkPrice" | "VolumeBar" | "PositionPnL"
    )
}

fn decorate_payload(payload: &mut Value, sequence: u64, replay: bool) -> Result<(), String> {
    let object = payload
        .as_object_mut()
        .ok_or_else(|| "outbound telemetry must be a MessagePack map".to_string())?;
    object.insert(
        "telemetry_schema_version".to_string(),
        Value::from(TELEMETRY_SCHEMA_VERSION),
    );
    object.insert("telemetry_sequence".to_string(), Value::from(sequence));
    object.insert("telemetry_ack_required".to_string(), Value::Bool(true));
    object.insert("telemetry_replay".to_string(), Value::Bool(replay));
    if object.get("terminal_summary_version").is_some() {
        object.insert("terminal_sequence".to_string(), Value::from(sequence));
        object.insert("terminal_watermark".to_string(), Value::from(sequence));
    }
    Ok(())
}

fn telemetry_event_checksum(sequence: u64, payload: &Value) -> Result<String, String> {
    let encoded = serde_json::to_vec(payload)
        .map_err(|error| format!("canonicalize telemetry checksum payload: {error}"))?;
    let mut digest = Sha256::new();
    digest.update(b"bongus.telemetry.event.v1\0");
    digest.update(sequence.to_be_bytes());
    digest.update(encoded);
    Ok(hex::encode(digest.finalize()))
}

fn validate_stored_event(record: &StoredTelemetryEvent) -> Result<(), String> {
    if record.schema_version != TELEMETRY_SCHEMA_VERSION || record.sequence == 0 {
        return Err("unsupported telemetry record identity".to_string());
    }
    let object = record
        .payload
        .as_object()
        .ok_or_else(|| "telemetry record payload is not an object".to_string())?;
    if object.get("telemetry_sequence").and_then(Value::as_u64) != Some(record.sequence)
        || object
            .get("telemetry_schema_version")
            .and_then(Value::as_u64)
            != Some(TELEMETRY_SCHEMA_VERSION as u64)
        || object.get("telemetry_replay").and_then(Value::as_bool) != Some(false)
        || object
            .get("telemetry_ack_required")
            .and_then(Value::as_bool)
            != Some(true)
    {
        return Err("telemetry record wire metadata is inconsistent".to_string());
    }
    let expected = telemetry_event_checksum(record.sequence, &record.payload)?;
    if record.checksum != expected {
        return Err("telemetry record checksum mismatch".to_string());
    }
    Ok(())
}

fn telemetry_cursor_checksum(generation: u64, high_water: u64, consumer_id: &str) -> String {
    let mut digest = Sha256::new();
    digest.update(b"bongus.telemetry.cursor.v1\0");
    digest.update(generation.to_be_bytes());
    digest.update(high_water.to_be_bytes());
    digest.update(consumer_id.as_bytes());
    hex::encode(digest.finalize())
}

fn validate_cursor(cursor: &TelemetryCursor) -> bool {
    cursor.schema_version == TELEMETRY_SCHEMA_VERSION
        && !cursor.consumer_id.trim().is_empty()
        && cursor.consumer_id.len() <= 128
        && cursor.checksum
            == telemetry_cursor_checksum(
                cursor.generation,
                cursor.high_water_sequence,
                &cursor.consumer_id,
            )
}

fn load_latest_cursor(path: &Path) -> Result<Option<TelemetryCursor>, String> {
    let mut valid = Vec::new();
    for suffix in [".a", ".b"] {
        let candidate = path_with_suffix(path, suffix);
        if !candidate.exists() {
            continue;
        }
        let metadata = candidate
            .metadata()
            .map_err(|error| format!("inspect telemetry cursor: {error}"))?;
        if metadata.len() > MAX_CURSOR_RECORD_BYTES as u64 {
            continue;
        }
        let Ok(bytes) = std::fs::read(&candidate) else {
            continue;
        };
        let Ok(cursor) = serde_json::from_slice::<TelemetryCursor>(&bytes) else {
            continue;
        };
        if validate_cursor(&cursor) {
            valid.push(cursor);
        }
    }
    Ok(valid.into_iter().max_by_key(|cursor| cursor.generation))
}

fn persist_cursor(path: &Path, cursor: TelemetryCursor) -> Result<(), String> {
    ensure_parent(path)?;
    let encoded =
        serde_json::to_vec(&cursor).map_err(|error| format!("encode telemetry cursor: {error}"))?;
    if encoded.len() > MAX_CURSOR_RECORD_BYTES {
        return Err("telemetry cursor exceeded byte cap".to_string());
    }
    let suffix = if cursor.generation.is_multiple_of(2) {
        ".a"
    } else {
        ".b"
    };
    let target = path_with_suffix(path, suffix);
    let mut file = OpenOptions::new()
        .create(true)
        .truncate(true)
        .write(true)
        .open(&target)
        .map_err(|error| format!("open telemetry cursor: {error}"))?;
    file.write_all(&encoded)
        .map_err(|error| format!("write telemetry cursor: {error}"))?;
    file.sync_all()
        .map_err(|error| format!("sync telemetry cursor: {error}"))
}

fn recover_interrupted_compaction(path: &Path) -> Result<(), String> {
    if path.exists() {
        return Ok(());
    }
    let previous = path_with_suffix(path, ".previous");
    if previous.exists() {
        std::fs::rename(&previous, path)
            .map_err(|error| format!("recover prior telemetry journal: {error}"))?;
    }
    Ok(())
}

fn ensure_parent(path: &Path) -> Result<(), String> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        std::fs::create_dir_all(parent)
            .map_err(|error| format!("create telemetry journal directory: {error}"))?;
    }
    Ok(())
}

fn file_len_or_zero(path: &Path) -> Result<u64, String> {
    match path.metadata() {
        Ok(metadata) => Ok(metadata.len()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(0),
        Err(error) => Err(format!("inspect telemetry journal size: {error}")),
    }
}

fn path_with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(suffix);
    PathBuf::from(value)
}

fn current_time_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| i64::try_from(duration.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::time::Duration;

    fn test_paths(label: &str) -> (PathBuf, PathBuf) {
        static NEXT: AtomicU64 = AtomicU64::new(1);
        let id = NEXT.fetch_add(1, Ordering::Relaxed);
        let root = std::env::temp_dir().join(format!(
            "bongus-telemetry-{label}-{}-{id}",
            std::process::id()
        ));
        (root.with_extension("jsonl"), root.with_extension("cursor"))
    }

    fn order_update(client_order_id: &str) -> Vec<u8> {
        rmp_serde::to_vec_named(&serde_json::json!({
            "event": "OrderUpdate",
            "client_order_id": client_order_id,
            "status": "FILLED",
            "filled_qty": 0.1,
        }))
        .unwrap()
    }

    #[test]
    fn disconnected_fill_replays_same_sequence_until_durable_ack() {
        let (journal_path, cursor_path) = test_paths("replay");
        let mut journal = TelemetryJournal::load(&journal_path, &cursor_path, 128 * 1024).unwrap();
        let live = journal.publish(&order_update("fill-1")).unwrap();
        assert_eq!(live.sequence, Some(1));
        let live_payload: Value = rmp_serde::from_slice(&live.payload).unwrap();
        assert_eq!(live_payload["telemetry_sequence"], 1);
        assert_eq!(live_payload["telemetry_replay"], false);
        drop(journal);

        let mut restarted =
            TelemetryJournal::load(&journal_path, &cursor_path, 128 * 1024).unwrap();
        let replay = restarted.replay_frames().unwrap();
        assert_eq!(replay.len(), 1);
        assert_eq!(replay[0].sequence, Some(1));
        let replay_payload: Value = rmp_serde::from_slice(&replay[0].payload).unwrap();
        assert_eq!(replay_payload["telemetry_replay"], true);
        assert_eq!(replay_payload["client_order_id"], "fill-1");

        restarted
            .acknowledge(&TelemetryAck {
                event: "TelemetryAck".to_string(),
                schema_version: TELEMETRY_SCHEMA_VERSION,
                consumer_id: "python-live-trader".to_string(),
                high_water_sequence: 1,
            })
            .unwrap();
        drop(restarted);

        let mut acknowledged =
            TelemetryJournal::load(&journal_path, &cursor_path, 128 * 1024).unwrap();
        assert!(acknowledged.replay_frames().unwrap().is_empty());
        assert_eq!(
            acknowledged
                .publish(&order_update("fill-2"))
                .unwrap()
                .sequence,
            Some(2)
        );
    }

    #[tokio::test]
    async fn terminal_handoff_ack_requires_successful_journal_persistence() {
        for (label, max_bytes, expect_ack) in [
            ("terminal-handoff", 128 * 1024, true),
            ("terminal-full", 32, false),
        ] {
            let (journal_path, cursor_path) = test_paths(label);
            let mut loaded =
                TelemetryJournal::load(&journal_path, &cursor_path, 128 * 1024).unwrap();
            loaded.max_bytes = max_bytes; // Inject exhaustion after valid startup.
            let journal = Arc::new(Mutex::new(loaded));
            let (clients, _client_rx) = broadcast::channel(8);
            let (ws_tx, mut ws_rx) = mpsc::channel(8);
            let (futures_tx, _futures_rx) = mpsc::channel(8);
            let (spot_tx, _spot_rx) = mpsc::channel(8);
            let payload = rmp_serde::to_vec_named(&serde_json::json!({
                "event": "OrderUpdate", "publication_id": "terminal-fixture", "status": "FILLED",
            }))
            .unwrap();
            let mut latched = false;
            relay_payload(
                payload,
                &clients,
                &journal,
                &ws_tx,
                &futures_tx,
                &spot_tx,
                &mut latched,
            )
            .await
            .unwrap();
            let event = ws_rx.recv().await.unwrap();
            assert_eq!(
                matches!(event, WsEvent::TerminalPublicationPersisted { .. }),
                expect_ack
            );
            if expect_ack {
                drop(journal);
                let recovered =
                    TelemetryJournal::load(&journal_path, &cursor_path, max_bytes).unwrap();
                assert_eq!(recovered.replay_frames().unwrap().len(), 1);
            } else {
                assert!(latched);
            }
            let _ = std::fs::remove_file(journal_path);
        }
    }

    #[test]
    fn market_data_remains_backward_compatible_and_is_not_journaled() {
        let (journal_path, cursor_path) = test_paths("ephemeral");
        let mut journal = TelemetryJournal::load(&journal_path, &cursor_path, 128 * 1024).unwrap();
        let raw = rmp_serde::to_vec_named(&serde_json::json!({
            "event": "MarkPrice",
            "symbol": "BTCUSDT",
            "mark_price": 60000.0,
        }))
        .unwrap();
        let frame = journal.publish(&raw).unwrap();
        assert_eq!(frame.sequence, None);
        assert_eq!(frame.payload, raw);
        assert!(!journal_path.exists());
    }

    #[test]
    fn ack_cannot_skip_undelivered_or_unpublished_events() {
        let (journal_path, cursor_path) = test_paths("ack-bound");
        let mut journal = TelemetryJournal::load(&journal_path, &cursor_path, 128 * 1024).unwrap();
        journal.publish(&order_update("fill-1")).unwrap();
        let error = journal
            .acknowledge(&TelemetryAck {
                event: "TelemetryAck".to_string(),
                schema_version: TELEMETRY_SCHEMA_VERSION,
                consumer_id: "python-live-trader".to_string(),
                high_water_sequence: 2,
            })
            .unwrap_err();
        assert!(error.contains("ahead of published"));
        assert_eq!(journal.replay_frames().unwrap().len(), 1);

        let observer_error = journal
            .acknowledge(&TelemetryAck {
                event: "TelemetryAck".to_string(),
                schema_version: TELEMETRY_SCHEMA_VERSION,
                consumer_id: "dashboard-observer".to_string(),
                high_water_sequence: 1,
            })
            .unwrap_err();
        assert!(observer_error.contains("not the configured primary"));
        assert_eq!(journal.replay_frames().unwrap().len(), 1);
    }

    #[test]
    fn acknowledged_records_compact_within_the_hard_cap() {
        let (journal_path, cursor_path) = test_paths("compact");
        let journal_cap = 512 * 1024;
        let mut journal = TelemetryJournal::load(&journal_path, &cursor_path, journal_cap).unwrap();
        for index in 0..1100 {
            let payload = rmp_serde::to_vec_named(&serde_json::json!({
                "event": "IntentAck",
                "intent_id": format!("intent-{index:04}"),
                "padding": "x".repeat(16),
            }))
            .unwrap();
            journal.publish(&payload).unwrap();
        }
        journal
            .acknowledge(&TelemetryAck {
                event: "TelemetryAck".to_string(),
                schema_version: TELEMETRY_SCHEMA_VERSION,
                consumer_id: "python-live-trader".to_string(),
                high_water_sequence: 1100,
            })
            .unwrap();
        assert!(journal.replay_frames().unwrap().is_empty());
        assert!(file_len_or_zero(&journal_path).unwrap() <= journal_cap);
        assert_eq!(
            journal
                .publish(&order_update("after-compaction"))
                .unwrap()
                .sequence,
            Some(1101)
        );
    }

    #[tokio::test]
    async fn recovery_barrier_drains_prior_events_and_holds_the_journal_cut() {
        let (journal_path, cursor_path) = test_paths("recovery-cut");
        let journal = Arc::new(Mutex::new(
            TelemetryJournal::load(&journal_path, &cursor_path, 128 * 1024).unwrap(),
        ));
        let (source_tx, source_rx) = broadcast::channel(16);
        let (clients_tx, _) = broadcast::channel(16);
        let (ws_tx, _ws_rx) = mpsc::channel(4);
        let (futures_control_tx, _futures_control_rx) = mpsc::channel(1);
        let (spot_control_tx, _spot_control_rx) = mpsc::channel(1);
        let (recovery_tx, recovery_rx) = mpsc::channel(1);
        let relay_journal = journal.clone();
        let relay = tokio::spawn(async move {
            run_telemetry_relay(
                source_rx,
                clients_tx,
                relay_journal,
                ws_tx,
                futures_control_tx,
                spot_control_tx,
                recovery_rx,
            )
            .await
        });
        for intent_id in ["before-1", "before-2"] {
            source_tx
                .send(
                    rmp_serde::to_vec_named(&serde_json::json!({
                        "event": "IntentAck",
                        "intent_id": intent_id,
                    }))
                    .unwrap(),
                )
                .unwrap();
        }
        let (reply_tx, reply_rx) = oneshot::channel();
        let (release_tx, release_rx) = oneshot::channel();
        let (resumed_tx, resumed_rx) = oneshot::channel();
        recovery_tx
            .send(TelemetryRelayControl::RecoveryBarrier {
                request_id: "telemetry-cut".to_string(),
                reply: reply_tx,
                release: release_rx,
                resumed: resumed_tx,
            })
            .await
            .unwrap();
        let snapshot = reply_rx.await.unwrap().unwrap();
        assert_eq!(snapshot.published_high_water_sequence, 2);

        source_tx
            .send(
                rmp_serde::to_vec_named(&serde_json::json!({
                    "event": "IntentAck",
                    "intent_id": "after-cut",
                }))
                .unwrap(),
            )
            .unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(30), journal.lock())
                .await
                .is_err(),
            "relay must hold the telemetry journal mutex during publication"
        );
        release_tx.send(()).unwrap();
        resumed_rx.await.unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            loop {
                if journal.lock().await.published_high_water() == 3 {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("post-cut telemetry is published after resume");
        drop(source_tx);
        assert!(
            relay
                .await
                .unwrap()
                .unwrap_err()
                .contains("source broadcast closed")
        );
        std::fs::remove_file(journal_path).ok();
        std::fs::remove_file(path_with_suffix(&cursor_path, ".a")).ok();
        std::fs::remove_file(path_with_suffix(&cursor_path, ".b")).ok();
    }

    #[test]
    fn recovery_snapshot_selects_the_exact_active_ack_cursor_generation() {
        let (journal_path, cursor_path) = test_paths("recovery-ack");
        let mut journal = TelemetryJournal::load(&journal_path, &cursor_path, 128 * 1024).unwrap();
        journal.publish(&order_update("recovery-ack-1")).unwrap();
        journal.publish(&order_update("recovery-ack-2")).unwrap();
        journal
            .acknowledge(&TelemetryAck {
                event: "TelemetryAck".to_string(),
                schema_version: TELEMETRY_SCHEMA_VERSION,
                consumer_id: "python-live-trader".to_string(),
                high_water_sequence: 1,
            })
            .unwrap();
        let snapshot = journal.prepare_recovery_snapshot().unwrap();
        assert_eq!(snapshot.published_high_water_sequence, 2);
        assert_eq!(snapshot.acknowledged_high_water_sequence, 1);
        assert_eq!(snapshot.cursor_generation, 1);
        assert_eq!(snapshot.active_cursor_suffix, ".b");
        assert_eq!(
            snapshot.active_cursor_path.as_deref(),
            Some(path_with_suffix(&cursor_path, ".b").as_path())
        );
        let cursor: TelemetryCursor =
            serde_json::from_slice(&snapshot.active_cursor_bytes).unwrap();
        assert_eq!(cursor.generation, 1);
        assert_eq!(cursor.high_water_sequence, 1);
        std::fs::remove_file(journal_path).ok();
        std::fs::remove_file(path_with_suffix(&cursor_path, ".a")).ok();
        std::fs::remove_file(path_with_suffix(&cursor_path, ".b")).ok();
    }
}
