# Changelog

Tất cả thay đổi đáng chú ý của project được ghi tại đây theo cấu trúc
Keep a Changelog. Project dùng Semantic Versioning.

Release này mang số `1.0.1` từ baseline `1.0.0`. Đây là lựa chọn có chủ đích
của maintainer: mục **Breaking** bên dưới mô tả các thay đổi phá tương thích,
mà theo Semantic Versioning lẽ ra thuộc một major mới — số hiệu `1.0.1` vì vậy
KHÔNG phản ánh mức thay đổi, hãy đọc mục **Breaking** và **Migration** trước
khi nâng cấp.

## [1.0.2] - 2026-08-25

Bản vá. Không có thay đổi phá tương thích. Tất cả sửa đổi dưới đây đến từ một
đợt kiểm thử bản `1.0.1` đã cài, trong đó bộ test 171 case đều xanh nhưng vẫn bỏ
lọt ba lỗi thật — vì harness test tự bơm sẵn trường mà runtime thật không gửi.

### Fixed

- `no-fake-pass` không còn chặn mọi mutation ở session chính. Theo thiết kế
  payload hook được tài liệu hoá của Claude Code, `agent_id` chỉ có mặt bên
  trong subagent và cố ý vắng mặt ở session chính; mốc `2.1.196` gắn với
  `prompt_id` chứ không gắn với `agent_id`. Hook đang nhầm hai trường đó, khiến
  receipt không bao giờ cấp được và luồng báo sẵn sàng của builder không thể
  hoàn tất. Actor không phải builder nay nhận một sentinel danh tính ổn định;
  watched builder vẫn fail-closed như cũ.
- `gloss-gate` không còn chặn oan định danh viết hoa nối bằng gạch dưới. Lớp ký
  tự chặn ở lookbehind và lookahead thiếu ký tự gạch dưới, nên đoạn sau gạch
  dưới bị tách thành token độc lập rồi bị coi là định nghĩa bịa.
- `no-fake-pass` không còn coi lệnh có cờ ghi hoặc thực thi lệnh con là chỉ đọc.
  Khớp tiền tố chỉ đọc nay mất hiệu lực khi lệnh chứa cờ nguy hiểm của chính
  tiền tố đó, kể cả khi không có ký tự chuyển hướng hay nối lệnh nào.
- Bảng biến môi trường trong `README.md` khớp lại với code cho busy timeout, và
  bổ sung biến trần lặp của `gloss-gate` vốn chưa từng được ghi.

### Changed

- `plan-gate` cho lệnh shell chứng minh được là chỉ đọc đi qua khi chưa có plan
  duyệt, dùng chung đúng danh sách đã siết ở trên. Chỉ áp cho `Bash`, không áp
  cho `PowerShell`. Fail-closed khi trường lệnh thiếu hoặc không phải chuỗi.

### Added

- 17 test hồi quy, gồm test khoá lại các hành vi chặn có chủ đích để lần sau
  không bị nới nhầm, và một test đồng bộ tài liệu đọc bằng cây cú pháp để tài
  liệu lệch code là bị bắt ngay.
- `tests/support.py` cho phép bỏ hẳn một trường khỏi payload test, thay vì chỉ
  đặt được giá trị rỗng — điều kiện cần để diễn tả "event thật không có trường
  này".

### Known issues

Hai vấn đề đã xác định nhưng CHƯA vá trong bản này:

- Plan approval bị thu hồi giữa chừng khi một sự kiện hệ thống tạo mã prompt
  mới. Đã đo: sự kiện hệ thống CÓ kích hoạt hook nộp prompt y như prompt người
  dùng, nên không phân biệt được bằng cách đếm sự kiện. Cần thiết kế lại theo
  hướng nhận dạng nội dung.
- Tài liệu của chính kit bị chính `gloss-gate` chặn ở 7 trên 7 file, do chúng
  dùng cú pháp meta của kit làm ví dụ. Đây là bốn dạng định nghĩa chặt hoạt
  động đúng thiết kế; khắc phục đòi hỏi quyết định chính sách, không phải sửa lỗi.

## [1.0.1] - 2026-08-25

### Breaking

- Yêu cầu Claude Code 2.1.196+, Python 3.9+ có `sqlite3`, và executable
  `python3` trên `PATH`.
- Thay verification contract dạng prose bằng
  `<project>/.claude/verification.json` schema version 1 với bốn step cố định.
- Builder phải kết thúc bằng `AGENT_KIT_RESULT_V1` có receipt còn hiệu lực cho
  mọi step active theo thứ tự `build` → `typecheck` → `lint` → `test` (bỏ qua
  step N/A); output hoặc prose không còn được nhận làm evidence.
