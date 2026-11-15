"""Link manifests: how an app advertises the fragment URLs it supports.

A stored app declares its linkable views in its own HTML source:

    <script type="application/json" id="datasette-app-links">
    [
      {
        "template": "#/customer/{id}",
        "description": "Detail view for one customer",
        "params": {"id": {"column": "sales/customers/id"}}
      }
    ]
    </script>

Keeping the manifest in the HTML means it is versioned in the same
revision as the router it describes and is edited with the same tools.
It is extracted into the app_links table whenever the HTML is saved, so
other apps - and datasette-agent - can discover it without parsing
every app's source. External apps supply the same list as
metadata["links"] when they are registered.

Everything in a manifest is untrusted text written by whoever authored
the app, so it is length-capped here and presented to models as data.
"""

from __future__ import annotations

import json
import re

LINKS_SCRIPT_ID = "datasette-app-links"
MAX_LINKS = 50
MAX_TEMPLATE_CHARS = 500
MAX_DESCRIPTION_CHARS = 500
MAX_PARAMS = 20

_SCRIPT_RE = re.compile(
    r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_ID_RE = re.compile(
    r"""\bid\s*=\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\s"'>]+))""",
    flags=re.IGNORECASE,
)
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _manifest_source(html):
    for match in _SCRIPT_RE.finditer(html or ""):
        id_match = _ID_RE.search(match.group("attrs"))
        if id_match is None:
            continue
        script_id = (
            id_match.group("dq") or id_match.group("sq") or id_match.group("bare")
        )
        if script_id == LINKS_SCRIPT_ID:
            return match.group("body")
    return None


def template_params(template):
    """Placeholder names in a template, in order, without duplicates."""
    return list(dict.fromkeys(_PLACEHOLDER_RE.findall(template or "")))


def parse_column(value):
    """A "database/table/column" reference as a dict, or None."""
    if not isinstance(value, str):
        return None
    parts = value.split("/")
    if len(parts) != 3 or not all(parts):
        return None
    return {"database": parts[0], "table": parts[1], "column": parts[2]}


def _normalize_param(name, value, errors, where):
    param = {}
    if value is None:
        return param
    if isinstance(value, str):
        # Shorthand: "params": {"id": "sales/customers/id"}
        value = {"column": value}
    if not isinstance(value, dict):
        errors.append(f"{where}: params.{name} must be an object")
        return param
    description = value.get("description")
    if isinstance(description, str) and description.strip():
        param["description"] = description.strip()[:MAX_DESCRIPTION_CHARS]
    column = value.get("column")
    if column is not None:
        if parse_column(column) is None:
            errors.append(
                f"{where}: params.{name}.column must look like database/table/column"
            )
        else:
            param["column"] = column
    return param


def normalize_links(value):
    """Validate a decoded manifest. Returns (links, errors).

    Invalid entries are dropped and described in errors rather than
    failing the whole manifest, so one typo does not hide every link.
    """
    errors = []
    if not isinstance(value, list):
        return [], ["The link manifest must be a JSON array of link objects"]
    if len(value) > MAX_LINKS:
        errors.append(f"Only the first {MAX_LINKS} links are used")
        value = value[:MAX_LINKS]
    links = []
    seen = set()
    for index, item in enumerate(value):
        where = f"links[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object")
            continue
        template = item.get("template")
        if not isinstance(template, str) or not template.startswith("#"):
            errors.append(f'{where}: "template" must be a string starting with #')
            continue
        if len(template) > MAX_TEMPLATE_CHARS:
            errors.append(f"{where}: template is too long")
            continue
        if template in seen:
            errors.append(f"{where}: duplicate template {template}")
            continue
        seen.add(template)
        raw_params = item.get("params") or {}
        if not isinstance(raw_params, dict):
            errors.append(f'{where}: "params" must be an object')
            raw_params = {}
        placeholders = template_params(template)
        for name in raw_params:
            if name not in placeholders:
                errors.append(
                    f"{where}: params.{name} is not a {{placeholder}} in the template"
                )
        params = {}
        for name in placeholders[:MAX_PARAMS]:
            params[name] = _normalize_param(name, raw_params.get(name), errors, where)
        description = item.get("description")
        links.append(
            {
                "template": template,
                "description": (
                    description.strip()[:MAX_DESCRIPTION_CHARS]
                    if isinstance(description, str)
                    else ""
                ),
                "params": params,
            }
        )
    return links, errors


def parse_app_links(html):
    """Extract the link manifest from app HTML. Returns (links, errors)."""
    source = _manifest_source(html)
    if source is None:
        return [], []
    try:
        value = json.loads(source)
    except ValueError as e:
        return [], [f"The {LINKS_SCRIPT_ID} script is not valid JSON: {e}"]
    return normalize_links(value)


def link_matches_table(link, database=None, table=None):
    """Does any param of this link take a value from that database/table?"""
    if not database and not table:
        return True
    for param in link["params"].values():
        column = parse_column(param.get("column"))
        if column is None:
            continue
        if database and column["database"] != database:
            continue
        if table and column["table"] != table:
            continue
        return True
    return False
