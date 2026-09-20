#!/usr/bin/env python3
"""booktool.py — 图书探测 / 文本提取 / 目录大纲 / 页锚检索 / 页面渲染。

配合 skill `book-kb-builder` 使用。

设计意图：把"读懂一本电子书"里所有机械、易错、重复的步骤从模型的工作流里
移出来——格式判定、文字层质量探测、目录提取、翻页渲染、按页检索——让模型
只专注于它真正擅长的事：阅读、判断与写作。

核心约定：所有输出都带**页锚**。
  - 文本：每页以 [[p.N]] 开头（N = PDF 物理页序号，从 1 开始）
  - 图片：文件名为 pNNNN.png（对应 PDF 物理页序号）
这样总结文档里的每一条结论都能回到原书的具体页面，而不是"大概是书里说的"。

两条提取路线各有盲区，本工具会自动比较后择优：
  - pdftotext：极快，但对缺 ToUnicode 映射的中文字体常输出乱码
  - PyMuPDF ：对中日韩 CID 字体更稳，还能渲染页面、读书签大纲
因此 probe 会同时报告用了哪种方法、以及另一种方法的结果有多差。

用法速查
    python booktool.py probe   <book> [--work DIR] [--json]
    python booktool.py outline <book> [--chapters] [--max-level 2]
    python booktool.py text    <book> [--pages 5-12] [--force]
    python booktool.py search  <book> --pattern 关键词 [--context 3] [--max-hits 20]
    python booktool.py render  <book> --pages 5,9-12 [--dpi 150] [--offset 13] [--out DIR]
    python booktool.py contact <book> --pages 9-120 [--per-sheet 20] [--out DIR]
"""
from __future__ import annotations

import argparse
import html.parser
import json
import math
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import zipfile
from pathlib import Path

# Windows 控制台默认可能是 GBK，中文输出会炸；统一强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PAGE_MARK = "[[p.{}]]"
DEFAULT_WORK = ".bookwork"
DEFAULT_DPI = 150
DEFAULT_MAX_WIDTH = 1500

# 判断"抽出的是不是人话"用的高频词。乱码文本几乎不会命中这些词。
ZH_STOPWORDS = "的了是在和与不为有这我你他她它们个中上下"
EN_STOPWORDS = ("the", "and", "of", "to", "in", "is", "are", "that", "for", "with")


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def norm_path(p: str) -> str:
    """把 Git-Bash 风格的 /c/Users/x 转成 Windows 能认的 C:/Users/x。

    模型经常从 shell 拿到 msys 路径再交给 Windows Python，不做这一步会得到
    "文件不存在"这种极具误导性的报错。
    """
    p = str(p).strip().strip('"').strip("'")
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    if m:
        return f"{m.group(1).upper()}:/{m.group(2)}"
    if p.startswith("~"):
        return os.path.expanduser(p)
    return p


def slugify(name: str, maxlen: int = 40) -> str:
    name = unicodedata.normalize("NFKC", str(name))
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name)
    name = re.sub(r"\s+", "", name).strip("._")
    return name[:maxlen] or "book"


def compact(s: str) -> str:
    """去掉所有空白。中文提取结果里常被插入多余空格，检索/比对前先压平。"""
    return re.sub(r"\s+", "", s)


def die(msg: str, code: int = 2):
    print(f"错误: {msg}", file=sys.stderr)
    sys.exit(code)


