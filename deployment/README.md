# Windows and Linux deployment

Bongus has separate native release artifacts for Windows and Linux. Python
application code is shared, but the Rust execution engine and Python wheels
must be built for the server operating system and CPU architecture. Never copy
`execution_engine.exe` to Linux or a Linux ELF binary to Windows.

Both release manifests declare a 20,000,000,000-byte total runtime memory
ceiling. The watchdog limits each child process to 1 GB. On Linux, the supplied
systemd unit additionally enforces `MemoryHigh=16 GB`, a hard
`MemoryMax=20,000,000,000`, and disables swap for the service cgroup. The
application storage guard retains its stricter 16 GB aggregate data-volume
budget plus required free-space headroom.

## Build on the target operating system

Windows packages are built on a controlled Windows build host:

```powershell
.\scripts\build_release.ps1 -OutputPath .\dist\bongus-release-windows
```

Production Windows builds require a clean source tree, a complete offline
wheelhouse, and a valid Authenticode-signed Rust executable.

Linux packages must be built on Linux with the same architecture as the
server (`x86_64` or `arm64`):

```bash
bash scripts/build_release.sh \
  --output dist/bongus-release-linux-x86_64 \
  --signing-key /secure/build-only/bongus-release-private.pem \
  --signer-subject 'ops@example.com'
```

The private key stays on the build host. The package contains only its public
key and a detached SHA-256 signature. Record the printed public-key SHA-256
fingerprint in a separate operator password manager; the Linux installer and
every live Rust restart require that out-of-band trust pin.

For paper/testnet validation only, a dirty or unsigned package can be made
explicitly:

```bash
bash scripts/build_release.sh --allow-dirty-source \
  --allow-unsigned-development --without-wheelhouse --no-archive
```

Such a package is marked `production_eligible=false`. It cannot run in live
mode. A deployable testnet package should retain the wheelhouse (omit only
`--without-wheelhouse`).

Builders never replace an existing output and exclude `.env`, Git metadata,
tests, databases, logs, Cargo output, caches, compiler toolchains, and research
data. Startup never runs Cargo, rustc, a package manager, or a networked pip
install.

## Install on a Linux server

Install Python 3.11 and basic runtime tools first. Extract the ZIP into a new
versioned directory under `/opt`; do not extract over a running version.

```bash
sha256sum -c bongus-release-linux-x86_64.zip.sha256
sudo mkdir -p /opt/bongus/releases/2026-08-09
sudo unzip bongus-release-linux-x86_64.zip -d /opt/bongus/releases/2026-08-09
cd /opt/bongus/releases/2026-08-09
sudo bash Install-BongusRelease.sh \
  --python /usr/local/bin/python3.11 \
  --trusted-key-sha256 '<fingerprint from the build operator>' \
  --install-systemd --service-user bongus --data-root /var/lib/bongus
```

Create the unprivileged account before installation if needed:

```bash
sudo useradd --system --home /var/lib/bongus --shell /usr/sbin/nologin bongus
```

The installer verifies every packaged byte and signature, creates `.venv`
using copies rather than escaping symlinks, installs only from the included
wheelhouse with `--no-index --no-cache-dir --only-binary=:all:`, checks the
600 MB Python-runtime budget, renders `/etc/systemd/system/bongus.service`, and
enables it. It also seeds `/var/lib/bongus/live_config.json` on first install
and preserves that mutable operator config on upgrades. The signed
release-root `live_config.json` remains an immutable seed, so hot config changes
do not invalidate the release inventory. The release tree is root-owned and
read-only to the service; logs, locks, heartbeats, journals, and databases live
under the service-owned data root. It deliberately does not start the bot.

