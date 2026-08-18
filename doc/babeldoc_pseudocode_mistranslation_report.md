# BabelDOC 伪代码误翻译分析报告

> 日期：2026-08-13
> 环境：BabelDOC 0.6.4（site-packages），doclayout 模型 `doclayout_yolo_docstructbench_imgsz1024.onnx`
> 现象：使用 BabelDOC 模式翻译含伪代码的论文时，伪代码块（Algorithm 标题、Input/Output、循环/赋值语句）被当成普通正文一起翻译。

## 结论（TL;DR）

BabelDOC 对"算法/伪代码块"的保护完全依赖**布局模型输出的 `algorithm` 类别**：识别为 `algorithm` 的字符会被跳过翻译并原样保留。但当前安装的布局模型 `doclayout_yolo_docstructbench_imgsz1024.onnx` **只有 10 个类别，其中没有 `algorithm`**，伪代码块被模型识别为 `plain text`，从而进入普通文本段落被翻译。

实测（用本地模型对构造的伪代码页做推理）：

```
detections: 1
  plain text   conf=0.82   xyxy=[68.6 59.5 321.2 275.9]
```

整个伪代码块（含 "Algorithm 1: ..." 标题、Input/Output、循环体）被一次性框为 `plain text`，无任何 `algorithm` 检出。

## 根因链路

### 1. BabelDOC 的伪代码保护机制（依赖布局类别）

- `babeldoc/format/pdf/document_il/utils/layout_helper.py` `is_text_layout()`：白名单判断是否为"文本布局"。**`algorithm` 不在白名单中**（`plain text`、`paragraph`、`title` 等均在）。
- `babeldoc/format/pdf/document_il/midend/paragraph_finder.py` `_group_characters_into_paragraphs()`：

```python
if not is_text_layout(char_layout) or self.is_isolated_formula(char):
    skip_chars.append(char)
    continue
```

非文本布局（含 `algorithm`）的字符被收集进 `skip_chars`，**不进入 `pdf_paragraph`** → 不参与翻译。

- `babeldoc/format/pdf/document_il/backend/pdf_creater.py`：导出阶段把 `page.pdf_character`（即 skip_chars）**原样绘制保留**，因此若模型正确输出 `algorithm`，伪代码会以原文出现在输出 PDF 中。

### 2. 翻译阶段不做任何兜底

- `babeldoc/format/pdf/document_il/midend/il_translator.py` `get_translate_input()`：仅跳过纯数字、纯占位符、纯公式段落，**不检查 `layout_label`**。
- 整个 BabelDOC 0.6.4 包内 `rg -i "pseudo|code_block|is_code"` **零命中**：没有基于关键词/样式的伪代码兜底检测。

### 3. 模型缺类别是根因

- `babeldoc/assets/assets.py` `get_doclayout_onnx_model_path()` 固定下载/使用 `doclayout_yolo_docstructbench_imgsz1024.onnx`。
- 该模型 ONNX metadata `names`（实测）：

```
{0:'title', 1:'plain text', 2:'abandon', 3:'figure', 4:'figure_caption',
 5:'table', 6:'table_caption', 7:'table_footnote', 8:'isolate_formula',
 9:'formula_caption'}
```

- 共 10 类，**无 `algorithm` 类**，也无 `code_algo_hybrid`。
- 而 `layout_helper.py` 的 `layout_priority` 列表里存在 `algorithm`、`code_algo_hybrid`、`line_number_hybrid` 等类别名——说明 BabelDOC 代码为含这些类别的模型（如 DocLayout-YOLO 21 类 / DocLayout-YOLO-Next 混合标签模型）预留了跳过路径，但当前精简模型永远不会输出这些标签。

### 4. 为什么之前可能没暴露

- DocLayout-YOLO 完整版（21 类）含 `algorithm` 类；若旧环境/旧模型为完整版，伪代码块会被识别为 `algorithm` 而正确跳过。
- 当前 10 类 docstructbench 变体移除了 `algorithm` 类，`algorithm` 退化为 `plain text`（行为回归）。

## 触发条件

任何"带框/不带框的伪代码或算法环境"（Algorithm 标题 + Input/Output + 缩进循环体），只要被模型框为 `plain text` 或 `paragraph`，都会被翻译。模型对伪代码的识别准确率与排版样式（框线、双栏、背景色）无关——本测试中无框无背景的纯文本伪代码即被误判为 `plain text`。

## 修复方向（按推荐顺序）

1. **更换布局模型为含 `algorithm` 类的版本（根治）**
   - BabelDOC 的 `TranslationConfig(doc_layout_model=...)` 已支持注入自定义模型。
   - fork 侧 `pdf2zh_next/high_level.py create_babeldoc_config()` 与 `pdf2zh/babeldoc_adapter.py` 中 `doc_layout_model=None` 均走默认模型；可改为加载 DocLayout-YOLO 21 类模型（或等待 BabelDOC 上游将默认模型升级为含 algorithm 类的版本）。
   - 注意：21 类模型的类别名映射需要与 `is_text_layout`/`layout_priority` 兼容（`algorithm` 已在列表内，兼容）。