def parse_pages(spec: str | None, page_count: int, offset: int = 0) -> list[int]:
    """解析 '5,9-12' 这类页范围，返回 1-based PDF 物理页号。

    offset: 书上印的页码 + offset = PDF 物理页号（扫描版页码校准用）。
    """
    if not spec:
        return list(range(1, min(page_count, 12) + 1))
    out: list[int] = []
    for part in str(spec).split(","):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"^(\d+)\s*[-–~]\s*(\d+)$", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            rng = range(min(a, b), max(a, b) + 1)
        elif part.isdigit():
            rng = [int(part)]
        else:
            die(f"看不懂的页范围: {part!r}（形如 5 或 9-12）")
            return []
        for n in rng:
            pdf_page = n + offset
            if 1 <= pdf_page <= page_count:
                out.append(pdf_page)
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def score_text(text: str) -> dict:
    """给一段提取结果打分，用于在多个提取器之间择优，以及识别乱码。

    关键指标是"人话命中数"：真实中文文本必然包含大量高频虚词，而乱码
    （CID 未映射被当拉丁码位输出）虽然也是合法字符，却几乎不命中任何一个。
    """
    if not text:
        return {"chars": 0, "cjk": 0, "latin_words": 0, "stopword_hits": 0,
                "weird": 0, "score": 0.0, "cjk_ratio": 0.0}
    total = len(text)
    cjk = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
    latin_words = len(re.findall(r"[A-Za-z]{3,}", text))
    lower = text.lower()
    zh_hits = sum(text.count(w) for w in ZH_STOPWORDS)
    en_hits = sum(lower.count(" " + w + " ") for w in EN_STOPWORDS)
    # 乱码文本会充斥这类符号/高位拉丁字符
    weird = sum(1 for c in text
                if (0x80 <= ord(c) <= 0x2FF) or c in "{}<>|~^`\\")
    stopword_hits = zh_hits + en_hits
    cjk_ratio = cjk / total
    score = cjk * 2.0 + zh_hits * 8.0 + latin_words * 1.5 + en_hits * 6.0 - weird * 1.5
    return {"chars": total, "cjk": cjk, "latin_words": latin_words,
            "stopword_hits": stopword_hits, "weird": weird,
            "score": round(score, 1), "cjk_ratio": round(cjk_ratio, 4)}


# --------------------------------------------------------------------------
# 书籍对象：PDF / EPUB / 纯文本
# --------------------------------------------------------------------------
class BaseBook:
    fmt = "?"
    page_count = 0
    metadata: dict = {}
    extract_method = ""

    def __init__(self, path: str):
        self.path = path
        self.name = Path(path).name
        self.stem = Path(path).stem

    def toc(self) -> list[tuple[int, str, int]]:
        return []

    def page_texts(self) -> list[str]:
        """返回 1-based 的每页文本（index 0 是占位空串）。"""
        raise NotImplementedError

    @staticmethod
    def assess_text(page_texts: list[str], score: dict | None = None) -> dict:
        """判定文字层可用性，决定走文本路线还是视觉路线。

        text-rich    → 可 grep、可机械校验引文，走文本路线
        text-partial → 混合，正文靠读文本、图版靠视觉
        text-garbled → 有字符但不成话（字体缺映射），等于没有
        scanned      → 没有文字层，只能视觉阅读
        """
        pages = page_texts[1:]
        body = "\n".join(pages)
        n = max(len(pages), 1)
        sc = score or score_text(body)
        with_text = sum(1 for p in pages if len(p.strip()) > 50)
        ratio_with_text = with_text / n
        per_page = sc["chars"] / n

        # 判定基准是**每页**密度而非总量：扫描件全书的产出约等于 0 字/页，
        # 而一本很薄的正经小册子总量虽小、每页却是满的。用总量会把后者误杀。
        if per_page < 15:
            verdict = "scanned"
        elif sc["chars"] < 100:
            verdict = "text-partial"  # 样本太小，不做乱码判定，宁可放过
        else:
            # 有字符却不含高频虚词 → 字体缺 ToUnicode 映射，抽出的是乱码
            hits_per_1k = sc["stopword_hits"] / (sc["chars"] / 1000.0)
            if hits_per_1k < 1.0:
                verdict = "text-garbled"
            elif ratio_with_text < 0.70 or per_page < 300:
                verdict = "text-partial"
            else:
                verdict = "text-rich"

        return {
            "verdict": verdict,
            "chars": sc["chars"],
            "cjk_chars": sc["cjk"],
            "cjk_ratio": sc["cjk_ratio"],
            "latin_words": sc["latin_words"],
            "stopword_hits": sc["stopword_hits"],
            "weird_chars": sc["weird"],
            "per_page_chars": round(per_page, 1),
            "pages_with_text": with_text,
            "quality_score": sc["score"],
        }


