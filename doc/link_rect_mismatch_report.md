# 超链接热区框与翻译后文本位置对不上的分析报告

> 版本：v1.1 · 日期：2026-08-05
> 现象：翻译后 PDF 中，点击链接时的热区框（link annotation `/Rect`）与页面上实际渲染的译文文字对不齐 —— 框留在原文位置，而译文文字被重新排版到了别处。
> 结论（TL;DR）：这是**结构性问题而非坐标换算 bug**。翻译管线只替换页面的内容流（`/Contents`），从不读取/重映射/重建页面的链接注释（`/Annots`），因此链接框会原样继承自源 PDF 页对象，永远指向**源文本的旧几何**，而译文按全新排版输出。任何发生位置/宽度/行数变化的段落，其链接框与译文必然错位；由于字体替换导致几乎每段都变，错位是系统性的。
> 修复状态：✅ **v1.1 已按「长线实现」落地方案 A（重投影）**，实现与验收见新增 §六。全量回归 1500 passed, 0 failed。

---

## 一、现象与证据链

### 1.1 现象
- 用户点击译文中的网址/参考文献/目录跳转时，鼠标热区仍落在源语言字形所在的位置，而非译文文字上。
- 视觉上表现为「一个看不见（或浏览器高亮的）矩形框」与屏幕上可见的译文文字错开。
- mono（英/中交错）与 dual（纯中文）两个输出都存在，但范围不同（见 §1.4）。

### 1.2 复现（等价的独立探针，非断言）
用与 `translate_stream` 完全相同的文档操作序列（复制源文档 → 替换内容流 → `insert_file` 合并 → `move_page` 交错）复现：

```python
# 源页文字 insert_text((72,100), ...)，链接框 Rect(72,90,260,104)
# 1) 替换页面内容流为译文（obj_patch 等价操作）
zh[0].set_contents(新内容流);  zh[0].insert_text((72,60), "translated ...", ...)
# 2) 查询链接框
print(zh[0].get_links())
#   → [(72.0, 90.0, 260.0, 104.0, 'http://example.com')]   # 框仍在 y=90..104
# 3) 查询译文文字实际 bbox
print(zh[0].get_text('dict')['blocks'][0]['bbox'])
#   → (72.0, 48.2, 324.5, 63.3)                             # 文字已到 y≈48..63
# 4) 合并成 mono：两页 get_links 都是同一条原始 rect
```

结论：**内容流被换掉后，链接框没有跟着走** —— 框属于页面字典 `/Annots`，翻页/复制/合并时会原样保留；而它描述的是`源文字`的位置，不是译文的位置。

### 1.3 代码路径核对
1. `pdf2zh/pdfinterp.py:266-293`：`process_page` 生成新的内容流 `ops_new`（译文渲染指令），并通过 `self.obj_patch[xref_key] = "q {ops_base}Q 1 0 0 1 {x0} {y0} cm {ops_new}"` 整体打包。**全程不触及 `/Annots`**。
2. `pdf2zh/high_level.py:431-454`：`obj_patch` 逐条 `doc_zh.update_stream(obj_id, ops_new)` 覆盖内容流对象；`high_level.py:458-466` 用 `set_contents` 把页面接到新内容流。**只动了内容，没动注释**。
3. `pdf2zh/high_level.py:323-326`：`doc_en = Document(stream)` 与 `doc_zh = Document(stream)` 都从**同一份源字节流**初始化 —— 链接注释已随源页对象进来。
4. `pdf2zh/high_level.py:478-483`：`doc_en.insert_file(doc_zh)` 后 `move_page(...)` 交错 —— 注释随页对象复制，原样保留。
5. 全仓检索 `get_links / insert_link / add_link / first_annot / /Annots / /Rect / /URI`：除 PDF/A 与 i18n 文案外**没有任何链接重映射逻辑**（`pdf2zh/high_level.py:573+` 的 PDF/A 转换也不处理链接位置）。

### 1.4 影响范围
| 输出 | 组成 | 链接框状态 |
| :--- | :--- | :--- |
| mono（`doc_en`，英中交错） | 偶数页 = 原始英文页（内容流未动） | ✅ 框与英文文字对齐（保持源正确性） |
| 同上 | 奇数页 = 插入的译文页（内容流已替换） | ❌ 框 = 源 rect，译文位置已变 |
| dual（`doc_zh`，纯中文） | 全部为译文页 | ❌ 所有带链接的页错位 |

即：任何被重新排版过的页面上，链接框都错位；只有 mono 中未翻译的英文原页是对的。

---

## 二、为什么「必然错位」：三个叠加因素

链接框固定 = `源文字`的包围盒；译文文字位置由排版器决定。两者在三个层面都会分道扬镳：