- Plan approval được cô lập theo session/prompt và chỉ được tạo sau
  `PostToolUse ExitPlanMode` có approved plan hợp lệ.
- Internal SQLite schema chuyển từ v1 sang v2. Plan approval v1 được giữ, nhưng
  receipt và mutation state v1 bị vô hiệu có chủ đích, không được tái sử dụng.

### Added

- Agent `reviewer` read-only cho code, repository, security, regression và test
  coverage; tổng roster hiện có sáu agent.
- SQLite state machine dùng chung cho plan approval, mutation epoch scope theo
  hash của canonical project path + session + prompt, và verification receipt
  vẫn bind với owner agent cùng exact contract/command/cwd.
- Một pure verification-contract validator dùng chung cho runtime hook và
  `tests/static_check.py`, tránh hai implementation schema lệch verdict.
- Regression tests cho hook behavior, hook wiring, agent contract, release
  contract và các đường bypass đã sửa.
- GitHub Actions workflow cho behavior matrix Ubuntu/macOS/Windows trên Python
  3.9/3.13, exact exec-form launcher smoke trước full behavior tests và strict
  plugin validation riêng. Đây là cấu hình CI; remote run **CHƯA VERIFY**.

### Changed

- Route code review tới `reviewer`, phản biện answer/plan tới `critic`, và yêu
  cầu có mutation dương tới `builder`. Write verb trong cụm phủ định gần được
  mask; positive write clause riêng vẫn route `BUILD`, còn conflict read-only
  cùng clause không phân giải được route `AMBIGUOUS`.
- Hook command chuyển sang exec form `python3` với `args`, timeout hữu hạn và
  matcher `SubagentStop` bao phủ tên builder bare lẫn `agent-kit:builder`.
- No-fake wiring nhận mutation success và failure của `Edit`, `Write`,
  `NotebookEdit`, `Bash`, `PowerShell`, `Monitor`, worktree và `mcp__.*` từ mọi
  actor; builder ownership chỉ áp dụng cho verification receipt và kết quả
  `SubagentStop`.
- Glossary dùng exact normalized comparison; glossary home có precedence, còn
  glossary project chỉ thêm token không xung đột.
- Verification command được match exact theo fingerprint contract hiện tại,
  command/cwd và chỉ foreground `PostToolUse` không-interrupted mới nhận receipt.
  Chạy lại step upstream vô hiệu chính nó cùng mọi downstream step.

### Fixed

- Hook ép UTF-8 cho stdin/stdout/stderr trước khi chạm protocol. Trước đó mọi
  hook in `json.dumps(..., ensure_ascii=False)` tiếng Việt ra stream mang
  encoding console mặc định; trên Windows (cp1252/cp437) hook chết bằng
  `UnicodeEncodeError` và CI Windows fail 52 test.
- Ngân sách chờ write lock của SQLite tính cho TRỌN một transaction/reader thay
  vì cho từng câu lệnh. `PRAGMA journal_mode = WAL` và `BEGIN IMMEDIATE` trước
  đây mỗi câu được cấp trọn `busy_timeout` riêng, nên tổng thời gian chờ có thể
  chạm 2x trần và vượt outer timeout của `plan-gate`.
- `plan-gate` nêu nguyên nhân gốc của `StateError` trong thông điệp chặn và log
  `plan_gate.state_unavailable`, thay vì chỉ một câu chung chung không chẩn
  đoán được từ log CI.
- Nâng trần chờ write lock của SQLite từ 250ms lên 3000ms (trần tối đa 4000ms).
  Mức cũ quá hẹp: khi nhiều hook cùng giành lock trên runner chậm, một tiến
  trình nhận `database is locked` và gate chặn oan một agent hợp lệ. Đo trên
  runner GitHub, 32 hook đồng thời cần 1.4–2.2s để rút hết hàng đợi write lock,
  nên trần 2500ms chỉ còn ~15% biên và vẫn thỉnh thoảng chặn oan một hook trong
  lô. Vì thời gian chờ nay do deadline quyết định (xem mục dưới), 4000ms là
  trần wall-clock thật, trong khi 2500ms `busy_timeout` từng tốn tới 3.96s trên
  macOS — worst case tăng khoảng 40ms, không phải 1.5s.
- Thời gian chờ write lock do `StateStore` tự cưỡng chế bằng đồng hồ monotonic,
  không giao phó cho `PRAGMA busy_timeout`. `busy_timeout` chỉ là trần mềm: nó
  ngừng KHỞI ĐỘNG lượt thử mới khi hết ngân sách, còn lượt đang ngủ vẫn ngủ hết.
  Đo trên runner GitHub khi lock bị giữ suốt, ngân sách 2500ms tốn 2.50s trên
  Linux, 2.64s trên Windows và 3.72–3.96s trên macOS; ngân sách 250ms tốn lần
  lượt 0.25s, 0.33s và 0.59–0.65s. Vượt trần tới một nửa thì không còn là trần,
  và chính nó đẩy `plan-gate` quá outer timeout.
