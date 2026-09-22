#!/usr/bin/env python3
"""verify.py — 阅读总结的"防编造"校验闸门。

配合 skill `book-kb-builder` 使用。

这份校验存在的理由：总结文档最容易出的问题不是写得不漂亮，而是
**看起来很像那么回事，其实原书没说过**。所以把可以机械检查的部分全部
交给脚本，不给"感觉应该没错"留空间：

  1. 结构完整性     必备章节是否齐全（缺了就说明漏读了）
  2. 页锚合法性     每个 (p.N) 是否落在书的页数范围内
  3. 引文逐字核对   每段「原文」是否真能在原书对应位置找到（有文字层时）
  4. 联网标注合规   网址只能出现在【联网补充】里，且必须带来源/日期/可信度
  5. 覆盖度声明     必须老实写出读了多少、略过多少
  6. 疑似无锚断言   带数字/年份却没有页锚的句子，单独列出来让作者核对

用法
    python verify.py --summary 总结.txt --source 页锚全文.pages.txt
    python verify.py --summary 总结.txt --book 原书.pdf --work .bookwork
    python verify.py --summary 总结.txt --pages 805 --json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

HEADER_MARK = "【书目信息】"
REQUIRED_SECTIONS = ["图书概况", "内容总结", "研究的问题", "研究方向",
                     "原文摘录", "覆盖度"]
OPTIONAL_SECTIONS = ["内容结构", "核心概念", "联网补充", "阅读方法", "术语"]
WEB_SECTION = "联网补充"
HEADER_REQUIRED_FIELDS = ["书名", "类别"]

# 引文引号：中文直角引号/弯引号/直引号。《》是书名号，不算引文。
QUOTE_PATTERNS = [
    re.compile(r"「([^」]{4,400})」"),
    re.compile(r"“([^”]{4,400})”"),
    re.compile(r'"([^"]{4,400})"'),
]
ANCHOR_RE = re.compile(r"[（(\[]\s*p\.\s*(\d+)\s*(·\s*图)?\s*[）)\]]")
PAGE_MARK_RE = re.compile(r"\[\[p\.(\d+)\]\]")
URL_RE = re.compile(r"https?://[^\s，。；）)】\]]+")
ELLIPSIS_RE = re.compile(r"……|…|\.\.\.|⋯")

# 断言里的"硬信息"——出现这些数字却没有页锚，通常是编造的温床
HARD_FACT_RE = re.compile(
    r"(\d+(\.\d+)?\s*%|\d{3,4}\s*年|\d+\s*(万|亿|千|百)?\s*(字|页|幅|张|件|人|种|册|版次))")
SCALE_WORDS = ("全书", "共", "约", "达", "超过", "近", "累计")

PUNCT_TO_DROP = "　 \t\r\n·—–-…⋯,，.。;；:：!！?？\"'“”‘’「」『』()（）[]【】《》<>/\\|~^"


def norm_path(p: str) -> str:
    p = str(p).strip().strip('"').strip("'")
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        return f"{m.group(1).upper()}:/{m.group(2)}"
    if p.startswith("~"):
        return sys.modules["os"].path.expanduser(p)
    return p


def normalize_for_match(s: str) -> str:
    """压平到"只留实义字符"，用来做引文比对。

    提取/OCR 会引入空格、全半角、标点差异，这些都不该让一段真实的引文
    被判成编造；反过来，只要字符序列对不上，就说明它不在原文里。
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.lower()
    for ch in PUNCT_TO_DROP:
        s = s.replace(ch, "")
    s = "".join(c for c in s if not c.isspace())
    return s


