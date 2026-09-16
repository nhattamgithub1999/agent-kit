# agent-kit

Plugin cho Claude Code, gồm một bộ subagent và một vòng kiểm chứng. Mục đích là
buộc agent làm việc theo ba nguyên tắc:

- **Không bịa.** Mọi khẳng định phải có nguồn thật. Chỗ nào không tra được thì
  phải hiện ra là chưa rõ, không được lấp cho câu văn liền mạch.
- **Có mục tiêu.** Mỗi task phải có tiêu chí hoàn thành kiểm chứng được, và phải
  có điều kiện dừng để không lặp vô hạn.
- **Review lại.** Việc làm xong phải đi qua vòng verify (build, typecheck, lint,
  test) và qua cổng phản biện.

Phiên bản: **v1.0.3**. Profile mặc định: DOCTRINE — plugin tiêm luật và ghi
log, **không hook nào chặn bằng exit code**. Ba hook chặn vẫn đi kèm repo, bật
bằng tay, xem mục "Bật lại ba gate".

## Cài đặt

Trong một phiên Claude Code:

```
/plugin marketplace add nhattamgithub1999/agent-kit
/plugin install agent-kit@agent-kit
```

Hoặc từ terminal:

```bash
claude plugin marketplace add nhattamgithub1999/agent-kit
claude plugin install agent-kit@agent-kit
```

Cài xong cần khởi động lại phiên để Claude Code nạp agent và hook.

## Nó giải quyết vấn đề gì

Năm kiểu sai mà một agent lập trình hay mắc, và bộ kit này xử lý mỗi kiểu bằng
cách nào:

| Kiểu sai | Trông như thế nào | Bộ kit làm gì |
|---|---|---|
| Báo cáo khống | "Đã sửa xong, test pass hết" — nhưng chưa chạy lệnh test nào | Luật trong `policy/common.md` và `agents/builder.md`: khẳng định pass phải kèm lệnh + output thật. Kiểm bằng `no-fake-pass.py`, **mặc định không bật** |
| Nhảy vào code | Sửa file ngay từ câu đầu, chưa ai biết "xong" nghĩa là gì | Luật Bước 0 trong `policy/supervisor.md`: đọc file → khai bậc → plan có DoD. Kiểm bằng `plan-gate.py`, **mặc định không bật** |
| Giao việc mù | Lập plan và gọi subagent khi chưa mở một file nào của repo | Cùng luật Bước 0. Kiểm bằng `flow-gate.py`, **mặc định không bật** |
| Plan không có người làm | Plan liệt kê việc nhưng không ghi ai làm bước nào | Luật "plan có người phụ trách": mỗi bước một nhãn `[agent]` + một dòng DoD |
| Bỏ qua quy trình | Policy nằm trong `CLAUDE.md` ở quá xa nên lượt đầu quên mất | `session-policy.py` và `prompt-intake.py` đưa policy vào đúng chỗ, đúng lúc — **đây là hai hook chạy mặc định** |

Nói thẳng ranh giới: ở bản mặc định, bộ kit **không chặn** bạn hay agent làm sai.
Nó nạp luật vào đúng chỗ, và ghi log khi thấy tín hiệu nghi ngờ. Prompt chỉ làm
giảm xác suất, không triệt tiêu — muốn ràng buộc bằng exit code thì bật ba gate
theo mục "Bật lại ba gate".

Ranh giới giữa "hook làm thật" và "chỉ là chữ" được liệt kê tường minh ở cuối
`policy/supervisor.md`. Đọc mục đó trước khi tin rằng một luật nào đó tự giữ được.

## Luồng chạy bên trong plugin

Đây là vòng đời của một lượt làm việc. Ô có ổ khoá là hook — tức là chỗ plugin can
thiệp thật vào runtime, không phải chỗ nhắc nhở bằng chữ.

