from __future__ import annotations

import pytest

from shiyuan import ocr


@pytest.fixture(autouse=True)
def _no_rapidocr(monkeypatch):
    """默认测试走 Windows OCR；需要测 RapidOCR 时在用例内覆盖为 True。"""
    monkeypatch.setattr(ocr, "_RAPID_AVAILABLE", False)
