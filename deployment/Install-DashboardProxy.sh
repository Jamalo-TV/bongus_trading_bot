#!/usr/bin/env bash
set -Eeuo pipefail

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly TEMPLATE_PATH="$SCRIPT_DIR/nginx/bongus-dashboard.conf.in"
readonly SITE_AVAILABLE="/etc/nginx/sites-available/bongus-dashboard.conf"
readonly SITE_ENABLED="/etc/nginx/sites-enabled/bongus-dashboard.conf"
readonly MANAGED_CERT_DIR="/etc/ssl/bongus-dashboard"
readonly MANAGED_CERT_FILE="$MANAGED_CERT_DIR/dashboard.crt"
readonly MANAGED_KEY_FILE="$MANAGED_CERT_DIR/dashboard.key"
readonly BACKUP_ROOT="/var/backups/bongus-dashboard-proxy"
readonly MIN_CERT_VALIDITY_SECONDS=2592000

PUBLIC_IP=""
SUPPLIED_CERT_FILE=""
SUPPLIED_KEY_FILE=""
CERT_DAYS=397
WORK_DIR=""
BACKUP_DIR=""
MUTATION_STARTED=false
DEPLOY_COMMITTED=false
NGINX_WAS_ACTIVE=false
SITE_AVAILABLE_EXISTED=false
SITE_ENABLED_EXISTED=false
CERT_FILE_EXISTED=false
KEY_FILE_EXISTED=false

log() {
    printf '[bongus-dashboard-proxy] %s\n' "$*"
}

die() {
    printf '[bongus-dashboard-proxy] ERROR: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage:
  sudo bash deployment/Install-DashboardProxy.sh --public-ip IPV4
  sudo bash deployment/Install-DashboardProxy.sh --public-ip IPV4 \
    --cert-file /secure/fullchain.pem --key-file /secure/private-key.pem

Options:
  --public-ip IPV4   Public IPv4 address clients use for the dashboard.
  --cert-file PATH   Existing PEM certificate containing that IPv4 SAN.
  --key-file PATH    Unencrypted PEM private key matching --cert-file.
  --cert-days DAYS   Self-signed certificate lifetime (default: 397).
  -h, --help         Show this help.

The script never installs packages or changes firewall rules. It keeps Uvicorn
on 127.0.0.1:8080 and exposes only nginx ports 80 and 443.
EOF
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is unavailable: $1"
}

