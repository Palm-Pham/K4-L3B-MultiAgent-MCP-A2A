# L3B Architecture Record

Tài liệu này mô tả hành vi hiện có trong `src/student_agent/workflow.py` và đường chạy của CLI. Khi tài liệu khác code, lấy code làm chuẩn. Các giới hạn bên dưới là trạng thái hiện tại, không phải tính năng đã được hứa hẹn.

## 1. System overview

Mỗi case được xử lý bằng một state machine bất đồng bộ, tuần tự. `_State` và `_Collector` được tạo mới trong mỗi lần gọi `solve_case`, nên cache và danh sách evidence đã dùng nằm trong phạm vi case. CLI dùng một MCP session cho toàn bộ lượt chạy, xử lý các case lần lượt và tự ghi `case_received` trước, `case_finalized` sau khi output đã được kiểm schema và ghi file.

```text
case-set.json + inputs/<case_id>.json
              │
              ▼
CLI: load/validate input → case_received
              │
              ▼
solve_case: discover tool schemas → route
              │
              ▼
order-agent: resolve candidate; lấy order/items/customer/product khi có căn cứ
              │
              ▼
payment-agent: payments → payment timeline → refund timeline
              │
              ▼
shipment-agent: shipment summary, timeline và seller delay
              │
              ▼
policy-agent: lấy policy, xét conflict, chọn primary issue và dựng JSON L3B v2
              │
              ▼
verifier: schema + một số bất biến chéo field → verification_completed
              │
              ▼
CLI: validate output → outputs/<case_id>.json → case_finalized

Các pha order/payment/shipment/policy ─► _Collector ─► MCP server audit
Các pha và CLI ─► TraceWriter ─► traces/trace.jsonl
```

`_State.handoff` chỉ cho phép chuyển `route → order → payment → shipment → policy → verify → done`. Mỗi bước chuyển ghi `handoff`; bước đến, trừ `done`, ghi thêm `task_assigned` bởi coordinator. Đây là các vai trò logic chạy nối tiếp trong một hàm, không có agent độc lập chạy song song hay giao thức A2A qua mạng.

`order-agent` kiểm tra tối đa bốn ID gồm `claimed_order_id` và `candidate_order_ids`. Nếu claimed order được MCP xác nhận thì chọn nó; nếu không có claimed ID và chỉ tìm thấy một candidate thì chọn candidate đó. Nó ghi nhận candidate bị từ chối khi MCP báo không tìm thấy, hoặc khi candidate tìm thấy có customer khác order đã chọn. Nếu chưa resolve được, payment và shipment bỏ qua truy vấn theo order; policy vẫn được gọi nếu có `policy_version`. Customer history và product context chỉ được hỏi khi cờ tương ứng trong `investigation_scope` bật và đã có ID cần thiết.

`payment-agent` đối chiếu danh sách payments với các sự kiện capture/refund; `shipment-agent` xác định `on_time`, `seller_delay`, `logistics_delay`, `lost`, `returned` hoặc giữ `insufficient_evidence` theo các trường hiện có. Mâu thuẫn được ghi trực tiếp vào `_State.conflicts` bởi các pha liên quan, tối đa năm mục; không có conflict resolver riêng. `policy-agent` chỉ chọn một `primary_issue` có trong claim và được các kết quả hiện có hỗ trợ, với điều kiện đã resolve order và có policy evidence. Khi có conflict, kết quả chính bị hạ thành `insufficient_evidence`/`needs_investigation`. Claim assessment chỉ đánh dấu claim trùng primary issue là `supported`; các claim còn lại là `insufficient_evidence`.

Output hiện đặt `payment_analysis.refundable_total_brl` là `null`, `financial_resolution.recommended_refund_brl` là `0` và `refund_lines` rỗng; workflow chưa tính số tiền hoàn đề xuất từ policy.

`_Collector` đối chiếu tên tool cố định trong `_TOOL_INTERFACE` với input schema do MCP discovery trả về, gọi MCP bằng `case_id`, kiểm schema và domain của response, giữ nguyên `evidence_ref`, cache theo `(tool, argument)` và giới hạn 18 lần gọi mỗi case. Chỉ evidence đã được `consume` mới xuất hiện trong output và sự kiện `tool_result_consumed`. Verifier không gọi thêm MCP; nếu kiểm tra thất bại, `solve_case` ném lỗi và CLI không ghi output/finalize cho case đó. Nếu thiếu dữ liệu mà không vi phạm kiểm tra hiện có, workflow vẫn trả kết quả với `insufficient_evidence`/`needs_investigation`.

## 2. Agent ownership

