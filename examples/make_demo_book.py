#!/usr/bin/env python3
"""生成一本用于演示的测试书（带文字层的中文 PDF）。

为什么需要它：这个 skill 的核心主张是"总结里的每句引文都能回原文核对"，
要验证这条主张就需要一本**内容已知**的书。这个脚本用 PyMuPDF 造一本 5 页的小书，
并把内容写死在这里，于是 examples/summary-bad.txt 里那句编造的引文必然对不上。

顺带演示一个真实世界的坑：PyMuPDF 自带的 CJK 字体（china-s）没有可用的
ToUnicode 映射，所以 pdftotext 抽这本书会得到乱码，而 PyMuPDF 自己抽是对的——
这正是 booktool.py 要同时跑两个提取器、按"像不像人话"择优的原因。

用法
    python examples/make_demo_book.py --out .bookwork
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# 书名固定，示例总结里引用的都是这些句子；改动这里必须同步改 examples/*.txt
TITLE = "网格系统基础"
AUTHOR = "测试作者"
PAGES = [
    [
        ("网格系统基础", 22),
        ("", 12),
        ("测试作者 编著", 13),
        ("", 12),
        ("演示用书 · 由 make_demo_book.py 生成", 10),
    ],
    [
        ("目　录", 16),
        ("", 12),
        ("第一章　网格系统的基本概念 …………………… 3", 12),
        ("第二章　版心与页边距 …………………………… 4", 12),
        ("第三章　结论 ……………………………………… 5", 12),
    ],
    [
        ("第一章　网格系统的基本概念", 16),
        ("", 12),
        ("网格是把版面划分为栏与行的结构系统。", 12),
        ("基线网格决定正文行距的垂直节律。", 12),
        ("栏宽与字号的比值影响阅读的舒适度。", 12),
    ],
    [
        ("第二章　版心与页边距", 16),
        ("", 12),
        ("版心是承载正文的区域，页边距是它与纸张边缘的距离。", 12),
        ("页边距的比例关系来自古典比例体系。", 12),
        ("内边距通常应小于外边距以利于装订。", 12),
    ],
    [
        ("第三章　结论", 16),
        ("", 12),
        ("网格系统的价值在于建立秩序而非限制创造。", 12),
        ("设计者应在秩序与自由之间取得平衡。", 12),
    ],
]
# 书签大纲：真实章节名，让 outline --chapters 在演示里也有东西可用
OUTLINE = [
    [1, "第一章　网格系统的基本概念", 3],
    [1, "第二章　版心与页边距", 4],
    [1, "第三章　结论", 5],
]


def build(out_dir: Path) -> Path:
    try:
        import pymupdf
    except ImportError:
        print("需要 PyMuPDF 才能生成演示书：\n"
              f'  "{sys.executable}" -m pip install pymupdf', file=sys.stderr)
        raise SystemExit(2)

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "grid-demo.pdf"

    doc = pymupdf.open()
    for i, lines in enumerate(PAGES):
        page = doc.new_page(width=595, height=842)
        y = 110
        for text, size in lines:
            if text:
                page.insert_text((72, y), text, fontsize=size, fontname="china-s")
            y += size + 16
        # 页脚印页码：这本演示书让"印刷页码 == PDF 页号"，省掉页码校准的干扰
        page.insert_text((300, 800), f"— {i + 1} —", fontsize=10, fontname="china-s")

    doc.set_metadata({"title": TITLE, "author": AUTHOR})
    doc.set_toc(OUTLINE)
    doc.save(str(target))
    doc.close()
    return target


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="make_demo_book.py",
                                 description="生成演示用的测试书（带文字层的中文 PDF）")
    ap.add_argument("--out", default=".bookwork", help="输出目录（默认 .bookwork）")
    args = ap.parse_args(argv)

    target = build(Path(args.out))
    print(f"已生成: {target}  ({target.stat().st_size / 1024:.0f} KB, "
          f"{len(PAGES)} 页, 含书签大纲)")
    print(f"书名: {TITLE}｜作者: {AUTHOR}")
    print("下一步：")
    out_dir = os.path.dirname(str(target)) or "."
    print(f'  python scripts/booktool.py probe "{target}" --work {out_dir}')
    print(f'  python scripts/verify.py --summary examples/summary-bad.txt '
          f'--source {out_dir}/grid-demo.pages.txt')
    return 0


if __name__ == "__main__":
    sys.exit(main())
