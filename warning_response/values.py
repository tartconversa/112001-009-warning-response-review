"""Controlled vocabularies for input values.

The application deliberately maps raw input values onto a small set of
semantic states.  The distinctions matter for review:

* ``delivered`` means the channel reported success, **not** that a resident
  received or understood the warning.
* ``acknowledged`` means a resident confirmed.  A confirmation without a
  successful send is treated as ambiguous rather than as coverage.
* A sent message with no confirmation is *not* counted as an acknowledged
  delivery.
"""

from __future__ import annotations

STATUS_UNKNOWN = "unknown"
STATUS_AMBIGUOUS = "ambiguous"

# Delivery / receipt statuses.
DELIVERY_SENT = "sent"              # channel accepted the message
DELIVERY_DELIVERED = "delivered"    # channel reported delivery to the handset
DELIVERY_ACKNOWLEDGED = "acknowledged"  # a resident explicitly confirmed
DELIVERY_FAILED = "failed"          # channel reported failure
DELIVERY_UNSUBSCRIBED = "unsubscribed"  # resident is not reachable by this channel
DELIVERY_NOT_SENT = "not_sent"      # campaign recorded but never dispatched

DELIVERY_STATUSES = {
    DELIVERY_SENT,
    DELIVERY_DELIVERED,
    DELIVERY_ACKNOWLEDGED,
    DELIVERY_FAILED,
    DELIVERY_UNSUBSCRIBED,
    DELIVERY_NOT_SENT,
    STATUS_AMBIGUOUS,
    STATUS_UNKNOWN,
}

# Statuses that indicate a message reached the channel successfully.  These
# are *not* proof that a resident saw or confirmed the warning.
DELIVERY_REACHED_CHANNEL = {DELIVERY_DELIVERED, DELIVERY_ACKNOWLEDGED}
# Statuses that represent a confirmed human acknowledgement.
DELIVERY_CONFIRMED = {DELIVERY_ACKNOWLEDGED}
DELIVERY_UNREACHABLE = {DELIVERY_FAILED, DELIVERY_UNSUBSCRIBED, DELIVERY_NOT_SENT,
                        STATUS_AMBIGUOUS, STATUS_UNKNOWN}

_DELIVERY_ALIASES = {
    "sent": DELIVERY_SENT,
    "queued": DELIVERY_SENT,
    "submitted": DELIVERY_SENT,
    "delivered": DELIVERY_DELIVERED,
    "success": DELIVERY_DELIVERED,
    "ok": DELIVERY_DELIVERED,
    "acknowledged": DELIVERY_ACKNOWLEDGED,
    "ack": DELIVERY_ACKNOWLEDGED,
    "confirmed": DELIVERY_ACKNOWLEDGED,
    "replied": DELIVERY_ACKNOWLEDGED,
    "read": DELIVERY_ACKNOWLEDGED,
    "failed": DELIVERY_FAILED,
    "error": DELIVERY_FAILED,
    "bounced": DELIVERY_FAILED,
    "unsubscribed": DELIVERY_UNSUBSCRIBED,
    "opted_out": DELIVERY_UNSUBSCRIBED,
    "not_sent": DELIVERY_NOT_SENT,
    "pending": DELIVERY_NOT_SENT,
    "skipped": DELIVERY_NOT_SENT,
    "unknown": STATUS_UNKNOWN,
    "": STATUS_UNKNOWN,
    # Ambiguous statuses: the channel accepted the request but there is no
    # evidence about the final outcome.
    "sent?": STATUS_AMBIGUOUS,
    "delivery_unknown": STATUS_AMBIGUOUS,
    "unclear": STATUS_AMBIGUOUS,
    "maybe": STATUS_AMBIGUOUS,
    "n/a": STATUS_AMBIGUOUS,
}


def resolve_delivery_status(raw: str) -> str:
    return _DELIVERY_ALIASES.get((raw or "").strip().lower(), STATUS_UNKNOWN)


# Request statuses.
REQUEST_OPEN = "open"
REQUEST_IN_PROGRESS = "in_progress"
REQUEST_RESOLVED = "resolved"
REQUEST_UNRESOLVED = "unresolved"

REQUEST_STATUSES = {REQUEST_OPEN, REQUEST_IN_PROGRESS, REQUEST_RESOLVED,
                    REQUEST_UNRESOLVED, STATUS_UNKNOWN}

