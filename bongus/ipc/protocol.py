"""Versioned, replay-safe commands shared by Python and Rust.

Protocol v2 defines a typed canonical byte representation for every immutable
risk-command field.  It deliberately does not rely on a language's JSON float
or object-ordering behaviour: strings are UTF-8 length-prefixed and floats are
encoded by their IEEE-754 bits.  Rust recomputes the same SHA-256 before an
instruction can reach the durable receipt journal.

Producer sequence and command-window fields are transport metadata.  They are
validated independently and excluded from the semantic command hash so a
durable outbox can detect an ``intent_id`` conflict before allocating those
fields.  Deterministic leg and client-order IDs *are* included in the hash.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import time
from numbers import Integral, Real
from typing import Any

EXECUTION_PROTOCOL_VERSION = 2
CONFIG_SYNC_INTENT = "CONFIG_SYNC"
RISK_CHANGING_INTENTS = frozenset(
    {"ENTER_LONG", "ENTER_SHORT", "EXIT_LONG", "EXIT_SHORT"}
)
DURABLE_COMMAND_INTENTS = RISK_CHANGING_INTENTS | frozenset({CONFIG_SYNC_INTENT})
LEGACY_SAFE_INTENTS = frozenset({"HEARTBEAT", "RESTORE_POSITION"})
ACK_STATES = frozenset(
    {"RECEIVED", "VALIDATED", "SUBMITTED", "TERMINAL", "REJECTED"}
)
ROUTE_POLICIES = frozenset(
    {
        "legacy_dual_maker",
        "post_only_dual",
        "maker_lead_ioc",
        "simultaneous_ioc",
        "sliced_ioc",
        "emergency_reduce_only",
    }
)
# The optimizer evaluates every route above, but the roadmap requires shadow
# evidence before an exchange-effecting cutover.  Keeping this allow-list
# separate makes an accidental recommendation-to-order wiring fail closed.
ACTIVE_ROUTE_POLICIES = frozenset({"legacy_dual_maker"})
DEFAULT_MAX_UNHEDGED_NOTIONAL_MS = 5_000_000.0
MAX_COMMAND_TTL_MS = 300_000

_TRANSPORT_FIELDS = frozenset(
    {"producer_id", "sequence", "created_at_ms", "deadline_at_ms"}
)
_DERIVED_FIELDS = frozenset(
    {
        "schema_version",
        "command_hash",
        "spot_client_order_id",
        "perp_client_order_id",
        "spot_leg_id",
        "perp_leg_id",
    }
)
_RISK_PAYLOAD_FIELDS = frozenset(
    {
        "account_id",
        "environment",
        "strategy_id",
        "cycle_id",
        "config_version_hash",
        "symbol",
        "intent",
        "quantity",
        "urgency",
        "max_slippage_bps",
        "route_policy",
        "route_model_version",
        "max_unhedged_notional_ms",
        "route_slice_count",
        "exposure_scale",
        "intent_id",
        "direction",
        "skip_spot_leg",
        "skip_perp_leg",
        "spot_quantity",
        "perp_quantity",
    }
)
_RISK_ENVELOPE_FIELDS = _RISK_PAYLOAD_FIELDS | _TRANSPORT_FIELDS | _DERIVED_FIELDS
_CONFIG_SYNC_PAYLOAD_FIELDS = frozenset(
    {
        "account_id",
        "environment",
        "strategy_id",
        "cycle_id",
        "config_version_hash",
        "config_canonical_json",
        "intent",
        "intent_id",
    }
)
_CONFIG_SYNC_DERIVED_FIELDS = frozenset({"schema_version", "command_hash"})
_CONFIG_SYNC_ENVELOPE_FIELDS = (
    _CONFIG_SYNC_PAYLOAD_FIELDS | _TRANSPORT_FIELDS | _CONFIG_SYNC_DERIVED_FIELDS
)
_ACK_FIELDS = frozenset(
    {
        "event",
        "schema_version",
        "intent_id",
        "producer_id",
        "sequence",
        "account_id",
        "environment",
        "strategy_id",
        "cycle_id",
        "config_version_hash",
        "spot_leg_id",
        "perp_leg_id",
        "spot_client_order_id",
        "perp_client_order_id",
        "command_hash",
        "ack_status",
        "reason",
        "event_time_ms",
        "replay",
        "declared_config_hash",
        "applied_config_hash",
        "config_status",
    }
)

# Field order and field type are part of the v2 wire specification.  The Rust
# implementation has the identical list in execution_engine/src/ipc.rs.
_CANONICAL_FIELD_TYPES: tuple[tuple[str, str], ...] = (
    ("schema_version", "int"),
    ("account_id", "string"),
    ("environment", "string"),
    ("strategy_id", "string"),
    ("cycle_id", "string"),
    ("config_version_hash", "string"),
    ("symbol", "string"),
    ("intent", "string"),
    ("quantity", "float"),
    ("urgency", "float"),
    ("max_slippage_bps", "float"),
    ("route_policy", "string"),
    ("route_model_version", "string"),
    ("max_unhedged_notional_ms", "float"),
    ("route_slice_count", "int"),
    ("exposure_scale", "float"),
    ("intent_id", "string"),
    ("direction", "optional_string"),
    ("skip_spot_leg", "bool"),
    ("skip_perp_leg", "bool"),
    ("spot_quantity", "optional_float"),
    ("perp_quantity", "optional_float"),
    ("spot_client_order_id", "string"),
    ("perp_client_order_id", "string"),
    ("spot_leg_id", "string"),
    ("perp_leg_id", "string"),
)
_CANONICAL_MAGIC = b"bongus-execution-command-v2\n"
_CONFIG_SYNC_CANONICAL_FIELD_TYPES: tuple[tuple[str, str], ...] = (
    ("schema_version", "int"),
    ("account_id", "string"),
    ("environment", "string"),
    ("strategy_id", "string"),
    ("cycle_id", "string"),
    ("config_version_hash", "string"),
    ("intent", "string"),
    ("intent_id", "string"),
    ("config_canonical_json", "string"),
)
_CONFIG_SYNC_CANONICAL_MAGIC = b"bongus-config-sync-command-v2\n"


class ExecutionProtocolError(ValueError):
    """Raised when a command cannot be represented safely on the wire."""


def deterministic_client_order_id(intent_id: str, leg: str) -> str:
    """Derive a Binance-safe client ID (<=36 chars) from the durable intent."""

    normalized_leg = "s" if leg.lower() == "spot" else "p"
    digest = hashlib.sha256(f"{intent_id}:{normalized_leg}".encode()).hexdigest()[:24]
    return f"bngs_{normalized_leg}_{digest}"


def _float(value: Any, field: str, *, default: float = 0.0) -> float:
    if value is None:
        result = default
    else:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ExecutionProtocolError(f"{field} must be numeric")
        result = float(value)
    if not math.isfinite(result):
        raise ExecutionProtocolError(f"{field} must be finite")
    return result


def _optional_float(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _float(value, field)


def _bool(value: Any, field: str, *, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ExecutionProtocolError(f"{field} must be boolean")
    return value


def _integer(value: Any, field: str, *, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ExecutionProtocolError(f"{field} must be an integer")
    return int(value)


def _string(value: Any, field: str, *, default: str = "") -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise ExecutionProtocolError(f"{field} must be a string")
    return value


def _validate_config_document(canonical_json: str, declared_hash: str) -> None:
    if not canonical_json:
        raise ExecutionProtocolError("config_canonical_json is required")
    if (
        len(declared_hash) != 64
        or any(ch not in "0123456789abcdef" for ch in declared_hash)
    ):
        raise ExecutionProtocolError("config_version_hash must be a lowercase SHA-256")
    actual_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    if actual_hash != declared_hash:
        raise ExecutionProtocolError("config snapshot SHA-256 does not match its declaration")
    try:
        document = json.loads(
            canonical_json,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"non-finite JSON constant {value}")
            ),
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ExecutionProtocolError("config_canonical_json is malformed") from exc
    if not isinstance(document, dict):
        raise ExecutionProtocolError("config_canonical_json must contain an object")
    try:
        reconstructed = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ExecutionProtocolError("config_canonical_json is malformed") from exc
    if reconstructed != canonical_json:
        raise ExecutionProtocolError("config_canonical_json is not canonical")

    # The Rust validator loads the same allowed-key schema and rejects drift.
    # Checking here catches a bad producer before it consumes an outbox slot.
    from bongus.core.config_manager import ConfigManager

    unknown = set(document) - ConfigManager.allowed_keys()
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ExecutionProtocolError(f"unknown effective-config key(s): {names}")
    required = {
        "pause_new_entries",
        "per_symbol_notional_cap_usd",
        "max_gross_exposure_usd",
    }
    missing = required - set(document)
    if missing:
        names = ", ".join(sorted(missing))
        raise ExecutionProtocolError(f"missing consensus config key(s): {names}")
    if not isinstance(document["pause_new_entries"], bool):
        raise ExecutionProtocolError("pause_new_entries must be boolean")
    for field in ("per_symbol_notional_cap_usd", "max_gross_exposure_usd"):
        value = document[field]
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ExecutionProtocolError(f"{field} must be numeric")
        if not math.isfinite(float(value)) or float(value) <= 0.0:
            raise ExecutionProtocolError(f"{field} must be positive and finite")


def canonical_config_sync_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the immutable body of a protocol-v2 config sync."""

    unknown = set(payload) - _CONFIG_SYNC_ENVELOPE_FIELDS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ExecutionProtocolError(f"unknown config-sync field(s): {names}")
    body: dict[str, Any] = {
        "schema_version": _integer(
            payload.get("schema_version"),
            "schema_version",
            default=EXECUTION_PROTOCOL_VERSION,
        ),
        "account_id": _string(payload.get("account_id"), "account_id").strip(),
        "environment": _string(payload.get("environment"), "environment").strip(),
        "strategy_id": _string(payload.get("strategy_id"), "strategy_id").strip(),
        "cycle_id": _string(payload.get("cycle_id"), "cycle_id").strip(),
        "config_version_hash": _string(
            payload.get("config_version_hash"), "config_version_hash"
        ).strip(),
        "intent": _string(payload.get("intent"), "intent").strip().upper(),
        "intent_id": _string(payload.get("intent_id"), "intent_id").strip(),
        "config_canonical_json": _string(
            payload.get("config_canonical_json"), "config_canonical_json"
        ),
    }
    if body["intent"] != CONFIG_SYNC_INTENT:
        raise ExecutionProtocolError("config sync must use CONFIG_SYNC intent")
    _validate_config_document(
        str(body["config_canonical_json"]),
        str(body["config_version_hash"]),
    )
    return body


