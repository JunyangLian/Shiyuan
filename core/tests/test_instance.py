from __future__ import annotations

import os

import pytest

from shiyuan import instance

pytestmark = pytest.mark.skipif(os.name != "nt", reason="命名互斥量仅在 Windows 生效")


def test_http_instance_guard_is_idempotent_in_same_process():
    port = 20000 + (os.getpid() % 10000)
    assert instance.acquire_http_instance(port) is True
    # sidecar 与 shiyuan.__main__ 可能先后调用，同进程内必须放行。
    assert instance.acquire_http_instance(port) is True
