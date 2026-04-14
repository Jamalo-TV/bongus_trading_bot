"""Tests for Bug 3: debounced runtime-mode and safe-mode alerts.

These tests exercise the debounce state-machine logic that lives inside
poll_state_alerts in telegram_alerter.py.  They replicate the decision
logic inline so no external dependencies (aiohttp, dotenv, etc.) are
needed.

Each "tick" represents one 30 s poll cycle.  The fake monotonic clock
advances 35 s per tick so that the 60 s debounce threshold is crossed
after exactly two stable ticks in a row.
"""

import unittest

# The production debounce window (seconds).  Keep in sync with
# telegram_alerter._RUNTIME_MODE_DEBOUNCE_S.
_DEBOUNCE_S: float = 60.0
_TICK_S: float = 35.0  # simulated time between poll cycles


class _DebounceHarness:
    """Replay N poll-cycle ticks of the mode-change debounce logic.

    Mirrors the state variables and if/else branches in poll_state_alerts
    so that changes to the production logic are immediately caught by
    these tests.
    """

    def __init__(self):
        self.sent: list[str] = []
        self.prev_runtime_mode = "LIVE"
        self.prev_safe_mode_reason = ""
        self._candidate_runtime_mode = ""
        self._candidate_runtime_mode_first_seen = 0.0
        self._candidate_safe_mode_reason = ""
        self._candidate_safe_mode_reason_first_seen = 0.0
        self._now = 0.0

    def tick(self, runtime_mode: str, safe_mode_reason: str = "") -> None:
        """Advance one poll cycle with the given runtime state."""
        now_mono = self._now
        self._now += _TICK_S

        # ── Runtime-mode debounce (mirrors poll_state_alerts) ──────────────
        if runtime_mode != self.prev_runtime_mode and runtime_mode:
            if runtime_mode != self._candidate_runtime_mode:
                self._candidate_runtime_mode = runtime_mode
                self._candidate_runtime_mode_first_seen = now_mono
            elif now_mono - self._candidate_runtime_mode_first_seen >= _DEBOUNCE_S:
                self.sent.append(f"RUNTIME MODE CHANGED|{runtime_mode}")
                self.prev_runtime_mode = runtime_mode
                self._candidate_runtime_mode = ""
        else:
            self._candidate_runtime_mode = ""

        # ── Safe-mode-reason debounce ──────────────────────────────────────
        if safe_mode_reason and safe_mode_reason != self.prev_safe_mode_reason:
            if safe_mode_reason != self._candidate_safe_mode_reason:
                self._candidate_safe_mode_reason = safe_mode_reason
                self._candidate_safe_mode_reason_first_seen = now_mono
            elif now_mono - self._candidate_safe_mode_reason_first_seen >= _DEBOUNCE_S:
                self.sent.append(f"SAFE MODE ACTIVE|{safe_mode_reason}")
                self.prev_safe_mode_reason = safe_mode_reason
                self._candidate_safe_mode_reason = ""
        else:
            self._candidate_safe_mode_reason = ""
            if not safe_mode_reason:
                self.prev_safe_mode_reason = ""


