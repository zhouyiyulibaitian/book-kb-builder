# 提取与阅读路线：文本路线 vs 视觉路线

处理一本书之前先跑 `probe`，它会告诉你这本书该走哪条路。**不要跳过这一步直接抽文本**——
扫描版抽出来的是空文件，你会白读一圈还以为书是空的。

```bash
python scripts/booktool.py probe "<书>" --work .bookwork
```

`probe` 会给出 `文字层: text-rich / text-partial / text-garbled / scanned` 和具体建议。
四个判定对应的做法：

| 判定 | 含义 | 怎么做 |
|---|---|---|
| `text-rich` | 文字层完好 | 走**文本路线**：`text`/`search` 读，引文可被 `verify.py` 机械核对 |
| `text-partial` | 部分页有文字（图版书、混合型） | **两条都用**：正文走文本路线，图版页走视觉路线 |
| `text-garbled` | 有字符但是乱码（字体缺 ToUnicode 映射） | 当扫描版处理，走**视觉路线** |
| `scanned` | 没有文字层（纯扫描图片） | 走**视觉路线**，全程靠看页面图 |

设计类书籍**大量是扫描版**（老书、图册、影印本），视觉路线不是罕见分支，是主力路线之一。

## 视觉路线怎么走

```bash
# 1) 先拿结构：很多扫描版仍带书签大纲，这是最省事的地图
python scripts/booktool.py outline "<书>" --chapters

# 2) 没有大纲就先看联系表，快速判断哪几页是目录、哪几页是图版
python scripts/booktool.py contact "<书>" --pages 1-60 --per-sheet 20

# 3) 校准页码：印刷页码和 PDF 页号通常差一个固定值
python scripts/booktool.py render "<书>" --pages 20 --out .bookwork/pages
#    → 用 Read 看这张图，记下页面底部印的页码，比如印刷 188 而 PDF 是 201
#    → 则 offset = 201 - 188 = 13

# 4) 之后带 offset 渲染：这样你按"书上看到的页码"就能定位到正确的页面
python scripts/booktool.py render "<书>" --pages 188-190 --offset 13 --out .bookwork/pages
```

**页锚一律写 PDF 页号**（`(p.201)` 指 PDF 第 201 页），这是全文唯一的锚定方式，
因为它无歧义、可在文件里直接跳转、也能被 `verify.py` 检查范围。
印刷页码另在「内容结构与章节地图」里附注即可，例如
`第一章（PDF p.40 / 印刷 p.1）`，方便拿实体书的人对照。

前置页用罗马数字时要特别小心：正文页码与罗马数字页码是两套编号，
偏移量通常不同（例如印刷 354 对应 PDF 393，而罗马数字 xiv 对应 PDF 39）。
校准只对**正文**做，前置页的锚直接用 PDF 页号。

渲染出来的 `pNNNN.png` 用 Read 工具逐张看。**只记录你真正看到的内容**：
看到了图表，就写图表的标题与内容；看不清，就标注「该页图像模糊，未能辨识」，
不要用常识补。看不清就提高分辨率重渲染：

```bash
python scripts/booktool.py render "<书>" --pages 188 --dpi 300 --max-width 2200
```

图表页、版式示例页往往需要更高 DPI；纯文字页 150 DPI 足够，图省 token。

### 页码校准为什么重要

扫描版的 PDF 页号包含封面、版权页、目录等前置页，而书里印的页码从正文重新起算，
两者差一个固定偏移。不做校准，你就无法从目录上的页码找到对应的 PDF 页，
只能靠翻缩略图猜——既慢又容易错位。**校准只做一次**，之后一直用。
`--offset` 的语义是：印刷页码 + offset = PDF 页号。

校准要做两次交叉验证：挑两页相隔较远的正文（比如印刷 p.1 和印刷 p.354），
分别渲染确认换算一致，再开始大规模渲染。只验一页有可能赶上装订错页。

## 文本路线怎么走

```bash
# probe 已把带页锚的全文写到 .bookwork/<书名>.pages.txt，格式为 [[p.N]] + 该页文本
python scripts/booktool.py text "<书>" --pages 14-20      # 按页看
python scripts/booktool.py search "<书>" --pattern 园林   # 关键词定位，返回页号+上下文
```

`search` 是走文本路线时最常用的命令：**先用它定位，再读命中附近**，
不要从头到尾通读提取文本（那样既慢又烧 token，而且读得并不比检索仔细）。
命中为空时换同义词、上位词、下位词再试，或先用 `outline` 确认章节名再按页读。

提取文本里的 `[[p.N]]` 是页锚，写总结时把它转成 `(p.N)`。

## 两个提取器的差别（别踩这个坑）

本工具会同时用两个提取器，然后**取更像人话的那份**：

- **PyMuPDF**：对中日韩 CID 字体更稳，还能渲染页面、读 PDF 书签大纲。**推荐首选。**
- **pdftotext**（xpdf/poppler）：极快，但中文字体缺 ToUnicode 映射时会输出乱码——
  看起来"有文字"，其实全是 `{,Nz Qh<` 这种噪声。

`probe` 会打印两者的质量分对比（`extractor_comparison`）。如果质量分都很低，
说明这本书真的没有可用文字层，别硬抽，转视觉路线。

乱码还有一个隐蔽后果：`verify.py` **无法**核对乱码文本里的引文，
所以你会在 `text-garbled` 的书上失去机械防编造的那道闸门，必须靠视觉阅读 + 页锚自觉。

## 其他格式

- **EPUB**：用标准库解包，按 spine 顺序切成"节"，页锚是 `[[p.N]]`（N = 第 N 节）。
  EPUB 没有固定页码，写页锚时用节号，并在总结里说明"epub 版无固定页码，锚为章节序"。
  若 EPUB 带 DRM，解出来会是空的，`probe` 会报 `drm`，此时请用户换无 DRM 的版本。
- **mobi / azw3 / djvu**：先转换。`ebook-convert "book.mobi" book.epub`（Calibre）。
- **txt / md**：按 60 行一块切，锚为块号。

## 渲染不出来怎么办

`render`/`contact` 依赖 PyMuPDF。如果缺失：

```bash
"<当前 python>" -m pip install pymupdf
```

**安装第三方包会改动用户的环境，先说明用途并征得同意再装。**
本书库 skill 自带独立虚拟环境（`.venv/`），优先用它，
这样不会污染用户的全局 Python：

```bash
.venv/Scripts/python.exe -m pip install pymupdf      # Windows
.venv/bin/python -m pip install pymupdf              # macOS / Linux
```

实在装不了、又必须读扫描版时，还有一条退路：用系统自带的截图/预览工具把页面导出成图片，
或用 `pdftoppm`/`pdftopng`（poppler 或 xpdf 的命令行工具）渲染，再交给 Read 看。
