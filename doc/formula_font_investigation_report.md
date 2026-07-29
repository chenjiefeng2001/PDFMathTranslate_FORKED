# PDF2ZH 数学公式字体处理与排版重叠问题调查报告

> 日期：2026-07-28 | 分析范围：converter.py, high_level.py, pdf2zh.py, font_resolver.py

---

## 一、问题总览

根据用户反馈和日志分析，当前 pdf2zh 在处理学术/数学 PDF 时存在三大核心问题：

| # | 问题 | 严重性 | 现象 |
|---|------|--------|------|
| P1 | **公式字体识别失效** | 🔴 致命 | 数学符号被当作普通文字送去翻译/重绘，坐标失真 |
| P2 | **字体子集化导致宽度偏移** | 🔴 致命 | 特殊数学符号占位宽高计算为 0，文字向前覆盖重叠 |
| P3 | **中英文叠加显示** | 🔴 致命 | 译文中文和原文英文同时渲染在同一位置，排版完全混乱 |
| P4 | **skip_subset_fonts 未完全贯通** | 🟡 中等 | CLI 参数存在但 translate_patch 中未使用 |

---

## 二、详细分析

### P1: 公式字体保护机制不足

#### 2.1 当前实现

公式字体判定实现在 `converter.py` 的 `vflag()` 闭包函数中（约 L217-L246）：

```python
def vflag(font: str, char: str):
    if isinstance(font, bytes):
        try:
            font = font.decode('utf-8')
        except UnicodeDecodeError:
            font = ""
    font = font.split("+")[-1]
    if re.match(r"\(cid:", char):
        return True
    if self.vfont:
        if re.match(self.vfont, font):
            return True
    else:
        if re.match(
            r"(CM[^R]|MS.M|XY|MT|BL|RM|EU|LA|RS|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy|stmary|.*Mono|.*Code|.*Ital|.*Sym|.*Math)",
            font,
        ):
            return True
    if self.vchar:
        if re.match(self.vchar, char):
            return True
    else:
        if (char and char != " "
            and (unicodedata.category(char[0]) in ["Lm","Mn","Sk","Sm","Zl","Zp","Zs"]
                 or ord(char[0]) in range(0x370, 0x400))):
            return True
    return False
```

#### 2.2 发现问题

**问题 1：默认公式字体正则遗漏大量 LaTeX 变体**

当前默认正则：
```
(CM[^R]|MS.M|XY|MT|BL|RM|EU|LA|RS|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy|stmary|.*Mono|.*Code|.*Ital|.*Sym|.*Math)
```

缺少以下常见的 Springer/Elsevier 数学字体变体：

| 遗漏的字体模式 | 常见来源 | 示例 |
|----------------|----------|------|
| `EUFM*` (Euler Fraktur) | AMS 数学包 | `EUFM10`, `EUFM7` |
| `MSBM*` (Math Symbol Bold) | AMS 扩展 | `MSBM10` |
| `CMSY*` (Computer Modern Symbol) | LaTeX 默认 | `CMSY10` |
| `CMEX*` (Computer Modern Extension) | LaTeX 大符号 | `CMEX10` |
| `CMMI*` (Computer Modern Math Italic) | LaTeX 数学斜体 | `CMMI10`, `CMMI7` |
| `S*` (Special/Stmaryrd) | St Mary's Road 符号包 | `S3`, `S5` |
| `STIX*` | STIX 数学字体 | `STIX*Math` |

### P2: 字体子集化 (Subsetting) 导致宽度偏移

#### 2.2.1 当前实现

在 `high_level.py` 的 `translate_stream()` 中（约 L549-L561）：

```python
if not skip_subset_fonts:
    try:
        doc_zh.subset_fonts(fallback=True)
    except Exception as subset_err:
        logger.warning("subset_fonts failed for doc_zh: %s", str(subset_err)[:120])
    try:
        doc_en.subset_fonts(fallback=True)
    except Exception as subset_err:
        logger.warning("subset_fonts failed for doc_en: %s", str(subset_err)[:120])
```

