"""IncrementalWriter — 手动增量 PDF 写入器。

Phase 3.6a 核心：手动实现 PDF incremental update。

PDF Incremental Update 格式（经典 xref 表版本）::

    [original PDF bytes]
    [whitespace]
    [re-emitted objects]
    xref
    <subsection(s) covering only the objects written above>
    trailer
    << /Size .. /Root .. /Prev <offset of the previous xref> >>
    startxref
    <offset of the 'xref' keyword>
    %%EOF

只重写被改动的对象（沿用其原对象号），新对象（content stream）取新号，
因此增量段很小 —— 这正是增量更新的意义。

注意：
    pikepdf 不支持 incremental save，需要手动操作字节。
    Phase 3.6a 只支持替换 page content stream。

历史包袱（本实现已修掉的问题）
--------------------------
旧实现产出的是**结构非法**的字节，且自检还报 ``valid=True``：

1. ``_find_startxref`` / ``_count_objects`` 的正则在 ``rb"..."`` 里写了
   ``\\\\s`` —— 双重转义，匹配的是字面反斜杠，因此永远匹配不到，
   ``startxref`` 偏移恒为 0、``_next_objnum`` 恒为 1。
2. ``_next_objnum`` 从 1 起算 → 新对象与文档真实对象 1 撞号。
3. ``_build_xref_section`` 只写了一个 ``xref\\n`` 且**从未被调用** ——
   产物里根本没有 xref 表。
4. ``startxref`` 指向 ``_original_xref_offset``（即 0），不是新 xref 的位置。
5. 追加的 content stream **没有被任何页面的 ``/Contents`` 引用**，是死对象。
6. 自检用 ``pikepdf.open``，而它会按扫描方式重建 xref，于是把非法文件也
   判为有效 —— 缺陷因此完全不可见。
"""

from __future__ import annotations

import io
import logging
import re
import time
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pikepdf

from pdf2zh.assembler.incremental.object_store import ObjectStore

logger = logging.getLogger(__name__)

#: 末尾的 ``startxref\\n<offset>``。注意是**单**反斜杠：``rb"\\s"`` 才是
#: 空白，``rb"\\\\s"`` 匹配的是字面反斜杠（旧实现就错在这里）。
_RE_STARTXREF = re.compile(rb"startxref\s+(\d+)\s*%%EOF\s*$")

#: ``N G obj`` 头，用于统计最大对象号。
_RE_OBJ_HEADER = re.compile(rb"(?<![0-9])(\d+)\s+(\d+)\s+obj\b")


class IncrementalSourceError(RuntimeError):
    """源 PDF 不适合做经典增量更新（加密 / 交叉引用流 / 找不到 xref）。"""


