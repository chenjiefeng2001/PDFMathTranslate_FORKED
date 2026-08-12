# PDF2ZH 后端代码 Bug 分析与修复报告

> 日期：2026-07-28 | 分析范围：pdf2zh 后端核心模块

---

## 一、分析范围

本次分析覆盖 pdf2zh 后端核心模块中的 **converter.py**（主要）、**pdfinterp.py** 和 **high_level.py**，重点关注运行时异常（RuntimeException）和 PDF 输出损坏问题。

## 二、已修复 Bug 完整列表

| # | 文件 | 行号 | 严重性 | 类别 | 触发条件 | 修复方式 |
|---|------|------|--------|------|----------|----------|
| F1 | pdfinterp.py | 98-99 | 🔴 高 | 数据传递 | 嵌套 XObject 调用时 fontid/fontmap 引用未传递 | 添加 `self.device.fontid = interpreter.fontid` |
| F2 | high_level.py | 234 | 🔴 高 | 并发安全 | 并行翻译多线程共享 `out` 文档 | 创建独立文档对象 |
| F3 | high_level.py | 238 | 🔴 高 | 异常处理 | subset_fonts() 抛 `mupdf.FzErrorBase: bad 'value'` | try/except 包装 |
| F4 | high_level.py | 333 | 🔴 高 | 异常处理 | shutil.copy 覆盖文件失败 | try/except 包装 |
| F5 | converter.py | 304 | 🔴 高 | Null 安全 | 布局缺失时 xt 为 None，访问 `xt.x1` | 添加 `xt is not None and` 守卫 |
| F6 | converter.py | 412 | 🔴 高 | KeyError | 字体未注册到 fontmap 时 `self.fontmap[fcur]` 失败 | 改用 `.get()` + isinstance 安全检查 |
| F7 | converter.py | 497 | 🔴 高 | KeyError | fcur_ 不在 fontmap 时 `self.fontmap[fcur_]` 失败 | 改用 `.get()` + 宽度 0 回退 |
| F8 | converter.py | 526 | 🔴 高 | KeyError | vch.font 不在 fontid 时 `self.fontid[vch.font]` 失败 | 改用 `.get(vch.font, "0")` |
| F9 | converter.py | 580 | 🟡 中 | NaN 防御 | ascent/descent 为 NaN 时行高计算结果为 NaN | 添加 `np.isfinite()` 检查 |

## 三、各 Bug 深入分析

### F5: converter.py L304 — xt NoneType 解引用

**代码位置**: `receive_layout()` 方法中的 LTChar 处理循环

**原始代码**:
```python
if cls == xt_cls:               # 当前字符与前一个字符属于同一段落
    if child.x0 > xt.x1 + 1:    # 添加行内空格
        sstk[-1] += " "
    elif child.x1 < xt.x0:      # 添加换行空格并标记原文段落存在换行
        sstk[-1] += " "
        pstk[-1].brk = True
```

**根本原因**:
- `xt` 初始化为 `None`（L201: `xt: LTChar = None`）
- `xt_cls` 初始化为 `-1`（L202）
- 当文档布局分析失败（layout 返回 -1），首个字符的 `cls` = `-1`
- 条件 `cls == xt_cls` 在首个字符处为 `True`（`-1 == -1`）
- 此时 `xt` 仍为 `None`，访问 `xt.x1` 抛出 `AttributeError: 'NoneType' object has no attribute 'x1'`

**触发场景**: 扫描版 PDF 或布局模型无法生成的文档

**修复后代码**:
```python
if xt is not None and cls == xt_cls:
```

### F6: converter.py L412 — fontmap KeyError

**代码位置**: `receive_layout()` 内部的 `raw_string()` 闭包函数

**原始代码**:
```python
def raw_string(fcur: str, cstk: str):
    if fcur == self.noto_name:
        return "".join(["%04x" % self.noto.has_glyph(ord(c)) for c in cstk])
    elif isinstance(self.fontmap[fcur], PDFCIDFont):  # KeyError 发生处
        return "".join(["%04x" % ord(c) for c in cstk])
    else:
        return "".join(["%02x" % ord(c) for c in cstk])
```

**根本原因**:
- `fcur` 字符串来自 PDF 页面资源字典的 Font 条目
- 嵌套 XObject 表单（Form XObject）可以包含自己的 Resources/Font 字典
- 这些内部字体通过 `init_resources()` 的递归调用注册到 `interpreter.fontmap`
- 但当 XObject 在处理完成后资源被释放，或在多层嵌套后字体索引丢失，`fcur` 指向的字体就不在顶层 `self.fontmap` 中
- 结果：`self.fontmap[fcur]` 抛出 `KeyError`