class Source:
    """带页锚的原文，支持"这段引文出现在第几页"的反查。"""

    def __init__(self, text: str):
        self.pages: list[tuple[int, str]] = []     # (页号, 原文)
        self.flat = ""                              # 压缩后的全文
        self.bounds: list[tuple[int, int]] = []     # (起始偏移, 页号)
        self._max_page = 0

        blocks: list[tuple[int, str]] = []
        buf: list[str] = []
        page_no: int | None = None
        for line in text.splitlines():
            m = PAGE_MARK_RE.match(line.strip())
            if m:
                if page_no is not None:
                    blocks.append((page_no, "\n".join(buf)))
                page_no = int(m.group(1))
                buf = []
            else:
                buf.append(line)
        if page_no is not None:
            blocks.append((page_no, "\n".join(buf)))

        for pno, body in blocks:
            self._max_page = max(self._max_page, pno)
            norm = normalize_for_match(body)
            if not norm:
                continue
            self.bounds.append((len(self.flat), pno))
            self.flat += norm
            self.pages.append((pno, body))

    @property
    def page_count(self) -> int:
        # 用标记里的最大页号，而不是"有内容的页数"——末页常常是空白的
        return self._max_page

    def locate(self, fragment: str) -> int | None:
        """返回该片段所在页码；找不到返回 None。"""
        needle = normalize_for_match(fragment)
        if len(needle) < 3:
            return None
        pos = self.flat.find(needle)
        if pos < 0:
            return None
        page = None
        for offset, pno in self.bounds:
            if offset <= pos:
                page = pno
            else:
                break
        return page


class Report:
    def __init__(self):
        self.checks: list[dict] = []

    def add(self, cid: str, title: str, level: str, detail: list[str], hint: str = ""):
        self.checks.append({"id": cid, "title": title, "level": level,
                            "detail": detail, "hint": hint})

    @property
    def failures(self):
        return [c for c in self.checks if c["level"] == "fail"]

    @property
    def warnings(self):
        return [c for c in self.checks if c["level"] == "warn"]

    def render(self) -> str:
        icon = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}
        out = []
        for c in self.checks:
            out.append(f"{icon.get(c['level'], '·')} {c['title']}")
            for d in c["detail"][:14]:
                out.append(f"     {d}")
            if len(c["detail"]) > 14:
                out.append(f"     …另有 {len(c['detail']) - 14} 条同类问题")
            if c["hint"]:
                out.append(f"     → {c['hint']}")
        out.append("")
        n_fail, n_warn = len(self.failures), len(self.warnings)
        if n_fail:
            out.append(f"结论：不通过（{n_fail} 项必须修，{n_warn} 项待核）")
            out.append("修法：删掉无法核实的句子，或补上能核实的页锚；"
                       "绝不要为了「看起来完整」而保留没有出处的断言。")
        elif n_warn:
            out.append(f"结论：通过（{n_warn} 项待人工确认）")
        else:
            out.append("结论：全部通过")
        return "\n".join(out)


# --------------------------------------------------------------------------
def header_span(lines: list[str]) -> tuple[int, int] | None:
    """【书目信息】块的行范围。

    这个块里的 "覆盖度：精读 3 页 / 全书 3 页" 这类字段行长得很像章节标题，
    必须先把它排除掉，否则会被当成"有覆盖度章节"，让检查形同虚设。
    """
    start = None
    for i, line in enumerate(lines):
        if HEADER_MARK in line:
            start = i
            break
    if start is None:
        return None
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s.startswith("【") or (not s and j > start + 1):
            return (start, j)
    return (start, len(lines))


def find_sections(text: str) -> dict[str, tuple[int, int]]:
    """定位各章节的行范围。标题允许带 '一、' '####' 等前缀。"""
    lines = text.splitlines()
    hspan = header_span(lines)
    marks: list[tuple[int, str]] = []
    names = REQUIRED_SECTIONS + OPTIONAL_SECTIONS
    for i, line in enumerate(lines):
        if hspan and hspan[0] <= i < hspan[1]:
            continue
        s = line.strip()
        if not s or len(s) > 60:
            continue
        s_clean = re.sub(r"^[#>*\-\s]*", "", s)
        s_clean = re.sub(r"^[一二三四五六七八九十0-9]+\s*[、.．)）]\s*", "", s_clean)
        for name in names:
            if s_clean.startswith(name) or (len(s_clean) <= 24 and name in s_clean):
                marks.append((i, name))
                break
    out: dict[str, tuple[int, int]] = {}
    for idx, (line_no, name) in enumerate(marks):
        end = marks[idx + 1][0] if idx + 1 < len(marks) else len(lines)
        if name not in out:            # 同名只取第一次
            out[name] = (line_no, end)
    return out


