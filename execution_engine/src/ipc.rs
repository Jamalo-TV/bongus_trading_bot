use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};
use tokio::sync::mpsc::Sender;
use tracing::{debug, error, info};
use zeromq::{PullSocket, Socket, SocketRecv};

pub const EXECUTION_PROTOCOL_VERSION: u16 = 2;
pub const DEFAULT_MAX_UNHEDGED_NOTIONAL_MS: f64 = 5_000_000.0;
pub const MAX_COMMAND_TTL_MS: i64 = 300_000;
pub const CONFIG_SYNC_INTENT: &str = "CONFIG_SYNC";

/// Resolve durable Rust runtime artifacts independently of the process cwd.
/// The production watchdog supplies absolute overrides, while this fallback
/// keeps direct `cargo run` invocations inside the repository's runtime tree.
pub fn default_rust_runtime_path(file_name: &str) -> PathBuf {
    let current = std::env::current_dir().unwrap_or_else(|_| PathBuf::from("."));
    let project_root = if current
        .file_name()
        .is_some_and(|name| name.eq_ignore_ascii_case("execution_engine"))
    {
        current.parent().unwrap_or(&current).to_path_buf()
    } else {
        current
    };
    project_root.join("runtime").join("rust").join(file_name)
}

/// Diagnostic subprocess boundary used by the cross-language config campaign.
/// It performs no networking or exchange action: one msgpack command is read
/// as hex from stdin and validated by the same production protocol/consensus
/// code that the order actor uses.
pub fn run_config_consensus_harness() -> Result<(), String> {
    let mut encoded = String::new();
    std::io::stdin()
        .read_line(&mut encoded)
        .map_err(|error| format!("read_stdin:{error}"))?;
    let bytes = hex::decode(encoded.trim()).map_err(|error| format!("decode_hex:{error}"))?;
    let instruction: AlphaInstruction =
        rmp_serde::from_slice(&bytes).map_err(|error| format!("decode_msgpack:{error}"))?;
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| i64::try_from(duration.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or_default();
    let declared_hash = instruction.config_version_hash.clone().unwrap_or_default();
    let mut consensus = ConfigConsensus::default();
    let before = consensus
        .entry_block_reason(&declared_hash)
        .unwrap_or("")
        .to_string();

    let (ack, applied) = if let Some(reason) = instruction.protocol_error(now_ms) {
        (
            ConfigAck::rejected(
                &instruction,
                consensus.applied_hash(),
                reason,
                now_ms,
                false,
            ),
            false,
        )
    } else {
        match consensus.apply(&instruction) {
            Ok(snapshot) => (
                ConfigAck::applied(&instruction, &snapshot, now_ms, false),
                true,
            ),
            Err(error) => (
                ConfigAck::rejected(
                    &instruction,
                    consensus.applied_hash(),
                    error.code(),
                    now_ms,
                    false,
                ),
                false,
            ),
        }
    };
    let same_hash_entry_block = consensus
        .entry_block_reason(&declared_hash)
        .unwrap_or("")
        .to_string();
    let mismatched_hash_entry_block = consensus
        .entry_block_reason(&"0".repeat(64))
        .unwrap_or("")
        .to_string();
    let payload = serde_json::json!({
        "schema_version": 1,
        "applied": applied,
        "before_entry_block": before,
        "active_hash": consensus.applied_hash().unwrap_or(""),
        "same_hash_entry_block": same_hash_entry_block,
        "mismatched_hash_entry_block": mismatched_hash_entry_block,
        "ack": ack,
    });
    println!(
        "{}",
        serde_json::to_string(&payload).map_err(|error| format!("encode_json:{error}"))?
    );
    Ok(())
}

#[derive(Debug, Clone, Deserialize)]
struct ConfigSyncSchema {
    schema_version: u16,
    allowed_keys: Vec<String>,
    required_consensus_keys: Vec<String>,
    compiled_max_per_symbol_notional_usd: u64,
    compiled_max_gross_exposure_usd: u64,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConfigSyncValidationError {
    MissingConfigSnapshot,
    InvalidConfigHash,
    ConfigHashMismatch,
    MalformedConfigSnapshot,
    NonCanonicalConfigSnapshot,
    ConfigSchemaUnavailable,
    ConfigSchemaVersionMismatch,
    UnknownConfigKey,
    MissingConsensusConfigKey,
    InvalidPauseNewEntries,
    InvalidRiskLimit,
    InvalidStorageControl,
    RiskLimitExceedsCompiledCeiling,
    InconsistentRiskLimits,
}

impl ConfigSyncValidationError {
    pub fn code(self) -> &'static str {
        match self {
            Self::MissingConfigSnapshot => "missing_config_snapshot",
            Self::InvalidConfigHash => "invalid_config_hash",
            Self::ConfigHashMismatch => "config_hash_mismatch",
            Self::MalformedConfigSnapshot => "malformed_config_snapshot",
            Self::NonCanonicalConfigSnapshot => "noncanonical_config_snapshot",
            Self::ConfigSchemaUnavailable => "config_schema_unavailable",
            Self::ConfigSchemaVersionMismatch => "config_schema_version_mismatch",
            Self::UnknownConfigKey => "unknown_config_key",
            Self::MissingConsensusConfigKey => "missing_consensus_config_key",
            Self::InvalidPauseNewEntries => "invalid_pause_new_entries",
            Self::InvalidRiskLimit => "invalid_risk_limit",
            Self::InvalidStorageControl => "invalid_storage_control",
            Self::RiskLimitExceedsCompiledCeiling => "risk_limit_exceeds_compiled_ceiling",
            Self::InconsistentRiskLimits => "inconsistent_risk_limits",
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct StorageControlUpdate {
    pub generation: u64,
    pub emergency_latched: bool,
    pub recovery_acknowledged: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ValidatedConfigSnapshot {
    pub config_hash: String,
    pub canonical_json: String,
    pub pause_new_entries: bool,
    pub per_symbol_notional_cap_usd: String,
    pub max_gross_exposure_usd: String,
    pub storage_control: Option<StorageControlUpdate>,
}

/// In-memory entry consensus. It starts fail-closed after every engine restart.
/// Call ``entry_block_reason`` only for entry commands; exits and repair paths
/// deliberately have no dependency on this state.
#[derive(Debug, Clone, Default)]
pub struct ConfigConsensus {
    active: Option<ValidatedConfigSnapshot>,
}

impl ConfigConsensus {
    pub fn validate(
        instruction: &AlphaInstruction,
    ) -> Result<ValidatedConfigSnapshot, ConfigSyncValidationError> {
        instruction.validate_config_sync_snapshot()
    }

    pub fn apply(
        &mut self,
        instruction: &AlphaInstruction,
    ) -> Result<ValidatedConfigSnapshot, ConfigSyncValidationError> {
        let snapshot = Self::validate(instruction)?;
        self.active = Some(snapshot.clone());
        Ok(snapshot)
    }

    pub fn active(&self) -> Option<&ValidatedConfigSnapshot> {
        self.active.as_ref()
    }

    pub fn applied_hash(&self) -> Option<&str> {
        self.active
            .as_ref()
            .map(|snapshot| snapshot.config_hash.as_str())
    }

    pub fn entry_block_reason(&self, command_config_hash: &str) -> Option<&'static str> {
        let Some(active) = self.active.as_ref() else {
            return Some("config_consensus_unavailable");
        };
        if active.config_hash != command_config_hash {
            return Some("config_consensus_hash_mismatch");
        }
        if active.pause_new_entries {
            return Some("config_pause_new_entries");
        }
        None
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct ConfigAck {
    pub event: String,
    pub schema_version: u16,
    pub intent_id: String,
    pub producer_id: String,
    pub sequence: u64,
    pub account_id: String,
    pub environment: String,
    pub strategy_id: String,
    pub cycle_id: String,
    pub config_version_hash: String,
    pub command_hash: String,
    pub ack_status: String,
    pub reason: String,
    pub event_time_ms: i64,
    pub replay: bool,
    pub declared_config_hash: String,
    pub applied_config_hash: String,
    pub config_status: String,
}

impl ConfigAck {
    pub fn applied(
        instruction: &AlphaInstruction,
        snapshot: &ValidatedConfigSnapshot,
        event_time_ms: i64,
        replay: bool,
    ) -> Self {
        Self::build(
            instruction,
            "TERMINAL",
            "",
            event_time_ms,
            replay,
            &snapshot.config_hash,
            "APPLIED",
        )
    }

    pub fn rejected(
        instruction: &AlphaInstruction,
        active_hash: Option<&str>,
        reason: &str,
        event_time_ms: i64,
        replay: bool,
    ) -> Self {
        Self::build(
            instruction,
            "REJECTED",
            reason,
            event_time_ms,
            replay,
            active_hash.unwrap_or(""),
            "REJECTED",
        )
    }

    /// The emergency control reached the FIFO actor boundary and the in-memory
    /// entry latch is active, but the storage-control checkpoint or terminal
    /// intent receipt could not be made durable. Python may use this only as a
    /// cancellation/reconciliation barrier; it must never authorize recovery.
    pub fn volatile_latched(
        instruction: &AlphaInstruction,
        snapshot: &ValidatedConfigSnapshot,
        reason: &str,
        event_time_ms: i64,
        replay: bool,
        ack_status: &str,
    ) -> Self {
        Self::build(
            instruction,
            ack_status,
            reason,
            event_time_ms,
            replay,
            &snapshot.config_hash,
            "VOLATILE_LATCHED",
        )
    }

    fn build(
        instruction: &AlphaInstruction,
        ack_status: &str,
        reason: &str,
        event_time_ms: i64,
        replay: bool,
        applied_config_hash: &str,
        config_status: &str,
    ) -> Self {
        let declared_config_hash = instruction.config_version_hash.clone().unwrap_or_default();
        Self {
            event: "ConfigAck".to_string(),
            schema_version: EXECUTION_PROTOCOL_VERSION,
            intent_id: instruction.intent_id.clone().unwrap_or_default(),
            producer_id: instruction.producer_id.clone().unwrap_or_default(),
            sequence: instruction.sequence.unwrap_or(0),
            account_id: instruction.account_id.clone().unwrap_or_default(),
            environment: instruction.environment.clone().unwrap_or_default(),
            strategy_id: instruction.strategy_id.clone().unwrap_or_default(),
            cycle_id: instruction.cycle_id.clone().unwrap_or_default(),
            config_version_hash: declared_config_hash.clone(),
            command_hash: instruction.command_hash.clone().unwrap_or_default(),
            ack_status: ack_status.to_string(),
            reason: reason.to_string(),
            event_time_ms,
            replay,
            declared_config_hash,
            applied_config_hash: applied_config_hash.to_string(),
            config_status: config_status.to_string(),
        }
    }
}

#[derive(Debug, Clone, Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct AlphaInstruction {
    #[serde(default)]
    pub schema_version: Option<u16>,
    #[serde(default)]
    pub producer_id: Option<String>,
    #[serde(default)]
    pub sequence: Option<u64>,
    #[serde(default)]
    pub created_at_ms: Option<i64>,
    #[serde(default)]
    pub deadline_at_ms: Option<i64>,
    #[serde(default)]
    pub command_hash: Option<String>,
    #[serde(default)]
    pub account_id: Option<String>,
    #[serde(default)]
    pub environment: Option<String>,
    #[serde(default)]
    pub strategy_id: Option<String>,
    #[serde(default)]
    pub cycle_id: Option<String>,
    #[serde(default)]
    pub config_version_hash: Option<String>,
    #[serde(default)]
    pub config_canonical_json: Option<String>,
    #[serde(default)]
    pub spot_leg_id: Option<String>,
    #[serde(default)]
    pub perp_leg_id: Option<String>,
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
    pub route_policy: Option<String>,
    #[serde(default)]
    pub route_model_version: Option<String>,
    #[serde(default)]
    pub max_unhedged_notional_ms: Option<f64>,
    #[serde(default)]
    pub route_slice_count: Option<u16>,
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
    #[serde(default)]
    pub spot_client_order_id: Option<String>,
    #[serde(default)]
    pub perp_client_order_id: Option<String>,
}

impl AlphaInstruction {
    pub fn seal_internal(mut self) -> Self {
        static LAST_SEQUENCE: AtomicU64 = AtomicU64::new(0);
        let now_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_millis() as u64)
            .unwrap_or(1);
        let mut observed = LAST_SEQUENCE.load(Ordering::Relaxed);
        let sequence = loop {
            let candidate = now_ms.max(observed.saturating_add(1));
            match LAST_SEQUENCE.compare_exchange_weak(
                observed,
                candidate,
                Ordering::SeqCst,
                Ordering::Relaxed,
            ) {
                Ok(_) => break candidate,
                Err(next) => observed = next,
            }
        };
        let base_intent_id = self
            .intent_id
            .clone()
            .unwrap_or_else(|| format!("rust-{}", self.intent.to_lowercase()));
        let intent_id = format!("{base_intent_id}:{sequence}");
        let client_id = |leg: &str| {
            let normalized_leg = if leg.eq_ignore_ascii_case("spot") {
                "s"
            } else {
                "p"
            };
            let mut digest = Sha256::new();
            digest.update(format!("{intent_id}:{normalized_leg}").as_bytes());
            format!(
                "bngs_{}_{}",
                normalized_leg,
                &hex::encode(digest.finalize())[..24]
            )
        };
        self.intent = self.intent.trim().to_uppercase();
        self.symbol = self.symbol.map(|symbol| symbol.trim().to_uppercase());
        self.direction = self
            .direction
            .map(|direction| direction.trim().to_lowercase());
        self.schema_version = Some(EXECUTION_PROTOCOL_VERSION);
        self.producer_id = Some("rust-ranking-strategy".to_string());
        self.sequence = Some(sequence);
        self.created_at_ms = Some(now_ms as i64);
        self.deadline_at_ms = Some(now_ms.saturating_add(30_000) as i64);
        self.account_id = Some(
            std::env::var("BINANCE_ACCOUNT_ID").unwrap_or_else(|_| "binance-default".to_string()),
        );
        self.environment =
            Some(std::env::var("TRADING_MODE").unwrap_or_else(|_| "paper".to_string()));
        self.strategy_id = Some("rust-ranking-strategy".to_string());
        self.cycle_id = Some(intent_id.clone());
        self.config_version_hash = Some(
            std::env::var("CONFIG_VERSION_HASH").unwrap_or_else(|_| "rust-static".to_string()),
        );
        self.spot_leg_id = Some(format!("{intent_id}:spot"));
        self.perp_leg_id = Some(format!("{intent_id}:perp"));
        self.spot_client_order_id = Some(client_id("spot"));
        self.perp_client_order_id = Some(client_id("perp"));
        self.route_policy
            .get_or_insert_with(|| "legacy_dual_maker".to_string());
        self.route_model_version
            .get_or_insert_with(|| "legacy-v1".to_string());
        self.max_unhedged_notional_ms
            .get_or_insert(DEFAULT_MAX_UNHEDGED_NOTIONAL_MS);
        self.route_slice_count.get_or_insert(1);
        self.intent_id = Some(intent_id);
        self.command_hash = Some(self.semantic_fingerprint());
        self
    }

    pub fn semantic_fingerprint(&self) -> String {
        let mut digest = Sha256::new();
        if self.intent.trim().eq_ignore_ascii_case(CONFIG_SYNC_INTENT) {
            digest.update(self.canonical_config_sync_command_bytes());
        } else {
            digest.update(self.canonical_command_bytes());
        }
        hex::encode(digest.finalize())
    }

    /// Config syncs use a separate v2 domain so extending the protocol does
    /// not change any existing execution-command hash or golden fixture.
    pub fn canonical_config_sync_command_bytes(&self) -> Vec<u8> {
        fn prefix(out: &mut Vec<u8>, name: &str) {
            out.extend_from_slice(name.as_bytes());
            out.push(b'=');
        }
        fn string(out: &mut Vec<u8>, name: &str, value: &str) {
            prefix(out, name);
            out.push(b's');
            out.extend_from_slice(value.len().to_string().as_bytes());
            out.push(b':');
            out.extend_from_slice(value.as_bytes());
            out.push(b'\n');
        }
        fn integer<T: std::fmt::Display>(out: &mut Vec<u8>, name: &str, value: T) {
            prefix(out, name);
            out.push(b'i');
            out.extend_from_slice(value.to_string().as_bytes());
            out.push(b'\n');
        }

        let mut out = b"bongus-config-sync-command-v2\n".to_vec();
        integer(
            &mut out,
            "schema_version",
            self.schema_version.unwrap_or(EXECUTION_PROTOCOL_VERSION),
        );
        string(
            &mut out,
            "account_id",
            self.account_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "environment",
            self.environment.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "strategy_id",
            self.strategy_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "cycle_id",
            self.cycle_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "config_version_hash",
            self.config_version_hash.as_deref().unwrap_or("").trim(),
        );
        string(&mut out, "intent", CONFIG_SYNC_INTENT);
        string(
            &mut out,
            "intent_id",
            self.intent_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "config_canonical_json",
            self.config_canonical_json.as_deref().unwrap_or(""),
        );
        out
    }

    /// Language-neutral v2 bytes. Keep the field order/types in lock-step
    /// with ``bongus.ipc.protocol._CANONICAL_FIELD_TYPES``.
    pub fn canonical_command_bytes(&self) -> Vec<u8> {
        fn prefix(out: &mut Vec<u8>, name: &str) {
            out.extend_from_slice(name.as_bytes());
            out.push(b'=');
        }
        fn string(out: &mut Vec<u8>, name: &str, value: &str) {
            prefix(out, name);
            out.push(b's');
            out.extend_from_slice(value.len().to_string().as_bytes());
            out.push(b':');
            out.extend_from_slice(value.as_bytes());
            out.push(b'\n');
        }
        fn optional_string(out: &mut Vec<u8>, name: &str, value: Option<&str>) {
            match value {
                Some(value) => string(out, name, value),
                None => {
                    prefix(out, name);
                    out.extend_from_slice(b"n\n");
                }
            }
        }
        fn integer<T: std::fmt::Display>(out: &mut Vec<u8>, name: &str, value: T) {
            prefix(out, name);
            out.push(b'i');
            out.extend_from_slice(value.to_string().as_bytes());
            out.push(b'\n');
        }
        fn float(out: &mut Vec<u8>, name: &str, value: f64) {
            prefix(out, name);
            out.push(b'f');
            out.extend_from_slice(format!("{:016x}", value.to_bits()).as_bytes());
            out.push(b'\n');
        }
        fn optional_float(out: &mut Vec<u8>, name: &str, value: Option<f64>) {
            match value {
                Some(value) => float(out, name, value),
                None => {
                    prefix(out, name);
                    out.extend_from_slice(b"n\n");
                }
            }
        }
        fn boolean(out: &mut Vec<u8>, name: &str, value: bool) {
            prefix(out, name);
            out.extend_from_slice(if value { b"b1\n" } else { b"b0\n" });
        }

        let mut out = b"bongus-execution-command-v2\n".to_vec();
        integer(
            &mut out,
            "schema_version",
            self.schema_version.unwrap_or(EXECUTION_PROTOCOL_VERSION),
        );
        string(
            &mut out,
            "account_id",
            self.account_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "environment",
            self.environment.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "strategy_id",
            self.strategy_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "cycle_id",
            self.cycle_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "config_version_hash",
            self.config_version_hash.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "symbol",
            &self.symbol.as_deref().unwrap_or("").trim().to_uppercase(),
        );
        string(&mut out, "intent", &self.intent.trim().to_uppercase());
        float(&mut out, "quantity", self.quantity);
        float(&mut out, "urgency", self.urgency);
        float(&mut out, "max_slippage_bps", self.max_slippage_bps);
        string(
            &mut out,
            "route_policy",
            &self
                .route_policy
                .as_deref()
                .unwrap_or("legacy_dual_maker")
                .trim()
                .to_lowercase(),
        );
        string(
            &mut out,
            "route_model_version",
            self.route_model_version
                .as_deref()
                .unwrap_or("legacy-v1")
                .trim(),
        );
        float(
            &mut out,
            "max_unhedged_notional_ms",
            self.max_unhedged_notional_ms
                .unwrap_or(DEFAULT_MAX_UNHEDGED_NOTIONAL_MS),
        );
        integer(
            &mut out,
            "route_slice_count",
            self.route_slice_count.unwrap_or(1),
        );
        float(&mut out, "exposure_scale", self.exposure_scale);
        string(
            &mut out,
            "intent_id",
            self.intent_id.as_deref().unwrap_or("").trim(),
        );
        let normalized_direction = self
            .direction
            .as_deref()
            .map(|direction| direction.trim().to_lowercase());
        optional_string(&mut out, "direction", normalized_direction.as_deref());
        boolean(&mut out, "skip_spot_leg", self.skip_spot_leg);
        boolean(&mut out, "skip_perp_leg", self.skip_perp_leg);
        optional_float(&mut out, "spot_quantity", self.spot_quantity);
        optional_float(&mut out, "perp_quantity", self.perp_quantity);
        string(
            &mut out,
            "spot_client_order_id",
            self.spot_client_order_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "perp_client_order_id",
            self.perp_client_order_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "spot_leg_id",
            self.spot_leg_id.as_deref().unwrap_or("").trim(),
        );
        string(
            &mut out,
            "perp_leg_id",
            self.perp_leg_id.as_deref().unwrap_or("").trim(),
        );
        out
    }

    pub fn validate_config_sync_snapshot(
        &self,
    ) -> Result<ValidatedConfigSnapshot, ConfigSyncValidationError> {
        let canonical_json = self
            .config_canonical_json
            .as_deref()
            .filter(|value| !value.is_empty())
            .ok_or(ConfigSyncValidationError::MissingConfigSnapshot)?;
        let declared_hash = self.config_version_hash.as_deref().unwrap_or("").trim();
        if declared_hash.len() != 64
            || !declared_hash
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Err(ConfigSyncValidationError::InvalidConfigHash);
        }
        let mut digest = Sha256::new();
        digest.update(canonical_json.as_bytes());
        if hex::encode(digest.finalize()) != declared_hash {
            return Err(ConfigSyncValidationError::ConfigHashMismatch);
        }

        let document: serde_json::Value = serde_json::from_str(canonical_json)
            .map_err(|_| ConfigSyncValidationError::MalformedConfigSnapshot)?;
        let object = document
            .as_object()
            .ok_or(ConfigSyncValidationError::MalformedConfigSnapshot)?;
        let reconstructed = serde_json::to_string(&document)
            .map_err(|_| ConfigSyncValidationError::MalformedConfigSnapshot)?;
        if reconstructed != canonical_json {
            return Err(ConfigSyncValidationError::NonCanonicalConfigSnapshot);
        }
        let schema: ConfigSyncSchema =
            serde_json::from_str(include_str!("../config_sync_schema_v2.json"))
                .map_err(|_| ConfigSyncValidationError::ConfigSchemaUnavailable)?;
        if schema.schema_version != EXECUTION_PROTOCOL_VERSION {
            return Err(ConfigSyncValidationError::ConfigSchemaVersionMismatch);
        }
        let allowed: HashSet<&str> = schema.allowed_keys.iter().map(String::as_str).collect();
        if object.keys().any(|key| !allowed.contains(key.as_str())) {
            return Err(ConfigSyncValidationError::UnknownConfigKey);
        }
        if schema
            .required_consensus_keys
            .iter()
            .any(|key| !object.contains_key(key))
        {
            return Err(ConfigSyncValidationError::MissingConsensusConfigKey);
        }
        let pause_new_entries = object
            .get("pause_new_entries")
            .and_then(serde_json::Value::as_bool)
            .ok_or(ConfigSyncValidationError::InvalidPauseNewEntries)?;
        let per_symbol = object
            .get("per_symbol_notional_cap_usd")
            .and_then(serde_json::Value::as_number)
            .map(ToString::to_string)
            .ok_or(ConfigSyncValidationError::InvalidRiskLimit)?;
        let max_gross = object
            .get("max_gross_exposure_usd")
            .and_then(serde_json::Value::as_number)
            .map(ToString::to_string)
            .ok_or(ConfigSyncValidationError::InvalidRiskLimit)?;
        if compare_positive_decimals(
            &per_symbol,
            &schema.compiled_max_per_symbol_notional_usd.to_string(),
        )
        .is_none_or(|ordering| ordering.is_gt())
            || compare_positive_decimals(
                &max_gross,
                &schema.compiled_max_gross_exposure_usd.to_string(),
            )
            .is_none_or(|ordering| ordering.is_gt())
        {
            return Err(ConfigSyncValidationError::RiskLimitExceedsCompiledCeiling);
        }
        if compare_positive_decimals(&per_symbol, &max_gross)
            .is_none_or(|ordering| ordering.is_gt())
        {
            return Err(ConfigSyncValidationError::InconsistentRiskLimits);
        }
        let storage_control_keys = [
            "storage_control_generation",
            "storage_emergency_latched",
            "storage_recovery_acknowledged",
        ];
        let storage_control_key_count = storage_control_keys
            .iter()
            .filter(|key| object.contains_key(**key))
            .count();
        let storage_control = if storage_control_key_count == 0 {
            None
        } else {
            if storage_control_key_count != storage_control_keys.len() {
                return Err(ConfigSyncValidationError::InvalidStorageControl);
            }
            let generation = object
                .get("storage_control_generation")
                .and_then(serde_json::Value::as_u64)
                .ok_or(ConfigSyncValidationError::InvalidStorageControl)?;
            let emergency_latched = object
                .get("storage_emergency_latched")
                .and_then(serde_json::Value::as_bool)
                .ok_or(ConfigSyncValidationError::InvalidStorageControl)?;
            let recovery_acknowledged = object
                .get("storage_recovery_acknowledged")
                .and_then(serde_json::Value::as_bool)
                .ok_or(ConfigSyncValidationError::InvalidStorageControl)?;
            // Setting the latch is an automated survival action. Clearing it
            // is a distinct operator-authorized transition. Requiring these
            // values to be complementary prevents ambiguous control records.
            let is_initial_clear_state =
                generation == 0 && !emergency_latched && !recovery_acknowledged;
            if !is_initial_clear_state
                && (generation == 0 || emergency_latched == recovery_acknowledged)
            {
                return Err(ConfigSyncValidationError::InvalidStorageControl);
            }
            Some(StorageControlUpdate {
                generation,
                emergency_latched,
                recovery_acknowledged,
            })
        };
        Ok(ValidatedConfigSnapshot {
            config_hash: declared_hash.to_string(),
            canonical_json: canonical_json.to_string(),
            pause_new_entries,
            per_symbol_notional_cap_usd: per_symbol,
            max_gross_exposure_usd: max_gross,
            storage_control,
        })
    }

    fn config_sync_protocol_error(&self, now_ms: i64) -> Option<&'static str> {
        if self.schema_version != Some(EXECUTION_PROTOCOL_VERSION) {
            return Some("unsupported_schema_version");
        }
        if self.intent.trim() != CONFIG_SYNC_INTENT {
            return Some("invalid_config_sync_intent");
        }
        if self.intent_id.as_deref().unwrap_or("").trim().is_empty() {
            return Some("missing_intent_id");
        }
        if self.producer_id.as_deref().unwrap_or("").trim().is_empty() {
            return Some("missing_producer_id");
        }
        if self.account_id.as_deref().unwrap_or("").trim().is_empty()
            || self.environment.as_deref().unwrap_or("").trim().is_empty()
            || self.strategy_id.as_deref().unwrap_or("").trim().is_empty()
            || self.cycle_id.as_deref().unwrap_or("").trim().is_empty()
        {
            return Some("missing_command_context");
        }
        if self.sequence.unwrap_or(0) == 0 {
            return Some("invalid_sequence");
        }
        if self
            .symbol
            .as_deref()
            .is_some_and(|value| !value.trim().is_empty())
            || self.quantity != 0.0
            || self.urgency != 0.0
            || self.max_slippage_bps != 0.0
            || self.exposure_scale != 0.0
            || self.route_policy.is_some()
            || self.route_model_version.is_some()
            || self.max_unhedged_notional_ms.is_some()
            || self.route_slice_count.is_some()
            || self.spot_leg_id.is_some()
            || self.perp_leg_id.is_some()
            || self.spot_client_order_id.is_some()
            || self.perp_client_order_id.is_some()
            || self.direction.is_some()
            || self.skip_spot_leg
            || self.skip_perp_leg
            || self.spot_quantity.is_some()
            || self.perp_quantity.is_some()
            || self.heartbeat_id.is_some()
            || self.spot_entry_price.is_some()
            || self.perp_entry_price.is_some()
            || self.spot_mark_price.is_some()
            || self.perp_mark_price.is_some()
        {
            return Some("unexpected_config_sync_field");
        }
        let created = self.created_at_ms.unwrap_or(0);
        let deadline = self.deadline_at_ms.unwrap_or(0);
        if created <= 0
            || deadline <= created
            || deadline.saturating_sub(created) > MAX_COMMAND_TTL_MS
        {
            return Some("invalid_command_window");
        }
        if created > now_ms.saturating_add(30_000) {
            return Some("created_at_in_future");
        }
        if deadline <= now_ms {
            return Some("expired_command");
        }
        let hash = self.command_hash.as_deref().unwrap_or("");
        if hash.len() != 64
            || !hash
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Some("invalid_command_hash");
        }
        if self.semantic_fingerprint() != hash {
            return Some("command_hash_mismatch");
        }
        self.validate_config_sync_snapshot()
            .err()
            .map(|error| error.code())
    }

    pub fn protocol_error(&self, now_ms: i64) -> Option<&'static str> {
        if self.intent.trim().eq_ignore_ascii_case(CONFIG_SYNC_INTENT) {
            return self.config_sync_protocol_error(now_ms);
        }
        if self.schema_version != Some(EXECUTION_PROTOCOL_VERSION) {
            return Some("unsupported_schema_version");
        }
        if self.intent_id.as_deref().unwrap_or("").trim().is_empty() {
            return Some("missing_intent_id");
        }
        if self.producer_id.as_deref().unwrap_or("").trim().is_empty() {
            return Some("missing_producer_id");
        }
        if self.account_id.as_deref().unwrap_or("").trim().is_empty()
            || self.environment.as_deref().unwrap_or("").trim().is_empty()
            || self.strategy_id.as_deref().unwrap_or("").trim().is_empty()
            || self.cycle_id.as_deref().unwrap_or("").trim().is_empty()
            || self
                .config_version_hash
                .as_deref()
                .unwrap_or("")
                .trim()
                .is_empty()
            || self.spot_leg_id.as_deref().unwrap_or("").trim().is_empty()
            || self.perp_leg_id.as_deref().unwrap_or("").trim().is_empty()
        {
            return Some("missing_command_context");
        }
        if self
            .spot_client_order_id
            .as_deref()
            .unwrap_or("")
            .trim()
            .is_empty()
            || self
                .perp_client_order_id
                .as_deref()
                .unwrap_or("")
                .trim()
                .is_empty()
        {
            return Some("missing_client_order_ids");
        }
        if self
            .spot_client_order_id
            .as_ref()
            .is_some_and(|id| id.len() > 36)
            || self
                .perp_client_order_id
                .as_ref()
                .is_some_and(|id| id.len() > 36)
        {
            return Some("invalid_client_order_ids");
        }
        let intent_id = self.intent_id.as_deref().unwrap_or("").trim();
        let expected_spot_client_id = deterministic_client_order_id(intent_id, "spot");
        let expected_perp_client_id = deterministic_client_order_id(intent_id, "perp");
        if self.spot_client_order_id.as_deref() != Some(expected_spot_client_id.as_str())
            || self.perp_client_order_id.as_deref() != Some(expected_perp_client_id.as_str())
            || self.spot_leg_id.as_deref() != Some(format!("{intent_id}:spot").as_str())
            || self.perp_leg_id.as_deref() != Some(format!("{intent_id}:perp").as_str())
        {
            return Some("non_deterministic_leg_ids");
        }
        if self.sequence.unwrap_or(0) == 0 {
            return Some("invalid_sequence");
        }
        let route_policy = self.route_policy.as_deref().unwrap_or("");
        if !matches!(
            route_policy,
            "legacy_dual_maker"
                | "post_only_dual"
                | "maker_lead_ioc"
                | "simultaneous_ioc"
                | "sliced_ioc"
                | "emergency_reduce_only"
        ) {
            return Some("unsupported_route_policy");
        }
        // Route recommendations remain shadow-only until their predeclared
        // paper/testnet promotion gate passes.  Never silently execute a new
        // route through the legacy chase implementation.
        if route_policy != "legacy_dual_maker" {
            return Some("route_policy_not_promoted");
        }
        if self
            .route_model_version
            .as_deref()
            .unwrap_or("")
            .trim()
            .is_empty()
        {
            return Some("missing_route_model_version");
        }
        let hedge_budget = self.max_unhedged_notional_ms.unwrap_or(f64::NAN);
        if !hedge_budget.is_finite() || hedge_budget <= 0.0 {
            return Some("invalid_hedge_risk_budget");
        }
        let slices = self.route_slice_count.unwrap_or(0);
        if slices == 0 || slices > 16 {
            return Some("invalid_route_slice_count");
        }
        if route_policy != "sliced_ioc" && slices != 1 {
            return Some("invalid_route_slice_count");
        }
        if self.symbol.as_deref().unwrap_or("").trim().is_empty() {
            return Some("missing_symbol");
        }
        if !self.quantity.is_finite()
            || !self.urgency.is_finite()
            || !self.max_slippage_bps.is_finite()
            || !self.exposure_scale.is_finite()
            || self.quantity < 0.0
            || !(0.0..=1.0).contains(&self.urgency)
            || self.max_slippage_bps < 0.0
            || !(0.0..=1.0).contains(&self.exposure_scale)
            || self.exposure_scale == 0.0
            || self
                .spot_quantity
                .is_some_and(|value| !value.is_finite() || value < 0.0)
            || self
                .perp_quantity
                .is_some_and(|value| !value.is_finite() || value < 0.0)
        {
            return Some("invalid_command_numeric");
        }
        if self
            .direction
            .as_deref()
            .is_some_and(|direction| !matches!(direction, "long" | "short"))
        {
            return Some("invalid_direction");
        }
        if self.heartbeat_id.is_some()
            || self.config_canonical_json.is_some()
            || self.spot_entry_price.is_some()
            || self.perp_entry_price.is_some()
            || self.spot_mark_price.is_some()
            || self.perp_mark_price.is_some()
        {
            return Some("unexpected_risk_command_field");
        }
        let created = self.created_at_ms.unwrap_or(0);
        let deadline = self.deadline_at_ms.unwrap_or(0);
        if created <= 0
            || deadline <= created
            || deadline.saturating_sub(created) > MAX_COMMAND_TTL_MS
        {
            return Some("invalid_command_window");
        }
        if created > now_ms.saturating_add(30_000) {
            return Some("created_at_in_future");
        }
        if deadline <= now_ms {
            return Some("expired_command");
        }
        let hash = self.command_hash.as_deref().unwrap_or("");
        if hash.len() != 64
            || !hash
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
        {
            return Some("invalid_command_hash");
        }
        if self.semantic_fingerprint() != hash {
            return Some("command_hash_mismatch");
        }
        None
    }
}