#### 2.2.2 发现问题

**问题 1：`skip_subset_fonts` 参数未传递到 `translate_patch`**

`translate_patch()` 函数的签名中有 `**kwarg: Any` 捕捉额外参数，但 `skip_subset_fonts` 在 `translate_patch` 内部没有被使用。该参数仅在 `translate_stream` 中被使用（对 `subset_fonts()` 的调用）。

**问题 2：`subset_fonts(fallback=True)` 对数学字体有破坏性**

MuPDF 的 `subset_fonts(fallback=True)` 在子集化时会对字体进行重编码。对于数学符号字体：
- MuPDF 可能无法正确解析 Type 3 字体的 glyph metrics
- 当 char_width 计算失败时，glyph 宽度被设为 0
- 宽度为 0 的字符在排版时不会向前移动光标，导致后续文字覆盖在前一文字之上
- 这种现象在 CJK 字体与数学符号混排时尤为明显

**问题 3：`fallback=True` 可能引入多余字体**

`fallback=True` 参数会为文档中所有使用的字符添加 fallback 字体。当文档包含大量 Unicode 数学符号时，可能导致：
- MuPDF 会尝试为每个符号添加 fallback 字体
- 多种数学字体的 fallback 链可能导致 glyph 选择混乱
- 最终渲染时符号可能来自错误字体，尺寸和位置都不正确

| `XITS*` | XITS 数学字体 | `XITSMath*` |
| `Cambria Math` | Office 数学 | |
| `Asana Math` | 开源数学字体 | |
| `Latin Modern Math` | 变体 | `LMMath*` |
| `MnSymbol*` | Alternative symbols | |
| `bb*` (blackboard bold) | 多种来源 | `bb10`, `bbold*` |
| `cal*` / `mathcal*` | 手写体 | `cal10`, `cmsy*` |
| `frak*` / `mathfrak*` | Fraktur 字体 | `frak10` |

**问题 2：字体名截断策略过于简单**

`font.split("+")[-1]` 只去除了前缀。实际 PDF 中字体名可能是：
- `/KJL+EUFM10` → 截断为 `EUFM10` ✅
- `/ABCDEF+CMMI10+Something` → 截断为 `Something` ❌（丢失关键信息）
- 更稳健的做法应当是：如果 font 包含 `+`，取 `+` 之后的部分直到遇到非字母数字字符为止。

**问题 3：角标检测阈值写死**

```python
or (cls == xt_cls and len(sstk[-1].strip()) > 1 and child.size < pstk[-1].size * 0.79)
```


### P3: 中英文叠加显示（排版混乱）

#### 2.3.1 当前实现

段落排版发生在 `converter.py` 的 `receive_layout()` 方法中。核心排版循环处理两类内容：
1. 普通文本 → 直接渲染为 PDF 文字指令
2. 公式内容 → 以 `{v0}`, `{v1}` 占位符嵌入文本，翻译后逐字符还原

#### 2.3.2 发现问题

**问题 1：公式占位符破坏翻译结构**

原文本类似：
```
"Let {v0} be a random variable where {v1} > 0"
```

翻译引擎可能将占位符当作普通文本处理，导致：
- 移动占位符位置
- 删除或复制占位符
- 在占位符内部插入空格

最终 `{v0}` 无法被正确还原为公式字符，公式字符被当作普通文字位置渲染。

**问题 2：行内公式宽度计算不一致**

公式宽度在 `vlen` 数组中预先计算：
```python
l = max([vch.x1 for vch in v]) - v[0].x0
```

但翻译后渲染时，公式字符的定位依赖于原始布局坐标。当原文段落换行位置与翻译后不同时：
- 公式的绝对坐标 (x0, y0) 是基于原文布局确定的
- 翻译后的文本长度变化导致段落重排
- 公式字符依然使用原始坐标，与周围文本错位

**问题 3：行高与基线对齐**