def check_structure(text: str, rep: Report):
    if HEADER_MARK not in text:
        rep.add("header", "书目信息块", "fail",
                [f"缺 {HEADER_MARK}，索引无法提取书名/作者/关键词"],
                hint="按模板在文档最前面补上【书目信息】块")
    else:
        head = text.split(HEADER_MARK, 1)[1]
        head = head.split("【", 1)[0]
        missing = [f for f in HEADER_REQUIRED_FIELDS if f not in head]
        if missing:
            rep.add("header", "书目信息块", "warn",
                    [f"缺字段：{'、'.join(missing)}"],
                    hint="补全后可让类别索引自动生成得更准确")
        else:
            rep.add("header", "书目信息块", "ok", [])

    secs = find_sections(text)
    missing = [s for s in REQUIRED_SECTIONS if s not in secs]
    if missing:
        rep.add("sections", "必备章节", "fail",
                [f"缺少章节：{'、'.join(missing)}"],
                hint="这些章节是总结的骨架，缺一个通常意味着有一块内容没读或没写")
    else:
        rep.add("sections", "必备章节", "ok",
                [f"齐全（可选章节：{'、'.join(s for s in OPTIONAL_SECTIONS if s in secs) or '无'}）"])
    return secs


def _cited_page_in(segment: str) -> int | None:
    """从一段文字里取它标注的页锚页码（没有则 None）。"""
    m = ANCHOR_RE.search(segment)
    return int(m.group(1)) if m else None


def _page_in_range(page: int, start: int | None, end: int | None) -> bool:
    """页锚允许写成区间（(p.7-9)）——比对时按区间命中即可。

    取不到区间时退化成"只认 start"。
    """
    if start is None:
        return page == end
    if end is None or start == end:
        return page == start
    lo, hi = (start, end) if start <= end else (end, start)
    return lo <= page <= hi


def check_anchors(text: str, secs: dict, page_count: int | None, rep: Report):
    anchors = [int(m.group(1)) for m in ANCHOR_RE.finditer(text)]
    visual = [m for m in ANCHOR_RE.finditer(text) if m.group(2)]
    pages = sorted(set(anchors))
    detail = [f"页锚 {len(anchors)} 处，覆盖 {len(pages)} 个不同页面"
              + (f"（{pages[0]}-{pages[-1]}）" if pages else "")]
    if visual:
        detail.append(f"其中视觉判读锚 {len(visual)} 处（(p.N·图)）")
    if not anchors:
        rep.add("anchors", "页锚", "fail",
                ["全文没有任何页锚——无法判断哪些内容真的来自原书"],
                hint="每个结论后面补 (p.N)；没有可提取文字的书用 (p.N·图)")
    elif page_count and max(anchors) > page_count:
        bad = sorted({a for a in anchors if a > page_count})
        rep.add("anchors", "页锚", "fail",
                [f"超出总页数 {page_count}：{bad[:10]}"],
                hint="页锚必须落在书里；扫描版注意用 --offset 换算印刷页码")
    elif page_count and len(pages) <= 2 and page_count > 40:
        rep.add("anchors", "页锚", "warn",
                detail + [f"但这是一本 {page_count} 页的书，页锚过于集中，"
                          "很可能只读了开头"],
                hint="回到原书扩大阅读范围，或在覆盖度里如实说明只读了这几页")
    else:
        rep.add("anchors", "页锚", "ok", detail)

    # 覆盖度：正文有专节最好；只有页眉字段的话勉强算过，但会提醒补写
    ratio_re = re.compile(r"\d+\s*/\s*\d+|\d+\s*%|约|其中|略过|未读|未涉及|跳读")
    if "覆盖度" in secs:
        s, e = secs["覆盖度"]
        body = "\n".join(text.splitlines()[s:e])
        if re.search(r"\d", body) and ratio_re.search(body):
            rep.add("coverage", "覆盖度声明", "ok", ["正文已写明阅读范围与数量"])
        else:
            rep.add("coverage", "覆盖度声明", "warn",
                    ["覆盖度章节没有写出具体数量（页数/百分比）"],
                    hint="写成「精读 X 页 / 全书 Y 页，其中第…章未读」这样可核对的形式")
    else:
        m = re.search(r"覆盖度\s*[：:]\s*(.+)", text)
        if m and re.search(r"\d", m.group(1)):
            rep.add("coverage", "覆盖度声明", "warn",
                    [f"只在书目信息里写了「{m.group(1).strip()[:40]}」，正文没有覆盖度专节"],
                    hint="正文补一节说明精读了哪些部分、略过了哪些部分，"
                         "以及因此造成的信息缺口")
        else:
            rep.add("coverage", "覆盖度声明", "fail",
                    ["没有声明阅读覆盖度"],
                    hint="必须如实写出读了多少、略过多少——这是判断结论可信度的前提")
    return pages


