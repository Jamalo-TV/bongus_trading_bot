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
  --wheelhouse PATH               Prebuilt wheel directory to consume
  --approved-wheelhouse-lock PATH Separately reviewed filename/SHA-256 lock
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
WHEELHOUSE_SOURCE=""
APPROVED_WHEELHOUSE_LOCK=""
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
        --wheelhouse) WHEELHOUSE_SOURCE="$2"; shift 2 ;;
        --approved-wheelhouse-lock) APPROVED_WHEELHOUSE_LOCK="$2"; shift 2 ;;
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
ACTUAL_PYTHON="$($PYTHON_BIN "$REPO_ROOT/scripts/release_manifest.py" check-python "$CONFIGURED_PYTHON")"
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

if ((WITHOUT_WHEELHOUSE == 1)) && {
    [[ -n "$WHEELHOUSE_SOURCE" ]] || [[ -n "$APPROVED_WHEELHOUSE_LOCK" ]]
}; then
    echo "--without-wheelhouse cannot be combined with wheelhouse inputs." >&2
    exit 2
fi
if [[ -n "$WHEELHOUSE_SOURCE" ]]; then
    if [[ "$WHEELHOUSE_SOURCE" != /* ]]; then WHEELHOUSE_SOURCE="$REPO_ROOT/$WHEELHOUSE_SOURCE"; fi
    [[ -d "$WHEELHOUSE_SOURCE" && ! -L "$WHEELHOUSE_SOURCE" ]] || {
        echo "Prebuilt wheelhouse is missing, linked, or non-directory: $WHEELHOUSE_SOURCE" >&2
        exit 2
    }
    WHEELHOUSE_SOURCE="$(realpath -e -- "$WHEELHOUSE_SOURCE")"
fi
if [[ -n "$APPROVED_WHEELHOUSE_LOCK" ]]; then
    if [[ "$APPROVED_WHEELHOUSE_LOCK" != /* ]]; then
        APPROVED_WHEELHOUSE_LOCK="$REPO_ROOT/$APPROVED_WHEELHOUSE_LOCK"
    fi
    [[ -f "$APPROVED_WHEELHOUSE_LOCK" && ! -L "$APPROVED_WHEELHOUSE_LOCK" ]] || {
        echo "Approved wheelhouse lock is missing, linked, or non-regular." >&2
        exit 2
    }
    APPROVED_WHEELHOUSE_LOCK="$(realpath -e -- "$APPROVED_WHEELHOUSE_LOCK")"
fi
PRODUCTION_INTENT=0
if [[ -n "$SIGNING_KEY" ]] && ((WITHOUT_WHEELHOUSE == 0)) \
    && [[ -z "$APPROVED_WHEELHOUSE_LOCK" ]]; then
    echo "A signed wheelhouse requires --approved-wheelhouse-lock; version pins alone are insufficient." >&2
    exit 2
fi
if [[ -n "$SIGNING_KEY" ]] && ((
    ALLOW_UNSIGNED == 0 \
        && WITHOUT_WHEELHOUSE == 0 \
        && NO_ARCHIVE == 0 \
        && ${#GIT_STATUS[@]} == 0
)); then
    [[ -n "$WHEELHOUSE_SOURCE" && -n "$APPROVED_WHEELHOUSE_LOCK" ]] || {
        echo "Production packaging requires --wheelhouse and --approved-wheelhouse-lock from separate review." >&2
        exit 2
    }
    PRODUCTION_INTENT=1
fi

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
ARCHIVE_SHA256_PATH="${ARCHIVE_PATH}.sha256"
ARCHIVE_SIGNATURE_PATH="${ARCHIVE_PATH}.sig"
ARCHIVE_PUBLIC_KEY_PATH="${ARCHIVE_PATH}.public.pem"
if ((NO_ARCHIVE == 0)) && {
    [[ -e "$ARCHIVE_PATH" ]] \
        || [[ -e "$ARCHIVE_SHA256_PATH" ]] \
        || [[ -e "$ARCHIVE_SIGNATURE_PATH" ]] \
        || [[ -e "$ARCHIVE_PUBLIC_KEY_PATH" ]]
}; then
    echo "Refusing to replace an existing archive: $ARCHIVE_PATH" >&2
    exit 2
fi

STAGING_ROOT="$(mktemp -d "$OUTPUT_PARENT/.bongus-release.XXXXXXXX")"
ARTIFACT_STAGING_ROOT=""
OUTPUT_PUBLISHED=0
ARCHIVE_PUBLISHED=0
ARCHIVE_SHA256_PUBLISHED=0
ARCHIVE_SIGNATURE_PUBLISHED=0
ARCHIVE_PUBLIC_KEY_PUBLISHED=0
BUILD_COMMITTED=0
cleanup() {
    local status=$?
    local cleanup_failed=0
    trap - EXIT
    set +e
    if [[ -n "$ARTIFACT_STAGING_ROOT" && -d "$ARTIFACT_STAGING_ROOT" ]]; then
        rm -rf -- "$ARTIFACT_STAGING_ROOT" || cleanup_failed=1
    fi
    if [[ -d "$STAGING_ROOT" ]]; then rm -rf -- "$STAGING_ROOT" || cleanup_failed=1; fi
    if ((BUILD_COMMITTED == 0)); then
        if ((ARCHIVE_PUBLIC_KEY_PUBLISHED == 1)); then
            rm -f -- "$ARCHIVE_PUBLIC_KEY_PATH" || cleanup_failed=1
        fi
        if ((ARCHIVE_SIGNATURE_PUBLISHED == 1)); then
            rm -f -- "$ARCHIVE_SIGNATURE_PATH" || cleanup_failed=1
        fi
        if ((ARCHIVE_SHA256_PUBLISHED == 1)); then
            rm -f -- "$ARCHIVE_SHA256_PATH" || cleanup_failed=1
        fi
        if ((ARCHIVE_PUBLISHED == 1)); then rm -f -- "$ARCHIVE_PATH" || cleanup_failed=1; fi
        if ((OUTPUT_PUBLISHED == 1)); then rm -rf -- "$OUTPUT_PATH" || cleanup_failed=1; fi
    fi
    if ((cleanup_failed == 1)); then
        echo "Release cleanup did not remove every owned staging/publication path." >&2
        if ((status == 0)); then status=1; fi
    fi
    exit "$status"
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
copy_release_file "$REPO_ROOT/scripts/check_operational_health.py" "scripts/check_operational_health.py"
copy_release_file "$REPO_ROOT/scripts/create_verified_backup_set.py" "scripts/create_verified_backup_set.py"
copy_release_file "$REPO_ROOT/scripts/upload_verified_offsite_backup.py" "scripts/upload_verified_offsite_backup.py"
copy_release_file "$REPO_ROOT/scripts/maintain_offsite_repository.py" "scripts/maintain_offsite_repository.py"
copy_release_file "$REPO_ROOT/scripts/collect_testnet_account_evidence.py" "scripts/collect_testnet_account_evidence.py"
copy_release_file "$REPO_ROOT/scripts/collect_soak_evidence.py" "scripts/collect_soak_evidence.py"
copy_release_file "$REPO_ROOT/scripts/run_paper_soak.py" "scripts/run_paper_soak.py"
copy_release_file "$REPO_ROOT/scripts/collect_daily_reconciliation.py" "scripts/collect_daily_reconciliation.py"
copy_release_file "$REPO_ROOT/bongus/testing/__init__.py" "bongus/testing/__init__.py"
copy_release_file "$REPO_ROOT/bongus/testing/soak_evidence.py" "bongus/testing/soak_evidence.py"
copy_release_file "$REPO_ROOT/bongus/testing/paper_soak.py" "bongus/testing/paper_soak.py"
copy_release_file "$REPO_ROOT/bongus/testing/daily_reconciliation_evidence.py" "bongus/testing/daily_reconciliation_evidence.py"
copy_release_file "$REPO_ROOT/bongus/testing/measurement_evidence.py" "bongus/testing/measurement_evidence.py"
copy_release_file "$REPO_ROOT/backup_db.py" "backup_db.py"
copy_release_file "$REPO_ROOT/requirements-runtime.txt" "requirements-runtime.txt"
copy_release_file "$REPO_ROOT/live_config.json" "live_config.json"
copy_release_file "$REPO_ROOT/config/binance_endpoints_v1.json" "config/binance_endpoints_v1.json"
copy_release_file "$REPO_ROOT/LICENSE" "LICENSE"
copy_release_file "$REPO_ROOT/deployment/Install-BongusRelease.sh" "Install-BongusRelease.sh"
copy_release_file "$REPO_ROOT/deployment/Install-BongusRelease.ps1" "Install-BongusRelease.ps1"
copy_release_file "$REPO_ROOT/deployment/bongus.service.in" "deployment/bongus.service.in"
copy_release_file "$REPO_ROOT/deployment/bongus.slice.in" "deployment/bongus.slice.in"
copy_release_file "$REPO_ROOT/deployment/bongus-ops-health.service.in" "deployment/bongus-ops-health.service.in"
copy_release_file "$REPO_ROOT/deployment/bongus-ops-health.timer.in" "deployment/bongus-ops-health.timer.in"
copy_release_file "$REPO_ROOT/deployment/bongus-backup.service.in" "deployment/bongus-backup.service.in"
copy_release_file "$REPO_ROOT/deployment/bongus-backup.timer.in" "deployment/bongus-backup.timer.in"
copy_release_file "$REPO_ROOT/deployment/bongus-offsite-backup.service.in" "deployment/bongus-offsite-backup.service.in"
copy_release_file "$REPO_ROOT/deployment/bongus-offsite-maintenance.service.in" "deployment/bongus-offsite-maintenance.service.in"
copy_release_file "$REPO_ROOT/deployment/bongus-offsite-maintenance.timer.in" "deployment/bongus-offsite-maintenance.timer.in"
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
    if [[ -n "$WHEELHOUSE_SOURCE" ]]; then
        WHEEL_COUNT=0
        while IFS= read -r -d '' wheel; do
            [[ -f "$wheel" && ! -L "$wheel" && "$wheel" == *.whl ]] || {
                echo "Prebuilt wheelhouse contains a linked, nested, or non-wheel entry: $wheel" >&2
                exit 2
            }
            cp -- "$wheel" "$STAGING_ROOT/wheelhouse/$(basename -- "$wheel")"
            WHEEL_COUNT=$((WHEEL_COUNT + 1))
        done < <(find "$WHEELHOUSE_SOURCE" -mindepth 1 -maxdepth 1 -print0)
        ((WHEEL_COUNT > 0)) || { echo "Prebuilt wheelhouse is empty." >&2; exit 2; }
    else
        # Network/materialized bytes can never qualify by version alone: an
        # explicitly supplied reviewed lock must match every filename/hash
        # before the release manifest or archive is signed.
        "$PYTHON_BIN" -m pip wheel --disable-pip-version-check --no-deps \
            --only-binary=:all: \
            --requirement "$REPO_ROOT/requirements-runtime.txt" \
            --wheel-dir "$STAGING_ROOT/wheelhouse"
    fi
    if [[ -n "$APPROVED_WHEELHOUSE_LOCK" ]]; then
        copy_release_file "$APPROVED_WHEELHOUSE_LOCK" "wheelhouse.lock.json"
    fi
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
    if ((PRODUCTION_INTENT == 1)); then PRODUCTION_ELIGIBLE=1; fi
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

if [[ -n "$SIGNING_KEY" ]]; then
    # Authenticate the inventory itself.  The detached Rust signature alone
    # cannot protect packaged Python, installer, or systemd bytes.
    openssl dgst -sha256 -sign "$SIGNING_KEY" \
        -out "$STAGING_ROOT/signatures/release-manifest.sig" \
        "$STAGING_ROOT/release-manifest.json"
fi

VERIFY_ARGS=("$REPO_ROOT/scripts/release_manifest.py" verify "$STAGING_ROOT")
if ((WITHOUT_WHEELHOUSE == 0)); then VERIFY_ARGS+=(--require-offline); fi
if ((PRODUCTION_ELIGIBLE == 1)); then
    VERIFY_ARGS+=(--require-production --trusted-linux-key-sha256 "$FINGERPRINT")
fi
"$PYTHON_BIN" "${VERIFY_ARGS[@]}" >/dev/null

if ((NO_ARCHIVE == 0)); then
    # Construct, sign, and verify the archive away from all public output
    # names.  In particular, an archive-signing failure cannot leave a
    # production-eligible release directory or a partial external artifact.
    ARTIFACT_STAGING_ROOT="$(mktemp -d "$OUTPUT_PARENT/.bongus-release-artifacts.XXXXXXXX")"
    STAGED_ARCHIVE_PATH="$ARTIFACT_STAGING_ROOT/$(basename -- "$ARCHIVE_PATH")"
    STAGED_ARCHIVE_SHA256_PATH="${STAGED_ARCHIVE_PATH}.sha256"
    STAGED_ARCHIVE_SIGNATURE_PATH="${STAGED_ARCHIVE_PATH}.sig"
    STAGED_ARCHIVE_PUBLIC_KEY_PATH="${STAGED_ARCHIVE_PATH}.public.pem"
    "$PYTHON_BIN" "$REPO_ROOT/scripts/release_manifest.py" archive \
        "$STAGING_ROOT" "$STAGED_ARCHIVE_PATH" >/dev/null
    if [[ -n "$SIGNING_KEY" ]]; then
        cp -- "$STAGING_ROOT/signatures/linux-release-public.pem" "$STAGED_ARCHIVE_PUBLIC_KEY_PATH"
        openssl dgst -sha256 -sign "$SIGNING_KEY" \
            -out "$STAGED_ARCHIVE_SIGNATURE_PATH" "$STAGED_ARCHIVE_PATH"
        openssl dgst -sha256 -verify "$STAGED_ARCHIVE_PUBLIC_KEY_PATH" \
            -signature "$STAGED_ARCHIVE_SIGNATURE_PATH" "$STAGED_ARCHIVE_PATH" >/dev/null
    fi
fi

mv -- "$STAGING_ROOT" "$OUTPUT_PATH"
OUTPUT_PUBLISHED=1
if ((NO_ARCHIVE == 0)); then
    mv -- "$STAGED_ARCHIVE_PATH" "$ARCHIVE_PATH"
    ARCHIVE_PUBLISHED=1
    mv -- "$STAGED_ARCHIVE_SHA256_PATH" "$ARCHIVE_SHA256_PATH"
    ARCHIVE_SHA256_PUBLISHED=1
    if [[ -n "$SIGNING_KEY" ]]; then
        mv -- "$STAGED_ARCHIVE_SIGNATURE_PATH" "$ARCHIVE_SIGNATURE_PATH"
        ARCHIVE_SIGNATURE_PUBLISHED=1
        mv -- "$STAGED_ARCHIVE_PUBLIC_KEY_PATH" "$ARCHIVE_PUBLIC_KEY_PATH"
        ARCHIVE_PUBLIC_KEY_PUBLISHED=1
    fi
fi
BUILD_COMMITTED=1

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
if [[ -e "$ARCHIVE_SIGNATURE_PATH" ]]; then
    echo "ArchiveSignature=$ARCHIVE_SIGNATURE_PATH"
    echo "ArchivePublicKey=$ARCHIVE_PUBLIC_KEY_PATH"
    echo "SigningKeySha256=$FINGERPRINT"
fi
