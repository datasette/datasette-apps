from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from datasette.plugins import pm
from datasette.utils import await_me_maybe


@dataclass
class AppCapability:
    name: str
    description: str
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    default_enabled: bool = False
    config_schema: dict[str, Any] | None = None
    handler: Callable[..., Awaitable[dict[str, Any]]] | None = None


def validate_capability_name(name):
    if not name or any(c in name for c in "/?#") or any(c.isspace() for c in name):
        raise ValueError("Capability names must be safe single URL path segments")


async def get_app_capabilities(datasette):
    capabilities = {}
    for hook_result in pm.hook.register_app_capabilities(datasette=datasette):
        result = await await_me_maybe(hook_result)
        if result is None:
            continue
        if isinstance(result, AppCapability):
            result = [result]
        for capability in result:
            validate_capability_name(capability.name)
            capabilities[capability.name] = capability
    return capabilities
