#!/usr/bin/env python3
"""
Stop / SubagentStop hook — chặn BỊA NGHĨA TỪ VIẾT TẮT.

VẤN ĐỀ NÓ GIẢI:
  "NDVLDTT" -> agent tự mở rộng thành "Người thanh toán tại nhà", không nguồn.
  Loại bịa này lọt qua mọi rule no-fabrication khác vì với model nó giống ĐỌC HIỂU
  chứ không giống KHẲNG ĐỊNH — model lấp khoảng trống để câu văn liền mạch.

TÍN HIỆU TẤT ĐỊNH:
  Viết tắt tiếng Việt ghép từ chữ đầu mỗi ÂM TIẾT. Nên nghĩa được mở rộng phải có
  chữ cái đầu KHỚP với viết tắt. Không khớp = bịa, không cần model tự giác.

      NDVLDTT               -> N D V L D T T   (7)
      "Người thanh toán tại nhà" -> N T T T N   (5)   -> KHÔNG KHỚP -> chặn

  Khớp nhưng KHÔNG có dấu nguồn (`file:line`, "theo ...", block code) -> cũng chặn
  ở profile thorough, vì khớp chữ cái không chứng minh nghĩa đúng.

CHỈNH:
  GLOSS_GATE=off          tắt hẳn
  GLOSS_GATE=warn         chỉ ghi log, không chặn
  GLOSS_MIN_LEN=4         độ dài viết tắt tối thiểu (mặc định 3)

GLOSSARY ĐÃ DUYỆT (khuyến nghị dùng):
  ~/.claude/glossary.txt hoặc <repo>/.claude/glossary.txt, mỗi dòng:
      NDVLDTT = <nghĩa chính thức>     # thêm sau khi anh xác nhận
  Token có trong glossary sẽ được đối chiếu TRỰC TIẾP với nghĩa chính thức —
  mâu thuẫn thì chặn ngay, không cần đoán qua chữ cái đầu.
  Viết tắt kỹ thuật phổ quát (API, MCP, SQL...) được bỏ qua mặc định.

Exit 2 = chặn, stderr quay lại cho model.
FAIL-OPEN: không parse được thì exit 0.
"""
import json
import os
import pathlib
import re
import sys
import unicodedata

LOG = pathlib.Path.home() / ".claude" / "gloss-gate.log"
MODE = os.environ.get("GLOSS_GATE", "block").lower()
MIN_LEN = int(os.environ.get("GLOSS_MIN_LEN", "3"))

# "ABC (nghĩa)" | "ABC = nghĩa" | "ABC: nghĩa" | "ABC là nghĩa" | "ABC - nghĩa"
# SỬA 22/08/2026: dash PHẢI có khoảng trắng hai bên. Trước đây "-" trần khiến mọi
# token HOA-CÓ-GẠCH-NỐI bị parse sai: FAIL-OPEN -> acr="FAIL", gloss="OPEN khi..."
# -> chữ cái không khớp -> chặn oan. Bằng chứng: hook chặn chính báo cáo cài kit.
# SỬA 22/08/2026 (2): "=" không được là một phần của == => != >= <= := . Phép SO SÁNH
# không phải ĐỊNH NGHĨA. Bằng chứng: "HEAD == local HEAD" bị parse thành cặp
# viết-tắt/nghĩa rồi chặn oan.
GLOSS = re.compile(
    r"\b([A-ZĐ][A-ZĐ0-9]{%d,11})\b(?:\s*\(([^)\n]{4,70})\)|"
    r"(?:\s*(?<![=!<>:])[=:](?![=>])\s*|\s+[–—-]\s+|\s+là\s+)([^\n.,;)]{4,70}))" % (MIN_LEN - 1)
)
# Dấu hiệu có nguồn thật đi kèm
EVIDENCE = re.compile(r"(`[^`\n]+:\d+`|\btheo\b|\bnguồn\b|```|\bxem\b\s+`)", re.I)
# Viết tắt kỹ thuật phổ quát — có trong training data, không cần nguồn.
KNOWN = {
    "API", "MCP", "HTTP", "HTTPS", "JSON", "XML", "SQL", "REST", "CRUD", "DTO",
    "ORM", "JWT", "SSO", "OIDC", "RBAC", "CQRS", "DDD", "TDD", "CI", "CD", "SDK",
    "URL", "URI", "UUID", "CSV", "YAML", "DNS", "TLS", "SLA", "DLQ", "MQ", "RAG",
    "LLM", "UAT", "PRD", "BRD", "URD", "BA", "PM", "QA", "POC", "MVP", "KPI",
}

# TỪ VỰNG CỦA CHÍNH KIT — KHÔNG phải viết tắt. Policy BẮT BUỘC mở lượt bằng dòng
# phân loại ("IMPLEMENT: ..." / "IMPLEMENT (...)"), và verifier phải phát nhãn
# GROUNDED/UNVERIFIABLE/FABRICATED. Không loại trừ thì gate chặn đúng cái định dạng
# mà kit yêu cầu — tự khoá chính nó. Bằng chứng: chặn dòng "Phân loại: IMPLEMENT (...)".
# Đây là danh sách TƯỜNG MINH, không phải suy đoán "trông giống từ thật".
KIT_VOCAB = {
    # nhãn phân loại của Bước 0 + route-prompt
    "IMPLEMENT", "EXPLORE", "DESIGN", "REVIEW", "BUILD", "AMBIGUOUS", "PLAN",
    # nhãn của verifier / critic / builder
    "GROUNDED", "UNVERIFIABLE", "FABRICATED", "VERDICT", "BLOCK", "BLOCKED",
    "READY", "QUESTION", "FACT", "PASS", "FAIL", "STOP", "SKIP", "DONE", "WARN",
    "NOTE", "TODO", "ATTEMPT", "CAP", "OPEN", "STRICT", "OFF", "ON",
    # git / tooling
    "HEAD", "MAIN", "MASTER", "ORIGIN", "README", "LICENSE", "ROOT", "DUMP",
    # placeholder metasyntax (viết trong tài liệu, không phải viết tắt nghiệp vụ)
    "TOKEN", "NAME", "VALUE", "KEY", "PATH", "EVENT", "FOO", "BAR", "BAZ", "XXX",
}
KNOWN |= KIT_VOCAB

