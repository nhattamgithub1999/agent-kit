#!/usr/bin/env python3
"""Route each actionable user prompt to the workspace's specialist agent.

This is a ``UserPromptSubmit`` hook. It is deliberately fail-open for malformed
hook payloads: routing context is useful, but it must never prevent Claude Code
from accepting a user message.
"""

import json
import os
import re
import sys
import unicodedata
from typing import Dict, Optional, Set, Tuple


DEFAULT_MIN_CHARS = 12
MIN_MIN_CHARS = 0
MAX_MIN_CHARS = 4096


def _route_min_chars() -> int:
    """Return a bounded threshold without letting a bad env value crash import."""

    raw = os.environ.get("ROUTE_MIN_CHARS", str(DEFAULT_MIN_CHARS))
    try:
        value = int(raw.strip(), 10)
    except (AttributeError, TypeError, ValueError):
        return DEFAULT_MIN_CHARS
    return max(MIN_MIN_CHARS, min(value, MAX_MIN_CHARS))


# Only complete, short acknowledgements are ignored. Prefix matching is unsafe:
# ``ok, now fix ...`` and ``@file review ...`` are actionable prompts.
ACKNOWLEDGEMENT = re.compile(
    r"(?:"
    r"ok(?:ay|e)?|ừ|uh|đúng|được|đồng ý|tiếp|continue|"
    r"yes|no|y|n|đã rõ|hiểu rồi|understood|got it|"
    r"thanks?(?: you)?|cảm ơn(?: bạn)?(?: nhé)?"
    r")[.!?…]*",
    re.IGNORECASE,
)