def check_quotes(text: str, secs: dict, source: Source | None, rep: Report):
    """只核对"声称出自原书正文"的引文。

    三类引文不属于此列，必须排除，否则会把正确写法误判成编造：
      - 元数据引用（书名/作者/出版信息），按约定用 (元数据) 标注
      - 【联网补充】章节里引用的网页内容，本来就不是原书的话
      - 书目信息块内部的字段
    """
    lines = text.splitlines()
    hspan = header_span(lines)
    web_span = secs.get(WEB_SECTION)
    found: list[tuple[str, int]] = []
    skipped = 0
    for i, line in enumerate(lines, start=1):
        idx = i - 1
        if hspan and hspan[0] <= idx < hspan[1]:
            continue
        if web_span and web_span[0] <= idx < web_span[1]:
            skipped += 1
            continue
        if "元数据" in line:
            skipped += 1
            continue
        for pat in QUOTE_PATTERNS:
            for m in pat.finditer(line):
                found.append((m.group(1).strip(), i))
    note = f"（另跳过 {skipped} 行元数据/联网引用，不按原书正文核对）" if skipped else ""
    if not found:
        rep.add("quotes", "引文逐字核对", "warn",
                ["全文没有找到任何引文（「」 或 中文弯引号）" + note],
                hint="原文摘录章节应给出可核对的逐字引文，否则无法验证内容真实性")
        return

    if source is None:
        rep.add("quotes", "引文逐字核对", "warn",
                [f"发现 {len(found)} 段引文，但此书没有可提取文字（扫描版），"
                 "脚本无法机械核对" + note],
                hint="扫描版请确保每段引文都带 (p.N·图)，方便人工回原书抽查")
        return

    bad: list[str] = []
    wrong_page: list[str] = []
    no_anchor: list[str] = []
    ok_pages: list[int] = []
    for quote, line_no in found:
        frags = [f for f in ELLIPSIS_RE.split(quote) if len(normalize_for_match(f)) >= 3]
        if not frags:
            continue
        # 省略号节选必须**逐段**都核对：只查第一段的话，
        # 「真实句子……编造的后半句」会整段通过（这是防编造闸门最要命的一个洞）。
        located: list[int] = []
        miss: list[str] = []
        for fi, frag in enumerate(frags, start=1):
            hit = source.locate(frag)
            if hit is None:
                tag = frag if len(frags) == 1 else f"第 {fi}/{len(frags)} 段「{frag[:24]}…」"
                miss.append(tag)
            else:
                located.append(hit)
        if miss:
            bad.append(f"第 {line_no} 行：「{quote[:36]}…」在原文中找不到（{('；'.join(miss))[:60]}）")
            continue
        ok_pages.extend(located)

        # 引文出现在哪一页，必须与它标注的 (p.N) 对得上。
        # 只校验"这句话存在"是不够的：真实存在于 p.5 的句子标成 (p.3) 同样是错的。
        line = lines[line_no - 1] if 0 < line_no <= len(lines) else ""
        tail = line.split(quote, 1)[-1] if quote in line else line
        start_pg = _cited_page_in(tail)
        end_pg = start_pg
        if start_pg is not None:
            rng = re.search(
                rf"p\.\s*{start_pg}\s*[-–~]\s*(\d+)",
                tail, re.IGNORECASE)
            if rng:
                end_pg = int(rng.group(1))
        if start_pg is None:
            no_anchor.append(f"第 {line_no} 行：「{quote[:28]}…」")
        elif not any(_page_in_range(p, start_pg, end_pg) for p in located):
            want = f"p.{start_pg}" if start_pg == end_pg else f"p.{start_pg}-{end_pg}"
            got = "、".join(f"p.{p}" for p in sorted(set(located))[:6])
            wrong_page.append(
                f"第 {line_no} 行：标注 {want}，但这段引文实际出现在 {got}")

    details = [f"{len(found)} 段引文全部能在原文中定位（涉及 {len(set(ok_pages))} 页）" + note]
    if bad:
        rep.add("quotes", "引文逐字核对", "fail",
                bad, hint="这些引文在原书里不存在——要么改回原文，要么删掉。"
                          "注意 OCR/提取可能造成个别字差异，请回原文逐字校对")
    if wrong_page:
        rep.add("quotes", "引文页锚", "fail", wrong_page,
                hint="引文是真的，但标错了页。请按实际出现页改正 (p.N)——"
                     "页锚错了，抽查的人会翻到那一页却看不到这句话")
    if no_anchor:
        rep.add("quotes", "引文页锚", "warn",
                no_anchor[:20] + ([f"（还有 {len(no_anchor) - 20} 条）"] if len(no_anchor) > 20 else []),
                hint="引文后面补上 (p.N)，否则无法判断这段话是否真的出自标注位置")
    if not bad and not wrong_page:
        rep.add("quotes", "引文逐字核对", "ok", details)


