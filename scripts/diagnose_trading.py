#!/usr/bin/env python3
"""
Trading Bot Diagnostic Script
Run this to identify why the bot isn't entering trades.
"""

import asyncio
import os
import sqlite3
import sys
from pathlib import Path

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bongus.core.config import (
    CAPITAL_PER_SLOT_USD,
    ENTRY_ANN_FUNDING_THRESHOLD_ALT,
    ENTRY_ANN_FUNDING_THRESHOLD_BTC,
    MAX_CONCURRENT_POSITIONS,
    TARGET_LEVERAGE,
)
from bongus.portfolio.portfolio_allocator import KELLY_FRACTION


class Diagnostic:
    def __init__(self):
        self.issues = []
        self.warnings = []
        self.passed = []

    def add_issue(self, msg):
        self.issues.append(f"  ❌ ISSUE: {msg}")

    def add_warning(self, msg):
        self.warnings.append(f"  ⚠️  WARNING: {msg}")

    def add_pass(self, msg):
        self.passed.append(f"  ✅ {msg}")

    def print_report(self):
        print("\n" + "="*60)
        print("🔍 TRADING BOT DIAGNOSTIC REPORT")
        print("="*60)

        if self.passed:
            print("\n📊 PASSING CHECKS:")
            for p in self.passed:
                print(p)

        if self.warnings:
            print("\n⚠️  WARNINGS:")
            for w in self.warnings:
                print(w)

        if self.issues:
            print("\n❌ CRITICAL ISSUES:")
            for i in self.issues:
                print(i)

        if not self.issues and not self.warnings:
            print("\n🎉 No issues found! Bot should be trading.")

        print("\n" + "="*60)


async def check_funding_rates(diag):
    """Check if funding rates are high enough to trigger entries."""
    print("\n📡 Checking Binance funding rates...")

    try:
        import requests
        resp = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex", timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # Filter to our monitored symbols
        symbols_of_interest = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "PEPEUSDT", "BNBUSDT", "ARBUSDT", "SUIUSDT"]

        high_enough = []
        too_low = []

        for item in data:
            symbol = item.get("symbol", "")
            if not any(s in symbol for s in symbols_of_interest):
                continue

            next_rate = float(item.get("nextFundingRate", 0) or 0)
            last_rate = float(item.get("lastFundingRate", 0) or 0)
            ann_next = next_rate * 1095  # 3 periods/day * 365
            last_rate * 1095

            # Determine threshold
            if "BTC" in symbol:
                threshold = ENTRY_ANN_FUNDING_THRESHOLD_BTC
            else:
                threshold = ENTRY_ANN_FUNDING_THRESHOLD_ALT

            if ann_next >= threshold:
                high_enough.append((symbol, ann_next * 100, threshold * 100))
            else:
                too_low.append((symbol, ann_next * 100, threshold * 100))

        if high_enough:
            diag.add_pass(f"Found {len(high_enough)} symbols above entry threshold:")
            for sym, rate, thresh in sorted(high_enough, key=lambda x: -x[1])[:5]:
                diag.add_pass(f"   {sym}: {rate:.2f}% (need >{thresh:.1f}%)")
        else:
            diag.add_issue("No symbols above entry threshold!")
            for sym, rate, thresh in sorted(too_low, key=lambda x: -x[1])[:5]:
                diag.add_warning(f"   {sym}: {rate:.2f}% (need >{thresh:.1f}%)")

    except Exception as e:
        diag.add_issue(f"Could not fetch funding rates: {e}")