```mermaid
flowchart TD
    S([Mở phiên]) --> H1["🔔 SessionStart<br/>session-policy.py"]
    H1 -->|"nạp common.md + supervisor.md"| P([User gửi prompt])
    P --> H2["🔔 UserPromptSubmit<br/>prompt-intake.py"]
    H2 -->|"nhắc quy ước, KHÔNG phán lớp"| RC["Main session<br/>đọc file thật trước"]
    RC --> M["Main session<br/>khai bậc · plan có nhãn · DoD"]
    M --> DEL{"Việc này của ai?"}

    DEL -->|"tra cứu"| EX["Explore<br/>read-only"]
    DEL -->|"thiết kế"| AR["architect<br/>đề xuất phương án"]
    DEL -->|"phản biện"| CR["critic<br/>không có tool"]

    H8["🔔 SubagentStart · session-policy.py<br/>nạp common.md + worker.md"]
    H8 -.-> EX
    H8 -.-> AR
    H8 -.-> BU
    AR --> PL
    DEL -->|"implement"| PL["Parent lập plan CHO builder<br/>các bước + tiêu chí nghiệm thu"]
    PL --> VE["verifier<br/>đối chiếu plan với code thật"]
    VE -->|"VERDICT SAFE_TO_BUILD"| BU["builder"]
    VE -->|"VERDICT BLOCK"| PL

    BU --> WR["Ghi file<br/>rồi chạy skill verify-loop"]
    WR --> H4["🔔 SubagentStop<br/>skill-nudge.py · gloss-gate.py (warn)"]
    H4 --> OUT([Trả lời user])
    EX --> OUT
    CR --> OUT
    OUT --> H5["🔔 Stop<br/>memory-nudge.py · gloss-gate.py (warn)"]
```

Chuông 🔔 nghĩa là hook có chạy nhưng **không chặn**. Bản mặc định không có ổ
khoá nào; sơ đồ này là sơ đồ thật, không phải sơ đồ mong muốn.

Đọc sơ đồ theo ba tầng:

1. **Trước khi nghĩ.** `session-policy.py` và `prompt-intake.py` đưa quy ước vào
   context. Cả hai chỉ *nhắc*; không cái nào phán prompt thuộc lớp nào.
2. **Trước khi giao việc.** Không có hook nào ở đây. `verifier` là cổng vào
   `builder`, nhưng nó là cổng do **bạn** gọi, không phải cổng do runtime ép.
3. **Sau khi làm xong.** `skill-nudge.py` gợi ý đúc kết skill, `memory-nudge.py`
   gợi ý lưu memory, `gloss-gate.py` ghi log token nghi bịa nghĩa. Cả ba đều chỉ
   gợi ý.

## Bên trong có gì

| Thành phần | Nội dung |
|---|---|
| `agents/` | Năm subagent: `Explore` (haiku), `architect` và `critic` (opus), `builder` và `verifier` (sonnet) |
| `hooks/` | Tám hook Python. Năm cái chạy mặc định (không chặn), ba cái là gate tuỳ chọn chưa đăng ký trong `hooks.json`. Xem bảng ở mục dưới |
| `skills/verify-loop/` | Skill chạy vòng verify: build, typecheck, lint, test |
| `policy/common.md` | Luật áp cho mọi agent. Vào cả phiên chính lẫn subagent |
| `policy/supervisor.md` | Luật điều phối. Chỉ vào phiên chính |
| `policy/worker.md` | Luật thực thi. Chỉ vào subagent, không chứa bảng Routing |
| `VERIFICATION.template.md` | Mẫu để khai lệnh build/test của từng project |
| `glossary.example.txt` | File mẫu glossary. `verifier` tra file này khi đối chiếu nghĩa viết tắt |
| `optional/orchestrator.md` | Bản thay thế system prompt. Không bật mặc định |

### Năm subagent, mỗi cái một việc