def check_web(text: str, secs: dict, rep: Report):
    urls = URL_RE.findall(text)
    if WEB_SECTION not in secs:
        if urls:
            rep.add("web", "联网内容标注", "fail",
                    [f"正文出现 {len(urls)} 个网址，但没有【{WEB_SECTION}】章节"],
                    hint="联网内容必须集中在【联网补充】章节里，并标注来源与可信度")
        else:
            rep.add("web", "联网内容标注", "ok", ["未使用联网内容"])
        return

    s, e = secs[WEB_SECTION]
    lines = text.splitlines()
    web_body = "\n".join(lines[s:e])
    outside: list[str] = []
    for i, line in enumerate(lines, start=1):
        if s <= i - 1 < e:
            continue
        if URL_RE.search(line):
            outside.append(f"第 {i} 行：{line.strip()[:60]}")
    if outside:
        rep.add("web", "联网内容标注", "fail",
                ["【联网补充】章节之外出现网址："] + outside,
                hint="联网内容泄漏到正文里了，会被当成原书内容，必须挪进【联网补充】")
        return

    web_urls = URL_RE.findall(web_body)
    # "本次没用联网"是合法写法，不该被判违规——只要正文里确实也没有网址
    if not web_urls and re.search(r"未使用联网|未联网|无联网|没有使用联网|未检索", web_body):
        rep.add("web", "联网内容标注", "ok",
                ["已声明未使用联网内容，且正文无网址泄漏"])
        return

    problems = []
    if not web_urls:
        problems.append("【联网补充】章节里没有任何网址"
                        "（若确实没用联网，请写明「本总结未使用联网检索内容」）")
    body_flat = re.sub(r"\s", "", web_body)
    if "检索日期" not in body_flat and not re.search(r"20\d{2}[-/年]\d{1,2}", web_body):
        problems.append("没有标注检索日期")
    if not re.search(r"可信度|等级|权威|一手|二手|A级|B级|C级", web_body):
        problems.append("没有标注可信度等级")
    if problems:
        rep.add("web", "联网内容标注", "fail",
                [f"【{WEB_SECTION}】章节不合规："] + problems,
                hint="每条联网信息都要写清：结论 + 网址 + 检索日期 + 可信度等级")
    else:
        rep.add("web", "联网内容标注", "ok",
                [f"【{WEB_SECTION}】章节合规（{len(web_urls)} 个来源，"
                 "来源与正文已隔离）"])


