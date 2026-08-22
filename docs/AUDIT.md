# AUDIT — agent kit v8.4 (profile THOROUGH)

## 0. Kết quả đo THẬT (đã chạy)

Phân biệt hai đơn vị đếm: **defect tiêm vào** != **check bị trip**
(1 defect có thể trip nhiều check).

```
Baseline v8.4:     141 checks | PASS 141 | FAIL 0
Negative control:   28/28 defect bat duoc  |  moi defect trip 1-2 check  |  0 false positive
```

Chạy lại bất cứ lúc nào: `python3 validate.py --selftest`.

| Defect tiêm vào | Check trip | Bắt? |
|---|---|---|
| `critic tools: Read → []` (fail-to-launch) | 1 | có |
| `Explore haiku → opus` (leo tier đắt) | 2 | có |
| Ngưỡng escalation `≥3 → ≥30 file` | 1 | có |
| Xoá escape hatch của builder | 1 | có |
| Xoá chặn PASS+ISSUES rỗng của critic | 1 | có |
| `ATTEMPT CAP 3 → 25` (lệch giữa các file) | 1 | có |
| Thêm câu cho phép báo pass giả | 1 | có |
| Tài liệu ghi sai model tier | 1 | có |
| `critic` mất `maxTurns` | 2 | có |
| builder được bảo tự gọi subagent dù thiếu tool `Agent` | 1 | có |
| Policy mất Bước 0 (quay lại bảng tra cứu) | 1 | có |
| Điều kiện KHÔNG delegate quay lại chủ quan | 1 | có |
| route-hook trỏ agent không tồn tại | 1 | có |
| plan-gate mất ngưỡng miễn phí (chặn cả sửa typo) | 1 | có |
| orchestrator mất cảnh báo thay-thế-system-prompt | 1 | có |
| verifier leo tier đắt bằng critic | 2 | có |
| verifier coi "không tìm thấy" là bịa | 1 | có |
| verifier mất ngưỡng dừng | 1 | có |
| ask-loop mất cap → vòng lặp hỏi vô hạn | 1 | có |
| policy gộp verifier với critic làm một | 1 | có |

**Nguyên tắc negative control** (kế thừa từ v7.2, và lần này áp cho chính mình):
tiêm ở mức TỐI THIỂU và CHÍNH XÁC. Tiêm quá tay khiến check fire vì lý do khác,
che mất lỗ hổng thật. Mutation phải đổi GIÁ TRỊ hoặc xoá RÀNG BUỘC, không phải
xoá từ khoá — nếu mutation cùng lớp với check thì bắt 100% là tất yếu, không
phải bằng chứng. Đây chính là lỗi của negative control v7.3 (xem `MIGRATION.md` §B).

**GIỚI HẠN:** chỉ kiểm TĨNH cấu trúc + giá trị cấu hình. KHÔNG đo được agent có
thật sự ngừng bịa hay không — cần chạy trên workload thật (§2–§6).

## 1. Defect đã sửa
v7.1: xem lịch sử. v7.3 → v8.0: 4 lỗi chặn + validator viết lại, chi tiết đầy đủ
ở `MIGRATION.md` §A và §B.

## 2. Đo fabrication rate (chỉ số quan trọng nhất — CHƯA CÓ SỐ)
20 output thật; mỗi CLAIM phân loại: **grounded** (kèm `file:line`/lệnh+output và
kiểm lại ĐÚNG) / **unverifiable** (không bằng chứng) / **fabricated** (có bằng
chứng nhưng kiểm lại SAI). Đo trước–sau để có delta; không baseline thì số sau
vô nghĩa.
**Bẫy:** ép `file:line` có thể sinh kiểu bịa mới — đúng format, sai nội dung.
Bắt buộc spot-check thật.

## 3. Goal & stop
Tỉ lệ task có DoD kiểm chứng được (mục tiêu 100%); số lần chạm ATTEMPT CAP
(chạm nhiều = task mơ hồ hoặc cascading failure, KHÔNG phải nâng cap).

## 4. Verification loop
**Escaped-defect rate** (lỗi lọt qua verify, người phát hiện sau) — thước đo
thật, không phải "số check đã chạy". Tỉ lệ ghi "CHƯA VERIFY" thay vì pass giả
(cao = trung thực). Verification phải RẺ HƠN chi phí lỗi nó bắt.

## 5. Critic gate
5 answer lỗi cố ý + 5 sạch → recall lỗi, false-FAIL, số case PASS+ISSUES rỗng
(phải = 0). Kiểm prompt gọi critic có lỡ chứa reasoning/trace.
Kiểm thêm ở v8.0: critic có thật sự spawn được không, và `maxTurns: 1` có khiến
nó trả rỗng khi lỡ gọi tool không.