| Agent | Dùng khi | Không dùng để |
|---|---|---|
| `Explore` | Tra cứu, tìm file, đọc hiểu hiện trạng | Sửa file, ra quyết định thiết kế |
| `architect` | Đề xuất phương án, so sánh trade-off | Tự sửa code |
| `builder` | Implement một thay đổi đã rõ phạm vi | Việc còn mơ hồ, hoặc cần quyết kiến trúc |
| `verifier` | Đối chiếu từng claim với codebase thật | Phản biện logic |
| `critic` | Phản biện độ chặt của lập luận | Sửa code |

Hai agent cuối là hai cổng chất lượng **khác nhau**, đừng dùng thay nhau.
`verifier` trả lời câu hỏi "thứ này có tồn tại không" nên nó có tool để đọc
codebase. `critic` trả lời câu hỏi "lập luận có chặt không" nên nó **không** có
tool, và chỉ được xem câu hỏi gốc cùng câu trả lời chứ không xem quá trình suy
luận — đó là điều giữ cho nó độc lập.

### Năm hook chạy mặc định — không cái nào chặn

Prompt chỉ làm giảm xác suất agent làm sai. Bản mặc định của kit **không** dựng
cổng chặn; nó đặt luật vào đúng chỗ và để lại dấu vết khi thấy tín hiệu nghi ngờ.

| Hook | Chạy lúc | Làm gì | Chặn? |
|---|---|---|---|
| `session-policy.py` | Mở phiên, và mỗi khi một subagent khởi động | Đưa policy vào context — plugin không đọc được `CLAUDE.md` nên đây là đường duy nhất. Phiên chính nhận luật điều phối, subagent nhận luật thực thi | Không |
| `prompt-intake.py` | Người dùng gửi prompt | Nhắc quy ước vì policy đã trôi xa trong context. Chỉ *nhắc*, không phán lớp | Không |
| `gloss-gate.py` | `Stop`, `SubagentStop` | Ghi log token viết tắt bị mở rộng nghĩa mà chữ cái đầu không khớp. Đăng ký sẵn ở chế độ `GLOSS_GATE=warn` | Không, chỉ ghi `~/.claude/gloss-gate.log` |
| `memory-nudge.py` | Sau `Write`/`Edit`, và `Stop` | Gợi ý lưu memory khi lượt có tín hiệu quyết định/điều chỉnh mà chưa thấy ghi vào `memory/*.md` | Không |
| `skill-nudge.py` | `builder` kết thúc | Gợi ý cân nhắc đúc kết `SKILL.md` khi task chạm ≥3 file và đã `VERDICT: READY`. Không tự ghi vào `skills/` | Không |

Cả năm đều **fail-open**: không đọc được dữ liệu đầu vào thì trả `exit 0`.

Hai ngưỡng của `skill-nudge` không phải số mới đặt ra — chúng lấy đúng ngưỡng đã
có: "≥3 file" là ngưỡng escalation trong `agents/builder.md`, `VERDICT: READY` là
output contract của skill `verify-loop`.

### Bật lại ba gate

`flow-gate.py`, `plan-gate.py` và `no-fake-pass.py` nằm trong `hooks/` nhưng
**không** có trong `hooks.json`. Chúng chặn thật bằng `exit 2`:

| Hook | Chạy lúc | Chặn cái gì |
|---|---|---|
| `flow-gate.py` | Trước `Read`/`Grep`/`Glob`/`Bash`/`Agent`/`ExitPlanMode`/`Edit`/`Write` | Lập plan hoặc giao việc khi lượt này chưa đọc file nào; giao việc bằng prompt cụt; gọi agent không khớp nhãn plan; giao `builder` khi chưa qua `verifier` hoặc prompt chưa chứa plan; `builder` ghi file khi chưa được duyệt |
| `plan-gate.py` | Trước khi ghi file | Nhảy vào sửa code khi chưa có plan |
| `no-fake-pass.py` | `builder` kết thúc | Báo "đã pass" mà không kèm lệnh đã chạy và output thật |

Bật bằng cách thêm chúng vào `hooks/hooks.json`. Mẫu đăng ký đầy đủ của bản 1.0.2
lấy ra bằng:

```bash
git show bcf22e2:hooks/hooks.json
```

Ba biến `FLOW_GATE=off`, `PLAN_GATE=off`, `NOFAKEPASS_AGENTS=` tắt lại từng cái
mà không cần sửa file.

**Vì sao mặc định tắt.** Bản 1.0.2 bật cả ba, và đo thật ngày 07/09/2026 cho thấy
hai trong ba cái đang hỏng theo hai hướng ngược nhau:

- `flow-gate` khoá dấu duyệt theo `prompt_id`. `prompt_id` **không bắc cầu** qua
  ranh giới cha–con một khi phiên chính đã đóng lượt trước lúc `builder` thực thi
  `Edit`, nên `builder` luôn thấy `builder_ok` không tồn tại và **bị chặn oan**
  dù parent đã làm đúng vòng duyệt.
- `no-fake-pass` đọc `agent_type` ở top-level payload `SubagentStop`, nhưng giá
  trị đó là **chuỗi rỗng** — matcher lọc đúng, chỉ field là rỗng. Hook fail-open,
  tức là **không bao giờ bắn**.

Bản 1.0.3 sửa cả hai (`hooks/flow-gate.py` khoá theo `session_id`;
`hooks/no-fake-pass.py` suy tên agent từ matcher khi chỉ theo dõi đúng một agent),
nhưng để mặc định tắt: bản sửa chưa chạy đủ lâu trên việc thật để đáng bật cho cả
team. Ai muốn siết thì bật và báo lại kết quả.

### Vòng duyệt trước khi builder được ghi file

`builder` không tự lập plan cho mình. Thứ tự bắt buộc:

1. **Parent lập plan** cho việc sắp giao, rồi nhúng thẳng vào prompt giao việc:
   ít nhất 2 bước, và ít nhất một dòng tiêu chí nghiệm thu.
2. **`verifier` đối chiếu plan với code thật.** `VERDICT: BLOCK` thì sửa plan,
   không giao `builder`.
3. **Parent chốt** bằng chính lời gọi spawn.

Ở bản mặc định, cả ba mắt xích là **kỷ luật của người điều phối**, không có hook
nào cưỡng chế. Bỏ bước 2 thì không có gì báo cho bạn biết. Muốn biến nó thành
ràng buộc thật thì bật `flow-gate` theo mục trên.

## Policy: đưa vào context bằng cách nào