def check_unanchored(text: str, secs: dict, rep: Report):
    """挑出"带硬信息却没有页锚"的段落。只提醒，不拦截。

    按**段落**而不是按行判断：文档是手写的，一句话常被折成两三行，页锚多半落在段末，
    按行判会把大量合规句子误报成无锚，反而淹没真正可疑的句子。
    页眉字段、联网补充节、覆盖度节本就不需要页锚，一并排除。
    """
    lines = text.splitlines()
    hspan = header_span(lines)
    spans = [secs.get(WEB_SECTION), secs.get("覆盖度"), hspan]

    def excluded(idx: int) -> bool:
        return any(s and s[0] <= idx < s[1] for s in spans)

    suspects = []
    para: list[str] = []
    para_start = 0

    def flush():
        if para:
            body = " ".join(para)
            if not ANCHOR_RE.search(body) and (
                    HARD_FACT_RE.search(body)
                    or (any(w in body for w in SCALE_WORDS) and re.search(r"\d", body))):
                suspects.append(f"第 {para_start} 行：{body.strip()[:70]}")
        para.clear()

    for i, line in enumerate(lines):
        s = line.strip()
        if not s or excluded(i) or s.startswith((">", "#")):
            flush()
            continue
        if not para:
            para_start = i + 1
        para.append(s)
    flush()
    if suspects:
        rep.add("unanchored", "疑似无锚断言", "warn",
                suspects, hint="这些句子含具体数字/年份却没有页锚。逐个回原书确认，"
                              "确认不了就删掉或改成不带数字的表述")
    else:
        rep.add("unanchored", "疑似无锚断言", "ok", ["未发现游离的硬信息"])


def load_source(args) -> Source | None:
    if args.source:
        p = Path(norm_path(args.source))
        if p.exists():
            return Source(p.read_text(encoding="utf-8", errors="replace"))
        print(f"⚠ 找不到页锚全文 {p}", file=sys.stderr)
    return None


def resolve_page_count(args, source: Source | None) -> int | None:
    if args.pages:
        return args.pages
    if source is not None and source.page_count:
        return source.page_count
    if args.manifest:
        p = Path(norm_path(args.manifest))
        if p.exists():
            try:
                return int(json.loads(p.read_text(encoding="utf-8"))["pages"])
            except Exception:
                pass
    manifest = Path(norm_path(args.work or ".bookwork")) / "_manifest.json"
    if manifest.exists():
        try:
            return int(json.loads(manifest.read_text(encoding="utf-8"))["pages"])
        except Exception:
            pass
    return None


def main(argv=None):
    ap = argparse.ArgumentParser(prog="verify.py", description="阅读总结防编造校验")
    ap.add_argument("--summary", required=True, help="生成的阅读总结 txt")
    ap.add_argument("--source", help="带页锚的全文（booktool probe 产出）")
    ap.add_argument("--manifest", help="booktool 的 _manifest.json")
    ap.add_argument("--work", help="booktool 工作目录（默认 .bookwork）")
    ap.add_argument("--pages", type=int, help="总页数（缺省则从 source/manifest 推断）")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="把警告也当作不通过")
    args = ap.parse_args(argv)

    summary_path = Path(norm_path(args.summary))
    if not summary_path.exists():
        print(f"错误: 找不到总结文档 {summary_path}", file=sys.stderr)
        return 2
    text = summary_path.read_text(encoding="utf-8", errors="replace")
    source = load_source(args)
    page_count = resolve_page_count(args, source)

    rep = Report()
    secs = check_structure(text, rep)
    check_anchors(text, secs, page_count, rep)
    check_quotes(text, secs, source, rep)
    check_web(text, secs, rep)
    check_unanchored(text, secs, rep)

    if args.json:
        print(json.dumps({
            "summary": str(summary_path),
            "page_count": page_count,
            "source": str(args.source) if args.source else None,
            "has_text_source": source is not None,
            "failures": len(rep.failures),
            "warnings": len(rep.warnings),
            "checks": rep.checks,
        }, ensure_ascii=False, indent=2))
    else:
        print(f"校验对象: {summary_path.name}")
        print(f"总页数  : {page_count if page_count else '未提供（页锚范围检查已跳过）'}")
        print(f"原文比对: {'有，可机械核对引文' if source else '无（扫描版或未提供）'}")
        print()
        print(rep.render())

    if rep.failures:
        return 1
    if args.strict and rep.warnings:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