| Actor | Trách nhiệm hiện có | MCP tool dùng qua `_Collector` | Handoff |
| --- | --- | --- | --- |
| Coordinator | CLI nhận/finalize case; `_State.handoff` giao pha kế tiếp | Không | `route → order`, sau `done` |
| Order agent | Resolve candidate; lấy items, customer history và product context theo scope | `get_order`, `get_order_items`, `get_customer_history`, `get_product_context` | `order → payment` |
| Payment agent | Đối soát payments, capture và refund | `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | `payment → shipment` |
| Shipment agent | Đánh giá shipment/timeline và seller bị chậm | `get_shipment_summary` | `shipment → policy` |
| Policy agent | Lấy policy, chọn issue và dựng output | `get_policy` | `policy → verify` |
| Verifier | Kiểm schema và các bất biến được mã hóa | Không | `verify → done` |

Customer/product resolution thuộc order agent. Conflict là dữ liệu do các pha ghi vào state và policy agent xem xét, không phải actor hoặc bước handoff riêng.

## 3. Entity resolution và A2A protocol

Không có xếp hạng candidate bằng điểm hoặc confidence threshold động. Quy tắc chọn order và từ chối candidate được mô tả ở mục 1; output đặt `entity_resolution.confidence` là `0.9` khi resolve được, ngược lại `0.2`. Nếu mọi candidate đã bị từ chối thì status là `not_found`; các trường hợp chưa chọn được còn lại là `ambiguous`.

Handoff là sự kiện trace giữa các vai trò logic, không có message envelope, timeout hay vòng lặp A2A. Thứ tự pha cố định trong `_PHASES` ngăn handoff sai thứ tự. Mỗi event mang `case_id` từ `_State`; trace không chứa chain-of-thought.

## 4. Evidence và conflict lifecycle

MCP response phải qua schema evidence và khớp domain mong đợi trước khi được dùng. `_Collector` phát hiện cùng `evidence_ref` đi kèm nội dung khác nhau trong một case. `_State.consume` ghi `tool_result_consumed` một lần cho mỗi ref và thêm vào `used`; output `evidence_refs` lấy từ tập này. Các claim thuộc nhóm payment/shipment được gắn với refs đã dùng; claim được support được bổ sung policy ref. Cache, `by_ref` và `used` được tạo lại cho từng case.

Các conflict hiện được ghi với `selected_source: null`, `resolution_code: unresolved` khi customer history, product, shipment hoặc payment không khớp theo quy tắc trong code. Workflow chưa chọn nguồn ưu tiên hay giải quyết từng conflict. `_verify` chỉ kiểm các claim refs là tập con của output refs và claim `supported` có ít nhất một ref; provenance theo team/run/case vẫn phụ thuộc MCP audit của server, không được kiểm hoàn toàn ở client.

## 5. Failure and efficiency policy

| Tình huống | Hành vi hiện có |
| --- | --- |
| Tool không có hoặc input schema không phù hợp | Ghi mã lỗi nội bộ, cache kết quả `None`, tiếp tục với dữ liệu còn lại |
| MCP timeout/connection error hoặc response lỗi | Ghi mã lỗi nội bộ, cache `None`; không retry |
| Vượt 18 MCP call/case | Ngừng gọi thêm, ghi `mcp_call_budget_exhausted` |
| Candidate không resolve được | Payment/shipment không gọi theo order; output `ambiguous` hoặc `not_found` |
| Dữ liệu nguồn mâu thuẫn | Ghi tối đa năm `data_conflicts` unresolved; policy hạ kết luận thành `insufficient_evidence` |
| Verifier phát hiện vi phạm | Ném lỗi; case không được finalize |

Cache dùng cặp `(tool, argument)` trong một case. Số MCP call và số lỗi được ghi trong `verification_completed.attributes`; danh sách lỗi không được xuất ra output. Không có retry hoặc truy vấn bổ sung do verifier yêu cầu.

## 6. Verification invariants

`_verify` kiểm: output hợp schema L3B v2 và đúng `case_id`; tập output refs bằng tập evidence đã consume; claim refs thuộc output refs và claim `supported` có ref; affected order IDs bằng resolved IDs và không giao với rejected IDs; late seller IDs thuộc seller IDs; tổng các refund lines bằng recommended refund; kết luận khác `insufficient_evidence` phải có policy ref; confidence trong `[0, 1]`; `action_required` có action và `no_action` không có action.

Verifier hiện chưa kiểm đầy đủ provenance team/run, thứ tự timeline, độ đúng số tiền capture/refund, các evidence group bắt buộc của scorer hoặc source precedence. Vì vậy pass verifier và JSON Schema chưa đủ để bảo đảm điểm semantic/evidence/provenance.

## 7. Reproducibility

Chạy `day09 validate-inputs`, `day09 run`, `day09 validate`, rồi `day09 package --output dist/submission.zip`. CLI xử lý case theo thứ tự trong `case-set.json` và dùng một MCP session; `_Collector` giới hạn 18 call/case. Không có random seed hay LLM config trong `workflow.py`. Thời điểm và `event_id` của trace được tạo khi chạy nên hai lần chạy không có trace byte-for-byte giống nhau. Không ghi API key vào output hoặc trace.