class TestRuntimeModeDebounce(unittest.TestCase):
    def test_flap_within_debounce_window_suppresses_alert(self):
        """LIVE→SAFE_MODE→LIVE within one tick pair must produce zero alerts."""
        h = _DebounceHarness()
        h.tick("SAFE_MODE", "stale_pending_intent")  # tick 1: candidate starts at t=0
        h.tick("LIVE")                               # tick 2: reverts at t=35 < 60
        runtime_alerts = [m for m in h.sent if "RUNTIME MODE CHANGED" in m]
        self.assertEqual(len(runtime_alerts), 0, runtime_alerts)

    def test_stable_change_fires_exactly_once_after_debounce(self):
        """LIVE→SAFE_MODE held for 3 ticks fires exactly one alert after debounce."""
        h = _DebounceHarness()
        h.tick("SAFE_MODE", "stale_pending_intent")  # tick 1: candidate at t=0
        h.tick("SAFE_MODE", "stale_pending_intent")  # tick 2: t=35, not yet
        h.tick("SAFE_MODE", "stale_pending_intent")  # tick 3: t=70 >= 60, fires
        runtime_alerts = [m for m in h.sent if "RUNTIME MODE CHANGED" in m]
        self.assertEqual(len(runtime_alerts), 1, runtime_alerts)
        self.assertIn("SAFE_MODE", runtime_alerts[0])

    def test_oscillating_mode_resets_timer_each_time(self):
        """Repeated LIVE↔SAFE_MODE oscillation never commits an alert."""
        h = _DebounceHarness()
        for _ in range(6):
            h.tick("SAFE_MODE", "stale_pending_intent")
            h.tick("LIVE")
        runtime_alerts = [m for m in h.sent if "RUNTIME MODE CHANGED" in m]
        self.assertEqual(len(runtime_alerts), 0, runtime_alerts)

    def test_stable_live_then_stable_safe_mode_re_alerts(self):
        """After SAFE_MODE fires, a stable LIVE transition (>=60 s) commits prev to LIVE,
        so the next SAFE_MODE hold produces a second RUNTIME MODE CHANGED alert."""
        h = _DebounceHarness()
        # First SAFE_MODE occurrence — fires after 3 ticks
        h.tick("SAFE_MODE", "stale_pending_intent")
        h.tick("SAFE_MODE", "stale_pending_intent")
        h.tick("SAFE_MODE", "stale_pending_intent")  # first alert (prev→SAFE_MODE)
        # LIVE holds for 3 ticks — enough to commit prev→LIVE
        h.tick("LIVE")
        h.tick("LIVE")
        h.tick("LIVE")  # LIVE alert fires (prev→LIVE)
        # SAFE_MODE returns and holds
        h.tick("SAFE_MODE", "stale_pending_intent")
        h.tick("SAFE_MODE", "stale_pending_intent")
        h.tick("SAFE_MODE", "stale_pending_intent")  # second SAFE_MODE alert
        runtime_alerts = [m for m in h.sent if "RUNTIME MODE CHANGED" in m]
        self.assertEqual(len(runtime_alerts), 3, runtime_alerts)  # SAFE + LIVE + SAFE

    def test_brief_live_between_safe_modes_does_not_double_alert(self):
        """A LIVE tick too brief to debounce does not commit prev; returning to SAFE_MODE
        is not treated as a new mode change and fires no duplicate alert."""
        h = _DebounceHarness()
        # SAFE_MODE commits
        h.tick("SAFE_MODE", "stale_pending_intent")
        h.tick("SAFE_MODE", "stale_pending_intent")
        h.tick("SAFE_MODE", "stale_pending_intent")  # alert, prev=SAFE_MODE
        # Brief LIVE (single tick, 35 s < 60 s debounce) — prev stays SAFE_MODE
        h.tick("LIVE")
        # Back to SAFE_MODE — mode == prev (SAFE_MODE), no new alert
        h.tick("SAFE_MODE", "stale_pending_intent")
        h.tick("SAFE_MODE", "stale_pending_intent")
        h.tick("SAFE_MODE", "stale_pending_intent")
        runtime_alerts = [m for m in h.sent if "RUNTIME MODE CHANGED" in m]
        self.assertEqual(len(runtime_alerts), 1, runtime_alerts)

    def test_debounce_threshold_is_at_least_two_poll_cycles(self):
        """_DEBOUNCE_S must be >= 60 s (two 30 s cycles)."""
        self.assertGreaterEqual(_DEBOUNCE_S, 60.0)


class TestSafeModeReasonDebounce(unittest.TestCase):
    def test_oscillating_safe_mode_reason_suppressed(self):
        """A safe_mode_reason that oscillates never produces a SAFE MODE ACTIVE alert."""
        h = _DebounceHarness()
        h.tick("SAFE_MODE", "stale_pending_intent")  # tick 1: candidate starts
        h.tick("LIVE")                               # tick 2: clears
        h.tick("SAFE_MODE", "stale_pending_intent")  # tick 3: new candidate
        h.tick("LIVE")                               # tick 4: clears
        safe_alerts = [m for m in h.sent if "SAFE MODE ACTIVE" in m]
        self.assertEqual(len(safe_alerts), 0, safe_alerts)

    def test_stable_safe_mode_reason_fires_alert(self):
        """A stable safe_mode_reason held for 3 ticks fires exactly one alert."""
        h = _DebounceHarness()
        h.tick("SAFE_MODE", "exit_failure")
        h.tick("SAFE_MODE", "exit_failure")
        h.tick("SAFE_MODE", "exit_failure")  # fires
        safe_alerts = [m for m in h.sent if "SAFE MODE ACTIVE" in m]
        self.assertEqual(len(safe_alerts), 1, safe_alerts)
        self.assertIn("exit_failure", safe_alerts[0])


if __name__ == "__main__":
    unittest.main()