_REQUEST_ALIASES = {
    "open": REQUEST_OPEN,
    "new": REQUEST_OPEN,
    "reported": REQUEST_OPEN,
    "in_progress": REQUEST_IN_PROGRESS,
    "assigned": REQUEST_IN_PROGRESS,
    "dispatched": REQUEST_IN_PROGRESS,
    "acknowledged": REQUEST_IN_PROGRESS,
    "resolved": REQUEST_RESOLVED,
    "closed": REQUEST_RESOLVED,
    "completed": REQUEST_RESOLVED,
    "unresolved": REQUEST_UNRESOLVED,
    "failed": REQUEST_UNRESOLVED,
    "cancelled": REQUEST_UNRESOLVED,
    "unable": REQUEST_UNRESOLVED,
    "unknown": STATUS_UNKNOWN,
    "": STATUS_UNKNOWN,
}


def resolve_request_status(raw: str) -> str:
    return _REQUEST_ALIASES.get((raw or "").strip().lower(), STATUS_UNKNOWN)


# Severities, ordered from least to most severe.
SEVERITY_RANK = {
    "advisory": 1,
    "watch": 2,
    "warning": 3,
    "severe": 4,
    "emergency": 5,
}
SEVERITIES = set(SEVERITY_RANK)

_SEVERITY_ALIASES = {
    "advisory": "advisory",
    "blue": "advisory",
    "info": "advisory",
    "note": "advisory",
    "watch": "watch",
    "yellow": "watch",
    "warning": "warning",
    "orange": "warning",
    "severe": "severe",
    "red": "severe",
    "emergency": "emergency",
    "black": "emergency",
    "highest": "emergency",
}


def resolve_severity(raw: str) -> str:
    return _SEVERITY_ALIASES.get((raw or "").strip().lower(), STATUS_UNKNOWN)


def severity_rank(value: str) -> int:
    return SEVERITY_RANK.get(value, 0)


# Request needs with a hierarchy: exposure to a hazard without help to move
# is more urgent than a general information request.
NEED_ORDER = {
    "information": 0,
    "supplies": 1,
    "medical": 2,
    "mobility": 3,
    "evacuation": 4,
    "rescue": 5,
}
NEED_ALIASES = {
    "info": "information",
    "information": "information",
    "supplies": "supplies",
    "food": "supplies",
    "water": "supplies",
    "medical": "medical",
    "medicine": "medical",
    "health": "medical",
    "mobility": "mobility",
    "transport": "mobility",
    "evacuation": "evacuation",
    "evacuate": "evacuation",
    "rescue": "rescue",
    "trapped": "rescue",
}


def resolve_need(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value in NEED_ORDER:
        return value
    return NEED_ALIASES.get(value, "information")


def need_rank(value: str) -> int:
    return NEED_ORDER.get(value, 0)


# Population granularity.  ``aggregate`` (or any of the area-level values)
# means the data can only support group statements, never "we identified
# person X".
GRANULARITY_LEVELS = {
    "aggregate": "aggregate",
    "district": "district",
    "neighbourhood": "neighbourhood",
    "street": "street",
    "roster": "roster",
    "individual": "individual",
    "named": "individual",
}
GRANULARITY_VALUES = set(GRANULARITY_LEVELS.values())
AGGREGATE_GRANULARITIES = {"aggregate", "district", "neighbourhood", "street"}
INDIVIDUAL_GRANULARITIES = {"roster", "individual"}

_GRANULARITY_ALIASES = {
    "aggregate": "aggregate",
    "group": "aggregate",
    "population": "aggregate",
    "district": "district",
    "town": "district",
    "county": "district",
    "neighbourhood": "neighbourhood",
    "neighborhood": "neighbourhood",
    "community": "neighbourhood",
    "street": "street",
    "block": "street",
    "roster": "roster",
    "household": "roster",
    "individual": "individual",
    "person": "individual",
    "named": "individual",
}


def resolve_granularity(raw: str) -> str:
    return _GRANULARITY_ALIASES.get((raw or "").strip().lower(), STATUS_UNKNOWN)


def is_aggregate(granularity: str) -> bool:
    return granularity in AGGREGATE_GRANULARITIES


# Task types and priorities.
TASK_CONTACT = "contact_community"
TASK_SHELTER = "shelter_capacity"
TASK_ASSIST = "assist_household"
TASK_VERIFY = "verify_acknowledgement"
TASK_TYPES = {TASK_CONTACT, TASK_SHELTER, TASK_ASSIST, TASK_VERIFY}

URGENCY_CRITICAL = "critical"
URGENCY_HIGH = "high"
URGENCY_MEDIUM = "medium"
URGENCY_LOW = "low"
URGENCY_RANK = {URGENCY_CRITICAL: 4, URGENCY_HIGH: 3, URGENCY_MEDIUM: 2, URGENCY_LOW: 1}