def check_state_store(diag):
    """Check the state database for issues."""
    print("\n💾 Checking state database...")

    db_path = Path("state.db")
    if not db_path.exists():
        diag.add_warning("state.db not found (may not have run before)")
        return

    try:
        conn = sqlite3.connect(str(db_path), timeout=5)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # Check positions
        cursor.execute("SELECT * FROM positions")
        positions = cursor.fetchall()
        if positions:
            diag.add_pass(f"Found {len(positions)} open positions in database")
            for pos in positions:
                print(f"   {pos['symbol']}: {pos['status']} | PnL: ${pos['net_pnl_usd']:.2f}")
        else:
            diag.add_pass("No open positions (database is clean)")

        # Check trade history
        cursor.execute("SELECT COUNT(*) as cnt, SUM(net_pnl_usd) as total_pnl FROM trade_history")
        row = cursor.fetchone()
        if row['cnt'] > 0:
            diag.add_pass(f"Trade history: {row['cnt']} trades, Total PnL: ${row['total_pnl'] or 0:.2f}")
        else:
            diag.add_warning("No completed trades in history")

        # Check risk state
        cursor.execute("SELECT * FROM risk_state")
        risk = cursor.fetchall()
        kill_switch = None
        for r in risk:
            if r['key'] == 'kill_switch':
                kill_switch = r['value']
            print(f"   Risk [{r['key']}]: {r['value']}")

        if kill_switch == 'true':
            diag.add_issue("KILL SWITCH is ACTIVE - new positions blocked!")

        conn.close()

    except Exception as e:
        diag.add_warning(f"Could not read state database: {e}")


def check_config(diag):
    """Check configuration settings."""
    print("\n⚙️  Checking configuration...")

    diag.add_pass(f"MAX_CONCURRENT_POSITIONS = {MAX_CONCURRENT_POSITIONS}")
    diag.add_pass(f"CAPITAL_PER_SLOT_USD = ${CAPITAL_PER_SLOT_USD:,}")
    diag.add_pass(f"TARGET_LEVERAGE = {TARGET_LEVERAGE}x")
    diag.add_pass(f"KELLY_FRACTION = {KELLY_FRACTION:.0%}")

    # Calculate expected notional
    expected_notional = CAPITAL_PER_SLOT_USD * KELLY_FRACTION * TARGET_LEVERAGE
    max_exposure = MAX_CONCURRENT_POSITIONS * expected_notional
    diag.add_pass(f"Expected notional/slot: ${expected_notional:,.0f}")
    diag.add_pass(f"Max gross exposure: ${max_exposure:,.0f}")

    # Check thresholds
    diag.add_pass(f"BTC entry threshold: {ENTRY_ANN_FUNDING_THRESHOLD_BTC*100:.0f}%")
    diag.add_pass(f"ALT entry threshold: {ENTRY_ANN_FUNDING_THRESHOLD_ALT*100:.0f}%")


def check_system(diag):
    """Check system requirements."""
    print("\n🖥️  Checking system status...")

    # Check if Rust engine is likely running
    import socket
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', 5555))
        sock.close()
        if result == 0:
            diag.add_pass("Rust execution engine (port 5555): CONNECTED")
        else:
            diag.add_warning("Rust execution engine (port 5555): NOT reachable")
    except:
        diag.add_warning("Could not check Rust engine status")

    # Check WebSocket port
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        result = sock.connect_ex(('127.0.0.1', 9000))
        sock.close()
        if result == 0:
            diag.add_pass("Telemetry WebSocket (port 9000): CONNECTED")
        else:
            diag.add_warning("Telemetry WebSocket (port 9000): NOT reachable")
    except:
        diag.add_warning("Could not check WebSocket status")


def main():
    diag = Diagnostic()

    print("Running trading bot diagnostic...")
    print("This will check funding rates, config, and system status.")

    # Run sync checks
    check_config(diag)
    check_state_store(diag)
    check_system(diag)

    # Run async funding check
    asyncio.run(check_funding_rates(diag))

    # Print summary
    diag.print_report()

    # Recommendations
    print("\n💡 RECOMMENDATIONS:")
    if any("KILL SWITCH" in i for i in diag.issues):
        print("  1. Deactivate the Kill Switch in the dashboard")
    if any("No symbols above" in i for i in diag.issues):
        print("  1. Current funding rates are too low for entry")
        print("  2. Wait for funding rates to increase (often during volatility)")
        print("  3. Or temporarily lower ENTRY_ANN_FUNDING_THRESHOLD")
    if any("NO TRADES" in i for i in diag.warnings):
        print("  1. Bot is working but no trades have completed yet")
        print("  2. Check logs in scripts/logs/live_trader.log")
    if not diag.issues and not diag.warnings:
        print("  Bot appears healthy! Check logs if trades still don't execute.")


if __name__ == "__main__":
    main()
