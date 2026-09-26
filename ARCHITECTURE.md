# L3B Architecture Record

## 1. System overview

Python runs agent roles in one process, in deterministic order:

Input -> Entity/customer -> Coordinator -> Order/product -> Shipment -> Payment/refund -> Policy -> Conflict resolver -> Verifier -> Output.

Roles exchange case-scoped CaseState data and observable trace handoffs. This is an in-process A2A design, without an LLM or external A2A service. MCP supplies official evidence. The CLI writes output atomically only after verification and schema validation.

## 2. Agent ownership

| Actor | Input | Responsibility | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| entity-agent | Candidates, customer hint | Match order/customer | get_order, get_customer_history | Selected order and rejected candidates |
| coordinator | Case and state | Schedule roles and lifecycle | None | task_assigned, case_finalized |
| order-agent | Resolved order | Item/product facts | get_order_items, get_product_context | shipment-agent |
| shipment-agent | Order/item facts | Seller handoff and delivery timing | get_shipment_summary | payment-agent |
| payment-agent | Resolved order | Capture/refund reconciliation | get_order_payments, get_payment_timeline, get_refund_timeline | policy-agent |
| policy-agent | Policy version | Read refund/action rules | get_policy | Policy facts |
| conflict-agent | Collected evidence | Find contradictions, build draft | None | verifier |
| verifier | State and draft | Invariants and schema | None | verification_completed |

OWNERS enforces actor tool permissions. Gateway discovers tools and refuses unavailable names before calling MCP.

## 3. Entity resolution and A2A protocol

Candidates are deduplicated with claimed order first. At most five are investigated. With a customer hint, an order must be corroborated by history or customer ID. A corroborated claimed order ends the search early. One match resolves; multiple matches remain ambiguous. Confidence is heuristic: 0.98 with history corroboration, 0.88 for a single match, 0.2 for ambiguity. The five-candidate budget can miss later candidates.

Observable messages carry case_id, event_id, actor, target, event_type, decision_code and evidence_refs where applicable. Each case has separate state/cache. Sequential handoffs avoid role races and loops. No private reasoning is traced. The verifier is a separate checking function using the same evidence, not a second evidence source.

## 4. Evidence and conflict lifecycle

Gateway validates the evidence envelope against the public schema. Evidence references remain unchanged and are linked to tool_result_consumed and output. Cache keys include tool and arguments and live only for one case. Candidate order evidence is kept separately; only the selected order supplies output facts.

Payment supports aggregates and confirmed/completed/succeeded events, deduplicating event IDs. Missing refund timelines are unknown, not zero. Shipment checks each seller's handoff against its deadline; missing timestamps cannot establish logistics responsibility.

Conflicts include cross-source order status/capture totals, duplicate IDs with different records, and shipment summary versus confirmed late events. Without verified source precedence, selected_source stays null, claims become insufficient_evidence and the case needs_investigation. Automatic conflict resolution remains limited to what the evidence establishes.

Policy must match version/currency and provide a valid rule. Refund must equal the rule amount, remain within captured minus refunded balance, and have no unresolved conflicts. Uninterpretable policy triggers investigation.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace behavior |
| --- | ---: | --- | --- |
| Transport/timeout | 1 retry | Missing evidence; stop if entity has no evidence | No successful consumption event |
| MCP execution error | 1 retry | Same; expose original CLI error | No fabricated evidence |
| Missing tool / invalid response | 0 | Missing evidence or stop | No successful consumption event |
| Ambiguous entity | 0 | needs_investigation | handoff / unresolved |
| Source conflict | 0 | No automatic refund | handoff / conflicts_reviewed |
| Invalid output | 0 | Do not write output | No verification_completed |

HTTP configuration: 300-second timeout, 30-second connect/write/pool limits. One case runs at a time. A normal resolved case with customer/product context makes nine distinct requests; up to thirteen with five candidates and all specialists, each with at most one retry. Calls, including failed calls, count toward server audit. Cache prevents repeat calls with identical arguments within the case.

## 6. Verification invariants

- Case ID and output evidence references match state.
- Claim references are subsets of consumed case evidence.
- Selected order matches its order evidence.
- Refund lines sum to recommended refund.
- Refunded amounts above capture cannot be reconciled.
- Recommended refund matches policy and available balance with no conflict.
- Output and every trace event pass the public schemas.

Only the server audit can establish team/run/case provenance. Local validation does not replace that check.

## 7. Reproducibility

Python >=3.11; dependency ranges are in pyproject.toml. requirements-lock.txt records the tested installed versions. No model, sampling or random seed is used. Event IDs are random and timestamps vary.

```powershell
python -m pip install -r requirements-lock.txt
python -m pip install -e . --no-deps
python -m pytest tests/test_workflow.py tests/test_starter.py -q
python -m ruff check src tests
day09 validate-inputs
day09 mcp-check --case-id L3B_CASE_001
day09 run --resume
day09 validate
day09 package --output dist/submission.zip
```

Resume retains valid completed outputs and their trace. Use it only within the same configuration, implementation and audit scope. A run without resume removes old outputs. The release-safety test targets a clean starter and fails when competition inputs have been extracted at root.

The ZIP contains only manifest, trace and 100 outputs. Review evidence and conclusions even after schema validation: schema success does not establish semantic or provenance scores. The user uploads the ZIP at /l3b and selects the final submission.
