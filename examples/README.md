# 可运行的例子

这个例子的目的很单一：**让你亲手看到 `verify.py` 把编造的引文抓出来**。

这个 skill 的核心主张是"总结里的每句引文都能回原文核对"。要验证这条主张，
就需要一本**内容已知**的书——所以 `make_demo_book.py` 用 PyMuPDF 造一本 5 页的
中文小书，内容写死在脚本里。

## 跑一遍

```bash
PY=.venv/Scripts/python.exe      # 或 .venv/bin/python

# 1) 造书（需要 PyMuPDF）
$PY examples/make_demo_book.py --out .bookwork

# 2) 探测：会看到两个提取器的质量分差距巨大
$PY scripts/booktool.py probe .bookwork/grid-demo.pdf --work .bookwork

# 3) 一份老实的总结 —— 应当全部通过
$PY scripts/verify.py --summary examples/summary-good.txt \
    --source .bookwork/grid-demo.pages.txt

# 4) 一份掺了假的总结 —— 应当被打回
$PY scripts/verify.py --summary examples/summary-bad.txt \
    --source .bookwork/grid-demo.pages.txt
```

## 第 2 步值得多看一眼

```
文字层    : text-partial（提取器 pymupdf）
            提取器 pymupdf: 质量分 609.0 (虚词 23, 乱码符号 1)
            提取器 pdftotext: 质量分 -90.0 (虚词 0, 乱码符号 73)
```

同一本书，`pdftotext` 抽出来几乎是垃圾。原因是 PyMuPDF 内置的 CJK 字体（`china-s`）
缺 ToUnicode 映射，`pdftotext` 只能把 CID 码位当拉丁字符吐出来，得到 `{,Nz Qh<`
这类**看起来有内容、其实全是噪声**的乱码。PyMuPDF 自己的提取器能正确还原。

这不是演示造出来的特例：真实中文扫描件里字体缺映射很常见，而且乱码比空文件更危险——
空文件你会立刻发现，乱码会让你以为"抽到了内容"，然后基于噪声写总结。
`booktool.py` 因此同时跑两个提取器，按**高频虚词命中数**打分择优。

## 第 4 步里埋了三个错误

`summary-bad.txt` 是**故意**写的反面样本，埋了三处问题，且都只在正文里，
不破坏结构（章节、页眉、覆盖度声明都是合规的），这样失败原因不会含混：

| # | 埋的错误 | 位置 | 由哪项检查抓出 |
|---|---|---|---|
| 1 | 页锚 `(p.9)` 超出全书 5 页 | 第三节 | 页锚范围 |
| 2 | 引文「本书是世界上第一本系统论述网格的著作。」原文里根本没有 | 第七节 | 引文逐字核对 |
| 3 | 把维基百科链接写进了正文，而不是【联网补充】节 | 第一节 | 联网内容标注 |

第 2 条是最要紧的那个：这句话读起来完全像这本书会说的话，结构、语气、页码标记
都做得规规矩矩——**人眼几乎不会怀疑它**。只有回原文逐字比对才能发现原书没写过。
这正是把"防编造"从口号变成脚本检查的意义。

预期输出（节选）：

```
❌ 页锚
     超出总页数 5：[9]
❌ 引文逐字核对
     第 53 行：「本书是世界上第一本系统论述网格的著作。…」在原文中找不到
❌ 联网内容标注
     【联网补充】章节之外出现网址：
     第 18 行：另据 https://zh.wikipedia.org/wiki/Grid_(graphic_design) 介绍，
结论：不通过（3 项必须修，0 项待核）
```

## 说明

- 演示书让"印刷页码 == PDF 页号"，省掉页码校准的干扰。真实书籍（尤其扫描版）
  两者通常差一个固定偏移，前置页还常用罗马数字另起一套编号——所以页锚统一用
  **PDF 页号**，见 `references/extraction.md`。
- `.bookwork/` 是工作目录，已在 `.gitignore` 里排除。
