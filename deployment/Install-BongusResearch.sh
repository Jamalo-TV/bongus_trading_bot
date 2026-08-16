#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: deployment/Install-BongusResearch.sh [options]

Install a manifest-bound isolated research release with no network package
access. The unit is verified and enabled, but never started automatically.

Options:
  --python PATH              Exact Python 3.11.15 executable (default: python3.11)
  --environment-root PATH    New virtual environment (default: /opt/bongus-research/venv)
  --data-root PATH           Dedicated evidence directory (default: /var/lib/bongus-research)
  --live-data-root PATH      Path hidden from service (default: /var/lib/bongus)
  --service-user USER        Dedicated unprivileged user (default: bongus-research)
  --service-group GROUP      Dedicated unprivileged group (default: bongus-research)
  --service-name NAME        Unit basename (default: bongus-research)
EOF
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
RESEARCH_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
PYTHON_BIN="python3.11"
ENVIRONMENT_ROOT="/opt/bongus-research/venv"
DATA_ROOT="/var/lib/bongus-research"
LIVE_DATA_ROOT="/var/lib/bongus"
SERVICE_USER="bongus-research"
SERVICE_GROUP="bongus-research"
SERVICE_NAME="bongus-research"

while (($#)); do
    case "$1" in
        --python) PYTHON_BIN="$2"; shift 2 ;;
        --environment-root) ENVIRONMENT_ROOT="$2"; shift 2 ;;
        --data-root) DATA_ROOT="$2"; shift 2 ;;
        --live-data-root) LIVE_DATA_ROOT="$2"; shift 2 ;;
        --service-user) SERVICE_USER="$2"; shift 2 ;;
        --service-group) SERVICE_GROUP="$2"; shift 2 ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

fail() { echo "$*" >&2; exit 2; }

