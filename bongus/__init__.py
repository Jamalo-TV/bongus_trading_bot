"""Bongus Trading Bot — hybrid Python/Rust algorithmic trading system."""

import logging
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

logging.getLogger(__name__).addHandler(logging.NullHandler())
