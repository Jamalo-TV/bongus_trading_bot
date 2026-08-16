use crate::order_manager::{EngineEvent, OrderRecoverySnapshot, RecoveryBarrierRelease};
use crate::telemetry::{TelemetryRecoverySnapshot, TelemetryRelayControl};
use crate::user_data_ws::{
    PrivateCursorRecoveryHandle, PrivateCursorRecoverySnapshot, UserDataStreamKind,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, BTreeSet};
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Component, Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::{Mutex, mpsc, oneshot};

const RECOVERY_SCHEMA_VERSION: u16 = 1;
const RECOVERY_EVIDENCE_KIND: &str = "bongus_rust_recovery_generation";
const RECOVERY_RESTORE_POLICY: &str = "empty_runtime_then_signed_reconciliation";
const CONTROL_SCHEMA_VERSION: u16 = 1;
const CONTROL_COMMAND: &str = "create_recovery_generation";
const DEFAULT_BARRIER_TIMEOUT_MS: u64 = 10_000;
const MIN_BARRIER_TIMEOUT_MS: u64 = 1_000;
const MAX_BARRIER_TIMEOUT_MS: u64 = 15_000;
const MAX_CONTROL_LINE_BYTES: usize = 4096;
const COPY_BUFFER_BYTES: usize = 256 * 1024;
const PUBLISHED_GENERATION_RETENTION_COUNT: usize = 1;

const EXPECTED_MEMBER_KEYS: [&str; 6] = [
    "execution_state",
    "intent_journal",
    "telemetry_journal",
    "telemetry_ack_cursor",
    "private_cursor_spot",
    "private_cursor_futures",
];

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct RecoveryMember {
    pub filename: String,
    pub restore_relative_path: String,
    pub sha256: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct RecoveryTelemetryWatermarks {
    pub published_high_water_sequence: u64,
    pub acknowledged_high_water_sequence: u64,
    pub cursor_generation: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct RecoveryPrivateCursorWatermark {
    pub through_ms: Option<i64>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct RecoveryGenerationManifest {
    pub schema_version: u16,
    pub evidence_kind: String,
    pub complete: bool,
    pub restore_policy: String,
    pub generation_id: String,
    pub barrier_request_id: String,
    pub created_at_ms: i64,
    pub terminal_sequence_watermark: u64,
    pub intent_producer_high_watermarks: BTreeMap<String, u64>,
    pub telemetry: RecoveryTelemetryWatermarks,
    pub private_stream_cursors: BTreeMap<String, RecoveryPrivateCursorWatermark>,
    pub members: BTreeMap<String, RecoveryMember>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct RecoveryGenerationResult {
    pub schema_version: u16,
    pub complete: bool,
    pub generation_id: String,
    pub manifest_path: String,
    pub manifest_sha256: String,
    pub manifest_size_bytes: u64,
    pub pause_ms: u64,
}

#[derive(Debug)]
struct RecoveryInputs {
    order: OrderRecoverySnapshot,
    telemetry: TelemetryRecoverySnapshot,
    spot: PrivateCursorRecoverySnapshot,
    futures: PrivateCursorRecoverySnapshot,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum PublishFailpoint {
    None,
    #[cfg(test)]
    AfterFirstMember,
    #[cfg(test)]
    AfterManifestSync,
    #[cfg(test)]
    AfterRename,
}

#[derive(Debug)]
enum MemberSource<'a> {
    File { path: &'a Path, required: bool },
    Bytes(&'a [u8]),
}

#[derive(Debug)]
struct MemberSpec<'a> {
    key: &'static str,
    filename: String,
    restore_relative_path: String,
    source: MemberSource<'a>,
}

fn current_time_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| i64::try_from(duration.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or_default()
}

fn recovery_runtime_path(file_name: &str) -> PathBuf {
    if let Some(runtime_root) =
        std::env::var_os("BONGUS_RUST_RUNTIME_DIR").filter(|value| !value.is_empty())
    {
        return PathBuf::from(runtime_root).join(file_name);
    }
    if let Some(data_root) = std::env::var_os("BONGUS_DATA_ROOT").filter(|value| !value.is_empty())
    {
        return PathBuf::from(data_root)
            .join("runtime")
            .join("rust")
            .join(file_name);
    }
    crate::ipc::default_rust_runtime_path(file_name)
}

fn identifier_is_valid(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
}

fn sha256_bytes(bytes: &[u8]) -> String {
    let mut digest = Sha256::new();
    digest.update(bytes);
    hex::encode(digest.finalize())
}

fn sha256_file(path: &Path) -> Result<(String, u64), String> {
    let mut file = File::open(path)
        .map_err(|error| format!("open recovery member {}: {error}", path.display()))?;
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    let mut buffer = vec![0_u8; COPY_BUFFER_BYTES];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|error| format!("read recovery member {}: {error}", path.display()))?;
        if read == 0 {
            break;
        }
        size = size.saturating_add(read as u64);
        digest.update(&buffer[..read]);
    }
    Ok((hex::encode(digest.finalize()), size))
}

fn ensure_before_deadline(deadline: Instant, context: &str) -> Result<(), String> {
    if Instant::now() >= deadline {
        return Err(format!(
            "recovery generation barrier timed out during {context}"
        ));
    }
    Ok(())
}

fn safe_relative_path(value: &str) -> Result<PathBuf, String> {
    if value.is_empty() || value.contains('\\') {
        return Err(format!("unsafe recovery relative path {value:?}"));
    }
    let path = Path::new(value);
    if path.is_absolute()
        || !path
            .components()
            .all(|component| matches!(component, Component::Normal(_)))
    {
        return Err(format!("unsafe recovery relative path {value:?}"));
    }
    Ok(path.to_path_buf())
}

fn sync_directory(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        File::open(path)
            .map_err(|error| format!("open recovery directory {}: {error}", path.display()))?
            .sync_all()
            .map_err(|error| format!("sync recovery directory {}: {error}", path.display()))?;
    }
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

fn set_file_immutable_permissions(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o440)).map_err(
            |error| {
                format!(
                    "set immutable recovery member permissions {}: {error}",
                    path.display()
                )
            },
        )?;
    }
    #[cfg(not(unix))]
    {
        let mut permissions = std::fs::metadata(path)
            .map_err(|error| format!("inspect recovery member {}: {error}", path.display()))?
            .permissions();
        permissions.set_readonly(true);
        std::fs::set_permissions(path, permissions).map_err(|error| {
            format!(
                "set immutable recovery member permissions {}: {error}",
                path.display()
            )
        })?;
    }
    Ok(())
}

fn set_directory_immutable_permissions(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o550)).map_err(
            |error| {
                format!(
                    "set immutable recovery directory permissions {}: {error}",
                    path.display()
                )
            },
        )?;
    }
    #[cfg(not(unix))]
    let _ = path;
    Ok(())
}

#[allow(clippy::permissions_set_readonly_false)] // Windows has no Unix mode bits.
fn make_tree_writable(path: &Path) -> Result<(), String> {
    let metadata = std::fs::symlink_metadata(path)
        .map_err(|error| format!("inspect stale recovery staging {}: {error}", path.display()))?;
    if metadata.file_type().is_symlink() {
        return Err(format!(
            "refusing symlink in recovery staging tree {}",
            path.display()
        ));
    }
    if metadata.is_dir() {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o700))
                .map_err(|error| format!("make stale recovery directory writable: {error}"))?;
        }
        #[cfg(not(unix))]
        {
            let mut permissions = metadata.permissions();
            permissions.set_readonly(false);
            std::fs::set_permissions(path, permissions)
                .map_err(|error| format!("make stale recovery directory writable: {error}"))?;
        }
        for child in std::fs::read_dir(path)
            .map_err(|error| format!("read stale recovery staging {}: {error}", path.display()))?
        {
            let child = child.map_err(|error| format!("read stale recovery child: {error}"))?;
            make_tree_writable(&child.path())?;
        }
    } else if metadata.is_file() {
        #[cfg(not(unix))]
        {
            let mut permissions = metadata.permissions();
            permissions.set_readonly(false);
            std::fs::set_permissions(path, permissions)
                .map_err(|error| format!("make stale recovery member writable: {error}"))?;
        }
    } else {
        return Err(format!(
            "unsupported entry in recovery staging tree {}",
            path.display()
        ));
    }
    Ok(())
}

fn cleanup_stale_staging(root: &Path) -> Result<(), String> {
    if !root.exists() {
        return Ok(());
    }
    for entry in std::fs::read_dir(root)
        .map_err(|error| format!("read recovery generation root: {error}"))?
    {
        let entry = entry.map_err(|error| format!("read recovery generation entry: {error}"))?;
        let name = entry.file_name().to_string_lossy().to_string();
        if !name.starts_with(".generation-") || !name.ends_with(".staging") {
            continue;
        }
        let path = entry.path();
        let metadata = std::fs::symlink_metadata(&path)
            .map_err(|error| format!("inspect stale recovery staging: {error}"))?;
        if !metadata.is_dir() || metadata.file_type().is_symlink() {
            return Err(format!(
                "refusing unsafe stale recovery staging entry {}",
                path.display()
            ));
        }
        make_tree_writable(&path)?;
        std::fs::remove_dir_all(&path).map_err(|error| {
            format!("remove stale recovery staging {}: {error}", path.display())
        })?;
    }
    sync_directory(root)
}