fn normalized_positive_decimal(raw: &str) -> Option<(Vec<u8>, i64)> {
    if raw.is_empty() || raw.starts_with('-') || raw.starts_with('+') {
        return None;
    }
    let mut exponent_split = raw.split(['e', 'E']);
    let mantissa = exponent_split.next()?;
    let exponent = match exponent_split.next() {
        Some(value) => value.parse::<i64>().ok()?,
        None => 0,
    };
    if exponent_split.next().is_some() || exponent.unsigned_abs() > 1_000_000 {
        return None;
    }
    let mut decimal_split = mantissa.split('.');
    let integer = decimal_split.next()?;
    let fraction = decimal_split.next().unwrap_or("");
    if decimal_split.next().is_some()
        || integer.is_empty()
        || (mantissa.contains('.') && fraction.is_empty())
        || !integer.bytes().all(|byte| byte.is_ascii_digit())
        || !fraction.bytes().all(|byte| byte.is_ascii_digit())
    {
        return None;
    }
    let all_digits: Vec<u8> = integer.bytes().chain(fraction.bytes()).collect();
    let leading_zeroes = all_digits
        .iter()
        .take_while(|digit| **digit == b'0')
        .count();
    if leading_zeroes == all_digits.len() {
        return None;
    }
    let digits = all_digits[leading_zeroes..].to_vec();
    let decimal_position = i64::try_from(integer.len())
        .ok()?
        .checked_add(exponent)?
        .checked_sub(i64::try_from(leading_zeroes).ok()?)?;
    Some((digits, decimal_position))
}

