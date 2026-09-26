"""In-process, evidence-first A2A workflow for L3B cases."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx2

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

logger = logging.getLogger(__name__)

OWNERS = {
    "get_order": "entity-agent",
    "get_customer_history": "entity-agent",
    "get_order_items": "order-agent",
    "get_product_context": "order-agent",
    "get_shipment_summary": "shipment-agent",
    "get_order_payments": "payment-agent",
    "get_payment_timeline": "payment-agent",
    "get_refund_timeline": "payment-agent",
    "get_policy": "policy-agent",
}
ISSUES = {
    "canceled_order_paid", "unavailable_order_paid", "late_delivery_seller",
    "late_delivery_logistics", "valid_split_payment", "payment_mismatch",
    "duplicate_charge", "refund_pending", "refund_failed", "unsupported_claim",
}


@dataclass
class CaseState:
    case: dict[str, Any]
    claims: list[dict[str, str]]
    candidates: list[str]
    claimed_id: str | None
    customer_hint: str | None
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    all_refs: list[str] = field(default_factory=list)
    attempted: set[tuple[str, tuple[tuple[str, str], ...]]] = field(default_factory=set)
    errors: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    order_id: str | None = None
    customer_id: str | None = None
    entity_confidence: float = 0.0

    @property
    def case_id(self) -> str:
        return self.case["case_id"]

    def data(self, tool: str) -> Any:
        return self.evidence.get(tool, {}).get("data")

    def refs(self, *tools: str) -> list[str]:
        if not tools:
            return self.all_refs[:30]
        return list(dict.fromkeys(
            self.evidence[tool]["evidence_ref"] for tool in tools if tool in self.evidence
        ))[:30]


class EvidenceClient:
    """Single-case MCP access with tool permissions, cache and bounded retry."""

    def __init__(self, state: CaseState, gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.state = state
        self.gateway = gateway
        self.trace = trace
        self.cache: dict[tuple[str, tuple[tuple[str, str], ...]], dict[str, Any]] = {}

    async def call(self, actor: str, tool: str, **kwargs: str) -> dict[str, Any] | None:
        if OWNERS[tool] != actor:
            raise ValueError(f"{actor} cannot use {tool}")
        key = (tool, tuple(sorted(kwargs.items())))
        if key in self.state.attempted:
            return self.cache.get(key)
        self.state.attempted.add(key)
        for attempt in range(2):
            try:
                response = await self.gateway.call(tool, case_id=self.state.case_id, **kwargs)
                if not isinstance(response, dict) or not isinstance(
                    response.get("evidence_ref"), str
                ):
                    raise ValueError("MCP response has no evidence_ref")
                self.cache[key] = response
                if tool != "get_order":
                    self.state.evidence[tool] = response
                ref = response["evidence_ref"]
                if ref not in self.state.all_refs:
                    self.state.all_refs.append(ref)
                self.trace.emit(
                    case_id=self.state.case_id, event_type="tool_result_consumed",
                    actor=actor, tool_name=tool, evidence_refs=[ref],
                )
                return response
            except (TimeoutError, ConnectionError, httpx2.TransportError) as exc:
                if attempt == 0:
                    continue
                self.state.errors.append(f"{tool}: {type(exc).__name__}: {exc}")
                logger.warning("%s: %s", self.state.case_id, self.state.errors[-1])
            except RuntimeError as exc:
                if attempt == 0 and "Error executing tool" in str(exc):
                    continue
                self.state.errors.append(f"{tool}: {type(exc).__name__}: {exc}")
                logger.warning("%s: %s", self.state.case_id, self.state.errors[-1])
                break
            except (ValueError, TypeError) as exc:
                self.state.errors.append(f"{tool}: {type(exc).__name__}: {exc}")
                logger.warning("%s: %s", self.state.case_id, self.state.errors[-1])
                break
        return None


def _maps(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        return [value, *(row for item in value.values() for row in _maps(item))]
    if isinstance(value, list):
        return [row for item in value for row in _maps(item)]
    return []


def _field(value: Any, *keys: str) -> Any:
    for row in _maps(value):
        for key in keys:
            item = row.get(key)
            if item is not None and item != "" and item != []:
                return item
    return None


def _ids(value: Any, *keys: str) -> list[str]:
    result: list[str] = []
    for row in _maps(value):
        for key in keys:
            item = row.get(key)
            values = item if isinstance(item, list) else [item]
            for identifier in values:
                if isinstance(identifier, str) and identifier and identifier not in result:
                    result.append(identifier)
    return result[:20]


def _money(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() and amount >= 0 else None
    except InvalidOperation:
        return None


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_after(left: Any, right: Any) -> bool | None:
    a, b = _date(left), _date(right)
    if a is None or b is None:
        return None
    try:
        return a > b
    except TypeError:
        return None


def _payment_rows(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    for row in _maps(data):
        for key in ("payments", "payment_rows"):
            value = row.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _event_total(data: Any, event_types: set[str]) -> Decimal | None:
    """Count confirmed events only; unavailable timelines are not zero refunds."""
    if not isinstance(data, dict) or not isinstance(data.get("events"), list):
        return None
    total = Decimal(0)
    seen: dict[str, dict[str, Any]] = {}
    for event in data["events"]:
        if not isinstance(event, dict):
            return None
        if event.get("event_type") not in event_types:
            continue
        if event.get("status") not in {"confirmed", "completed", "succeeded"}:
            return None
        event_id = event.get("event_id")
        if isinstance(event_id, str):
            if event_id in seen:
                if seen[event_id] != event:
                    return None
                continue
            seen[event_id] = event
        amount = _money(event.get("amount_brl"))
        if amount is None:
            return None
        total += amount
    return total


def _policy_rule(state: CaseState, issue: str) -> dict[str, Any] | None:
    data = state.data("get_policy")
    if not isinstance(data, dict) or data.get("currency") != "BRL":
        return None
    if data.get("policy_version") != state.case.get("policy_version"):
        return None
    rules = data.get("rules")
    rule = rules.get(issue) if isinstance(rules, dict) else None
    if not isinstance(rule, dict) or _money(rule.get("refund_brl")) is None:
        return None
    if rule.get("case_status") not in {"action_required", "no_action", "needs_investigation"}:
        return None
    action = rule.get("recommended_action")
    if not isinstance(action, str) or not 1 <= len(action) <= 80:
        return None
    return rule


async def _resolve(state: CaseState, client: EvidenceClient) -> None:
    if state.customer_hint:
        await client.call(
            "entity-agent", "get_customer_history", customer_unique_id=state.customer_hint
        )
    history_ids = _ids(state.data("get_customer_history"), "order_id", "order_ids")
    matches: list[tuple[str, str | None]] = []
    for candidate in state.candidates[:5]:
        response = await client.call("entity-agent", "get_order", order_id=candidate)
        data = response.get("data") if response else None
        actual = _field(data, "order_id")
        if actual != candidate:
            if response and candidate not in state.rejected:
                state.rejected.append(candidate)
            continue
        customer = _field(data, "customer_unique_id")
        customer = customer if isinstance(customer, str) and customer else None
        corroborated = candidate in history_ids or (
            state.customer_hint is not None and customer == state.customer_hint
        )
        if state.customer_hint and not corroborated:
            if customer and candidate not in state.rejected:
                state.rejected.append(candidate)
            continue
        matches.append((candidate, customer))
        if candidate == state.claimed_id and corroborated:
            break
    if len(matches) == 1:
        state.order_id, order_customer = matches[0]
        state.evidence["get_order"] = client.cache[
            ("get_order", (("order_id", state.order_id),))
        ]
        state.customer_id = order_customer or (
            state.customer_hint if state.order_id in history_ids else None
        )
        state.entity_confidence = 0.98 if state.order_id in history_ids else 0.88
    elif len(matches) > 1:
        state.entity_confidence = 0.2


async def _specialists(state: CaseState, client: EvidenceClient, trace: TraceWriter) -> None:
    assert state.order_id is not None
    order_id = state.order_id
    trace.emit(case_id=state.case_id, event_type="task_assigned",
               actor="coordinator", target="order-agent")
    await client.call("order-agent", "get_order_items", order_id=order_id)
    if state.case.get("investigation_scope", {}).get("include_product_context"):
        await client.call("order-agent", "get_product_context", order_id=order_id)
    trace.emit(case_id=state.case_id, event_type="handoff", actor="order-agent",
               target="shipment-agent", decision_code="order_facts_ready")
    await client.call("shipment-agent", "get_shipment_summary", order_id=order_id)
    trace.emit(case_id=state.case_id, event_type="handoff", actor="shipment-agent",
               target="payment-agent", decision_code="shipment_facts_ready")
    await client.call("payment-agent", "get_order_payments", order_id=order_id)
    await client.call("payment-agent", "get_payment_timeline", order_id=order_id)
    await client.call("payment-agent", "get_refund_timeline", order_id=order_id)
    trace.emit(case_id=state.case_id, event_type="handoff", actor="payment-agent",
               target="policy-agent", decision_code="financial_facts_ready")
    version = state.case.get("policy_version")
    if isinstance(version, str) and version:
        await client.call("policy-agent", "get_policy", policy_version=version)


def _shipment(state: CaseState) -> tuple[str, list[str], bool]:
    data = state.data("get_shipment_summary")
    if data is None:
        return "insufficient_evidence", [], False
    if any(c["field"].startswith(("shipment", "order_items", "order_history"))
           for c in _conflicts(state)):
        return "conflicting", [], False
    status = str(_field(data, "shipment_status", "status") or "").lower()
    if status in {"lost", "returned"}:
        return status, [], True
    delivered_late = _is_after(
        _field(data, "order_delivered_customer_date", "delivered_at", "delivered_customer_at"),
        _field(data, "order_estimated_delivery_date", "estimated_delivery_at"),
    )
    # Compare each seller's own handoff with that seller's deadline.
    items = [row for row in _maps(state.data("get_order_items"))
             if isinstance(row.get("seller_id"), str)]
    sellers = set(_ids(items, "seller_id"))
    shipment_rows = [row for row in _maps(data) if "seller_id" in row
                     and any(k in row for k in ("carrier_handoff_at",
                                                "delivered_carrier_at",
                                                "order_delivered_carrier_date"))]
    checks: list[tuple[str, bool | None]] = []
    for item in items:
        seller = item["seller_id"]
        rows = [row for row in shipment_rows if row.get("seller_id") == seller]
        # An order-wide handoff is usable only for a single-seller order.
        if not rows and len(sellers) == 1:
            rows = [data]
        if not rows:
            checks.append((seller, None))
        for row in rows:
            checks.append((seller, _is_after(
                _field(row, "carrier_handoff_at", "order_delivered_carrier_date",
                       "delivered_carrier_at"),
                item.get("shipping_limit_date") or row.get("shipping_limit_date"),
            )))
    late_sellers = sorted({seller for seller, late in checks if late is True})
    complete = bool(checks) and all(late is not None for _, late in checks)
    complete = complete and delivered_late is not None
    if late_sellers:
        return "seller_delay", late_sellers, complete
    if delivered_late is True and complete:
        return "logistics_delay", [], True
    if delivered_late is False:
        return "on_time", [], complete
    return "insufficient_evidence", [], False


def _payment(state: CaseState) -> tuple[str, Decimal | None, Decimal | None, Decimal | None]:
    base, timeline, refunds = (
        state.data("get_order_payments"),
        state.data("get_payment_timeline"),
        state.data("get_refund_timeline"),
    )
    captured = _money(_field(timeline, "captured_total_brl", "captured_total"))
    if captured is None:
        captured = _event_total(timeline, {"captured"})
    if captured is None:
        captured = _money(_field(base, "captured_total_brl", "captured_total"))
    refunded = _money(_field(refunds, "refunded_total_brl", "refunded_total"))
    if refunded is None:
        refunded = _event_total(refunds, {"refunded", "refund_completed"})
    if captured is not None and refunded is not None and refunded > captured:
        return "capture_mismatch", captured, refunded, None
    remaining = captured - refunded if (
        captured is not None and refunded is not None
    ) else None
    status = str(refunds.get("refund_status", refunds.get("status", ""))).lower() \
        if isinstance(refunds, dict) else ""
    if _field(timeline, "duplicate_capture") is True:
        verdict = "duplicate_capture"
    elif _field(timeline, "capture_mismatch") is True:
        verdict = "capture_mismatch"
    elif status in {"failed", "failure"}:
        verdict = "refund_failed"
    elif status in {"pending", "processing", "initiated"}:
        verdict = "refund_pending"
    elif status in {"refunded", "completed", "succeeded"}:
        verdict = "refunded"
    elif captured is not None and refunded is not None:
        verdict = "refunded" if captured > 0 and refunded == captured else "reconciled"
    else:
        verdict = "insufficient_evidence"
    if any(c["field"].startswith(("payment", "captured")) for c in _conflicts(state)):
        return "insufficient_evidence", captured, refunded, None
    return verdict, captured, refunded, remaining


def _conflicts(state: CaseState) -> list[dict[str, Any]]:
    """Report only disagreements on the same field from two consumed sources."""
    conflicts: list[dict[str, Any]] = []
    order_status = _field(state.data("get_order"), "order_status")
    shipment_order_status = _field(state.data("get_shipment_summary"), "order_status")
    if order_status is not None and shipment_order_status is not None and (
        order_status != shipment_order_status
    ):
        conflicts.append({
            "field": "order_status", "sources": ["get_order", "get_shipment_summary"],
            "selected_source": None, "resolution_code": "unresolved_source_conflict",
        })
    base_capture = _money(_field(
        state.data("get_order_payments"), "captured_total_brl", "captured_total"
    ))
    timeline_capture = _money(_field(
        state.data("get_payment_timeline"), "captured_total_brl", "captured_total"
    ))
    if base_capture is not None and timeline_capture is not None and (
        base_capture != timeline_capture
    ):
        conflicts.append({
            "field": "captured_total_brl",
            "sources": ["get_order_payments", "get_payment_timeline"],
            "selected_source": None,
            "resolution_code": "unresolved_source_conflict",
        })
    def conflict(field: str, sources: list[str]) -> None:
        conflicts.append({"field": field, "sources": sources,
                          "selected_source": None, "resolution_code": "unresolved_source_conflict"})

    groups = [
        ("order_history", "get_customer_history", "order_id"),
        ("order_items", "get_order_items", "order_item_id"),
        ("payment_rows", "get_order_payments", "payment_sequential"),
    ]
    for field_name, tool, key in groups:
        seen: dict[str, tuple[int, dict[str, Any]]] = {}
        for index, row in enumerate(_maps(state.data(tool))):
            identifier = row.get(key)
            if identifier is None or (
                state.order_id and row.get("order_id") not in {None, state.order_id}
            ):
                continue
            identifier = str(identifier)
            if identifier in seen and seen[identifier][1] != row:
                conflict(field_name, [f"{tool}[{seen[identifier][0]}]", f"{tool}[{index}]"])
                break
            seen[identifier] = (index, row)
    shipment = state.data("get_shipment_summary")
    if isinstance(shipment, dict):
        late = _is_after(_field(shipment, "delivered_customer_at", "delivered_at",
                                "order_delivered_customer_date"),
                         _field(shipment, "estimated_delivery_at", "order_estimated_delivery_date"))
        if late is False and any(row.get("event_type") == "delivered_late"
                                 and row.get("status") == "confirmed"
                                 for row in _maps(shipment.get("events"))):
            conflict("shipment_delivery", ["get_shipment_summary.summary",
                                           "get_shipment_summary.events"])
    return conflicts[:5]


def _claim(
    state: CaseState, topic: str, order_status: str, shipment: str, payment: str,
    captured: Decimal | None, remaining: Decimal | None,
) -> tuple[str, list[str]]:
    if topic in {"late_delivery_seller", "late_delivery_logistics"}:
        expected = "seller_delay" if topic.endswith("seller") else "logistics_delay"
        if shipment in {"insufficient_evidence", "conflicting"}:
            return "insufficient_evidence", ["get_shipment_summary", "get_order_items"]
        return ("supported" if shipment == expected else "unsupported",
                ["get_shipment_summary", "get_order_items"])
    if topic in {"canceled_order_paid", "unavailable_order_paid"}:
        expected = "canceled" if topic.startswith("canceled") else "unavailable"
        if not order_status or captured is None:
            return "insufficient_evidence", [
                "get_order", "get_order_payments", "get_payment_timeline"
            ]
        return ("supported" if order_status == expected and captured is not None
                and captured > 0 else "unsupported",
                ["get_order", "get_order_payments", "get_payment_timeline"])
    expected_payment = {
        "duplicate_charge": "duplicate_capture", "payment_mismatch": "capture_mismatch",
        "refund_pending": "refund_pending", "refund_failed": "refund_failed",
    }
    if topic in expected_payment:
        if payment == "insufficient_evidence":
            return "insufficient_evidence", [
                "get_order_payments", "get_payment_timeline", "get_refund_timeline"
            ]
        return ("supported" if payment == expected_payment[topic] else "unsupported",
                ["get_order_payments", "get_payment_timeline", "get_refund_timeline"])
    if topic == "valid_split_payment":
        rows = _payment_rows(state.data("get_order_payments"))
        values = [_money(row.get("payment_value")) for row in rows]
        supported = (
            len(rows) > 1 and all(value is not None for value in values)
            and captured is not None and sum(values, Decimal(0)) == captured
            and payment == "reconciled"
        )
        return ("supported" if supported else "insufficient_evidence",
                ["get_order_payments", "get_payment_timeline"])
    if topic == "requested_full_refund":
        tools = ["get_order", "get_payment_timeline", "get_refund_timeline", "get_policy"]
        if remaining == 0:
            return "unsupported", tools
        if remaining is None or _conflicts(state):
            return "insufficient_evidence", tools
        issue = {"canceled": "canceled_order_paid", "unavailable": "unavailable_order_paid"}.get(
            order_status
        )
        if issue is None:
            return "insufficient_evidence", tools
        rule = _policy_rule(state, issue)
        if rule is None:
            return "insufficient_evidence", tools
        approved = _money(rule["refund_brl"])
        return ("supported" if approved == remaining else "unsupported"), tools
    if topic == "unsupported_claim":
        clear = (
            order_status == "delivered" and shipment == "on_time"
            and payment == "reconciled" and state.data("get_policy") is not None
        )
        return ("supported" if clear else "insufficient_evidence",
                ["get_order", "get_shipment_summary", "get_payment_timeline", "get_policy"])
    return "insufficient_evidence", []


def _build(state: CaseState) -> dict[str, Any]:
    resolved = [state.order_id] if state.order_id else []
    order_status = str(_field(state.data("get_order"), "order_status", "status") or "").lower()
    shipment, late_sellers, timeline_complete = _shipment(state)
    payment, captured, refunded, refundable = _payment(state)
    assessments: list[dict[str, Any]] = []
    for item in state.claims[:5]:
        verdict, tools = _claim(
            state, item["topic"], order_status, shipment, payment, captured, refundable
        )
        refs = state.refs(*tools) if tools else []
        if _conflicts(state) or not resolved or not refs or (
            item["topic"].startswith("late_delivery") and not timeline_complete
        ):
            verdict = "insufficient_evidence"
        assessments.append({
            "claim_id": item["claim_id"], "verdict": verdict,
            "confidence": 0.85 if verdict == "supported" else (
                0.7 if verdict == "unsupported" else 0.2
            ), "evidence_refs": refs,
        })
    supported = [claim["topic"] for claim, result in zip(state.claims, assessments, strict=True)
                 if result["verdict"] == "supported" and claim["topic"] in ISSUES]
    primary = supported[0] if supported else (
        "unsupported_claim" if resolved and assessments and all(
            result["verdict"] == "unsupported" for result in assessments
        ) else "insufficient_evidence"
    )
    status = ("needs_investigation" if primary == "insufficient_evidence" else
              "no_action" if primary in {"unsupported_claim", "valid_split_payment"} else
              "action_required")
    conflicts = _conflicts(state)
    refund = Decimal(0)
    rule = _policy_rule(state, primary)
    action = "investigate_missing_evidence"
    if rule is not None and not conflicts:
        proposed = _money(rule["refund_brl"])
        if proposed == 0 or (refundable is not None and proposed <= refundable):
            refund = proposed
            status = rule["case_status"]
            action = rule["recommended_action"]
        else:
            status = "needs_investigation"
    else:
        status = "needs_investigation"
    causes = [] if primary in {"insufficient_evidence", "unsupported_claim"} else [
        {"cause_code": primary.upper(), "rank": 1}
    ]
    party = {
        "late_delivery_seller": "seller",
        "late_delivery_logistics": "logistics_provider",
        "payment_mismatch": "payment_provider",
        "duplicate_charge": "payment_provider",
    }.get(primary, "platform")
    parties = [{"party_type": party, "party_id": (
        late_sellers[0] if party == "seller" and late_sellers else None
    )}] if causes else []
    if rule is not None and not conflicts:
        policy_parties = rule.get("responsible_parties")
        if isinstance(policy_parties, list) and len(policy_parties) <= 5 and all(
            isinstance(p, dict) and set(p) == {"party_type", "party_id"}
            and p["party_type"] in {"seller", "platform", "logistics_provider",
                                    "payment_provider", "customer", "unknown"}
            and (p["party_id"] is None or
                 isinstance(p["party_id"], str) and len(p["party_id"]) <= 128)
            for p in policy_parties
        ):
            parties = policy_parties
    return {
        "schema_version": "day09-l3b-output-v2", "case_id": state.case_id,
        "assessment": {
            "primary_issue": primary, "secondary_issues": list(dict.fromkeys(
                issue for issue in supported[1:] if issue != primary
            ))[:10], "case_status": status,
            "confidence": 0.15 if not resolved else (
                0.85 if primary != "insufficient_evidence" else 0.35
            ),
        },
        "affected_entities": {
            "order_ids": resolved,
            "item_ids": _ids(state.data("get_order_items"), "order_item_id", "item_id")
            if resolved else [],
            "seller_ids": _ids(state.data("get_order_items"), "seller_id") if resolved else [],
            "payment_references": _ids(
                state.data("get_order_payments"), "payment_reference", "payment_id"
            ) if resolved else [],
            "shipment_ids": _ids(state.data("get_shipment_summary"), "shipment_id")
            if resolved else [],
        },
        "claim_assessments": assessments,
        "entity_resolution": {
            "status": "resolved" if resolved else (
                "ambiguous" if state.candidates else "not_found"
            ), "resolved_order_ids": resolved,
            "rejected_candidates": state.rejected[:20],
            "confidence": state.entity_confidence,
        },
        "customer_context": {
            "customer_unique_id": state.customer_id,
            "related_order_ids": _ids(
                state.data("get_customer_history"), "order_id", "order_ids"
            ) if state.customer_id else [],
        },
        "shipment_analysis": {
            "verdict": shipment, "late_seller_ids": late_sellers,
            "timeline_complete": timeline_complete,
        },
        "payment_analysis": {
            "verdict": payment,
            "captured_total_brl": float(captured) if captured is not None else None,
            "refunded_total_brl": float(refunded) if refunded is not None else None,
            "refundable_total_brl": float(refundable) if refundable is not None else None,
        },
        "root_cause_analysis": {"ranked_causes": causes, "responsible_parties": parties},
        "evidence_refs": state.refs(), "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": float(refund),
            "refund_lines": [{
                "reason_code": primary, "amount_brl": float(refund),
                "entity_id": state.order_id,
            }] if refund > 0 else [],
        },
        "resolution_actions": [action],
    }


def _verify(state: CaseState, output: dict[str, Any], trace: TraceWriter) -> None:
    refs = set(state.refs())
    if output["case_id"] != state.case_id or set(output["evidence_refs"]) != refs:
        raise ValueError("case scope or evidence references changed")
    if any(not set(item["evidence_refs"]).issubset(refs)
           for item in output["claim_assessments"]):
        raise ValueError("a claim references unconsumed evidence")
    amount = Decimal(str(output["financial_resolution"]["recommended_refund_brl"]))
    lines = output["financial_resolution"]["refund_lines"]
    if amount != sum((Decimal(str(line["amount_brl"])) for line in lines), Decimal(0)):
        raise ValueError("refund lines do not reconcile")
    if state.order_id and _field(state.data("get_order"), "order_id") != state.order_id:
        raise ValueError("selected order does not match order evidence")
    payment = output["payment_analysis"]
    captured, refunded = payment["captured_total_brl"], payment["refunded_total_brl"]
    if (captured is not None and refunded is not None and refunded > captured
            and (payment["verdict"] != "capture_mismatch" or amount > 0)):
        raise ValueError("invalid payment reconciliation")
    if amount > 0:
        rule = _policy_rule(state, output["assessment"]["primary_issue"])
        remaining = _payment(state)[3]
        if (rule is None or amount != _money(rule["refund_brl"]) or remaining is None
                or amount > remaining or _conflicts(state)):
            raise ValueError("refund policy or available balance has not been verified")
    trace.contracts.validate_output(output, f"outputs/{state.case_id}.json")


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Investigate one scoped case and return a validated L3B output."""
    request = case.get("customer_request") or {}
    claims = [
        {"claim_id": item["claim_id"], "topic": item["topic"]}
        for item in request.get("claims", [])
        if isinstance(item, dict) and isinstance(item.get("claim_id"), str)
        and isinstance(item.get("topic"), str)
    ][:5]
    claimed = request.get("claimed_order_id")
    claimed = claimed if isinstance(claimed, str) and claimed else None
    candidates = list(dict.fromkeys(
        item for item in [claimed, *case.get("candidate_order_ids", [])]
        if isinstance(item, str) and item
    ))[:20]
    hint = case.get("customer_unique_id_hint")
    state = CaseState(
        case, claims, candidates, claimed,
        hint if isinstance(hint, str) and hint else None,
    )
    client = EvidenceClient(state, gateway, trace)
    trace.emit(case_id=state.case_id, event_type="task_assigned",
               actor="coordinator", target="entity-agent")
    await _resolve(state, client)
    if not state.all_refs and state.errors:
        raise RuntimeError(
            f"{state.case_id}: MCP returned no evidence; run stopped. "
            + " | ".join(state.errors)
        )
    trace.emit(case_id=state.case_id, event_type="handoff", actor="entity-agent",
               target="coordinator", decision_code="resolved" if state.order_id else "unresolved")
    if state.order_id:
        await _specialists(state, client, trace)
    if state.data("get_policy") is not None:
        trace.emit(
            case_id=state.case_id, event_type="policy_decided", actor="policy-agent",
            decision_code="evidence_reviewed",
        )
    trace.emit(
        case_id=state.case_id, event_type="task_assigned", actor="coordinator",
        target="conflict-agent",
    )
    output = _build(state)
    trace.emit(
        case_id=state.case_id, event_type="handoff", actor="conflict-agent",
        target="verifier", decision_code="conflicts_reviewed",
    )
    _verify(state, output, trace)
    trace.emit(
        case_id=state.case_id, event_type="verification_completed",
        actor="verifier", decision_code="validated", evidence_refs=state.refs()[:20],
    )
    return output