WRITE_VERB = re.compile(
    r"\b(?:"
    r"sửa|thêm|triển khai|viết|tạo|xoá|xóa|đổi|cập nhật|"
    r"fix|add|implement|write|create|refactor|delete|remove|update|"
    r"migrate|patch|change|edit|apply"
    r")\b",
    re.IGNORECASE,
)
# A fix mentioned as the object of a recommendation is not itself an
# instruction to mutate. The span ends at that noun/verb, so an explicit
# ``implement``/``apply`` after it remains an independent positive action.
RECOMMENDATION_FIX = re.compile(
    r"\b(?:"
    r"(?:đề\s+xuất|khuyến\s+nghị)\s+"
    r"(?:(?:một|các)\s+)?(?:cách\s+)?(?:sửa(?:\s+lỗi)?|khắc\s+phục)"
    r"|(?:suggest(?:s|ed|ing)?|recommend(?:s|ed|ing)?|"
    r"propos(?:e|es|ed|ing))\s+"
    r"(?:(?:an?|the|some)\s+)?(?:fix(?:es)?|changes?)"
    r")\b",
    re.IGNORECASE,
)
# Clause boundaries are intentionally punctuation-only. Coordinators such as
# ``but``/``nhưng`` remain in the clause so directly contradictory instructions
# are not promoted to BUILD merely because they contain a conjunction.
CLAUSE_BREAK = re.compile(r"[,;.!?…\r\n]+")
NEGATION_BEFORE_WRITE = re.compile(
    r"(?:"
    r"\bkhông(?!\s+chỉ\b)|\bđừng|\bchớ|"
    r"\bdo\s+not(?!\s+only\b)|\bdon['’]?t(?!\s+only\b)|"
    r"\bnot(?!\s+only\b)|\bnever|\bwithout|\bno"
    r")"
    r"(?:[ \t]+[\w'’\-]+){0,3}[ \t:–—-]*$",
    re.IGNORECASE,
)
READ_ONLY_MARKER = re.compile(
    r"(?P<only_review>\b(?:"
    r"(?:review|audit|check)\s+only|only\s+(?:review|audit|check)|"
    r"chỉ\s+(?:review|audit|check|kiểm\s+tra|rà\s+soát)"
    r")\b)"
    r"|\b(?:read[ -]?only|readonly|chỉ\s+đọc)\b"
    r"|\bno(?:\s+[\w'’\-]+){0,2}\s+"
    r"(?:changes?|edits?|modifications?|implementations?)\b"
    r"|\bwithout(?:\s+[\w'’\-]+){0,2}\s+"
    r"(?:implementing|applying|changing|editing|modifying|writing)\b",
    re.IGNORECASE,
)
NOT_ONLY_PREFIX = re.compile(r"\b(?:không|not)\s+$", re.IGNORECASE)
READ_VERB = re.compile(
    r"\b(?:"
    r"tìm|ở đâu|liệt kê|đọc|xem|tra cứu|"
    r"where|find|grep|list|read|search|locate"
    r")\b",
    re.IGNORECASE,
)
DESIGN_VERB = re.compile(
    r"\b(?:"
    r"kiến trúc|thiết kế|nên dùng|so sánh|phương án|đánh đổi|"
    r"architecture|design|compare|trade[ -]?offs?|options?|scale|schema"
    r")\b",
    re.IGNORECASE,
)
REVIEW_VERB = re.compile(
    r"\b(?:"
    r"review|audit|critique|check|đánh giá|phản biện|kiểm tra(?: lại)?|"
    r"rà(?: soát| lại)|có vấn đề gì"
    r")\b",
    re.IGNORECASE,
)
LOGIC_TARGET = re.compile(
    r"\b(?:"
    r"câu trả lời|kế hoạch|lập luận|luận điểm|logic|thiết kế|kiến trúc|"
    r"answer|response|plan|reasoning|argument|design|architecture"
    r")\b",
    re.IGNORECASE,
)
CODE_TARGET_WORD = re.compile(
    r"\b(?:"
    r"code|source|source code|codebase|file|module|function|class|repo|"
    r"repository|project|security|vulnerability|dependencies|"
    r"mã nguồn|tệp|hàm|lớp|dự án|bảo mật|lỗ hổng|phụ thuộc"
    r")\b",
    re.IGNORECASE,
)
FILE_TARGET = re.compile(
    r"(?:"
    r"@[\w./\\-]+|`[^`\r\n]+`|"
    r"(?:^|\s)[\w.-]+(?:[/\\][\w.@() +\-]+)+|"
    r"(?:^|\s)[\w@() +\-]+\.(?:py|pyi|js|jsx|ts|tsx|java|kt|kts|go|"
    r"rs|rb|php|cs|cpp|cxx|cc|c|h|hpp|swift|scala|sh|bash|zsh|ps1|"
    r"sql|vue|svelte|json|ya?ml|toml|xml|md)\b"
    r")",
    re.IGNORECASE,
)


# This text describes workspace conventions instead of impersonating an
# out-of-band system command. Claude Code injects it next to the user prompt.
BASE = (
    "Quy ước xử lý task trong workspace này: mỗi task đi theo trình tự phân loại "
    "→ plan 3–7 bước → DoD kiểm chứng được. Task không có tiêu chí kiểm chứng "
    "được thì được làm rõ bằng câu hỏi trước, không suy đoán."
)

ROUTES: Dict[str, str] = {
    "EXPLORE": (
        "Prompt này thuộc lớp TRA CỨU. Lớp này do agent `Explore` (haiku) "
        "đảm nhiệm; main session không grep dàn trải."
    ),
    "BUILD": (
        "Prompt này thuộc lớp IMPLEMENT. Lớp này do agent `builder` đảm nhiệm "
        "khi đã có DoD; chưa có DoD thì task được làm rõ trước khi viết code."
    ),
    "DESIGN": (
        "Prompt này thuộc lớp THIẾT KẾ. Lớp này do agent `architect` đảm nhiệm, "
        "đầu ra là 2–3 phương án kèm tiêu chí và khuyến nghị; lượt này không sửa code."
    ),
    "CODE_REVIEW": (
        "Prompt này thuộc lớp CODE_REVIEW. Lớp này do agent `reviewer` chỉ-đọc "
        "đảm nhiệm và báo phát hiện kèm vị trí file; lượt này không sửa code."
    ),
    "REVIEW": (
        "Prompt này thuộc lớp PHẢN BIỆN. Lớp này do agent `critic` đảm nhiệm, "
        "và chỉ nhận câu hỏi gốc + answer, không nhận reasoning/trace."
    ),
    "AMBIGUOUS": (
        "Chưa xác định được prompt này thuộc lớp nào. Quy ước ở đây là làm rõ "
        "phạm vi và DoD bằng tối đa 2 câu hỏi trước khi hành động."
    ),
}