fn prune_published_generations(root: &Path, protected_generation_id: &str) -> Result<(), String> {
    if !identifier_is_valid(protected_generation_id) {
        return Err("protected recovery generation identity is invalid".to_string());
    }
    let mut published = Vec::new();
    for entry in std::fs::read_dir(root)
        .map_err(|error| format!("read recovery generation root for retention: {error}"))?
    {
        let entry = entry.map_err(|error| format!("read recovery retention entry: {error}"))?;
        let name = entry.file_name().to_string_lossy().to_string();
        if name.starts_with(".generation-") && name.ends_with(".staging") {
            continue;
        }
        if !name.starts_with("generation-") {
            return Err(format!(
                "unexpected recovery generation-root entry during retention: {name}"
            ));
        }
        let directory = entry.path();
        let manifest_path = directory.join("manifest.json");
        let verified = verify_recovery_generation(&manifest_path)?;
        let manifest_bytes = std::fs::read(&manifest_path)
            .map_err(|error| format!("read recovery retention manifest: {error}"))?;
        let manifest: RecoveryGenerationManifest = serde_json::from_slice(&manifest_bytes)
            .map_err(|error| format!("decode recovery retention manifest: {error}"))?;
        if manifest.generation_id != verified.generation_id {
            return Err(
                "recovery retention manifest identity changed after verification".to_string(),
            );
        }
        published.push((manifest.created_at_ms, manifest.generation_id, directory));
    }
    if !published
        .iter()
        .any(|(_, generation_id, _)| generation_id == protected_generation_id)
    {
        return Err("protected recovery generation is missing during retention".to_string());
    }
    published.sort_by(|left, right| (right.0, &right.1).cmp(&(left.0, &left.1)));
    let mut retained = BTreeSet::from([protected_generation_id.to_string()]);
    for (_, generation_id, _) in &published {
        if retained.len() >= PUBLISHED_GENERATION_RETENTION_COUNT {
            break;
        }
        retained.insert(generation_id.clone());
    }
    for (_, generation_id, directory) in published.into_iter().rev() {
        if retained.contains(&generation_id) {
            continue;
        }
        make_tree_writable(&directory)?;
        std::fs::remove_dir_all(&directory).map_err(|error| {
            format!(
                "remove superseded recovery generation {}: {error}",
                directory.display()
            )
        })?;
    }
    sync_directory(root)
}

