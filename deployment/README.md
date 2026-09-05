# Windows and Linux deployment

Bongus has separate native release artifacts for Windows and Linux. Python
application code is shared, but the Rust execution engine and Python wheels
must be built for the server operating system and CPU architecture. Never copy
`execution_engine.exe` to Linux or a Linux ELF binary to Windows.

The Linux systemd cgroup is the authoritative production resource boundary.
The supplied unit provisionally enforces `MemoryHigh=3,000,000,000`, a hard
`MemoryMax=3,500,000,000`, and no swap on a 4 GB trading VPS. Accept that host
size only after a seven-day soak proves total RSS p99 below 70%, CPU p95 below
60%, no OOM or restart, stable SQLite write latency, and more than 30% free
disk. Move to an 8 GB host if any gate fails; do not relax the service limit to
hide a failed soak. The build manifest's broader packaging ceiling is not
permission to raise the production cgroup limit.

Run research collection on a separate host whenever trading is active. Never
run active-active traders or automatic failover against the same account.

## Build on the target operating system

Windows development packages are built on a controlled Windows build host:

```powershell
.\scripts\build_release.ps1 -OutputPath .\dist\bongus-release-windows
```

Windows packages are currently development-only. Authenticode on the Rust
binary does not authenticate the Python, installer, and unit files, so the
builder refuses to mark a Windows archive production eligible until a
whole-manifest trust-pinned verifier is implemented. Production deployment is
Linux-only.

Linux packages must be built on Linux with the same architecture as the
server (`x86_64` or `arm64`). First materialize a development wheelhouse, then
generate and separately review its deterministic exact-filename/SHA-256 lock:

```bash
python3.11 -m pip wheel --no-deps --wheel-dir dist/runtime-wheels \
  --requirement requirements-runtime.txt
bash scripts/build_release.sh \
  --output dist/wheel-review-linux-x86_64 \
  --wheelhouse dist/runtime-wheels \
  --allow-unsigned-development --no-archive
python3.11 scripts/release_manifest.py lock-wheelhouse \
  requirements-runtime.txt dist/wheel-review-linux-x86_64/wheelhouse \
  /secure/reviewed/bongus-linux-x86_64-wheelhouse.lock.json
```

The development wheel step also builds the pinned source-only `sgmllib3k`
dependency. It runs on the controlled build host, never at trading startup.
An all-binary network download alone cannot materialize that dependency.

Review the lock and every referenced wheel on a separate review step, then
make that wheel directory and lock read-only. The production build consumes
those reviewed bytes without invoking pip or the network:

```bash
bash scripts/build_release.sh \
  --output dist/bongus-release-linux-x86_64 \
  --wheelhouse /secure/reviewed/linux-x86_64-wheelhouse \
  --approved-wheelhouse-lock /secure/reviewed/bongus-linux-x86_64-wheelhouse.lock.json \
  --signing-key /secure/build-only/bongus-release-private.pem \
  --signer-subject 'ops@example.com'
```

The private key stays on the build host. The builder signs the Rust binary,
the complete file-inventory manifest, and the final ZIP. It emits the ZIP's
detached signature and public key next to the archive. Record the printed
public-key SHA-256 fingerprint in a separate operator password manager; the
Linux installer and every live Rust restart require that out-of-band trust
pin.

`requirements-runtime.txt` pins versions, while `wheelhouse.lock.json` binds
the exact approved filenames, the requirements-file hash, and every wheel's
SHA-256. A production manifest is impossible without a matching reviewed
lock. Network-materialized development wheels remain non-production; a signed
wheelhouse is rejected unless the explicit reviewed lock matches byte for
byte.

The local Ultraplan acceptance platform is Ubuntu 24.04 x86_64. Build on the
intended distribution when its architecture, glibc or OpenSSL compatibility
differs; the local ELF artifact is not a promise of compatibility with every
Linux distribution.

For paper/testnet validation only, a dirty or unsigned package can be made
explicitly:

```bash
bash scripts/build_release.sh --allow-dirty-source \
  --allow-unsigned-development --without-wheelhouse --no-archive
```

Such a package is marked `production_eligible=false`. It cannot run in live
mode. For a deployable paper/testnet package, replace `--without-wheelhouse`
with `--wheelhouse dist/runtime-wheels`, prepared by the wheel step above.

Builders never replace an existing output and exclude `.env`, Git metadata,
tests, databases, logs, Cargo output, caches, compiler toolchains, and research
data. Startup never runs Cargo, rustc, a package manager, or a networked pip
install.

