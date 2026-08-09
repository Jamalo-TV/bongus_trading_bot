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
enables it. It deliberately does not start the bot.

For an unsigned testnet artifact, replace the trust-pin argument with
`--allow-development-package`. The watchdog still refuses `TRADING_MODE=live`.

## Configure and run for the soak period

Create `/opt/bongus/releases/2026-08-09/.env` with mode `0600`. Start with
paper or Binance testnet, never live:

```dotenv
TRADING_MODE=testnet
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
BONGUS_RELEASE_SIGNING_KEY_SHA256=<required for a production-signed live release>
```

Then start and observe it:

```bash
sudo chmod 600 /opt/bongus/releases/2026-08-09/.env
sudo chown bongus:bongus /opt/bongus/releases/2026-08-09/.env
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

Then create `.env` with `TRADING_MODE=testnet`, apply mode `0600`, and use the
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
