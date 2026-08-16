#!/usr/bin/env bash
set -euo pipefail

usage() {
    echo "Usage: Verify-BongusArchive.sh ARCHIVE SIGNATURE PUBLIC_KEY EXPECTED_KEY_SHA256" >&2
}

if (($# != 4)); then
    usage
    exit 2
fi

ARCHIVE="$1"
SIGNATURE="$2"
PUBLIC_KEY="$3"
EXPECTED_KEY_SHA256="${4,,}"

command -v openssl >/dev/null || { echo "openssl is required." >&2; exit 2; }
command -v sha256sum >/dev/null || { echo "sha256sum is required." >&2; exit 2; }
command -v realpath >/dev/null || { echo "realpath is required." >&2; exit 2; }
[[ "$EXPECTED_KEY_SHA256" =~ ^[0-9a-f]{64}$ ]] || {
    echo "Expected release signing-key fingerprint is malformed." >&2
    exit 2
}

# Check the caller-supplied directory entries before canonicalization.  Once
# realpath follows a symlink, testing the resolved path cannot tell that the
# operator actually supplied a linked archive, signature, or public key.
for artifact in "$ARCHIVE" "$SIGNATURE" "$PUBLIC_KEY"; do
    [[ -f "$artifact" && ! -L "$artifact" ]] || {
        echo "Release verification input is missing, linked, or non-regular: $artifact" >&2
        exit 2
    }
done

ARCHIVE="$(realpath -e -- "$ARCHIVE")"
SIGNATURE="$(realpath -e -- "$SIGNATURE")"
PUBLIC_KEY="$(realpath -e -- "$PUBLIC_KEY")"
for artifact in "$ARCHIVE" "$SIGNATURE" "$PUBLIC_KEY"; do
    [[ -f "$artifact" && ! -L "$artifact" ]] || {
        echo "Release verification input is missing, linked, or non-regular: $artifact" >&2
        exit 2
    }
done
[[ "$ARCHIVE" != "$SIGNATURE" && "$ARCHIVE" != "$PUBLIC_KEY" && "$SIGNATURE" != "$PUBLIC_KEY" ]] || {
    echo "Release verification inputs must be distinct files." >&2
    exit 2
}

OBSERVED_KEY_SHA256="$(
    openssl pkey -pubin -in "$PUBLIC_KEY" -outform DER \
        | sha256sum \
        | awk '{print $1}'
)"
[[ "$OBSERVED_KEY_SHA256" == "$EXPECTED_KEY_SHA256" ]] || {
    echo "Release public key does not match the operator trust pin." >&2
    exit 2
}
openssl dgst -sha256 -verify "$PUBLIC_KEY" \
    -signature "$SIGNATURE" "$ARCHIVE" >/dev/null

printf 'VerifiedArchive=%s\nSigningKeySha256=%s\n' "$ARCHIVE" "$OBSERVED_KEY_SHA256"
