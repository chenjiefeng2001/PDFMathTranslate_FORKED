"""BenchmarkDataset — 测试数据集定义。

定义不同类型的 PDF 测试数据集。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class BenchmarkDataset:
    """测试数据集。"""

    name: str = ""
    pdf_path: Optional[Path] = None
    pages: int = 0
    file_size_mb: float = 0.0
    characteristics: list[str] = field(default_factory=list)

    @classmethod
    def from_path(cls, path: str | Path, name: str = "") -> BenchmarkDataset:
        """从文件路径创建。"""
        path = Path(path)
        return cls(
            name=name or path.stem,
            pdf_path=path,
            pages=0,  # 需要 pikepdf 解析
            file_size_mb=path.stat().st_size / 1024 / 1024,
            characteristics=[],
        )


@dataclass
class BenchmarkSuite:
    """测试套件。"""

    datasets: list[BenchmarkDataset] = field(default_factory=list)

    def add(self, dataset: BenchmarkDataset) -> None:
        self.datasets.append(dataset)

    def find_by_name(self, name: str) -> Optional[BenchmarkDataset]:
        for d in self.datasets:
            if d.name == name:
                return d
        return None


def default_suite() -> BenchmarkSuite:
    """默认测试套件。"""
    suite = BenchmarkSuite()

    # 检查现有测试文件
    test_dir = Path(__file__).parent.parent / "tests" / "file"

    for pdf in test_dir.glob("*.pdf"):
        pdf_path = pdf
        file_size = pdf.stat().st_size / 1024 / 1024

        # 特征检测
        chars = []
        if file_size > 10:
            chars.append("large")
        if "itbook" in pdf.name.lower():
            chars.extend(["technical_book", "toc", "730pages"])

        # 页数估计
        if "itbook" in pdf.name.lower():
            pages = 730
        elif "rust" in pdf.name.lower():
            pages = 681
        elif "2104" in pdf.name:
            pages = 160
        elif "2407" in pdf.name:
            pages = 333
        else:
            pages = 100

        suite.add(
            BenchmarkDataset(
                name=pdf.stem,
                pdf_path=pdf_path,
                pages=pages,
                file_size_mb=file_size,
                characteristics=chars,
            )
        )

    return suite
