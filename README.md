# book-kb-builder

> 把一本电子书读成一份**每条结论都能翻回原书页码**的总结，并按类别归档成可长期检索的分层知识库。
>
> A ZCode skill that reads uploaded books into a page-anchored, citation-verifiable knowledge base —
> with an anti-fabrication gate that mechanically checks every quotation against the source.

面向设计类图书（尤其是**扫描版**老书与图册）设计。

---

## 它解决什么问题

让模型"读一遍书然后写总结"，产出看着漂亮，但有三处会崩：

1. **会编造。** 模型会用训练时的常识悄悄替换原书观点——"这本书大概会讲包豪斯吧"，
   于是知识库里出现原书根本没写的内容，而且三个月后没人分得清哪句是谁说的。
2. **读不完。** 一本 800 页的书读不完，但模型不会说自己没读完，而是用目录标题
   假装知道章节内容。
3. **设计类老书抽不出字。** 扫描版、影印本、图册没有文字层，常规文本抽取拿到空文件。

这个 skill 的设计目标不是"写得更像书评"，而是**让可核对性可被机器检查**。

## 三个核心设计

### 1. 文字层探测决定路线（而不是无脑抽文本）

`booktool.py probe` 先判断这本书有没有可用文字层，并**自动在两个提取器之间择优**：

| 判定 | 做法 |
|---|---|
| `text-rich` | 文本路线：检索定位 + 精读，引文可被机器核对 |
| `text-partial` | 两条都用：正文读文本，图版页读图片 |
| `text-garbled` | 字体缺 ToUnicode 映射，抽出的是乱码 → 按扫描版处理 |
| `scanned` | 无文字层 → 视觉路线：渲染页面图片来读 |

之所以要"择优"：`pdftotext` 极快，但中文 CID 字体缺映射时会输出 `{,Nz Qh<` 这种
**看起来有内容、其实全是噪声**的乱码；PyMuPDF 对中日韩字体更稳。脚本按"高频虚词命中数"
打分，取更像人话的那份。

### 2. 三种出处标记 + 机器校验闸门

正文里的每一条实质结论都必须挂一种标记，三种标记**不许混用**：

| 标记 | 含义 |
|---|---|
| `(p.188)` | 来自原书 **PDF 第 188 页**的文字 |
| `(p.188·图)` | 来自 PDF 第 188 页**图像的视觉判读** |
| `【联网补充】` | 来自**联网检索**，不是原书内容；只允许出现在专门章节，且须带网址 + 检索日期 + 可信度分级 |

`verify.py` 是闸门，不是建议。它会：

- 检查必备章节是否齐全（缺章节通常意味着漏读）
- 检查每个页锚是否落在书的页数范围内
- **逐字核对引文**：把总结里的「引文」规范化后回原文搜索，找不到就判不通过
- 检查网址是否泄漏到正文里
- 检查覆盖度是否写清（读了多少、略过多少）
- 挑出"带具体数字却没有页锚"的段落

实测能抓出编造的引文、越界页锚、正文里混入的维基链接；同时正确放行真实引文。

### 3. 保留目录结构 + 分层索引（借鉴 deep-rag）

不做无脑切块。保留"类别/书名"的目录结构，并生成两级 `_索引.md` 当**知识地图**：
先读地图定位，再按需下钻，比盲目的相似度检索更准，也不会丢结构。

## 知识库结构

```
知识库/                                   ← 知识库根（默认 ./知识库）
├─ _索引.md                               ← 根索引：类别地图 + 每本书一句话
├─ <类别>/
│  ├─ _索引.md                            ← 类别索引
│  └─ <图书名称>/
│     ├─ <原电子版文件>                    ← 原件，原样保留
│     └─ <图书名称>_阅读总结.txt           ← 生成的总结（主要产物）
└─ _全文提取/<类别>/<图书名称>.pages.txt   ← 带 [[p.N]] 页锚的全文（有文字层时才有）
```

书目目录严格保持"类别 / 书名 /（电子版 + 总结）"三级结构；`_索引.md` 与
`_全文提取/` 是自动维护的辅助物。

## 安装

把本仓库放到 ZCode 的 skills 目录（`~/.agents/skills/book-kb-builder/`），
然后建一个自带虚拟环境：