# Glossary đã duyệt của tổ chức: mỗi dòng "VIẾTTẮT = nghĩa chính thức".
# Nằm ở ~/.claude/glossary.txt hoặc <repo>/.claude/glossary.txt
def load_glossary() -> dict:
    g = {}
    for p in (pathlib.Path.home() / ".claude" / "glossary.txt",
              pathlib.Path.cwd() / ".claude" / "glossary.txt"):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    g[strip_accents(k.strip()).upper()] = v.strip()
        except OSError:
            pass
    return g


# Âm tiết nối thường bị lược khi tạo viết tắt tiếng Việt
FILLER = {"và", "của", "tại", "trong", "cho", "với", "các", "về", "theo", "the",
          "of", "for", "and", "a", "an"}


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return s.replace("Đ", "D").replace("đ", "d")


def initials(gloss: str) -> str:
    words = [w for w in re.split(r"[\s/–—-]+", gloss.strip()) if w]
    words = [w for w in words if w.lower() not in FILLER]
    return "".join(strip_accents(w[0]).upper() for w in words if w[:1].isalpha())


def is_subsequence(a: str, b: str) -> bool:
    it = iter(b)
    return all(ch in it for ch in a)


def log(msg: str) -> None:
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(msg.rstrip() + "\n")
    except OSError:
        pass


def walk_strings(o, d=0):
    if d > 8:
        return
    if isinstance(o, str):
        yield o
    elif isinstance(o, dict):
        for v in o.values():
            yield from walk_strings(v, d + 1)
    elif isinstance(o, list):
        for v in o:
            yield from walk_strings(v, d + 1)


def last_message(payload) -> str:
    """Stop/SubagentStop cấp sẵn text lượt cuối. Transcript ghi BẤT ĐỒNG BỘ và có
    thể chưa có lượt hiện tại -> đọc transcript là bỏ lọt đúng thứ cần kiểm."""
    if isinstance(payload, dict):
        for k in ("last_assistant_message", "lastAssistantMessage"):
            v = payload.get(k)
            if isinstance(v, str) and v.strip():
                return v
            if isinstance(v, dict):  # có thể là message object
                parts = [b.get("text", "") for b in (v.get("content") or [])
                         if isinstance(b, dict)]
                if any(parts):
                    return "\n".join(parts)
    return ""


def read_text(payload) -> str:
    for s in walk_strings(payload):
        if s.endswith(".jsonl") and os.path.isfile(s):
            try:
                return pathlib.Path(s).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
    return "\n".join(walk_strings(payload))


def main() -> int:
    if MODE == "off":
        return 0
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        log("FAIL-OPEN: stdin không phải JSON")
        return 0

    text = last_message(payload) or read_text(payload)
    if not text.strip():
        log("FAIL-OPEN: không lấy được nội dung")
        return 0

    glossary = load_glossary()
    bad = []
    for m in GLOSS.finditer(text):
        acr = strip_accents(m.group(1)).upper()
        gloss = (m.group(2) or m.group(3) or "").strip()
        if not gloss or gloss.isupper():          # ABC (XYZ) = alias, không phải gloss
            continue
        ini = initials(gloss)
        if len(ini) < 2:
            continue
        if acr in glossary:
            # Có định nghĩa chính thức -> đối chiếu thẳng, đây là tín hiệu MẠNH NHẤT.
            if initials(glossary[acr]) != ini:
                bad.append((acr, gloss, ini,
                            f"MÂU THUẪN glossary: chính thức là \"{glossary[acr]}\""))
            continue
        if acr in KNOWN:
            continue
        matched = is_subsequence(ini, acr) or is_subsequence(acr, ini)
        # Bối cảnh quanh chỗ gloss có dấu nguồn không
        ctx = text[max(0, m.start() - 200): m.end() + 200]
        sourced = bool(EVIDENCE.search(ctx))
        if not matched:
            bad.append((acr, gloss, ini, "CHỮ CÁI KHÔNG KHỚP"))
        elif not sourced:
            bad.append((acr, gloss, ini, "khớp chữ cái nhưng KHÔNG có nguồn"))

    if not bad:
        return 0

    lines = [f"  {a} → \"{g}\" (chữ đầu: {i}) — {why}" for a, g, i, why in bad[:5]]
    msg = (
        "BLOCKED bởi gloss-gate: có vẻ bạn đã TỰ MỞ RỘNG NGHĨA từ viết tắt mà không "
        "có nguồn.\n" + "\n".join(lines) +
        "\n\nQuy tắc: KHÔNG đoán nghĩa từ viết tắt / thuật ngữ nghiệp vụ. Giữ "
        "NGUYÊN VĂN token và đánh dấu [CHƯA RÕ: <token>], rồi tra theo thứ tự "
        "(1) tài liệu trong repo (2) MCP knowledge base (3) hỏi user. "
        "Không có nguồn thì HỎI, đừng suy từ chữ cái đầu."
    )
    log(f"{'WARN' if MODE == 'warn' else 'BLOCK'} {len(bad)} gloss: "
        + "; ".join(f"{a}->{g}" for a, g, _i, _w in bad[:5]))
    if MODE == "warn":
        return 0
    print(msg, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
