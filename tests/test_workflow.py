import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest

from student_agent.mcp_gateway import EvidenceGateway
from student_agent.workflow import (
    CaseState,
    EvidenceClient,
    _build,
    _payment,
    _resolve,
    _shipment,
)


def state(**data):
    result = CaseState({"case_id": "TEST_CASE"}, [], [], None, None)
    result.evidence = {
        key: {"data": value, "evidence_ref": "ev_" + str(i).zfill(20)}
        for i, (key, value) in enumerate(data.items())
    }
    result.all_refs = [item["evidence_ref"] for item in result.evidence.values()]
    return result


class Trace:
    def emit(self, **kwargs):
        pass


def test_selected_order_keeps_its_own_evidence():
    class Gateway:
        async def call(self, tool, **kwargs):
            oid = kwargs.get("order_id")
            data = ({"order_ids": ["A"]} if tool == "get_customer_history" else
                    {"order_id": oid, "customer_unique_id": "C" if oid == "A" else "D"})
            return {"data": data, "evidence_ref": "ev_" + str(oid).ljust(20, "x")}
    s = state()
    s.candidates, s.customer_hint = ["A", "B"], "C"
    asyncio.run(_resolve(s, EvidenceClient(s, Gateway(), Trace())))
    assert s.order_id == "A"
    assert s.data("get_order")["order_id"] == "A"
    assert s.refs("get_order") == ["ev_" + "A".ljust(20, "x")]
    assert s.rejected == ["B"]


def test_ambiguous_orders_do_not_publish_candidate_facts():
    class Gateway:
        async def call(self, tool, **kwargs):
            return {"data": {"order_id": kwargs["order_id"]}, "evidence_ref": "ev_" + "x"*20}
    s = state()
    s.candidates, s.claimed_id = ["A", "B"], "A"
    asyncio.run(_resolve(s, EvidenceClient(s, Gateway(), Trace())))
    assert s.order_id is None
    assert s.data("get_order") is None


def test_missing_handoff_cannot_blame_logistics():
    s = state(get_shipment_summary={"delivered_at": "2020-01-12",
                                   "estimated_delivery_at": "2020-01-10"},
              get_order_items={"items": [{"seller_id": "S"}]})
    assert _shipment(s) == ("insufficient_evidence", [], False)


def test_only_late_seller_is_identified():
    s = state(get_shipment_summary={
        "delivered_at": "2020-01-12", "estimated_delivery_at": "2020-01-10",
        "shipments": [{"seller_id": "A", "carrier_handoff_at": "2020-01-06"},
                      {"seller_id": "B", "carrier_handoff_at": "2020-01-04"}]},
        get_order_items={"items": [
            {"seller_id": "A", "shipping_limit_date": "2020-01-05"},
            {"seller_id": "B", "shipping_limit_date": "2020-01-05"}]})
    assert _shipment(s) == ("seller_delay", ["A"], True)
    s.evidence["get_shipment_summary"]["data"]["shipments"][0]["carrier_handoff_at"] = "2020-01-04"
    assert _shipment(s) == ("logistics_delay", [], True)


def test_refund_exceeding_capture_is_not_reconciled():
    s = state(get_payment_timeline={"captured_total": 100},
              get_refund_timeline={"refunded_total": 150})
    verdict, captured, refunded, remaining = _payment(s)
    assert verdict == "capture_mismatch"
    assert (captured, refunded, remaining) == (100, 150, None)


def test_duplicate_capture_is_not_hidden_by_pending_refund():
    s = state(get_payment_timeline={"captured_total": 100, "duplicate_capture": True},
              get_refund_timeline={"refunded_total": 0, "status": "pending"})
    assert _payment(s)[0] == "duplicate_capture"


@pytest.mark.parametrize("policy", [{}, {"message": "No refunds allowed"}, None])
def test_uninterpreted_policy_never_authorizes_full_refund(policy):
    s = state(get_order={"order_id": "A", "order_status": "canceled"},
              get_payment_timeline={"captured_total": 100},
              get_refund_timeline={"refunded_total": 0}, get_policy=policy)
    s.order_id = "A"
    s.claims = [{"claim_id": "claim-1", "topic": "canceled_order_paid"},
                {"claim_id": "claim-2", "topic": "requested_full_refund"}]
    output = _build(s)
    assert output["financial_resolution"]["recommended_refund_brl"] == 0
    assert output["assessment"]["case_status"] == "needs_investigation"
    assert output["claim_assessments"][1]["verdict"] == "insufficient_evidence"