2. **fork 侧关键词兜底（短期可行）**
   - 在翻译前（pdf2zh 的 BabelDOC translator wrapper 的 `translate()` / `translate_batch()`，或 `get_translate_input` 之后）对段落文本做启发式检测：
     - 行首匹配 `^\s*(Algorithm|Procedure|Function)\s+(\d+|[A-Za-z])`
     - 含 `Input:` / `Output:` / `Require:` / `Ensure:` 且行数 ≥ 2
     - 行模式命中 `for .* do`、`end for`、`while .* do`、`if .* then`、`return \w+` 等
   - 命中的段落直接返回原文（不翻译），可做双语占位（dual 模式保留原文，mono 模式也保留原文——与 algorithm 布局行为一致）。
   - 风险：误伤正文中包含此类句子的段落（伪阳性），建议加行数/整体匹配率阈值。

3. **上游报告**
   - 建议向 BabelDOC（PDFMathTranslate-Next / YADT）反馈：默认模型缺少 `algorithm` 类导致伪代码被翻译；可附带本报告的实测证据。

## 验证步骤（复现）

```bash
# 构造伪代码 PDF 并用本地模型推理（脚本已实测）
python diag_pseudo.py
# 输出: 整个伪代码块 → plain text (conf=0.82)
```

复现翻译行为：用 BabelDOC 模式翻译上述 PDF（英→中），输出 PDF 中伪代码行会被翻译为中文。

## 涉及文件

| 文件 | 作用 |
|---|---|
| `babeldoc/.../midend/paragraph_finder.py` | 非文本布局字符 → skip（不翻译）|
| `babeldoc/.../utils/layout_helper.py` | `is_text_layout()` 白名单（无 algorithm）、layout_priority |
| `babeldoc/.../midend/il_translator.py` | `get_translate_input()` 无布局兜底 |
| `babeldoc/.../backend/pdf_creater.py` | skip_chars 原样绘制保留 |
| `babeldoc/assets/assets.py` | 固定 10 类 docstructbench 模型 |
| fork: `pdf2zh_next/high_level.py create_babeldoc_config()` | `doc_layout_model=None`（默认模型）|
| fork: `pdf2zh/babeldoc_adapter.py` | 同上 |


## 修复落地（2026-08-13，本仓库）

采用"修复方向 1 + 方向 2 折中"：**不替换默认布局模型**，而是引入
**PP-DocLayoutV2**（25 类，含 `algorithm`）作为辅助检测器，与默认 10 类模型
**融合**（见 `pdf2zh/doclayout_pseudocode.py`）。

### 机制

1. BabelDOC 默认模型照常负责全部常规布局（翻译质量行为不变）；
2. 每页额外用 PP-DocLayoutV2 检出 `algorithm` 框；
3. 默认模型输出的**文本框**若被 `algorithm` 框覆盖比例 `>= 0.35`，类别被改写为
   `algorithm`，置信度抬到 0.99 —— BabelDOC `layout_priority` 中 `algorithm`
   优先级第 4 位，其字符直接跳过翻译、输出 PDF 原样保留。

### 入口注入（`doc_layout_model`）

| 入口 | 位置 | 方式 |
|---|---|---|
| CLI `--babeldoc`（legacy） | `pdf2zh/pdf2zh.py` `yadt_main()` | `doc_layout_model=_build_doclayout_model()` |
| GUI / 服务（legacy fallback） | `pdf2zh/babeldoc_adapter.py` `run_babeldoc_translation()` | 同上 |
| GUI / 服务（next kernel 主路径） | `pdf2zh/babeldoc_next_adapter.py` | `create_babeldoc_config()` 之后 `config.doc_layout_model = build_pseudo_code_protected_layout_model()` |

kernel submodule（`pdf2zh_next/high_level.py create_babeldoc_config()`）**不改**：
其 `doc_layout_model=None` 由 next adapter 在调用点覆盖。

### 模型分发

- 模型：`PP-DocLayoutV2.onnx`（约 204 MB，PaddlePaddle 官方 / alex-dinh ONNX 版）。
- 路径：`babeldoc.const.get_cache_file_path("PP-DocLayoutV2.onnx", "models")`
  （即 `~/.cache/babeldoc/models/`），可用环境变量 `PDF2ZH_PP_DOCLAYOUT_MODEL` 覆盖。
- 下载：`python -m pdf2zh.doclayout_pseudocode --download`（hf-mirror，幂等）。
- 缺失/加载失败 → 自动降级为默认模型，BabelDOC 模式本身不受影响。

### 验证（实测）

```bash
python tools/diag_fused_babeldoc.py   # 端到端对照
```

| 项目 | 默认模型（修复前） | 融合模型（修复后） |
|---|---|---|
| 伪代码行被翻译 | 6/7 | **0/7** |
| 正文仍被翻译 | 1/1 | 1/1 |
| mono PDF 保留伪代码原文 | 部分损坏 | **7/7，无损坏** |

真实论文页误检扫描（arxiv_1412.6980 / arxiv_2410.12628 前 8 页）：仅含
Algorithm 1 的页面被保护，无伪代码页面零提升。单元测试
`tests/test_doclayout_pseudocode.py`（10 条）覆盖提升/阈值/非文本类/numpy conf。
