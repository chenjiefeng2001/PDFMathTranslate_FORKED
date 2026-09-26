"""pytest conftest — 注册 golden helpers 中的 fixtures。"""

from tests.golden_helpers import (  # noqa: F401
    golden_artifact,
    golden_expected,
    golden_fixture_name,
    golden_input_pdf,
)