fn copy_member(
    staging: &Path,
    spec: &MemberSpec<'_>,
    deadline: Instant,
) -> Result<RecoveryMember, String> {
    ensure_before_deadline(deadline, spec.key)?;
    let relative = safe_relative_path(&spec.filename)?;
    let _ = safe_relative_path(&spec.restore_relative_path)?;
    let target = staging.join(relative);
    if let Some(parent) = target.parent() {
        std::fs::create_dir_all(parent).map_err(|error| {
            format!(
                "create recovery member directory {}: {error}",
                parent.display()
            )
        })?;
    }
    let mut output = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&target)
        .map_err(|error| format!("create recovery member {}: {error}", target.display()))?;
    let mut digest = Sha256::new();
    let mut size = 0_u64;
    match &spec.source {
        MemberSource::File { path, required } => match std::fs::symlink_metadata(path) {
            Ok(metadata) => {
                if metadata.file_type().is_symlink() || !metadata.is_file() {
                    return Err(format!(
                        "recovery source {} is not a regular file",
                        path.display()
                    ));
                }
                let expected_size = metadata.len();
                let mut input = File::open(path)
                    .map_err(|error| format!("open recovery source {}: {error}", path.display()))?;
                let mut buffer = vec![0_u8; COPY_BUFFER_BYTES];
                loop {
                    ensure_before_deadline(deadline, spec.key)?;
                    let read = input.read(&mut buffer).map_err(|error| {
                        format!("read recovery source {}: {error}", path.display())
                    })?;
                    if read == 0 {
                        break;
                    }
                    output.write_all(&buffer[..read]).map_err(|error| {
                        format!("write recovery member {}: {error}", target.display())
                    })?;
                    digest.update(&buffer[..read]);
                    size = size
                        .checked_add(read as u64)
                        .ok_or_else(|| "recovery member size overflow".to_string())?;
                }
                if size != expected_size {
                    return Err(format!(
                        "recovery source changed while copying {}: expected {expected_size} bytes, copied {size}",
                        path.display()
                    ));
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound && !*required => {}
            Err(error) => {
                return Err(format!(
                    "inspect recovery source {}: {error}",
                    path.display()
                ));
            }
        },
        MemberSource::Bytes(bytes) => {
            output
                .write_all(bytes)
                .map_err(|error| format!("write recovery member {}: {error}", target.display()))?;
            digest.update(bytes);
            size = bytes.len() as u64;
        }
    }
    output
        .sync_all()
        .map_err(|error| format!("sync recovery member {}: {error}", target.display()))?;
    drop(output);
    set_file_immutable_permissions(&target)?;
    ensure_before_deadline(deadline, spec.key)?;
    Ok(RecoveryMember {
        filename: spec.filename.clone(),
        restore_relative_path: spec.restore_relative_path.clone(),
        sha256: hex::encode(digest.finalize()),
        size_bytes: size,
    })
}

fn publish_recovery_generation_inner(
    root: &Path,
    generation_id: &str,
    inputs: &RecoveryInputs,
    deadline: Instant,
    failpoint: PublishFailpoint,
) -> Result<RecoveryGenerationResult, String> {
    #[cfg(not(test))]
    let _ = failpoint;
    if !identifier_is_valid(generation_id) || !identifier_is_valid(&inputs.order.barrier_request_id)
    {
        return Err("recovery generation identity is invalid".to_string());
    }
    if inputs.spot.stream_kind != UserDataStreamKind::Spot
        || inputs.futures.stream_kind != UserDataStreamKind::Futures
    {
        return Err("private recovery cursor roles are inconsistent".to_string());
    }
    if inputs.telemetry.acknowledged_high_water_sequence
        > inputs.telemetry.published_high_water_sequence
    {
        return Err("telemetry ACK is ahead of its published high-water".to_string());
    }
    if !matches!(inputs.telemetry.active_cursor_suffix.as_str(), ".a" | ".b") {
        return Err("telemetry recovery cursor suffix is invalid".to_string());
    }
    if let Some(path) = &inputs.telemetry.active_cursor_path
        && (!path
            .to_string_lossy()
            .ends_with(&inputs.telemetry.active_cursor_suffix)
            || std::fs::read(path)
                .map_err(|error| format!("re-read active telemetry cursor: {error}"))?
                != inputs.telemetry.active_cursor_bytes)
    {
        return Err("active telemetry cursor changed after the relay barrier".to_string());
    }

    std::fs::create_dir_all(root)
        .map_err(|error| format!("create recovery generation root: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let metadata = std::fs::symlink_metadata(root)
            .map_err(|error| format!("inspect recovery generation root: {error}"))?;
        if metadata.file_type().is_symlink() || !metadata.is_dir() {
            return Err("recovery generation root is not a real directory".to_string());
        }
        std::fs::set_permissions(root, std::fs::Permissions::from_mode(0o750))
            .map_err(|error| format!("secure recovery generation root: {error}"))?;
    }
    cleanup_stale_staging(root)?;
    ensure_before_deadline(deadline, "staging setup")?;

    let final_name = format!("generation-{generation_id}");
    let final_directory = root.join(&final_name);
    let staging = root.join(format!(".{final_name}.staging"));
    if final_directory.exists() || staging.exists() {
        return Err(format!(
            "recovery generation {generation_id} already exists"
        ));
    }
    std::fs::create_dir(&staging)
        .map_err(|error| format!("create recovery staging directory: {error}"))?;

    let cursor_filename = format!(
        "members/execution_telemetry.jsonl.cursor{}",
        inputs.telemetry.active_cursor_suffix
    );
    let cursor_restore_path = format!(
        "execution_telemetry.jsonl.cursor{}",
        inputs.telemetry.active_cursor_suffix
    );
    let specs = [
        MemberSpec {
            key: "execution_state",
            filename: "members/execution_state.jsonl".to_string(),
            restore_relative_path: "execution_state.jsonl".to_string(),
            source: MemberSource::File {
                path: &inputs.order.execution_state_path,
                required: true,
            },
        },
        MemberSpec {
            key: "intent_journal",
            filename: "members/execution_intents.jsonl".to_string(),
            restore_relative_path: "execution_intents.jsonl".to_string(),
            source: MemberSource::File {
                path: &inputs.order.intent_journal_path,
                required: false,
            },
        },
        MemberSpec {
            key: "telemetry_journal",
            filename: "members/execution_telemetry.jsonl".to_string(),
            restore_relative_path: "execution_telemetry.jsonl".to_string(),
            source: MemberSource::File {
                path: &inputs.telemetry.journal_path,
                required: false,
            },
        },
        MemberSpec {
            key: "telemetry_ack_cursor",
            filename: cursor_filename,
            restore_relative_path: cursor_restore_path,
            source: MemberSource::Bytes(&inputs.telemetry.active_cursor_bytes),
        },
        MemberSpec {
            key: "private_cursor_spot",
            filename: "members/private_stream_cursors/spot.jsonl".to_string(),
            restore_relative_path: "private_stream_cursors/spot.jsonl".to_string(),
            source: MemberSource::File {
                path: &inputs.spot.cursor_path,
                required: false,
            },
        },
        MemberSpec {
            key: "private_cursor_futures",
            filename: "members/private_stream_cursors/futures.jsonl".to_string(),
            restore_relative_path: "private_stream_cursors/futures.jsonl".to_string(),
            source: MemberSource::File {
                path: &inputs.futures.cursor_path,
                required: false,
            },
        },
    ];

    let mut members = BTreeMap::new();
    for (index, spec) in specs.iter().enumerate() {
        let member = copy_member(&staging, spec, deadline)?;
        members.insert(spec.key.to_string(), member);
        #[cfg(test)]
        if index == 0 && failpoint == PublishFailpoint::AfterFirstMember {
            return Err("simulated crash after first recovery member".to_string());
        }
        #[cfg(not(test))]
        let _ = index;
    }

    let private_stream_cursors = BTreeMap::from([
        (
            "futures".to_string(),
            RecoveryPrivateCursorWatermark {
                through_ms: inputs.futures.through_ms,
            },
        ),
        (
            "spot".to_string(),
            RecoveryPrivateCursorWatermark {
                through_ms: inputs.spot.through_ms,
            },
        ),
    ]);
    let manifest = RecoveryGenerationManifest {
        schema_version: RECOVERY_SCHEMA_VERSION,
        evidence_kind: RECOVERY_EVIDENCE_KIND.to_string(),
        complete: true,
        restore_policy: RECOVERY_RESTORE_POLICY.to_string(),
        generation_id: generation_id.to_string(),
        barrier_request_id: inputs.order.barrier_request_id.clone(),
        created_at_ms: current_time_ms().max(1),
        terminal_sequence_watermark: inputs.order.terminal_sequence_watermark,
        intent_producer_high_watermarks: inputs.order.intent_producer_high_watermarks.clone(),
        telemetry: RecoveryTelemetryWatermarks {
            published_high_water_sequence: inputs.telemetry.published_high_water_sequence,
            acknowledged_high_water_sequence: inputs.telemetry.acknowledged_high_water_sequence,
            cursor_generation: inputs.telemetry.cursor_generation,
        },
        private_stream_cursors,
        members,
    };
    let mut manifest_bytes = serde_json::to_vec_pretty(&manifest)
        .map_err(|error| format!("encode recovery manifest: {error}"))?;
    manifest_bytes.push(b'\n');
    let manifest_staging_path = staging.join("manifest.json");
    let mut manifest_file = OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(&manifest_staging_path)
        .map_err(|error| format!("create recovery manifest: {error}"))?;
    manifest_file
        .write_all(&manifest_bytes)
        .map_err(|error| format!("write recovery manifest: {error}"))?;
    manifest_file
        .sync_all()
        .map_err(|error| format!("sync recovery manifest: {error}"))?;
    drop(manifest_file);
    set_file_immutable_permissions(&manifest_staging_path)?;
    sync_directory(&staging.join("members/private_stream_cursors"))?;
    sync_directory(&staging.join("members"))?;
    sync_directory(&staging)?;
    #[cfg(test)]
    if failpoint == PublishFailpoint::AfterManifestSync {
        return Err("simulated crash after recovery manifest sync".to_string());
    }
    set_directory_immutable_permissions(&staging.join("members/private_stream_cursors"))?;
    set_directory_immutable_permissions(&staging.join("members"))?;
    set_directory_immutable_permissions(&staging)?;
    ensure_before_deadline(deadline, "atomic generation publish")?;
    std::fs::rename(&staging, &final_directory)
        .map_err(|error| format!("atomically publish recovery generation: {error}"))?;
    sync_directory(root)?;
    #[cfg(test)]
    if failpoint == PublishFailpoint::AfterRename {
        return Err("simulated crash after recovery generation rename".to_string());
    }
    ensure_before_deadline(deadline, "generation publication")?;

    let final_manifest = final_directory.join("manifest.json");
    let manifest_path = final_manifest
        .to_str()
        .ok_or_else(|| "recovery manifest path is not valid UTF-8".to_string())?
        .to_string();
    Ok(RecoveryGenerationResult {
        schema_version: CONTROL_SCHEMA_VERSION,
        complete: true,
        generation_id: generation_id.to_string(),
        manifest_path,
        manifest_sha256: sha256_bytes(&manifest_bytes),
        manifest_size_bytes: manifest_bytes.len() as u64,
        pause_ms: 0,
    })
}

fn publish_recovery_generation(
    root: &Path,
    generation_id: &str,
    inputs: &RecoveryInputs,
    deadline: Instant,
    failpoint: PublishFailpoint,
) -> Result<RecoveryGenerationResult, String> {
    let result =
        publish_recovery_generation_inner(root, generation_id, inputs, deadline, failpoint);
    #[cfg(test)]
    let preserve_crash_artifacts = failpoint != PublishFailpoint::None;
    #[cfg(not(test))]
    let preserve_crash_artifacts = false;
    if result.is_err() && !preserve_crash_artifacts {
        let staging = root.join(format!(".generation-{generation_id}.staging"));
        if staging.exists() {
            let _ = make_tree_writable(&staging);
            let _ = std::fs::remove_dir_all(staging);
            let _ = sync_directory(root);
        }
    }
    result
}

fn collect_generation_files(
    root: &Path,
    directory: &Path,
    files: &mut BTreeSet<String>,
) -> Result<(), String> {
    let metadata = std::fs::symlink_metadata(directory)
        .map_err(|error| format!("inspect recovery generation directory: {error}"))?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(format!(
            "recovery generation contains an unsafe directory {}",
            directory.display()
        ));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o222 != 0 {
            return Err(format!(
                "recovery generation directory remains writable: {}",
                directory.display()
            ));
        }
    }
    for entry in std::fs::read_dir(directory)
        .map_err(|error| format!("read recovery generation directory: {error}"))?
    {
        let entry = entry.map_err(|error| format!("read recovery generation entry: {error}"))?;
        let path = entry.path();
        let metadata = std::fs::symlink_metadata(&path)
            .map_err(|error| format!("inspect recovery generation entry: {error}"))?;
        if metadata.file_type().is_symlink() {
            return Err(format!(
                "recovery generation contains a symlink {}",
                path.display()
            ));
        }
        if metadata.is_dir() {
            collect_generation_files(root, &path, files)?;
            continue;
        }
        if !metadata.is_file() {
            return Err(format!(
                "recovery generation contains a non-file entry {}",
                path.display()
            ));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            if metadata.permissions().mode() & 0o222 != 0 {
                return Err(format!(
                    "recovery generation member remains writable: {}",
                    path.display()
                ));
            }
        }
        let relative = path
            .strip_prefix(root)
            .map_err(|_| "recovery member escaped generation root".to_string())?;
        let normalized = relative
            .components()
            .map(|component| component.as_os_str().to_string_lossy())
            .collect::<Vec<_>>()
            .join("/");
        if !files.insert(normalized.clone()) {
            return Err(format!("duplicate recovery generation member {normalized}"));
        }
    }
    Ok(())
}

fn scan_jsonl_values(path: &Path) -> Result<Vec<serde_json::Value>, String> {
    let file = File::open(path)
        .map_err(|error| format!("open recovery JSONL member {}: {error}", path.display()))?;
    let mut rows = Vec::new();
    for (index, line) in BufReader::new(file).lines().enumerate() {
        let line = line
            .map_err(|error| format!("read recovery JSONL member {}: {error}", path.display()))?;
        if line.trim().is_empty() {
            continue;
        }
        let row = serde_json::from_str(&line).map_err(|error| {
            format!(
                "invalid recovery JSONL member {} line {}: {error}",
                path.display(),
                index + 1
            )
        })?;
        rows.push(row);
    }
    Ok(rows)
}

fn verify_execution_state_semantics(
    path: &Path,
    manifest: &RecoveryGenerationManifest,
) -> Result<(), String> {
    let rows = scan_jsonl_values(path)?;
    let latest = rows
        .last()
        .ok_or_else(|| "recovery execution state is empty".to_string())?;
    let watermark = latest
        .get("terminal_sequence_watermark")
        .and_then(serde_json::Value::as_u64)
        .ok_or_else(|| "recovery execution state lacks terminal_sequence_watermark".to_string())?;
    if watermark != manifest.terminal_sequence_watermark {
        return Err(format!(
            "execution terminal watermark mismatch: manifest={}, state={watermark}",
            manifest.terminal_sequence_watermark
        ));
    }
    let expected_reason = format!(
        "recovery_generation_barrier_active:{}",
        manifest.barrier_request_id
    );
    if latest
        .get("continuous_risk_reason")
        .and_then(serde_json::Value::as_str)
        != Some(expected_reason.as_str())
    {
        return Err("execution state does not carry the matching active barrier".to_string());
    }
    Ok(())
}

fn verify_intent_watermarks(path: &Path, expected: &BTreeMap<String, u64>) -> Result<(), String> {
    let mut actual = BTreeMap::<String, u64>::new();
    for row in scan_jsonl_values(path)? {
        let producer = row
            .get("producer_id")
            .and_then(serde_json::Value::as_str)
            .ok_or_else(|| "intent journal row lacks producer_id".to_string())?;
        let sequence = row
            .get("sequence")
            .and_then(serde_json::Value::as_u64)
            .filter(|sequence| *sequence > 0)
            .ok_or_else(|| "intent journal row lacks a positive sequence".to_string())?;
        actual
            .entry(producer.to_string())
            .and_modify(|value| *value = (*value).max(sequence))
            .or_insert(sequence);
    }
    if &actual != expected {
        return Err(format!(
            "intent producer high-water mismatch: manifest={expected:?}, journal={actual:?}"
        ));
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

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RecoveryTelemetryCursor {
    schema_version: u16,
    generation: u64,
    high_water_sequence: u64,
    consumer_id: String,
    checksum: String,
}

fn verify_telemetry_semantics(
    journal_path: &Path,
    cursor_path: &Path,
    expected: &RecoveryTelemetryWatermarks,
) -> Result<(), String> {
    if expected.acknowledged_high_water_sequence > expected.published_high_water_sequence {
        return Err("recovery telemetry ACK is ahead of published high-water".to_string());
    }
    let cursor_bytes = std::fs::read(cursor_path)
        .map_err(|error| format!("read recovery telemetry cursor: {error}"))?;
    let cursor: RecoveryTelemetryCursor = serde_json::from_slice(&cursor_bytes)
        .map_err(|error| format!("decode recovery telemetry cursor: {error}"))?;
    if cursor.schema_version != 1
        || cursor.generation != expected.cursor_generation
        || cursor.high_water_sequence != expected.acknowledged_high_water_sequence
        || cursor.consumer_id.trim().is_empty()
        || cursor.checksum
            != telemetry_cursor_checksum(
                cursor.generation,
                cursor.high_water_sequence,
                &cursor.consumer_id,
            )
    {
        return Err("recovery telemetry cursor semantics are invalid".to_string());
    }
    let expected_suffix = if cursor.generation.is_multiple_of(2) {
        ".a"
    } else {
        ".b"
    };
    if !cursor_path.to_string_lossy().ends_with(expected_suffix) {
        return Err("recovery telemetry cursor generation/suffix mismatch".to_string());
    }

    let mut previous = 0_u64;
    let mut last = None;
    for row in scan_jsonl_values(journal_path)? {
        let sequence = row
            .get("sequence")
            .and_then(serde_json::Value::as_u64)
            .filter(|sequence| *sequence > 0)
            .ok_or_else(|| "telemetry journal row lacks a positive sequence".to_string())?;
        if sequence <= previous {
            return Err("recovery telemetry journal sequence is not monotonic".to_string());
        }
        previous = sequence;
        last = Some(sequence);
    }
    let effective_high_water = last.unwrap_or(expected.acknowledged_high_water_sequence);
    if effective_high_water != expected.published_high_water_sequence {
        return Err(format!(
            "telemetry published high-water mismatch: manifest={}, journal={effective_high_water}",
            expected.published_high_water_sequence
        ));
    }
    Ok(())
}

fn verify_private_cursor(
    path: &Path,
    stream: &str,
    expected_through_ms: Option<i64>,
) -> Result<(), String> {
    let mut previous = None;
    for row in scan_jsonl_values(path)? {
        if row.get("stream").and_then(serde_json::Value::as_str) != Some(stream) {
            return Err(format!(
                "recovery private cursor role mismatch for {stream}"
            ));
        }
        let through_ms = row
            .get("through_ms")
            .and_then(serde_json::Value::as_i64)
            .filter(|value| *value >= 0)
            .ok_or_else(|| format!("recovery private cursor {stream} has invalid through_ms"))?;
        if previous.is_some_and(|value| through_ms < value) {
            return Err(format!("recovery private cursor {stream} regressed"));
        }
        previous = Some(through_ms);
    }
    if previous != expected_through_ms {
        return Err(format!(
            "private cursor {stream} watermark mismatch: manifest={expected_through_ms:?}, cursor={previous:?}"
        ));
    }
    Ok(())
}

pub(crate) fn verify_recovery_generation(
    manifest_path: &Path,
) -> Result<RecoveryGenerationResult, String> {
    let manifest_metadata = std::fs::symlink_metadata(manifest_path)
        .map_err(|error| format!("inspect recovery manifest: {error}"))?;
    if manifest_metadata.file_type().is_symlink()
        || !manifest_metadata.is_file()
        || manifest_metadata.len() > 1024 * 1024
    {
        return Err("recovery manifest is not a bounded regular file".to_string());
    }
    let manifest_bytes =
        std::fs::read(manifest_path).map_err(|error| format!("read recovery manifest: {error}"))?;
    let manifest: RecoveryGenerationManifest = serde_json::from_slice(&manifest_bytes)
        .map_err(|error| format!("decode recovery manifest: {error}"))?;
    if manifest.schema_version != RECOVERY_SCHEMA_VERSION
        || manifest.evidence_kind != RECOVERY_EVIDENCE_KIND
        || !manifest.complete
        || manifest.restore_policy != RECOVERY_RESTORE_POLICY
        || manifest.created_at_ms <= 0
        || !identifier_is_valid(&manifest.generation_id)
        || !identifier_is_valid(&manifest.barrier_request_id)
    {
        return Err("recovery manifest identity is invalid".to_string());
    }
    let generation_directory = manifest_path
        .parent()
        .ok_or_else(|| "recovery manifest has no generation directory".to_string())?;
    let expected_directory_name = format!("generation-{}", manifest.generation_id);
    if generation_directory
        .file_name()
        .and_then(|name| name.to_str())
        != Some(expected_directory_name.as_str())
    {
        return Err("recovery generation directory/id mismatch".to_string());
    }
    let member_keys = manifest
        .members
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    let expected_keys = EXPECTED_MEMBER_KEYS.into_iter().collect::<BTreeSet<_>>();
    if member_keys != expected_keys {
        return Err("recovery manifest member set is not exact".to_string());
    }
    let private_keys = manifest
        .private_stream_cursors
        .keys()
        .map(String::as_str)
        .collect::<BTreeSet<_>>();
    if private_keys != BTreeSet::from(["futures", "spot"])
        || manifest
            .intent_producer_high_watermarks
            .iter()
            .any(|(producer, sequence)| producer.trim().is_empty() || *sequence == 0)
    {
        return Err("recovery manifest cursor/high-water set is invalid".to_string());
    }

    let mut expected_files = BTreeSet::from(["manifest.json".to_string()]);
    let mut restore_paths = BTreeSet::new();
    for (key, member) in &manifest.members {
        let filename = safe_relative_path(&member.filename)?;
        let _ = safe_relative_path(&member.restore_relative_path)?;
        if !member.filename.starts_with("members/")
            || member.sha256.len() != 64
            || !member
                .sha256
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            || !expected_files.insert(member.filename.clone())
            || !restore_paths.insert(member.restore_relative_path.clone())
        {
            return Err(format!("recovery member {key} metadata is invalid"));
        }
        let member_path = generation_directory.join(filename);
        let metadata = std::fs::symlink_metadata(&member_path)
            .map_err(|error| format!("inspect recovery member {key}: {error}"))?;
        if metadata.file_type().is_symlink() || !metadata.is_file() {
            return Err(format!("recovery member {key} is not a regular file"));
        }
        let cap = match key.as_str() {
            "execution_state" | "telemetry_journal" => 32_000_000,
            "intent_journal" => 82_000_000,
            "telemetry_ack_cursor" => 4096,
            "private_cursor_spot" | "private_cursor_futures" => 17_000_000,
            _ => 0,
        };
        if metadata.len() != member.size_bytes || metadata.len() > cap {
            return Err(format!("recovery member {key} size is invalid"));
        }
        let (sha256, size) = sha256_file(&member_path)?;
        if size != member.size_bytes || sha256 != member.sha256 {
            return Err(format!("recovery member {key} hash mismatch"));
        }
    }
    let mut actual_files = BTreeSet::new();
    collect_generation_files(
        generation_directory,
        generation_directory,
        &mut actual_files,
    )?;
    if actual_files != expected_files {
        return Err(format!(
            "recovery generation file set is not exact: expected={expected_files:?}, actual={actual_files:?}"
        ));
    }

    let member_path = |key: &str| -> Result<PathBuf, String> {
        let member = manifest
            .members
            .get(key)
            .ok_or_else(|| format!("missing recovery member {key}"))?;
        Ok(generation_directory.join(safe_relative_path(&member.filename)?))
    };
    verify_execution_state_semantics(&member_path("execution_state")?, &manifest)?;
    verify_intent_watermarks(
        &member_path("intent_journal")?,
        &manifest.intent_producer_high_watermarks,
    )?;
    verify_telemetry_semantics(
        &member_path("telemetry_journal")?,
        &member_path("telemetry_ack_cursor")?,
        &manifest.telemetry,
    )?;
    verify_private_cursor(
        &member_path("private_cursor_spot")?,
        "spot",
        manifest
            .private_stream_cursors
            .get("spot")
            .and_then(|cursor| cursor.through_ms),
    )?;
    verify_private_cursor(
        &member_path("private_cursor_futures")?,
        "futures",
        manifest
            .private_stream_cursors
            .get("futures")
            .and_then(|cursor| cursor.through_ms),
    )?;

    Ok(RecoveryGenerationResult {
        schema_version: CONTROL_SCHEMA_VERSION,
        complete: true,
        generation_id: manifest.generation_id,
        manifest_path: manifest_path
            .to_str()
            .ok_or_else(|| "recovery manifest path is not valid UTF-8".to_string())?
            .to_string(),
        manifest_sha256: sha256_bytes(&manifest_bytes),
        manifest_size_bytes: manifest_bytes.len() as u64,
        pause_ms: 0,
    })
}

#[derive(Clone)]
pub(crate) struct RecoveryCoordinator {
    order_tx: mpsc::Sender<EngineEvent>,
    telemetry_tx: mpsc::Sender<TelemetryRelayControl>,
    spot_cursor: PrivateCursorRecoveryHandle,
    futures_cursor: PrivateCursorRecoveryHandle,
    generations_root: PathBuf,
    barrier_timeout: Duration,
    serial: Arc<Mutex<()>>,
}

impl RecoveryCoordinator {
    pub(crate) fn from_env(
        order_tx: mpsc::Sender<EngineEvent>,
        telemetry_tx: mpsc::Sender<TelemetryRelayControl>,
        spot_cursor: PrivateCursorRecoveryHandle,
        futures_cursor: PrivateCursorRecoveryHandle,
    ) -> Result<Self, String> {
        let generations_root = std::env::var("BONGUS_RECOVERY_GENERATIONS_DIR")
            .map(PathBuf::from)
            .unwrap_or_else(|_| recovery_runtime_path("recovery_generations"));
        let generations_root = if generations_root.is_absolute() {
            generations_root
        } else {
            std::env::current_dir()
                .map_err(|error| format!("resolve recovery generation root: {error}"))?
                .join(generations_root)
        };
        let timeout_ms = match std::env::var("BONGUS_RECOVERY_BARRIER_TIMEOUT_MS") {
            Ok(value) => value
                .parse::<u64>()
                .ok()
                .filter(|value| {
                    (MIN_BARRIER_TIMEOUT_MS..=MAX_BARRIER_TIMEOUT_MS).contains(value)
                })
                .ok_or_else(|| {
                    format!(
                        "BONGUS_RECOVERY_BARRIER_TIMEOUT_MS must be {MIN_BARRIER_TIMEOUT_MS}..={MAX_BARRIER_TIMEOUT_MS}"
                    )
                })?,
            Err(_) => DEFAULT_BARRIER_TIMEOUT_MS,
        };
        Ok(Self {
            order_tx,
            telemetry_tx,
            spot_cursor,
            futures_cursor,
            generations_root,
            barrier_timeout: Duration::from_millis(timeout_ms),
            serial: Arc::new(Mutex::new(())),
        })
    }

    #[cfg(test)]
    fn for_test(
        order_tx: mpsc::Sender<EngineEvent>,
        telemetry_tx: mpsc::Sender<TelemetryRelayControl>,
        spot_cursor: PrivateCursorRecoveryHandle,
        futures_cursor: PrivateCursorRecoveryHandle,
        generations_root: PathBuf,
        barrier_timeout: Duration,
    ) -> Self {
        Self {
            order_tx,
            telemetry_tx,
            spot_cursor,
            futures_cursor,
            generations_root,
            barrier_timeout,
            serial: Arc::new(Mutex::new(())),
        }
    }

    async fn before_deadline<T, F>(
        deadline: tokio::time::Instant,
        context: &str,
        future: F,
    ) -> Result<T, String>
    where
        F: std::future::Future<Output = T>,
    {
        tokio::time::timeout_at(deadline, future)
            .await
            .map_err(|_| format!("recovery generation barrier timed out during {context}"))
    }

    pub(crate) async fn create_generation(
        &self,
        request_id: String,
    ) -> Result<RecoveryGenerationResult, String> {
        if !identifier_is_valid(&request_id) {
            return Err("recovery control request id is invalid".to_string());
        }
        let serialization_deadline = tokio::time::Instant::now() + self.barrier_timeout;
        let _serial = Self::before_deadline(
            serialization_deadline,
            "waiting for another recovery request",
            self.serial.lock(),
        )
        .await?;
        std::fs::create_dir_all(&self.generations_root)
            .map_err(|error| format!("create recovery generation root: {error}"))?;
        cleanup_stale_staging(&self.generations_root)?;

        let generation_id = format!(
            "{}-{:016x}",
            current_time_ms().max(1),
            rand::random::<u64>()
        );
        let pause_started = Instant::now();
        let deadline = pause_started + self.barrier_timeout;
        let async_deadline = tokio::time::Instant::from_std(deadline);

        let (order_reply_tx, order_reply_rx) = oneshot::channel();
        let (order_release_tx, order_release_rx) = oneshot::channel();
        let (order_resumed_tx, order_resumed_rx) = oneshot::channel();
        Self::before_deadline(
            async_deadline,
            "requesting the order-actor barrier",
            self.order_tx.send(EngineEvent::RecoveryBarrier {
                request_id: request_id.clone(),
                reply: order_reply_tx,
                release: order_release_rx,
                resumed: order_resumed_tx,
            }),
        )
        .await?
        .map_err(|_| "order actor is unavailable for recovery barrier".to_string())?;
        let order = Self::before_deadline(
            async_deadline,
            "waiting for the order-actor barrier",
            order_reply_rx,
        )
        .await?
        .map_err(|_| "order actor dropped the recovery barrier reply".to_string())??;
        if order.barrier_request_id != request_id {
            return Err("order actor returned a mismatched barrier request id".to_string());
        }

        let (telemetry_reply_tx, telemetry_reply_rx) = oneshot::channel();
        let (telemetry_release_tx, telemetry_release_rx) = oneshot::channel();
        let (telemetry_resumed_tx, telemetry_resumed_rx) = oneshot::channel();
        Self::before_deadline(
            async_deadline,
            "requesting the telemetry relay barrier",
            self.telemetry_tx
                .send(TelemetryRelayControl::RecoveryBarrier {
                    request_id: request_id.clone(),
                    reply: telemetry_reply_tx,
                    release: telemetry_release_rx,
                    resumed: telemetry_resumed_tx,
                }),
        )
        .await?
        .map_err(|_| "telemetry relay is unavailable for recovery barrier".to_string())?;
        let telemetry = Self::before_deadline(
            async_deadline,
            "waiting for the telemetry relay barrier",
            telemetry_reply_rx,
        )
        .await?
        .map_err(|_| "telemetry relay dropped the recovery barrier reply".to_string())??;

        let spot = Self::before_deadline(
            async_deadline,
            "locking the Spot private cursor",
            self.spot_cursor.lock_for_recovery(),
        )
        .await?;
        let futures = Self::before_deadline(
            async_deadline,
            "locking the Futures private cursor",
            self.futures_cursor.lock_for_recovery(),
        )
        .await?;
        let spot_snapshot = spot.prepare_recovery_snapshot()?;
        let futures_snapshot = futures.prepare_recovery_snapshot()?;
        let inputs = RecoveryInputs {
            order,
            telemetry,
            spot: spot_snapshot,
            futures: futures_snapshot,
        };
        let mut result = match publish_recovery_generation(
            &self.generations_root,
            &generation_id,
            &inputs,
            deadline,
            PublishFailpoint::None,
        ) {
            Ok(result) => result,
            Err(error) => {
                drop(futures);
                drop(spot);
                let _ = telemetry_release_tx.send(());
                let _ = order_release_tx.send(RecoveryBarrierRelease::Failed {
                    reason: error.clone(),
                });
                return Err(error);
            }
        };

        // The immutable rename is the commit point. Release cursor writers,
        // then prove the telemetry relay has dropped its journal mutex before
        // allowing the order actor to emit post-barrier state.
        drop(futures);
        drop(spot);
        telemetry_release_tx
            .send(())
            .map_err(|_| "telemetry relay disappeared at recovery release".to_string())?;
        Self::before_deadline(
            async_deadline,
            "waiting for telemetry relay resume",
            telemetry_resumed_rx,
        )
        .await?
        .map_err(|_| "telemetry relay did not confirm recovery resume".to_string())?;

        order_release_tx
            .send(RecoveryBarrierRelease::Published {
                generation_id: generation_id.clone(),
            })
            .map_err(|_| "order actor disappeared at recovery release".to_string())?;
        let order_resume_result = match Self::before_deadline(
            async_deadline,
            "waiting for order actor resume",
            order_resumed_rx,
        )
        .await
        {
            Ok(Ok(result)) => result,
            Ok(Err(_)) => Err("order actor dropped recovery resume acknowledgement".to_string()),
            Err(error) => {
                let _ = self.order_tx.try_send(EngineEvent::RecoveryBarrierFailed {
                    request_id: request_id.clone(),
                    reason: "ambiguous order-actor resume after immutable generation publish"
                        .to_string(),
                });
                return Err(error);
            }
        };
        order_resume_result?;
        result.pause_ms = u64::try_from(pause_started.elapsed().as_millis()).unwrap_or(u64::MAX);
        prune_published_generations(&self.generations_root, &generation_id).map_err(|error| {
            format!("recovery generation was published but source retention failed: {error}")
        })?;
        Ok(result)
    }
}

#[derive(Debug, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RecoveryControlRequest {
    schema_version: u16,
    command: String,
    request_id: String,
}

#[derive(Debug, Serialize)]
#[serde(deny_unknown_fields)]
struct RecoveryControlError {
    schema_version: u16,
    complete: bool,
    request_id: String,
    error: String,
}

pub(crate) fn recovery_control_socket_path() -> PathBuf {
    std::env::var("BONGUS_RECOVERY_CONTROL_SOCKET")
        .map(PathBuf::from)
        .unwrap_or_else(|_| recovery_runtime_path("recovery-control.sock"))
}

fn safe_control_error(error: &str) -> String {
    error
        .chars()
        .filter(|character| !character.is_control())
        .take(1024)
        .collect()
}

async fn read_bounded_line<R>(
    reader: &mut R,
    max_bytes: usize,
    deadline: tokio::time::Instant,
    label: &str,
) -> Result<Vec<u8>, String>
where
    R: tokio::io::AsyncRead + Unpin,
{
    use tokio::io::AsyncReadExt;

    let mut line = vec![0_u8; max_bytes.saturating_add(1)];
    let mut used = 0_usize;
    loop {
        if used >= line.len() {
            return Err(format!("{label} exceeded byte cap"));
        }
        let read = tokio::time::timeout_at(deadline, reader.read(&mut line[used..]))
            .await
            .map_err(|_| format!("{label} read timed out"))?
            .map_err(|error| format!("read {label}: {error}"))?;
        if read == 0 {
            return Err(format!("{label} ended before its newline delimiter"));
        }
        used = used.saturating_add(read);
        if let Some(newline) = line[..used].iter().position(|byte| *byte == b'\n') {
            if newline + 1 != used {
                return Err(format!("{label} contained trailing bytes"));
            }
            if used > max_bytes {
                return Err(format!("{label} exceeded byte cap"));
            }
            line.truncate(used);
            return Ok(line);
        }
        if used >= max_bytes {
            return Err(format!("{label} exceeded byte cap"));
        }
    }
}

#[cfg(unix)]
struct RecoverySocketCleanup {
    path: PathBuf,
    device: u64,
    inode: u64,
}

#[cfg(unix)]
impl Drop for RecoverySocketCleanup {
    fn drop(&mut self) {
        use std::os::unix::fs::{FileTypeExt, MetadataExt};
        if std::fs::symlink_metadata(&self.path).is_ok_and(|metadata| {
            metadata.file_type().is_socket()
                && metadata.dev() == self.device
                && metadata.ino() == self.inode
        }) {
            let _ = std::fs::remove_file(&self.path);
        }
    }
}

#[cfg(unix)]
pub(crate) async fn run_recovery_control_server(
    coordinator: RecoveryCoordinator,
    ready: Option<oneshot::Sender<()>>,
) -> Result<(), String> {
    run_recovery_control_server_at(coordinator, ready, recovery_control_socket_path()).await
}

#[cfg(unix)]
async fn run_recovery_control_server_at(
    coordinator: RecoveryCoordinator,
    ready: Option<oneshot::Sender<()>>,
    configured: PathBuf,
) -> Result<(), String> {
    use std::os::unix::fs::{FileTypeExt, MetadataExt, PermissionsExt};
    use tokio::io::AsyncWriteExt;
    use tokio::net::UnixListener;

    let socket_path = if configured.is_absolute() {
        configured
    } else {
        std::env::current_dir()
            .map_err(|error| format!("resolve recovery control socket: {error}"))?
            .join(configured)
    };
    let parent = socket_path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .ok_or_else(|| "recovery control socket has no parent directory".to_string())?;
    let parent_preexisting = parent.exists();
    std::fs::create_dir_all(parent)
        .map_err(|error| format!("create recovery control directory: {error}"))?;
    if !parent_preexisting {
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o750))
            .map_err(|error| format!("set recovery control directory mode 0750: {error}"))?;
    }
    let parent_metadata = std::fs::symlink_metadata(parent)
        .map_err(|error| format!("inspect recovery control directory: {error}"))?;
    if parent_metadata.file_type().is_symlink()
        || !parent_metadata.is_dir()
        || parent_metadata.permissions().mode() & 0o777 != 0o750
    {
        return Err("recovery control parent must be a real directory with mode 0750".to_string());
    }
    if socket_path.exists() {
        let metadata = std::fs::symlink_metadata(&socket_path)
            .map_err(|error| format!("inspect stale recovery control socket: {error}"))?;
        if metadata.file_type().is_symlink() || !metadata.file_type().is_socket() {
            return Err(format!(
                "refusing to replace non-socket recovery control path {}",
                socket_path.display()
            ));
        }
        match std::os::unix::net::UnixStream::connect(&socket_path) {
            Ok(_) => {
                return Err(format!(
                    "recovery control socket {} is already active",
                    socket_path.display()
                ));
            }
            Err(error)
                if matches!(
                    error.kind(),
                    std::io::ErrorKind::ConnectionRefused | std::io::ErrorKind::NotFound
                ) => {}
            Err(error) => {
                return Err(format!(
                    "could not prove recovery control socket is stale: {error}"
                ));
            }
        }
        std::fs::remove_file(&socket_path)
            .map_err(|error| format!("remove stale recovery control socket: {error}"))?;
    }
    let listener = UnixListener::bind(&socket_path)
        .map_err(|error| format!("bind recovery control socket: {error}"))?;
    std::fs::set_permissions(&socket_path, std::fs::Permissions::from_mode(0o660))
        .map_err(|error| format!("set recovery control socket mode 0660: {error}"))?;
    let socket_metadata = std::fs::symlink_metadata(&socket_path)
        .map_err(|error| format!("inspect recovery control socket ownership: {error}"))?;
    if !socket_metadata.file_type().is_socket()
        || socket_metadata.gid() != parent_metadata.gid()
        || socket_metadata.permissions().mode() & 0o777 != 0o660
    {
        return Err(format!(
            "recovery control socket must inherit runtime service group and mode 0660 (parent_gid={}, socket_gid={}, mode={:o})",
            parent_metadata.gid(),
            socket_metadata.gid(),
            socket_metadata.permissions().mode() & 0o777
        ));
    }
    let _cleanup = RecoverySocketCleanup {
        path: socket_path.clone(),
        device: socket_metadata.dev(),
        inode: socket_metadata.ino(),
    };
    sync_directory(parent)?;
    tracing::info!(
        "Recovery generation control listening on {} (local AF_UNIX, mode 0660)",
        socket_path.display()
    );
    if let Some(ready) = ready {
        ready
            .send(())
            .map_err(|_| "recovery control readiness receiver disappeared".to_string())?;
    }

    loop {
        let (mut stream, _) = listener
            .accept()
            .await
            .map_err(|error| format!("accept recovery control connection: {error}"))?;
        let mut request_id = "invalid".to_string();
        let response = match read_bounded_line(
            &mut stream,
            MAX_CONTROL_LINE_BYTES,
            tokio::time::Instant::now() + Duration::from_secs(2),
            "recovery control request",
        )
        .await
        {
            Err(error) => Err(error),
            Ok(line) => match serde_json::from_slice::<RecoveryControlRequest>(&line) {
                Err(error) => Err(format!("invalid recovery control JSON: {error}")),
                Ok(request) => {
                    request_id = request.request_id.clone();
                    if request.schema_version != CONTROL_SCHEMA_VERSION
                        || request.command != CONTROL_COMMAND
                        || !identifier_is_valid(&request.request_id)
                    {
                        Err("unsupported recovery control request".to_string())
                    } else {
                        coordinator.create_generation(request.request_id).await
                    }
                }
            },
        };
        let encoded = match response {
            Ok(result) => serde_json::to_vec(&result)
                .map_err(|error| format!("encode recovery control response: {error}"))?,
            Err(error) => serde_json::to_vec(&RecoveryControlError {
                schema_version: CONTROL_SCHEMA_VERSION,
                complete: false,
                request_id,
                error: safe_control_error(&error),
            })
            .map_err(|encode_error| {
                format!("encode recovery control error response: {encode_error}")
            })?,
        };
        if let Err(error) = stream.write_all(&encoded).await {
            tracing::warn!(
                "Recovery control caller disconnected before its response: {}",
                error
            );
            continue;
        }
        if let Err(error) = stream.write_all(b"\n").await {
            tracing::warn!(
                "Recovery control caller disconnected before its delimiter: {}",
                error
            );
            continue;
        }
        if let Err(error) = stream.shutdown().await {
            tracing::warn!("Recovery control response shutdown failed: {}", error);
        }
    }
}