def canonical_command_body(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize the complete immutable v2 command body.

    This function accepts either a pre-envelope risk payload or a completed
    envelope.  Unknown keys fail closed; generated leg identifiers are derived
    when hashing a pre-envelope payload so the outbox's preallocation conflict
    check and the final Rust validation cover the same semantics.
    """

    unknown = set(payload) - _RISK_ENVELOPE_FIELDS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ExecutionProtocolError(f"unknown risk-command field(s): {names}")

    intent_id = _string(payload.get("intent_id"), "intent_id").strip()
    direction_value = payload.get("direction")
    direction = (
        None
        if direction_value is None
        else _string(direction_value, "direction").strip().lower()
    )
    schema_version = _integer(
        payload.get("schema_version"),
        "schema_version",
        default=EXECUTION_PROTOCOL_VERSION,
    )
    route_slice_count = _integer(
        payload.get("route_slice_count"), "route_slice_count", default=1
    )

    body: dict[str, Any] = {
        "schema_version": schema_version,
        "account_id": _string(payload.get("account_id"), "account_id").strip(),
        "environment": _string(payload.get("environment"), "environment").strip(),
        "strategy_id": _string(payload.get("strategy_id"), "strategy_id").strip(),
        "cycle_id": _string(payload.get("cycle_id"), "cycle_id").strip(),
        "config_version_hash": _string(
            payload.get("config_version_hash"), "config_version_hash"
        ).strip(),
        "symbol": _string(payload.get("symbol"), "symbol").strip().upper(),
        "intent": _string(payload.get("intent"), "intent").strip().upper(),
        "quantity": _float(payload.get("quantity"), "quantity"),
        "urgency": _float(payload.get("urgency"), "urgency"),
        "max_slippage_bps": _float(
            payload.get("max_slippage_bps"), "max_slippage_bps"
        ),
        "route_policy": _string(
            payload.get("route_policy"),
            "route_policy",
            default="legacy_dual_maker",
        ).strip().lower(),
        "route_model_version": _string(
            payload.get("route_model_version"),
            "route_model_version",
            default="legacy-v1",
        ).strip(),
        "max_unhedged_notional_ms": _float(
            payload.get(
                "max_unhedged_notional_ms", DEFAULT_MAX_UNHEDGED_NOTIONAL_MS
            ),
            "max_unhedged_notional_ms",
        ),
        "route_slice_count": route_slice_count,
        "exposure_scale": _float(payload.get("exposure_scale"), "exposure_scale"),
        "intent_id": intent_id,
        "direction": direction,
        "skip_spot_leg": _bool(payload.get("skip_spot_leg"), "skip_spot_leg"),
        "skip_perp_leg": _bool(payload.get("skip_perp_leg"), "skip_perp_leg"),
        "spot_quantity": _optional_float(
            payload.get("spot_quantity"), "spot_quantity"
        ),
        "perp_quantity": _optional_float(
            payload.get("perp_quantity"), "perp_quantity"
        ),
        "spot_client_order_id": _string(
            payload.get("spot_client_order_id"),
            "spot_client_order_id",
            default=deterministic_client_order_id(intent_id, "spot"),
        ).strip(),
        "perp_client_order_id": _string(
            payload.get("perp_client_order_id"),
            "perp_client_order_id",
            default=deterministic_client_order_id(intent_id, "perp"),
        ).strip(),
        "spot_leg_id": _string(
            payload.get("spot_leg_id"),
            "spot_leg_id",
            default=f"{intent_id}:spot",
        ).strip(),
        "perp_leg_id": _string(
            payload.get("perp_leg_id"),
            "perp_leg_id",
            default=f"{intent_id}:perp",
        ).strip(),
    }
    return body


def _canonical_scalar(kind: str, value: Any) -> bytes:
    if kind == "string":
        encoded = str(value).encode("utf-8")
        return b"s" + str(len(encoded)).encode("ascii") + b":" + encoded
    if kind == "optional_string":
        if value is None:
            return b"n"
        return _canonical_scalar("string", value)
    if kind == "int":
        return b"i" + str(int(value)).encode("ascii")
    if kind == "float":
        bits = struct.unpack(">Q", struct.pack(">d", float(value)))[0]
        return f"f{bits:016x}".encode("ascii")
    if kind == "optional_float":
        if value is None:
            return b"n"
        return _canonical_scalar("float", value)
    if kind == "bool":
        return b"b1" if bool(value) else b"b0"
    raise AssertionError(f"unsupported canonical field type {kind!r}")


def canonical_command_bytes(payload: dict[str, Any]) -> bytes:
    """Return the language-neutral v2 bytes covered by ``command_hash``."""

    if str(payload.get("intent") or "").strip().upper() == CONFIG_SYNC_INTENT:
        return canonical_config_sync_command_bytes(payload)
    body = canonical_command_body(payload)
    encoded = bytearray(_CANONICAL_MAGIC)
    for field, kind in _CANONICAL_FIELD_TYPES:
        encoded.extend(field.encode("ascii"))
        encoded.extend(b"=")
        encoded.extend(_canonical_scalar(kind, body[field]))
        encoded.extend(b"\n")
    return bytes(encoded)


def canonical_config_sync_command_bytes(payload: dict[str, Any]) -> bytes:
    """Return the distinct v2 byte domain covered by a config-sync hash."""

    body = canonical_config_sync_body(payload)
    encoded = bytearray(_CONFIG_SYNC_CANONICAL_MAGIC)
    for field, kind in _CONFIG_SYNC_CANONICAL_FIELD_TYPES:
        encoded.extend(field.encode("ascii"))
        encoded.extend(b"=")
        encoded.extend(_canonical_scalar(kind, body[field]))
        encoded.extend(b"\n")
    return bytes(encoded)


def command_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_command_bytes(payload)).hexdigest()


def build_config_sync_envelope(
    payload: dict[str, Any],
    *,
    producer_id: str,
    sequence: int,
    ttl_ms: int,
    created_at_ms: int | None = None,
) -> dict[str, Any]:
    """Build a durable, replay-safe protocol-v2 effective-config sync."""

    unknown = set(payload) - _CONFIG_SYNC_PAYLOAD_FIELDS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ExecutionProtocolError(f"unknown config-sync field(s): {names}")
    body = canonical_config_sync_body(payload)
    if not body["intent_id"]:
        raise ExecutionProtocolError("CONFIG_SYNC requires intent_id")
    for required_context in (
        "account_id",
        "environment",
        "strategy_id",
        "cycle_id",
    ):
        if not body[required_context]:
            raise ExecutionProtocolError(f"{required_context} is required")
    if not isinstance(producer_id, str) or not producer_id.strip():
        raise ExecutionProtocolError("producer_id is required")
    if isinstance(sequence, bool) or not isinstance(sequence, Integral) or sequence <= 0:
        raise ExecutionProtocolError("sequence must be positive")
    if (
        isinstance(ttl_ms, bool)
        or not isinstance(ttl_ms, Integral)
        or not 0 < ttl_ms <= MAX_COMMAND_TTL_MS
    ):
        raise ExecutionProtocolError(
            f"ttl_ms must be between 1 and {MAX_COMMAND_TTL_MS}"
        )
    if created_at_ms is not None and (
        isinstance(created_at_ms, bool) or not isinstance(created_at_ms, Integral)
    ):
        raise ExecutionProtocolError("created_at_ms must be an integer")
    created = int(time.time() * 1000) if created_at_ms is None else int(created_at_ms)
    if created <= 0:
        raise ExecutionProtocolError("created_at_ms must be positive")
    envelope = dict(body)
    envelope.update(
        {
            "producer_id": producer_id.strip(),
            "sequence": int(sequence),
            "created_at_ms": created,
            "deadline_at_ms": created + int(ttl_ms),
        }
    )
    envelope["command_hash"] = command_hash(envelope)
    return envelope


def build_command_envelope(
    payload: dict[str, Any],
    *,
    producer_id: str,
    sequence: int,
    ttl_ms: int,
    created_at_ms: int | None = None,
) -> dict[str, Any]:
    """Build and validate one immutable version-2 risk command envelope."""

    if str(payload.get("intent") or "").strip().upper() == CONFIG_SYNC_INTENT:
        return build_config_sync_envelope(
            payload,
            producer_id=producer_id,
            sequence=sequence,
            ttl_ms=ttl_ms,
            created_at_ms=created_at_ms,
        )
    unknown = set(payload) - _RISK_PAYLOAD_FIELDS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ExecutionProtocolError(f"unknown risk-command field(s): {names}")

    body = canonical_command_body(payload)
    intent = str(body["intent"])
    if intent not in RISK_CHANGING_INTENTS:
        raise ExecutionProtocolError(f"{intent or '<missing>'} is not a risk-changing intent")
    intent_id = str(body["intent_id"])
    if not intent_id:
        raise ExecutionProtocolError("risk-changing commands require intent_id")
    if not isinstance(producer_id, str) or not producer_id.strip():
        raise ExecutionProtocolError("producer_id is required")
    if isinstance(sequence, bool) or not isinstance(sequence, Integral) or sequence <= 0:
        raise ExecutionProtocolError("sequence must be positive")
    if (
        isinstance(ttl_ms, bool)
        or not isinstance(ttl_ms, Integral)
        or not 0 < ttl_ms <= MAX_COMMAND_TTL_MS
    ):
        raise ExecutionProtocolError(
            f"ttl_ms must be between 1 and {MAX_COMMAND_TTL_MS}"
        )
    if not body["symbol"]:
        raise ExecutionProtocolError("symbol is required")

    route_policy = str(body["route_policy"])
    if route_policy not in ROUTE_POLICIES:
        raise ExecutionProtocolError(f"unsupported route_policy {route_policy!r}")
    if route_policy not in ACTIVE_ROUTE_POLICIES:
        raise ExecutionProtocolError(f"route_policy {route_policy!r} has not passed promotion gates")
    if route_policy == "emergency_reduce_only" and not intent.startswith("EXIT_"):
        raise ExecutionProtocolError("emergency_reduce_only is exit-only")
    if not body["route_model_version"]:
        raise ExecutionProtocolError("route_model_version is required")
    hedge_budget = float(body["max_unhedged_notional_ms"])
    if hedge_budget <= 0.0:
        raise ExecutionProtocolError("max_unhedged_notional_ms must be positive and finite")
    route_slice_count = int(body["route_slice_count"])
    if not 1 <= route_slice_count <= 16:
        raise ExecutionProtocolError("route_slice_count must be between 1 and 16")
    if route_policy != "sliced_ioc" and route_slice_count != 1:
        raise ExecutionProtocolError("route_slice_count > 1 requires sliced_ioc")

    for field in ("quantity", "max_slippage_bps", "spot_quantity", "perp_quantity"):
        value = body[field]
        if value is not None and float(value) < 0.0:
            raise ExecutionProtocolError(f"{field} must be non-negative")
    if not 0.0 <= float(body["urgency"]) <= 1.0:
        raise ExecutionProtocolError("urgency must be between 0 and 1")
    if not 0.0 < float(body["exposure_scale"]) <= 1.0:
        raise ExecutionProtocolError("exposure_scale must be between 0 and 1")
    direction = body["direction"]
    if direction not in {None, "long", "short"}:
        raise ExecutionProtocolError("direction must be long, short, or absent")

    for required_context in (
        "account_id",
        "environment",
        "strategy_id",
        "cycle_id",
        "config_version_hash",
    ):
        if not body[required_context]:
            raise ExecutionProtocolError(f"{required_context} is required")

    if created_at_ms is not None and (
        isinstance(created_at_ms, bool) or not isinstance(created_at_ms, Integral)
    ):
        raise ExecutionProtocolError("created_at_ms must be an integer")
    created = int(time.time() * 1000) if created_at_ms is None else int(created_at_ms)
    if created <= 0:
        raise ExecutionProtocolError("created_at_ms must be positive")
    envelope = dict(body)
    envelope.update(
        {
            "producer_id": producer_id.strip(),
            "sequence": int(sequence),
            "created_at_ms": created,
            "deadline_at_ms": created + int(ttl_ms),
        }
    )
    envelope["command_hash"] = command_hash(envelope)
    return envelope


def validate_ack(event: dict[str, Any]) -> tuple[str, str]:
    """Validate an ACK and return ``(intent_id, ack_status)``."""

    unknown = set(event) - _ACK_FIELDS
    if unknown:
        names = ", ".join(sorted(str(key) for key in unknown))
        raise ExecutionProtocolError(f"unknown ACK field(s): {names}")
    ack_version = event.get("schema_version")
    if (
        isinstance(ack_version, bool)
        or not isinstance(ack_version, Integral)
        or int(ack_version) != EXECUTION_PROTOCOL_VERSION
    ):
        raise ExecutionProtocolError("unsupported ACK schema_version")
    intent_id = _string(event.get("intent_id"), "ACK intent_id").strip()
    if not intent_id:
        raise ExecutionProtocolError("ACK is missing intent_id")
    status = _string(event.get("ack_status"), "ACK ack_status").upper()
    if status not in ACK_STATES:
        raise ExecutionProtocolError(f"unsupported ACK state {status!r}")
    ack_hash = _string(event.get("command_hash"), "ACK command_hash")
    if len(ack_hash) != 64 or any(ch not in "0123456789abcdef" for ch in ack_hash):
        raise ExecutionProtocolError("ACK has an invalid command_hash")
    event_name = _string(event.get("event"), "ACK event")
    if event_name == "ConfigAck":
        declared_hash = _string(
            event.get("declared_config_hash"), "declared_config_hash"
        )
        applied_hash = _string(
            event.get("applied_config_hash"), "applied_config_hash"
        )
        config_status = _string(event.get("config_status"), "config_status").upper()
        if (
            len(declared_hash) != 64
            or any(ch not in "0123456789abcdef" for ch in declared_hash)
        ):
            raise ExecutionProtocolError("ConfigAck has an invalid declared_config_hash")
        if declared_hash != _string(
            event.get("config_version_hash"), "config_version_hash"
        ):
            raise ExecutionProtocolError("ConfigAck declared hash conflicts with command context")
        if config_status == "APPLIED":
            if status != "TERMINAL" or applied_hash != declared_hash:
                raise ExecutionProtocolError("applied ConfigAck has inconsistent status or hash")
        elif config_status == "REJECTED":
            if status != "REJECTED":
                raise ExecutionProtocolError("rejected ConfigAck must use REJECTED ACK state")
            if applied_hash and (
                len(applied_hash) != 64
                or any(ch not in "0123456789abcdef" for ch in applied_hash)
            ):
                raise ExecutionProtocolError("ConfigAck has an invalid applied_config_hash")
        else:
            raise ExecutionProtocolError(f"unsupported config ACK state {config_status!r}")
    return intent_id, status