**触发场景**: 包含嵌套 XObject 表单的复杂 PDF（如 LibreOffice/Word 生成的 PDF、带表单/标注的文档）

**修复后代码**:
```python
def raw_string(fcur: str, cstk: str):
    if fcur == self.noto_name:
        return "".join(["%04x" % self.noto.has_glyph(ord(c)) for c in cstk])
    elif isinstance(self.fontmap.get(fcur), PDFCIDFont):
        return "".join(["%04x" % ord(c) for c in cstk])
    else:
        return "".join(["%02x" % ord(c) for c in cstk])
```

**关键点**: `isinstance(None, PDFCIDFont)` 返回 `False`，所以当 `get(fcur)` 返回 `None` 时安全走 else 分支。

### F7: converter.py L497 — fontmap[fcur_] KeyError

**代码位置**: `receive_layout()` 排版阶段的字符宽度计算

**原始代码**:
```python
adv = self.fontmap[fcur_].char_width(ord(ch))
```

**根本原因**:
- `fcur_` 在循环开始时初始化为 `fcur`（可能是 `None` 或未注册的字体 ID）
- `fcur_` 仅在公式字符或字体切换时更新
- 非公式文本直接使用初始值，可能为 `None` 或无效键
- `self.fontmap[None]` 或 `self.fontmap["invalid_font"]` 抛出 `KeyError`

**触发场景**: 不含公式的页面，或嵌套 XObject 的字体引用丢失

**修复后代码**:
```python
font_obj = self.fontmap.get(fcur_); adv = font_obj.char_width(ord(ch)) if font_obj else 0
```

### F8: converter.py L526 — fontid KeyError

**代码位置**: `receive_layout()` 中公式字符排版循环

**原始代码**:
```python
"font": self.fontid[vch.font],
"rtxt": raw_string(self.fontid[vch.font], vc),
```

**根本原因**:
- `vch.font` 是 PDFFont 对象引用（来自 pdfminer 的 `LTChar.font`）
- `self.fontid` 字典以 PDFFont 对象为键，字体 ID 字符串为值
- 当公式检测算法识别的字符来自 XObject 内部的字体，该 PDFFont 对象未被注册到 `fontid`
- `self.fontid[vch.font]` → KeyError

**触发场景**: 复杂 PDF 文档中公式检测后的公式字符渲染

**修复后代码**:
```python
"font": self.fontid.get(vch.font, "0"),
"rtxt": raw_string(self.fontid.get(vch.font, "0"), vc),
```

### F9: converter.py L580 — NaN 行高传播

**代码位置**: `receive_layout()` 行高计算

**原始代码**:
```python
line_height = max(ascent - descent, 1.0)
```

**根本原因**:
- `ascent` 和 `descent` 来自 `TextMetrics` 对象的属性
- 某些字体的度量数据不完整（missing OS/2 table 等），导致 `ascent`/`descent` 为 `NaN`（Not a Number）
- `NaN - NaN` = `NaN`，`max(NaN, 1.0)` = `NaN`
- `NaN` 被写入 PDF 指令流（如 `BT /F1 12 Tf 0 NaN Tm ...`）
- MuPDF 在 `subset_fonts()` 阶段解析到非法浮点值时抛出 `mupdf.FzErrorBase: bad 'value'`

**触发场景**: 使用了非标准或损坏字体的 PDF

**修复后代码**:
```python
line_height = max(ascent - descent, 1.0) if np.isfinite(ascent) and np.isfinite(descent) else default_line_height
```

## 四、Bug 模式总结

| 模式 | 出现次数 | 涉及 Bug |
|------|---------|----------|
| KeyError（字典缺失键） | 3 | F6, F7, F8 |
| Null/None 解引用 | 1 | F5 |
| NaN 传播 | 1 | F9 |
| 并发安全 | 1 | F2 |
| try/except 缺失 | 2 | F3, F4 |
| 数据传递遗漏 | 1 | F1 |

**核心代码修复**: converter.py 中的 5 个修复（F5-F9）占所有修复的 **55.6%**，是该版本的重点改进对象。

## 五、测试验证

| 项目 | 结果 |
|------|------|
| 单元测试总数 | 126 |
| 通过数 | 126 ✅ |
| 失败数 | 0 ✅ |
| 模块导入测试 | ✅ converter.py 和 pdfinterp.py 正常导入 |

