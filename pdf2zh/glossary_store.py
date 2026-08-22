"""Module: glossary_store — 专业词表库（导入 / 导出 / 装载）。

词表 CSV 沿用 BabelDOC 上游事实标准（``babeldoc.glossary.Glossary.from_csv``）：

    source,target[,tgt_lng]

- ``tgt_lng`` 可选；存在时按目标语过滤（大小写与 ``-``/``_`` 归一后比较）；
- 编码探测优先 chardet（babeldoc 依赖，必然可用），失败回退 utf-8/gb18030。

词表库目录：``~/.config/PDFMathTranslate/glossaries/*.csv``（与 config.json
同根）。导入即"校验 + 规范化文件名拷贝入库"，导出即"从库拷出（可选转存
规范化 UTF-8 BOM）"。命令行管理入口：

    python -m pdf2zh.glossary_store import PATH... [--name NAME]
    python -m pdf2zh.glossary_store export NAME [DEST]
    python -m pdf2zh.glossary_store list
"""
from __future__ import annotations

import csv
import io
import re
import shutil
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STORE_SUBDIR = "glossaries"
#: 允许的词表库名称字符（防路径穿越；保留中英文/数字/空格/连字符/下标点）
_SAFE_NAME_RE = re.compile(r"[^\w.\- \u4e00-\u9fff]+", re.UNICODE)
_REQUIRED_COLUMNS = ("source", "target")


class GlossaryError(ValueError):
    """词表校验/装载失败（带可操作的错误信息）。"""


def store_dir() -> Path:
    """词表库目录（懒创建）。

    根目录与 :mod:`pdf2zh.config` 的 config.json 保持同一根
    （``~/.config/PDFMathTranslate``），不直接依赖 ConfigManager 单例，
    避免管理命令在无配置环境下的初始化副作用。
    """
    d = Path.home() / ".config" / "PDFMathTranslate" / STORE_SUBDIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _decode(raw: bytes) -> Tuple[str, str]:
    """bytes → (text, encoding_name)。chardet 探测优先，常见中文编码兜底。"""
    try:
        import chardet

        enc = (chardet.detect(raw) or {}).get("encoding") or "utf-8"
        return raw.decode(enc, errors="strict"), enc
    except Exception:  # noqa: BLE001 -- chardet 缺失或解码失败
        for cand in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                return raw.decode(cand), cand
            except UnicodeDecodeError:
                continue
        raise GlossaryError(
            "无法识别词表文件编码（已尝试 chardet/utf-8/gb18030）；"
            "请另存为 UTF-8 后重试"
        )


def parse_csv(path: Any) -> List[Dict[str, str]]:
    """解析词表 CSV → ``[{"source","target","tgt_lng"}, ...]``。

    校验：必需列 source/target；空行跳过；source/target 空白剔除、
    空 target 报错（行号定位）。
    """
    p = Path(path)
    if not p.is_file():
        raise GlossaryError(f"词表文件不存在: {p}")
    text, _enc = _decode(p.read_bytes())
    reader = csv.DictReader(io.StringIO(text, newline=""), doublequote=True)
    fields = [f.strip() for f in (reader.fieldnames or [])]
    missing = [c for c in _REQUIRED_COLUMNS if c not in fields]
    if missing:
        raise GlossaryError(
            f"词表 {p.name} 缺少必需列 {missing}（需表头 source,target[,tgt_lng]）"
        )
    entries: List[Dict[str, str]] = []
    for row in reader:  # line_num 始终是文件物理行号（表头=1）
        src = (row.get("source") or "").strip()
        tgt = (row.get("target") or "").strip()
        lng = (row.get("tgt_lng") or "").strip()
        if not src and not tgt:
            continue  # 纯空行
        if not src or not tgt:
            raise GlossaryError(
                f"词表 {p.name} 第 {reader.line_num} 行：source/target 不能为空"
            )
        entries.append({"source": src, "target": tgt, "tgt_lng": lng})
    if not entries:
        raise GlossaryError(f"词表 {p.name} 没有任何词条")
    return entries


def _normalize_tgt_lng(lang: str) -> str:
    return str(lang or "").strip().lower().replace("-", "_")


def filter_entries_for(entries: List[Dict[str, str]], lang_out: str
                       ) -> List[Dict[str, str]]:
    """按目标语过滤词条（与上游 from_csv 语义一致；空 tgt_lng 全语种生效）。"""
    want = _normalize_tgt_lng(lang_out)
    out = []
    for e in entries:
        lng = e.get("tgt_lng") or ""
        if not lng.strip() or _normalize_tgt_lng(lng) == want:
            out.append(e)
    return out


