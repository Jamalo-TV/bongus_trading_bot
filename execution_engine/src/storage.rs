//! Durable Rust-side storage emergency control.
//!
//! The checkpoint is intentionally tiny and independent of the larger intent
//! and execution journals.  A generation can clear the latch only after an
//! atomic, checksummed, file-and-directory-fsynced install succeeds.

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs::OpenOptions;
use std::io::Write;
use std::path::{Path, PathBuf};

const STORAGE_CONTROL_SCHEMA_VERSION: u16 = 1;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub(crate) struct StorageControlRecord {
    pub(crate) schema_version: u16,
    pub(crate) generation: u64,
    pub(crate) emergency_latched: bool,
    pub(crate) checksum: String,
}

impl StorageControlRecord {
    pub(crate) fn new(generation: u64, emergency_latched: bool) -> Self {
        let checksum = Self::checksum_for(generation, emergency_latched);
        Self {
            schema_version: STORAGE_CONTROL_SCHEMA_VERSION,
            generation,
            emergency_latched,
            checksum,
        }
    }

    fn checksum_for(generation: u64, emergency_latched: bool) -> String {
        let mut digest = Sha256::new();
        digest.update(b"bongus-storage-control-v1\n");
        digest.update(generation.to_string().as_bytes());
        digest.update(b"\n");
        digest.update(if emergency_latched { b"1" } else { b"0" });
        hex::encode(digest.finalize())
    }

    pub(crate) fn validate(&self) -> Result<(), String> {
        if self.schema_version != STORAGE_CONTROL_SCHEMA_VERSION
            || self.generation == 0
            || self.checksum != Self::checksum_for(self.generation, self.emergency_latched)
        {
            return Err("invalid storage-control checkpoint".to_string());
        }
        Ok(())
    }

    pub(crate) fn load(path: &Path) -> Result<Option<Self>, String> {
        recover_previous(path)?;
        if !path.exists() {
            return Ok(None);
        }
        let encoded =
            std::fs::read(path).map_err(|err| format!("read storage-control checkpoint: {err}"))?;
        let record: Self = serde_json::from_slice(&encoded)
            .map_err(|err| format!("decode storage-control checkpoint: {err}"))?;
        record.validate()?;
        Ok(Some(record))
    }

    pub(crate) fn persist(&self, path: &Path) -> Result<(), String> {
        self.validate()?;
        let parent = path
            .parent()
            .filter(|candidate| !candidate.as_os_str().is_empty())
            .unwrap_or_else(|| Path::new("."));
        std::fs::create_dir_all(parent)
            .map_err(|err| format!("create storage-control directory: {err}"))?;
        let next = path_with_suffix(path, ".next");
        let previous = path_with_suffix(path, ".previous");
        {
            let mut file = OpenOptions::new()
                .create(true)
                .truncate(true)
                .write(true)
                .open(&next)
                .map_err(|err| format!("create storage-control checkpoint: {err}"))?;
            serde_json::to_writer(&mut file, self)
                .map_err(|err| format!("encode storage-control checkpoint: {err}"))?;
            file.write_all(b"\n")
                .map_err(|err| format!("write storage-control checkpoint: {err}"))?;
            file.sync_all()
                .map_err(|err| format!("sync storage-control checkpoint: {err}"))?;
        }
        if previous.exists() {
            std::fs::remove_file(&previous)
                .map_err(|err| format!("remove stale storage-control checkpoint: {err}"))?;
        }
        if path.exists() {
            rename_durable(path, &previous)
                .map_err(|err| format!("rotate storage-control checkpoint: {err}"))?;
        }
        if let Err(err) = rename_durable(&next, path) {
            if !path.exists() && previous.exists() {
                let _ = rename_durable(&previous, path);
            }
            return Err(format!("install storage-control checkpoint: {err}"));
        }
        sync_directory(parent)?;
        if previous.exists() {
            std::fs::remove_file(&previous)
                .map_err(|err| format!("remove prior storage-control checkpoint: {err}"))?;
            sync_directory(parent)?;
        }
        Ok(())
    }
}