For an unsigned testnet artifact, replace the trust-pin argument with
`--allow-development-package`. The watchdog still refuses `TRADING_MODE=live`.

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
sudo chown -R bongus:bongus /var/lib/bongus
sudo test -f /var/lib/bongus/runtime/storage_health.json
sudo test -f /var/lib/bongus/runtime/rust/storage_control.json
sudo test -s /var/lib/bongus/runtime/emergency-storage.reserve
```

The inherited storage snapshot and Rust control journal intentionally remain
authoritative after the move. If they contain a risk latch, allow the restarted
bot to collect fresh recovery proof and use the authenticated recovery workflow;
do not delete the files to bypass it.

## Configure and run for the soak period

Create `/opt/bongus/releases/2026-08-09/.env` owned by `root:bongus` with mode
`0640`. Start with
paper or Binance testnet, never live:

```dotenv
TRADING_MODE=testnet
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BONGUS_RELEASE_SIGNING_KEY_SHA256=<required for a production-signed live release>
```

The watchdog loads this file without overriding the unit's manifest-bound
`BONGUS_DATA_ROOT`, then passes the resulting environment to every child.

Apply runtime overrides to `/var/lib/bongus/live_config.json`, not the signed
copy under `/opt`. Set `autonomous_startup_recovery` there only for a paper or
testnet soak; the watchdog ignores it in live mode.

Then start and observe it:

```bash
sudo chmod 640 /opt/bongus/releases/2026-08-09/.env
sudo chown root:bongus /opt/bongus/releases/2026-08-09/.env
sudo systemctl start bongus
sudo systemctl status bongus --no-pager
sudo journalctl -u bongus -f
```

The systemd service and internal watchdog keep it running across crashes and
server reboots. Useful checks during a multi-day soak:

```bash
systemctl show bongus -p MemoryCurrent -p MemoryPeak -p MemoryHigh -p MemoryMax
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

## Git-first testnet deployment

If Git is how you transfer the code, push the reviewed changes first and clone
that exact clean commit on the Linux server. Build a native, offline-installable
testnet package on that server, then install it:

```bash
sudo apt-get update
sudo apt-get install -y git curl build-essential pkg-config libssl-dev \
  python3.11 python3.11-venv unzip openssl
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
  sh -s -- -y --profile minimal --default-toolchain 1.94.1
source "$HOME/.cargo/env"

git clone <your-repository-url> bongus_trading_bot
cd bongus_trading_bot
git checkout <the-reviewed-commit>
bash scripts/build_release.sh \
  --output "$PWD/dist/bongus-testnet-linux" \
  --allow-unsigned-development --no-archive

sudo useradd --system --home /var/lib/bongus --shell /usr/sbin/nologin bongus || true
sudo mkdir -p /opt/bongus/releases
sudo cp -a "$PWD/dist/bongus-testnet-linux" /opt/bongus/releases/testnet-soak
cd /opt/bongus/releases/testnet-soak
sudo bash Install-BongusRelease.sh --python "$(command -v python3.11)" \
  --allow-development-package --install-systemd \
  --service-user bongus --data-root /var/lib/bongus
```

Then create `.env` with `TRADING_MODE=testnet`, apply owner `root:bongus` and
mode `0640`, and use the
same `systemctl start/status` and `journalctl` commands above. The build step
uses the network; the installed release and multi-day runtime do not invoke a
compiler or download dependencies.

## Windows install

Verify the ZIP digest, extract into a new directory, and run:

```powershell
.\Install-BongusRelease.ps1
.\.venv\Scripts\python.exe -m bongus.monitoring.king_watchdog
```

The Windows installer requires the exact manifest-pinned Python version, an
offline wheelhouse, the canonical `.venv`, Authenticode signer verification,
the 600 MB runtime budget, and 4 GB post-install free-space headroom.

## Live approval artifact

Live mode also requires the existing schema-v3 short-lived operator approval.
It binds the effective configuration hash, installed release-manifest hash,
native Rust binary hash, signed exchange account UID, Gate-D decision artifact,
operator identity, nonce, and expiry. Keep both the approval HMAC key and the
Linux signing-key fingerprint outside the release and runtime volume.