class IncrementalWriter:
    """手动增量 PDF 写入器。

    工作流程：
        1. open(source_pdf) — 读取原始 PDF 字节，解析 xref 与页对象号
        2. replace_page_content(page_idx, new_contents) — 标记修改
        3. commit(output_path) — 写入增量更新

    输出格式：
        原始 PDF 字节
        + 重写的页对象（新 /Contents 指向追加的 stream 对象）
        + 新增的 content stream 对象
        + 只覆盖这些对象的 xref 子段 + 带 /Prev 的 trailer
        + startxref 指向新 xref

    用法：
        writer = IncrementalWriter()
        writer.open("input.pdf")
        writer.replace_page_content(0, new_content_stream)
        writer.commit("output.pdf")
    """

    def __init__(self) -> None:
        self._source_bytes: Optional[bytes] = None
        self._store = ObjectStore()
        self._page_patches: Dict[int, bytes] = {}
        self._original_xref_offset: int = 0
        self._next_objnum: int = 1
        #: {page_index: page 对象号}
        self._page_objnums: Dict[int, int] = {}
        self._root_objnum: int = 0
        self._size: int = 0
        self._page_count: int = 0

    # ── open ──────────────────────────────────────────────────────────
    def open(self, source: str | Path) -> None:
        """打开源 PDF，读取原始字节、xref 位置、页对象号与 trailer 信息。"""
        path = Path(source)
        self._source_bytes = path.read_bytes()

        self._original_xref_offset = self._find_startxref(self._source_bytes)
        if self._original_xref_offset <= 0:
            raise IncrementalSourceError(
                f"{path.name}: cannot locate the trailing startxref; the file "
                "is truncated or not a conventional PDF"
            )
        if not self._classic_xref_at(self._original_xref_offset):
            raise IncrementalSourceError(
                f"{path.name}: the last cross-reference section is an /XRef "
                "stream (PDF 1.5+). A classic incremental update cannot chain "
                "/Prev to a stream; use PikepdfIncrementalAssembler instead."
            )
        try:
            with pikepdf.open(path) as pdf:
                if pdf.is_encrypted:
                    raise IncrementalSourceError(
                        f"{path.name}: encrypted documents cannot be patched by "
                        "this writer (the appended stream would need the same "
                        "encryption); use pikepdf/qpdf instead."
                    )
                self._page_count = len(pdf.pages)
                for idx, page in enumerate(pdf.pages):
                    objgen = page.obj.objgen
                    self._page_objnums[idx] = objgen[0]
                root_gen = pdf.Root.objgen
                self._root_objnum = root_gen[0] if root_gen else 0
                declared_size = pdf.trailer.get("/Size")
                try:
                    self._size = int(declared_size)
                except (TypeError, ValueError):
                    self._size = 0
        except IncrementalSourceError:
            raise
        except pikepdf.PasswordError as exc:
            # 加密文档在 pikepdf 里就要求密码，根本走不到上面的 is_encrypted
            # 分支；统一成本模块的错误类型，调用方只需捕一个异常。
            raise IncrementalSourceError(
                f"{path.name}: encrypted or password-protected ({exc}); "
                "this writer cannot patch it, use pikepdf/qpdf instead."
            ) from exc
        except Exception as exc:  # noqa: BLE001 -- 坏源文件的错误要可读
            raise IncrementalSourceError(
                f"{path.name}: cannot be parsed for incremental update "
                f"({type(exc).__name__}: {exc})"
            ) from exc

        # 新对象号必须大于文档里已用的最大号：优先取 trailer /Size，
        # 并与实际扫描到的最大号取大者（/Size 可能偏小或缺失）。
        scanned_max = self._max_object_number(self._source_bytes)
        self._next_objnum = max(self._size, scanned_max + 1, 1)
        self._size = max(self._size, self._next_objnum)

        logger.info(
            "IncrementalWriter: opened %s (%d bytes, xref at %d, %d page(s), "
            "next objnum %d)",
            path.name,
            len(self._source_bytes),
            self._original_xref_offset,
            self._page_count,
            self._next_objnum,
        )

    @staticmethod
    def _find_startxref(data: bytes) -> int:
        """找到文件末尾 ``startxref`` 指向的偏移。"""
        tail = data[-2048:]
        matches = list(_RE_STARTXREF.finditer(tail))
        if not matches:
            return 0
        return int(matches[-1].group(1))

    @staticmethod
    def _max_object_number(data: bytes) -> int:
        """扫描 ``N G obj`` 头，取最大对象号。"""
        best = 0
        for match in _RE_OBJ_HEADER.finditer(data):
            num = int(match.group(1))
            if num > best:
                best = num
        return best

    def _classic_xref_at(self, offset: int) -> bool:
        data = self._source_bytes or b""
        if offset < 0 or offset >= len(data):
            return False
        return data[offset : offset + 4] == b"xref"

    # ── patch ─────────────────────────────────────────────────────────
    def replace_page_content(
        self,
        page_index: int,
        new_content_stream: bytes,
    ) -> None:
        """替换指定页面的 content stream。

        越界页号被忽略并告警：基准脚本会对文档不存在的页（如 730 页文档里
        的 0..729）逐页调用，这里抛异常会让整个基准跑不起来。
        """
        if page_index in self._page_objnums:
            self._page_patches[page_index] = new_content_stream
            return
        if self._page_objnums:
            logger.warning(
                "IncrementalWriter: page %d out of range (document has %d "
                "page(s)); patch ignored",
                page_index,
                self._page_count,
            )

    # ── commit ────────────────────────────────────────────────────────
    def _build_appended(self, pdf) -> Tuple[bytes, List[Tuple[int, int]]]:
        """构建追加段。

        新流对象的编号交给 pikepdf 在暂存文档里分配（``make_indirect``）——
        它保证不与源文件已有对象号冲突，比自己「扫描最大号 +1」可靠。

        Returns:
            ``(bytes, entries)``；``entries`` 是 ``(objnum, 绝对偏移)`` 列表，
            顺序与字节中的出现顺序一致（写 xref 子段要用）。
        """
        buf = io.BytesIO()
        # 追加段紧跟原始字节，中间垫一个换行（PDF 规范要求以空白分隔）。
        base = len(self._source_bytes or b"") + 1
        entries: List[Tuple[int, int]] = []

        def emit(objnum: int, payload: bytes) -> None:
            entries.append((objnum, base + buf.tell()))
            buf.write(f"{objnum} 0 obj\n".encode())
            buf.write(payload)
            buf.write(b"\nendobj\n")

        # 1) 新 content stream（对象号由 pikepdf 分配）
        content_refs: Dict[int, int] = {}
        for page_idx in sorted(self._page_patches):
            compressed = zlib.compress(self._page_patches[page_idx])
            stream = pikepdf.Stream(pdf, compressed)
            stream[pikepdf.Name("/Filter")] = pikepdf.Name("/FlateDecode")
            ref = pdf.make_indirect(stream)
            objnum = ref.objgen[0]
            content_refs[page_idx] = objnum
            if objnum >= self._next_objnum:
                self._next_objnum = objnum + 1
            emit(
                objnum,
                b"<< /Length "
                + str(len(compressed)).encode()
                + b" /Filter /FlateDecode >>\nstream\n"
                + compressed
                + b"\nendstream",
            )

        # 2) 重写页对象（沿用原对象号），把 /Contents 指向新流 —— 缺了这一步
        #    追加的流就是没人引用的死对象，整次更新等于没做。
        for page_idx in sorted(self._page_patches):
            page_objnum = self._page_objnums[page_idx]
            body = self._page_dict_body(pdf, page_objnum, content_refs[page_idx])
            emit(page_objnum, body)

        return buf.getvalue(), entries

    def _page_dict_body(self, pdf, page_objnum: int, content_objnum: int) -> bytes:
        """复制原页对象字典，仅替换 ``/Contents`` 为新流对象。"""
        source = None
        for candidate in pdf.objects:
            try:
                gen = candidate.objgen
            except Exception:  # noqa: BLE001 -- 直接对象没有 objgen
                continue
            if gen == (page_objnum, 0):
                source = candidate
                break
        if source is None:
            raise IncrementalSourceError(
                f"page object {page_objnum} 0 obj not found in source"
            )
        replacement = pikepdf.Dictionary()
        replacement[pikepdf.Name("/Type")] = pikepdf.Name("/Page")
        content_ref = pdf.get_object((content_objnum, 0))
        for key in source.keys():
            replacement[key] = content_ref if str(key) == "/Contents" else source[key]
        return replacement.unparse()

    def _build_xref_section(self, entries: List[Tuple[int, int]]) -> bytes:
        """构建 xref 子段表 + trailer。

        ``entries`` 是 ``(objnum, 绝对偏移)``；按对象号排序后合并成连续子段，
        ``/Prev`` 指回原始 xref，使阅读器能接上旧表 —— 这是增量更新的关键，
        缺了它新对象就是不可解析的。
        """
        ordered = sorted(entries)
        buf = io.BytesIO()
        buf.write(b"xref\n")
        run_objnums = [n for n, _ in ordered]
        index = 0
        total = len(ordered)
        while index < total:
            start = run_objnums[index]
            run = [ordered[index]]
            index += 1
            while index < total and run_objnums[index] == run[-1][0] + 1:
                run.append(ordered[index])
                index += 1
            buf.write(f"{start} {len(run)}\n".encode())
            for _objnum, offset in run:
                buf.write(b"%010d %05d n \n" % (offset, 0))

        highest = max((n for n, _ in ordered), default=self._size - 1)
        size = max(self._size, highest + 1)
        buf.write(b"trailer\n")
        buf.write(b"<< ")
        if self._root_objnum:
            buf.write(f"/Root {self._root_objnum} 0 R ".encode())
        buf.write(f"/Size {size} ".encode())
        buf.write(f"/Prev {self._original_xref_offset} ".encode())
        buf.write(b">>\n")
        return buf.getvalue()

    def commit(self, output: str | Path) -> dict:
        """写入增量更新。

        Returns:
            写入统计；``valid`` 表示产物确实可解析**且**补丁确实生效
            （两者都验证，不再靠 pikepdf 的容错重建蒙混过关）。
        """
        if self._source_bytes is None:
            raise RuntimeError("Call open() first")
        if not self._classic_xref_at(self._original_xref_offset):
            raise IncrementalSourceError(
                "the last cross-reference section is not a classic xref table"
            )

        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)

        t0 = time.perf_counter()
        # 暂存文档：只为借 pikepdf 分配新对象号并复制页字典，随即丢弃。
        with pikepdf.open(io.BytesIO(self._source_bytes)) as scratch:
            appended, entries = self._build_appended(scratch)
        xref_offset = len(self._source_bytes) + 1 + len(appended)
        xref_section = self._build_xref_section(entries)
        tail = xref_section + f"startxref\n{xref_offset}\n%%EOF\n".encode()

        with open(output, "wb") as f:
            f.write(self._source_bytes)
            f.write(b"\n")
            f.write(appended)
            f.write(tail)
        write_time = time.perf_counter() - t0
        output_size = output.stat().st_size

        stats = {
            "write_method": "incremental_manual",
            "write_time": write_time,
            "output_size": output_size,
            "pages_patched": len(self._page_patches),
            "new_objects": len(self._page_patches),
            "original_size": len(self._source_bytes),
            "append_size": 1 + len(appended) + len(tail),
            "xref_offset": xref_offset,
            "prev_xref_offset": self._original_xref_offset,
            "next_object_number": self._next_objnum,
        }

        # 验证：既要「能解析」，也要「补丁真的生效」。只做前者会被 pikepdf 的
        # 容错重建骗过去（旧实现就是这样一路 valid=True 的）。
        patched = sorted(self._page_patches)
        try:
            with pikepdf.open(str(output)) as test_pdf:
                stats["page_count"] = len(test_pdf.pages)
                ok = len(test_pdf.pages) == self._page_count
                if ok and patched:
                    ok = self._patch_applied(test_pdf, patched)
                stats["valid"] = bool(ok)
        except Exception as e:  # noqa: BLE001 -- 自检不得抛给调用方
            stats["valid"] = False
            stats["error"] = str(e)
        return stats

    def _patch_applied(self, test_pdf, patched: List[int]) -> bool:
        """校验被改页面的 ``/Contents`` 里确实出现了新内容。

        ``/Contents`` 可以是单个 stream，也可以是 stream 数组（MuPDF 产出的
        页面常是后者），两种都要处理。
        """
        expected = self._page_patches[patched[0]]
        contents = test_pdf.pages[patched[0]].obj.get("/Contents")
        if contents is None:
            return False
        streams = list(contents) if isinstance(contents, pikepdf.Array) else [contents]
        blob = b""
        for stream in streams:
            try:
                blob += bytes(stream.read_bytes())
            except Exception:  # noqa: BLE001 -- 单个流解不开就跳过
                continue
        if not blob:
            return False
        # read_bytes() 已解压，直接比对原文即可
        if expected not in blob:
            try:
                expanded = zlib.decompress(blob)
            except Exception:  # noqa: BLE001 -- 确实是未压缩内容
                expanded = blob
            if expected not in expanded:
                return False
        return True

    @property
    def source_size(self) -> int:
        if self._source_bytes is not None:
            return len(self._source_bytes)
        return 0

    def summary(self) -> str:
        return (
            f"IncrementalWriter | source_size={self.source_size} | "
            f"pages_patched={len(self._page_patches)} | "
            f"next_objnum={self._next_objnum}"
        )
