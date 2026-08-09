#!/usr/bin/env bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: scripts/build_release.sh [options]

Build a native Linux release. Run this script on the same Linux architecture
as the server (x86_64 or arm64).

Options:
  --output PATH                   Release directory (must not exist)
  --python PATH                   Python 3.11 executable
  --rust-binary PATH              Prebuilt native Rust ELF executable
  --skip-rust-build               Do not invoke cargo
  --without-wheelhouse            Development validation only
  --no-archive                    Do not create the deterministic ZIP
  --allow-dirty-source            Development validation only
  --allow-unsigned-development    Development/testnet package only
  --signing-key PATH              PEM private key for a production package
  --signer-subject TEXT           Operator/audit identity for that key
EOF
}

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd -P)"
OUTPUT_PATH="$REPO_ROOT/dist/bongus-release-linux-$(uname -m)"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
RUST_BINARY_PATH=""
SIGNING_KEY=""
SIGNER_SUBJECT=""
SKIP_RUST_BUILD=0
WITHOUT_WHEELHOUSE=0
NO_ARCHIVE=0
ALLOW_DIRTY=0
ALLOW_UNSIGNED=0

while (($#)); do
    case "$1" in
        --output) OUTPUT_PATH="$2"; shift 2 ;;
        --python) PYTHON_BIN="$2"; shift 2 ;;
        --rust-binary) RUST_BINARY_PATH="$2"; shift 2 ;;
        --signing-key) SIGNING_KEY="$2"; shift 2 ;;
        --signer-subject) SIGNER_SUBJECT="$2"; shift 2 ;;
        --skip-rust-build) SKIP_RUST_BUILD=1; shift ;;
        --without-wheelhouse) WITHOUT_WHEELHOUSE=1; shift ;;
        --no-archive) NO_ARCHIVE=1; shift ;;
        --allow-dirty-source) ALLOW_DIRTY=1; shift ;;
        --allow-unsigned-development) ALLOW_UNSIGNED=1; shift ;;
        --help|-h) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done

[[ "$(uname -s)" == "Linux" ]] || { echo "Linux release builds must run on Linux." >&2; exit 2; }
command -v "$PYTHON_BIN" >/dev/null || { echo "Python executable not found: $PYTHON_BIN" >&2; exit 2; }
command -v git >/dev/null || { echo "git is required." >&2; exit 2; }

CONFIGURED_PYTHON="$(tr -d '[:space:]' < "$REPO_ROOT/.python-version")"
EXPECTED_PYTHON_SERIES="${CONFIGURED_PYTHON%.*}"
ACTUAL_PYTHON="$($PYTHON_BIN -c 'import platform; print(platform.python_version())')"
[[ "$ACTUAL_PYTHON" == "$EXPECTED_PYTHON_SERIES".* ]] || {
    echo "Release packaging requires Python $EXPECTED_PYTHON_SERIES.x; got $ACTUAL_PYTHON." >&2
    exit 2
}
EXPECTED_RUST="$(sed -n 's/^[[:space:]]*channel[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$REPO_ROOT/rust-toolchain.toml")"
[[ -n "$EXPECTED_RUST" ]] || { echo "rust-toolchain.toml has no channel." >&2; exit 2; }

