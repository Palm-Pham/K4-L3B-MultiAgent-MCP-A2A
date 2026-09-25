# L3B Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

### Mục tiêu và ranh giới

Mỗi case là một cuộc điều tra độc lập: xác định đúng order/customer, kiểm tra các claim bằng dữ liệu MCP, kết luận shipment/payment/refund theo policy và xuất một JSON L3B có evidence truy vết được. Ưu tiên độ đúng nghiệp vụ (40%), evidence và provenance (mỗi phần 15%); kiểm soát số MCP call vì mọi call đều được audit và tính vào efficiency. Không suy ra dữ kiện thiếu từ nội dung khiếu nại hoặc từ case khác.

`day09 run` đã đọc và kiểm tra `case-set.json` cùng đúng 100 file trong `inputs/`, mở một MCP session, gọi `solve_case(case, gateway, trace)` lần lượt cho từng case, kiểm tra output theo schema rồi ghi `outputs/<case_id>.json`. CLI hiện phát `case_received` và `case_finalized`; workflow chỉ phát các sự kiện thực sự xảy ra ở giữa. `solve_case` hiện là stub, nên sơ đồ dưới đây là **thiết kế cần triển khai**, chưa phải hành vi của chương trình.

```text
case-set.json + inputs/<case_id>.json
              │
              ▼
     CLI: kiểm tra input, tạo case context riêng, phát case_received
              │
              ▼
     Entity/customer resolver ──► candidate đã xác nhận / ambiguous / not_found
              │
              ▼
     Coordinator ──► giao việc theo claim và investigation_scope
              │                  │
              │                  ├─► order/product specialist
              │                  ├─► shipment specialist
              │                  └─► payment/refund specialist
              │                            │
              ▼                            ▼
     Conflict + policy resolver ◄── phát hiện dữ liệu mâu thuẫn / policy liên quan
              │
              ▼
     Independent verifier ──► tổng hợp claim, kiểm tra evidence và field nhất quán
              │
              ▼
     JSON L3B v2 → CLI schema validation → outputs/<case_id>.json

     Entity / specialists / conflict-policy ─► EvidenceGateway ─► MCP server audit
     Các actor ─► TraceWriter ─► traces/trace.jsonl
```

### Luồng dữ liệu và quyết định

1. **Tiếp nhận:** Tạo context chỉ sống trong một case, gồm `case_id`, `opened_at`, claim, `candidate_order_ids`, `customer_unique_id_hint`, `policy_version` và `investigation_scope`. Candidate và hint chỉ là giả thuyết; chưa được xem là order/customer đã xác nhận. Không để cache, evidence hoặc kết luận đi qua ranh giới case.
2. **Resolve entity:** Kiểm tra order được claim và các candidate bằng MCP, đối chiếu order/customer/item và ghi lý do chấp nhận hoặc loại. Chỉ chuyển ID có bằng chứng sang `resolved_order_ids`; nếu chưa phân biệt được thì giữ `ambiguous` hoặc `not_found`, giảm mức chắc chắn và chỉ điều tra phạm vi còn có căn cứ.
3. **Lập kế hoạch điều tra:** Coordinator dựa trên claim, entity đã xác nhận và `investigation_scope` để giao việc. Chỉ gọi customer history/product context khi cần theo scope; chọn tool từ discovery của MCP Gateway, không đoán tên. Các specialist nhận cùng `case_id` và tập evidence hợp lệ, trả về phát hiện có claim/evidence liên kết; handoff đi qua coordinator để tránh gọi lặp.
4. **Đối chiếu và xử lý xung đột:** Order/product xác định item/seller; shipment lập timeline và trách nhiệm giao hàng; payment/refund đối soát capture, refund và số tiền. Conflict/policy resolver so sánh các nguồn, áp dụng policy có evidence, lưu mâu thuẫn chưa giải được vào `data_conflicts` thay vì ép kết luận. Mỗi MCP response phải qua schema validation; giữ nguyên `evidence_ref` do server cấp cùng `case_id` và domain, chỉ đưa ref đã thực sự dùng vào output/trace.
5. **Kiểm chứng độc lập:** Verifier rà từng claim, resolved/rejected candidate, quan hệ customer/order, timeline, phép tính BRL, policy, mức confidence và liên kết claim–evidence. Nếu thiếu bằng chứng, chỉ truy vấn bổ sung khi có mục đích và còn ngân sách; với dữ kiện chưa thể xác minh, dùng trạng thái `needs_investigation`/`insufficient_evidence` phù hợp. Nếu vẫn thiếu nhóm evidence bắt buộc của contract/scorer thì chặn finalize và báo lỗi rõ ràng, không tạo evidence giả. Trước khi trả kết quả, kiểm tra toàn bộ field theo `l3b-output-v2.schema.json` và các bất biến chéo field.
6. **Ghi kết quả:** `solve_case` trả một object `day09-l3b-output-v2` cho đúng `case_id`. CLI kiểm schema, ghi output và phát `case_finalized`; trace ghi `task_assigned`, `handoff`, `tool_result_consumed`, `policy_decided` và `verification_completed` khi có sự kiện thật, không ghi nội dung suy luận riêng. `day09 validate` rồi `day09 package` tạo ZIP chỉ chứa manifest, trace và outputs.