class PdfBook(BaseBook):
    fmt = "pdf"

    def __init__(self, path: str):
        super().__init__(path)
        self._doc = None
        self._pages: list[str] | None = None
        self._candidates: dict[str, dict] = {}

    @property
    def doc(self):
        if self._doc is not None:
            return self._doc
        try:
            import pymupdf
        except Exception:
            return None
        self._doc = pymupdf.open(self.path)
        return self._doc

    @property
    def page_count(self) -> int:
        if self.doc is not None:
            return self.doc.page_count
        raw = self._raw_pdftotext()
        return max(len(raw.split("\f")) - 1, 0)

    @property
    def metadata(self) -> dict:
        if self.doc is None:
            return {}
        md = self.doc.metadata or {}
        return {k: v for k, v in md.items()
                if v and k in ("title", "author", "subject", "keywords", "creationDate")}

    def toc(self):
        if self.doc is None:
            return []
        return [tuple(e) for e in self.doc.get_toc(simple=True)]

    # ---- 两个提取器 ------------------------------------------------------
    def _raw_pdftotext(self) -> str:
        exe = find_pdftotext()
        if not exe:
            return ""
        r = subprocess.run([exe, "-layout", "-enc", "UTF-8", self.path, "-"],
                           capture_output=True)
        if r.returncode not in (0, 99):  # xpdf 用 99 表示"有警告但成功"
            return ""
        return r.stdout.decode("utf-8", "replace")

    def _pages_pdftotext(self) -> list[str]:
        parts = self._raw_pdftotext().split("\f")
        if parts and not parts[-1].strip():
            parts = parts[:-1]  # 每页后都有一个 \f，末段是空串
        return [""] + parts

    def _pages_pymupdf(self) -> list[str]:
        if self.doc is None:
            return [""]
        return [""] + [p.get_text() for p in self.doc]

    def choose_extraction(self, force: str | None = None) -> tuple[list[str], str]:
        """比较两条提取路线的产出，取更像人话的那份。"""
        if self._pages is not None and not force:
            return self._pages, self.extract_method

        order = ["pymupdf", "pdftotext"]
        if force:
            order = [force]
        best_pages, best_method, best_score = [""], "", None
        for method in order:
            try:
                pages = self._pages_pymupdf() if method == "pymupdf" else self._pages_pdftotext()
            except Exception as exc:
                self._candidates[method] = {"error": str(exc)[:200]}
                continue
            sc = score_text("\n".join(pages[1:]))
            self._candidates[method] = sc
            if best_score is None or sc["score"] > best_score["score"]:
                best_pages, best_method, best_score = pages, method, sc
            # 已经足够好就不必再试另一种
            if not force and sc["stopword_hits"] > 0 and sc["chars"] > 2000:
                break
        self._pages, self.extract_method = best_pages, best_method
        return best_pages, best_method

    def page_texts(self) -> list[str]:
        return self.choose_extraction()[0]

    def render(self, page_no: int, dpi: int = DEFAULT_DPI, max_width: int = DEFAULT_MAX_WIDTH):
        """渲染一页为 pixmap；宽度超限时自动降 DPI，避免产出巨图。"""
        if self.doc is None:
            die("渲染页面需要 PyMuPDF。安装："
                f"\"{sys.executable}\" -m pip install pymupdf")
        page = self.doc[page_no - 1]
        effective = dpi
        if page.rect.width > 0:
            projected = page.rect.width / 72.0 * dpi
            if projected > max_width:
                effective = max(72, int(dpi * max_width / projected))
        return page.get_pixmap(dpi=effective), effective

    @property
    def candidates(self) -> dict:
        return self._candidates