fn compare_positive_decimals(left: &str, right: &str) -> Option<std::cmp::Ordering> {
    let (left_digits, left_position) = normalized_positive_decimal(left)?;
    let (right_digits, right_position) = normalized_positive_decimal(right)?;
    match left_position.cmp(&right_position) {
        std::cmp::Ordering::Equal => {
            let width = left_digits.len().max(right_digits.len());
            for index in 0..width {
                let left_digit = left_digits.get(index).copied().unwrap_or(b'0');
                let right_digit = right_digits.get(index).copied().unwrap_or(b'0');
                match left_digit.cmp(&right_digit) {
                    std::cmp::Ordering::Equal => {}
                    ordering => return Some(ordering),
                }
            }
            Some(std::cmp::Ordering::Equal)
        }
        ordering => Some(ordering),
    }
}

fn deterministic_client_order_id(intent_id: &str, leg: &str) -> String {
    let normalized_leg = if leg.eq_ignore_ascii_case("spot") {
        "s"
    } else {
        "p"
    };
    let mut digest = Sha256::new();
    digest.update(format!("{intent_id}:{normalized_leg}").as_bytes());
    format!(
        "bngs_{normalized_leg}_{}",
        &hex::encode(digest.finalize())[..24]
    )
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
pub struct IntentReceipt {
    #[serde(default)]
    pub schema_version: u16,
    pub intent_id: String,
    pub producer_id: String,
    pub sequence: u64,
    #[serde(default)]
    pub created_at_ms: i64,
    #[serde(default)]
    pub deadline_at_ms: i64,
    pub account_id: String,
    pub environment: String,
    pub strategy_id: String,
    pub cycle_id: String,
    pub config_version_hash: String,
    pub spot_leg_id: String,
    pub perp_leg_id: String,
    pub spot_client_order_id: String,
    pub perp_client_order_id: String,
    pub command_hash: String,
    pub semantic_fingerprint: String,
    pub ack_status: String,
    pub reason: String,
    pub updated_at_ms: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ReceiptDecision {
    New(IntentReceipt),
    Replay(IntentReceipt),
    Conflict,
    NonMonotonicSequence,
}

/// Append-only, fsync'd receipt journal.  It closes the receive-before-ACK
/// boundary and survives engine restarts; deterministic exchange client IDs
/// close the remaining send-before-status-update replay window.
pub struct IntentJournal {
    path: PathBuf,
    max_bytes: u64,
    transition_reserve_bytes: u64,
    receipts: HashMap<String, IntentReceipt>,
    last_sequences: HashMap<String, u64>,
    sequence_owners: HashMap<(String, u64), String>,
}

#[derive(Debug, Clone, Copy)]
enum JournalWriteClass {
    NewRisk,
    SurvivalCommand,
    Transition,
}

impl IntentJournal {
    // Keep the complete Rust durable-artifact envelope below the deployment's
    // 150 MB component budget: 80 MB intents + 30 MB execution state + 30 MB
    // telemetry, with 10 MB left for cursors/control/checkpoints.
    const DEFAULT_MAX_BYTES: u64 = 80_000_000;
    const DEFAULT_TRANSITION_RESERVE_BYTES: u64 = 1024 * 1024;

    #[cfg(not(test))]
    pub fn from_env() -> Result<Self, String> {
        let path = std::env::var("EXECUTION_INTENT_JOURNAL_PATH")
            .map(PathBuf::from)
            .unwrap_or_else(|_| default_rust_runtime_path("execution_intents.jsonl"));
        let max_bytes = std::env::var("EXECUTION_INTENT_JOURNAL_MAX_BYTES")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .filter(|value| *value >= 2 * Self::DEFAULT_TRANSITION_RESERVE_BYTES)
            .unwrap_or(Self::DEFAULT_MAX_BYTES)
            .min(Self::DEFAULT_MAX_BYTES);
        Self::load_with_limits(path, max_bytes, Self::DEFAULT_TRANSITION_RESERVE_BYTES)
    }

    #[cfg(test)]
    pub fn load(path: impl AsRef<Path>) -> Result<Self, String> {
        Self::load_with_limits(
            path,
            Self::DEFAULT_MAX_BYTES,
            Self::DEFAULT_TRANSITION_RESERVE_BYTES,
        )
    }

    fn load_with_limits(
        path: impl AsRef<Path>,
        max_bytes: u64,
        transition_reserve_bytes: u64,
    ) -> Result<Self, String> {
        let path = path.as_ref().to_path_buf();
        if transition_reserve_bytes >= max_bytes {
            return Err("intent journal transition reserve must be below its byte cap".to_string());
        }
        Self::recover_interrupted_compaction(&path)?;
        let mut receipts = HashMap::new();
        let mut last_sequences = HashMap::new();
        let mut sequence_owners = HashMap::new();
        let mut records_on_disk = 0_usize;
        if path.exists() {
            let file = File::open(&path).map_err(|err| format!("open intent journal: {err}"))?;
            for (line_no, line) in BufReader::new(file).lines().enumerate() {
                let line = line.map_err(|err| format!("read intent journal: {err}"))?;
                if line.trim().is_empty() {
                    continue;
                }
                records_on_disk = records_on_disk.saturating_add(1);
                let receipt: IntentReceipt = serde_json::from_str(&line)
                    .map_err(|err| format!("invalid intent journal line {}: {err}", line_no + 1))?;
                if ack_rank(&receipt.ack_status).is_none() {
                    return Err(format!(
                        "invalid intent journal ACK state on line {}: {}",
                        line_no + 1,
                        receipt.ack_status
                    ));
                }
                if receipt.sequence == 0
                    || receipt.intent_id.trim().is_empty()
                    || receipt.producer_id.trim().is_empty()
                {
                    return Err(format!(
                        "invalid intent journal identity on line {}",
                        line_no + 1
                    ));
                }
                let sequence_key = (receipt.producer_id.clone(), receipt.sequence);
                if let Some(owner) = sequence_owners.get(&sequence_key) {
                    if owner != &receipt.intent_id {
                        return Err(format!(
                            "producer sequence reused by distinct intents on line {}",
                            line_no + 1
                        ));
                    }
                } else {
                    sequence_owners.insert(sequence_key, receipt.intent_id.clone());
                }
                if let Some(previous) = receipts.get(&receipt.intent_id) {
                    if !same_command(previous, &receipt) {
                        return Err(format!(
                            "intent command changed inside journal on line {}",
                            line_no + 1
                        ));
                    }
                    if !valid_ack_progression(&previous.ack_status, &receipt.ack_status) {
                        return Err(format!(
                            "non-monotonic intent ACK on journal line {}: {} -> {}",
                            line_no + 1,
                            previous.ack_status,
                            receipt.ack_status
                        ));
                    }
                }
                last_sequences
                    .entry(receipt.producer_id.clone())
                    .and_modify(|seq: &mut u64| *seq = (*seq).max(receipt.sequence))
                    .or_insert(receipt.sequence);
                receipts.insert(receipt.intent_id.clone(), receipt);
            }
        }
        let journal = Self {
            path,
            max_bytes,
            transition_reserve_bytes,
            receipts,
            last_sequences,
            sequence_owners,
        };
        let current_bytes = journal.file_len_or_zero()?;
        if current_bytes > max_bytes
            || (current_bytes >= max_bytes / 2
                && records_on_disk > journal.receipts.len().saturating_mul(2))
        {
            journal.compact_latest()?;
        }
        if journal.file_len_or_zero()? > max_bytes {
            return Err(format!(
                "intent journal remains above byte budget after compaction: current={}, limit={max_bytes}",
                journal.file_len_or_zero()?
            ));
        }
        Ok(journal)
    }

    fn path_with_suffix(path: &Path, suffix: &str) -> PathBuf {
        let mut value = path.as_os_str().to_os_string();
        value.push(suffix);
        PathBuf::from(value)
    }

    fn recover_interrupted_compaction(path: &Path) -> Result<(), String> {
        if path.exists() {
            return Ok(());
        }
        let previous = Self::path_with_suffix(path, ".previous");
        if previous.exists() {
            std::fs::rename(&previous, path)
                .map_err(|err| format!("recover prior intent journal: {err}"))?;
        }
        Ok(())
    }

    fn file_len_or_zero(&self) -> Result<u64, String> {
        self.path
            .metadata()
            .map(|metadata| metadata.len())
            .or_else(|err| {
                if err.kind() == std::io::ErrorKind::NotFound {
                    Ok(0)
                } else {
                    Err(err)
                }
            })
            .map_err(|err| format!("inspect intent journal size: {err}"))
    }

    fn compact_latest(&self) -> Result<(), String> {
        if let Some(parent) = self
            .path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            std::fs::create_dir_all(parent)
                .map_err(|err| format!("create intent journal directory: {err}"))?;
        }
        let mut latest: Vec<&IntentReceipt> = self.receipts.values().collect();
        latest.sort_by(|left, right| {
            left.producer_id
                .cmp(&right.producer_id)
                .then_with(|| left.sequence.cmp(&right.sequence))
                .then_with(|| left.intent_id.cmp(&right.intent_id))
        });
        let mut projected = 0_u64;
        for receipt in &latest {
            projected = projected
                .saturating_add(
                    serde_json::to_vec(receipt)
                        .map_err(|err| format!("encode compacted intent receipt: {err}"))?
                        .len() as u64,
                )
                .saturating_add(1);
        }
        if projected > self.max_bytes {
            return Err(format!(
                "latest intent receipts exceed journal byte budget: projected={projected}, limit={}",
                self.max_bytes
            ));
        }
        let next = Self::path_with_suffix(&self.path, ".next");
        let previous = Self::path_with_suffix(&self.path, ".previous");
        {
            let mut file = OpenOptions::new()
                .create(true)
                .truncate(true)
                .write(true)
                .open(&next)
                .map_err(|err| format!("create compacted intent journal: {err}"))?;
            for receipt in latest {
                serde_json::to_writer(&mut file, receipt)
                    .map_err(|err| format!("encode compacted intent receipt: {err}"))?;
                file.write_all(b"\n")
                    .map_err(|err| format!("write compacted intent receipt: {err}"))?;
            }
            file.sync_all()
                .map_err(|err| format!("sync compacted intent journal: {err}"))?;
        }
        if previous.exists() {
            std::fs::remove_file(&previous)
                .map_err(|err| format!("remove stale intent checkpoint: {err}"))?;
        }
        if self.path.exists() {
            std::fs::rename(&self.path, &previous)
                .map_err(|err| format!("rotate intent journal: {err}"))?;
        }
        if let Err(err) = std::fs::rename(&next, &self.path) {
            if !self.path.exists() && previous.exists() {
                let _ = std::fs::rename(&previous, &self.path);
            }
            return Err(format!("install compacted intent journal: {err}"));
        }
        if previous.exists() {
            let _ = std::fs::remove_file(previous);
        }
        Ok(())
    }

    fn append(&self, receipt: &IntentReceipt, class: JournalWriteClass) -> Result<(), String> {
        if let Some(parent) = self
            .path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
        {
            std::fs::create_dir_all(parent)
                .map_err(|err| format!("create intent journal directory: {err}"))?;
        }
        let encoded =
            serde_json::to_vec(receipt).map_err(|err| format!("encode intent receipt: {err}"))?;
        let current_bytes = self.file_len_or_zero()?;
        let write_limit = match class {
            JournalWriteClass::NewRisk => {
                self.max_bytes.saturating_sub(self.transition_reserve_bytes)
            }
            JournalWriteClass::SurvivalCommand => self
                .max_bytes
                .saturating_sub(self.transition_reserve_bytes / 2),
            JournalWriteClass::Transition => self.max_bytes,
        };
        let mut projected = current_bytes
            .saturating_add(encoded.len() as u64)
            .saturating_add(1);
        if projected > write_limit && current_bytes > 0 {
            self.compact_latest()?;
            projected = self
                .file_len_or_zero()?
                .saturating_add(encoded.len() as u64)
                .saturating_add(1);
        }
        if projected > write_limit {
            return Err(format!(
                "intent journal byte budget exceeded: projected={projected}, limit={write_limit}"
            ));
        }
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(|err| format!("append intent journal: {err}"))?;
        file.write_all(&encoded)
            .map_err(|err| format!("write intent receipt: {err}"))?;
        file.write_all(b"\n")
            .map_err(|err| format!("write intent receipt: {err}"))?;
        file.sync_data()
            .map_err(|err| format!("sync intent receipt: {err}"))
    }

    pub fn receive(
        &mut self,
        instruction: &AlphaInstruction,
        now_ms: i64,
    ) -> Result<ReceiptDecision, String> {
        let intent_id = instruction.intent_id.clone().unwrap_or_default();
        let semantic_fingerprint = instruction.semantic_fingerprint();
        let command_hash = instruction.command_hash.clone().unwrap_or_default();
        if let Some(existing) = self.receipts.get(&intent_id) {
            if existing.semantic_fingerprint == semantic_fingerprint
                && existing.command_hash == command_hash
                && existing.schema_version
                    == instruction
                        .schema_version
                        .unwrap_or(EXECUTION_PROTOCOL_VERSION)
                && existing.producer_id == instruction.producer_id.clone().unwrap_or_default()
                && existing.sequence == instruction.sequence.unwrap_or(0)
                && existing.created_at_ms == instruction.created_at_ms.unwrap_or(0)
                && existing.deadline_at_ms == instruction.deadline_at_ms.unwrap_or(0)
            {
                return Ok(ReceiptDecision::Replay(existing.clone()));
            }
            return Ok(ReceiptDecision::Conflict);
        }
        let producer_id = instruction.producer_id.clone().unwrap_or_default();
        let sequence = instruction.sequence.unwrap_or(0);
        if self
            .sequence_owners
            .contains_key(&(producer_id.clone(), sequence))
        {
            return Ok(ReceiptDecision::NonMonotonicSequence);
        }
        if sequence <= self.last_sequences.get(&producer_id).copied().unwrap_or(0) {
            return Ok(ReceiptDecision::NonMonotonicSequence);
        }
        let receipt = IntentReceipt {
            schema_version: instruction
                .schema_version
                .unwrap_or(EXECUTION_PROTOCOL_VERSION),
            intent_id: intent_id.clone(),
            producer_id: producer_id.clone(),
            sequence,
            created_at_ms: instruction.created_at_ms.unwrap_or(0),
            deadline_at_ms: instruction.deadline_at_ms.unwrap_or(0),
            account_id: instruction.account_id.clone().unwrap_or_default(),
            environment: instruction.environment.clone().unwrap_or_default(),
            strategy_id: instruction.strategy_id.clone().unwrap_or_default(),
            cycle_id: instruction.cycle_id.clone().unwrap_or_default(),
            config_version_hash: instruction.config_version_hash.clone().unwrap_or_default(),
            spot_leg_id: instruction.spot_leg_id.clone().unwrap_or_default(),
            perp_leg_id: instruction.perp_leg_id.clone().unwrap_or_default(),
            spot_client_order_id: instruction.spot_client_order_id.clone().unwrap_or_default(),
            perp_client_order_id: instruction.perp_client_order_id.clone().unwrap_or_default(),
            command_hash,
            semantic_fingerprint,
            ack_status: "RECEIVED".to_string(),
            reason: String::new(),
            updated_at_ms: now_ms,
        };
        let write_class = if matches!(
            instruction.intent.trim(),
            "EXIT_LONG" | "EXIT_SHORT" | CONFIG_SYNC_INTENT
        ) {
            JournalWriteClass::SurvivalCommand
        } else {
            JournalWriteClass::NewRisk
        };
        self.append(&receipt, write_class)?;
        self.last_sequences.insert(producer_id, sequence);
        self.sequence_owners.insert(
            (receipt.producer_id.clone(), receipt.sequence),
            intent_id.clone(),
        );
        self.receipts.insert(intent_id, receipt.clone());
        Ok(ReceiptDecision::New(receipt))
    }

    pub fn transition(
        &mut self,
        intent_id: &str,
        ack_status: &str,
        reason: &str,
        now_ms: i64,
    ) -> Result<Option<IntentReceipt>, String> {
        let Some(existing) = self.receipts.get(intent_id) else {
            return Ok(None);
        };
        if ack_rank(ack_status).is_none() {
            return Err(format!("unsupported ACK transition state {ack_status}"));
        }
        // Duplicate, regressive, and post-terminal callbacks are idempotent
        // no-ops.  In particular, a delayed REJECTED cannot overwrite a
        // durable TERMINAL receipt (or vice versa).
        if !valid_ack_progression(&existing.ack_status, ack_status)
            || existing.ack_status == ack_status
        {
            return Ok(Some(existing.clone()));
        }
        let mut updated = existing.clone();
        updated.ack_status = ack_status.to_string();
        updated.reason = reason.to_string();
        updated.updated_at_ms = now_ms;
        self.append(&updated, JournalWriteClass::Transition)?;
        self.receipts.insert(intent_id.to_string(), updated.clone());
        Ok(Some(updated))
    }
}

fn ack_rank(status: &str) -> Option<u8> {
    match status {
        "RECEIVED" => Some(0),
        "VALIDATED" => Some(1),
        "SUBMITTED" => Some(2),
        "TERMINAL" | "REJECTED" => Some(3),
        _ => None,
    }
}

fn valid_ack_progression(current: &str, next: &str) -> bool {
    let (Some(current_rank), Some(next_rank)) = (ack_rank(current), ack_rank(next)) else {
        return false;
    };
    if matches!(current, "TERMINAL" | "REJECTED") {
        return current == next;
    }
    next_rank >= current_rank
}

fn same_command(left: &IntentReceipt, right: &IntentReceipt) -> bool {
    left.schema_version == right.schema_version
        && left.intent_id == right.intent_id
        && left.producer_id == right.producer_id
        && left.sequence == right.sequence
        && left.created_at_ms == right.created_at_ms
        && left.deadline_at_ms == right.deadline_at_ms
        && left.account_id == right.account_id
        && left.environment == right.environment
        && left.strategy_id == right.strategy_id
        && left.cycle_id == right.cycle_id
        && left.config_version_hash == right.config_version_hash
        && left.spot_leg_id == right.spot_leg_id
        && left.perp_leg_id == right.perp_leg_id
        && left.spot_client_order_id == right.spot_client_order_id
        && left.perp_client_order_id == right.perp_client_order_id
        && left.command_hash == right.command_hash
        && left.semantic_fingerprint == right.semantic_fingerprint
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

    pub async fn run(
        &mut self,
        readiness: tokio::sync::oneshot::Sender<Result<(), String>>,
    ) -> Result<(), String> {
        info!("Starting IPC ZeroMQ Receiver on {}", self.endpoint);
        let mut socket = PullSocket::new();

        match socket.bind(&self.endpoint).await {
            Ok(_) => {
                info!("Listening for alpha instructions on {}", self.endpoint);
                let _ = readiness.send(Ok(()));
            }
            Err(e) => {
                let reason = format!("Failed to bind ZeroMQ socket: {e}");
                error!("{}", reason);
                let _ = readiness.send(Err(reason.clone()));
                return Err(reason);
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
                    return Err(format!("ZeroMQ receive loop failed: {e}"));
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn versioned_instruction() -> AlphaInstruction {
        AlphaInstruction {
            symbol: Some("BTCUSDT".to_string()),
            intent: "ENTER_LONG".to_string(),
            quantity: 0.1,
            urgency: 0.5,
            max_slippage_bps: 5.0,
            exposure_scale: 1.0,
            intent_id: Some("protocol-test-intent".to_string()),
            ..AlphaInstruction::default()
        }
        .seal_internal()
    }

    #[test]
    fn protocol_rejects_unknown_version_and_expired_commands() {
        let mut instruction = versioned_instruction();
        let now_ms = instruction.created_at_ms.unwrap();
        instruction.schema_version = Some(EXECUTION_PROTOCOL_VERSION + 1);
        assert_eq!(
            instruction.protocol_error(now_ms),
            Some("unsupported_schema_version")
        );

        instruction.schema_version = Some(EXECUTION_PROTOCOL_VERSION);
        instruction.deadline_at_ms = Some(now_ms + 1);
        assert_eq!(
            instruction.protocol_error(now_ms + 2),
            Some("expired_command")
        );
    }

    #[test]
    fn python_and_rust_share_the_v2_golden_envelope() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/execution_command_v2.json"
        ))
        .unwrap();
        assert_eq!(
            fixture["protocol_version"].as_u64(),
            Some(EXECUTION_PROTOCOL_VERSION as u64)
        );
        let instruction: AlphaInstruction =
            serde_json::from_value(fixture["envelope"].clone()).unwrap();
        let expected_hash = fixture["envelope"]["command_hash"].as_str().unwrap();
        assert_eq!(instruction.semantic_fingerprint(), expected_hash);
        assert_eq!(
            instruction.protocol_error(fixture["created_at_ms"].as_i64().unwrap() + 1),
            None
        );

        let mut mutated = instruction.clone();
        mutated.quantity += 0.001;
        assert_eq!(
            mutated.protocol_error(fixture["created_at_ms"].as_i64().unwrap() + 1),
            Some("command_hash_mismatch")
        );
    }

    fn config_sync_instruction() -> AlphaInstruction {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/config_sync_command_v2.json"
        ))
        .unwrap();
        serde_json::from_value(fixture["envelope"].clone()).unwrap()
    }

    fn reseal_config_sync(instruction: &mut AlphaInstruction) {
        let canonical_json = instruction.config_canonical_json.as_deref().unwrap();
        let mut digest = Sha256::new();
        digest.update(canonical_json.as_bytes());
        instruction.config_version_hash = Some(hex::encode(digest.finalize()));
        instruction.command_hash = Some(instruction.semantic_fingerprint());
    }

    #[test]
    fn python_and_rust_share_the_v2_config_sync_golden_envelope() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/config_sync_command_v2.json"
        ))
        .unwrap();
        assert_eq!(
            fixture["protocol_version"].as_u64(),
            Some(EXECUTION_PROTOCOL_VERSION as u64)
        );
        let instruction = config_sync_instruction();
        assert_eq!(
            instruction.semantic_fingerprint(),
            fixture["envelope"]["command_hash"].as_str().unwrap()
        );
        let now_ms = fixture["created_at_ms"].as_i64().unwrap() + 1;
        assert_eq!(instruction.protocol_error(now_ms), None);
        let snapshot = instruction.validate_config_sync_snapshot().unwrap();
        assert!(snapshot.pause_new_entries);
        assert_eq!(snapshot.per_symbol_notional_cap_usd, "2500");
        assert_eq!(snapshot.max_gross_exposure_usd, "10000");
    }

    #[test]
    fn config_sync_recomputes_hash_and_rejects_unknown_or_raised_risk() {
        let mut mismatched = config_sync_instruction();
        mismatched.config_canonical_json = Some(
            r#"{"max_gross_exposure_usd":10000,"pause_new_entries":false,"per_symbol_notional_cap_usd":2500}"#
                .to_string(),
        );
        mismatched.command_hash = Some(mismatched.semantic_fingerprint());
        assert_eq!(
            mismatched.protocol_error(mismatched.created_at_ms.unwrap() + 1),
            Some("config_hash_mismatch")
        );

        let mut raised = config_sync_instruction();
        raised.config_canonical_json = Some(
            r#"{"max_gross_exposure_usd":22000,"pause_new_entries":false,"per_symbol_notional_cap_usd":5000.01}"#
                .to_string(),
        );
        reseal_config_sync(&mut raised);
        assert_eq!(
            raised.protocol_error(raised.created_at_ms.unwrap() + 1),
            Some("risk_limit_exceeds_compiled_ceiling")
        );

        let mut unknown = config_sync_instruction();
        unknown.config_canonical_json = Some(
            r#"{"future_risk_bypass":true,"max_gross_exposure_usd":10000,"pause_new_entries":true,"per_symbol_notional_cap_usd":2500}"#
                .to_string(),
        );
        reseal_config_sync(&mut unknown);
        assert_eq!(
            unknown.protocol_error(unknown.created_at_ms.unwrap() + 1),
            Some("unknown_config_key")
        );
    }

    #[test]
    fn config_sync_accepts_control_plane_and_storage_guard_keys_without_trusting_them() {
        let mut instruction = config_sync_instruction();
        instruction.config_canonical_json = Some(
            r#"{"allow_reverse_spot_entry":false,"decision_engine_stage":"shadow","live_approval_artifact_path":"","live_approval_required":true,"max_gross_exposure_usd":10000,"pause_new_entries":true,"per_symbol_notional_cap_usd":2500,"research_evidence_min_interval_seconds":900,"storage_component_budgets_bytes":{"rust_journals":150000000},"storage_critical_free_bytes":1000000000,"storage_reserve_bytes":512000000}"#
                .to_string(),
        );
        reseal_config_sync(&mut instruction);

        let snapshot = instruction.validate_config_sync_snapshot().unwrap();
        assert!(snapshot.pause_new_entries);
        assert_eq!(snapshot.per_symbol_notional_cap_usd, "2500");
        assert_eq!(snapshot.max_gross_exposure_usd, "10000");

        // These keys participate in the signed snapshot hash, but they do not
        // weaken the execution engine's compiled entry ceilings or its hard
        // ban on unsupported short-spot entry lifecycle management.
        assert_eq!(
            instruction.protocol_error(instruction.created_at_ms.unwrap() + 1),
            None
        );
    }

    #[test]
    fn config_sync_validates_monotonic_storage_control_shape() {
        let cases = [
            (
                r#"{"max_gross_exposure_usd":10000,"pause_new_entries":false,"per_symbol_notional_cap_usd":2500,"storage_control_generation":0,"storage_emergency_latched":false,"storage_recovery_acknowledged":false}"#,
                Some(StorageControlUpdate {
                    generation: 0,
                    emergency_latched: false,
                    recovery_acknowledged: false,
                }),
            ),
            (
                r#"{"max_gross_exposure_usd":10000,"pause_new_entries":true,"per_symbol_notional_cap_usd":2500,"storage_control_generation":4,"storage_emergency_latched":true,"storage_recovery_acknowledged":false}"#,
                Some(StorageControlUpdate {
                    generation: 4,
                    emergency_latched: true,
                    recovery_acknowledged: false,
                }),
            ),
            (
                r#"{"max_gross_exposure_usd":10000,"pause_new_entries":true,"per_symbol_notional_cap_usd":2500,"storage_control_generation":5,"storage_emergency_latched":false,"storage_recovery_acknowledged":true}"#,
                Some(StorageControlUpdate {
                    generation: 5,
                    emergency_latched: false,
                    recovery_acknowledged: true,
                }),
            ),
        ];
        for (canonical, expected) in cases {
            let mut instruction = config_sync_instruction();
            instruction.config_canonical_json = Some(canonical.to_string());
            reseal_config_sync(&mut instruction);
            assert_eq!(
                instruction
                    .validate_config_sync_snapshot()
                    .unwrap()
                    .storage_control,
                expected
            );
        }

        for invalid in [
            r#"{"max_gross_exposure_usd":10000,"pause_new_entries":true,"per_symbol_notional_cap_usd":2500,"storage_control_generation":1,"storage_emergency_latched":true}"#,
            r#"{"max_gross_exposure_usd":10000,"pause_new_entries":true,"per_symbol_notional_cap_usd":2500,"storage_control_generation":1,"storage_emergency_latched":false,"storage_recovery_acknowledged":false}"#,
        ] {
            let mut instruction = config_sync_instruction();
            instruction.config_canonical_json = Some(invalid.to_string());
            reseal_config_sync(&mut instruction);
            assert_eq!(
                instruction.validate_config_sync_snapshot(),
                Err(ConfigSyncValidationError::InvalidStorageControl)
            );
        }
    }

    #[test]
    fn config_consensus_is_entry_only_fail_closed_and_emits_typed_ack() {
        let instruction = config_sync_instruction();
        let mut consensus = ConfigConsensus::default();
        assert_eq!(
            consensus.entry_block_reason(instruction.config_version_hash.as_deref().unwrap()),
            Some("config_consensus_unavailable")
        );
        let snapshot = consensus.apply(&instruction).unwrap();
        assert_eq!(consensus.active(), Some(&snapshot));
        assert_eq!(
            consensus.applied_hash(),
            Some(snapshot.config_hash.as_str())
        );
        assert_eq!(
            consensus.entry_block_reason("different-config-hash"),
            Some("config_consensus_hash_mismatch")
        );
        assert_eq!(
            consensus.entry_block_reason(&snapshot.config_hash),
            Some("config_pause_new_entries")
        );

        let ack = ConfigAck::applied(&instruction, &snapshot, 1_700_000_000_001, false);
        assert_eq!(ack.event, "ConfigAck");
        assert_eq!(ack.ack_status, "TERMINAL");
        assert_eq!(ack.config_status, "APPLIED");
        assert_eq!(ack.applied_config_hash, snapshot.config_hash);

        // Exit and repair callers have no generic blocker API: only the
        // explicitly named entry gate consults consensus.
        let rejected = ConfigAck::rejected(
            &instruction,
            consensus.applied_hash(),
            "unknown_config_key",
            1_700_000_000_002,
            false,
        );
        assert_eq!(rejected.ack_status, "REJECTED");
        assert_eq!(rejected.config_status, "REJECTED");
        assert_eq!(rejected.applied_config_hash, snapshot.config_hash);
    }

    #[test]
    fn deserializer_rejects_unknown_protocol_fields() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/execution_command_v2.json"
        ))
        .unwrap();
        let mut envelope = fixture["envelope"].clone();
        envelope
            .as_object_mut()
            .unwrap()
            .insert("future_field".to_string(), serde_json::json!(true));
        assert!(serde_json::from_value::<AlphaInstruction>(envelope).is_err());
    }

    #[test]
    fn route_fields_are_sealed_and_nonpromoted_routes_fail_closed() {
        let mut instruction = versioned_instruction();
        let now_ms = instruction.created_at_ms.unwrap();
        assert_eq!(
            instruction.route_policy.as_deref(),
            Some("legacy_dual_maker")
        );
        assert_eq!(
            instruction.route_model_version.as_deref(),
            Some("legacy-v1")
        );
        assert_eq!(
            instruction.max_unhedged_notional_ms,
            Some(DEFAULT_MAX_UNHEDGED_NOTIONAL_MS)
        );
        assert_eq!(instruction.route_slice_count, Some(1));
        assert_eq!(instruction.protocol_error(now_ms), None);

        instruction.route_policy = Some("simultaneous_ioc".to_string());
        assert_eq!(
            instruction.protocol_error(now_ms),
            Some("route_policy_not_promoted")
        );
    }

    #[test]
    fn journal_survives_restart_and_distinguishes_replay_from_conflict() {
        let path = std::env::temp_dir().join(format!(
            "bongus-journal-restart-{}-{}.jsonl",
            std::process::id(),
            versioned_instruction().sequence.unwrap()
        ));
        let _ = std::fs::remove_file(&path);
        let instruction = versioned_instruction();
        let now_ms = instruction.created_at_ms.unwrap();

        let mut journal = IntentJournal::load(&path).unwrap();
        assert!(matches!(
            journal.receive(&instruction, now_ms).unwrap(),
            ReceiptDecision::New(_)
        ));
        journal
            .transition(
                instruction.intent_id.as_deref().unwrap(),
                "SUBMITTED",
                "",
                now_ms + 1,
            )
            .unwrap();
        drop(journal);

        let mut restarted = IntentJournal::load(&path).unwrap();
        match restarted.receive(&instruction, now_ms + 2).unwrap() {
            ReceiptDecision::Replay(receipt) => assert_eq!(receipt.ack_status, "SUBMITTED"),
            other => panic!("expected durable replay, got {other:?}"),
        }

        let mut transport_conflict = instruction.clone();
        transport_conflict.sequence = Some(instruction.sequence.unwrap() + 1);
        assert_eq!(
            restarted.receive(&transport_conflict, now_ms + 2).unwrap(),
            ReceiptDecision::Conflict
        );

        let mut conflicting = instruction.clone();
        conflicting.quantity = 0.2;
        assert_eq!(
            restarted.receive(&conflicting, now_ms + 3).unwrap(),
            ReceiptDecision::Conflict
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn journal_rejects_new_intent_with_reused_sequence() {
        let path = std::env::temp_dir().join(format!(
            "bongus-journal-sequence-{}-{}.jsonl",
            std::process::id(),
            versioned_instruction().sequence.unwrap()
        ));
        let _ = std::fs::remove_file(&path);
        let first = versioned_instruction();
        let mut second = versioned_instruction();
        second.intent_id = Some("protocol-test-intent-2".to_string());
        second.sequence = first.sequence;
        second.command_hash = Some(second.semantic_fingerprint());
        let mut journal = IntentJournal::load(&path).unwrap();
        journal
            .receive(&first, first.created_at_ms.unwrap())
            .unwrap();
        assert_eq!(
            journal
                .receive(&second, second.created_at_ms.unwrap())
                .unwrap(),
            ReceiptDecision::NonMonotonicSequence
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn journal_never_regresses_or_flips_a_terminal_ack() {
        let path = std::env::temp_dir().join(format!(
            "bongus-journal-monotonic-{}-{}.jsonl",
            std::process::id(),
            versioned_instruction().sequence.unwrap()
        ));
        let _ = std::fs::remove_file(&path);
        let instruction = versioned_instruction();
        let intent_id = instruction.intent_id.as_deref().unwrap();
        let now_ms = instruction.created_at_ms.unwrap();
        let mut journal = IntentJournal::load(&path).unwrap();
        journal.receive(&instruction, now_ms).unwrap();
        journal
            .transition(intent_id, "VALIDATED", "", now_ms + 1)
            .unwrap();
        journal
            .transition(intent_id, "SUBMITTED", "", now_ms + 2)
            .unwrap();
        journal
            .transition(intent_id, "TERMINAL", "filled_cycle", now_ms + 3)
            .unwrap();

        let regressed = journal
            .transition(intent_id, "SUBMITTED", "late_callback", now_ms + 4)
            .unwrap()
            .unwrap();
        assert_eq!(regressed.ack_status, "TERMINAL");
        assert_eq!(regressed.reason, "filled_cycle");
        let flipped = journal
            .transition(intent_id, "REJECTED", "late_reject", now_ms + 5)
            .unwrap()
            .unwrap();
        assert_eq!(flipped.ack_status, "TERMINAL");
        assert_eq!(flipped.reason, "filled_cycle");
        assert!(
            journal
                .transition(intent_id, "UNKNOWN", "", now_ms + 6)
                .is_err()
        );

        drop(journal);
        let restarted = IntentJournal::load(&path).unwrap();
        assert_eq!(
            restarted.receipts.get(intent_id).unwrap().ack_status,
            "TERMINAL"
        );
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn journal_pressure_blocks_new_entries_but_preserves_exit_and_terminal_ack() {
        let path = std::env::temp_dir().join(format!(
            "bongus-journal-survival-reserve-{}-{}.jsonl",
            std::process::id(),
            versioned_instruction().sequence.unwrap()
        ));
        let _ = std::fs::remove_file(&path);
        let mut journal = IntentJournal::load_with_limits(&path, 24_000, 12_000).unwrap();
        let mut sequence = 1_u64;
        loop {
            let mut entry = versioned_instruction();
            entry.intent_id = Some(format!("entry-pressure-{sequence}"));
            entry.sequence = Some(sequence);
            entry.command_hash = Some(entry.semantic_fingerprint());
            if journal
                .receive(&entry, entry.created_at_ms.unwrap())
                .is_err()
            {
                break;
            }
            sequence += 1;
        }

        let mut exit = versioned_instruction();
        exit.intent = "EXIT_LONG".to_string();
        exit.intent_id = Some("survival-exit".to_string());
        exit.sequence = Some(sequence);
        exit.command_hash = Some(exit.semantic_fingerprint());
        assert!(matches!(
            journal.receive(&exit, exit.created_at_ms.unwrap()).unwrap(),
            ReceiptDecision::New(_)
        ));
        journal
            .transition(
                "survival-exit",
                "VALIDATED",
                "",
                exit.created_at_ms.unwrap() + 1,
            )
            .unwrap();
        let terminal = journal
            .transition(
                "survival-exit",
                "TERMINAL",
                "reduce_only_exit_complete",
                exit.created_at_ms.unwrap() + 2,
            )
            .unwrap()
            .unwrap();
        assert_eq!(terminal.ack_status, "TERMINAL");
        assert_eq!(terminal.reason, "reduce_only_exit_complete");
        let _ = std::fs::remove_file(path);
    }

    #[test]
    fn intent_journal_compacts_to_latest_monotonic_receipts() {
        let path = std::env::temp_dir().join(format!(
            "bongus-journal-compaction-{}-{}.jsonl",
            std::process::id(),
            versioned_instruction().sequence.unwrap()
        ));
        let _ = std::fs::remove_file(&path);
        let max_bytes = 200_000;
        let mut journal = IntentJournal::load_with_limits(&path, max_bytes, 40_000).unwrap();
        for sequence in 1..=80_u64 {
            let mut instruction = versioned_instruction();
            instruction.intent_id = Some(format!("compacted-intent-{sequence:03}"));
            instruction.sequence = Some(sequence);
            instruction.command_hash = Some(instruction.semantic_fingerprint());
            let now_ms = instruction.created_at_ms.unwrap();
            journal.receive(&instruction, now_ms).unwrap();
            journal
                .transition(
                    instruction.intent_id.as_deref().unwrap(),
                    "VALIDATED",
                    "",
                    now_ms + 1,
                )
                .unwrap();
            journal
                .transition(
                    instruction.intent_id.as_deref().unwrap(),
                    "SUBMITTED",
                    "",
                    now_ms + 2,
                )
                .unwrap();
            journal
                .transition(
                    instruction.intent_id.as_deref().unwrap(),
                    "TERMINAL",
                    "filled_cycle",
                    now_ms + 3,
                )
                .unwrap();
        }
        let bytes = path.metadata().unwrap().len();
        assert!(bytes <= max_bytes);
        let lines = std::fs::read_to_string(&path).unwrap().lines().count();
        assert!(lines < 80 * 4, "transition history should have compacted");

        drop(journal);
        let restarted = IntentJournal::load_with_limits(&path, max_bytes, 40_000).unwrap();
        assert_eq!(restarted.receipts.len(), 80);
        assert!(
            restarted
                .receipts
                .values()
                .all(|receipt| receipt.ack_status == "TERMINAL")
        );
        let _ = std::fs::remove_file(path);
    }
}
