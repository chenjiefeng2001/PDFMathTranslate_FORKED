# Commit 7A — Architecture Audit + RenderPlan Unification

> 目标：把「每个 semantic feature → 一个 side-channel → 一个 renderer 特判」
> 收敛为「SemanticNode → TranslationUnit → 统一 RenderPlan → 按 kind 分派的
> Renderer」，并重新审视 `converter.py` 1094 行的定位。
>
> 本 commit 不做 Layout（那是 7B+）。

## 1. 现状盘点（改动前）

### 1.1 Side-channels

| side-channel | 职责 | 消费端 |
|---|---|---|
| `v3/semantic_sidechannel.py` | 代码保护 / 样式 marker（字符级） | `converter.py`（keep 掩码 + PDF text op） |
| `v3/list_sidechannel.py` | List 块逐 item 翻译载荷 | `document_model.translate_document` → `magicpdf_renderer` |
| `v3/toc_sidechannel.py` | TOC 结构化条目（title-only 翻译） | `document_model.translate_document` |
| `v3/toc_render_sidechannel.py` | TOC 逐条目渲染命令 | `document_model.translate_document` → `magicpdf_renderer` |
| `v3/formula_side_channel.py` | 公式 LaTeX（magicpdf 路径） | `magicpdf_cli` |

### 1.2 旧问题（7A 要消除的）

1. **Translation 特判散落**：`translate_document` 里 4 条 `if block.kind == ...`
   分支，各自调用自己的 side-channel、各自写自己的 metadata 字段
   （`list_items` / `toc_entries` / `toc_commands`）。
2. **Renderer 字段探测**：`magicpdf_renderer` 靠「`entry.get("list_items")` 非空」
   「`entry.get("toc_commands")` 非空」判断渲染路径，而不是显式的 kind。
3. **无统一 TranslationUnit**：没有「一个块 = 一个翻译单元」的抽象；
   list/toc 各自定义了自己的载荷契约。
4. **converter 行数定位模糊**：1094 行是「没超 1095」的结果，不是
   「converter = orchestration only」的结果。

## 2. 7A 做了什么

### 2.1 新增 `v3/render_payload.py` — 统一分派入口

```python
block_translation_unit(block, translate_fn, model) -> TranslationUnit
    # kind ∈ {skip, preserve, list, toc, flow}
    # payload 携带结构化载荷（list 的 commands / toc 的 entries+commands）
    # 任何异常回退 flow（side-channel 纪律）

build_render_payload(unit) -> {"kind", "commands", "entries"}
payload_commands(payload) -> [RenderCommand dicts]
```

要点：

- **Translation 输入统一**：`translate_document` 不再自己写 4 条特判，而是
  调 `block_translation_unit` —— 一个块只有一个 TranslationUnit。
- **Geometry 只消费不重算**：`payload` 里的 title_x/page_x/indent/bbox/
  marker_x/content_x 全部来自解析阶段并原样透传；本模块不重推断。
- **兼容性**：`translate_document` 仍把 `list_items` / `toc_entries` /
  `toc_commands` 写回 block.metadata（既有消费端/测试依赖）。

### 2.2 `document_model.translate_document` — 重构为统一分派

改动前：4 条特判 + 各自 try/except + 各自统计。
改动后：`unit = block_translation_unit(...)` → 按 kind 写回 metadata + 统计。
行为完全等价（同套测试通过）。

### 2.3 `render_plan_from_model` — 输出统一 `render_payload`

```json
{
  "kind": "list" | "toc" | "flow" | "preserve",
  "commands": [...],
  "entries": [...]
}
```

保留 `list_items` / `toc_entries` / `toc_commands` 旧字段（兼容）。

### 2.4 `magicpdf_renderer` — 按 `render_payload.kind` 分派

```python
if payload_kind == "list" or (无 payload 且 list_items 非空):  → list renderer
if payload_kind == "toc"  or (无 payload 且 toc_commands 非空): → toc renderer
else:                                                          → flow text
```

旧字段探测保留为兼容回退；新路径优先读统一 payload。

## 3. 目标架构（7B 之后）

```
                    SemanticNode
                         │
          ┌──────────────┼──────────────┐
          ↓              ↓              ↓
      CodeBlock       ListNode       TOCNode
          │              │              │
          └──────────────┼──────────────┘
                         ↓
                  TranslationUnit        ← 7A：已统一
                         ↓
                    Render Plan          ← 7A：已统一 render_payload.kind
                         ↓
                 ┌───────┼────────┐
                 ↓       ↓        ↓
               Code     List      TOC
              Renderer Renderer Renderer  ← 按 kind 分派（已统一）
```

## 4. converter.py 定位（重新审视 1094 行）

- 1095 行本身不是目标；目标是 `converter = orchestration only`。
- 现状：converter 只 import 一个 v3 side-channel（`semantic_sidechannel`），
  其余特判（list/toc/style 翻译、渲染载荷）全部外移到 v3 层。**方向正确**。
- 7A 不动 converter（行数维持 1094），7B 起如果 converter 出现新的
  编排需求，优先外移而非加行。

## 5. 验收

- [x] `tests/test_toc_document_integration.py`（11）通过 —— TOC 结构化路径不回归
- [x] `tests/test_semantic_toc_renderer.py` + `test_toc_render_integration.py`
      （28）通过 —— 视觉 TOC 几何不回归
- [x] `tests/test_magicpdf_list_render.py` + `test_semantic_list_renderer.py`
      （15+）通过 —— List 渲染不回归
- [x] `tests/v3/test_v12_document_model.py` / `test_v23_layout_inspector.py` 通过
- [x] `converter.py = 1094` 行（未改）
- [ ] 全量 `tests/` 回归（Windows 既有 2 项已知失败除外）