class EpubBook(BaseBook):
    """EPUB = zip + OPF + XHTML，标准库即可解析，无需第三方依赖。

    EPUB 没有固定页码，页锚退化为"章节锚" [[p.N]]（N = 第 N 个 spine 文档）。
    """
    fmt = "epub"
    extract_method = "stdlib"

    def __init__(self, path: str):
        super().__init__(path)
        self._sections: list[tuple[str, str]] | None = None
        self._meta: dict = {}
        self._drm = False

    def _load(self):
        if self._sections is not None:
            return
        if not zipfile.is_zipfile(self.path):
            die(f"{self.name} 不是合法的 EPUB（zip 容器损坏）")
        sections: list[tuple[str, str]] = []
        with zipfile.ZipFile(self.path) as z:
            names = z.namelist()
            self._drm = any(n.startswith("META-INF/encryption.xml") for n in names)
            opf_name = None
            try:
                container = z.read("META-INF/container.xml").decode("utf-8", "replace")
                m = re.search(r'full-path="([^"]+)"', container)
                if m:
                    opf_name = m.group(1)
            except KeyError:
                pass
            if not opf_name:
                opf_name = next((n for n in names if n.endswith(".opf")), None)
            if not opf_name:
                die("EPUB 里找不到 OPF 清单文件")

            opf = z.read(opf_name).decode("utf-8", "replace")
            base = os.path.dirname(opf_name)

            def pick(tag):
                m = re.search(rf"<dc:{tag}[^>]*>(.*?)</dc:{tag}>", opf, re.S | re.I)
                return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""

            self._meta = {"title": pick("title"), "author": pick("creator")}

            manifest = {}
            for m in re.finditer(r"<item\b[^>]*>", opf, re.I):
                tag = m.group(0)
                idm = re.search(r'id="([^"]+)"', tag)
                href = re.search(r'href="([^"]+)"', tag)
                if idm and href:
                    manifest[idm.group(1)] = href.group(1)

            order = re.findall(r'<itemref[^>]*idref="([^"]+)"', opf, re.I) or list(manifest)
            stripper = _HtmlToText()
            for sid in order:
                href = manifest.get(sid)
                if not href:
                    continue
                full = (f"{base}/{href}" if base else href).replace("\\", "/")
                if full.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".svg", ".css")):
                    continue
                try:
                    data = z.read(full)
                except KeyError:
                    continue
                stripper.reset()
                stripper.feed(data.decode("utf-8", "replace"))
                text = stripper.text()
                if text.strip():
                    sections.append((full, text))
        self._sections = sections

    @property
    def page_count(self) -> int:
        self._load()
        return len(self._sections or [])

    @property
    def metadata(self) -> dict:
        self._load()
        return self._meta

    def toc(self):
        self._load()
        out = []
        for i, (href, text) in enumerate(self._sections or [], start=1):
            first = next((ln.strip() for ln in text.splitlines() if ln.strip()), href)
            out.append((1, first[:80], i))
        return out

    def page_texts(self) -> list[str]:
        self._load()
        return [""] + [t for _, t in (self._sections or [])]


class TxtBook(BaseBook):
    """纯文本 / Markdown：按固定行数切块，行号即锚。"""
    fmt = "text"
    extract_method = "read"
    LINES_PER_BLOCK = 60

    def __init__(self, path: str):
        super().__init__(path)
        self._lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()

    @property
    def page_count(self) -> int:
        return max(1, math.ceil(len(self._lines) / self.LINES_PER_BLOCK))

    def page_texts(self) -> list[str]:
        n = self.LINES_PER_BLOCK
        return [""] + ["\n".join(self._lines[i:i + n])
                       for i in range(0, len(self._lines), n)]


class _HtmlToText(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}
    SKIP = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip = 0

    def reset(self):
        super().reset()
        self._out = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self._out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BLOCK:
            self._out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self._out.append(data)

    def text(self) -> str:
        joined = "".join(self._out)
        joined = re.sub(r"[ \t\u00a0]+", " ", joined)
        joined = re.sub(r"\n\s*\n\s*\n+", "\n\n", joined)
        return joined.strip()


# --------------------------------------------------------------------------
def find_pdftotext() -> str | None:
    exe = shutil.which("pdftotext")
    if exe:
        return exe
    for cand in (
        r"C:\Program Files\Git\mingw64\bin\pdftotext.exe",
        r"C:\Program Files\Git\usr\bin\pdftotext.exe",
        r"C:\Program Files\poppler\Library\bin\pdftotext.exe",
        "/usr/bin/pdftotext",
        "/usr/local/bin/pdftotext",
    ):
        if os.path.exists(cand):
            return cand
    return None


def have_pymupdf() -> bool:
    try:
        import pymupdf  # noqa
        return True
    except Exception:
        return False


def detect_format(path: str) -> str:
    ext = Path(path).suffix.lower()
    try:
        with open(path, "rb") as fh:
            head = fh.read(8)
    except OSError as exc:
        die(f"读不了这个文件：{exc}")
        return ""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        if ext == ".epub":
            return "epub"
        try:
            if zipfile.is_zipfile(path):
                with zipfile.ZipFile(path) as z:
                    if "META-INF/container.xml" in z.namelist():
                        return "epub"
        except Exception:
            pass
        return "zip"
    if ext in (".txt", ".md", ".markdown", ".rst"):
        return "text"
    return ext.lstrip(".") or "unknown"