Plugin của Claude Code **không** nạp `CLAUDE.md` đặt ở gốc plugin
([tài liệu](https://code.claude.com/docs/en/plugins-reference)). Vì vậy khối
policy không thể đi theo đường đó.

Thay vào đó, hook `session-policy.py` đọc các file trong `policy/` rồi trả nội
dung về qua `hookSpecificOutput.additionalContext` ở event `SessionStart`. Muốn
sửa policy thì sửa đúng file đó, vì không có bản copy nào khác trong repo.

Tài liệu chính thức không nói rõ `SessionStart` có nhận `additionalContext` hay
không, nên điều này đã được kiểm bằng thực nghiệm có đối chứng: đặt một chuỗi
canary chỉ tồn tại trong file policy, rồi so sánh phiên có bật hook với phiên
`POLICY_HOOK=off`. Phiên bật hook đọc được canary, phiên tắt hook thì không.

## Một bước bắt buộc cho từng project

```bash
cat VERIFICATION.template.md >> <project>/.claude/CLAUDE.md
```

Sau đó điền lệnh build, typecheck, lint và test **thật** của project đó. Bỏ bước
này thì agent phải tự suy đoán lệnh, và như vậy là mất luôn nguyên tắc không bịa.

## Glossary — nên làm ngay

```bash
cp glossary.example.txt ~/.claude/glossary.txt
```

Rồi điền dần vào đó, mỗi dòng một cặp `VIẾTTẮT = nghĩa chính thức`.

Với những token có trong file này, `verifier` đối chiếu trực tiếp nghĩa mà agent
viết ra với nghĩa chính thức bạn đã khai (`agents/verifier.md`, mục Quy trình
bước 5). Mâu thuẫn thì gán nhãn `FABRICATED`; không có nguồn thì `UNVERIFIABLE`,
kể cả khi chữ cái đầu khớp. Việc này trước đây do hook làm bằng cách so chữ cái
đầu, và đã bị gỡ vì bắt nhầm quá nhiều — xem "Những giới hạn đã biết".

Chỉ thêm một dòng khi bạn **đã xác nhận** nghĩa của nó. Một dòng sai ở đây sẽ hợp
thức hoá đúng loại lỗi mà file này sinh ra để chặn.

## Điều chỉnh bằng biến môi trường

Plugin không có cách khai báo biến môi trường
([tài liệu](https://code.claude.com/docs/en/plugins-reference)), nên các hook dùng
giá trị mặc định của profile THOROUGH. Muốn đổi thì `export` trong shell trước khi
chạy `claude`.

| Biến | Mặc định | Tác dụng |
|---|---|---|
| `ROUTE_MIN_CHARS` | 12 | Prompt ngắn hơn ngưỡng này thì không phân loại |
| `PLAN_GATE_FREE_EDITS` | 0 | Số lần ghi file được miễn trước khi gate bắt đầu chặn |
| `PLAN_GATE` | — | Đặt `off` để tắt plan gate |
| `PLAN_GATE_PLAN_TOOLS` | — | Thêm tool được tính là "đã có plan", cách nhau bằng dấu phẩy |
| `NOFAKEPASS_AGENTS` | `builder` | Agent nào bị soi khi khẳng định "đã pass" |
| `NOFAKEPASS_STRICT` | — | Đặt `1` để chặn cả khi không nhận diện được agent nào đang chạy |
| `GLOSS_GATE` | `warn` (đặt sẵn trong `hooks.json`) | `block` là chặn thật, `off` là tắt hẳn. Xem "Những giới hạn đã biết" trước khi đặt `block` |
| `GLOSS_MIN_LEN` | 3 | Độ dài tối thiểu của viết tắt mới bị soi |
| `MEMORY_NUDGE` | — | Đặt `off` để tắt gợi ý lưu memory |
| `SKILL_NUDGE` | — | Đặt `off` để tắt gợi ý đúc kết skill |
| `SKILLNUDGE_AGENTS` | `builder` | Agent nào được soi để gợi ý đúc kết skill |
| `FLOW_GATE` | — | Đặt `off` để tắt flow gate, nếu bạn đã tự bật nó |
| `FLOW_GATE_MIN_STEPS` | 2 | Số bước tối thiểu prompt giao `builder` phải có |
| `FLOW_GATE_REQUIRE_VERIFIER` | — | Đặt `0` để bỏ yêu cầu `verifier` chạy trước `builder` |
| `POLICY_HOOK` | — | Đặt `off` để không đưa policy vào context |
| `POLICY_FILE` | — | Trỏ tới file policy khác |

## Hiệu quả: đo được gì, chưa đo được gì

Phần này viết theo đúng nguyên tắc mà bộ kit đòi ở agent — số nào có thì nói, số
nào chưa có thì nói là chưa có.

### Đo được, và bạn tự chạy lại được ngay

| Chạy cái này | Ra cái này | Nó chứng minh gì |
|---|---|---|
| `claude plugin validate . --strict` | Validation passed, không warning | Manifest, hook config và frontmatter của cả 5 agent đều đúng schema |

Đó là lệnh duy nhất bạn kiểm lại được từ bản phát hành này.

### Đo được, nhưng bạn phải tin tôi

Hai số dưới đây đo bằng `kit-selfcheck.py`, script kiểm cấu hình nội bộ **không
đóng gói kèm plugin**:

| Số đo nội bộ | Kết quả | Nó chứng minh gì |
|---|---|---|
| 145 check ngữ nghĩa cấu hình | PASS 145, FAIL 0 | 145 ràng buộc về **giá trị** cấu hình đang đúng: model tier từng agent, ngưỡng số, tool nào bị cấm, và tính nhất quán chéo giữa các file |
| Đối chứng âm: tiêm 30 defect | Bắt 30/30 | Validator bị tiêm 30 lỗi thật rồi phải bắt đủ 30 — nó không phải loại luôn báo pass |

Số thứ hai đáng tin hơn số thứ nhất, vì một validator luôn nói "ổn" thì vô dụng,
mà chỉ có đối chứng âm mới phân biệt được hai loại đó.

Nhưng cả hai đều là số **tôi báo lại**, không phải số bạn kiểm lại được từ repo
này. Hãy đọc chúng đúng ở mức đó. Bản ghi đo nội bộ trước khi đóng gói ở mức 141
check và 28/28 defect; con số hiện tại cao hơn vì mỗi lỗi tìm được trong quá trình
đóng gói thành plugin đều được thêm một check để nó không tái diễn. Bản ghi đó giữ
nguyên số cũ — sửa số trong một bản ghi đo là đúng loại việc mà bộ kit này tồn tại
để chặn.

### Gate có bắn thật không — sổ ghi từ chính lúc làm repo này

Sáu lần gate can thiệp vào chính việc làm ra repo này, tất cả đều nằm trong git
log hoặc trong log hook trên máy:

| Bị chặn ở đâu | Đúng hay oan | Xử lý |
|---|---|---|
| `plan-gate` chặn lệnh ghi file khi phiên chưa có plan | Đúng | Không nới gate. Ba đường thoát nó nêu ra đều dùng được |
| `gloss-gate` chặn vì token có gạch nối bị tách sai | Oan | Sửa: dấu gạch nối phải có khoảng trắng hai bên |
| `gloss-gate` chặn chính dòng phân loại mà policy bắt buộc phải in | Oan, và là tự mâu thuẫn | Sửa: thêm danh sách từ vựng của chính kit vào diện bỏ qua |
| `gloss-gate` chặn vì toán tử so sánh bị coi là dấu gán nghĩa | Oan | Sửa: dấu gán phải đứng độc lập, không phải phần của `==`, `=>`, `!=` |
| `gloss-gate` chặn ba lần liên tiếp một báo cáo, trong đó có `VERIFY`, `POST`, `IDE` | Oan | Không vá nữa. Gỡ hẳn khỏi `hooks.json`: ba lần vá trước cho thấy đây là lỗi cơ chế, không phải lỗi danh sách miễn trừ |
| `no-fake-pass` cho qua mọi lượt của `builder` suốt nhiều tháng | Không bắn, và không ai biết | `agent_type` runtime là `agent-kit:builder`, không khớp `{"builder"}`. Sửa so khớp theo tên trần |
| `no-fake-pass` VẪN không bắn sau lần sửa trên (đo 07/09/2026) | Không bắn, lần thứ hai | `agent_type` ở top-level payload là **chuỗi rỗng**, nên so khớp kiểu gì cũng trượt. Sửa: khi chỉ theo dõi đúng một agent thì tin matcher đã lọc đúng, không fail-open nữa |
| `flow-gate` chặn `builder` ghi file dù parent đã qua đủ vòng duyệt (đo 07/09/2026) | Oan, 100% số lần | Dấu duyệt khoá theo `prompt_id`, mà `prompt_id` không bắc cầu qua ranh giới cha–con khi phiên chính đã đóng lượt. Sửa: khoá `verified`/`builder_ok` theo `session_id`, đúng pattern `plan-gate.py` vốn không dính lỗi này |

Mỗi lần cắn oan đều thành fix kèm test hồi quy, và không lần nào gate bị nới ra
cho dễ chịu. Nhưng bốn dòng cuối bảng dạy một bài khác, đắt hơn:

- **Vá ba lần rồi vẫn oan thì vấn đề nằm ở cơ chế, không nằm ở danh sách miễn
  trừ.** `gloss-gate` được vá ba lần trước khi có ai hỏi liệu "so chữ cái đầu" có
  thật sự đo được chuyện bịa nghĩa hay không. Câu trả lời là không.
- **Một gate im lặng nguy hiểm hơn một gate cắn oan.** `gloss-gate` cắn oan nên bị
  phát hiện và sửa ngay. `no-fake-pass` thì cho qua 100% và không phát ra tín hiệu
  nào, nên nó chết âm thầm rất lâu trong khi tài liệu vẫn gọi nó là "chốt tất định
  duy nhất". Từ đó rút ra: cổng nào cũng cần một cách kiểm rằng **nó vẫn đang bắn**,
  không chỉ kiểm rằng nó chặn đúng.
- **Sửa một gate rồi tin là xong, không đo lại, là cách nó chết lần thứ hai.**
  `no-fake-pass` được sửa một lần vì tên agent có tiền tố, và tài liệu ghi là đã
  khắc phục. Mãi tới lần đo 07/09 mới lộ ra nó vẫn im, vì nguyên nhân thật nằm ở
  chỗ khác: field rỗng chứ không phải sai tên. Cả hai lần đều "sửa đúng thứ mình
  nhìn thấy", và chỉ lần đo bằng payload thật mới nói được thứ nào thực sự sai.
- **Đó là lý do bản 1.0.3 để ba gate mặc định tắt.** Một cổng vừa được sửa chưa
  chạy đủ lâu trên việc thật thì chưa đủ tư cách chặn việc của cả team.

### Chưa đo được — và tại sao chưa

Bản ghi đo nội bộ khai thẳng những chỉ số **chưa có số**: tỉ lệ bịa, tỉ lệ defect
lọt lưới, tỉ lệ có plan, tỉ lệ delegate, tổng chi phí, và tỉ lệ chặn oan trên task
nhỏ. Lý do ghi trong đó: không có baseline thì con số đo sau vô nghĩa.

Vì vậy đừng đọc bộ kit này như một thứ đã được chứng minh làm giảm tỉ lệ bịa bao
nhiêu phần trăm. Cái đo được là **cấu hình đúng như thiết kế** và **cơ chế chặn có
hoạt động**. Cái chưa đo được là **kết quả cuối trên việc thật**.

Bản ghi đó còn nói rõ một điều mà tài liệu quảng cáo thường bỏ qua: khi so hai
phiên bản mà chỉ có số đo tĩnh, kết luận duy nhất rút ra được là bản mới **đắt
hơn**, chứ không phải tốt hơn.

### Chi phí đã biết

| Khoản | Lượng | Ghi chú |
|---|---|---|
| Khối policy | 106 dòng, 5.877 ký tự, một lần mỗi phiên | Vào context ở `SessionStart`, không phải mỗi lượt |
| Khối nhắc quy ước | 417 ký tự, khoảng 119 token mỗi lượt | Đo bằng cách chạy `hooks/prompt-intake.py` với payload mẫu |
| Ba hook chặn (nếu bạn tự bật) | Không tốn token | Chúng chỉ đọc payload và trả exit code |
| `gloss-gate` / `memory-nudge` / `skill-nudge` | Không tốn token khi im lặng | `memory-nudge` và `skill-nudge` chỉ tốn token ở lượt chúng thật sự gợi ý |

Quy đổi ký tự sang token dùng ước lượng 3,5 ký tự một token cho văn bản Việt–Anh
trộn. Đó là **ước lượng**, không phải đo bằng tokenizer thật: con số ký tự là đếm
được, con số token thì không.

## Những giới hạn đã biết

**`gloss-gate` chạy ở chế độ `warn`, không phải `block`, và đây là lý do.** Cơ chế của nó là so
chữ cái đầu của cụm từ đứng sau dấu hai chấm với token viết hoa đứng trước. Cơ chế
đó tất định về mặt tính toán nhưng **không tương quan** với việc có bịa nghĩa hay
không, nên nó bắt nhầm mọi câu tiếng Việt kỹ thuật có dạng `TOKEN` + dấu hai chấm
+ một cụm từ. Số đo trên `~/.claude/gloss-gate.log`: trong 60 lần chặn, 30 lần là
token `VERIFY` — tức là hook chặn đúng câu `CHƯA VERIFY: <lý do>` mà chính
`policy/common.md` bắt buộc agent phải viết khi không chạy được lệnh verify.
Kit phạt sự trung thực. Các lần chặn khác gồm `POST`, `GET`, `IDE`, `FINDINGS` —
đều là heading hoặc câu thường.

Bản 1.0.2 gỡ hẳn nó khỏi `hooks.json`. Bản 1.0.3 đăng ký lại, nhưng ghim
`GLOSS_GATE=warn` ngay trong `hooks.json`: hook ghi log vào
`~/.claude/gloss-gate.log` để bạn còn thấy tín hiệu, và không chặn ai cả. Muốn nó
chặn thật thì `export GLOSS_GATE=block` — đọc lại đoạn trên trước khi làm vậy.
Việc chống bịa nghĩa viết tắt vẫn thuộc về `verifier`, nơi có tool để tra glossary
thật thay vì đoán qua chữ cái đầu.

**Ba gate mặc định tắt.** `flow-gate`, `plan-gate`, `no-fake-pass` không có trong
`hooks.json`. Bản mặc định vì thế **không chặn** bất cứ điều gì bằng exit code:
mọi luật trong `policy/` phụ thuộc vào việc người điều phối tự giữ. Lý do và cách
bật lại ở mục "Bật lại ba gate".

**`no-fake-pass` chỉ nhận bằng chứng ở ba dạng:** block code, dòng bắt đầu bằng
`$ <lệnh>`, hoặc câu ghi rõ `CHƯA VERIFY`. Nhắc tên lệnh bằng inline backtick
không được tính là bằng chứng.

**Policy có tới được subagent hay không thì CHƯA VERIFY.** Hook có chạy ở
`SubagentStart` và test hộp đen xác nhận nó **sinh ra** đúng khối `worker.md`
(test 22, 25, 26). Nhưng thứ chưa kiểm được là runtime có thật sự **giao** khối
đó vào context của subagent hay không. Subagent là một
context riêng, nên rất có thể nó không nhận khối policy này — khác với bản cài thủ
công, nơi `CLAUDE.md` tới được mọi agent. Bù lại, các luật cốt lõi đã được viết
thẳng vào từng file trong `agents/`, nên subagent không đi làm mà tay trắng. Dù
vậy đây vẫn là điều chưa đo, không phải điều đã bảo đảm.

**Mọi số đo của kit đều là kiểm tĩnh.** Chúng đo cấu hình có đúng hay không, chứ
không đo được agent có thật sự ngừng bịa hay không.

## Tự kiểm

```bash
claude plugin validate . --strict   # manifest và component của plugin
python3 tests/test_hooks.py         # test hộp đen cho hooks/*.py, chạy hook thật
```

Bộ kiểm ngữ nghĩa 145 check và đối chứng âm 30 defect là công cụ nội bộ, không
phát hành kèm plugin. Kết quả của chúng ghi ở mục Hiệu quả bên trên.

## Đóng góp

`main` là branch được bảo vệ, mọi thay đổi đi qua pull request. Trước khi mở PR,
chạy lệnh ở mục Tự kiểm và dán output vào phần mô tả.

## Tác giả

Phát triển tại **Phòng ISCSU2**. Người phát triển: **TamBN3** — tambn3@fpt.com.

Báo lỗi hoặc góp ý thì mở issue trên repo, kèm output của lệnh ở mục Tự kiểm.

## Giấy phép

MIT. Xem file `LICENSE`.