#[cfg(not(unix))]
pub(crate) async fn run_recovery_control_server(
    _coordinator: RecoveryCoordinator,
    ready: Option<oneshot::Sender<()>>,
) -> Result<(), String> {
    if let Some(ready) = ready {
        let _ = ready.send(());
    }
    std::future::pending::<()>().await;
    Ok(())
}

#[cfg(unix)]
pub(crate) async fn run_recovery_generation_cli(arguments: &[String]) -> Result<(), String> {
    use std::os::unix::fs::FileTypeExt;
    use tokio::io::AsyncWriteExt;
    use tokio::net::UnixStream;

    let mut socket_path = recovery_control_socket_path();
    let mut timeout_ms = MAX_BARRIER_TIMEOUT_MS;
    let mut index = 1_usize;
    while index < arguments.len() {
        match arguments[index].as_str() {
            "--create-recovery-generation" => index += 1,
            "--socket" => {
                socket_path = arguments
                    .get(index + 1)
                    .map(PathBuf::from)
                    .ok_or_else(|| "--socket requires an absolute path".to_string())?;
                index += 2;
            }
            "--timeout-ms" => {
                timeout_ms = arguments
                    .get(index + 1)
                    .and_then(|value| value.parse::<u64>().ok())
                    .filter(|value| {
                        (MIN_BARRIER_TIMEOUT_MS..=MAX_BARRIER_TIMEOUT_MS).contains(value)
                    })
                    .ok_or_else(|| {
                        format!(
                            "--timeout-ms must be {MIN_BARRIER_TIMEOUT_MS}..={MAX_BARRIER_TIMEOUT_MS}"
                        )
                    })?;
                index += 2;
            }
            unknown => {
                return Err(format!(
                    "unknown recovery generation CLI argument {unknown}"
                ));
            }
        }
    }
    if !socket_path.is_absolute() {
        return Err("recovery control socket path must be absolute".to_string());
    }
    let metadata = std::fs::symlink_metadata(&socket_path)
        .map_err(|error| format!("inspect recovery control socket: {error}"))?;
    if metadata.file_type().is_symlink() || !metadata.file_type().is_socket() {
        return Err("recovery control path is not a local Unix socket".to_string());
    }
    let deadline = tokio::time::Instant::now() + Duration::from_millis(timeout_ms);
    let mut stream = tokio::time::timeout_at(deadline, UnixStream::connect(&socket_path))
        .await
        .map_err(|_| "connect to recovery control socket timed out".to_string())?
        .map_err(|error| format!("connect to recovery control socket: {error}"))?;
    let request_id = format!(
        "backup-{}-{:016x}",
        current_time_ms().max(1),
        rand::random::<u64>()
    );
    let request = RecoveryControlRequest {
        schema_version: CONTROL_SCHEMA_VERSION,
        command: CONTROL_COMMAND.to_string(),
        request_id,
    };
    let mut encoded = serde_json::to_vec(&request)
        .map_err(|error| format!("encode recovery control request: {error}"))?;
    encoded.push(b'\n');
    tokio::time::timeout_at(deadline, stream.write_all(&encoded))
        .await
        .map_err(|_| "write recovery control request timed out".to_string())?
        .map_err(|error| format!("write recovery control request: {error}"))?;
    let response = read_bounded_line(
        &mut stream,
        64 * 1024,
        deadline,
        "recovery control response",
    )
    .await?;
    let value: serde_json::Value = serde_json::from_slice(&response)
        .map_err(|error| format!("decode recovery control response: {error}"))?;
    if value.get("complete").and_then(serde_json::Value::as_bool) != Some(true) {
        let error = value
            .get("error")
            .and_then(serde_json::Value::as_str)
            .unwrap_or("recovery generation request failed");
        return Err(safe_control_error(error));
    }
    let result: RecoveryGenerationResult = serde_json::from_value(value)
        .map_err(|error| format!("invalid successful recovery response: {error}"))?;
    let verified = verify_recovery_generation(Path::new(&result.manifest_path))?;
    if verified.generation_id != result.generation_id
        || verified.manifest_sha256 != result.manifest_sha256
        || verified.manifest_size_bytes != result.manifest_size_bytes
    {
        return Err("recovery response does not match the immutable manifest".to_string());
    }
    println!(
        "{}",
        serde_json::to_string(&result)
            .map_err(|error| format!("encode verified recovery response: {error}"))?
    );
    Ok(())
}