@pytest.mark.parametrize("failures", [1, 2])
def test_httpx_timeout_retry_is_bounded(failures):
    class Gateway:
        calls = 0
        async def call(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= failures:
                raise httpx2.ReadTimeout("timeout")
            return {"data": {}, "evidence_ref": "ev_" + "x"*20}
    s, gateway = state(), Gateway()
    client = EvidenceClient(s, gateway, Trace())
    result = asyncio.run(client.call("entity-agent", "get_order", order_id="A"))
    assert gateway.calls == 2
    assert (result is None) == (failures == 2)
    asyncio.run(client.call("entity-agent", "get_order", order_id="A"))
    assert gateway.calls == 2


@pytest.mark.parametrize("failures", [1, 2])
def test_mcp_execution_error_retry_is_bounded(failures):
    class Gateway:
        calls = 0

        async def call(self, *args, **kwargs):
            self.calls += 1
            if self.calls <= failures:
                raise RuntimeError("MCP tool get_order failed: Error executing tool get_order")
            return {"data": {}, "evidence_ref": "ev_" + "x" * 20}

    s, gateway = state(), Gateway()
    result = asyncio.run(
        EvidenceClient(s, gateway, Trace()).call("entity-agent", "get_order", order_id="A")
    )
    assert gateway.calls == 2
    assert (result is None) == (failures == 2)


def test_mcp_sdk_error_response_is_handled():
    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="get_policy")])

        async def call_tool(self, *args, **kwargs):
            return SimpleNamespace(is_error=True, content=[SimpleNamespace(text="tool failed")])
    with pytest.raises(RuntimeError, match="tool failed"):
        asyncio.run(EvidenceGateway(Session(), None).call("get_policy", case_id="TEST_CASE"))


def test_total_mcp_failure_stops_case_instead_of_returning_empty_output():
    from student_agent.workflow import solve_case

    class Gateway:
        async def call(self, *args, **kwargs):
            raise RuntimeError("Error executing tool get_order")

    case = {"case_id": "TEST_CASE", "candidate_order_ids": ["A"]}
    with pytest.raises(RuntimeError, match="MCP returned no evidence; run stopped"):
        asyncio.run(solve_case(case, Gateway(), Trace()))