## Install on a Linux server

Provision a separately reviewed CPython 3.11.15 source build (including
`venv`) at `/usr/local/bin/python3.11`; a distribution's older generic 3.11
package does not satisfy the reviewed patch floor. Later final 3.11 security
patches are compatible, but 3.11.14 or older, another minor series, and
pre-release interpreters are rejected. Install OpenSSL and the basic runtime
tools separately. Authenticate the archive *before extraction* using the
public-key fingerprint from the separate operator record. A ZIP-provided
SHA-256 sidecar detects transfer errors but is not an authenticity check and
is not sufficient by itself.

```bash
set -euo pipefail
ARCHIVE=bongus-release-linux-x86_64.zip
PUBLIC_KEY="${ARCHIVE}.public.pem"
SIGNATURE="${ARCHIVE}.sig"
EXPECTED_KEY_SHA256='<fingerprint from the build operator>'
TRUSTED_CHECKOUT=/srv/reviewed-bongus-source
bash "$TRUSTED_CHECKOUT/deployment/Verify-BongusArchive.sh" \
  "$ARCHIVE" "$SIGNATURE" "$PUBLIC_KEY" "$EXPECTED_KEY_SHA256"
sha256sum -c "${ARCHIVE}.sha256"

sudo mkdir -p /opt/bongus/releases/2026-08-09
sudo unzip "$ARCHIVE" -d /opt/bongus/releases/2026-08-09
cd /opt/bongus/releases/2026-08-09
```

Create the unprivileged account before installation if needed:

```bash
sudo groupadd --system bongus
sudo useradd --system --home /var/lib/bongus --gid bongus \
  --shell /usr/sbin/nologin bongus
sudo groupadd --system bongus-backup
sudo useradd --system --no-create-home --home /nonexistent \
  --gid bongus-backup --groups bongus --shell /usr/sbin/nologin bongus-backup
sudo groupadd --system bongus-offsite
sudo useradd --system --no-create-home --home /nonexistent \
  --gid bongus-offsite --groups bongus,bongus-backup \
  --shell /usr/sbin/nologin bongus-offsite
sudo groupadd --system bongus-maintenance
sudo useradd --system --no-create-home --home /nonexistent \
  --gid bongus-maintenance --groups bongus-backup \
  --shell /usr/sbin/nologin bongus-maintenance
sudo bash Install-BongusRelease.sh \
  --python /usr/local/bin/python3.11 \
  --trusted-key-sha256 "$EXPECTED_KEY_SHA256" \
  --install-systemd --service-user bongus --data-root /var/lib/bongus
```

The installer verifies every packaged byte and signature, creates `.venv`
using copies rather than escaping symlinks, installs only from the included
wheelhouse with `--no-index --no-cache-dir --only-binary=:all:`, checks the
600 MB Python-runtime budget, renders `/etc/systemd/system/bongus.service`, and
enables it. It also seeds `/var/lib/bongus/live_config.json` on first install.
The current installer is intentionally new-install-only; follow the documented
stop, verify, and rollback procedure before replacing an installed unit. The signed
release-root `live_config.json` remains an immutable seed, so hot config changes
do not invalidate the release inventory. The release tree is root-owned and
read-only to the service; logs, locks, heartbeats, journals, and databases live
under the service-owned data root. It also installs and enables, but does not
start, a one-minute read-only operational health timer. The timer validates
backup age, chrony synchronization/offset, and the independent runtime
heartbeat; it never sends notifications. A separate enabled-but-not-started
timer publishes one coherent, hash-bound split-store generation every 10
minutes, reserving a five-minute copy/upload target inside the 15-minute RPO.
That target remains unproven until the real 5.13 GB database pipeline passes a
Linux live-WAL timing gate; unit tests do not establish it.
The data filesystem must provide at least 60 GB total capacity and 28 GB free
before installation: 20 GB must remain after the first backup while up to 8 GB
is staged for that operation.
The operator must still configure encrypted offsite transfer and an independent
monitor that pages on either timer/service failure.

For an unsigned testnet artifact, replace the trust-pin argument with
`--allow-development-package`. The watchdog still refuses `TRADING_MODE=live`.

### Configure encrypted offsite backups

Install a reviewed Restic build at `/usr/bin/restic`, create a remote encrypted
repository, and provision its credentials before expecting the health timer to
pass. Local filesystem repositories are rejected because they do not satisfy
the offsite requirement. One example for an S3-compatible destination is:

```bash
sudo install -d -m 0750 -o root -g bongus /etc/bongus
sudo install -m 0640 -o root -g bongus-offsite /secure/restic-password \
  /etc/bongus/restic-password
sudo install -m 0640 -o root -g bongus-offsite /dev/stdin \
  /etc/bongus/offsite-backup.env <<'EOF'
RESTIC_REPOSITORY=s3:https://s3.example.invalid/bongus-production
RESTIC_PASSWORD_FILE=/etc/bongus/restic-password
BONGUS_EXPECTED_RESTIC_REPOSITORY_ID=replace-with-reviewed-restic-config-id
BONGUS_EXPECTED_RESTIC_BINARY_SHA256=replace-with-sha256-of-/usr/bin/restic
BONGUS_EXPECTED_RESTIC_VERSION=replace-with-exact-x.y.z
AWS_ACCESS_KEY_ID=replace-me
AWS_SECRET_ACCESS_KEY=replace-me
EOF
# The frequent writer credential above must be append-only. Retention uses a
# different identity and a separately provisioned delete-capable credential.
sudo install -m 0640 -o root -g bongus-maintenance /secure/restic-password \
  /etc/bongus/restic-maintenance-password
sudo install -m 0640 -o root -g bongus-maintenance /dev/stdin \
  /etc/bongus/offsite-maintenance.env <<'EOF'
RESTIC_REPOSITORY=s3:https://s3.example.invalid/bongus-production
RESTIC_PASSWORD_FILE=/etc/bongus/restic-maintenance-password
BONGUS_EXPECTED_RESTIC_REPOSITORY_ID=replace-with-reviewed-restic-config-id
BONGUS_EXPECTED_RESTIC_BINARY_SHA256=replace-with-sha256-of-/usr/bin/restic
BONGUS_EXPECTED_RESTIC_VERSION=replace-with-exact-x.y.z
AWS_ACCESS_KEY_ID=replace-with-separate-maintenance-key
AWS_SECRET_ACCESS_KEY=replace-with-separate-maintenance-secret
EOF
# Initialize that exact repository once using your provider-approved secret
# injection procedure. Obtain its 64-hex ID with `restic cat config`, verify it
# out of band, and pin it above before starting the units below.
# Record `sha256sum /usr/bin/restic` and the exact `x.y.z` semantic version
# reported by `/usr/bin/restic version`; both identities are rechecked before any upload
# or delete-capable maintenance command and are written into the health receipt.
sudo systemctl start bongus-backup.service
sudo systemctl status bongus-offsite-backup.service --no-pager
sudo systemctl start bongus-offsite-maintenance.service
sudo test -s /var/lib/bongus/offsite/upload/latest.json
sudo test -s /var/lib/bongus/offsite/maintenance/latest.json
```

`Verify-BongusArchive.sh` must come from the separately reviewed source
checkout, not from inside the unverified ZIP or an adjacent download. It is
root-independent and exits before extraction on a wrong pin, invalid
signature, link, or missing file. Keep the verified inputs in an
operator-owned non-shared directory until extraction finishes.

Use your provider's secret-injection mechanism instead of the illustrative
shell expansion when available; never put these values in the release tree or
Git. Each successful verified local backup triggers the static offsite unit
under a protected namespace the trader cannot modify. It uploads the complete
hash-bound database set and recovery configuration without a shell, then
atomically advances the receipt. Every set invokes the local Rust barrier and
binds its immutable six-member journal/cursor generation; copying the live
mutable Rust directory is forbidden. Configure a separate monitor
to page on `bongus-ops-health.service` failure. The repository cannot supply
your storage account, network policy, or paging destination.

The upload credential must be append-only. Daily retention runs under the
distinct `bongus-maintenance` identity, uses a separately reviewed
delete-capable credential, groups changing backup paths by the stable
`bongus-operational` tag, and is killed after four minutes so it cannot hold the
repository lock across the next recovery window. Health requires a fresh
retention receipt for the same pinned repository. Provider quota, object-lock,
and actual prune-duration evidence remain operator responsibilities.

### Continue an existing testnet account safely

Skip this section only for a genuinely fresh exchange account and empty data
root. To move an existing bot to Linux, stop both the source bot and the Linux
service before copying any state. Stage and transfer the authoritative data-root
files as one offline set; never concatenate an old and new Rust journal.

