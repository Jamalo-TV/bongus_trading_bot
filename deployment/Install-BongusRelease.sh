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
DATA_ROOT="/var/lib/bongus"
SERVICE_NAME="bongus"

while (($#)); do
    case "$1" in
        --python) PYTHON_BIN="$2"; shift 2 ;;
        --allow-development-package) ALLOW_DEVELOPMENT=1; shift ;;
        --trusted-key-sha256) TRUSTED_KEY_SHA256="$2"; shift 2 ;;
        --install-systemd) INSTALL_SYSTEMD=1; shift ;;
        --service-user) SERVICE_USER="$2"; shift 2 ;;
        --service-group) SERVICE_GROUP="$2"; shift 2 ;;
        --data-root) DATA_ROOT="$2"; shift 2 ;;
        --service-name) SERVICE_NAME="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || { echo "This installer is for Linux." >&2; exit 2; }
command -v "$PYTHON_BIN" >/dev/null || { echo "Python executable not found: $PYTHON_BIN" >&2; exit 2; }
MANIFEST_TOOL="$RELEASE_ROOT/scripts/release_manifest.py"
[[ -f "$MANIFEST_TOOL" && ! -L "$MANIFEST_TOOL" ]] || { echo "Release verifier is missing or linked." >&2; exit 2; }

VERIFY_ARGS=("$MANIFEST_TOOL" verify "$RELEASE_ROOT" --require-offline)
if ((ALLOW_DEVELOPMENT == 0)); then VERIFY_ARGS+=(--require-production); fi
MANIFEST_JSON="$($PYTHON_BIN "${VERIFY_ARGS[@]}")"

readarray -t CONTRACT < <(
    "$PYTHON_BIN" -c 'import json,sys; m=json.load(sys.stdin); r=m["rust_binary"]; s=r["signature"]; print(m["toolchains"]["python"]); print(str(m["production_eligible"]).lower()); print(r["platform"]); print(s.get("signer_fingerprint", "")); print(m["size_contract"]["python_runtime_max_bytes"]); print(m["size_contract"]["minimum_free_after_install_bytes"]); print(m["size_contract"]["total_runtime_memory_max_bytes"]); print(r["path"])' <<<"$MANIFEST_JSON"
)
EXPECTED_PYTHON="${CONTRACT[0]}"
PRODUCTION_ELIGIBLE="${CONTRACT[1]}"
RUST_PLATFORM="${CONTRACT[2]}"
MANIFEST_FINGERPRINT="${CONTRACT[3]}"
PYTHON_RUNTIME_MAX_BYTES="${CONTRACT[4]}"
MINIMUM_FREE_AFTER_INSTALL_BYTES="${CONTRACT[5]}"
TOTAL_MEMORY_MAX_BYTES="${CONTRACT[6]}"
RUST_RELATIVE_PATH="${CONTRACT[7]}"

[[ "$RUST_PLATFORM" == "linux" ]] || { echo "Release contains a non-Linux Rust executable." >&2; exit 2; }
chmod 0755 "$RELEASE_ROOT/$RUST_RELATIVE_PATH"
if [[ "$PRODUCTION_ELIGIBLE" == "true" ]]; then
    TRUSTED_KEY_SHA256="${TRUSTED_KEY_SHA256,,}"
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

ACTUAL_PYTHON="$($PYTHON_BIN -c 'import platform; print(platform.python_version())')"
[[ "$ACTUAL_PYTHON" == "$EXPECTED_PYTHON" ]] || {
    echo "Installer requires exact Python $EXPECTED_PYTHON; got $ACTUAL_PYTHON." >&2
    exit 2
}

ENVIRONMENT_PATH="$RELEASE_ROOT/.venv"
[[ ! -e "$ENVIRONMENT_PATH" ]] || { echo "Refusing to replace existing $ENVIRONMENT_PATH" >&2; exit 2; }
if ((INSTALL_SYSTEMD == 1)); then
    [[ "$EUID" -eq 0 ]] || { echo "--install-systemd requires root." >&2; exit 2; }
    case "$RELEASE_ROOT/" in
        /home/*|/root/*|/run/user/*)
            echo "systemd installation requires the release under /opt or another non-home system path." >&2
            exit 2
            ;;
    esac
    [[ "$SERVICE_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || { echo "Invalid service user." >&2; exit 2; }
    [[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.@-]+$ ]] || { echo "Invalid service name." >&2; exit 2; }
    SERVICE_GROUP="${SERVICE_GROUP:-$SERVICE_USER}"
    id "$SERVICE_USER" >/dev/null 2>&1 || { echo "Service user does not exist: $SERVICE_USER" >&2; exit 2; }
    getent group "$SERVICE_GROUP" >/dev/null || { echo "Service group does not exist: $SERVICE_GROUP" >&2; exit 2; }
    DATA_ROOT="$(realpath -m -- "$DATA_ROOT")"
    [[ "$DATA_ROOT" == /* && "$DATA_ROOT" != "/" ]] || { echo "--data-root must be a specific absolute path." >&2; exit 2; }
    UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
    [[ ! -e "$UNIT_PATH" ]] || { echo "Refusing to replace existing unit: $UNIT_PATH" >&2; exit 2; }
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
    install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_ROOT"
    chown -R "$SERVICE_USER:$SERVICE_GROUP" "$RELEASE_ROOT"
    RELEASE_ROOT="$RELEASE_ROOT" DATA_ROOT="$DATA_ROOT" SERVICE_USER="$SERVICE_USER" \
        SERVICE_GROUP="$SERVICE_GROUP" TOTAL_MEMORY_MAX_BYTES="$TOTAL_MEMORY_MAX_BYTES" \
        "$VENV_PYTHON" - "$RELEASE_ROOT/deployment/bongus.service.in" "$UNIT_PATH" <<'PY'
import os
import sys
from pathlib import Path

template = Path(sys.argv[1]).read_text(encoding="utf-8")
replacements = {
    "@RELEASE_ROOT@": os.environ["RELEASE_ROOT"],
    "@DATA_ROOT@": os.environ["DATA_ROOT"],
    "@SERVICE_USER@": os.environ["SERVICE_USER"],
    "@SERVICE_GROUP@": os.environ["SERVICE_GROUP"],
    "@MEMORY_MAX_BYTES@": os.environ["TOTAL_MEMORY_MAX_BYTES"],
}
for marker, value in replacements.items():
    template = template.replace(marker, value)
if "@" in template:
    raise SystemExit("unresolved systemd template marker")
Path(sys.argv[2]).write_text(template, encoding="utf-8")
PY
    chmod 0644 "$UNIT_PATH"
    systemctl daemon-reload
    systemctl enable "${SERVICE_NAME}.service"
    echo "Installed and enabled ${SERVICE_NAME}.service; it was not started."
fi

echo "Linux release installed and verified (python_runtime_bytes=$RUNTIME_BYTES)."
echo "The service hard memory limit is $TOTAL_MEMORY_MAX_BYTES bytes (20 GB)."
if ((INSTALL_SYSTEMD == 0)); then
    echo "Start explicitly: $VENV_PYTHON -m bongus.monitoring.king_watchdog"
else
    echo "After creating $RELEASE_ROOT/.env, start: sudo systemctl start ${SERVICE_NAME}.service"
fi
