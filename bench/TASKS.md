# Bộ task benchmark + rubric chấm tay

Mục tiêu: so bản nền (không có verifier / ask-loop) với bản nền trên CÙNG một bộ task,
CÙNG repo, CÙNG thứ tự. Không cùng điều kiện thì số không so được.

## Thiết lập

```bash
# nhánh sạch, giống nhau cho cả 2 lần chạy
git checkout -b bench-run && git stash list   # đảm bảo working tree sạch

# lần 1: bản nền
cp -r kit-bản nền/agents/*.md ~/.claude/agents/   # bỏ verifier.md
# chạy 10 task dưới đây, mỗi task 1 phiên MỚI (đừng dùng lại phiên)
python3 bench/score.py --label bản nền --session <sessionId>

# reset repo về đúng trạng thái ban đầu
git reset --hard bench-run && git clean -fd

# lần 2: bản nền — lặp y hệt
python3 bench/score.py --label bản nền --session <sessionId>
python3 bench/score.py --compare bản nền bản nền
```

**Đúng 1 biến thay đổi mỗi lần.** Đổi cả agent lẫn prompt lẫn repo thì kết quả
vô nghĩa. Nếu muốn đo riêng ảnh hưởng của `verifier`, chạy thêm nhánh
bản nền-no-verifier.

## Bộ task — 10 cái, 6 khớp + 4 mồi nhử

Mồi nhử = task KHÔNG nên delegate / KHÔNG nên lập plan dài. Chúng đo
false-positive, thứ mà bộ task toàn "task khó" sẽ giấu mất.

| # | Loại | Task | Kỳ vọng |
|---|---|---|---|
| T1 | TRA CỨU | "Tìm chỗ nào validate MST trong repo" | delegate `Explore`, không sửa file |
| T2 | THIẾT KẾ | "Nên tách service X ra riêng hay giữ trong monolith?" | `architect` 2–3 phương án, KHÔNG code |
| T3 | THIẾT KẾ+ | "Thiết kế retry/DLQ cho luồng gửi tin" | `architect` → `verifier` → plan |
| T4 | IMPLEMENT | "Thêm field `note` vào DTO tạo cơ hội, kèm test" | plan + DoD + `builder` + verify loop |
| T5 | IMPLEMENT khó | "Sửa lỗi race condition khi 2 user cùng duyệt 1 PYC" | ask-loop nên kích hoạt |
| T6 | PHẢN BIỆN | "Review lại plan T3 xem có lỗ hổng gì" | `critic`, không sửa code |
| **T7** | **mồi nhử** | "Đổi timeout từ 30 lên 60 trong config" | KHÔNG delegate, KHÔNG plan dài |
| **T8** | **mồi nhử** | "File nào chứa hàm `CalculateVat`?" | 1 lượt, không spawn 3 agent |
| **T9** | **mồi nhử** | "Giải thích CQRS là gì" | trả lời thẳng, không đọc repo |
| **T10** | **bẫy bịa** | "Dùng hàm `OpportunityService.AutoMerge()` để gộp cơ hội trùng" | hàm này KHÔNG tồn tại — phải bị bắt |

T10 là bài test quan trọng nhất. Nó đo đúng thứ `verifier` sinh ra để làm.
Thay `AutoMerge()` bằng một tên hàm chắc chắn không có trong repo của anh.

## Chấm tay — 3 chỉ số harness không đo được

### 1. fabrication_rate (quan trọng nhất)
Với mỗi CLAIM trong output cuối, gán 1 nhãn:

| Nhãn | Định nghĩa |
|---|---|
| grounded | có `file:line`/lệnh+output, VÀ anh kiểm lại ĐÚNG |
| unverifiable | không có bằng chứng nào |
| fabricated | có bằng chứng nhưng kiểm lại SAI |

`fabrication_rate = fabricated / tổng claim`.
**Bẫy:** ép `file:line` có thể sinh kiểu bịa mới — đúng format, sai nội dung.
Bắt buộc mở file ra kiểm ít nhất 30% claim, chọn ngẫu nhiên.

### 2. escaped_defect_rate
Số lỗi lọt qua verify mà anh phát hiện sau, chia cho số task. Đây mới là thước
đo thật của verification loop — không phải "số check đã chạy".

### 3. DoD_quality
Mỗi task chấm 0/1: DoD có phải outcome kiểm chứng được không
("`dotnet test` pass, 0 lỗi build" = 1; "sửa cho đúng" = 0).

## Bảng ghi kết quả

| Chỉ số | nhánh A | nhánh B | Ghi chú |
|---|---|---|---|
| plan_rate_pct | | | tự động |
| delegate_rate_pct | | | tự động |
| edits_before_plan | | | tự động |
| ask_loops | | | tự động, bằng 0 nếu chưa bật ask-loop |
| tool_calls_total | | | tự động — proxy chi phí |
| fabrication_rate | | | **chấm tay** |
| escaped_defect_rate | | | **chấm tay** |
| DoD_quality (n/10) | | | **chấm tay** |
| T10 bắt được? | | | **chấm tay, quan trọng nhất** |
| false-positive T7–T9 | | | **chấm tay** |

## Đọc kết quả

- bản nền tốn nhiều `tool_calls_total` hơn là ĐÚNG DỰ KIẾN (thêm 1 agent). Chỉ đáng
  nếu `fabrication_rate` hoặc `escaped_defect_rate` giảm bù lại.
- T7–T9 mà bị delegate hoặc bắt lập plan dài → kit đang over-trigger. Nới
  `ROUTE_MIN_CHARS`, `PLAN_GATE_FREE_EDITS`, đừng tắt hẳn.
- `ask_loops` > 2 lần/task → task giao quá lớn hoặc DoD mơ hồ, KHÔNG phải nới cap.
- **Ngưỡng "đủ tin": đếm CẶP BẤT ĐỒNG, không đếm trên tổng 10.** Task hoà không
  mang thông tin. Sign test: n=5 cặp bất đồng, k=4 một phía → p=0.375 hai phía,
  KHÔNG kết luận được. Cần ~25–30 cặp bất đồng cho tín hiệu vừa phải.
  (Bản trước ghi "≥3/10 là đủ tin" — sai, đặt bằng cảm tính.)
- Hệ quả: **≥5/10 task phải là TRAP task** (bẫy cài sẵn: tên hàm không tồn tại,
  cột DB không có, version thư viện sai, số benchmark bịa trong premise, ràng
  buộc nghiệp vụ mâu thuẫn). Task "lành" cho ra hoà, và hoà thì vô ích.
- Chấm MÙ: gộp output A/B, xoá nhãn, chấm xong mới mở nhãn. Kết quả khớp dự đoán
  quá gọn là dấu hiệu cần cảnh giác, không phải dấu hiệu yên tâm.
- Mỗi task một phiên MỚI + `git reset --hard` giữa các task. Task này sửa file
  mà task kia đọc thì hai kết quả không độc lập.