Copy these items into `/var/lib/bongus` before the first Linux start:

- `state.db*`, `audit.db*`, `research.db*`, and `migration-manifest.json`
- `live_config.json` (the mutable runtime copy, not the signed release seed)
- `.watchdog_state.json`
- `runtime/storage_health.json` and `runtime/emergency-storage.reserve`
- the complete `runtime/rust/` tree, including private-stream cursors and
  `storage_control.json`

Do not copy `.watchdog.lock` or a stale `runtime_heartbeat.json`. Do not copy a
SQLite database without its matching WAL/SHM files while the source process is
running. After transfer, reject symlinks and restore service ownership:

```bash
sudo test -z "$(find /var/lib/bongus -type l -print -quit)"
sudo chown root:bongus /var/lib/bongus && sudo chmod 1770 /var/lib/bongus
sudo chown bongus:bongus /var/lib/bongus/live_config.json \
  /var/lib/bongus/migration-manifest.json /var/lib/bongus/.watchdog_state.json \
  /var/lib/bongus/*.db*
sudo chmod 0640 /var/lib/bongus/live_config.json \
  /var/lib/bongus/migration-manifest.json /var/lib/bongus/.watchdog_state.json \
  /var/lib/bongus/*.db*
sudo chown -R bongus:bongus /var/lib/bongus/runtime
sudo chown bongus-backup:bongus /var/lib/bongus/backups && sudo chmod 2750 /var/lib/bongus/backups
sudo chown root:bongus-backup /var/lib/bongus/offsite && sudo chmod 1770 /var/lib/bongus/offsite
sudo chown bongus-offsite:bongus-backup /var/lib/bongus/offsite/upload && sudo chmod 2750 /var/lib/bongus/offsite/upload
sudo chown bongus-maintenance:bongus-backup /var/lib/bongus/offsite/maintenance && sudo chmod 2750 /var/lib/bongus/offsite/maintenance
sudo chown root:bongus-backup /var/lib/bongus/offsite/locks && sudo chmod 2770 /var/lib/bongus/offsite/locks
sudo test -f /var/lib/bongus/runtime/storage_health.json
sudo test -f /var/lib/bongus/runtime/rust/storage_control.json
sudo test -s /var/lib/bongus/runtime/emergency-storage.reserve
```

The inherited storage snapshot and Rust control journal intentionally remain
authoritative after the move. If they contain a risk latch, allow the restarted
bot to collect fresh recovery proof and use the authenticated recovery workflow;
do not delete the files to bypass it.

## Configure and run for the soak period

Create `/etc/bongus/trader.env` owned by `root:root` with mode `0600`. The
systemd manager reads it before dropping privileges; backup/offsite identities
cannot read exchange credentials. Start with credential-free paper mode, never live:

```dotenv
TRADING_MODE=paper
```

Install that exact content without exposing it to the service or recovery
groups:

```bash
sudo install -d -m 0750 -o root -g bongus /etc/bongus
sudo install -m 0600 -o root -g root /dev/stdin /etc/bongus/trader.env <<'EOF'
TRADING_MODE=paper
EOF
```

Keep `pause_new_entries=true` in `/var/lib/bongus/live_config.json` for the
initial health/restart observation. Dedicated Binance testnet credentials may
be added only for the separately authorized signed testnet campaign; disable
withdrawals and restrict the key by IP before doing so.

Systemd loads this file before starting the watchdog. The unit's
manifest-bound `BONGUS_DATA_ROOT` remains authoritative, and the watchdog
passes the resulting environment to every child.

Apply runtime overrides to `/var/lib/bongus/live_config.json`, not the signed
copy under `/opt`. Set `autonomous_startup_recovery` there only for a paper or
testnet soak; the watchdog ignores it in live mode.

Then start and observe it:

```bash
sudo systemctl start bongus
sudo systemctl start bongus-backup.timer bongus-ops-health.timer
sudo systemctl status bongus --no-pager
sudo journalctl -u bongus -f
```

The systemd service and internal watchdog keep it running across crashes and
server reboots. Useful checks during a multi-day soak:

```bash
systemctl show bongus -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax
systemctl list-timers bongus-ops-health.timer --no-pager
systemctl list-timers bongus-backup.timer --no-pager
systemctl status bongus-ops-health.service --no-pager
systemctl status bongus-backup.service --no-pager
sudo journalctl -u bongus --since '24 hours ago' --priority=warning
df -h /var/lib/bongus
```

