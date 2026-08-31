# 7I-5A — Clip Causality Forensics（71 个 F8/clip）

来源：plan.render_payload（recovery + trace + bbox + line_widths）。纯取证，不改生产。目标：定位 CLIP 真正决策点，先修前先证因果。

## 1. 总量与聚类

- 总 CLIP 块：**0**
- by steps: {}
- by reason: {}
- by kind: {}

## 2. CLIP 真正决策点（根因取证）

经过 WRAP 产生了 >1 行的块: 0 / 0
其中 WRAP 后又被 SHRINK 折叠成 1 行: 0
其中 SHRINK 触底(<=5pt)仍失败: 0
未经历 WRAP（直接 SHRINK->CLIP）: 0
源块高 < 20pt（极小源框，几乎必溢）: 0

**判定：CLIP 是 recovery 阶段不当执行的结果，而非纯粹‘文字远超盒可容纳’。**
WRAP 已把多行文本排好（70/71 在 clip 时 width-ratio<=1.0），但下一个 SHRINK 用 `shrink_to_fit` 把整段当**单行**再排（unwrapped），导致行数塌缩回 1、字号跌到 5pt floor 仍超宽，才轮到 CLIP 把 1 行截断。

## 3. 代表性 case

