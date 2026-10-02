#!/usr/bin/env python3
"""
Kiểm kho bài học ~/.claude/learnings (skill `learn`).

  check.py                         kiểm toàn kho, exit 1 nếu có ERROR
  check.py --report                kèm cảnh báo rà soát (candidate để lâu, v.v.)
  check.py --similar "<mô tả>" [--against DIR ...]
                                   liệt kê mục gần giống để kiểm trùng TRƯỚC khi ghi
  check.py --projects              quét thêm auto memory của mọi project
                                   (~/.claude/projects/*/memory): secret, deprecated còn trong mục lục

Chỉ dùng stdlib. Không sửa file nào.
"""
import argparse
import datetime as dt
import pathlib
import re
import sys
import unicodedata

FOLDER_STATUS = {"personal": "verified", "candidates": "candidate", "deprecated": "deprecated"}
REQUIRED_META = ("type", "scope", "status")

SECRET = re.compile(
    r"(?i)(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token|client[_-]?secret)"
    r"\s*[:=]\s*['\"]?[^\s'\"`]{8,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY-----"
    r"|\bAKIA[0-9A-Z]{16}\b"
    r"|\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\."
)
INJECTION = re.compile(
    r"(?i)ignore (?:all |any )?(?:previous|prior|above) instructions|system prompt|"
    r"you must now|disregard (?:the|all)|bỏ qua (?:mọi|các|tất cả) (?:chỉ dẫn|hướng dẫn)"
)
PII = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|(?<!\d)(?:\+84|0)\d{9}(?!\d)")
CHANGELOG_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \| [\w() -]+ \| [\w-]+ \| [\w*.-]+ \|")
INDEX_LINK = re.compile(r"^\s*-\s*\[[^\]]+\]\(([^)]+)\)")


def parse(path: pathlib.Path):
    """Frontmatter tối giản: key: value ở cấp 0, và block metadata thụt lề."""
    text = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m:
        return None, text
    fm, body = m.groups()
    top, meta, in_meta = {}, {}, False
    for line in fm.splitlines():
        if not line.strip():
            continue
        if re.match(r"^\S", line):
            k, _, v = line.partition(":")
            in_meta = k.strip() == "metadata"
            if not in_meta:
                top[k.strip()] = v.strip().strip('"')
        elif in_meta:
            k, _, v = line.strip().partition(":")
            meta[k.strip()] = v.strip().strip('"')
    top["metadata"] = meta
    return top, body


def tokens(s: str) -> set:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn").replace("đ", "d")
    return {w for w in re.split(r"[^a-z0-9]+", s) if len(w) >= 3}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def entries(root: pathlib.Path, extra_dirs=()):
    for folder in FOLDER_STATUS:
        for p in sorted((root / folder).glob("*.md")):
            yield folder, p
    for d in extra_dirs:
        for p in sorted(pathlib.Path(d).expanduser().glob("*.md")):
            if p.name != "MEMORY.md":
                yield "external", p