Stop cleanly with `sudo systemctl stop bongus`. Do not use real API keys or
live mode until the external Gate B/C/D runtime evidence and operator approval
requirements have independently passed.

## Secure dashboard access by public IPv4

Keep Uvicorn strictly on `127.0.0.1:8080`. Basic authentication protects the
application route, but sending it over plain HTTP would expose the credentials.
The repository includes an idempotent root-run nginx deployment asset that
terminates TLS on 443, redirects port 80 to HTTPS, proxies HTTP and WebSocket
traffic to loopback, validates nginx before reload, and automatically restores
the prior files if validation or reload fails.

Install nginx and OpenSSL explicitly with your operating-system package manager;
the script never installs packages or changes firewall state. From a reviewed
repository checkout, run:

```bash
sudo bash deployment/Install-DashboardProxy.sh \
  --public-ip 66.42.45.59
```

By default, the script creates a root-owned self-signed certificate containing
the public address as an IPv4 Subject Alternative Name. A browser trust warning
is expected until that exact certificate is imported into the operator device's
trust store. Compare the SHA-256 fingerprint printed by the script over a second
trusted channel before importing or accepting it. A real DNS name with an ACME
certificate is strongly preferred for normal browser use. The self-signed
profile deliberately omits HSTS; enable HSTS only after installing a publicly
trusted certificate.

To use an existing unencrypted private key and certificate instead, the
certificate must match the key, remain valid for at least 30 days, and contain
the requested IP SAN:

```bash
sudo bash deployment/Install-DashboardProxy.sh \
  --public-ip 66.42.45.59 \
  --cert-file /secure/dashboard-fullchain.pem \
  --key-file /secure/dashboard-private-key.pem
```

The managed certificate and key are written under
`/etc/ssl/bongus-dashboard/`. Prior nginx and certificate files are copied to a
timestamped root-only directory under `/var/backups/bongus-dashboard-proxy/`.
The script prints that directory and leaves `ROLLBACK.txt` there. Rerunning the
same command reuses a matching managed certificate that has at least 30 days of
validity remaining.

Verify that only nginx is public and that Uvicorn remains private:

```bash
sudo nginx -t
sudo ss -lntp | grep -E ':80|:443|:8080'
curl --insecure --silent --output /dev/null \
  --write-out 'HTTP %{http_code}\n' https://66.42.45.59/
```

The expected dashboard response without credentials is `401 Unauthorized`.
There must be no `0.0.0.0:8080` or `[::]:8080` listener.

Only after nginx validation succeeds, update UFW. These broad rules match a
public dashboard deployment:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw delete allow 8080/tcp
sudo ufw status verbose
```

Restrict 80/443 to the operator's fixed source IP instead when practical. The
installer intentionally never runs `ufw`, `iptables`, or `nft`; firewall policy
remains a separate, auditable operator action. If public access is unnecessary,
the safest option remains an SSH tunnel with Uvicorn and port 8080 on loopback.

## Git-first paper deployment

The automated operational acceptance run is available from a source checkout
or an installed release with its native Rust executable and Python dependencies:

```bash
python3.11 scripts/run_paper_soak.py \
  --duration-seconds 1800 --output "$HOME/bongus-paper-acceptance-001"
```

Use the release's `.venv/bin/python` when running from an installed package.
Use a parent directory writable by the executing account; `/var/lib` itself
normally requires root. The example above is a standalone source-checkout run.
The output directory must be new and writable. Ports 5555, 9000 and 8080 must
be free. The runner starts the real watchdog, Rust engine, trader, dashboard,
supervisor and alerter. Its child environment contains no inherited exchange,
Telegram or AI credentials and explicitly disables both Python and Rust dotenv
loading. It copies a separate paper configuration with entries enabled, so
normal strategy decisions run without changing the paused release seed.

The 30-minute clock starts only after runtime readiness, current public funding,
IPC connectivity and every required processing loop have been observed. A
process/session change, stale processing loop or observer gap fails the run.
The runner then stops its own runtime and checks all three SQLite databases.
`paper-soak-report.json` records the exact source and binary hashes, duration,
shutdown result and integrity checks; the companion JSONL contains raw samples.
An economically unsuitable market may correctly produce zero paper orders.
Use the deterministic order/fault tests to validate those execution paths.
This operational report never approves real-money trading or proves PnL.

For several days of unattended operation use the systemd installation below,
the health/backup timers and daily reconciliation. A 30-minute acceptance run
does not replace the separate 30-day operational or 90-day economic gates.

If Git is how you transfer the code, push the reviewed changes first and clone
that exact clean commit on the Linux server. Build a native, offline-installable
paper package on that server, then install it. This remains a development
artifact (`production_eligible=false`) and cannot start in live mode:

```bash
sudo apt-get update
sudo apt-get install -y git curl build-essential pkg-config libssl-dev \
  unzip openssl chrony