def test_cli_reports_underlying_mcp_error(monkeypatch, capsys):
    from student_agent import cli

    async def failed_check(*args):
        raise ExceptionGroup("session", [RuntimeError("Error executing tool get_policy")])

    monkeypatch.setattr(cli, "_check_mcp", failed_check)
    monkeypatch.setattr("sys.argv", ["day09", "mcp-check"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    assert "Error executing tool get_policy" in capsys.readouterr().err


def test_mcp_check_preserves_existing_artifacts(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from student_agent import cli

    output = tmp_path / "outputs" / "CASE_001.json"
    output.parent.mkdir()
    output.write_text("existing output")
    trace = tmp_path / "traces" / "trace.jsonl"
    trace.parent.mkdir()
    trace.write_text("existing trace")

    class Gateway:
        async def list_tools(self):
            return ["get_order"]

        async def call(self, *args, **kwargs):
            raise RuntimeError("server error")

    @asynccontextmanager
    async def connect(*args):
        yield Gateway()

    monkeypatch.setattr(cli.Settings, "load", lambda root: SimpleNamespace(
        mcp_endpoint="https://example.invalid", team_api_key="test"))
    monkeypatch.setattr(cli, "load_case_set", lambda root: SimpleNamespace(
        cases={"CASE_001": {"customer_request": {"claimed_order_id": "ORDER_001"}}}))
    monkeypatch.setattr(cli, "connect_gateway", connect)
    with pytest.raises(RuntimeError, match="server error"):
        asyncio.run(cli._check_mcp(tmp_path, "CASE_001"))
    assert output.read_text() == "existing output"
    assert trace.read_text() == "existing trace"


def test_resume_keeps_completed_outputs_and_removes_incomplete_trace(tmp_path):
    import json

    from student_agent import cli
    from student_agent.contracts import Contracts
    from student_agent.trace import TraceWriter

    contracts = Contracts(Path("contracts/schemas"))
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    complete = state()
    complete.case["case_id"] = "CASE_001"
    output = _build(complete)
    (output_root / "CASE_001.json").write_text(json.dumps(output), encoding="utf-8")

    trace_path = tmp_path / "traces" / "trace.jsonl"
    trace = TraceWriter(trace_path, contracts)
    trace.emit(case_id="CASE_001", event_type="case_received", actor="coordinator")
    trace.emit(case_id="CASE_001", event_type="case_finalized", actor="coordinator")
    trace.emit(case_id="CASE_002", event_type="case_received", actor="coordinator")

    completed = cli._prepare_resume(
        output_root, trace_path, ("CASE_001", "CASE_002"), contracts
    )
    assert completed == {"CASE_001"}
    events = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines()]
    assert {event["case_id"] for event in events} == {"CASE_001"}
    assert (output_root / "CASE_001.json").exists()


def test_event_totals_and_unknown_refund_timeline():
    s = state(get_payment_timeline={"events": [
        {"event_type": "captured", "amount_brl": "89.00", "status": "confirmed"},
        {"event_type": "captured", "amount_brl": "16.00", "status": "confirmed"}]},
        get_refund_timeline={"events": [
            {"event_type": "refunded", "amount_brl": "5.00", "status": "completed"}]})
    assert _payment(s) == ("reconciled", 105, 5, 100)
    s.evidence.pop("get_refund_timeline")
    assert _payment(s) == ("insufficient_evidence", 105, None, None)


def test_repeated_event_id_does_not_double_count():
    event = {"event_id": "E1", "event_type": "captured",
             "amount_brl": "10", "status": "confirmed"}
    s = state(get_payment_timeline={"events": [event, event]},
              get_refund_timeline={"events": []})
    assert _payment(s)[1:] == (10, 0, 10)


def test_shipment_summary_conflict_with_event():
    s = state(get_shipment_summary={"delivered_customer_at": "2020-01-09",
        "estimated_delivery_at": "2020-01-10",
        "events": [{"event_type": "delivered_late", "status": "confirmed"}]})
    assert _shipment(s) == ("conflicting", [], False)
    output = _build(s)
    assert output["data_conflicts"][0]["selected_source"] is None


def test_duplicate_payment_rows_block_automatic_reconciliation():
    s = state(get_order_payments=[
        {"payment_sequential": "1", "payment_value": "89"},
        {"payment_sequential": "1", "payment_value": "16"}],
        get_payment_timeline={"captured_total": 105},
        get_refund_timeline={"refunded_total": 0})
    assert _payment(s)[0] == "insufficient_evidence"
    assert _payment(s)[3] is None


def test_duplicate_payment_rows_without_order_id_on_resolved_case():
    s = state(get_order_payments=[
        {"payment_sequential": "1", "payment_value": "89"},
        {"payment_sequential": "1", "payment_value": "16"}],
        get_payment_timeline={"captured_total": 105},
        get_refund_timeline={"refunded_total": 0})
    s.order_id = "A"
    assert _payment(s)[0] == "insufficient_evidence"
    assert _payment(s)[3] is None


def test_gateway_rejects_undiscovered_tool_without_calling_server():
    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name="get_order")])

        async def call_tool(self, *args, **kwargs):
            pytest.fail("undiscovered tool must not be called")

    with pytest.raises(ValueError, match="not available"):
        asyncio.run(EvidenceGateway(Session(), None).call("get_policy", case_id="TEST_CASE"))


@pytest.mark.parametrize("amount,expected", [(40, 40), (110, 0)])
def test_refund_uses_policy_amount_with_balance_limit(amount, expected):
    from pathlib import Path

    from student_agent.contracts import Contracts
    from student_agent.workflow import _verify

    s = state(get_order={"order_id": "A", "order_status": "canceled"},
              get_payment_timeline={"captured_total": 100},
              get_refund_timeline={"refunded_total": 0},
              get_policy={"currency": "BRL", "policy_version": "EC_POLICY_V2", "rules": {
                  "canceled_order_paid": {"refund_brl": amount,
                      "case_status": "action_required", "recommended_action": "issue_refund",
                      "responsible_parties": [{"party_type": "platform", "party_id": None}]}}})
    s.case["policy_version"] = "EC_POLICY_V2"
    s.order_id = "A"
    s.claims = [{"claim_id": "claim-1", "topic": "canceled_order_paid"}]
    output = _build(s)
    assert output["financial_resolution"]["recommended_refund_brl"] == expected
    trace = SimpleNamespace(contracts=Contracts(Path("contracts/schemas")))
    _verify(s, output, trace)
    if not expected:
        assert output["assessment"]["case_status"] == "needs_investigation"