## 6. Triggering (CHƯA CÓ SỐ — phải đo trước khi mở rộng)
- 10 task (6 khớp + 4 mồi nhử) → precision / recall / false-spawn.
- Đo riêng: `Explore` override có nhận không, hay Claude vẫn dùng built-in.
- Đo tần suất cổng escalation của builder kích hoạt. >30% số task → task giao
  đang quá lớn, chia nhỏ chứ đừng nới ngưỡng.

## 7. Hook (mới ở v8.0)
`no-fake-pass.py` hiện FAIL-OPEN vì schema payload `SubagentStop` chưa verify
trên máy đích. Cần đo: số lần BLOCK đúng, số lần FAIL-OPEN (ghi trong
`~/.claude/no-fake-pass.log`). FAIL-OPEN nhiều = hook chưa đọc được report =
đang không bảo vệ gì; chạy `DUMP=1` và siết lại.
Heuristic hiện tại (claim-pass mà không có ``` / `$ ` / `CHƯA VERIFY`) là điểm
khởi đầu, chưa được tune trên dữ liệu thật.

## 13. Lớp bịa MỚI: lấp nghĩa từ viết tắt (v8.4)

Ca thật: `NDVLDTT` -> agent tự mở rộng thành "người thanh toán tại nhà", không nguồn.
Lọt qua TOÀN BỘ 124 check của v8.3. Bốn nguyên nhân:

1. Danh sách cấm là TẬP ĐÓNG ("API/config/thư viện/flag/số liệu/benchmark/trích
   dẫn") -> thuật ngữ nghiệp vụ không nằm trong đó, đọc như được phép.
2. Với model, mở rộng viết tắt giống ĐỌC HIỂU chứ không giống KHẲNG ĐỊNH, nên
   rule no-fabrication không kích hoạt. Đây là "lấp khoảng trống im lặng".
3. `verifier` dùng lại đúng danh sách đóng đó ở bước tách CLAIM, và bước 2 của nó
   ("bỏ qua claim loại ý kiến") còn chủ động loại gloss ra.
4. `builder` dùng tools allowlist -> mất hết MCP -> không tra được KB nội bộ dù
   nghĩa chính thức có thể đang nằm ở đó.

Tín hiệu tất định tìm ra được: viết tắt tiếng Việt ghép từ chữ đầu mỗi ÂM TIẾT,
nên gloss bịa thường KHÔNG khớp chữ cái. `NDVLDTT` (7 chữ) vs "người thanh toán
tại nhà" -> NTTN (4). Kiểm được bằng máy, không phụ thuộc model tự giác.

Đã sửa: nguyên tắc đóng thay danh sách liệt kê; rule "Token chưa rõ" ở 4 agent;
gloss là CLAIM trong verifier; builder chuyển sang denylist (giữ MCP);
`hooks/gloss-gate.py` + `glossary.example.txt`.

GIỚI HẠN: hook chỉ bắt gloss có DẠNG NHẬN RA ĐƯỢC (`ABC (nghĩa)`, `ABC = nghĩa`,
`ABC là nghĩa`). Nếu agent dùng nghĩa bịa RẢI RÁC trong văn xuôi mà không bao giờ
viết ra dạng gloss thì hook mù. Lớp phòng thủ cho ca đó vẫn chỉ là prompt +
glossary + verifier, tức là vẫn xác suất.

## 11. Ngưỡng "đủ tin" của bench — SỬA LỖI

`bench/TASKS.md` từng ghi "chênh ≥3/10 là đủ tin". SAI, đặt bằng cảm tính.
Đúng: chỉ đếm CẶP BẤT ĐỒNG, không đếm trên tổng task. Sign test với n cặp bất
đồng, k về một phía:

```
n=5,  k=4  ->  p = 0.375  (hai phía)   KHÔNG kết luận được
n=10, k=9  ->  p = 0.021              tin được
cần ~25-30 cặp bất đồng cho tín hiệu vừa phải
```

Kéo theo: bộ 10 task với 6 hoà chỉ cho 4-5 cặp bất đồng -> gần như không bao giờ
đạt significance. Muốn đo thật phải hoặc tăng số task, hoặc lặp mỗi task 3 vòng
và so tỉ lệ thắng, hoặc tăng tỉ lệ TRAP task (task có bẫy cài sẵn) lên >=5/10 —
vì task "lành" không phân biệt được hai nhánh.

## 12. Profile THOROUGH — đánh đổi đã chốt
Người dùng chọn đổi token + thời gian lấy độ chính xác, chấp nhận over-trigger
(T7). Do đó KHÔNG áp 2 patch giảm false-positive đã đề xuất trước đó, và KHÔNG
chạy nhánh C (v8.1 + stop-rule, phương án rẻ). Ghi lại để sau này không ai
"tối ưu" ngược lại mà không biết đây là quyết định có chủ đích.

Stop-rule T10 VẪN được thêm vào `architect` + `builder` — nó rẻ và không xung
đột với verifier; hai lớp bảo vệ cho cùng một lỗi là chấp nhận được ở profile này.

## 10. Benchmark v8.1 vs v8.2 (CHƯA CÓ SỐ — harness đã sẵn)

ĐO ĐƯỢC TĨNH (đã chạy):
```
policy global   v8.1 48 dòng ~658 token  |  v8.2 61 dòng ~847 token
                delta +190 token MỖI LƯỢT, MỖI AGENT
agent files     v8.1 4 file  |  v8.2 5 file (thêm verifier, sonnet)
static checks   v8.1 98      |  v8.2 120
negative ctrl   v8.1 15/15   |  v8.2 20/20
```
Ước lượng token dùng 3.5 ký tự/token cho văn bản Việt-Anh trộn — LÀ ƯỚC LƯỢNG,
không phải đo bằng tokenizer thật.

CHƯA ĐO ĐƯỢC (cần chạy `bench/`): fabrication_rate, escaped_defect_rate,
plan_rate, delegate_rate, tổng chi phí, tỉ lệ over-trigger trên task nhỏ.
Không có mấy số này thì KHÔNG kết luận được v8.2 tốt hơn v8.1 — chỉ kết luận
được nó ĐẮT HƠN, vì phần đắt hơn thì đo tĩnh thấy rồi.

## 9. Workflow compliance (mới ở v8.1 — CHƯA CÓ SỐ)
Vấn đề: agent không theo workflow / không lập plan khi nhận prompt.
Nguyên nhân đã xác định: (a) policy là bảng tra cứu không có entry point;
(b) CLAUDE.md nạp một lần, khoảng cách vị trí lớn dần trong phiên;
(c) escape hatch "đủ ngữ cảnh" do chính agent tự chấm; (d) phân loại và thực thi
chung một lượt nên bước phân loại bị bỏ; (e) kit v8.0 chỉ ràng buộc subagent,
không ràng buộc lượt đầu của main session.

Cần đo, 20 lượt thật, TRƯỚC và SAU khi bật hook:
- **Plan rate**: % lượt có phân loại + plan + DoD trước hành động đầu tiên.
- **Delegation rate**: % lượt đúng-lớp có delegate (so với bảng routing).
- **Drift theo độ dài phiên**: chia lượt 1–5 / 6–15 / 16+. Nếu plan rate tụt
  theo nhóm sau thì nguyên nhân (b) là chính; nếu phẳng thì là (a)/(c).
- **Chi phí route-prompt**: % lượt bị tiêm × ~100 token. >70% lượt bị tiêm →
  MIN_CHARS đang quá thấp.
- **plan-gate false positive**: số lần BLOCK mà anh phải đặt `PLAN_GATE=off`.
  Cao → nới `PLAN_GATE_FREE_EDITS`, đừng tắt hẳn.
Chỉ bật `optional/orchestrator.md` khi đã đo 3 chỉ số đầu mà vẫn chưa đạt.

## 8. Điều kit KHÔNG giải quyết được
- Delegation từ parent vẫn XÁC SUẤT. v8.0 chỉ loại bỏ delegation LỒNG NHAU
  (subagent → subagent) bằng escalation tất định. Muốn cưỡng chế nhánh
  parent → subagent thì phải dùng hook hoặc `@agent-<name>` thủ công.
- No-fabrication policy là chỉ dẫn, không phải cơ chế; giảm xác suất, không triệt
  tiêu. Chỉ hook là cơ chế — và chỉ với đúng pattern nó bắt được.
- Escape hatch chỉ bắt mâu thuẫn khi agent tình cờ chạm vùng dữ liệu liên quan.
- Verification loop chỉ mạnh ngang bộ check khai báo. Không điền
  `VERIFICATION.template.md` vào project → sụp cả trụ "không bịa".
- `validate.py` kiểm cấu hình, không kiểm hành vi. 98/98 PASS không nói gì về
  chất lượng output.
- `route-prompt.py` phân loại bằng regex tiếng Việt + Anh. Prompt lai, viết tắt,
  hoặc lối diễn đạt khác sẽ rơi vào AMBIGUOUS (fail-safe: buộc hỏi lại, không
  buộc làm sai). Chưa tune trên corpus prompt thật của anh.
- `gloss-gate.py` chỉ bắt gloss viết ra thành dạng nhận diện được; nghĩa bịa dùng
  ngầm trong văn xuôi thì mù. Và khớp chữ cái đầu KHÔNG chứng minh nghĩa đúng —
  nó chỉ loại được ca sai rõ ràng.
- `plan-gate.py` chỉ đếm được tool call, không đọc được CHẤT LƯỢNG plan. Một
  TodoWrite rỗng cũng mở khoá. Nó chặn "không có plan", không chặn "plan tệ".