[[ "$(uname -s)" == "Linux" ]] || fail "Research service installation requires Linux."
[[ "$EUID" -eq 0 ]] || fail "Research service installation requires root."
[[ "$RESEARCH_ROOT" == /opt/bongus-research/releases/* ]] || {
    fail "Extract the immutable release below /opt/bongus-research/releases before installation."
}
[[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || fail "Invalid service user."
[[ "$SERVICE_GROUP" =~ ^[a-z_][a-z0-9_-]{0,30}$ ]] || fail "Invalid service group."
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || fail "Invalid service name."
[[ ! -e /nonexistent && ! -L /nonexistent ]] || fail "The locked service home path unexpectedly exists."

for command_name in getent groupadd id install passwd realpath runuser systemctl systemd-analyze useradd usermod; do
    command -v "$command_name" >/dev/null || fail "Required host command is missing: $command_name"
done
PYTHON_REAL="$(command -v "$PYTHON_BIN")" || fail "Python executable not found: $PYTHON_BIN"
if command -v nologin >/dev/null; then
    NOLOGIN_SHELL="$(command -v nologin)"
elif [[ -x /usr/sbin/nologin ]]; then
    NOLOGIN_SHELL="/usr/sbin/nologin"
else
    fail "A nologin shell is required."
fi

for exact_path in "$ENVIRONMENT_ROOT" "$DATA_ROOT" "$LIVE_DATA_ROOT"; do
    [[ "$exact_path" == /* && "$exact_path" != "/" ]] || fail "Deployment paths must be specific absolute paths."
done
ENVIRONMENT_ROOT="$(realpath -m -- "$ENVIRONMENT_ROOT")"
DATA_ROOT="$(realpath -m -- "$DATA_ROOT")"
LIVE_DATA_ROOT="$(realpath -m -- "$LIVE_DATA_ROOT")"
paths_overlap() {
    local first="$1" second="$2"
    [[ "$first" == "$second" || "$first" == "$second/"* || "$second" == "$first/"* ]]
}
paths_overlap "$RESEARCH_ROOT" "$ENVIRONMENT_ROOT" && fail "Release and environment roots must not overlap."
paths_overlap "$RESEARCH_ROOT" "$DATA_ROOT" && fail "Release and data roots must not overlap."
paths_overlap "$RESEARCH_ROOT" "$LIVE_DATA_ROOT" && fail "Release and live-data roots must not overlap."
paths_overlap "$ENVIRONMENT_ROOT" "$DATA_ROOT" && fail "Environment and data roots must not overlap."
paths_overlap "$ENVIRONMENT_ROOT" "$LIVE_DATA_ROOT" && fail "Environment and live-data roots must not overlap."
paths_overlap "$DATA_ROOT" "$LIVE_DATA_ROOT" && fail "Research and live-data roots must not overlap."
[[ ! -e "$ENVIRONMENT_ROOT" && ! -L "$ENVIRONMENT_ROOT" ]] || fail "Refusing to replace existing environment: $ENVIRONMENT_ROOT"
UNIT_DESTINATION="/etc/systemd/system/${SERVICE_NAME}.service"
[[ ! -e "$UNIT_DESTINATION" && ! -L "$UNIT_DESTINATION" ]] || fail "Refusing to replace an existing service unit."
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    fail "Refusing to install over an active research service."
fi

# This verifier runs before user/group, filesystem, package, or systemd mutation.
# It rejects links, non-root ownership, writable release content, missing or
# extra inventory, hash changes, a wrong host target, and an incomplete wheel.
"$PYTHON_REAL" -I - "$RESEARCH_ROOT" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import platform
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

root = Path(sys.argv[1]).resolve()
manifest_name = "research-release-manifest.json"
digest_name = "research-release-manifest.sha256"
manifest_path = root / manifest_name
digest_path = root / digest_name
for control in (manifest_path, digest_path):
    if control.is_symlink() or not control.is_file():
        raise SystemExit(f"missing or linked release control file: {control.name}")
manifest_bytes = manifest_path.read_bytes()
try:
    manifest = json.loads(manifest_bytes)
except (UnicodeDecodeError, json.JSONDecodeError) as exc:
    raise SystemExit("research manifest is not valid UTF-8 JSON") from exc
canonical = (json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()
if canonical != manifest_bytes:
    raise SystemExit("research manifest is not canonical JSON")
digest = hashlib.sha256(manifest_bytes).hexdigest()
if digest_path.read_text(encoding="ascii") != f"{digest}  {manifest_name}\n":
    raise SystemExit("research manifest digest mismatch")
if manifest.get("schema_version") != 1 or manifest.get("release_kind") != "bongus-cross-venue-research":
    raise SystemExit("unsupported research release manifest")
if set(manifest) != {
    "boundary_sha256", "entrypoints", "file_count", "files", "hash_algorithm",
    "release_kind", "requirements", "schema_version", "target", "wheelhouse",
}:
    raise SystemExit("research release manifest schema is not exact")
if manifest.get("hash_algorithm") != "sha256":
    raise SystemExit("research release must use SHA-256")
boundary_sha256 = manifest.get("boundary_sha256")
if (
    not isinstance(boundary_sha256, str)
    or len(boundary_sha256) != 64
    or any(character not in "0123456789abcdef" for character in boundary_sha256)
):
    raise SystemExit("research boundary digest is invalid")

target = manifest.get("target")
if not isinstance(target, dict):
    raise SystemExit("missing research target")
machine = platform.machine().casefold()
machine = "x86_64" if machine in {"amd64", "x86_64"} else machine
if target != {
    "architecture": machine,
    "os": "linux",
    "python_abi": "cp311",
    "python_implementation": "cp",
    "python_version": "3.11.15",
    "wheel_platform": f"manylinux_2_28_{machine}",
} or machine not in {"x86_64", "aarch64"}:
    raise SystemExit("release target does not match this Linux host")
if platform.python_version() != "3.11.15":
    raise SystemExit("installer requires exact Python 3.11.15")

entrypoints = [
    "scripts/screen_binance_hyperliquid_history.py",
    "scripts/collect_binance_hyperliquid_shadow.py",
    "scripts/replay_binance_hyperliquid.py",
    "scripts/backtest_binance_hyperliquid.py",
    "scripts/report_binance_hyperliquid.py",
    "scripts/verify_cross_venue_dataset.py",
    "scripts/evaluate_binance_hyperliquid.py",
    "scripts/probe_cross_venue_region.py",
    "scripts/evaluate_cross_venue_regions.py",
]
modules = [
    "__init__.py", "artifacts.py", "boundary.py", "cadence.py", "collector.py",
    "evaluation.py", "evidence.py", "feeds.py", "kernel.py", "normalization.py",
    "historical.py", "publication.py", "region_probe.py", "region_probe_network.py", "replay.py",
    "schema.py", "storage.py",
]
fixed = {
    *(f"bongus/research/cross_venue/{name}" for name in modules),
    "bongus/__init__.py",
    "bongus/research/__init__.py",
    "bongus/exchanges/__init__.py",
    "bongus/exchanges/hyperliquid_read_only.py",
    *entrypoints,
    "research/experiments/binance_hyperliquid_v1.json",
    "docs/BINANCE_HYPERLIQUID_RESEARCH.md",
    "requirements-cross-venue.txt",
    "deployment/bongus-research.service.in",
    "deployment/Install-BongusResearch.sh",
}
expected_wheel = f"pyarrow-23.0.1-cp311-cp311-manylinux_2_28_{machine}.whl"
fixed.add(f"wheelhouse-cross-venue/{expected_wheel}")
if manifest.get("entrypoints") != entrypoints:
    raise SystemExit("research CLI inventory mismatch")
requirements = manifest.get("requirements")
if requirements != {
    "path": "requirements-cross-venue.txt",
    "pins": ["pyarrow==23.0.1"],
    "sha256": hashlib.sha256((root / "requirements-cross-venue.txt").read_bytes()).hexdigest(),
}:
    raise SystemExit("research dependency contract mismatch")
dependency_lines = [
    line.strip()
    for line in (root / "requirements-cross-venue.txt").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
]
if dependency_lines != ["pyarrow==23.0.1"]:
    raise SystemExit("research dependency pin changed")
if manifest.get("wheelhouse") != {
    "complete": True,
    "path": "wheelhouse-cross-venue",
    "wheels": [expected_wheel],
}:
    raise SystemExit("offline wheelhouse contract mismatch")

files = manifest.get("files")
if not isinstance(files, list) or manifest.get("file_count") != len(files):
    raise SystemExit("invalid release file count")
expected = {}
for item in files:
    if not isinstance(item, dict) or set(item) != {"path", "sha256", "size"}:
        raise SystemExit("invalid release file entry")
    relative = item.get("path")
    if not isinstance(relative, str) or PurePosixPath(relative).as_posix() != relative:
        raise SystemExit("non-canonical release path")
    if relative.startswith("/") or ".." in PurePosixPath(relative).parts or relative in expected:
        raise SystemExit("unsafe or duplicate release path")
    expected[relative] = item
if set(expected) != fixed:
    raise SystemExit("release contains content outside the research-only allowlist")

for trusted_directory in (
    Path("/opt"),
    Path("/opt/bongus-research"),
    Path("/opt/bongus-research/releases"),
):
    trusted_stat = os.lstat(trusted_directory)
    if not stat.S_ISDIR(trusted_stat.st_mode) or trusted_stat.st_uid != 0 or trusted_stat.st_mode & 0o022:
        raise SystemExit(f"release parent is not root-owned and immutable: {trusted_directory}")

actual = {}
for directory, directory_names, file_names in os.walk(root, followlinks=False):
    directory_path = Path(directory)
    directory_stat = os.lstat(directory_path)
    if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(directory_stat.st_mode):
        raise SystemExit(f"unsafe release directory: {directory_path}")
    if directory_stat.st_uid != 0 or directory_stat.st_mode & 0o022:
        raise SystemExit(f"release directory is not root-owned and immutable: {directory_path}")
    for name in (*directory_names, *file_names):
        candidate = directory_path / name
        candidate_stat = os.lstat(candidate)
        if stat.S_ISLNK(candidate_stat.st_mode):
            raise SystemExit(f"release contains a symbolic link: {candidate}")
    for name in file_names:
        candidate = directory_path / name
        candidate_stat = os.lstat(candidate)
        if not stat.S_ISREG(candidate_stat.st_mode):
            raise SystemExit(f"release contains a non-regular file: {candidate}")
        if candidate_stat.st_uid != 0 or candidate_stat.st_mode & 0o022:
            raise SystemExit(f"release file is not root-owned and immutable: {candidate}")
        relative = candidate.relative_to(root).as_posix()
        if relative in {manifest_name, digest_name}:
            continue
        payload = candidate.read_bytes()
        actual[relative] = {"path": relative, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
if actual != expected:
    raise SystemExit("release inventory, size, or SHA-256 verification failed")

wheel = root / "wheelhouse-cross-venue" / expected_wheel
try:
    with zipfile.ZipFile(wheel) as archive:
        metadata_names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
        wheel_names = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
        if len(metadata_names) != 1 or len(wheel_names) != 1:
            raise SystemExit("PyArrow wheel metadata inventory is invalid")
        metadata = archive.read(metadata_names[0]).decode("utf-8")
        wheel_metadata = archive.read(wheel_names[0]).decode("utf-8")
except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
    raise SystemExit("PyArrow wheel is invalid") from exc
headers = {}
for line in metadata.splitlines():
    if not line:
        break
    if line[:1].isspace() or ":" not in line:
        continue
    key, value = line.split(":", 1)
    headers.setdefault(key.casefold(), []).append(value.strip())
if headers.get("name") != ["pyarrow"] or headers.get("version") != ["23.0.1"]:
    raise SystemExit("PyArrow wheel name/version mismatch")
if headers.get("requires-dist"):
    raise SystemExit("wheel has undeclared transitive dependencies")
if f"Tag: cp311-cp311-manylinux_2_28_{machine}" not in wheel_metadata.splitlines():
    raise SystemExit("PyArrow wheel target tag mismatch")
PY

verify_identity() {
    local passwd_entry group_entry account_name password_status account_uid account_gid account_home account_shell
    local group_name group_password group_gid group_members primary_group all_groups
    passwd_entry="$(getent passwd "$SERVICE_USER")" || fail "Service user lookup failed after creation."
    group_entry="$(getent group "$SERVICE_GROUP")" || fail "Service group lookup failed after creation."
    IFS=: read -r account_name _ account_uid account_gid _ account_home account_shell <<<"$passwd_entry"
    IFS=: read -r group_name group_password group_gid group_members <<<"$group_entry"
    [[ "$account_name" == "$SERVICE_USER" && "$group_name" == "$SERVICE_GROUP" ]] || fail "Dedicated identity name mismatch."
    [[ "$account_uid" =~ ^[0-9]+$ && "$account_uid" -gt 0 && "$account_uid" -lt 1000 ]] || fail "Service account must have a system UID."
    [[ "$group_gid" =~ ^[0-9]+$ && "$group_gid" -gt 0 && "$group_gid" -lt 1000 && "$account_gid" == "$group_gid" ]] || fail "Service account primary group mismatch."
    [[ "$account_home" == "/nonexistent" && "$account_shell" == "$NOLOGIN_SHELL" ]] || fail "Service account home/shell is not locked down."
    [[ -z "$group_members" || "$group_members" == "$SERVICE_USER" ]] || fail "Service group contains another account."
    if getent passwd | awk -F: -v gid="$group_gid" -v user="$SERVICE_USER" '$4 == gid && $1 != user { found=1 } END { exit found }'; then
        :
    else
        fail "Service group is the primary group of another account."
    fi
    primary_group="$(id -gn "$SERVICE_USER")"
    all_groups="$(id -Gn "$SERVICE_USER")"
    [[ "$primary_group" == "$SERVICE_GROUP" && "$all_groups" == "$SERVICE_GROUP" ]] || fail "Service account has non-dedicated group access."
    password_status="$(passwd -S "$SERVICE_USER" | awk '{print $2}')"
    [[ "$password_status" == "L" || "$password_status" == "LK" ]] || fail "Service account password is not locked."
}

USER_EXISTS=0
GROUP_EXISTS=0
getent passwd "$SERVICE_USER" >/dev/null && USER_EXISTS=1
getent group "$SERVICE_GROUP" >/dev/null && GROUP_EXISTS=1
if ((USER_EXISTS == 1 && GROUP_EXISTS == 0)); then
    fail "Existing service user has no exact dedicated group; refusing partial identity mutation."
fi
if ((GROUP_EXISTS == 1)); then
    group_entry="$(getent group "$SERVICE_GROUP")"
    IFS=: read -r _ _ group_gid group_members <<<"$group_entry"
    [[ "$group_gid" =~ ^[0-9]+$ && "$group_gid" -gt 0 && "$group_gid" -lt 1000 ]] || fail "Existing service group is not a system group."
    [[ -z "$group_members" || "$group_members" == "$SERVICE_USER" ]] || fail "Existing service group contains another account."
    if ! getent passwd | awk -F: -v gid="$group_gid" -v user="$SERVICE_USER" '$4 == gid && $1 != user { found=1 } END { exit found }'; then
        fail "Existing service group is the primary group of another account."
    fi
else
    groupadd --system "$SERVICE_GROUP"
fi
if ((USER_EXISTS == 1)); then
    verify_identity
else
    useradd --system --gid "$SERVICE_GROUP" --home-dir /nonexistent --shell "$NOLOGIN_SHELL" --no-create-home "$SERVICE_USER"
    usermod --lock "$SERVICE_USER"
    verify_identity
fi

"$PYTHON_REAL" -I -m venv --copies "$ENVIRONMENT_ROOT"
VENV_PYTHON="$ENVIRONMENT_ROOT/bin/python"
"$VENV_PYTHON" -I -m pip --isolated install --disable-pip-version-check --no-index --no-cache-dir \
    --only-binary=:all: --no-deps --find-links "$RESEARCH_ROOT/wheelhouse-cross-venue" \
    --requirement "$RESEARCH_ROOT/requirements-cross-venue.txt"
"$VENV_PYTHON" -I -m pip --isolated check
"$VENV_PYTHON" -I - <<'PY'
import pyarrow

if pyarrow.__version__ != "23.0.1":
    raise SystemExit("installed PyArrow version does not match the release pin")
if not pyarrow.Codec.is_available("zstd"):
    raise SystemExit("installed PyArrow lacks Zstandard support")
PY

install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_ROOT"
run_as_service() {
    runuser -u "$SERVICE_USER" -- env -i \
        HOME=/nonexistent PATH="$ENVIRONMENT_ROOT/bin:/usr/bin:/bin" \
        PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 "$@"
}

ENTRYPOINTS=(
    screen_binance_hyperliquid_history.py
    collect_binance_hyperliquid_shadow.py
    replay_binance_hyperliquid.py
    backtest_binance_hyperliquid.py
    report_binance_hyperliquid.py
    verify_cross_venue_dataset.py
    evaluate_binance_hyperliquid.py
    probe_cross_venue_region.py
    evaluate_cross_venue_regions.py
)
for entrypoint in "${ENTRYPOINTS[@]}"; do
    run_as_service "$VENV_PYTHON" -I "$RESEARCH_ROOT/scripts/$entrypoint" --help >/dev/null
done
run_as_service "$VENV_PYTHON" -I "$RESEARCH_ROOT/scripts/collect_binance_hyperliquid_shadow.py" \
    --database "$DATA_ROOT/research.db" \
    --artifact-root "$DATA_ROOT/artifacts" \
    --startup-check >/dev/null

UNIT_CHECK_DIR="$(mktemp -d /run/bongus-research-unit.XXXXXXXX)"
UNIT_TEMP="$UNIT_CHECK_DIR/${SERVICE_NAME}.service"
cleanup() { rm -rf -- "$UNIT_CHECK_DIR"; }
trap cleanup EXIT
RESEARCH_ROOT="$RESEARCH_ROOT" VENV_PYTHON="$VENV_PYTHON" DATA_ROOT="$DATA_ROOT" \
    LIVE_DATA_ROOT="$LIVE_DATA_ROOT" SERVICE_USER="$SERVICE_USER" SERVICE_GROUP="$SERVICE_GROUP" \
    "$VENV_PYTHON" -I - "$RESEARCH_ROOT/deployment/bongus-research.service.in" "$UNIT_TEMP" <<'PY'
import os
import sys
from pathlib import Path

source = Path(sys.argv[1]).read_text(encoding="utf-8")
replacements = {
    "@BONGUS_RESEARCH_ROOT@": os.environ["RESEARCH_ROOT"],
    "@BONGUS_RESEARCH_PYTHON@": os.environ["VENV_PYTHON"],
    "@BONGUS_RESEARCH_DATA@": os.environ["DATA_ROOT"],
    "@BONGUS_LIVE_DATA@": os.environ["LIVE_DATA_ROOT"],
    "User=bongus-research": f"User={os.environ['SERVICE_USER']}",
    "Group=bongus-research": f"Group={os.environ['SERVICE_GROUP']}",
}
for old, new in replacements.items():
    source = source.replace(old, new)
if "@BONGUS_" in source:
    raise SystemExit("unresolved research service placeholder")
Path(sys.argv[2]).write_text(source, encoding="utf-8", newline="\n")
PY
chmod 0644 "$UNIT_TEMP"
chown root:root "$UNIT_TEMP"
systemd-analyze verify "$UNIT_TEMP"
install -m 0644 -o root -g root "$UNIT_TEMP" "$UNIT_DESTINATION"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}.service"
if systemctl is-active --quiet "${SERVICE_NAME}.service"; then
    fail "Research service unexpectedly became active during enable-only installation."
fi
echo "Installed, boundary-checked, and enabled ${SERVICE_NAME}.service; it was not started."