def _normalise(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def is_acknowledgement(prompt: str) -> bool:
    return ACKNOWLEDGEMENT.fullmatch(_normalise(prompt).strip()) is not None


def _clause_index(text: str, position: int) -> int:
    return sum(1 for _match in CLAUSE_BREAK.finditer(text, 0, position))


def _clause_prefix(text: str, position: int) -> str:
    start = 0
    for boundary in CLAUSE_BREAK.finditer(text, 0, position):
        start = boundary.end()
    return text[start:position]


def _is_negated_write(text: str, position: int) -> bool:
    return NEGATION_BEFORE_WRITE.search(_clause_prefix(text, position)) is not None


def _mutation_signals(text: str) -> Tuple[bool, bool, bool]:
    """Return ``(positive_write, read_only, same_clause_conflict)``.

    Negated write matches are masked by recording only their read-only clause.
    A positive match in another clause remains actionable. When both signals
    occupy one clause, routing cannot safely choose which instruction wins.
    """

    positive_clauses: Set[int] = set()
    read_only_clauses: Set[int] = set()
    recommendation_spans = tuple(
        (match.start(), match.end()) for match in RECOMMENDATION_FIX.finditer(text)
    )
    for match in WRITE_VERB.finditer(text):
        clause = _clause_index(text, match.start())
        if any(start <= match.start() < end for start, end in recommendation_spans):
            continue
        if _is_negated_write(text, match.start()):
            read_only_clauses.add(clause)
        else:
            positive_clauses.add(clause)

    for match in READ_ONLY_MARKER.finditer(text):
        if match.group("only_review"):
            prefix = text[max(0, match.start() - 24) : match.start()]
            # ``không chỉ review`` / ``not only review`` introduces an additive
            # action, not a read-only constraint.
            if NOT_ONLY_PREFIX.search(prefix):
                continue
        read_only_clauses.add(_clause_index(text, match.start()))

    return (
        bool(positive_clauses),
        bool(read_only_clauses),
        bool(positive_clauses & read_only_clauses),
    )


def classify(prompt: str) -> str:
    """Classify deterministically, with mutation requests taking precedence."""

    text = _normalise(prompt)
    write, read_only, mutation_conflict = _mutation_signals(text)
    code_target = (
        CODE_TARGET_WORD.search(text) is not None
        or FILE_TARGET.search(text) is not None
    )
    recommendation = RECOMMENDATION_FIX.search(text) is not None
    review = REVIEW_VERB.search(text) is not None or (
        recommendation and read_only and code_target
    )
    logic_target = LOGIC_TARGET.search(text) is not None

    # A request to change the artefact is implementation even when phrased as a
    # review. Read-only code review wins over critique when both targets appear.
    if mutation_conflict:
        return "AMBIGUOUS"
    if write:
        return "BUILD"
    if review and (code_target or (read_only and not logic_target)):
        return "CODE_REVIEW"
    if review and logic_target:
        return "REVIEW"
    if review:
        return "AMBIGUOUS"
    if DESIGN_VERB.search(text) is not None:
        return "DESIGN"
    if READ_VERB.search(text) is not None:
        return "EXPLORE"
    return "AMBIGUOUS"


def _extract_prompt(payload: object) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("prompt", "user_prompt", "message", "text"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0

    prompt = _extract_prompt(payload)
    if prompt is None:
        return 0
    if len(prompt) < _route_min_chars() or is_acknowledgement(prompt):
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"{BASE}\n{ROUTES[classify(prompt)]}",
        }
    }
    print(json.dumps(output, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
