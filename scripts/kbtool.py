#!/usr/bin/env python3
"""kbtool.py — 建知识库目录树 / 落库 / 生成分层索引。

配合 skill `book-kb-builder` 使用。

目录约定（用户指定，不可随意改动）：

    知识库/
    └─ 类别/
       └─ 图书名称/
          ├─ <原电子版文件>            ← 原件，原样保留
          └─ <图书名称>_阅读总结.txt    ← 生成的阅读总结

另外在知识库根下维护一个平行目录，用于 RAG 检索与引文核对（可删）：

    知识库/_全文提取/<类别>/<书名>.pages.txt   ← 带 [[p.N]] 页锚的全文

索引策略参考 deep-rag：不做无脑切块，而是保留目录结构并生成一张
"知识地图"（每层目录一句用途、每本书一行要点），让模型先看地图再按需下钻。

用法
    python kbtool.py plan  --kb-root R --category C --title T
    python kbtool.py build --kb-root R --category C --title T --book B --summary S [--extract F]
    python kbtool.py index --kb-root R
    python kbtool.py list  --kb-root R [--category C]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

DEFAULT_KB = "知识库"
SUMMARY_SUFFIX = "_阅读总结.txt"
EXTRACT_ROOT = "_全文提取"
INDEX_NAME = "_索引.md"
CATEGORY_NOTE = "_类别说明.txt"
HEADER_MARK = "【书目信息】"
FIELD_ORDER = ["书名", "作者", "出版", "类别", "关键词", "一句话概括",
               "阅读方式", "覆盖度", "来源文件", "成文日期"]


def norm_path(p: str) -> str:
    p = str(p).strip().strip('"').strip("'")
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        return f"{m.group(1).upper()}:/{m.group(2)}"
    if p.startswith("~"):
        return os.path.expanduser(p)
    return p


def die(msg: str, code: int = 2):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(code)


def sanitize_segment(name: str) -> str:
    """把一段目录名（书名或类别名）清洗成合法文件夹名。

    只替换 Windows 真正不允许的字符，**不做 NFKC 归一化**——那会把
    「中国古典园林史（第三版）」的全角括号改成半角，等于悄悄改了书名。
    目录是要给人看的，书名该长什么样就长什么样。
    结尾的点和空格会被 Windows 静默吞掉，导致后续路径对不上，必须去掉。
    """
    name = str(name).strip().strip("　")
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", " ", name)
    name = re.sub(r"\s*_\s*", "_", name)   # "Typography: A-Z" → "Typography_A-Z"
    name = name.rstrip(". ")
    return name or "未命名"


def category_parts(category: str) -> list[str]:
    """类别可以写成 '设计理论/字体与排版' 这样的多级路径。"""
    parts = [sanitize_segment(p) for p in re.split(r"[/＞>]+", category) if p.strip()]
    if not parts:
        die(f"类别名不合法: {category!r}")
    return parts


def build_category_path(kb_root: Path, category: str) -> Path:
    return kb_root.joinpath(*category_parts(category))


def parse_summary_header(text: str) -> dict:
    """从总结文档的【书目信息】块里取出结构化字段，供索引使用。"""
    fields: dict[str, str] = {}
    lines = text.splitlines()
    started = False
    for raw in lines:
        line = raw.strip()
        if not line:
            if started and fields:
                break
            continue
        if HEADER_MARK in line:
            started = True
            continue
        if not started:
            continue
        if line.startswith("【"):
            break
        m = re.match(r"^[-*\s]*([^：:]{1,12})\s*[：:]\s*(.+)$", line)
        if m:
            key = m.group(1).strip().lstrip("-* ").strip()
            fields[key] = m.group(2).strip()
    return fields


def read_summary(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        die(f"读不了总结文档 {path}: {exc}")
        return ""


def gather_books(kb_root: Path) -> list[dict]:
    """扫描知识库，收集每本书的信息（跳过 _ 开头的辅助目录/文件）。"""
    books = []
    if not kb_root.exists():
        return books
    for root, dirs, files in os.walk(kb_root):
        dirs[:] = [d for d in dirs if not d.startswith("_") and not d.startswith(".")]
        rp = Path(root)
        if rp == kb_root:
            continue
        summaries = [f for f in files if f.endswith(SUMMARY_SUFFIX)]
        if not summaries:
            continue
        # 含总结文档的目录 = 一本书
        summary_path = rp / summaries[0]
        others = [f for f in files if not f.endswith(SUMMARY_SUFFIX)
                  and not f.startswith("_")]
        fields = parse_summary_header(read_summary(summary_path))
        rel = rp.relative_to(kb_root)
        parts = rel.parts
        books.append({
            "dir": rp,
            "rel": str(rel).replace("\\", "/"),
            "category": "/".join(parts[:-1]) or "(未分类)",
            "title": parts[-1],
            "summary_file": summary_path.name,
            "ebook_files": others,
            "fields": fields,
        })
    books.sort(key=lambda b: (b["category"], b["title"]))
    return books


def one_line(book: dict) -> str:
    f = book["fields"]
    bits = []
    if f.get("作者"):
        bits.append(f["作者"])
    if f.get("出版"):
        bits.append(f["出版"])
    return "｜".join(bits)


def category_purpose(books: list[dict], cat_dir: Path) -> str:
    """类别的一句话用途：优先读 _类别说明.txt，否则用本类图书关键词拼。"""
    note = cat_dir / CATEGORY_NOTE
    if note.exists():
        for line in note.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    words: list[str] = []
    for b in books:
        for w in re.split(r"[；;，,、/]+", b["fields"].get("关键词", "")):
            w = w.strip()
            if w and w not in words:
                words.append(w)
    return "、".join(words[:6]) if words else "（待补类别说明）"


def render_category_index(kb_root: Path, category: str, books: list[dict]) -> str:
    cat_dir = kb_root.joinpath(*category.split("/")) if category != "(未分类)" else kb_root
    lines = [f"# {category} —— 类别索引", "",
             "> 本文件由 kbtool.py 自动生成，请勿手工编辑。",
             f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}｜本类图书 {len(books)} 册", "",
             f"**类别用途**：{category_purpose(books, cat_dir)}", ""]
    for b in books:
        f = b["fields"]
        lines.append(f"## {b['title']}")
        lines.append(f"- 目录：`{b['rel']}/`")
        if one_line(b):
            lines.append(f"- {one_line(b)}")
        for key in ("关键词", "一句话概括", "阅读方式", "覆盖度"):
            if f.get(key):
                lines.append(f"- {key}：{f[key]}")
        lines.append(f"- 总结文档：`{b['summary_file']}`")
        if b["ebook_files"]:
            lines.append(f"- 电子版：{'、'.join(f'`{n}`' for n in b['ebook_files'])}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_root_index(kb_root: Path, books: list[dict]) -> str:
    by_cat: dict[str, list[dict]] = {}
    for b in books:
        by_cat.setdefault(b["category"], []).append(b)

    lines = ["# 知识库索引", "",
             "> 本文件由 kbtool.py 自动生成，请勿手工编辑。",
             f"> 生成时间：{datetime.now():%Y-%m-%d %H:%M}｜"
             f"类别 {len(by_cat)} 个｜图书 {len(books)} 册", "",
             "## 怎么用这个知识库", "",
             "1. 先读本文件，确定问题属于哪个类别；",
             "2. 进类别目录读它的 `_索引.md`，锁定具体书目；",
             f"3. 打开该书的 `*{SUMMARY_SUFFIX}` 读总结；需要原文时，",
             f"   到 `{EXTRACT_ROOT}/` 下同名文件里按 `[[p.N]]` 页锚核对。", "",
             "**页锚约定**：`[[p.N]]` 与正文里的 `(p.N)` 都指 PDF 第 N 页；"
             "`(p.N·图)` 表示该页没有可提取文字，结论来自对页面图像的视觉判读。", "",
             "**可信度约定**：正文各节内容均出自原书；凡是标注【联网补充】的段落"
             "来自联网检索，不是原书内容，引用前请自行复核。", "",
             "## 目录地图", "", "```", "知识库/"]
    cats = sorted(by_cat)
    for ci, cat in enumerate(cats):
        cat_books = by_cat[cat]
        last_cat = ci == len(cats) - 1
        cat_prefix = "└─ " if last_cat else "├─ "
        purpose = category_purpose(cat_books, kb_root.joinpath(*cat.split("/")))
        lines.append(f"{cat_prefix}{cat}/  （{len(cat_books)} 册）—— {purpose}")
        for bi, b in enumerate(cat_books):
            last_book = bi == len(cat_books) - 1
            indent = "   " if last_cat else "│  "
            book_prefix = "└─ " if last_book else "├─ "
            meta = one_line(b)
            lines.append(f"{indent}{book_prefix}{b['title']}/"
                         + (f"   {meta}" if meta else ""))
            summary = indent + ("   " if last_book else "│  ") + "   "
            blurb = b["fields"].get("一句话概括", "")
            if blurb:
                lines.append(f"{summary}{blurb}")
    lines += ["```", "", "## 类别明细", ""]
    for cat in cats:
        lines.append(f"- **{cat}**（{len(by_cat[cat])} 册）"
                     f"→ `{cat}/{INDEX_NAME}`")
    return "\n".join(lines).rstrip() + "\n"


def write_indexes(kb_root: Path) -> dict:
    books = gather_books(kb_root)
    by_cat: dict[str, list[dict]] = {}
    for b in books:
        by_cat.setdefault(b["category"], []).append(b)

    written = []
    root_index = kb_root / INDEX_NAME
    root_index.parent.mkdir(parents=True, exist_ok=True)
    root_index.write_text(render_root_index(kb_root, books), encoding="utf-8")
    written.append(str(root_index))

    for cat, cat_books in by_cat.items():
        if cat == "(未分类)":
            continue
        cat_dir = kb_root.joinpath(*cat.split("/"))
        cat_dir.mkdir(parents=True, exist_ok=True)
        idx = cat_dir / INDEX_NAME
        idx.write_text(render_category_index(kb_root, cat, cat_books), encoding="utf-8")
        written.append(str(idx))
    return {"root": str(root_index), "category_indexes": written[1:],
            "books": len(books), "categories": len(by_cat)}


def cmd_plan(args):
    kb_root = Path(norm_path(args.kb_root))
    title = sanitize_segment(args.title)
    cat_dir = build_category_path(kb_root, args.category)
    book_dir = cat_dir / title
    info = {
        "kb_root": str(kb_root),
        "category_dir": str(cat_dir),
        "book_dir": str(book_dir),
        "kb_root_exists": kb_root.exists(),
        "category_exists": cat_dir.exists(),
        "book_dir_exists": book_dir.exists(),
        "existing_books": len(gather_books(kb_root)),
        "existing_categories": sorted({b["category"] for b in gather_books(kb_root)}),
    }
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print(f"知识库根 : {info['kb_root']}"
          f"{'' if info['kb_root_exists'] else '  （不存在，将创建）'}")
    print(f"类别目录 : {info['category_dir']}"
          f"{'' if info['category_exists'] else '  （不存在，将创建）'}")
    print(f"图书目录 : {info['book_dir']}"
          f"{'' if info['book_dir_exists'] else '  （不存在，将创建）'}")
    print(f"现有规模 : {info['existing_books']} 册 / {len(info['existing_categories'])} 类别")
    if info["existing_categories"]:
        print("已有类别 : " + "；".join(info["existing_categories"]))
    return 0


def cmd_build(args):
    kb_root = Path(norm_path(args.kb_root))
    title = sanitize_segment(args.title)
    book_dir = build_category_path(kb_root, args.category) / title
    book_dir.mkdir(parents=True, exist_ok=True)

    book_src = Path(norm_path(args.book))
    if not book_src.exists():
        die(f"找不到电子版: {book_src}")
    summary_src = Path(norm_path(args.summary))
    if not summary_src.exists():
        die(f"找不到总结文档: {summary_src}")

    actions = []

    # 1) 电子版原件
    target = book_dir / book_src.name
    if target.exists():
        if target.stat().st_size == book_src.stat().st_size:
            actions.append(f"电子版已存在且大小一致，跳过: {target.name}")
        elif args.force:
            shutil.copy2(book_src, target)
            actions.append(f"电子版已覆盖: {target.name}")
        else:
            actions.append(f"⚠ 电子版同名但大小不同，已跳过（加 --force 可覆盖）: {target.name}")
    else:
        if args.move:
            shutil.move(str(book_src), str(target))
            actions.append(f"电子版已移动入库: {target.name}")
        else:
            shutil.copy2(book_src, target)
            actions.append(f"电子版已复制入库: {target.name}")

    # 2) 阅读总结
    summary_target = book_dir / f"{title}{SUMMARY_SUFFIX}"
    shutil.copy2(summary_src, summary_target)
    actions.append(f"阅读总结已写入: {summary_target.name}")

    # 3) 页锚全文（有文字层的书才有；供检索与引文核对）
    if args.extract:
        extract_src = Path(norm_path(args.extract))
        if extract_src.exists():
            edir = kb_root / EXTRACT_ROOT
            for seg in category_parts(args.category):
                edir = edir / seg
            edir.mkdir(parents=True, exist_ok=True)
            etarget = edir / f"{title}.pages.txt"
            shutil.copy2(extract_src, etarget)
            actions.append(f"页锚全文已存入检索目录: {etarget.relative_to(kb_root)}")
        else:
            actions.append(f"⚠ 未找到页锚全文，跳过: {extract_src}")

    index_info = write_indexes(kb_root)
    actions.append(f"索引已刷新: {Path(index_info['root']).name} + "
                   f"{len(index_info['category_indexes'])} 个类别索引")

    if args.json:
        print(json.dumps({"book_dir": str(book_dir), "actions": actions,
                          "index": index_info}, ensure_ascii=False, indent=2))
        return 0
    print(f"入库位置: {book_dir}")
    for a in actions:
        print(f"  - {a}")
    print(f"知识库现状: {index_info['books']} 册 / {index_info['categories']} 类别")
    return 0


def cmd_index(args):
    kb_root = Path(norm_path(args.kb_root))
    if not kb_root.exists():
        die(f"知识库根目录不存在: {kb_root}")
    info = write_indexes(kb_root)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    print(f"已刷新索引：{info['root']}")
    for p in info["category_indexes"]:
        print(f"  - {p}")
    print(f"共 {info['books']} 册 / {info['categories']} 类别")
    return 0


def cmd_list(args):
    kb_root = Path(norm_path(args.kb_root))
    books = gather_books(kb_root)
    if args.category:
        books = [b for b in books if b["category"] == args.category]
    if args.json:
        print(json.dumps(books, ensure_ascii=False, indent=2, default=str))
        return 0
    if not books:
        print(f"知识库 {kb_root} 里还没有书（或该类别为空）。")
        return 0
    cur = None
    for b in books:
        if b["category"] != cur:
            cur = b["category"]
            print(f"\n[{cur}]")
        print(f"  {b['title']}  ({one_line(b) or '未填写作者/出版'})")
        print(f"    目录: {b['rel']}/")
    print(f"\n共 {len(books)} 册")
    return 0


def build_parser():
    ap = argparse.ArgumentParser(prog="kbtool.py",
                                 description="知识库建树 / 落库 / 分层索引")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=fn)
        p.add_argument("--kb-root", default=DEFAULT_KB,
                       help=f"知识库根目录（默认 ./{DEFAULT_KB}）")
        return p

    p = add("plan", cmd_plan, "预演：显示将创建的路径，不落盘")
    p.add_argument("--category", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--json", action="store_true")

    p = add("build", cmd_build, "落库：建目录、放电子版与总结、刷新索引")
    p.add_argument("--category", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--book", required=True, help="电子版文件路径")
    p.add_argument("--summary", required=True, help="生成的阅读总结 txt 路径")
    p.add_argument("--extract", help="带页锚的全文（有文字层时传入）")
    p.add_argument("--move", action="store_true",
                   help="移动而非复制电子版（慎用，默认复制）")
    p.add_argument("--force", action="store_true", help="同名电子版不同时覆盖")
    p.add_argument("--json", action="store_true")

    p = add("index", cmd_index, "只刷新索引")
    p.add_argument("--json", action="store_true")

    p = add("list", cmd_list, "列出知识库现有图书")
    p.add_argument("--category", help="只看某个类别")
    p.add_argument("--json", action="store_true")

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