### Kế hoạch triển khai theo thứ tự

1. **Nền tảng evidence:** Bổ sung case context, registry evidence theo case và lớp gọi MCP có tool discovery, tham số đúng `case_id`, cache theo case, giới hạn call/retry. Chốt hợp đồng dữ liệu nội bộ cho phát hiện của từng actor và handoff.
2. **Entity + coordinator:** Resolve candidate bằng evidence, xác định nhánh điều tra theo claim/scope, ghi trace giao việc và handoff. Kiểm thử case có exact order, candidate sai và candidate mơ hồ.
3. **Specialists + conflict/policy:** Điều tra order/product, customer, shipment và payment/refund; tính toán từ dữ liệu được trả về, map từng kết luận với `evidence_ref`, biểu diễn xung đột chưa giải quyết.
4. **Verifier + output:** Tổng hợp đúng các field L3B v2, kiểm tra schema và tính nhất quán, hiệu chỉnh confidence theo mức evidence; thử trên case đơn lẻ trước khi chạy đủ 100 case.
5. **Đo và tinh chỉnh:** Dùng `day09 validate-inputs`, `day09 run`, `day09 validate`, `day09 package`; kiểm tra trace/output và số MCP call theo case. Ưu tiên sửa lỗi hard gate (case ID, schema, evidence thiếu/sai phạm vi) trước khi tối ưu efficiency.

Các ngưỡng resolve, quy tắc handoff và quyền tool cụ thể sẽ được chốt ở mục 2–6 sau khi xem tool discovery và payload MCP thực tế; không giả định trước tên tool hoặc thứ tự ưu tiên nguồn mà contract chưa công bố.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Tool permission | Output/handoff |
| --- | --- | --- | --- | --- |
| Entity/customer | TODO | TODO | TODO | TODO |
| Coordinator | TODO | TODO | TODO | TODO |
| Order/product | TODO | TODO | TODO | TODO |
| Shipment | TODO | TODO | TODO | TODO |
| Payment/refund | TODO | TODO | TODO | TODO |
| Policy | TODO | TODO | TODO | TODO |
| Conflict resolver | TODO | TODO | TODO | TODO |
| Verifier | TODO | TODO | TODO | TODO |

Áp dụng least privilege; tool discovery không đồng nghĩa mọi actor đều được gọi mọi tool.

## 3. Entity resolution và A2A protocol

Mô tả cách xếp hạng/reject candidate, confidence threshold, message envelope, correlation theo `case_id`, điều kiện handoff, timeout và cách tránh vòng lặp. Không trace nội dung suy luận riêng.

## 4. Evidence và conflict lifecycle

Mô tả cách validate MCP response, lưu `evidence_ref`, chọn source theo policy, biểu diễn unresolved conflict, map evidence vào claim/output và emit `tool_result_consumed`. Evidence không được tái sử dụng giữa các case.

## 5. Failure and efficiency policy

| Failure | Retry budget | Fallback | Trace event/code |
| --- | ---: | --- | --- |
| MCP timeout | TODO | TODO | TODO |
| Entity not found/ambiguous | TODO | TODO | TODO |
| Source conflict | TODO | TODO | TODO |
| Invalid specialist result | TODO | TODO | TODO |

Nêu query budget/cache strategy để tránh gọi lặp và quét rộng. Retry phải có giới hạn, idempotent và không biến missing evidence thành dữ liệu phỏng đoán.

## 6. Verification invariants

Liệt kê kiểm tra trước finalize: schema, entity scope, rejected candidates, evidence ownership, claim linkage, timeline, payment/refund totals, source precedence, responsibility/action consistency và confidence bounds.

## 7. Reproducibility

Ghi model/config, dependency pinning, concurrency limit, random seed (nếu có), lệnh chạy và giới hạn tài nguyên. Không ghi API key.