# Fail closed for private, loopback, link-local, documentation, benchmark,
# multicast, and otherwise non-public IPv4 ranges. This deliberately rejects
# special-purpose ranges even when an individual address has a narrow exception.
validate_public_ipv4() {
    local ip="${1:-}"
    local a b c d extra octet
    local ai bi ci di

    IFS=. read -r a b c d extra <<<"$ip"
    [[ -n "$a" && -n "$b" && -n "$c" && -n "$d" && -z "${extra:-}" ]] || return 1
    for octet in "$a" "$b" "$c" "$d"; do
        [[ "$octet" =~ ^[0-9]{1,3}$ ]] || return 1
        ((10#$octet <= 255)) || return 1
    done

    ai=$((10#$a))
    bi=$((10#$b))
    ci=$((10#$c))
    di=$((10#$d))
    ((ai >= 1 && ai <= 223)) || return 1
    ((di >= 0 && di <= 255)) || return 1

    ((ai != 10 && ai != 127)) || return 1
    ! ((ai == 100 && bi >= 64 && bi <= 127)) || return 1
    ! ((ai == 169 && bi == 254)) || return 1
    ! ((ai == 172 && bi >= 16 && bi <= 31)) || return 1
    ! ((ai == 192 && bi == 0 && ci == 0)) || return 1
    ! ((ai == 192 && bi == 0 && ci == 2)) || return 1
    ! ((ai == 192 && bi == 88 && ci == 99)) || return 1
    ! ((ai == 192 && bi == 168)) || return 1
    ! ((ai == 198 && (bi == 18 || bi == 19))) || return 1
    ! ((ai == 198 && bi == 51 && ci == 100)) || return 1
    ! ((ai == 203 && bi == 0 && ci == 113)) || return 1
    return 0
}

parse_args() {
    while (($#)); do
        case "$1" in
            --public-ip)
                (($# >= 2)) || die "--public-ip requires a value"
                PUBLIC_IP="$2"
                shift 2
                ;;
            --cert-file)
                (($# >= 2)) || die "--cert-file requires a value"
                SUPPLIED_CERT_FILE="$2"
                shift 2
                ;;
            --key-file)
                (($# >= 2)) || die "--key-file requires a value"
                SUPPLIED_KEY_FILE="$2"
                shift 2
                ;;
            --cert-days)
                (($# >= 2)) || die "--cert-days requires a value"
                CERT_DAYS="$2"
                shift 2
                ;;
            -h|--help)
                usage
                exit 0
                ;;
            *)
                die "unknown argument: $1"
                ;;
        esac
    done
}

validate_inputs() {
    [[ "$(id -u)" == "0" ]] || die "run this script as root"
    [[ -n "$PUBLIC_IP" ]] || die "--public-ip is required"
    validate_public_ipv4 "$PUBLIC_IP" || die "--public-ip must be a public IPv4 address"
    [[ "$CERT_DAYS" =~ ^[0-9]+$ ]] || die "--cert-days must be an integer"
    ((CERT_DAYS >= 31 && CERT_DAYS <= 3970)) || die "--cert-days must be between 31 and 3970"
    if [[ -n "$SUPPLIED_CERT_FILE" || -n "$SUPPLIED_KEY_FILE" ]]; then
        [[ -n "$SUPPLIED_CERT_FILE" && -n "$SUPPLIED_KEY_FILE" ]] || \
            die "--cert-file and --key-file must be supplied together"
        [[ -r "$SUPPLIED_CERT_FILE" ]] || die "certificate is not readable: $SUPPLIED_CERT_FILE"
        [[ -r "$SUPPLIED_KEY_FILE" ]] || die "private key is not readable: $SUPPLIED_KEY_FILE"
    fi
    [[ -r "$TEMPLATE_PATH" ]] || die "nginx template is missing: $TEMPLATE_PATH"

    require_command nginx
    require_command openssl
    require_command install
    require_command mktemp
    require_command cmp
    require_command sed
    require_command grep
    require_command ss
}

assert_loopback_backend() {
    local listeners
    local state recvq sendq local_address peer_address
    listeners="$(ss -H -ltn)" || die "could not enumerate TCP listeners with ss"
    while read -r state recvq sendq local_address peer_address _; do
        [[ -n "${local_address:-}" ]] || continue
        case "$local_address" in
            127.0.0.1:8080|'[::1]:8080') ;;
            *:8080)
                die "port 8080 is listening outside loopback ($local_address); fix the Uvicorn bind first"
                ;;
        esac
    done <<<"$listeners"
}

certificate_valid_for_ip() {
    local cert_file="$1"
    local key_file="$2"
    local cert_public_key="$WORK_DIR/certificate-public-key.der"
    local private_public_key="$WORK_DIR/private-public-key.der"

    openssl x509 -in "$cert_file" -noout >/dev/null 2>&1 || return 1
    openssl pkey -in "$key_file" -passin pass: -noout >/dev/null 2>&1 || return 1
    openssl x509 -in "$cert_file" -checkend "$MIN_CERT_VALIDITY_SECONDS" -noout \
        >/dev/null 2>&1 || return 1
    # OpenSSL's hostname verifier tokenizes the SAN extension and compares the
    # binary IP value exactly. A text/substring search would let 1.1.1.1 match
    # an unrelated SAN such as 1.1.1.10.
    openssl x509 -in "$cert_file" -noout -checkip "$PUBLIC_IP" \
        >/dev/null 2>&1 || return 1
    openssl x509 -in "$cert_file" -pubkey -noout 2>/dev/null | \
        openssl pkey -pubin -outform DER >"$cert_public_key" 2>/dev/null || return 1
    openssl pkey -in "$key_file" -passin pass: -pubout -outform DER \
        >"$private_public_key" 2>/dev/null || return 1
    cmp -s "$cert_public_key" "$private_public_key"
}

generate_self_signed_certificate() {
    local cert_file="$1"
    local key_file="$2"

    log "Generating a self-signed TLS certificate with IP SAN $PUBLIC_IP"
    openssl req -x509 -newkey rsa:3072 -sha256 -nodes \
        -days "$CERT_DAYS" \
        -keyout "$key_file" \
        -out "$cert_file" \
        -subj "/CN=$PUBLIC_IP" \
        -addext "subjectAltName=IP:$PUBLIC_IP" \
        -addext "keyUsage=critical,digitalSignature,keyEncipherment" \
        -addext "extendedKeyUsage=serverAuth" \
        >/dev/null 2>&1
    chmod 0600 "$key_file"
    chmod 0644 "$cert_file"
}

stage_certificate() {
    local staged_cert="$WORK_DIR/dashboard.crt"
    local staged_key="$WORK_DIR/dashboard.key"

    if [[ -n "$SUPPLIED_CERT_FILE" ]]; then
        cp -- "$SUPPLIED_CERT_FILE" "$staged_cert"
        cp -- "$SUPPLIED_KEY_FILE" "$staged_key"
        certificate_valid_for_ip "$staged_cert" "$staged_key" || \
            die "supplied certificate/key must match, remain valid for 30 days, and contain IP SAN $PUBLIC_IP"
        log "Validated the supplied certificate and private key"
    elif [[ -r "$MANAGED_CERT_FILE" && -r "$MANAGED_KEY_FILE" ]]; then
        cp -- "$MANAGED_CERT_FILE" "$staged_cert"
        cp -- "$MANAGED_KEY_FILE" "$staged_key"
        if certificate_valid_for_ip "$staged_cert" "$staged_key"; then
            log "Reusing the existing valid managed certificate"
        else
            generate_self_signed_certificate "$staged_cert" "$staged_key"
        fi
    else
        generate_self_signed_certificate "$staged_cert" "$staged_key"
    fi

    certificate_valid_for_ip "$staged_cert" "$staged_key" || \
        die "staged certificate validation failed"
}

render_nginx_config() {
    sed \
        -e "s|@PUBLIC_IP@|$PUBLIC_IP|g" \
        -e "s|@CERT_FILE@|$MANAGED_CERT_FILE|g" \
        -e "s|@KEY_FILE@|$MANAGED_KEY_FILE|g" \
        "$TEMPLATE_PATH" >"$WORK_DIR/bongus-dashboard.conf"

    ! grep -Eq '@(PUBLIC_IP|CERT_FILE|KEY_FILE)@' "$WORK_DIR/bongus-dashboard.conf" || \
        die "nginx template still contains unresolved placeholders"
}

backup_target() {
    local source="$1"
    local label="$2"
    local existed_variable="$3"

    if [[ -e "$source" || -L "$source" ]]; then
        cp -a -- "$source" "$BACKUP_DIR/$label"
        printf -v "$existed_variable" '%s' true
    fi
}

write_rollback_notes() {
    cat >"$BACKUP_DIR/ROLLBACK.txt" <<EOF
Bongus dashboard proxy backup created before deployment.

Automatic rollback runs if nginx validation or reload fails. For a later manual
rollback, stop and inspect nginx first. Restore the corresponding files from:
  $BACKUP_DIR

Managed targets:
  $SITE_AVAILABLE (previously existed: $SITE_AVAILABLE_EXISTED)
  $SITE_ENABLED (previously existed: $SITE_ENABLED_EXISTED)
  $MANAGED_CERT_FILE (previously existed: $CERT_FILE_EXISTED)
  $MANAGED_KEY_FILE (previously existed: $KEY_FILE_EXISTED)

After restoring or removing newly introduced targets, run:
  nginx -t
  systemctl reload nginx
EOF
    chmod 0600 "$BACKUP_DIR/ROLLBACK.txt"
}

restore_target() {
    local destination="$1"
    local label="$2"
    local existed="$3"

    rm -f -- "$destination"
    if [[ "$existed" == true ]]; then
        cp -a -- "$BACKUP_DIR/$label" "$destination"
    fi
}

reload_nginx() {
    if command -v systemctl >/dev/null 2>&1; then
        if systemctl is-active --quiet nginx; then
            systemctl reload nginx
        else
            systemctl start nginx
        fi
    else
        nginx -s reload
    fi
}

rollback_changes() {
    [[ "$MUTATION_STARTED" == true && -n "$BACKUP_DIR" ]] || return 0
    log "Rolling back dashboard proxy files"

    restore_target "$SITE_ENABLED" site-enabled "$SITE_ENABLED_EXISTED"
    restore_target "$SITE_AVAILABLE" site-available "$SITE_AVAILABLE_EXISTED"
    restore_target "$MANAGED_CERT_FILE" certificate "$CERT_FILE_EXISTED"
    restore_target "$MANAGED_KEY_FILE" private-key "$KEY_FILE_EXISTED"

    if nginx -t >/dev/null 2>&1 && [[ "$NGINX_WAS_ACTIVE" == true ]]; then
        reload_nginx >/dev/null 2>&1 || true
    fi
}

cleanup() {
    local status=$?
    trap - EXIT INT TERM
    if ((status != 0)) && [[ "$DEPLOY_COMMITTED" != true ]]; then
        rollback_changes || true
    fi
    if [[ -n "$WORK_DIR" && -d "$WORK_DIR" ]]; then
        rm -rf -- "$WORK_DIR"
    fi
    exit "$status"
}

install_managed_file() {
    local source="$1"
    local destination="$2"
    local mode="$3"
    local temporary="${destination}.bongus-new.$$"

    install -o root -g root -m "$mode" "$source" "$temporary"
    mv -f -- "$temporary" "$destination"
}

install_proxy() {
    local nginx_dump

    install -d -o root -g root -m 0755 "$(dirname -- "$SITE_AVAILABLE")"
    install -d -o root -g root -m 0755 "$(dirname -- "$SITE_ENABLED")"
    install -d -o root -g root -m 0755 "$MANAGED_CERT_DIR"
    install -d -o root -g root -m 0700 "$BACKUP_ROOT"

    BACKUP_DIR="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    install -d -o root -g root -m 0700 "$BACKUP_DIR"
    backup_target "$SITE_AVAILABLE" site-available SITE_AVAILABLE_EXISTED
    backup_target "$SITE_ENABLED" site-enabled SITE_ENABLED_EXISTED
    backup_target "$MANAGED_CERT_FILE" certificate CERT_FILE_EXISTED
    backup_target "$MANAGED_KEY_FILE" private-key KEY_FILE_EXISTED
    write_rollback_notes

    if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet nginx; then
        NGINX_WAS_ACTIVE=true
    fi

    MUTATION_STARTED=true
    install_managed_file "$WORK_DIR/dashboard.crt" "$MANAGED_CERT_FILE" 0644
    install_managed_file "$WORK_DIR/dashboard.key" "$MANAGED_KEY_FILE" 0600
    install_managed_file "$WORK_DIR/bongus-dashboard.conf" "$SITE_AVAILABLE" 0644
    rm -f -- "$SITE_ENABLED"
    ln -s "$SITE_AVAILABLE" "$SITE_ENABLED"

    nginx -t
    nginx_dump="$(nginx -T 2>&1)"
    grep -Fq 'bongus-dashboard-proxy-managed' <<<"$nginx_dump" || \
        die "nginx does not include $SITE_ENABLED; deployment was rolled back"
    reload_nginx
    DEPLOY_COMMITTED=true
}

print_result() {
    local fingerprint
    fingerprint="$(openssl x509 -in "$MANAGED_CERT_FILE" -noout -fingerprint -sha256)"
    log "Dashboard proxy is configured at https://$PUBLIC_IP/"
    log "Certificate $fingerprint"
    log "Verify this fingerprint out of band before trusting the self-signed certificate."
    log "Uvicorn remains private at http://127.0.0.1:8080."
    log "Backup: $BACKUP_DIR"
    log "Firewall rules were not changed. See deployment/README.md before opening ports."
}

main() {
    parse_args "$@"
    validate_inputs
    assert_loopback_backend
    WORK_DIR="$(mktemp -d)"
    chmod 0700 "$WORK_DIR"
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    stage_certificate
    render_nginx_config
    install_proxy
    print_result
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
