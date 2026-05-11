from __future__ import annotations

import pytest

from nexus.models import ToolExecutionContext


@pytest.fixture
def tool_context(tmp_path):
    return ToolExecutionContext(session_id="test-session", working_directory=tmp_path)
