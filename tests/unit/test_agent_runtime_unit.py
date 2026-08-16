from __future__ import annotations

import pytest

from platform_core.agent.tools import ToolRegistry, ToolSpec
from platform_core.identity.capabilities import Capability

pytestmark = pytest.mark.unit


def _handler(_ctx, arguments):
    return arguments


def test_write_tools_cannot_bypass_the_approval_gate() -> None:
    with pytest.raises(ValueError, match="must require approval"):
        ToolSpec(
            name="test.write",
            description="unsafe registration",
            side_effect="write",
            capability=Capability.TOOL_INVOKE_WRITE,
            handler=_handler,
        )


def test_registry_rejects_duplicate_and_invalid_tool_names() -> None:
    tools = ToolRegistry()
    spec = ToolSpec(
        name="test.read",
        description="read-only test tool",
        side_effect="none",
        capability=Capability.TOOL_INVOKE_READONLY,
        handler=_handler,
    )
    tools.register(spec)

    with pytest.raises(ValueError, match="already registered"):
        tools.register(spec)
    with pytest.raises(ValueError, match="invalid tool name"):
        ToolSpec(
            name="UPPER CASE",
            description="invalid",
            side_effect="none",
            capability=Capability.TOOL_INVOKE_READONLY,
            handler=_handler,
        )