#[cfg(not(unix))]
pub(crate) async fn run_recovery_generation_cli(_arguments: &[String]) -> Result<(), String> {
    Err("recovery generation control requires a Unix host".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicBool, Ordering};

    fn unique_directory(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "bongus-recovery-{label}-{}-{:016x}",
            std::process::id(),
            rand::random::<u64>()
        ))
    }

    fn write_fixture(path: &Path, bytes: &[u8]) {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent).expect("create recovery fixture directory");
        }
        std::fs::write(path, bytes).expect("write recovery fixture");
    }

    fn fixture_inputs(root: &Path, request_id: &str) -> RecoveryInputs {
        let source = root.join("live");
        let execution_path = source.join("execution_state.jsonl");
        let intent_path = source.join("execution_intents.jsonl");
        let telemetry_path = source.join("execution_telemetry.jsonl");
        let spot_path = source.join("private_stream_cursors/spot.jsonl");
        let futures_path = source.join("private_stream_cursors/futures.jsonl");
        write_fixture(
            &execution_path,
            format!(
                "{{\"schema_version\":7,\"terminal_sequence_watermark\":17,\"continuous_risk_reason\":\"recovery_generation_barrier_active:{request_id}\"}}\n"
            )
            .as_bytes(),
        );
        write_fixture(
            &intent_path,
            b"{\"producer_id\":\"python-live\",\"sequence\":3}\n{\"producer_id\":\"python-live\",\"sequence\":5}\n",
        );
        write_fixture(&telemetry_path, b"");
        write_fixture(&spot_path, b"{\"stream\":\"spot\",\"through_ms\":101}\n");
        write_fixture(
            &futures_path,
            b"{\"stream\":\"futures\",\"through_ms\":202}\n",
        );
        let consumer_id = "python-live-trader";
        let cursor = serde_json::json!({
            "schema_version": 1,
            "generation": 0,
            "high_water_sequence": 0,
            "consumer_id": consumer_id,
            "checksum": telemetry_cursor_checksum(0, 0, consumer_id),
        });
        RecoveryInputs {
            order: OrderRecoverySnapshot {
                barrier_request_id: request_id.to_string(),
                execution_state_path: execution_path,
                intent_journal_path: intent_path,
                terminal_sequence_watermark: 17,
                intent_producer_high_watermarks: BTreeMap::from([("python-live".to_string(), 5)]),
            },
            telemetry: TelemetryRecoverySnapshot {
                journal_path: telemetry_path,
                active_cursor_path: None,
                active_cursor_bytes: serde_json::to_vec(&cursor).expect("encode cursor"),
                active_cursor_suffix: ".a".to_string(),
                published_high_water_sequence: 0,
                acknowledged_high_water_sequence: 0,
                cursor_generation: 0,
            },
            spot: PrivateCursorRecoverySnapshot {
                stream_kind: UserDataStreamKind::Spot,
                cursor_path: spot_path,
                through_ms: Some(101),
            },
            futures: PrivateCursorRecoverySnapshot {
                stream_kind: UserDataStreamKind::Futures,
                cursor_path: futures_path,
                through_ms: Some(202),
            },
        }
    }

    fn cleanup_fixture(path: &Path) {
        if path.exists() {
            make_tree_writable(path).expect("make recovery fixture removable");
            std::fs::remove_dir_all(path).expect("remove recovery fixture");
        }
    }

    #[allow(clippy::permissions_set_readonly_false)] // Windows has no Unix mode bits.
    fn make_file_writable(path: &Path) {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o640))
                .expect("make recovery member writable");
        }
        #[cfg(not(unix))]
        {
            let mut permissions = std::fs::metadata(path).unwrap().permissions();
            permissions.set_readonly(false);
            std::fs::set_permissions(path, permissions).unwrap();
        }
    }

    fn restore_file_immutable(path: &Path) {
        set_file_immutable_permissions(path).expect("restore recovery member permissions");
    }

    #[test]
    fn immutable_generation_verifies_and_detects_member_tampering() {
        let root = unique_directory("verify-tamper");
        let inputs = fixture_inputs(&root, "request-verify");
        let generations = root.join("generations");
        let result = publish_recovery_generation(
            &generations,
            "1000-aaaaaaaaaaaaaaaa",
            &inputs,
            Instant::now() + Duration::from_secs(5),
            PublishFailpoint::None,
        )
        .expect("publish immutable generation");
        let verified = verify_recovery_generation(Path::new(&result.manifest_path))
            .expect("verify immutable generation");
        assert_eq!(verified.generation_id, result.generation_id);
        assert_eq!(verified.manifest_sha256, result.manifest_sha256);

        let manifest: RecoveryGenerationManifest =
            serde_json::from_slice(&std::fs::read(&result.manifest_path).expect("read manifest"))
                .expect("parse manifest");
        let state_member = Path::new(&result.manifest_path)
            .parent()
            .unwrap()
            .join(&manifest.members["execution_state"].filename);
        make_file_writable(&state_member);
        let mut tampered = std::fs::read(&state_member).unwrap();
        tampered[0] ^= 1;
        std::fs::write(&state_member, tampered).unwrap();
        restore_file_immutable(&state_member);
        assert!(
            verify_recovery_generation(Path::new(&result.manifest_path))
                .unwrap_err()
                .contains("hash mismatch")
        );
        cleanup_fixture(&root);
    }

    #[test]
    fn engine_owned_retention_keeps_only_the_protected_verified_generation() {
        let root = unique_directory("generation-retention");
        let inputs = fixture_inputs(&root, "request-retention");
        let generations = root.join("generations");
        for generation_id in [
            "3000-aaaaaaaaaaaaaaaa",
            "3001-bbbbbbbbbbbbbbbb",
            "3002-cccccccccccccccc",
        ] {
            publish_recovery_generation(
                &generations,
                generation_id,
                &inputs,
                Instant::now() + Duration::from_secs(5),
                PublishFailpoint::None,
            )
            .expect("publish retention fixture generation");
        }

        prune_published_generations(&generations, "3002-cccccccccccccccc")
            .expect("prune superseded recovery generations");

        let remaining = std::fs::read_dir(&generations)
            .unwrap()
            .map(|entry| entry.unwrap().file_name().to_string_lossy().to_string())
            .collect::<Vec<_>>();
        assert_eq!(remaining, vec!["generation-3002-cccccccccccccccc"]);
        verify_recovery_generation(
            &generations.join("generation-3002-cccccccccccccccc/manifest.json"),
        )
        .expect("retained generation remains valid");
        cleanup_fixture(&root);
    }

    #[test]
    fn crash_staging_is_never_published_and_restart_cleans_it() {
        let root = unique_directory("crash-restart");
        let inputs = fixture_inputs(&root, "request-crash");
        let generations = root.join("generations");
        let first_id = "2000-bbbbbbbbbbbbbbbb";
        assert!(
            publish_recovery_generation(
                &generations,
                first_id,
                &inputs,
                Instant::now() + Duration::from_secs(5),
                PublishFailpoint::AfterFirstMember,
            )
            .unwrap_err()
            .contains("simulated crash")
        );
        assert!(!generations.join(format!("generation-{first_id}")).exists());
        assert!(
            generations
                .join(format!(".generation-{first_id}.staging"))
                .exists()
        );

        let manifest_crash_id = "2001-cbcbcbcbcbcbcbcb";
        assert!(
            publish_recovery_generation(
                &generations,
                manifest_crash_id,
                &inputs,
                Instant::now() + Duration::from_secs(5),
                PublishFailpoint::AfterManifestSync,
            )
            .unwrap_err()
            .contains("simulated crash")
        );
        assert!(
            !generations
                .join(format!("generation-{manifest_crash_id}"))
                .exists()
        );
        assert!(
            generations
                .join(format!(".generation-{manifest_crash_id}.staging"))
                .exists()
        );

        let restarted = publish_recovery_generation(
            &generations,
            "2002-cccccccccccccccc",
            &inputs,
            Instant::now() + Duration::from_secs(5),
            PublishFailpoint::None,
        )
        .expect("restart publishes a fresh generation");
        assert!(
            !generations
                .join(format!(".generation-{manifest_crash_id}.staging"))
                .exists()
        );
        verify_recovery_generation(Path::new(&restarted.manifest_path))
            .expect("restarted generation verifies");
        cleanup_fixture(&root);
    }

    #[test]
    fn crash_after_atomic_rename_leaves_a_complete_verifiable_generation() {
        let root = unique_directory("post-rename");
        let inputs = fixture_inputs(&root, "request-renamed");
        let generations = root.join("generations");
        let generation_id = "3000-dddddddddddddddd";
        assert!(
            publish_recovery_generation(
                &generations,
                generation_id,
                &inputs,
                Instant::now() + Duration::from_secs(5),
                PublishFailpoint::AfterRename,
            )
            .unwrap_err()
            .contains("simulated crash")
        );
        let manifest = generations
            .join(format!("generation-{generation_id}"))
            .join("manifest.json");
        verify_recovery_generation(&manifest).expect("renamed generation is a valid commit");
        cleanup_fixture(&root);
    }

    #[test]
    fn expired_deadline_never_publishes_a_generation() {
        let root = unique_directory("deadline");
        let inputs = fixture_inputs(&root, "request-timeout");
        let generations = root.join("generations");
        let generation_id = "4000-eeeeeeeeeeeeeeee";
        assert!(
            publish_recovery_generation(
                &generations,
                generation_id,
                &inputs,
                Instant::now(),
                PublishFailpoint::None,
            )
            .unwrap_err()
            .contains("timed out")
        );
        assert!(
            !generations
                .join(format!("generation-{generation_id}"))
                .exists()
        );
        cleanup_fixture(&root);
    }

    #[tokio::test]
    async fn private_cursor_writer_is_quiesced_by_the_recovery_lock() {
        let root = unique_directory("cursor-lock");
        let path = root.join("spot.jsonl");
        let handle = PrivateCursorRecoveryHandle::for_test(UserDataStreamKind::Spot, path);
        let locked = handle.lock_for_recovery().await;
        let writer_handle = handle.clone();
        let mut writer =
            tokio::spawn(async move { writer_handle.write_cursor_for_recovery_test(900).await });
        assert!(
            tokio::time::timeout(Duration::from_millis(30), &mut writer)
                .await
                .is_err(),
            "cursor writer must remain blocked during the recovery cut"
        );
        drop(locked);
        writer
            .await
            .expect("cursor writer task")
            .expect("cursor writer resumes");
        let snapshot = handle
            .lock_for_recovery()
            .await
            .prepare_recovery_snapshot()
            .expect("read cursor after resume");
        assert_eq!(snapshot.through_ms, Some(900));
        cleanup_fixture(&root);
    }

    #[tokio::test]
    async fn coordinator_timeout_drops_the_order_barrier_and_publishes_nothing() {
        let root = unique_directory("coordinator-timeout");
        let (order_tx, mut order_rx) = mpsc::channel(2);
        let (telemetry_tx, _telemetry_rx) = mpsc::channel(1);
        let spot = PrivateCursorRecoveryHandle::for_test(
            UserDataStreamKind::Spot,
            root.join("spot.jsonl"),
        );
        let futures = PrivateCursorRecoveryHandle::for_test(
            UserDataStreamKind::Futures,
            root.join("futures.jsonl"),
        );
        let barrier_dropped = Arc::new(AtomicBool::new(false));
        let dropped_for_actor = barrier_dropped.clone();
        let actor = tokio::spawn(async move {
            if let Some(EngineEvent::RecoveryBarrier { release, .. }) = order_rx.recv().await {
                assert!(release.await.is_err());
                dropped_for_actor.store(true, Ordering::SeqCst);
            } else {
                panic!("expected recovery barrier");
            }
        });
        let coordinator = RecoveryCoordinator::for_test(
            order_tx,
            telemetry_tx,
            spot,
            futures,
            root.join("generations"),
            Duration::from_millis(30),
        );
        assert!(
            coordinator
                .create_generation("request-never-replied".to_string())
                .await
                .unwrap_err()
                .contains("timed out")
        );
        actor.await.expect("fake actor exits");
        assert!(barrier_dropped.load(Ordering::SeqCst));
        assert!(
            std::fs::read_dir(root.join("generations"))
                .expect("generation root")
                .next()
                .is_none()
        );
        cleanup_fixture(&root);
    }

    #[tokio::test]
    async fn control_protocol_rejects_oversize_or_trailing_frames() {
        let oversized = vec![b'x'; 65];
        let mut oversized_slice = oversized.as_slice();
        assert!(
            read_bounded_line(
                &mut oversized_slice,
                64,
                tokio::time::Instant::now() + Duration::from_secs(1),
                "test request",
            )
            .await
            .unwrap_err()
            .contains("byte cap")
        );
        let mut trailing = b"{}\nextra".as_slice();
        assert!(
            read_bounded_line(
                &mut trailing,
                64,
                tokio::time::Instant::now() + Duration::from_secs(1),
                "test request",
            )
            .await
            .unwrap_err()
            .contains("trailing bytes")
        );
    }

    #[tokio::test]
    async fn coordinator_publishes_then_resumes_telemetry_before_order_actor() {
        let root = unique_directory("coordinator-success");
        let request_id = "request-coordinator";
        let inputs = fixture_inputs(&root, request_id);
        let (order_tx, mut order_rx) = mpsc::channel(2);
        let (telemetry_tx, mut telemetry_rx) = mpsc::channel(1);
        let order_snapshot = inputs.order.clone();
        let telemetry_snapshot = inputs.telemetry.clone();
        let order_resumed = Arc::new(AtomicBool::new(false));
        let telemetry_resumed = Arc::new(AtomicBool::new(false));
        let order_resumed_actor = order_resumed.clone();
        let telemetry_seen_by_order = telemetry_resumed.clone();
        let order_actor = tokio::spawn(async move {
            let Some(EngineEvent::RecoveryBarrier {
                reply,
                release,
                resumed,
                ..
            }) = order_rx.recv().await
            else {
                panic!("expected order recovery barrier");
            };
            reply.send(Ok(order_snapshot)).unwrap();
            match release.await.unwrap() {
                RecoveryBarrierRelease::Published { generation_id } => {
                    assert!(identifier_is_valid(&generation_id));
                    assert!(telemetry_seen_by_order.load(Ordering::SeqCst));
                }
                RecoveryBarrierRelease::Failed { reason } => {
                    panic!("unexpected recovery failure: {reason}")
                }
            }
            order_resumed_actor.store(true, Ordering::SeqCst);
            resumed.send(Ok(())).unwrap();
        });
        let telemetry_resumed_actor = telemetry_resumed.clone();
        let telemetry_actor = tokio::spawn(async move {
            let Some(TelemetryRelayControl::RecoveryBarrier {
                reply,
                release,
                resumed,
                ..
            }) = telemetry_rx.recv().await
            else {
                panic!("expected telemetry recovery barrier");
            };
            reply.send(Ok(telemetry_snapshot)).unwrap();
            release.await.unwrap();
            telemetry_resumed_actor.store(true, Ordering::SeqCst);
            resumed.send(()).unwrap();
        });

        let spot = PrivateCursorRecoveryHandle::for_test(
            UserDataStreamKind::Spot,
            inputs.spot.cursor_path.clone(),
        );
        let futures = PrivateCursorRecoveryHandle::for_test(
            UserDataStreamKind::Futures,
            inputs.futures.cursor_path.clone(),
        );
        let coordinator = RecoveryCoordinator::for_test(
            order_tx,
            telemetry_tx,
            spot,
            futures,
            root.join("generations"),
            Duration::from_secs(2),
        );
        let result = coordinator
            .create_generation(request_id.to_string())
            .await
            .expect("coordinator publishes generation");
        assert!(result.complete);
        assert!(order_resumed.load(Ordering::SeqCst));
        assert!(telemetry_resumed.load(Ordering::SeqCst));
        verify_recovery_generation(Path::new(&result.manifest_path))
            .expect("coordinator generation verifies after restart");
        order_actor.await.unwrap();
        telemetry_actor.await.unwrap();
        cleanup_fixture(&root);
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn unix_control_socket_is_group_accessible_and_returns_verified_manifest() {
        use std::os::unix::fs::{MetadataExt, PermissionsExt};
        use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader as AsyncBufReader};
        use tokio::net::UnixStream;

        let root = unique_directory("unix-control");
        let runtime = root.join("runtime");
        std::fs::create_dir_all(&runtime).unwrap();
        std::fs::set_permissions(&runtime, std::fs::Permissions::from_mode(0o750)).unwrap();
        let socket_path = runtime.join("recovery-control.sock");
        let source = root.join("live");
        std::fs::create_dir_all(&source).unwrap();
        let execution_path = source.join("execution_state.jsonl");
        let intent_path = source.join("execution_intents.jsonl");
        let telemetry_path = source.join("execution_telemetry.jsonl");
        write_fixture(&intent_path, b"");
        write_fixture(&telemetry_path, b"");
        let spot = PrivateCursorRecoveryHandle::for_test(
            UserDataStreamKind::Spot,
            source.join("private_stream_cursors/spot.jsonl"),
        );
        let futures = PrivateCursorRecoveryHandle::for_test(
            UserDataStreamKind::Futures,
            source.join("private_stream_cursors/futures.jsonl"),
        );
        let (order_tx, mut order_rx) = mpsc::channel(2);
        let (telemetry_tx, mut telemetry_rx) = mpsc::channel(1);
        let order_actor = tokio::spawn(async move {
            let Some(EngineEvent::RecoveryBarrier {
                request_id,
                reply,
                release,
                resumed,
            }) = order_rx.recv().await
            else {
                panic!("expected control-path order barrier");
            };
            write_fixture(
                &execution_path,
                format!(
                    "{{\"terminal_sequence_watermark\":9,\"continuous_risk_reason\":\"recovery_generation_barrier_active:{request_id}\"}}\n"
                )
                .as_bytes(),
            );
            reply
                .send(Ok(OrderRecoverySnapshot {
                    barrier_request_id: request_id,
                    execution_state_path: execution_path,
                    intent_journal_path: intent_path,
                    terminal_sequence_watermark: 9,
                    intent_producer_high_watermarks: BTreeMap::new(),
                }))
                .unwrap();
            assert!(matches!(
                release.await.unwrap(),
                RecoveryBarrierRelease::Published { .. }
            ));
            resumed.send(Ok(())).unwrap();
        });
        let telemetry_actor = tokio::spawn(async move {
            let Some(TelemetryRelayControl::RecoveryBarrier {
                reply,
                release,
                resumed,
                ..
            }) = telemetry_rx.recv().await
            else {
                panic!("expected control-path telemetry barrier");
            };
            let consumer_id = "python-live-trader";
            let cursor = serde_json::json!({
                "schema_version": 1,
                "generation": 0,
                "high_water_sequence": 0,
                "consumer_id": consumer_id,
                "checksum": telemetry_cursor_checksum(0, 0, consumer_id),
            });
            reply
                .send(Ok(TelemetryRecoverySnapshot {
                    journal_path: telemetry_path,
                    active_cursor_path: None,
                    active_cursor_bytes: serde_json::to_vec(&cursor).unwrap(),
                    active_cursor_suffix: ".a".to_string(),
                    published_high_water_sequence: 0,
                    acknowledged_high_water_sequence: 0,
                    cursor_generation: 0,
                }))
                .unwrap();
            release.await.unwrap();
            resumed.send(()).unwrap();
        });
        let coordinator = RecoveryCoordinator::for_test(
            order_tx,
            telemetry_tx,
            spot,
            futures,
            runtime.join("recovery_generations"),
            Duration::from_secs(2),
        );
        let (ready_tx, ready_rx) = oneshot::channel();
        let server_socket = socket_path.clone();
        let server = tokio::spawn(async move {
            run_recovery_control_server_at(coordinator, Some(ready_tx), server_socket).await
        });
        ready_rx.await.unwrap();
        let parent_metadata = std::fs::metadata(&runtime).unwrap();
        let socket_metadata = std::fs::metadata(&socket_path).unwrap();
        assert_eq!(socket_metadata.permissions().mode() & 0o777, 0o660);
        assert_eq!(socket_metadata.gid(), parent_metadata.gid());

        let mut client = UnixStream::connect(&socket_path).await.unwrap();
        client
            .write_all(
                b"{\"schema_version\":1,\"command\":\"create_recovery_generation\",\"request_id\":\"service-backup-1\"}\n",
            )
            .await
            .unwrap();
        let mut reader = AsyncBufReader::new(client);
        let mut response = Vec::new();
        reader.read_until(b'\n', &mut response).await.unwrap();
        let result: RecoveryGenerationResult = serde_json::from_slice(&response).unwrap();
        assert!(result.complete);
        verify_recovery_generation(Path::new(&result.manifest_path)).unwrap();
        order_actor.await.unwrap();
        telemetry_actor.await.unwrap();
        server.abort();
        let _ = server.await;
        cleanup_fixture(&root);
    }

    #[test]
    fn telemetry_snapshot_materializes_a_restorable_zero_ack_cursor() {
        let root = unique_directory("telemetry-zero");
        let journal_path = root.join("telemetry.jsonl");
        let cursor_path = root.join("telemetry.cursor");
        let journal =
            crate::telemetry::TelemetryJournal::load_for_recovery_test(&journal_path, &cursor_path)
                .expect("load empty telemetry journal");
        let snapshot = journal
            .prepare_recovery_snapshot()
            .expect("prepare empty telemetry snapshot");
        let cursor: RecoveryTelemetryCursor =
            serde_json::from_slice(&snapshot.active_cursor_bytes).expect("decode zero cursor");
        assert_eq!(cursor.generation, 0);
        assert_eq!(cursor.high_water_sequence, 0);
        assert_eq!(snapshot.active_cursor_suffix, ".a");
        cleanup_fixture(&root);
    }
}
