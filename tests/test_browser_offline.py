"""Run in the sandbox-capable container, with its network disabled."""
import importlib.util
import os
from pathlib import Path
import asyncio

import pytest


@pytest.mark.skipif(os.environ.get('TAOBAO_OFFLINE_BROWSER') != '1', reason='container sandbox required')
def test_offline_browser():
    path = Path(__file__).resolve().parents[1] / 'scripts/offline_check.py'
    spec = importlib.util.spec_from_file_location('offline_check', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert asyncio.run(module.check())['offline_browser'] == 'passed'