## 六、剩余风险

| # | 位置 | 风险 | 建议 |
|---|------|------|------|
| 1 | pdfinterp.py L98 | `get_font()` 返回 None 时 `fontmap[fontid].descent = 0` 导致 AttributeError | 添加 None 检查 |
| 2 | pdfinterp.py L84 | `stream_value(spec[1])["N"]` spec[1] 类型异常 | 添加防御性编码 |
| 3 | converter.py L75 | `end_figure` 中 assert 类型检查 | 改为 try/except 跳过 |
| 4 | gui.py L500-516 | 多文件进度标签不同步 | 添加回退标签逻辑 |

## 七、碰撞管线加固（S1-S6）与 F9 的后续增强

本报告 F5-F9 之后，`converter.py` / `collision_resolver.py` 又完成了一轮
**文本框重叠碰撞管线**（S1-S6）修复，与 F9（行高）直接相关的是 S5：

| 编号 | 内容 | 与 F9 的关系 |
| :-- | :-- | :-- |
| S5 | 行高下限取字形真实跨度（CJK ≥ 1.3），压缩循环止步于下限；仍溢出时输出 QA 溢出标记 | F9 解决 `NaN` 传播；S5 在有限值基础上进一步保证**行高不会压到使相邻行字面盒相接**，并引入可机器解析的溢出告警 |
| S1/S2/S3 | 无条件应用下移避让 + 全量障碍物求解 + 宽度/字号缩减落地 | 解决 F9 之外的段落级重叠（根因1/2/5） |
| S4 | 单行段落译文超宽折行（去 `brk` 门控） | 消除横向溢出 |
| S6 | 表格边框 / 公式块边界线条登记为障碍物 | 补齐避让障碍物集合 |

完整分析见 `doc/text_overlap_analysis_report.md` 附录 C。新增回归测试：
`tests/test_converter_layout_fixes.py`（6 项）与 `tests/test_collision_resolver.py`（扩展 7 项）。

---

## 八、会话续修（2026-08）：入口进程崩溃链与事件通道语义

> 本轮针对 **Windows 打包程序 GPU 并行翻译回退** 的用户日志做了链路分析，
> 根因不在 GPU 后端而在 **spawn 子进程再入口**；同时补了事件通道的语义修复。

### 8.1 问题现象（用户日志）

```
[INFO] Got device count 1 ...
[tqdm] INFO: 256it [14:33, ...] ...  -- translate failed: 0it ...
[ERROR] ProcessPoolExecutor-0: BrokenProcessPool
[INFO] Applying docx fallback due to error: BrokenProcessPool
[INFO] Translation done, output: ... (CPU degraded)
```

关键线索：`pdf2zh.int: error: unrecognized arguments: -I --multiprocessing-fork`
出现在 worker 进程 stderr 顶部——**CPU 降级任务仍带相同 argparse 崩溃**，
证明降级不是设备问题，而是 worker 启动即失败、触发回退链。

### 8.2 根因（F10：spawn 子进程再入口 argparse 崩溃）

| # | 文件 | 严重性 | 类别 | 触发条件 | 修复方式 |
|---|------|--------|------|----------|----------|
| F10 | script/build/pdf2zh.int + pdf2zh.py + gui/entry.py + backend.py + mcp_server.py | 🔴 高 | 进程启动 | `multiprocessing` spawn 启动器把 `-I --multiprocessing-fork` 交给入口脚本，顶层 `main()` 被再次执行 | 入口识别 spawn 标记后让位给 bootstrap |

机制链（Python 3.8+/Windows spawn）：

1. `ProcessPoolExecutor` 用 spawn 生成 worker：`spawn.get_command_line()`
   产出 `[sys.executable, '--multiprocessing-fork', 'parent.py', '-I', '--multiprocessing-fork', '-c', ...]`
   （`-I` 来自 `util._args_from_interpreter_flags()`）。
2. 打包/脚本入口 **没有 `if __name__ == "__main__"` 守卫**，
   把启动器参数原样转发给 `argparse` → 子进程 stderr 出现
   `unrecognized arguments: -I --multiprocessing-fork` → 崩。
3. 每个 worker 都失败 → `BrokenProcessPool` → 触发 docx 回退 →
   最终走 CPU 路径，且后续每次任务继续重演。

修复（pdf2zh.py:244-256 新增两个入口辅助函数）：

