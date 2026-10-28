import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

import pledge_parse  # noqa: E402


@pytest.fixture(scope="session")
def doc():
    cache = {}

    def load(rcept_no):
        if rcept_no not in cache:
            cache[rcept_no] = pledge_parse.parse_document((FIXTURES / f"{rcept_no}.xml").read_text(encoding="utf-8"))
        return cache[rcept_no]
    return load
