"""In-process, evidence-first A2A workflow for L3B cases."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

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
                self.state.evidence[tool] = response
                ref = response["evidence_ref"]
                if ref not in self.state.all_refs:
                    self.state.all_refs.append(ref)
                self.trace.emit(
                    case_id=self.state.case_id, event_type="tool_result_consumed",
                    actor=actor, tool_name=tool, evidence_refs=[ref],
                )
                return response
            except (TimeoutError, ConnectionError) as exc:
                if attempt == 0:
                    continue
                self.state.errors.append(f"{tool}: {type(exc).__name__}")
            except (RuntimeError, ValueError, TypeError) as exc:
                self.state.errors.append(f"{tool}: {type(exc).__name__}")
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
    for row in _maps(data):
        for key in ("payments", "payment_rows"):
            value = row.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


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
        corroborated = candidate in history_ids or customer == state.customer_hint
        if state.customer_hint and not corroborated:
            if customer and candidate not in state.rejected:
                state.rejected.append(candidate)
            continue
        matches.append((candidate, customer))
        if candidate == state.claimed_id and corroborated:
            break
    if len(matches) == 1:
        state.order_id, order_customer = matches[0]
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
    status = str(_field(data, "shipment_status", "status") or "").lower()
    if status in {"lost", "returned"}:
        return status, [], True
    delivered_late = _is_after(
        _field(data, "order_delivered_customer_date", "delivered_at"),
        _field(data, "order_estimated_delivery_date", "estimated_delivery_at"),
    )
    seller_late = _is_after(
        _field(data, "order_delivered_carrier_date", "carrier_handoff_at"),
        _field(data, "shipping_limit_date")
        or _field(state.data("get_order_items"), "shipping_limit_date"),
    )
    if seller_late:
        return (
            "seller_delay", _ids(state.data("get_order_items"), "seller_id"),
            delivered_late is not None,
        )
    if delivered_late is True:
        return "logistics_delay", [], True
    if delivered_late is False:
        return "on_time", [], True
    return "insufficient_evidence", [], False


def _payment(state: CaseState) -> tuple[str, Decimal | None, Decimal | None, Decimal | None]:
    base, timeline, refunds = (
        state.data("get_order_payments"),
        state.data("get_payment_timeline"),
        state.data("get_refund_timeline"),
    )
    captured = _money(_field(timeline, "captured_total_brl", "captured_total"))
    if captured is None:
        captured = _money(_field(base, "captured_total_brl", "captured_total"))
    refunded = _money(_field(refunds, "refunded_total_brl", "refunded_total"))
    remaining = max(Decimal(0), captured - refunded) if (
        captured is not None and refunded is not None
    ) else None
    status = str(_field(refunds, "refund_status", "status") or "").lower()
    if status in {"failed", "failure"}:
        verdict = "refund_failed"
    elif status in {"pending", "processing", "initiated"}:
        verdict = "refund_pending"
    elif status in {"refunded", "completed", "succeeded"}:
        verdict = "refunded"
    elif _field(timeline, "duplicate_capture") is True:
        verdict = "duplicate_capture"
    elif _field(timeline, "capture_mismatch") is True:
        verdict = "capture_mismatch"
    elif captured is not None and refunded is not None:
        verdict = "reconciled"
    else:
        verdict = "insufficient_evidence"
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
            "selected_source": "get_order", "resolution_code": "authoritative_order",
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
            "selected_source": "get_payment_timeline",
            "resolution_code": "authoritative_payment_timeline",
        })
    return conflicts


def _claim(
    state: CaseState, topic: str, order_status: str, shipment: str, payment: str,
    captured: Decimal | None, remaining: Decimal | None,
) -> tuple[str, list[str]]:
    if topic in {"late_delivery_seller", "late_delivery_logistics"}:
        expected = "seller_delay" if topic.endswith("seller") else "logistics_delay"
        if shipment == "insufficient_evidence":
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
        if not order_status or remaining is None or state.data("get_policy") is None:
            return "insufficient_evidence", [
                "get_order", "get_refund_timeline", "get_policy"
            ]
        eligible = order_status in {"canceled", "unavailable"} and (
            remaining is not None and remaining > 0
        )
        return ("supported" if eligible else "unsupported",
                ["get_order", "get_refund_timeline", "get_policy"])
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
        if not resolved or not refs or (
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
    refund = Decimal(0)
    if primary in {"canceled_order_paid", "unavailable_order_paid", "refund_failed"} and (
        refundable is not None and state.data("get_policy") is not None
    ):
        refund = refundable
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
        "evidence_refs": state.refs(), "data_conflicts": _conflicts(state),
        "financial_resolution": {
            "currency": "BRL", "recommended_refund_brl": float(refund),
            "refund_lines": [{
                "reason_code": primary, "amount_brl": float(refund),
                "entity_id": state.order_id,
            }] if refund > 0 else [],
        },
        "resolution_actions": [
            "investigate_missing_evidence" if status == "needs_investigation" else (
                "issue_refund" if refund > 0 else (
                    "review_case" if status == "action_required" else "no_action"
                )
            )
        ],
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