def check(root: pathlib.Path, max_index: int, report: bool, today: dt.date, stale_days: int):
    errors, warns = [], []
    verified = set()
    descs = []
    for folder, p in entries(root):
        rel = f"{folder}/{p.name}"
        fm, body = parse(p)
        text = p.read_text(encoding="utf-8", errors="replace")
        if SECRET.search(text):
            errors.append(f"{rel}: có chuỗi giống secret — không được lưu vào memory")
        if PII.search(body or ""):
            warns.append(f"{rel}: có email/số điện thoại — kiểm có phải dữ liệu cá nhân không")
        if fm is None:
            errors.append(f"{rel}: thiếu frontmatter")
            continue
        meta = fm["metadata"]
        missing = [k for k in ("name", "description") if not fm.get(k)] + \
                  [f"metadata.{k}" for k in REQUIRED_META if not meta.get(k)]
        if missing:
            errors.append(f"{rel}: thiếu {', '.join(missing)}")
        if fm.get("name") and fm["name"] != p.stem:
            errors.append(f"{rel}: name '{fm['name']}' khác tên file")
        st = meta.get("status")
        if st and st != FOLDER_STATUS[folder]:
            errors.append(f"{rel}: status '{st}' không khớp thư mục {folder}/ (phải là {FOLDER_STATUS[folder]})")
        if st == "verified" and not meta.get("evidence"):
            errors.append(f"{rel}: verified nhưng thiếu metadata.evidence")
        if st == "deprecated" and not meta.get("deprecated_reason"):
            errors.append(f"{rel}: deprecated nhưng thiếu metadata.deprecated_reason")
        if meta.get("scope") and meta["scope"] != "personal":
            warns.append(f"{rel}: scope '{meta['scope']}' — kiến thức riêng project nên ở auto memory của project")
        if folder == "personal":
            verified.add(p.stem)
        if folder != "deprecated":
            descs.append((rel, tokens(fm.get("description", "") + " " + p.stem)))
        if report and folder == "candidates" and meta.get("created"):
            try:
                age = (today - dt.date.fromisoformat(meta["created"])).days
                if age > stale_days:
                    warns.append(f"{rel}: candidate {age} ngày — xác minh (có bằng chứng) hoặc deprecate")
            except ValueError:
                warns.append(f"{rel}: created '{meta['created']}' sai định dạng YYYY-MM-DD")

    idx = root / "INDEX.md"
    listed = set()
    if not idx.exists():
        errors.append("INDEX.md: không tồn tại")
    else:
        lines = idx.read_text(encoding="utf-8", errors="replace").splitlines()
        items = [l for l in lines if INDEX_LINK.match(l)]
        if len(items) > max_index:
            errors.append(f"INDEX.md: {len(items)} mục > giới hạn {max_index} — gộp hoặc deprecate bớt")
        for n, line in enumerate(lines, 1):
            if INJECTION.search(line):
                errors.append(f"INDEX.md:{n}: câu giống chèn chỉ dẫn — INDEX được nạp như instructions")
            if re.search(r"https?://", line):
                errors.append(f"INDEX.md:{n}: có URL — để URL trong file chi tiết, không ở INDEX")
            m = INDEX_LINK.match(line)
            if not m:
                continue
            if len(line) > 240:
                warns.append(f"INDEX.md:{n}: dòng dài {len(line)} ký tự (>240)")
            target = m.group(1)
            if not target.startswith("personal/"):
                errors.append(f"INDEX.md:{n}: chỉ được trỏ vào personal/ (verified), đang trỏ '{target}'")
            if not (root / target).exists():
                errors.append(f"INDEX.md:{n}: link '{target}' không tồn tại")
            listed.add(pathlib.Path(target).stem)
    log_slugs = set()
    ch = root / "CHANGELOG.md"
    if ch.exists():
        for n, line in enumerate(ch.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip() or line.startswith("#") or line.startswith("ngày |"):
                continue
            if not CHANGELOG_LINE.match(line):
                errors.append(f"CHANGELOG.md:{n}: sai định dạng 'YYYY-MM-DD | action | scope | slug | ghi chú'")
            else:
                log_slugs.add(line.split("|")[3].strip())
    else:
        errors.append("CHANGELOG.md: không tồn tại")
    for folder, p in entries(root):
        if p.stem not in log_slugs:
            warns.append(f"{folder}/{p.name}: chưa có dòng nào trong CHANGELOG (thiếu lịch sử)")
    for s in sorted(verified - listed):
        warns.append(f"personal/{s}.md: verified nhưng chưa có trong INDEX.md")

    for i in range(len(descs)):
        for j in range(i + 1, len(descs)):
            score = jaccard(descs[i][1], descs[j][1])
            if score >= 0.5:
                warns.append(f"có thể trùng ({score:.2f}): {descs[i][0]} ~ {descs[j][0]}")
    return errors, warns


def check_projects(projects: pathlib.Path):
    errors, warns, legacy, total = [], [], 0, 0
    for mem in sorted(projects.glob("*/memory")):
        idx = mem / "MEMORY.md"
        index_text = idx.read_text(encoding="utf-8", errors="replace") if idx.exists() else ""
        for p in sorted(mem.glob("*.md")):
            if p.name == "MEMORY.md":
                continue
            total += 1
            rel = f"{mem.parent.name}/memory/{p.name}"
            if SECRET.search(p.read_text(encoding="utf-8", errors="replace")):
                errors.append(f"{rel}: có chuỗi giống secret")
            fm, _ = parse(p)
            st = ((fm or {}).get("metadata") or {}).get("status")
            if not st:
                legacy += 1
            elif st == "deprecated" and f"({p.name})" in index_text:
                warns.append(f"{rel}: deprecated nhưng MEMORY.md vẫn trỏ tới — gỡ dòng mục lục")
    warns.append(f"project memory: {total} file, {legacy} file cũ chưa có metadata.status (coi là verified-legacy)")
    return errors, warns


def similar(root: pathlib.Path, text: str, against):
    q = tokens(text)
    scored = []
    for folder, p in entries(root, against):
        fm, _ = parse(p)
        desc = (fm or {}).get("description", "")
        scored.append((jaccard(q, tokens(desc + " " + p.stem)), f"{folder}/{p.name}" if folder != "external" else str(p), desc))
    scored.sort(reverse=True)
    for score, where, desc in scored[:5]:
        if score > 0:
            print(f"{score:.2f}  {where}  — {desc[:120]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path.home() / ".claude" / "learnings"))
    ap.add_argument("--max-index", type=int, default=60)
    ap.add_argument("--stale-days", type=int, default=14)
    ap.add_argument("--today", default=None, help="YYYY-MM-DD (cho test)")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--similar", default=None)
    ap.add_argument("--against", action="append", default=[], help="thư mục memory khác để so trùng")
    ap.add_argument("--projects", nargs="?", const=str(pathlib.Path.home() / ".claude" / "projects"), default=None)
    a = ap.parse_args()
    root = pathlib.Path(a.root).expanduser()
    if a.similar is not None:
        similar(root, a.similar, a.against)
        return 0
    today = dt.date.fromisoformat(a.today) if a.today else dt.date.today()
    errors, warns = check(root, a.max_index, a.report, today, a.stale_days)
    if a.projects:
        e2, w2 = check_projects(pathlib.Path(a.projects).expanduser())
        errors += e2
        warns += w2
    for e in errors:
        print("ERROR:", e)
    for w in warns:
        print("WARN:", w)
    print(f"== {len(errors)} ERROR, {len(warns)} WARN")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
