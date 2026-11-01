import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixtures" / "agent-1.4.json"


@pytest.fixture
def agent_json():
    """last30days-skill の agent JSON（schema_version 1.4）の必須構造を保つ最小の記録（自作）。"""
    return json.loads(FIXTURE.read_text())
