# Changelog

Tất cả thay đổi đáng chú ý của project được ghi tại đây theo cấu trúc
Keep a Changelog. Project dùng Semantic Versioning.

Release này mang số `1.0.1` từ baseline `1.0.0`. Đây là lựa chọn có chủ đích
của maintainer: mục **Breaking** bên dưới mô tả các thay đổi phá tương thích,
mà theo Semantic Versioning lẽ ra thuộc một major mới — số hiệu `1.0.1` vì vậy
KHÔNG phản ánh mức thay đổi, hãy đọc mục **Breaking** và **Migration** trước
khi nâng cấp.

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
  chạm 2x trần và vượt outer timeout 5s của `plan-gate`.
- `plan-gate` nêu nguyên nhân gốc của `StateError` trong thông điệp chặn và log
  `plan_gate.state_unavailable`, thay vì chỉ một câu chung chung không chẩn
  đoán được từ log CI.
- Nâng trần chờ write lock của SQLite từ 250ms lên 2000ms (trần tối đa 2500ms).
  Mức cũ quá hẹp: khi nhiều hook cùng giành lock trên runner chậm, một tiến
  trình nhận `database is locked` và gate chặn oan một agent hợp lệ. Trần mới
  vẫn nằm dưới ngân sách outer timeout 5s của `plan-gate` và dưới mốc 4s mà
  test lock contention yêu cầu để fail-closed.
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
