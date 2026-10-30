import json
from pathlib import Path

import pytest

from jms_mcp.config import Settings


@pytest.fixture
def document():
    return json.loads(
        (Path(__file__).parent / "fixtures/jumpserver-3.10.15-minimal.json").read_text()
    )


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        jumpserver_url="https://jumpserver.example",
        api_token="upstream-test-token",
        api_key="inbound-test-key",
    )
