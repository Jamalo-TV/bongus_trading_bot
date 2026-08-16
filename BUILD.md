# Reproducible build and runtime entry points

## Supported toolchains

- Python 3.11.15, declared in `.python-version`
- Rust 1.94.1 with `rustfmt`, declared in `rust-toolchain.toml`

Python 3.11.15 is the reviewed source-build floor. Release builders and
installers accept a later final 3.11 security patch, but reject an older patch,
another major/minor series, or any alpha/beta/RC interpreter. CI remains pinned
to the exact reviewed floor.

`requirements.txt` is the human-maintained dependency input.
`requirements.lock` is the exact Python 3.11 dependency graph used by CI and
deployments. `execution_engine/Cargo.lock` is the exact Rust dependency graph.

## Clean-room setup

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python scripts/release_manifest.py check-python 3.11.15
python -m pip install -r requirements.lock
python -m pip check
```

On POSIX shells, activate with `source .venv/bin/activate` instead.

## Canonical process entry points

`bongus/runtime/process_manifest.json` is the machine-readable process source
of truth. The watchdog validates it at startup; tests reject a second trader
implementation or a manifest/watchdog mismatch.

The only canonical trader implementation and process entry point is:

```text
python -m scripts.live_trader_v2
```

`bongus.monitoring.king_watchdog` launches exactly that module. The root
`live_trader_v2.py` remains only as a deprecated compatibility delegate so old
operator commands and imports do not break. It contains no trader
implementation. `bongus.runtime.live_trader` is not a production entry point.

The supervised stack remains:

```text
python -m bongus.monitoring.king_watchdog
```

## Validation

```powershell
python -m pytest tests -q
python -m pyright
python -m compileall -q bongus scripts
cargo fmt --manifest-path execution_engine\Cargo.toml --all -- --check
cargo test --manifest-path execution_engine\Cargo.toml --locked
python scripts/verify_masterplan.py --run-local-checks
```

The master-plan verifier separates local code/test completion from empirical
promotion gates. A successful local suite can still correctly report
`BLOCKED_EVIDENCE` for the million-trace campaign, representative exchange
cycles, settlement samples, cost calibration, unattended soak, or canary data.

To produce the explicit Phase 1 fault artifact:

```powershell
python scripts/run_execution_fault_campaign.py --traces 1000000 --workers 4 --output verification_artifacts\phase1_fault_campaign.json
python scripts/verify_masterplan.py
```

Pyright intentionally checks the active `bongus` package and canonical trader
module. Archived `.claude` and `.worktrees` trees are excluded.

## Updating Python dependencies

Dependency changes require regenerating and validating the lock on Python 3.11.15:

```powershell
python -m pip install pip-tools==7.5.3
python -m piptools compile --resolver=backtracking --strip-extras --no-annotate requirements.txt --output-file=requirements.lock
python -m pip install -r requirements.lock
python -m pip check
python -m pytest tests -q
python -m pyright
```

The existing output file is used as the no-upgrade constraint. Add
`--upgrade` only for an intentional dependency refresh. Review the complete
lock diff; do not edit transitive pins by hand.