def safe_name(name: str) -> str:
    """把任意名称规整为安全的库内文件名（去路径分隔/控制符，空白折叠为下划线）。"""
    cleaned = unicodedata.normalize("NFC", str(name or "")).strip()
    cleaned = _SAFE_NAME_RE.sub("_", cleaned)
    cleaned = re.sub(r"\s+", "_", cleaned).strip("._")
    return cleaned[:80] or "glossary"


# ── 库操作 ────────────────────────────────────────────────────────────────────


def import_to_store(src: Any, name: Optional[str] = None,
                    overwrite: bool = True) -> Path:
    """校验并拷贝词表进库。返回库内路径。"""
    entries = parse_csv(src)  # 先校验再落库
    del entries
    base = safe_name(name or Path(src).stem)
    dest = store_dir() / f"{base}.csv"
    if dest.exists() and not overwrite:
        raise GlossaryError(f"词表已存在: {dest.name}（overwrite=False）")
    shutil.copyfile(str(src), str(dest))
    return dest


def export_from_store(name: str, dest: Any,
                      bom: bool = True) -> Path:
    """从库导出词表到 ``dest``（默认 UTF-8 BOM，Excel 直开不乱码）。"""
    src = store_dir() / f"{safe_name(name)}.csv"
    if not src.is_file():
        raise GlossaryError(f"词表不存在: {name}")
    entries = parse_csv(src)
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf, fieldnames=["source", "target", "tgt_lng"], doublequote=True,
    )
    writer.writeheader()
    for e in entries:
        writer.writerow({"source": e["source"], "target": e["target"],
                         "tgt_lng": e.get("tgt_lng") or ""})
    dest = Path(dest)
    if dest.parent and str(dest.parent):
        dest.parent.mkdir(parents=True, exist_ok=True)
    data = buf.getvalue()
    if bom and not data.startswith("\ufeff"):
        data = "\ufeff" + data
    dest.write_text(data, encoding="utf-8")
    return dest


def list_store() -> List[Dict[str, Any]]:
    """列出词表库：``[{"name","path","entries"}]``（损坏文件标注 error）。"""
    out = []
    for p in sorted(store_dir().glob("*.csv")):
        item: Dict[str, Any] = {
            "name": p.stem, "path": str(p), "entries": None,
        }
        try:
            item["entries"] = len(parse_csv(p))
        except Exception as exc:  # noqa: BLE001 -- 列表页不因单个坏文件中断
            item["error"] = str(exc)
        out.append(item)
    return out


def resolve_store_names(names: List[str]) -> List[str]:
    """把库内名称解析为绝对路径（供任务请求直接引用）。"""
    paths = []
    for n in names or []:
        p = store_dir() / f"{safe_name(n)}.csv"
        if not p.is_file():
            raise GlossaryError(f"词表不存在: {n}")
        paths.append(str(p))
    return paths


# ── 装载（babeldoc 双链共用） ─────────────────────────────────────────────────


def load_babeldoc_glossaries(paths: Optional[List[str]], lang_out: str) -> list:
    """CSV 路径列表 → babeldoc ``Glossary`` 对象列表（空输入返回 []）。

    先经 :func:`parse_csv` 预检（错误信息更友好），再交给上游
    ``Glossary.from_csv`` 构建 hyperscan 匹配结构。
    """
    paths = [str(p) for p in (paths or []) if str(p).strip()]
    if not paths:
        return []
    for p in paths:
        parse_csv(p)  # 预检：缺失列/坏行在此给出可读错误
    try:
        from babeldoc.glossary import Glossary
    except Exception as exc:  # noqa: BLE001 -- babeldoc 未安装
        raise GlossaryError(
            "BabelDOC 词表引擎不可用（babeldoc 未安装？）：glossary 仅在 "
            "babeldoc 解析链路生效"
        ) from exc
    return [
        Glossary.from_csv(Path(p), target_lang_out=lang_out)
        for p in paths
    ]


# ── CLI 管理入口 ──────────────────────────────────────────────────────────────


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(
        prog="python -m pdf2zh.glossary_store",
        description="pdf2zh 专业词表库管理（import/export/list）",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_imp = sub.add_parser("import", help="导入词表 CSV 进库")
    p_imp.add_argument("paths", nargs="+")
    p_imp.add_argument("--name", help="库内名称（缺省取文件名）")
    p_exp = sub.add_parser("export", help="导出词表")
    p_exp.add_argument("name")
    p_exp.add_argument("dest", nargs="?", default=None)
    sub.add_parser("list", help="列出词表库")

    args = parser.parse_args(argv)
    if args.cmd == "import":
        results = []
        for p in args.paths:
            dest = import_to_store(
                p, name=args.name if len(args.paths) == 1 else None,
            )
            results.append(str(dest))
        print(json.dumps(results, ensure_ascii=False))
    elif args.cmd == "export":
        dest = export_from_store(args.name, args.dest or f"{safe_name(args.name)}.csv")
        print(str(dest))
    else:
        print(json.dumps(list_store(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