CJK 字体（如 SourceHanSerifCN）和西文字体（如原文档的 Times New Roman）的基线高度、ascent/descent 不同。当前行高计算（约 L574-L586）：

```python
line_height = default_line_height
tm_line = self.text_metrics.get(fcur) if self.text_metrics else None
if tm_line:
    ascent = getattr(tm_line, 'ascent', 0.8)
    descent = getattr(tm_line, 'descent', -0.2)
    line_height = max(ascent - descent, 1.0)
```

这里只考虑了字体本身的度量，但没有考虑：
- CJK 和西文字体混排时的基线对齐
- 翻译后段落高度膨胀导致的跨页重叠
- 不受 CollisionResolver 保护时直接堆叠

### P4: `skip_subset_fonts` 参数贯通性问题

#### 2.4.1 参数传递链路

## 三、核心技术栈分析

```
┌─────────────────────────────────────────────────────────────┐
│  PDF2ZH 公式/排版流水线                                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. PDFMiner 解析原始 PDF                                     │
│     ↓                                                        │
│  2. ONNX Layout Detection（段落/公式/表格区域分类）             │
│     ↓                                                        │
│  3. vflag() 公式字体识别（converter.py）                      │
│     ↓                                                        │
│  4. 原文提取 → 公式占位符替换 → 翻译                          │
│     ↓                                                        │
│  5. 段落排版（converter.py receive_layout）                   │
│     ↓                                                        │
│  6. MuPDF 文档写入 + subset_fonts()                           │
│     ↓                                                        │
│  7. 输出 mono/dual PDF                                        │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

关键问题出现在 **步骤 3、5、6**：

| 步骤 | 问题描述 |
|------|----------|
| 3 (vflag) | 公式字体正则覆盖不全 |
| 5 (排版) | 公式坐标基于原文，翻译后错位 + CJK/西文基线不兼容 |
| 6 (subset) | MuPDF 子集化破坏数学字体的 glyph 宽度 |

## 四、修复建议

### P1 修复：增强公式字体保护

**4.1.1 扩展默认 vfont 正则**

将默认公式字体正则更新为覆盖常见 Springer/Elsevier/AMS/LaTeX 字体：

```python
r"(CM[^R]|MS[BM]|XY|MT|BL|RM|EU[FM]|LA|RS|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy|stmary|"
r".*Mono|.*Code|.*Ital|.*Sym|.*Math|EUFM|MSBM|CMSY|CMEX|CMMI|S[0-9]|"
r"STIX.*Math|XITS.*Math|Cambria Math|Asana Math|LMMath|MnSymbol|"
r"bb[0-9]?|bbold|cal[0-9]?|frak[0-9]?|mathscr)"
```

**4.1.2 改进字体名截断**

```python
def _extract_font_name(font: str) -> str:
    """从 PDF 字体引用中提取规范字体名"""
    if isinstance(font, bytes):
        try:
            font = font.decode('utf-8')
        except UnicodeDecodeError:
            return ""
    # 处理 /ABCDEF+CMMI10 格式
    if "+" in font:
        font = font.split("+")[-1]
    # 提取纯字体名，去除数字后缀（如 CMMI10 → CMMI）
    return re.sub(r"\d+$", "", font)
```

### P2 修复：子集化保护

**4.2.1 将 skip_subset_fonts 传入 translate_patch**

在 `translate_patch` 函数签名中显式添加 `skip_subset_fonts: bool = False` 参数。

**4.2.2 数学字体豁免子集化**

在调用 `subset_fonts()` 之前，检测并保护已知数学字体：

```python
def _protect_math_fonts(doc):
    """保护数学字体不被 MuPDF subset_fonts 破坏"""
    xreflen = doc.xref_length()
    for xref in range(1, xreflen):
        try:
            font_type = doc.xref_get_key(xref, "/Subtype")
            if font_type[0] == "name" and "Type3" in font_type[1]:
                # Type3 字体跳过子集化
                ...
        except Exception:
            pass