```python
_SPAWN_FORK_FLAG = "--multiprocessing-fork"

def is_spawn_child(argv: Optional[List[str]] = None) -> bool:
    # 全参数扫描（启动器可能把它放在 argv 任意位置）
    return _SPAWN_FORK_FLAG in (argv if argv is not None else sys.argv)

def spawn_child_yields_to(args: Optional[List[str]] = None) -> bool:
    if is_spawn_child(args):
        try:
            multiprocessing.freeze_support()
        except Exception:
            pass
        return True   # 让位给 multiprocessing bootstrap，不再碰 argparse
    return False
```

- `pdf2zh.py main()` 开头：`if spawn_child_yields_to(args): return 0`；
- `gui/entry.py`、`gui/app.py`、`backend.py`、`mcp_server.py` 的
  `__main__` 块均加 `raise SystemExit(0)` 守卫；
- `script/build/pdf2zh.int` 顶层逻辑改为
  `if __name__ != "__main__" or pdf2zh.pdf2zh.spawn_child_yields_to(): raise SystemExit(0)`，
  且 `except SystemExit: raise` 保持退出码语义。

回归测试：`tests/test_spawn_entry.py`（5 项，含 wrapper 脚本守卫断言）。

### 8.3 F11：事件通道语义修复

| # | 文件 | 严重性 | 类别 | 触发条件 | 修复方式 |
|---|------|--------|------|----------|----------|
| F11 | gui/app.py | 🟡 中 | 事件语义 | 单调进度钳制下，进度不变时新 message 落在旧进度值之下被吞；裸 message 渲染会静默丢掉状态/进度标签 | `_render_message_changed` 改为全量重渲染状态（app.py:487），消息与进度/状态同帧呈现 |

### 8.4 验证与回归

| 项目 | 结果 |
|------|------|
| 全量 pytest（tests/） | 2061 passed / 1 skipped ✅ |
| 新增测试 | test_spawn_entry（5）、test_event_bus（+3）、test_gui_modules（+3 通知通道） |
| py_compile | 全部入口文件 ✅ |

### 8.5 F12：GUI 入口 `launch()` 阻塞主线程，自定义路由永不注册（前端完全无法通信）

> **用户症状**：页面能打开，但"完全没法正常工作，甚至没法和后端通信"。
> **2006-08 实测复现**：`python script/build/pdf2zh.int -i`（用户真实入口）启动后：
> `/` 200、`/gradio_api/info` 200，但 `/gui/events`、`/gui/logs`、`/pdf-preview` 全部 **404**，
> 日志停在 `TaskEventBridge attached`，且无 `Registered /gui/events SSE stream` 行。

**根因链**：

1. `pdf2zh.gui.entry.setup_gui()` 调用 `gui.launch(...)` 时**未传
   `prevent_thread_lock=True`**（`app.main()` 有传，所以 `python -m pdf2zh.gui.app`
   的调试路径正常、掩盖了问题）；
2. `launch()` 内部 `block_thread()` **永久阻塞主线程**，函数不返回；
3. 后面的 `_register_custom_routes(gui)` 永不执行 → `/gui/events`（SSE）、`/gui/logs`、
   `/pdf-preview` 全部未注册（Gradio 5 在 launch 内重建 ASGI app，预先注册也会被丢弃）;
4. 浏览器 `EventSource` 连 404 → 事件驱动前端失去推送 → 页面看起来"死"的。

**修复（entry.py）**：

- `launch(..., prevent_thread_lock=True)`，成功后 `_register_custom_routes(gui)`，
  再 `gui.block_thread()` 手动阻塞——与 `app.main()` 流程对齐；
- 附带修复：端口被旧实例占用时不再抛 `OSError` 崩溃，`_find_free_port()`
  自动顺延到下一个空闲端口并输出 WARNING（实测 7860 被占 → 7862 接管）；
- PyStand 每次启动会用 `script/_pystand_static.int` **重新生成**
  `script/build/pdf2zh.int`，因此把 spawn 守卫（F10）同步进了模板，
  否则打包应用会静默丢失 spawn 修复（该问题已二次复现）。

验证（真实入口 `pdf2zh.int -i` + 无头浏览器 E2E）：上传 PDF → 点击翻译 →
SSE 收到 24 个事件（`seq=24`）→ 状态 100% 完成呈现，浏览器 0 个 JS 报错。

| 项目 | 结果 |
|------|------|
| 全量 pytest（tests/） | **2069 passed / 1 skipped** ✅ |
| 新增测试 | test_gui_entry（_find_free_port、max_file_size ×4）、test_spawn_entry（+模板守卫） |