def open_book(path: str) -> BaseBook:
    path = norm_path(path)
    if not os.path.exists(path):
        die(f"找不到文件: {path}")
    fmt = detect_format(path)
    if fmt == "pdf":
        return PdfBook(path)
    if fmt == "epub":
        return EpubBook(path)
    if fmt == "text":
        return TxtBook(path)
    if fmt in ("mobi", "azw3", "djvu", "chm"):
        die(f"{fmt} 需要先转换格式：用 Calibre 转成 epub 或 pdf 再入库（"
            f"例如 ebook-convert \"{path}\" out.epub）")
    die(f"暂不支持的格式: .{fmt}（目前支持 pdf / epub / txt / md）")
    raise SystemExit(2)


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------
def cmd_probe(args):
    book = open_book(args.book)
    if isinstance(book, PdfBook):
        pages, method = book.choose_extraction(force=args.method)
    else:
        pages, method = book.page_texts(), book.extract_method
    quality = BaseBook.assess_text(pages)

    info = {
        "file": book.path,
        "name": book.name,
        "format": book.fmt,
        "size_mb": round(os.path.getsize(book.path) / 1048576, 1),
        "pages": book.page_count,
        "page_unit": "页" if book.fmt != "epub" else "节",
        "metadata": book.metadata,
        "outline_entries": len(book.toc()),
        "extract_method": method,
        "text": quality,
        "tools": {"pdftotext": bool(find_pdftotext()), "pymupdf": have_pymupdf()},
    }
    if isinstance(book, PdfBook) and book.candidates:
        info["extractor_comparison"] = book.candidates
    if isinstance(book, EpubBook) and book._drm:
        info["drm"] = True

    wd = Path(norm_path(args.work or DEFAULT_WORK))
    wd.mkdir(parents=True, exist_ok=True)
    extract_path = None
    if quality["verdict"] in ("text-rich", "text-partial"):
        extract_path = wd / f"{slugify(book.stem)}.pages.txt"
        with open(extract_path, "w", encoding="utf-8", newline="\n") as fh:
            for i, text in enumerate(pages[1:], start=1):
                fh.write(PAGE_MARK.format(i) + "\n" + text.rstrip() + "\n\n")
        info["extract_file"] = str(extract_path)
    else:
        info["extract_file"] = None

    # 明确下一步该走哪条路，避免模型在扫描版上反复徒劳地抽文本
    advice = []
    if quality["verdict"] == "scanned":
        advice.append("无可用文字层 → 走视觉阅读路线：outline 摸结构，render 出"
                      "目录/前言/各章开篇/结语的页面图，用视觉阅读。")
        if book.fmt == "pdf" and not have_pymupdf():
            advice.append("渲染页面需要 PyMuPDF，且要征得用户同意后才能安装："
                          f"\"{sys.executable}\" -m pip install pymupdf")
    elif quality["verdict"] == "text-garbled":
        advice.append("文字层严重乱码（字体缺 ToUnicode 映射）→ 按无文字层处理，"
                      "走视觉阅读路线。")
    else:
        advice.append("文字层可用 → 先用 text/search 读，图版与版式页再用 render "
                      "补充视觉判读。")
    if info["outline_entries"]:
        advice.append(f"PDF 自带 {info['outline_entries']} 条书签大纲："
                      "outline --chapters 可直接拿到章节页范围。")
    else:
        advice.append("没有书签大纲：需 render 目录页，用视觉阅读确定章节页范围。")
    if book.fmt == "pdf":
        advice.append("记得做页码校准：渲染一页正文，看印刷页码与 PDF 页号差多少，"
                      "后续 render 用 --offset 换算。")
    info["advice"] = advice

    (wd / "_manifest.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    unit = info["page_unit"]
    print(f"文件      : {info['name']}")
    print(f"格式      : {info['format']}｜{info['pages']} {unit}｜{info['size_mb']} MB")
    if info["metadata"]:
        print(f"元数据    : {json.dumps(info['metadata'], ensure_ascii=False)}")
    print(f"文字层    : {quality['verdict']}（提取器 {method}）")
    print(f"            每{unit}均 {quality['per_page_chars']} 字｜有文字 "
          f"{quality['pages_with_text']}/{info['pages']} {unit}｜"
          f"中文占比 {quality['cjk_ratio']}｜虚词命中 {quality['stopword_hits']}")
    if isinstance(book, PdfBook) and book.candidates:
        for m, sc in book.candidates.items():
            if "error" in sc:
                print(f"            提取器 {m}: 失败 {sc['error']}")
            else:
                print(f"            提取器 {m}: 质量分 {sc['score']} "
                      f"(虚词 {sc['stopword_hits']}, 乱码符号 {sc['weird']})")
    print(f"书签大纲  : {info['outline_entries']} 条")
    if extract_path:
        print(f"页锚全文  : {extract_path}")
    print("建议      :")
    for a in advice:
        print(f"  - {a}")
    return 0


# 扫描仪/OCR 流水线生成的占位大纲名，如 cov001 / fow012 / !00001 / 000355
SCAN_ARTIFACT_RE = re.compile(r"^[!_]?[a-z]{0,4}\d{1,6}$", re.I)


def outline_is_degenerate(toc: list) -> tuple[bool, float]:
    """判断大纲是不是"有书签但没用"。

    扫描件常带一份由扫描流水线生成的逐页书签，名字形如 fow001 / 000123，
    页数对得上但完全不是章节名。把它当目录用会白白浪费一整轮阅读。
    """
    if not toc:
        return True, 0.0
    junk = 0
    for lvl, title, _pg in toc:
        t = str(title).strip()
        if SCAN_ARTIFACT_RE.match(t) and not re.search(r"[\u4e00-\u9fff]", t):
            junk += 1
    ratio = junk / len(toc)
    return ratio >= 0.6, ratio


def cmd_outline(args):
    book = open_book(args.book)
    toc = book.toc()
    if not toc:
        print("（这本书没有书签大纲。）")
        print("请 render 目录页后用视觉阅读取章节结构：扫描版印刷目录通常在前言之后，"
              "2-6 页左右；也可先用 contact 生成联系表快速定位。")
        return 1

    degenerate, ratio = outline_is_degenerate(toc)
    if degenerate:
        print(f"⚠ 这本书有 {len(toc)} 条书签，但其中 {ratio:.0%} 是扫描流水线生成的"
              "占位名（如 fow001 / 000123），**不能当目录用**。")
        print(f"样例：{'、'.join(str(e[1]).strip() for e in toc[:6])} … "
              f"{'、'.join(str(e[1]).strip() for e in toc[-3:])}")
        print("→ 改走视觉路线取结构：先 contact 生成联系表定位印刷目录页，"
              "再 render 高清读目录。")
        print(f"（仍要看这份占位大纲可加 --force）")
        if not args.force:
            return 1

    entries = [(lvl, title.strip(), pg) for lvl, title, pg in toc if lvl <= args.max_level]
    if len(entries) > args.max_entries:
        print(f"⚠ 大纲条目过多（{len(entries)} 条），只显示前 {args.max_entries} 条；"
              "这通常说明它不是真正的章节目录。")
        entries = entries[:args.max_entries]
    if args.chapters:
        tops = [(t, p) for lvl, t, p in entries if lvl == 1]
        print(f"共 {book.page_count} {('页' if book.fmt != 'epub' else '节')}；"
              f"顶层章节 {len(tops)} 个")
        for i, (title, start) in enumerate(tops):
            end = tops[i + 1][1] - 1 if i + 1 < len(tops) else book.page_count
            print(f"  {start:>5}-{end:<5} ({end - start + 1:>4})  {title}")
        return 0
    for lvl, title, pg in entries:
        print(f"{'  ' * (lvl - 1)}{pg:>6}  {title}")
    return 0


def cmd_text(args):
    book = open_book(args.book)
    pages = book.page_texts()
    quality = BaseBook.assess_text(pages)
    if quality["verdict"] in ("scanned", "text-garbled") and not args.force:
        print(f"⚠ 文字层判定为 {quality['verdict']}，抽出来的文本是空的或乱码。")
        print("请改走视觉阅读：booktool.py outline <book> --chapters 定位章节，"
              "再 render 出对应页面图片来读。")
        print("（确实要强行抽取可加 --force）")
        return 1
    for n in parse_pages(args.pages, book.page_count):
        print(PAGE_MARK.format(n))
        print(pages[n].rstrip() if n < len(pages) else "")
        print()
    return 0


def cmd_search(args):
    book = open_book(args.book)
    pages = book.page_texts()
    quality = BaseBook.assess_text(pages)
    if quality["verdict"] in ("scanned", "text-garbled"):
        print(f"⚠ 文字层判定为 {quality['verdict']}，无法全文检索。")
        print("改走视觉路线：outline --chapters 定位章节 → render 该章首页与关键页 → "
              "用视觉阅读确认内容。")
        return 1

    try:
        pattern = re.compile(args.pattern, re.I if args.ignore_case else 0)
    except re.error as exc:
        die(f"正则不合法: {exc}")
        return 2
    needle = compact(args.pattern) if not args.regex else None

    hits = 0
    for n, text in enumerate(pages[1:], start=1):
        if not text:
            continue
        lines = text.splitlines()
        for i, line in enumerate(lines):
            matched = bool(pattern.search(line))
            if not matched and needle and needle in compact(line):
                matched = True  # 提取时被插入空格的情况
            if not matched:
                continue
            hits += 1
            lo, hi = max(0, i - args.context), min(len(lines), i + args.context + 1)
            print(PAGE_MARK.format(n))
            for j in range(lo, hi):
                print(f"{'>' if j == i else ' '} {lines[j]}")
            print()
            if hits >= args.max_hits:
                print(f"（已达 --max-hits {args.max_hits}，如需更多请调高上限）")
                return 0
            break  # 每页只报第一处，避免同页刷屏
    if not hits:
        print(f"未命中: {args.pattern!r}。可换同义词/近义词，或先用 outline 确认章节名。")
        return 1
    print(f"命中 {hits} 处")
    return 0


def cmd_render(args):
    book = open_book(args.book)
    pages = parse_pages(args.pages, book.page_count, offset=args.offset)
    if not pages:
        die("页范围为空。扫描版注意用 --offset 做页码校准。")
    out = Path(norm_path(args.out or str(Path(DEFAULT_WORK) / "pages")))
    out.mkdir(parents=True, exist_ok=True)
    dpi = args.dpi or DEFAULT_DPI
    for n in pages:
        pix, used = book.render(n, dpi=dpi, max_width=args.max_width)
        fn = out / f"p{n:04d}.png"
        pix.save(str(fn))
        note = "" if used == dpi else f"（受 --max-width 限制，实际 {used}dpi）"
        print(f"  {fn.name}  {pix.width}x{pix.height}  "
              f"{fn.stat().st_size / 1024:.0f}KB{note}")
    print(f"已渲染 {len(pages)} 页 → {out}")
    if args.offset:
        print(f"（页码校准 offset={args.offset}：书上第 X 页 = PDF 第 X+{args.offset} 页）")
    print("下一步：用 Read 工具逐张查看这些 PNG，只记录你真正看到的内容。")
    return 0


def cmd_contact(args):
    """把连续多页拼成缩略图联系表，快速扫读结构与图版分布。"""
    book = open_book(args.book)
    if getattr(book, "doc", None) is None:
        die("contact 需要 PyMuPDF。安装："
            f"\"{sys.executable}\" -m pip install pymupdf")
    pages = parse_pages(args.pages, book.page_count, offset=args.offset)
    if not pages:
        die("页范围为空")
    out = Path(norm_path(args.out or str(Path(DEFAULT_WORK) / "contact")))
    out.mkdir(parents=True, exist_ok=True)

    import pymupdf

    per, cols = args.per_sheet, args.cols
    rows = math.ceil(per / cols)
    thumb_w = args.thumb_width
    margin, gap, label_h = 12, 8, 14
    sheets = 0
    for start in range(0, len(pages), per):
        chunk = pages[start:start + per]
        pix0, _ = book.render(chunk[0], dpi=72, max_width=thumb_w)
        thumb_h = int(pix0.height * (thumb_w / pix0.width))
        cell_w, cell_h = thumb_w + gap, thumb_h + label_h + gap
        doc = pymupdf.open()
        sheet = doc.new_page(width=margin * 2 + cols * cell_w,
                             height=margin * 2 + rows * cell_h)
        for idx, pno in enumerate(chunk):
            r, c = divmod(idx, cols)
            x, y = margin + c * cell_w, margin + r * cell_h
            pix, _ = book.render(pno, dpi=72, max_width=thumb_w)
            sheet.insert_image(pymupdf.Rect(x, y, x + thumb_w, y + thumb_h), pixmap=pix)
            sheet.insert_text((x + 2, y + thumb_h + 11), f"PDF p{pno}",
                              fontsize=8, fontname="china-s")
        fn = out / f"sheet{sheets + 1:02d}_p{chunk[0]}-{chunk[-1]}.png"
        sheet.get_pixmap(dpi=args.sheet_dpi).save(str(fn))
        sheets += 1
        print(f"  {fn.name}  {fn.stat().st_size / 1024:.0f}KB")
        doc.close()
    print(f"已生成 {sheets} 张联系表 → {out}")
    print("联系表用于快速判断哪几页是目录、哪几页是图版、章节从哪页开始。"
          "字看不清是正常的——定位到目标页后再用 render 高清渲染。")
    return 0


# --------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(
        prog="booktool.py", description="图书探测 / 提取 / 大纲 / 检索 / 渲染（带页锚）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(func=fn)
        return p

    p = add("probe", cmd_probe, "探测格式/页数/文字层质量，导出页锚全文")
    p.add_argument("book")
    p.add_argument("--work", help=f"工作目录（默认 {DEFAULT_WORK}）")
    p.add_argument("--method", choices=["pymupdf", "pdftotext"],
                   help="强制指定提取器（默认自动择优）")
    p.add_argument("--json", action="store_true", help="输出 JSON")

    p = add("outline", cmd_outline, "读 PDF 书签大纲；--chapters 给章节页范围")
    p.add_argument("book")
    p.add_argument("--chapters", action="store_true", help="只列顶层章节 + 页范围")
    p.add_argument("--max-level", type=int, default=3, help="最大层级（默认 3）")
    p.add_argument("--max-entries", type=int, default=120,
                   help="最多打印多少条（默认 120，防止异常大纲刷屏）")
    p.add_argument("--force", action="store_true", help="即使判定为占位大纲也照打")

    p = add("text", cmd_text, "按页打印页锚文本（默认前 12 页）")
    p.add_argument("book")
    p.add_argument("--pages", help="页范围，如 5-12 或 3,9,20")
    p.add_argument("--force", action="store_true", help="即使判定扫描版也强抽")

    p = add("search", cmd_search, "在页锚全文里检索，返回页号 + 上下文")
    p.add_argument("book")
    p.add_argument("--pattern", required=True, help="关键词")
    p.add_argument("--context", type=int, default=3, help="上下文字行数（默认 3）")
    p.add_argument("--max-hits", type=int, default=20)
    p.add_argument("--ignore-case", action="store_true")
    p.add_argument("--regex", action="store_true",
                   help="按正则解释 pattern（默认按纯文本，更稳）")

    p = add("render", cmd_render, "渲染指定页为 PNG（文件名即 PDF 页号）")
    p.add_argument("book")
    p.add_argument("--pages", help="页范围，如 5,9-12；默认前 12 页")
    p.add_argument("--dpi", type=int, help=f"分辨率（默认 {DEFAULT_DPI}）")
    p.add_argument("--max-width", type=int, default=DEFAULT_MAX_WIDTH,
                   help=f"图像最大宽度像素（默认 {DEFAULT_MAX_WIDTH}，超限自动降 DPI）")
    p.add_argument("--offset", type=int, default=0,
                   help="页码校准：书上印的页码 + offset = PDF 页号")
    p.add_argument("--out", help="输出目录（默认 .bookwork/pages）")

    p = add("contact", cmd_contact, "多页拼成缩略图联系表，快速扫结构/图版")
    p.add_argument("book")
    p.add_argument("--pages", help="页范围（建议先 outline --chapters 选范围）")
    p.add_argument("--per-sheet", type=int, default=20, help="每张表几页（默认 20）")
    p.add_argument("--cols", type=int, default=5, help="每行几列（默认 5）")
    p.add_argument("--thumb-width", type=int, default=230, help="单页缩略图宽度像素")
    p.add_argument("--sheet-dpi", type=int, default=110)
    p.add_argument("--offset", type=int, default=0)
    p.add_argument("--out", help="输出目录（默认 .bookwork/contact）")

    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
