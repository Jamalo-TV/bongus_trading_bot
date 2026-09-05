#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: bash Install-BongusRelease.sh [options]

Options:
  --python PATH                    Python 3.11 executable (default python3.11)
  --allow-development-package     Permit a non-production package for paper/testnet only
  --trusted-key-sha256 SHA256      Out-of-band Linux release public-key fingerprint
  --install-systemd               Install and enable (but do not start) a system service
  --service-user USER             Existing unprivileged service user (default bongus)
  --service-group GROUP           Existing service group (default same as user)
  --backup-user USER              Existing local backup/health user (default bongus-backup)
  --backup-group GROUP            Existing backup group (default same as backup user)
  --offsite-user USER             Existing Restic user (default bongus-offsite)
  --offsite-group GROUP           Existing offsite-secret group (default same as offsite user)
  --maintenance-user USER         Existing delete-capable Restic user (default bongus-maintenance)
  --maintenance-group GROUP       Existing maintenance-secret group (default same as maintenance user)
  --data-root PATH                Runtime DB/state directory (default /var/lib/bongus)
  --service-name NAME             Unit basename (default bongus)
EOF
}

RELEASE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
ALLOW_DEVELOPMENT=0
INSTALL_SYSTEMD=0
TRUSTED_KEY_SHA256="${BONGUS_RELEASE_SIGNING_KEY_SHA256:-}"
SERVICE_USER="bongus"
SERVICE_GROUP=""
BACKUP_USER="bongus-backup"
BACKUP_GROUP=""
OFFSITE_USER="bongus-offsite"
OFFSITE_GROUP=""
MAINTENANCE_USER="bongus-maintenance"
MAINTENANCE_GROUP=""
DATA_ROOT="/var/lib/bongus"
SERVICE_NAME="bongus"
BACKUP_OPERATION_MAX_BYTES=8000000000

while (($#)); do
    case "$1" in
        --python) PYTHON_BIN="$2"; shift 2 ;;
        --allow-development-package) ALLOW_DEVELOPMENT=1; shift ;;
        --trusted-key-sha256) TRUSTED_KEY_SHA256="$2"; shift 2 ;;
        --install-systemd) INSTALL_SYSTEMD=1; shift ;;
        --service-user) SERVICE_USER="$2"; shift 2 ;;
        --service-group) SERVICE_GROUP="$2"; shift 2 ;;
        --backup-user) BACKUP_USER="$2"; shift 2 ;;
        --backup-group) BACKUP_GROUP="$2"; shift 2 ;;
        --offsite-user) OFFSITE_USER="$2"; shift 2 ;;
        --offsite-group) OFFSITE_GROUP="$2"; shift 2 ;;
        --maintenance-user) MAINTENANCE_USER="$2"; shift 2 ;;
        --maintenance-group) MAINTENANCE_GROUP="$2"; shift 2 ;;
        --data-root) DATA_ROOT="$2"; shift 2 ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || { echo "This installer is for Linux." >&2; exit 2; }
command -v "$PYTHON_BIN" >/dev/null || { echo "Python executable not found: $PYTHON_BIN" >&2; exit 2; }
command -v flock >/dev/null || { echo "util-linux flock is required." >&2; exit 2; }
MANIFEST_TOOL="$RELEASE_ROOT/scripts/release_manifest.py"
[[ -f "$MANIFEST_TOOL" && ! -L "$MANIFEST_TOOL" ]] || { echo "Release verifier is missing or linked." >&2; exit 2; }