mapfile -t GIT_STATUS < <(git -C "$REPO_ROOT" status --porcelain=v1 --untracked-files=normal)
if ((${#GIT_STATUS[@]} > 0 && ALLOW_DIRTY == 0)); then
    echo "The worktree is dirty. Commit/review changes or pass --allow-dirty-source." >&2
    exit 2
fi
SOURCE_REVISION="$(git -C "$REPO_ROOT" rev-parse HEAD)"
[[ "$SOURCE_REVISION" =~ ^[0-9a-f]{40}$ ]] || { echo "Cannot resolve source revision." >&2; exit 2; }
if ((${#GIT_STATUS[@]} > 0)); then SOURCE_REVISION="${SOURCE_REVISION}-dirty"; fi

if [[ -z "$RUST_BINARY_PATH" ]]; then
    if ((SKIP_RUST_BUILD == 0)); then
        command -v cargo >/dev/null || { echo "cargo is required on the build host." >&2; exit 2; }
        cargo build --manifest-path "$REPO_ROOT/execution_engine/Cargo.toml" --locked --release
    fi
    RUST_BINARY_PATH="$REPO_ROOT/execution_engine/target/release/execution_engine"
elif [[ "$RUST_BINARY_PATH" != /* ]]; then
    RUST_BINARY_PATH="$REPO_ROOT/$RUST_BINARY_PATH"
fi
RUST_BINARY_PATH="$(realpath -e -- "$RUST_BINARY_PATH")"
[[ -f "$RUST_BINARY_PATH" && ! -L "$RUST_BINARY_PATH" && -s "$RUST_BINARY_PATH" ]] || {
    echo "Native Rust executable is missing, linked, or empty: $RUST_BINARY_PATH" >&2
    exit 2
}

OUTPUT_PARENT="$(dirname -- "$OUTPUT_PATH")"
mkdir -p -- "$OUTPUT_PARENT"
OUTPUT_PARENT="$(realpath -e -- "$OUTPUT_PARENT")"
OUTPUT_PATH="$OUTPUT_PARENT/$(basename -- "$OUTPUT_PATH")"
[[ "$OUTPUT_PATH" != "$REPO_ROOT" && ! -e "$OUTPUT_PATH" ]] || {
    echo "Release output must be new and cannot be the repository root: $OUTPUT_PATH" >&2
    exit 2
}
ARCHIVE_PATH="${OUTPUT_PATH}.zip"
if ((NO_ARCHIVE == 0)) && { [[ -e "$ARCHIVE_PATH" ]] || [[ -e "${ARCHIVE_PATH}.sha256" ]]; }; then
    echo "Refusing to replace an existing archive: $ARCHIVE_PATH" >&2
    exit 2
fi

STAGING_ROOT="$(mktemp -d "$OUTPUT_PARENT/.bongus-release.XXXXXXXX")"
STAGING_ACTIVE=1
cleanup() {
    if ((STAGING_ACTIVE == 1)); then rm -rf -- "$STAGING_ROOT"; fi
}
trap cleanup EXIT

copy_release_file() {
    local source="$1" relative="$2" destination
    [[ -f "$source" && ! -L "$source" ]] || { echo "Unsafe release input: $source" >&2; exit 2; }
    destination="$STAGING_ROOT/$relative"
    mkdir -p -- "$(dirname -- "$destination")"
    cp -- "$source" "$destination"
}

while IFS= read -r -d '' source; do
    relative="${source#"$REPO_ROOT/"}"
    case "$relative" in
        bongus/research/*|bongus/testing/*) continue ;;
        *.py|*.json|*.html) copy_release_file "$source" "$relative" ;;
    esac
done < <(find "$REPO_ROOT/bongus" -type f -print0)

copy_release_file "$REPO_ROOT/scripts/__init__.py" "scripts/__init__.py"
copy_release_file "$REPO_ROOT/scripts/live_trader_v2.py" "scripts/live_trader_v2.py"
copy_release_file "$REPO_ROOT/scripts/release_manifest.py" "scripts/release_manifest.py"
copy_release_file "$REPO_ROOT/requirements-runtime.txt" "requirements-runtime.txt"
copy_release_file "$REPO_ROOT/live_config.json" "live_config.json"
copy_release_file "$REPO_ROOT/LICENSE" "LICENSE"
copy_release_file "$REPO_ROOT/deployment/Install-BongusRelease.sh" "Install-BongusRelease.sh"
copy_release_file "$REPO_ROOT/deployment/Install-BongusRelease.ps1" "Install-BongusRelease.ps1"
copy_release_file "$REPO_ROOT/deployment/bongus.service.in" "deployment/bongus.service.in"
copy_release_file "$REPO_ROOT/deployment/README.md" "README.md"
copy_release_file "$RUST_BINARY_PATH" "bin/execution_engine"
chmod 0755 "$STAGING_ROOT/bin/execution_engine" "$STAGING_ROOT/Install-BongusRelease.sh"

$PYTHON_BIN - "$STAGING_ROOT/bongus/runtime/process_manifest.json" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("schema_version") != 1 or payload["processes"]["rust"].get("kind") != "binary":
    raise SystemExit("source process manifest does not declare the Rust binary")
payload["processes"]["rust"]["target"] = "bin/execution_engine"
path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
PY

if ((WITHOUT_WHEELHOUSE == 0)); then
    mkdir -p "$STAGING_ROOT/wheelhouse"
    "$PYTHON_BIN" -m pip wheel --disable-pip-version-check --no-deps \
        --requirement "$REPO_ROOT/requirements-runtime.txt" \
        --wheel-dir "$STAGING_ROOT/wheelhouse"
fi

PRODUCTION_ELIGIBLE=0
MANIFEST_SIGNATURE_ARGS=(--rust-signature-status NotChecked)
if [[ -n "$SIGNING_KEY" ]]; then
    command -v openssl >/dev/null || { echo "openssl is required for signing." >&2; exit 2; }
    SIGNING_KEY="$(realpath -e -- "$SIGNING_KEY")"
    [[ -f "$SIGNING_KEY" && ! -L "$SIGNING_KEY" ]] || { echo "Unsafe signing key." >&2; exit 2; }
    [[ -n "$SIGNER_SUBJECT" ]] || { echo "--signer-subject is required with --signing-key." >&2; exit 2; }
    mkdir -p "$STAGING_ROOT/signatures"
    openssl pkey -in "$SIGNING_KEY" -pubout -out "$STAGING_ROOT/signatures/linux-release-public.pem"
    openssl dgst -sha256 -sign "$SIGNING_KEY" \
        -out "$STAGING_ROOT/signatures/execution_engine.sig" \
        "$STAGING_ROOT/bin/execution_engine"
    FINGERPRINT="$(openssl pkey -pubin -in "$STAGING_ROOT/signatures/linux-release-public.pem" -outform DER | sha256sum | awk '{print $1}')"
    MANIFEST_SIGNATURE_ARGS=(
        --rust-signature-status Valid
        --rust-signature-scheme openssl-sha256
        --rust-signer-fingerprint "$FINGERPRINT"
        --rust-signer-subject "$SIGNER_SUBJECT"
        --rust-signature-path signatures/execution_engine.sig
        --rust-public-key-path signatures/linux-release-public.pem
    )
    if ((WITHOUT_WHEELHOUSE == 0 && ${#GIT_STATUS[@]} == 0)); then PRODUCTION_ELIGIBLE=1; fi
elif ((ALLOW_UNSIGNED == 0)); then
    echo "Production Linux packaging requires --signing-key, or explicitly use --allow-unsigned-development." >&2
    exit 2
fi

MANIFEST_ARGS=(
    "$REPO_ROOT/scripts/release_manifest.py" create "$STAGING_ROOT"
    --source-revision "$SOURCE_REVISION"
    --python-version "$ACTUAL_PYTHON"
    --rust-toolchain "$EXPECTED_RUST"
    "${MANIFEST_SIGNATURE_ARGS[@]}"
)
if ((PRODUCTION_ELIGIBLE == 1)); then MANIFEST_ARGS+=(--production-eligible); fi
if ((WITHOUT_WHEELHOUSE == 1)); then MANIFEST_ARGS+=(--allow-missing-wheelhouse); fi
"$PYTHON_BIN" "${MANIFEST_ARGS[@]}" >/dev/null

VERIFY_ARGS=("$REPO_ROOT/scripts/release_manifest.py" verify "$STAGING_ROOT")
if ((WITHOUT_WHEELHOUSE == 0)); then VERIFY_ARGS+=(--require-offline); fi
if ((PRODUCTION_ELIGIBLE == 1)); then VERIFY_ARGS+=(--require-production); fi
"$PYTHON_BIN" "${VERIFY_ARGS[@]}" >/dev/null

mv -- "$STAGING_ROOT" "$OUTPUT_PATH"
STAGING_ACTIVE=0
if ((NO_ARCHIVE == 0)); then
    "$PYTHON_BIN" "$REPO_ROOT/scripts/release_manifest.py" archive "$OUTPUT_PATH" "$ARCHIVE_PATH" >/dev/null
fi

"$PYTHON_BIN" - "$OUTPUT_PATH/release-manifest.json" "$OUTPUT_PATH" "$ARCHIVE_PATH" <<'PY'
import json
import sys
from pathlib import Path

manifest = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"ReleaseDirectory={sys.argv[2]}")
print(f"Archive={sys.argv[3] if Path(sys.argv[3]).exists() else ''}")
print(f"ProductionEligible={str(manifest['production_eligible']).lower()}")
print(f"OfflineInstallable={str(manifest['offline_installable']).lower()}")
print(f"RustBinarySha256={manifest['rust_binary']['sha256']}")
print(f"MemoryMaxBytes={manifest['size_contract']['total_runtime_memory_max_bytes']}")
PY