fn path_with_suffix(path: &Path, suffix: &str) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(suffix);
    PathBuf::from(value)
}

fn recover_previous(path: &Path) -> Result<(), String> {
    if path.exists() {
        return Ok(());
    }
    let previous = path_with_suffix(path, ".previous");
    if previous.exists() {
        rename_durable(&previous, path)
            .map_err(|err| format!("recover prior storage-control checkpoint: {err}"))?;
        let parent = path.parent().unwrap_or_else(|| Path::new("."));
        sync_directory(parent)?;
    }
    Ok(())
}

#[cfg(windows)]
fn rename_durable(source: &Path, destination: &Path) -> std::io::Result<()> {
    use std::os::windows::ffi::OsStrExt;

    const MOVEFILE_WRITE_THROUGH: u32 = 0x0000_0008;
    #[link(name = "Kernel32")]
    unsafe extern "system" {
        fn MoveFileExW(
            existing_file_name: *const u16,
            new_file_name: *const u16,
            flags: u32,
        ) -> i32;
    }

    let source: Vec<u16> = source.as_os_str().encode_wide().chain(Some(0)).collect();
    let destination: Vec<u16> = destination
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect();
    // SAFETY: both pointers reference NUL-terminated UTF-16 buffers that live
    // for the duration of the call. The destination is absent by construction.
    if unsafe {
        MoveFileExW(
            source.as_ptr(),
            destination.as_ptr(),
            MOVEFILE_WRITE_THROUGH,
        )
    } == 0
    {
        return Err(std::io::Error::last_os_error());
    }
    Ok(())
}

#[cfg(not(windows))]
fn rename_durable(source: &Path, destination: &Path) -> std::io::Result<()> {
    std::fs::rename(source, destination)
}

#[cfg(windows)]
fn sync_directory(path: &Path) -> Result<(), String> {
    use std::os::windows::fs::OpenOptionsExt;

    const FILE_FLAG_BACKUP_SEMANTICS: u32 = 0x0200_0000;
    let directory = OpenOptions::new()
        .read(true)
        .custom_flags(FILE_FLAG_BACKUP_SEMANTICS)
        .open(path)
        .map_err(|err| format!("open storage-control directory for sync: {err}"))?;
    directory
        .sync_all()
        .or_else(|err| {
            // Windows does not expose a non-privileged directory-fsync
            // primitive: FlushFileBuffers on a directory handle returns
            // ERROR_ACCESS_DENIED. The metadata operation itself used
            // MOVEFILE_WRITE_THROUGH above, which is the supported durability
            // barrier for the rename. Preserve all other failures.
            if err.raw_os_error() == Some(5) {
                Ok(())
            } else {
                Err(err)
            }
        })
        .map_err(|err| format!("sync storage-control directory: {err}"))
}

#[cfg(not(windows))]
fn sync_directory(path: &Path) -> Result<(), String> {
    std::fs::File::open(path)
        .and_then(|directory| directory.sync_all())
        .map_err(|err| format!("sync storage-control directory: {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unique_path(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "bongus-{label}-{}-{}.json",
            std::process::id(),
            rand::random::<u64>()
        ))
    }

    #[test]
    fn checkpoint_round_trip_rejects_torn_or_changed_records() {
        let path = unique_path("storage-control-module");
        let record = StorageControlRecord::new(7, true);
        record.persist(&path).unwrap();
        assert_eq!(StorageControlRecord::load(&path).unwrap(), Some(record));

        let mut payload: serde_json::Value =
            serde_json::from_slice(&std::fs::read(&path).unwrap()).unwrap();
        payload["emergency_latched"] = serde_json::Value::Bool(false);
        std::fs::write(&path, serde_json::to_vec(&payload).unwrap()).unwrap();
        assert!(StorageControlRecord::load(&path).is_err());
        let _ = std::fs::remove_file(path);
    }
}
