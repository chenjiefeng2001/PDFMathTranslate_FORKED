# 7I-2 — F4 Render Investigation（page 300，只取证未修）

日期：2026-08-31 · 对象：7I-1 requalification 发现的 `F4 × 2 @ page 300`（FDS=render）
方法：7H stage-faithful 溯源链展开 + page-300 artifact 证据链 + 嵌入字体逆向。

## 1. 结论（先说答案）

> **F4 归类 = E 变体（源 PDF 自带缺陷，本管线零责任）+ 检测器语义盲区。**
> 两个 `(cid:N)` 字面量在 **parser 阶段**就存在于源文本——源 PDF 的字体编码
> 缺口（MTMI 无 code 3 映射、Times-Roman 无 ToUnicode），pdfminer 按 spec 输出
> `(cid:N)` 占位。本管线 model→translation→layout→render **忠实透传**，未引入
> 也未放大。检测器把「源 PDF 固有的 cid 占位」误记到 renderer 头上
> （first_divergence 应为 **parser** 而非 render）。

## 2. 两个 F4 对象（glyph 身份已逆向确认）

| node_id | kind | cid 字面量 | 字体 | glyph 身份（fontTools CFF 逆向） | 语义 |
|---------|------|-----------|------|--------------------------------|------|
| `p300_1` | heading | `(cid:129)` | GLBJJG+Times-Roman | **bullet**（•，Adobe StandardEncoding 0x81=bullet） | 列表项目符号 |
| `p300_12` | paragraph | `(cid:3)` | GLBJKM+MTMI | **Theta1**（Θ 变体，MathTimeItalic glyph#3） | 渐近记号 Θ(log w) |

证据（`doc/7i1-multiprocessor-provenance/page-300/`）：

- `src-char-3-MTMI.png`：源页 glyph 3 视觉裁剪（Θ 形）
- `rendered-cid-span.png` / `f4-side-by-side.png`：本管线渲染输出同一位置（字面量文本）
- `evidence.json`：13 块 source/parser/model/translation/layout 逐块证据
- `render-provenance.json`：渲染器逐块 provenance（13/13 present）
- `pdf-fonts.json`：页面 7 个 Type1 字体资源清单
- `visual-crop.png`：整页 150dpi 截图

## 3. 决定性证据

### 3.1 parser 阶段已存在（pdfminer 按 PDF spec 行事）

```text
pdfminer LTChar 级提取 page 300:
  char: (cid:129) | font: GLBJJG+Times-Roman | size: 10.0
  char: (cid:129) | font: GLBJJG+Times-Roman | size: 10.0
  char: (cid:3)   | font: GLBJKM+MTMI       | size: 10.0
```

pymupdf 原始提取（rawdict）同样只能拿到控制字符 `\x03` / `\x81`——**源 PDF
文本层本身就无 Unicode 信息**，不是提取器 bug。

### 3.2 字体编码缺口（逆向嵌入 CFF + ToUnicode CMap）

- **MTMI（xref 5219, CFF 4863B）**：glyph 顺序 `[0]=.notdef, [1]=parenleft,
  [2]=parenright, [3]=Theta1, ...`；其 ToUnicode CMap（xref 2082）
  `begincodespacerange <05> <7a>`——**code 3 在 codespace 之外，无任何映射**。
  → pdfminer 依 spec 输出 `(cid:3)`。
- **Times-Roman（xref 5206）**：**ToUnicode = null**；glyph 表含 `bullet`
  （glyph#79）；Adobe StandardEncoding 中 0x81 = bullet。
  → pdfminer 输出 `(cid:129)`。

### 3.3 本管线各阶段逐级透传（stage-faithful）

```text
parser  : source_text = '...has depth (cid:3)(log w). Can...'   ← 已含字面量
model   : span.glyphs = ['(cid:3)', '(']                        ← 透传
identity: translated  = 同 source                                ← 透传（preserved 语义不变）
layout  : plan.text   = 同                                       ← 透传
render  : 输出 PDF Helvetica 5pt 字面量 '(cid:3)' 写入文本层        ← 透传
detector: rendered_text 含 '(cid:' → F4 @ first_divergence=render  ← 归因错位
```

视觉对照（`f4-side-by-side.png`）：源页该位置显示 **Θ**（数学符号），
渲染输出显示 **"(cid:3)" 字面文本**——渲染层「忠实」重现了 parser 给的坏文本。

## 4. F4 候选归类裁决

| 候选 | 判定 | 依据 |
|------|------|------|
| A font selection | ❌ | 渲染用 Helvetica 正常出字，无字体选择错误 |
| B Unicode→glyph mapping | ❌（对本管线而言） | 缺映射发生在**源 PDF**，非渲染侧 |
| C embedded font/resource | ❌（同上） | 源字体资源完整，缺的是 ToUnicode 元数据 |
| D renderer text emission | ❌ | 渲染器逐字透传，未改写 |
| E inspection false positive | **⚠️ 半成立** | 缺陷**真实存在**（译文文本层有 cid 垃圾），但**归因错位**：FDS 应为 parser（源 PDF 固有），检测器记成 render |

## 5. 修复方向（不在本阶段实施）

1. **检测器归因修正（低成本，建议先做）**：`_detect_f4_font_anomaly` 前先比对
   `source_text`——若 `(cid:` 已存在于 source/parser 文本，`first_divergence`
   应判 **parser**（源 PDF 固有），而非 render。`stage_verdicts.parser=FAIL`。
2. **语义还原（可选增强）**：已知映射表（MTMI/cmmi glyph 名 → Unicode）：
   `Theta1→Θ`、`bullet→•`；parser 侧对 `(cid:N)` 做「字体名+glyph 顺序」查表
   还原，还原失败才保留字面量。属 7I-detectors/语义层工作。
3. **真实翻译场景注意**：`(cid:N)` 字面量会被 translator 当普通文本翻译——
   `preserved` 语义（code/formula）不受影响，但普通段落中的 cid 会进译文。
   修复 2 同时解决此问题。

## 6. 状态

7I-2 取证阶段 **✅ COMPLETE**（F4 × 2 定性：源 PDF 固有 + 检测器归因错位）。
修复（检测器归因 + cid 语义还原）→ 下一阶段决策。
