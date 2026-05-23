"""Phase 0 smoke test: package imports and version is exposed."""

import claims


def test_package_imports() -> None:
    assert claims.__version__ == "0.1.0"
