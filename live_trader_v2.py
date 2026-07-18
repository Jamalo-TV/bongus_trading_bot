"""Deprecated compatibility entry point for :mod:`scripts.live_trader_v2`.

The implementation lives only in ``scripts.live_trader_v2``. Existing imports
and direct invocations of this root-level file continue to delegate there.
New process definitions must use ``python -m scripts.live_trader_v2``.
"""

from scripts.live_trader_v2 import *  # noqa: F401,F403
from scripts.live_trader_v2 import run_cli as _run_canonical_cli


if __name__ == "__main__":
    _run_canonical_cli()