# Provision the reviewed CPython 3.11.15 source build separately, then prove it:
/usr/local/bin/python3.11 -c \
  'import sys, sqlite3, ssl, venv, ensurepip; assert sys.version_info[:3] >= (3, 11, 15) and sys.version_info[:2] == (3, 11) and sys.version_info.releaselevel == "final"'
/usr/local/bin/python3.11 -m pip --version
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
  sh -s -- -y --profile minimal --default-toolchain 1.94.1
source "$HOME/.cargo/env"

git clone <your-repository-url> bongus_trading_bot
cd bongus_trading_bot
git checkout <the-reviewed-commit>
# sgmllib3k ships as source; build its exact pinned wheel on the build host.
# Production wheelhouses still require the separate review/SHA-256 approval.
python3.11 -m pip wheel --no-deps --wheel-dir dist/runtime-wheels \
  --requirement requirements-runtime.txt
bash scripts/build_release.sh \
  --output "$PWD/dist/bongus-paper-linux" \
  --wheelhouse "$PWD/dist/runtime-wheels" \
  --allow-unsigned-development --no-archive

sudo groupadd --system bongus || true
sudo useradd --system --home /var/lib/bongus --gid bongus \
  --shell /usr/sbin/nologin bongus || true
sudo groupadd --system bongus-backup || true
sudo useradd --system --no-create-home --home /nonexistent \
  --gid bongus-backup --groups bongus --shell /usr/sbin/nologin bongus-backup || true
sudo groupadd --system bongus-offsite || true
sudo useradd --system --no-create-home --home /nonexistent \
  --gid bongus-offsite --groups bongus,bongus-backup \
  --shell /usr/sbin/nologin bongus-offsite || true
sudo groupadd --system bongus-maintenance || true
sudo useradd --system --no-create-home --home /nonexistent \
  --gid bongus-maintenance --groups bongus-backup \
  --shell /usr/sbin/nologin bongus-maintenance || true
sudo mkdir -p /opt/bongus/releases
sudo cp -a "$PWD/dist/bongus-paper-linux" /opt/bongus/releases/paper-soak
cd /opt/bongus/releases/paper-soak
sudo bash Install-BongusRelease.sh --python "$(command -v python3.11)" \
  --allow-development-package --install-systemd \
  --service-user bongus --data-root /var/lib/bongus
```

Then create the root-owned mode-`0600` `/etc/bongus/trader.env` exactly as shown
in the paper-soak section above, with only `TRADING_MODE=paper`, keep
`pause_new_entries=true`, and use the same `systemctl start/status` and
`journalctl` commands. The build step uses the network; the installed release
and multi-day runtime do not invoke a compiler or download dependencies.
Testnet credentials and `TRADING_MODE=testnet` are a later, separately
authorized campaign; they are not required for the first unattended soak.

## Windows development/test install

Windows direct execution is not an authoritative production path. Production
uses the reviewed Linux systemd units above; use this profile only for local
paper/testnet work.

Verify the ZIP digest, extract into a new directory, and run:

```powershell
.\Install-BongusRelease.ps1 -AllowDevelopmentPackage
.\.venv\Scripts\python.exe -m bongus.monitoring.king_watchdog
```

Both installers accept only a final CPython release in the manifest-pinned
major/minor series. The host patch may equal or exceed the manifest baseline;
patch downgrades, another series, and alpha/beta/RC interpreters are rejected.
The Windows installer also requires an offline wheelhouse, the canonical
`.venv`, the 600 MB runtime budget, and 20 GB post-install free-space headroom.
This remains a development/test path even if the Rust executable separately
carries Authenticode.

## Live approval artifact

Live mode also requires the existing schema-v3 short-lived operator approval.
It binds the effective configuration hash, installed release-manifest hash,
native Rust binary hash, signed exchange account UID, Gate-D decision artifact,
operator identity, nonce, and expiry. Keep both the approval HMAC key and the
Linux signing-key fingerprint outside the release and runtime volume.