- `PRAGMA journal_mode = WAL` được thử lại theo cùng deadline. Câu lệnh này trả
  `SQLITE_BUSY` mà không qua busy handler, nên khi nhiều hook cùng tạo database
  một tiến trình chặn oan ngay lập tức với `cannot open SQLite state` — quan sát
  được trên runner macOS ở lô 32 hook chỉ mất 0.785s, tức chưa hề chạm ngân sách.
- Outer timeout của hook stateful (`plan-gate`, `no-fake-pass`) nâng từ 5s lên
  6s để giữ đúng bất biến "outer ≥ trần busy + 2s" sau khi trần busy lên 4000ms.
- Ghi log JSONL nguyên tử giữa các tiến trình. POSIX `O_APPEND` để kernel chốt
  offset cuối ngay trong `write()`, nhưng CRT của Windows chỉ mô phỏng bằng
  `lseek(END)` rồi `write`, nên hai hook có thể chốt cùng offset và một bản ghi
  bị đè: CI Windows đếm 31 dòng thay vì 32. Nay cả hai nền tảng giữ khoá vùng
  byte độc quyền quanh lần ghi, đặt ngoài vùng dữ liệu; thêm `O_BINARY` để CRT
  không viết `\n` thành `\r\n` và làm lệch trần byte so với byte thật.
- Đóng đường mutation qua `Bash`, `PowerShell`, `Monitor`, worktree và
  `mcp__.*` trước khi plan được duyệt.
- Không còn mở plan gate sớm từ `PreToolUse`, `EnterPlanMode`, `TodoWrite` hoặc
  `EnterWorktree`.
- Receipt cũ sau mutation project-scope và receipt cross-session/prompt/project/
  agent/contract không còn hợp lệ cho `READY`; mutation từ agent khác trong cùng
  project/session/prompt cũng tăng epoch chung và làm stale receipt owner.
- Verification failure hoặc interrupted xoá live chain. Background verification
  taint prompt vĩnh viễn trong prompt đó; sau khi đợi/cancel phải gửi prompt mới
  rồi chạy lại chain.
- Giá trị env số sai format được fallback/clamp thay vì làm hook crash.
- Glossary không còn duyệt nghĩa chỉ vì trùng chữ cái đầu, code fence hoặc cue
  prose; citation phải resolve tới dòng local chứa đúng token và nghĩa.
- `Stop`/`SubagentStop` có `stop_hook_active` truthy được glossary bỏ qua trước
  khi parse định nghĩa, tránh block đệ quy; hỗ trợ cả snake_case và camelCase.
- Miễn trừ plan file dùng native filesystem containment. POSIX không còn chấp
  nhận lexical Windows path; Windows từ chối cross-drive, traversal,
  symlink/junction/reparse point và parent thiếu hoặc không kiểm chứng được.

### Security

- State lưu hash domain-separated thay vì raw runtime ID, plan, project/file
  path, command, cwd, receipt và secret; các giá trị gốc không được persist.
- State/log/dump từ chối symlink và ownership không an toàn; trên POSIX áp dụng
  directory `0700`, file `0600`.
- Diagnostic log/dump được redact, giới hạn kích thước và rotate một backup;
  mutation/`READY` fail-closed khi state an toàn không khả dụng.

### Migration

- Nâng Claude Code/Python theo prerequisites và bảo đảm `python3` có trên
  `PATH` trên mọi OS, sau đó khởi động lại Claude Code. Windows support có điều
  kiện vì hook dùng literal `python3`, không alias sang `python`.
- Lần mở state v1 sẽ migration v1→v2 trong một SQLite transaction: giữ
  `plan_approvals`, xoá/tạo lại mutation và verification tables. Migration lỗi
  rollback nguyên vẹn về v1 thay vì để schema dở dang.
- Tạo hoặc merge idempotent `.claude/verification.json`; khai exact command/cwd
  cho step active và lý do `N/A:` cho từng step `null`.
- Chạy lại toàn bộ active chain theo contract hiện tại; không mang receipt hoặc
  mutation epoch v1 sang v2.
- Rà lại `~/.claude/glossary.txt` trước, rồi chỉ thêm token project không xung
  đột tại `.claude/glossary.txt`.
- Cập nhật builder integration để giữ receipt hook cấp và phát đúng một dòng
  `AGENT_KIT_RESULT_V1` khi kết thúc.