TRUSTED_KEY_SHA256="${TRUSTED_KEY_SHA256,,}"
if ((ALLOW_DEVELOPMENT == 0)) || [[ -n "$TRUSTED_KEY_SHA256" ]]; then
    [[ "$TRUSTED_KEY_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
        echo "Production install requires an out-of-band --trusted-key-sha256 pin." >&2
        exit 2
    }
fi
VERIFY_ARGS=("$MANIFEST_TOOL" verify "$RELEASE_ROOT" --require-offline)
if [[ -n "$TRUSTED_KEY_SHA256" ]]; then
    VERIFY_ARGS+=(--trusted-linux-key-sha256 "$TRUSTED_KEY_SHA256")
fi
if ((ALLOW_DEVELOPMENT == 0)); then VERIFY_ARGS+=(--require-production); fi
MANIFEST_JSON="$($PYTHON_BIN "${VERIFY_ARGS[@]}")"

readarray -t CONTRACT < <(
    "$PYTHON_BIN" -c 'import json,sys; m=json.load(sys.stdin); r=m["rust_binary"]; s=r["signature"]; print(m["toolchains"]["python"]); print(str(m["production_eligible"]).lower()); print(r["platform"]); print(s.get("signer_fingerprint", "")); print(m["size_contract"]["python_runtime_max_bytes"]); print(m["size_contract"]["minimum_free_after_install_bytes"]); print(m["size_contract"]["total_runtime_storage_max_bytes"]); print(r["path"])' <<<"$MANIFEST_JSON"
)
EXPECTED_PYTHON="${CONTRACT[0]}"
PRODUCTION_ELIGIBLE="${CONTRACT[1]}"
RUST_PLATFORM="${CONTRACT[2]}"
MANIFEST_FINGERPRINT="${CONTRACT[3]}"
PYTHON_RUNTIME_MAX_BYTES="${CONTRACT[4]}"
MINIMUM_FREE_AFTER_INSTALL_BYTES="${CONTRACT[5]}"
TOTAL_RUNTIME_STORAGE_MAX_BYTES="${CONTRACT[6]}"
RUST_RELATIVE_PATH="${CONTRACT[7]}"

[[ "$RUST_PLATFORM" == "linux" ]] || { echo "Release contains a non-Linux Rust executable." >&2; exit 2; }
chmod 0755 "$RELEASE_ROOT/$RUST_RELATIVE_PATH"
if [[ "$PRODUCTION_ELIGIBLE" == "true" ]]; then
    [[ "$TRUSTED_KEY_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
        echo "Production install requires an out-of-band --trusted-key-sha256 pin." >&2
        exit 2
    }
    [[ "$TRUSTED_KEY_SHA256" == "$MANIFEST_FINGERPRINT" ]] || {
        echo "Release signing key does not match the operator trust pin." >&2
        exit 2
    }
elif ((ALLOW_DEVELOPMENT == 0)); then
    echo "Development-only release requires --allow-development-package." >&2
    exit 2
fi

ACTUAL_PYTHON="$($PYTHON_BIN "$MANIFEST_TOOL" check-python "$EXPECTED_PYTHON")"

ENVIRONMENT_PATH="$RELEASE_ROOT/.venv"
[[ ! -e "$ENVIRONMENT_PATH" && ! -L "$ENVIRONMENT_PATH" ]] || { echo "Refusing to replace existing $ENVIRONMENT_PATH" >&2; exit 2; }
if ((INSTALL_SYSTEMD == 1)); then
    [[ "$EUID" -eq 0 ]] || { echo "--install-systemd requires root." >&2; exit 2; }
    command -v systemd-analyze >/dev/null || { echo "systemd-analyze is required for --install-systemd." >&2; exit 2; }
    command -v chronyc >/dev/null || { echo "chronyc/Chrony is required for the production health gate." >&2; exit 2; }
    case "$RELEASE_ROOT/" in
        /home/*|/root/*|/run/user/*)
            echo "systemd installation requires the release under /opt or another non-home system path." >&2
            exit 2
            ;;
    esac
    [[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || { echo "Invalid service user." >&2; exit 2; }
    [[ "$BACKUP_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || { echo "Invalid backup user." >&2; exit 2; }
    [[ "$OFFSITE_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || { echo "Invalid offsite user." >&2; exit 2; }
    [[ "$MAINTENANCE_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || { echo "Invalid maintenance user." >&2; exit 2; }
    [[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || { echo "Invalid service name." >&2; exit 2; }
    SERVICE_GROUP="${SERVICE_GROUP:-$SERVICE_USER}"
    BACKUP_GROUP="${BACKUP_GROUP:-$BACKUP_USER}"
    OFFSITE_GROUP="${OFFSITE_GROUP:-$OFFSITE_USER}"
    MAINTENANCE_GROUP="${MAINTENANCE_GROUP:-$MAINTENANCE_USER}"
    id "$SERVICE_USER" >/dev/null 2>&1 || { echo "Service user does not exist: $SERVICE_USER" >&2; exit 2; }
    getent group "$SERVICE_GROUP" >/dev/null || { echo "Service group does not exist: $SERVICE_GROUP" >&2; exit 2; }
    id "$BACKUP_USER" >/dev/null 2>&1 || { echo "Backup user does not exist: $BACKUP_USER" >&2; exit 2; }
    getent group "$BACKUP_GROUP" >/dev/null || { echo "Backup group does not exist: $BACKUP_GROUP" >&2; exit 2; }
    id "$OFFSITE_USER" >/dev/null 2>&1 || { echo "Offsite user does not exist: $OFFSITE_USER" >&2; exit 2; }
    getent group "$OFFSITE_GROUP" >/dev/null || { echo "Offsite group does not exist: $OFFSITE_GROUP" >&2; exit 2; }
    id "$MAINTENANCE_USER" >/dev/null 2>&1 || { echo "Maintenance user does not exist: $MAINTENANCE_USER" >&2; exit 2; }
    getent group "$MAINTENANCE_GROUP" >/dev/null || { echo "Maintenance group does not exist: $MAINTENANCE_GROUP" >&2; exit 2; }
    SERVICE_UID="$(id -u "$SERVICE_USER")"
    BACKUP_UID="$(id -u "$BACKUP_USER")"
    OFFSITE_UID="$(id -u "$OFFSITE_USER")"
    MAINTENANCE_UID="$(id -u "$MAINTENANCE_USER")"
    SERVICE_GID="$(getent group "$SERVICE_GROUP" | awk -F: '{print $3}')"
    BACKUP_GID="$(getent group "$BACKUP_GROUP" | awk -F: '{print $3}')"
    OFFSITE_GID="$(getent group "$OFFSITE_GROUP" | awk -F: '{print $3}')"
    MAINTENANCE_GID="$(getent group "$MAINTENANCE_GROUP" | awk -F: '{print $3}')"
    [[ "$SERVICE_UID" != 0 && "$BACKUP_UID" != 0 && "$OFFSITE_UID" != 0 \
        && "$MAINTENANCE_UID" != 0 ]] || {
        echo "Service, backup, offsite, and maintenance identities must not be root." >&2
        exit 2
    }
    [[ "$SERVICE_GID" != 0 && "$BACKUP_GID" != 0 && "$OFFSITE_GID" != 0 \
        && "$MAINTENANCE_GID" != 0 ]] || {
        echo "Service, backup, offsite, and maintenance purpose groups must not be root." >&2
        exit 2
    }
    UNIQUE_UID_COUNT="$(printf '%s\n' "$SERVICE_UID" "$BACKUP_UID" "$OFFSITE_UID" "$MAINTENANCE_UID" | sort -u | wc -l)"
    [[ "$UNIQUE_UID_COUNT" == 4 ]] || {
        echo "Service, backup, offsite, and maintenance users must have distinct numeric UIDs." >&2
        exit 2
    }
    UNIQUE_GID_COUNT="$(printf '%s\n' "$SERVICE_GID" "$BACKUP_GID" "$OFFSITE_GID" "$MAINTENANCE_GID" | sort -u | wc -l)"
    [[ "$UNIQUE_GID_COUNT" == 4 ]] || {
        echo "Service, backup, offsite, and maintenance purpose groups must have distinct numeric GIDs." >&2
        exit 2
    }
    [[ ! -L "$DATA_ROOT" ]] || { echo "--data-root cannot be a symbolic link." >&2; exit 2; }
    DATA_ROOT="$(realpath -m -- "$DATA_ROOT")"
    [[ "$DATA_ROOT" == /* && "$DATA_ROOT" != "/" ]] || { echo "--data-root must be a specific absolute path." >&2; exit 2; }
    if [[ "$DATA_ROOT" == "$RELEASE_ROOT" \
        || "$DATA_ROOT" == "$RELEASE_ROOT/"* \
        || "$RELEASE_ROOT" == "$DATA_ROOT/"* ]]; then
        echo "--data-root and the signed release tree must not overlap." >&2
        exit 2
    fi
    DATA_VOLUME_PROBE="$DATA_ROOT"
    while [[ ! -e "$DATA_VOLUME_PROBE" ]]; do
        PARENT_PROBE="$(dirname -- "$DATA_VOLUME_PROBE")"
        [[ "$PARENT_PROBE" != "$DATA_VOLUME_PROBE" ]] || {
            echo "Could not resolve an existing data-volume ancestor." >&2
            exit 2
        }
        DATA_VOLUME_PROBE="$PARENT_PROBE"
    done
    [[ -d "$DATA_VOLUME_PROBE" && ! -L "$DATA_VOLUME_PROBE" ]] || {
        echo "Data-volume ancestor must be an unlinked directory: $DATA_VOLUME_PROBE" >&2
        exit 2
    }
    read -r DATA_VOLUME_TOTAL DATA_VOLUME_FREE < <(
        df -PB1 "$DATA_VOLUME_PROBE" | awk 'NR==2 {print $2, $4}'
    )
    ((DATA_VOLUME_TOTAL >= TOTAL_RUNTIME_STORAGE_MAX_BYTES)) || {
        echo "Data volume is below the runtime storage contract: total=$DATA_VOLUME_TOTAL required=$TOTAL_RUNTIME_STORAGE_MAX_BYTES" >&2
        exit 2
    }
    REQUIRED_DATA_FREE_BEFORE_BACKUP=$((MINIMUM_FREE_AFTER_INSTALL_BYTES + BACKUP_OPERATION_MAX_BYTES))
    ((DATA_VOLUME_FREE >= REQUIRED_DATA_FREE_BEFORE_BACKUP)) || {
        echo "Data volume lacks first-backup peak headroom: free=$DATA_VOLUME_FREE required=$REQUIRED_DATA_FREE_BEFORE_BACKUP" >&2
        exit 2
    }
    UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
    SLICE_PATH="/etc/systemd/system/${SERVICE_NAME}.slice"
    HEALTH_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}-ops-health.service"
    HEALTH_TIMER_PATH="/etc/systemd/system/${SERVICE_NAME}-ops-health.timer"
    BACKUP_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}-backup.service"
    BACKUP_TIMER_PATH="/etc/systemd/system/${SERVICE_NAME}-backup.timer"
    OFFSITE_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}-offsite-backup.service"
    MAINTENANCE_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}-offsite-maintenance.service"
    MAINTENANCE_TIMER_PATH="/etc/systemd/system/${SERVICE_NAME}-offsite-maintenance.timer"
    for UNIT_CANDIDATE in "$UNIT_PATH" "$SLICE_PATH" "$HEALTH_UNIT_PATH" "$HEALTH_TIMER_PATH" \
        "$BACKUP_UNIT_PATH" "$BACKUP_TIMER_PATH" "$OFFSITE_UNIT_PATH" \
        "$MAINTENANCE_UNIT_PATH" "$MAINTENANCE_TIMER_PATH"; do
        [[ ! -e "$UNIT_CANDIDATE" && ! -L "$UNIT_CANDIDATE" ]] || {
            echo "Refusing to replace existing or linked unit: $UNIT_CANDIDATE" >&2
            exit 2
        }
    done
fi
AVAILABLE_BYTES="$(df -PB1 "$RELEASE_ROOT" | awk 'NR==2 {print $4}')"
REQUIRED_FREE_BEFORE=$((PYTHON_RUNTIME_MAX_BYTES + MINIMUM_FREE_AFTER_INSTALL_BYTES))
((AVAILABLE_BYTES >= REQUIRED_FREE_BEFORE)) || {
    echo "Insufficient install headroom: free=$AVAILABLE_BYTES required=$REQUIRED_FREE_BEFORE" >&2
    exit 2
}

"$PYTHON_BIN" -m venv --copies "$ENVIRONMENT_PATH"
VENV_PYTHON="$ENVIRONMENT_PATH/bin/python"
"$VENV_PYTHON" -m pip install --disable-pip-version-check --no-index --no-cache-dir \
    --only-binary=:all: --find-links "$RELEASE_ROOT/wheelhouse" \
    --requirement "$RELEASE_ROOT/requirements-runtime.txt"
"$VENV_PYTHON" -m pip check

"$VENV_PYTHON" - "$ENVIRONMENT_PATH" <<'PY'
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
for directory, names, files in os.walk(root, followlinks=False):
    for name in (*names, *files):
        path = Path(directory, name)
        if path.is_symlink():
            resolved = path.resolve(strict=True)
            try:
                resolved.relative_to(root)
            except ValueError as exc:
                raise SystemExit(f"virtualenv link escapes its directory: {path} -> {resolved}") from exc
PY

RUNTIME_BYTES="$(du -sb "$ENVIRONMENT_PATH" | awk '{print $1}')"
((RUNTIME_BYTES <= PYTHON_RUNTIME_MAX_BYTES)) || {
    echo "Installed Python runtime exceeds its hard budget: $RUNTIME_BYTES" >&2
    exit 2
}
REMAINING_BYTES="$(df -PB1 "$RELEASE_ROOT" | awk 'NR==2 {print $4}')"
((REMAINING_BYTES >= MINIMUM_FREE_AFTER_INSTALL_BYTES)) || {
    echo "Install violated required free-space headroom: $REMAINING_BYTES" >&2
    exit 2
}

if ((INSTALL_SYSTEMD == 1)); then
    # Root owns the sticky top-level namespace. The service group can create
    # its database/WAL files but cannot rename or replace root-owned recovery
    # and offsite mountpoints.
    install -d -m 1770 -o root -g "$SERVICE_GROUP" "$DATA_ROOT"
    # Open every namespace component through a no-follow directory descriptor.
    # This makes an existing service-controlled symlink or replacement race a
    # hard installer failure instead of allowing root to escape DATA_ROOT.
    "$PYTHON_BIN" - "$DATA_ROOT" \
        "$SERVICE_UID" "$SERVICE_GID" "$BACKUP_UID" "$BACKUP_GID" \
        "$OFFSITE_UID" "$MAINTENANCE_UID" <<'PY'
import os
import stat
import sys

root, service_uid, service_gid, backup_uid, backup_gid, offsite_uid, maintenance_uid = sys.argv[1:]
root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
try:
    specifications = (
        ("runtime", int(service_uid), int(service_gid), 0o750),
        ("backups", int(backup_uid), int(service_gid), 0o2750),
        ("offsite", 0, int(backup_gid), 0o1770),
    )
    for name, uid, gid, mode in specifications:
        try:
            os.mkdir(name, mode=mode, dir_fd=root_fd)
        except FileExistsError:
            pass
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=root_fd,
        )
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise SystemExit(f"runtime namespace child is not a directory: {name}")
            os.fchown(descriptor, uid, gid)
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    offsite_fd = os.open(
        "offsite",
        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        dir_fd=root_fd,
    )
    try:
        for name, uid, mode in (
            ("upload", int(offsite_uid), 0o2750),
            ("maintenance", int(maintenance_uid), 0o2750),
            ("locks", 0, 0o2770),
        ):
            try:
                os.mkdir(name, mode=mode, dir_fd=offsite_fd)
            except FileExistsError:
                pass
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=offsite_fd,
            )
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISDIR(metadata.st_mode):
                    raise SystemExit(f"offsite namespace child is not a directory: {name}")
                os.fchown(descriptor, uid, int(backup_gid))
                os.fchmod(descriptor, mode)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        os.fsync(offsite_fd)
    finally:
        os.close(offsite_fd)
    os.fsync(root_fd)
finally:
    os.close(root_fd)
PY
    for DATABASE_MEMBER in \
        state.db state.db-wal state.db-shm \
        audit.db audit.db-wal audit.db-shm \
        research.db research.db-wal research.db-shm \
        migration-manifest.json .watchdog_state.json; do
        MEMBER_PATH="$DATA_ROOT/$DATABASE_MEMBER"
        if [[ -L "$MEMBER_PATH" ]]; then
            echo "Refusing linked runtime database member: $MEMBER_PATH" >&2
            exit 2
        elif [[ -e "$MEMBER_PATH" ]]; then
            [[ -f "$MEMBER_PATH" ]] || {
                echo "Runtime database member is not a regular file: $MEMBER_PATH" >&2
                exit 2
            }
            chown "$SERVICE_USER:$SERVICE_GROUP" "$MEMBER_PATH"
            chmod 0640 "$MEMBER_PATH"
        fi
    done
    RUNTIME_CONFIG_PATH="$DATA_ROOT/live_config.json"
    if [[ -L "$RUNTIME_CONFIG_PATH" ]]; then
        echo "Refusing linked runtime config: $RUNTIME_CONFIG_PATH" >&2
        exit 2
    elif [[ -e "$RUNTIME_CONFIG_PATH" ]]; then
        [[ -f "$RUNTIME_CONFIG_PATH" ]] || {
            echo "Runtime config is not a regular file: $RUNTIME_CONFIG_PATH" >&2
            exit 2
        }
        # Preserve an operator-provisioned config during this new-install-only
        # flow; replacing installed units remains explicitly unsupported.
        chown "$SERVICE_USER:$SERVICE_GROUP" "$RUNTIME_CONFIG_PATH"
        chmod 0640 "$RUNTIME_CONFIG_PATH"
    else
        install -m 0640 -o "$SERVICE_USER" -g "$SERVICE_GROUP" \
            "$RELEASE_ROOT/live_config.json" "$RUNTIME_CONFIG_PATH"
    fi
    # The service can read/execute the signed release but cannot mutate it.
    # All operational writes are confined to DATA_ROOT by the unit sandbox.
    chown -R "root:$SERVICE_GROUP" "$RELEASE_ROOT"
    chmod -R u=rwX,g=rX,o= "$RELEASE_ROOT"
    RELEASE_ROOT="$RELEASE_ROOT" DATA_ROOT="$DATA_ROOT" SERVICE_USER="$SERVICE_USER" \
        SERVICE_GROUP="$SERVICE_GROUP" BACKUP_USER="$BACKUP_USER" BACKUP_GROUP="$BACKUP_GROUP" \
        OFFSITE_USER="$OFFSITE_USER" OFFSITE_GROUP="$OFFSITE_GROUP" SERVICE_NAME="$SERVICE_NAME" \
        MAINTENANCE_USER="$MAINTENANCE_USER" MAINTENANCE_GROUP="$MAINTENANCE_GROUP" \
        "$VENV_PYTHON" - \
        "$RELEASE_ROOT/deployment/bongus.slice.in" "$SLICE_PATH" \
        "$RELEASE_ROOT/deployment/bongus.service.in" "$UNIT_PATH" \
        "$RELEASE_ROOT/deployment/bongus-ops-health.service.in" "$HEALTH_UNIT_PATH" \
        "$RELEASE_ROOT/deployment/bongus-ops-health.timer.in" "$HEALTH_TIMER_PATH" \
        "$RELEASE_ROOT/deployment/bongus-backup.service.in" "$BACKUP_UNIT_PATH" \
        "$RELEASE_ROOT/deployment/bongus-backup.timer.in" "$BACKUP_TIMER_PATH" \
        "$RELEASE_ROOT/deployment/bongus-offsite-backup.service.in" "$OFFSITE_UNIT_PATH" \
        "$RELEASE_ROOT/deployment/bongus-offsite-maintenance.service.in" "$MAINTENANCE_UNIT_PATH" \
        "$RELEASE_ROOT/deployment/bongus-offsite-maintenance.timer.in" "$MAINTENANCE_TIMER_PATH" <<'PY'
import os
import re
import sys
from pathlib import Path

replacements = {
    "@RELEASE_ROOT@": os.environ["RELEASE_ROOT"],
    "@DATA_ROOT@": os.environ["DATA_ROOT"],
    "@SERVICE_USER@": os.environ["SERVICE_USER"],
    "@SERVICE_GROUP@": os.environ["SERVICE_GROUP"],
    "@BACKUP_USER@": os.environ["BACKUP_USER"],
    "@BACKUP_GROUP@": os.environ["BACKUP_GROUP"],
    "@OFFSITE_USER@": os.environ["OFFSITE_USER"],
    "@OFFSITE_GROUP@": os.environ["OFFSITE_GROUP"],
    "@MAINTENANCE_USER@": os.environ["MAINTENANCE_USER"],
    "@MAINTENANCE_GROUP@": os.environ["MAINTENANCE_GROUP"],
    "@SERVICE_NAME@": os.environ["SERVICE_NAME"],
}
for source_name, destination_name in zip(sys.argv[1::2], sys.argv[2::2]):
    template = Path(source_name).read_text(encoding="utf-8")
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    if re.search(r"@[A-Z][A-Z0-9_]*@", template):
        raise SystemExit(f"unresolved systemd template marker in {source_name}")
    Path(destination_name).write_text(template, encoding="utf-8")
PY
    chmod 0644 "$SLICE_PATH" "$UNIT_PATH" "$HEALTH_UNIT_PATH" "$HEALTH_TIMER_PATH" \
        "$BACKUP_UNIT_PATH" "$BACKUP_TIMER_PATH" "$OFFSITE_UNIT_PATH" \
        "$MAINTENANCE_UNIT_PATH" "$MAINTENANCE_TIMER_PATH"
    systemd-analyze verify "$SLICE_PATH" "$UNIT_PATH" "$HEALTH_UNIT_PATH" "$HEALTH_TIMER_PATH" \
        "$BACKUP_UNIT_PATH" "$BACKUP_TIMER_PATH" "$OFFSITE_UNIT_PATH" \
        "$MAINTENANCE_UNIT_PATH" "$MAINTENANCE_TIMER_PATH"
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service"
    systemctl enable "${SERVICE_NAME}-ops-health.timer"
    systemctl enable "${SERVICE_NAME}-backup.timer"
    systemctl enable "${SERVICE_NAME}-offsite-maintenance.timer"
    echo "Installed and enabled ${SERVICE_NAME}.service, its read-only health timer, and its 10-minute backup timer; none was started."
    echo "The static offsite upload unit remains inactive until /etc/bongus/offsite-backup.env and Restic are operator-provisioned."
fi

echo "Linux release installed and verified (python_runtime_bytes=$RUNTIME_BYTES)."
echo "The service hard memory limit is 3500000000 bytes (3.5 GB)."
if ((INSTALL_SYSTEMD == 0)); then
    echo "No service unit was installed; this mode is development/test only, not production."
    echo "Development start only: $VENV_PYTHON -m bongus.monitoring.king_watchdog"
else
    echo "After provisioning root-only /etc/bongus/trader.env, start: sudo systemctl start ${SERVICE_NAME}.service ${SERVICE_NAME}-backup.timer ${SERVICE_NAME}-ops-health.timer"
fi