```bash
python -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt   # Windows
# .venv/bin/python -m pip install -r requirements.txt         # macOS / Linux
```

其余依赖都是可选的：

| 依赖 | 用途 | 缺了会怎样 |
|---|---|---|
| **PyMuPDF** | 渲染页面、读 PDF 书签大纲 | **扫描版读不了**（文本路线仍可用） |
| `pdftotext` | 快速文本提取的备选提取器 | 少一个提取器，不影响主流程 |

脚本会自动把 Git-Bash 风格的 `/c/Users/x` 路径转成 Windows 路径，从 shell 里
直接传路径不用手工转换。

## 快速上手

```bash
PY=.venv/Scripts/python.exe      # 或 .venv/bin/python

# 1) 探测：这本书该走哪条路？
$PY scripts/booktool.py probe "我的书.pdf" --work .bookwork

# 2) 拿结构：有书签大纲就直接给出章节页范围
$PY scripts/booktool.py outline "我的书.pdf" --chapters

# 3a) 文本路线：检索定位 + 按页精读
$PY scripts/booktool.py search "我的书.pdf" --pattern 园林
$PY scripts/booktool.py text   "我的书.pdf" --pages 14-20

# 3b) 视觉路线（扫描版）：缩略图定位 → 高清渲染 → 用 Read 看图
$PY scripts/booktool.py contact "我的书.pdf" --pages 1-40 --per-sheet 20
$PY scripts/booktool.py render  "我的书.pdf" --pages 38,39 --dpi 170

# 4) 写总结（照 references/summary-template.md 的结构）

# 5) 校验闸门
$PY scripts/verify.py --summary "我的书_阅读总结.txt" --source .bookwork/我的书.pages.txt

# 6) 落库 + 刷新索引
$PY scripts/kbtool.py build --kb-root 知识库 \
    --category "建筑与环境设计" --title "我的书" \
    --book "我的书.pdf" --summary "我的书_阅读总结.txt"
```

完整工作流、阅读预算、分类规则、联网政策见
[`SKILL.md`](SKILL.md) 与 [`references/`](references/)。

## 可运行的例子

`examples/` 里有一个自包含的演示：生成一本带文字层的中文测试书，
用一份**故意编造了一句引文**的总结去喂 `verify.py`，看闸门把它抓出来。

```bash
$PY examples/make_demo_book.py --out .bookwork            # 生成 demo 书
$PY scripts/booktool.py probe .bookwork/grid-demo.pdf --work .bookwork
$PY scripts/verify.py --summary examples/summary-bad.txt \
    --source .bookwork/grid-demo.pages.txt
```

详见 [`examples/README.md`](examples/README.md)。

## 真实场景里的坑（本 skill 就是为这些写的）

- **扫描版是常态，不是例外。** 这个 skill 最初的四本设计/建筑类书**全是扫描件、
  零文字层**（`pdftotext` 抽出来是空的）。所以视觉阅读是主力路线。
- **书签大纲可能是假的。** 扫描流水线会产出一份逐页书签（`fow001`、`000123`），
  页数对得上但完全不是章节名。`outline` 会检测并拒绝这种退化大纲，
  改让你用缩略图联系表去定位印刷目录。
- **目录页码和 PDF 页号不是一回事。** 前置页常用罗马数字、与正文两套编号。
  页锚统一用 **PDF 页号**（无歧义、可跳转、可校验），印刷页码只在章节地图里附注。
- **阅读预算是真的会用完。** 用尽时正确做法是**降低覆盖度声明，而不是降低真实性**。
  一份老实标注"精读 8 页 / 全书 396 页"的总结有用；一份假装读全的总结有害。

## 参考

架构上参考了两个项目：

- [boluo2077/deep-rag](https://github.com/boluo2077/deep-rag) —— 保留目录结构、
  生成"知识地图"再按需下钻，而不是把文档切碎做相似度检索
- [ConardLi/rag-skill](https://github.com/ConardLi/rag-skill) —— 分层目录索引、
  渐进式检索（先 grep 定位再局部读）、处理文件前先读 references 的强制性检查清单

## 许可

本仓库尚未选择开源许可证（默认保留所有权利）。如需他人可自由使用，
建议补一个 [MIT](https://choosealicense.com/licenses/mit/) 或
[Apache-2.0](https://choosealicense.com/licenses/apache-2.0/)。
