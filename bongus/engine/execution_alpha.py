"""Execution-alpha simulation: routing, fill probability, and expected costs."""

from dataclasses import dataclass

import msgpack
import zmq


@dataclass
class VenueQuote:
    venue: str
    bid: float
    ask: float
    depth_usd: float
    fee_bps: float
    latency_ms: int
    reliability: float
    obi: float = 0.0  # Order Book Imbalance: -1.0 to 1.0
    queue_position: float = 0.5  # 0.0 (front of queue) to 1.0 (back of queue)


@dataclass
class OrderIntent:
    symbol: str
    side: str
    quantity: float
    urgency: float
    max_slippage_bps: float
    exposure_scale: float = 1.0


@dataclass
class ExecutionPlan:
    venue: str
    order_type: str
    limit_price: float
    expected_cost_bps: float
    fill_probability: float


def _spread_bps(quote: VenueQuote) -> float:
    mid = (quote.bid + quote.ask) / 2.0
    if mid <= 0:
        return 10_000.0
    return abs(quote.ask - quote.bid) / mid * 10_000.0


def estimate_fill_probability(intent: OrderIntent, quote: VenueQuote, order_type: str) -> float:
    spread = _spread_bps(quote)
    depth_factor = min(1.0, quote.depth_usd / max(1.0, intent.quantity))
    latency_penalty = min(0.5, quote.latency_ms / 2000.0)

    obi_factor = quote.obi * 0.1 if intent.side.lower() == "buy" else -quote.obi * 0.1
    queue_factor = (0.5 - quote.queue_position) * 0.2

    if order_type == "market":
        base = 0.995
    else:
        base = max(0.2, 1.0 - spread / 50.0) + obi_factor + queue_factor

    probability = base * depth_factor * quote.reliability * (1.0 - latency_penalty)
    return max(0.0, min(1.0, probability))


def expected_cost_bps(intent: OrderIntent, quote: VenueQuote, order_type: str) -> float:
    spread = _spread_bps(quote)
    slip_bps = (1.0 - min(1.0, quote.depth_usd / max(intent.quantity, 1.0))) * 8.0

    if order_type == "market":
        crossing_cost = spread / 2.0 + slip_bps
    else:
        crossing_cost = max(0.1, spread * 0.15)

    urgency_penalty = intent.urgency * 2.0
    return quote.fee_bps + crossing_cost + urgency_penalty


def route_order(intent: OrderIntent, quotes: list[VenueQuote]) -> ExecutionPlan:
    if not quotes:
        raise ValueError("At least one quote is required")

    best: ExecutionPlan | None = None

    for quote in quotes:
        order_type = "market" if intent.urgency >= 0.7 else "limit"
        fill_prob = estimate_fill_probability(intent, quote, order_type)
        cost_bps = expected_cost_bps(intent, quote, order_type)

        mid = (quote.bid + quote.ask) / 2.0
        if intent.side.lower() == "buy":
            limit_price = mid * (1.0 + intent.max_slippage_bps / 10_000.0)
        else:
            limit_price = mid * (1.0 - intent.max_slippage_bps / 10_000.0)

        candidate = ExecutionPlan(
            venue=quote.venue,
            order_type=order_type,
            limit_price=limit_price,
            expected_cost_bps=cost_bps,
            fill_probability=fill_prob,
        )

        if best is None:
            best = candidate
            continue

        score_candidate = candidate.expected_cost_bps - candidate.fill_probability * 3.0
        score_best = best.expected_cost_bps - best.fill_probability * 3.0
        if score_candidate < score_best:
            best = candidate

    if best is None:
        raise RuntimeError("No valid execution plan produced")

    return best

class RustIPCBridge:
    def __init__(self, endpoint="ipc:///tmp/bongus.sock"):
        self.context = zmq.Context()
        self.socket = self.context.socket(zmq.PUSH)
        self.socket.connect(endpoint)

    def dispatch_intent(self, intent: OrderIntent):
        """Sends translated instructions to the Rust engine dynamically."""
        action = "ENTER_LONG" if intent.side.lower() == "buy" else "ENTER_SHORT"

        # Rust expects: symbol, intent, quantity, urgency, max_slippage_bps, exposure_scale
        payload = {
            "symbol": intent.symbol,
            "intent": action,
            "quantity": float(intent.quantity),
            "urgency": float(intent.urgency),
            "max_slippage_bps": float(intent.max_slippage_bps),
            "exposure_scale": float(intent.exposure_scale)
        }
        
        packed = msgpack.packb(payload)
        self.socket.send(packed)
        print(f"[IPC] Sent payload to Rust: {payload}")

    def close(self):
        self.socket.close()
        self.context.term()