```

### P3 修复：排版稳定性

**4.3.1 公式占位符保护**

翻译前使用 XML 风格标记替代 `{v0}` 占位符，翻译后精确还原：

```
原文: "Let α + β be a sum"
替换: "Let <math idx='0'>α + β</math> be a sum"
翻译: "设 <math idx='0'>α + β</math> 是一个和"
还原: "设 {v0} 是一个和"
```

**4.3.2 CJK/西文混排行高兼容**

```python
def _has_cjk(text: str) -> bool:
    return any('\u4e00' <= c <= '\u9fff' or '\u3000' <= c <= '\u303f' for c in text)

if _has_cjk(translated_text):
    line_height = max(default_line_height, 1.3)  # CJK 需要更大的行高
```

## 五、完整架构建议

```text
当前缺少的组件：
1. MathFontRegistry - 管理已知数学字体列表和保护策略
2. FontSubsetProtector - 控制子集化时的字体豁免
3. BidiLayoutEngine - 处理 CJK/西文基线混合
4. ParagraphLayoutEngine（已规划 2.0） - 从源头减少行高膨胀
```

### 优先级建议

| 优先级 | 任务 | 预计改动量 | 影响范围 |
|--------|------|-----------|----------|
| P0 | 扩展 vfont 默认正则 + 修复字体名截断 | ~20 行 | 所有学术 PDF |
| P0 | 添加 `skip_subset_fonts` 显式传递 | ~10 行 | CLI 用户 |
| P1 | 实现 `_protect_math_fonts()` 豁免函数 | ~30 行 | subset 行为 |
| P1 | 改善公式占位符保护策略 | ~40 行 | 翻译质量 |
| P2 | CJK/西文混排行高兼容 | ~25 行 | 中文翻译 |
| P3 | 数学字体注册表 MathFontRegistry | ~80 行 | 长期维护 |

## 六、测试建议

1. **vfont 正则测试**: 包含已知 Springer/Elsevier 数学字体样本的单元测试
2. **subset 保护测试**: 验证 subset_fonts 后数学符号宽度不为 0
3. **翻译占位符测试**: 验证 `{v0}` 在翻译后位置不变
4. **行高计算测试**: CJK/西文混排时行高的确定性计算
5. **集成测试**: 使用真实学术 PDF（如 arXiv 论文）验证完整流程

## 附录：已知典型失败 PDF 样式

| PDF 类型 | 字体特征 | 预期故障模式 |
|----------|----------|--------------|
| Springer 数学教材 | `KJL+EUFM10`, `XYZ+MSBM10`, `ABCD+CMMI10` | 公式未保护 → 误翻译 → 位置错误 |
| Elsevier 论文 | `STIX*Math`, `XITS*Math` | 公式未识别 → 子集化破坏 |
| AMS 论文 | `CMSY10`, `CMEX10`, `MSAM10` | 大符号（∑, ∫）宽度为 0 → 重叠 |
| IEEE 会议 | `EUFM10`, `bb10`, `cal10` | 黑体/花体字母误识别 |


```
CLI (pdf2zh.py) → TranslateRequest → kernel/legacy.py
  → high_level.translate() → translate_stream() → translate_patch()
```

发现：
1. `pdf2zh.py`: ✅ 正确解析 `--skip-subset-fonts`
2. `TranslateRequest`: ✅ 包含 `skip_subset_fonts` 字段
3. `kernel/legacy.py`: ⚠️ 构建 kwargs 时传递
4. `translate_stream`: ✅ 签名包含 `skip_subset_fonts`，用于控制 subset
5. `translate_patch`: ❌ 签名使用 `**kwarg: Any` 接收，但内部未使用

0.79 的阈值来源于对常规角标的经验统计。但在 Springer 数学书中：
- 大写角标可能达到 0.82-0.85
- 首字母放大的段落后继文字可能降到 0.75-0.80
- 导致部分角标未正确识别