### 2.1 字体替换改变字形度量（几乎每段都错）
`pdf2zh/high_level.py:316-331` 用 GoNotoKurrent 等 noto 字体替换原始字体。同一字符串在不同字体的 advance width 不同 → 译文即使从同一 x0 起笔，**右边界也会漂移**。链接框宽度 `x1-x0` 是按源字体算的，覆盖不住/覆盖多了。

### 2.2 碰撞避让与行高重排改变 y
`pdf2zh/converter.py:792` `y += shift`（碰撞求解下移）、行高按目标语言 map（`LANG_LINEHEIGHT_MAP`，`converter.py:501-505`）重算后，段落整体 y 与行距都变。链接框 `y0/y1` 停在源行高上。

### 2.3 换行规则改变行数
CJK 译文更长、按 `x1_bound` 折行（`converter.py` toc_mode / S4 折行）后，一句话可能从 1 行变 2 行、或起止 x 改变 → 链接框既盖不准单行，也覆盖不了多行。**同一链接若其源文字跨两行，则译文的两行位置与源两行完全无关，链接框完全失效。**

> 反证：如果只是坐标 y 翻转，那么翻页旋转页会时而对时而不对；而此处错位在无旋转、无页偏的简单页面上也复现（探针 §1.2），说明不是坐标变换 bug，而是「注释未被重映射」这一设计空缺。

---

## 三、根因定位（按严重度）

| # | 根因 | 位置 | 性质 |
| :--- | :--- | :--- | :--- |
| R1 | 管线不读取任何 link 注释，不随译文重算 rect | 全链路（`pdfinterp.py` / `high_level.py` / `converter.py`） | 功能缺失 |
| R2 | 页面 `/Annots` 随 `insert_file`/文档复制原样迁移，旧 rect 被带进译文页 | `high_level.py:323-326, 478-483` | 数据迁移但语义失效 |
| R3 | 译文排版（字体 advance + 碰撞位移 + 行高 + 折行）与源排版不可比 | `converter.py` 排版段 | 必要条件，非 bug |
| R4 | `_gate_records` 已记录段落「源几何→求解后几何」映射（V8.4 门控），但未用于链接重映射 | `pdf2zh/v3/mainline_wiring.py` | 具备修复所需的桥接数据，未利用 |

R4 是修复的关键资产：`receive_layout` 已在 `_gate_records` 中记录每个段落碰撞求解前的 `x0,x1,y` 与求解后的 `y`（含每行 `lidx`），即**段落级 src→dst 几何映射**已存在，只差把链接框投影到译文段落上。

---

## 四、修复方向（建议，未实现）

### 方案 A（推荐）：链接框随译文段落重投影
在 `obj_patch` 写回之后、子集化/写盘之前（`high_level.py` 约 454→548 之间），对 `doc_zh`（dual）每个译文页：
1. 用 `page.get_links()` 取每条链接的 `from` rect 与目标文字；
2. 用 pdfminer 已解析的 lazy 段落几何（或直接复用 `_gate_records` 的 src 段落矩形）定位该 rect 命中的源段落；
3. 用求解后几何计算位移 `(Δx, Δy)` 与宽度缩放 `w_tran / w_src`，重写 `/Rect`；
4. 多行链接按命中的多段落合并成联合发生区域（或按行拆成多段热区）；
5. `update_link` 写回，随后在 mono 的译文页上也做同样处理（原英文页不动）。

工作量集中在「源段落 ⇄ 链接 rect」的匹配（按中心点包含判定即可，链接 rect 通常与源段落 bbox 高度重合）。

### 方案 B（兜底）：译文页删除链接
对无法可靠匹配的译文页直接删除 `/Annots`。代价是丢失点击能力，但保证「不出现错误热区」—— 对「宁缺毋滥」场景可接受。

### 方案 C（最小见效）：仅长度近似
不重投影，只把 rect 整体平移到译文段落新 y、并按字号比例放大/缩小 —— 解决「框完全错开」的观感，但宽度/行数仍可能不精确。

### 验收口径
- 用含链接的 PDF（网址 / 内部跳转 / 跨行链接）翻译为 zh-CN，逐条断言：
  - 译文页链接 rect 与 `get_text('dict')` 对应译文 span bbox 的 IoU（≥ 0.5）；
  - mono 原英文页链接 rect 保持原值（零回归）；
  - 链接总数不变（不丢链接、不产生幽灵链接）。

---

## 五、结论

- 链接框与译文对不上是**管线未对 link 注释做任何重映射**的结构性缺失，不是数值/坐标 bug；
- 根因 R1–R3 决定错位是系统性的（字体替换、碰撞位移、折行共同作用），mono 的 `原文页` 是唯一不受影响的部分；
- 已存在的 `_gate_records`（V8.4）正好提供了段落级 src→dst 几何，为方案 A（重投影）准备了桥接数据，修复成本可控；
- 建议先实现方案 A 并配 §四 的验收测试，可同时覆盖 dual / mono / 多行链接三种形态。

