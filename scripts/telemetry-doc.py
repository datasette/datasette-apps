#!/usr/bin/env python3
"""Render README.md's OpenTelemetry reference from the telemetry registry.

Rewrites the block between the telemetry-reference markers so the docs
cannot drift from the code; `--check` exits non-zero if the file would
change. `tests/test_telemetry_registry.py` imports `rendered_readme()` and
compares its output with the README, so the existing pytest run enforces
freshness.

Usage: uv run scripts/telemetry-doc.py [--check]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from datasette_apps import telemetry_registry as reg  # noqa: E402

README = ROOT / "README.md"
START = "<!-- telemetry-reference:start -->"
END = "<!-- telemetry-reference:end -->"

REQUEST_SPANS = "core's HTTP request spans"
TOOL_SPANS = "the agent tool span"


def _md(text: str) -> str:
    "Registry descriptions use RST double-backtick literals; fold to Markdown."
    return " ".join(text.replace("``", "`").split())


def _attribute_names(attributes) -> str:
    return ", ".join(
        f"`{attribute}`" + (" *(optional)*" if attribute.optional else "")
        for attribute in attributes
    )


def _used_on(attribute) -> list[str]:
    places = [f"`{span}`" for span in reg.SPANS if attribute in span.attributes]
    places += [
        f"`{metric}`" for metric in reg.METRICS if attribute in metric.attributes
    ]
    if attribute in reg.REQUEST_SPAN_ATTRIBUTES:
        places.append(REQUEST_SPANS)
    if attribute in reg.TOOL_SPAN_ATTRIBUTES:
        places.append(TOOL_SPANS)
    return places


def render() -> str:
    lines: list[str] = []
    add = lines.append
    add("#### Spans")
    add("")
    for span in reg.SPANS:
        kind = f" *({span.kind.name})*" if span.kind.name != "INTERNAL" else ""
        add(f"**`{span}`**{kind} - {_md(span.description)}")
        add("")
        add(f"Attributes: {_attribute_names(span.attributes)}.")
        add("")
    add("#### Attributes on spans this plugin does not own")
    add("")
    add(
        f"- On {REQUEST_SPANS} (scope `datasette`): "
        f"{_attribute_names(reg.REQUEST_SPAN_ATTRIBUTES)}."
    )
    add(
        f"- On {TOOL_SPANS} (datasette-agent's `execute_tool {{tool}}`, scope "
        f"`datasette_agent`, when it has telemetry): "
        f"{_attribute_names(reg.TOOL_SPAN_ATTRIBUTES)}."
    )
    add("")
    add("#### Metrics")
    add("")
    for metric in reg.METRICS:
        add(
            f"**`{metric}`** *({metric.kind}, unit `{metric.unit}`)* - "
            f"{_md(metric.description)}"
        )
        add("")
        if metric.attributes:
            add(f"Attributes: {_attribute_names(metric.attributes)}.")
            add("")
        if metric.buckets is not None:
            buckets = ", ".join(str(boundary) for boundary in metric.buckets)
            add(f"Bucket boundaries: {buckets}.")
            add("")
    add("#### Attributes")
    add("")
    for attribute in reg.ATTRIBUTES:
        parts = [f"- `{attribute}`"]
        if attribute.optional:
            parts.append("*(optional)*")
        parts.append("-")
        parts.append(_md(attribute.description))
        if attribute.values is not None:
            values = ", ".join(
                f"`{str(value).lower() if isinstance(value, bool) else value}`"
                for value in sorted(attribute.values, key=str)
            )
            parts.append(f"One of: {values}.")
        parts.append(f"Used on: {', '.join(_used_on(attribute))}.")
        add(" ".join(parts))
    return "\n".join(lines).rstrip() + "\n"


def rendered_readme(text: str) -> str:
    "`text` with the block between the markers replaced by `render()`."
    if START not in text or END not in text:
        raise ValueError(f"README.md is missing the {START} / {END} markers")
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    return f"{head}{START}\n\n{render()}\n{END}{tail}"


def main() -> int:
    check = "--check" in sys.argv
    text = README.read_text()
    try:
        updated = rendered_readme(text)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    if updated == text:
        return 0
    if check:
        print(
            "README.md telemetry reference is stale; run "
            "`uv run scripts/telemetry-doc.py`",
            file=sys.stderr,
        )
        return 1
    README.write_text(updated)
    print("Rewrote README.md telemetry reference")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
