#!/usr/bin/env python3
"""
Ghi INDEX.md / CHANGELOG.md của ~/.claude/learnings AN TOÀN khi nhiều phiên chạy song
song: khoá file (fcntl) + ghi nguyên tử (tmp → os.replace). CHANGELOG chỉ nối thêm.

  learnctl.py log <action> <scope> <slug> "<ghi chú>"
  learnctl.py index-add <slug> "<nhóm>" "<mô tả ≤150 ký tự>"
  learnctl.py index-remove <slug>
  learnctl.py move <slug> <personal|candidates|deprecated>   (đổi status theo thư mục)

Mọi lệnh tự ghi một dòng CHANGELOG. --root DIR để test.
"""
import argparse
import contextlib
import datetime as dt
import fcntl
import os
import pathlib
import re
import sys

ACTIONS = {"add", "update", "verify", "deprecate", "merge", "migrate", "policy", "index-add", "index-remove", "move"}
STATUS = {"personal": "verified", "candidates": "candidate", "deprecated": "deprecated"}


@contextlib.contextmanager
def locked(root: pathlib.Path):
    root.mkdir(parents=True, exist_ok=True)
    with open(root / ".lock", "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def atomic_write(path: pathlib.Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def log(root, action, scope, slug, note):
    if action not in ACTIONS:
        sys.exit(f"action phải thuộc {sorted(ACTIONS)}")
    line = f"{dt.date.today().isoformat()} | {action} | {scope} | {slug} | {note.replace(chr(10), ' ')}\n"
    with open(root / "CHANGELOG.md", "a", encoding="utf-8") as f:  # O_APPEND: chỉ nối thêm
        f.write(line)


def index_add(root, slug, group, desc):
    if not (root / "personal" / f"{slug}.md").exists():
        sys.exit(f"personal/{slug}.md không tồn tại — INDEX chỉ nhận mục verified trong personal/")
    desc = desc if len(desc) <= 150 else desc[:147] + "…"
    entry = f"- [{slug}](personal/{slug}.md) — {desc}"
    idx = root / "INDEX.md"
    lines = idx.read_text(encoding="utf-8").splitlines() if idx.exists() else ["# Bài học cá nhân — mục lục", ""]
    lines = [l for l in lines if f"(personal/{slug}.md)" not in l]
    head = f"## {group}"
    if head not in lines:
        lines += ["", head] if lines and lines[-1] != "" else [head]
    at = lines.index(head) + 1
    while at < len(lines) and lines[at].startswith("- "):
        at += 1
    lines.insert(at, entry)
    atomic_write(idx, "\n".join(lines).rstrip() + "\n")


def index_remove(root, slug):
    idx = root / "INDEX.md"
    lines = idx.read_text(encoding="utf-8").splitlines()
    atomic_write(idx, "\n".join(l for l in lines if f"(personal/{slug}.md)" not in l).rstrip() + "\n")


def move(root, slug, dest):
    src = next((root / d / f"{slug}.md" for d in STATUS if (root / d / f"{slug}.md").exists()), None)
    if src is None:
        sys.exit(f"không tìm thấy {slug}.md")
    t = src.read_text(encoding="utf-8")
    t = re.sub(r"^(\s+status:\s*)\S+", rf"\g<1>{STATUS[dest]}", t, count=1, flags=re.M)
    t = re.sub(r"^(\s+updated:\s*)\S+", rf"\g<1>{dt.date.today().isoformat()}", t, count=1, flags=re.M)
    dst = root / dest / f"{slug}.md"
    atomic_write(dst, t)
    if src != dst:
        src.unlink()
    if dest != "personal":
        index_remove(root, slug)
    return src.parent.name


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(pathlib.Path.home() / ".claude" / "learnings"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("log"); p.add_argument("action"); p.add_argument("scope"); p.add_argument("slug"); p.add_argument("note")
    p = sub.add_parser("index-add"); p.add_argument("slug"); p.add_argument("group"); p.add_argument("desc")
    p = sub.add_parser("index-remove"); p.add_argument("slug")
    p = sub.add_parser("move"); p.add_argument("slug"); p.add_argument("dest", choices=sorted(STATUS))
    a = ap.parse_args()
    root = pathlib.Path(a.root).expanduser()
    with locked(root):
        if a.cmd == "log":
            log(root, a.action, a.scope, a.slug, a.note)
        elif a.cmd == "index-add":
            index_add(root, a.slug, a.group, a.desc)
            log(root, "index-add", "personal", a.slug, f"nhóm {a.group}")
        elif a.cmd == "index-remove":
            index_remove(root, a.slug)
            log(root, "index-remove", "personal", a.slug, "")
        elif a.cmd == "move":
            frm = move(root, a.slug, a.dest)
            log(root, "move", "personal", a.slug, f"{frm} → {a.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