---

## 六、修复落地（v1.1，方案 A 长线实现）

### 6.1 设计原则（对应「长线、可维护、健壮」要求）
- **纯逻辑与 I/O 分离**：新模块 `pdf2zh/v3/link_remap.py` 只做 rect 数学（归一化、中心点匹配、IoU、仿射投影、多段合并），完全不 import fitz；fitz 只出现在最后的 `remap_document_links` 入口。
- **复用既有桥接数据（R4）**：不新增解析通道，直接扩展现有 `_gate_records`（`v3/mainline_wiring.py:16`），每段同时记录 `src_box`（pdfminer 源几何，与链接 /Rect 同坐标系）与 `dst_box`（求解后译文几何）。
- **converter.py 零净增行**：848 行守线保持 848；只在第 807 行把 `pstk[id].y0/y1` 追加进既有记录调用（单行改动，不新增行）。
- **side-channel 不变式**：采集/投影全链路异常仅记日志，绝不向主链路抛错；无桥接数据时安全降级为「不重定位」（保持旧行为，零回归）。
- **灰度开关**：`FeatureFlags.relink_links`（默认 True）+ `ServiceConfig.relink_links`（默认 True），可一键回滚；runtime_service 已同步到全局 flags。

### 6.2 代码变更清单
| 文件 | 变更 |
| :--- | :--- |
| `pdf2zh/v3/link_remap.py` | **新增**：`normalize_rect/rect_area/rect_center/rect_iou`、`match_link_to_paragraphs`（中心点包含 + IoU 兜底）、`project_rect`（仿射投影，退化源框降级平移）、`compute_link_updates`、`records_to_boxes`（新旧记录 schema 兼容）、`remap_document_links`（fitz 入口，逐页守护 + 旋转页跳过 + cropbox 偏移可选） |
| `pdf2zh/v3/mainline_wiring.py` | `_new_gate_record` 增加 `src_y0/src_y1` → 输出 `src_box/dst_box`；`run_mainline_channels` 按页存档 `conv.gate_records_by_page[pageid]`（link_remap 开启时） |
| `pdf2zh/converter.py` | 第 807 行传递 `pstk[id].y0, pstk[id].y1`（**行数 848 不变**，< 850 守线通过） |
| `pdf2zh/high_level.py` | `translate_patch` 增加 `link_remap` 参数并写 `device.gate_records_by_page`；`v3_output["link_records"]` 回传；`translate_stream` 增加 `relink_links` 参数，在 `set_contents` 之后、`insert_file` 之前调用 `_relink_translated_doc(doc_zh, v3_output)`（译文副本在 mono 合并时自动继承修正 rect，原文页不动） |
| `pdf2zh/v3/feature_flags.py` / `services/runtime_service.py` | 新增 `relink_links` 开关（默认 True），服务层透传 |

### 6.3 验收结果（对照 §四 验收口径）
| 验收项 | 结果 |
| :--- | :--- |
| 译文页链接 rect 与译后 span IoU ≥ 0.5 | ✅ `tests/v3/test_link_remap.py::TestFitzIntegration::test_relink_rect_tracks_translated_span`：源 rect `(72,90,260,104)` → 重定位后 `(72,48,320,63)`，与译后 span bbox 完全重合 |
| mono 原英文页链接 rect 零回归 | ✅ 探针验证：mono 偶数（原文）页 rect 保持 `(72,90,260,104)` 原值；奇数（译文）页继承修正值 |
| 链接总数不变 | ✅ 只对匹配段落执行 `update_link`，不增删链接；无匹配时原 rect 原样保留 |
| 全量回归 | ✅ **1500 passed, 0 failed**（含新增 19 条 link_remap 单测 + 2 条主链路桥接测试） |
| converter 守线 | ✅ 848 行（< 850），`test_v4_migration.py::TestV4Migration::test_line_count` 通过 |

### 6.4 已知边界（有意为之，均已记录在代码注释）
- 旋转页（`page.rotation != 0`）跳过重定位（保守处理，日志可见）。
- 非零 cropbox 偏移页通过 `page_shifts` 参数支持（测试覆盖）。
- 无 `_gate_records` 桥接数据的路径（如并行 worker 内、旧调用方）自动降级为「不重定位」，行为与 v1.0 一致。
- 链接若完全落在记录段落之外（如整页水印链接），保持原 rect 不动（宁可不错，不可错配）。

*证据：`pdf2zh/pdfinterp.py:266-293`、`pdf2zh/high_level.py:316-483`、`pdf2zh/converter.py:501-505,792`、`pdf2zh/v3/mainline_wiring.py`（gate 记录）、`pdf2zh/v3/link_remap.py`（V8.5 新增）。*