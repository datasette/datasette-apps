"""Panel templates: small, consistent dashboard-style apps.

Every template is an ordinary stored app - one self-contained HTML file -
assembled from three pieces:

- a CONFIG block at the top, the only part that normally needs editing
- the shared "panel kit" CSS and JS helpers (identical in every template,
  which is what keeps panels looking like one family)
- a short template-specific script that turns query rows into the panel

Templates are designed to be pinned at the top of a page as a panel (see
panels.py) but work as full-page apps too.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from datasette.utils import escape_sqlite

_TEMPLATE_DIR = Path(__file__).parent / "panel_templates"
_SPLIT_MARKER = "/* ==== JS ==== */"

# Panels sit on a 12 column grid
PANEL_WIDTHS = [
    (12, "Full width"),
    (8, "Two thirds"),
    (6, "Half"),
    (4, "One third"),
    (3, "One quarter"),
]
PANEL_WIDTH_VALUES = [width for width, _ in PANEL_WIDTHS]
DEFAULT_PANEL_WIDTH = 12
DEFAULT_PANEL_HEIGHT = 240

_COMMON_FIELDS = {
    "database": (
        "Database to query. Leave empty to use the database of the page the "
        "panel is pinned to."
    ),
    "table": (
        "Table that {{table}} and {{filtered}} in the SQL refer to when the "
        "panel is not pinned to a table page."
    ),
    "label": "Short sentence-case label shown above the data.",
    "prefix": 'Text placed before numbers, for example "$".',
    "suffix": 'Text placed after numbers, for example " ms".',
    "format": (
        '"compact" (default: 1,284 / 12.9K / 4.2M), "full" (never abbreviate) '
        'or "percent" (0.42 shows as 42%).'
    ),
    "refresh_seconds": "Re-run the query this often. 0 means load once.",
}


@dataclass(frozen=True)
class PanelTemplate:
    id: str
    name: str
    description: str
    # What the SQL query has to return
    sql_shape: str
    width: int
    height: int
    config: dict
    fields: dict = field(default_factory=dict)
    thumbnail: str = ""

    def field_docs(self):
        docs = {}
        for key in self.config:
            docs[key] = self.fields.get(key) or _COMMON_FIELDS.get(key, "")
        return docs

    def as_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "sql_shape": self.sql_shape,
            "default_width": self.width,
            "default_height": self.height,
            "config": self.field_docs(),
            "config_defaults": dict(self.config),
        }


def _thumb(body):
    return (
        '<svg viewBox="0 0 120 64" width="120" height="64" aria-hidden="true">'
        + body
        + "</svg>"
    )


_T_LABEL = '<rect x="8" y="9" width="34" height="4" rx="2" fill="#c9c8c2"/>'
_T_VALUE = '<rect x="8" y="20" width="52" height="14" rx="3" fill="#0b0b0b"/>'

TEMPLATES = [
    PanelTemplate(
        id="stat",
        name="Stat tile",
        description=(
            "One headline number with a label, and an optional change against "
            "a previous value."
        ),
        sql_shape=(
            "One row with a `value` column. Add a `previous` column to show "
            "the percentage change."
        ),
        width=3,
        height=120,
        config={
            "label": "Total orders",
            "database": "",
            "table": "",
            "sql": "select 1284 as value, 1140 as previous",
            "delta_label": "vs previous period",
            "up_is_good": True,
            "caption": "",
            "prefix": "",
            "suffix": "",
            "format": "compact",
            "refresh_seconds": 0,
        },
        fields={
            "sql": "Returns one row: `value`, and optionally `previous`.",
            "delta_label": "What `previous` means, shown after the change.",
            "up_is_good": "false if a rising number is bad news (errors, cost).",
            "caption": "Optional small print under the number.",
        },
        thumbnail=_thumb(
            _T_LABEL
            + _T_VALUE
            + '<rect x="8" y="42" width="22" height="4" rx="2" fill="#006300"/>'
            '<rect x="34" y="42" width="38" height="4" rx="2" fill="#c9c8c2"/>'
        ),
    ),
    PanelTemplate(
        id="stat-row",
        name="KPI row",
        description="A strip of up to six headline numbers side by side.",
        sql_shape=(
            "Either one row where every column is a tile (column name is the "
            "label), or several rows of `label`, `value`."
        ),
        width=12,
        height=96,
        config={
            "database": "",
            "table": "",
            "sql": (
                'select 1284 as "Orders", 312 as "Customers", '
                '58210 as "Revenue", 4.6 as "Avg rating"'
            ),
            "prefix": "",
            "suffix": "",
            "format": "compact",
            "refresh_seconds": 0,
        },
        fields={"sql": "One row of named columns, or rows of `label`, `value`."},
        thumbnail=_thumb(
            "".join(
                f'<rect x="{x}" y="20" width="20" height="4" rx="2" fill="#c9c8c2"/>'
                f'<rect x="{x}" y="30" width="{w}" height="11" rx="3" fill="#0b0b0b"/>'
                + (
                    f'<rect x="{x - 6}" y="16" width="1" height="30" fill="#e4e3df"/>'
                    if x > 10
                    else ""
                )
                for x, w in ((8, 24), (46, 18), (84, 26))
            )
        ),
    ),
    PanelTemplate(
        id="sparkline",
        name="Number with trend",
        description=(
            "A headline number above a sparkline, for one measure tracked " "over time."
        ),
        sql_shape="Rows of `x` (a date or period) and `value`, oldest first.",
        width=4,
        height=140,
        config={
            "label": "Orders per month",
            "database": "",
            "table": "",
            "sql": (
                "select '2026-01' as x, 80 as value union all select '2026-02', 96 "
                "union all select '2026-03', 91 union all select '2026-04', 118 "
                "union all select '2026-05', 134"
            ),
            "headline": "last",
            "delta_label": "vs previous",
            "up_is_good": True,
            "prefix": "",
            "suffix": "",
            "format": "compact",
            "refresh_seconds": 0,
        },
        fields={
            "sql": "Rows of `x`, `value` ordered oldest to newest.",
            "headline": '"last" shows the newest value, "sum" the total.',
            "delta_label": "Shown after the change between the last two points.",
            "up_is_good": "false if a rising number is bad news.",
        },
        thumbnail=_thumb(
            _T_LABEL
            + '<rect x="8" y="18" width="36" height="11" rx="3" fill="#0b0b0b"/>'
            '<path d="M8 54 L28 48 L48 50 L68 41 L88 43 L110 34 L110 58 L8 58 Z" '
            'fill="#2a78d6" opacity=".1"/>'
            '<path d="M8 54 L28 48 L48 50 L68 41 L88 43 L110 34" fill="none" '
            'stroke="#2a78d6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
            '<circle cx="110" cy="34" r="3" fill="#2a78d6"/>'
        ),
    ),
    PanelTemplate(
        id="timeseries",
        name="Time series",
        description=(
            "A line chart with gridlines and a hover readout, for change " "over time."
        ),
        sql_shape="Rows of `x` (a date or period) and `value`, oldest first.",
        width=8,
        height=240,
        config={
            "label": "Orders per month",
            "database": "",
            "table": "",
            "sql": (
                "select '2026-01' as x, 80 as value union all select '2026-02', 96 "
                "union all select '2026-03', 91 union all select '2026-04', 118 "
                "union all select '2026-05', 134"
            ),
            "prefix": "",
            "suffix": "",
            "format": "compact",
            "refresh_seconds": 0,
        },
        fields={"sql": "Rows of `x`, `value` ordered oldest to newest."},
        thumbnail=_thumb(
            "".join(
                f'<rect x="18" y="{y}" width="94" height="1" fill="#e4e3df"/>'
                f'<rect x="8" y="{y - 1}" width="6" height="3" rx="1" fill="#c9c8c2"/>'
                for y in (12, 28, 44)
            )
            + '<path d="M18 46 L34 38 L50 41 L66 27 L82 30 L98 17 L112 20 L112 52 L18 52 Z" '
            'fill="#2a78d6" opacity=".1"/>'
            '<path d="M18 46 L34 38 L50 41 L66 27 L82 30 L98 17 L112 20" fill="none" '
            'stroke="#2a78d6" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>'
            '<rect x="18" y="57" width="12" height="3" rx="1" fill="#c9c8c2"/>'
            '<rect x="100" y="57" width="12" height="3" rx="1" fill="#c9c8c2"/>'
        ),
    ),
    PanelTemplate(
        id="bars",
        name="Top categories",
        description="Horizontal bars ranking the biggest groups in a column.",
        sql_shape="Rows of `label` and `value`, biggest first.",
        width=6,
        height=240,
        config={
            "label": "Orders by status",
            "database": "",
            "table": "",
            "sql": (
                "select 'Delivered' as label, 840 as value union all "
                "select 'Shipped', 260 union all select 'Processing', 120 "
                "union all select 'Returned', 64"
            ),
            "prefix": "",
            "suffix": "",
            "format": "compact",
            "refresh_seconds": 0,
        },
        fields={"sql": "Rows of `label`, `value`, already sorted and limited."},
        thumbnail=_thumb(
            "".join(
                f'<rect x="8" y="{y}" width="22" height="4" rx="2" fill="#c9c8c2"/>'
                f'<path d="M36 {y - 2} h{w - 3} a3 3 0 0 1 3 3 v2 a3 3 0 0 1 -3 3 h-{w - 3} z" '
                'fill="#2a78d6"/>'
                for y, w in ((12, 72), (25, 50), (38, 31), (51, 16))
            )
        ),
    ),
    PanelTemplate(
        id="recent",
        name="Recent rows",
        description="A compact list of the latest rows, like an activity feed.",
        sql_shape=(
            "Rows with a `title` column, plus optional `subtitle` and `meta` "
            "(usually a date), newest first."
        ),
        width=6,
        height=240,
        config={
            "label": "Latest orders",
            "database": "",
            "table": "",
            "sql": (
                "select 'Order #1284' as title, 'Ada Lovelace' as subtitle, "
                "'2026-05-14' as meta union all "
                "select 'Order #1283', 'Grace Hopper', '2026-05-13'"
            ),
            "refresh_seconds": 0,
        },
        fields={"sql": "Rows of `title`, optional `subtitle`, optional `meta`."},
        thumbnail=_thumb(
            _T_LABEL
            + "".join(
                f'<rect x="8" y="{y}" width="{w}" height="4" rx="2" fill="#0b0b0b"/>'
                f'<rect x="92" y="{y}" width="20" height="4" rx="2" fill="#c9c8c2"/>'
                f'<rect x="8" y="{y + 8}" width="104" height="1" fill="#e4e3df"/>'
                for y, w in ((21, 46), (34, 58), (47, 38))
            )
        ),
    ),
    PanelTemplate(
        id="meter",
        name="Progress meter",
        description="A number shown as progress towards a goal.",
        sql_shape=(
            "One row with a `value` column. Add a `goal` column to compute "
            "the goal in SQL instead of CONFIG.goal."
        ),
        width=4,
        height=120,
        config={
            "label": "Orders this quarter",
            "database": "",
            "table": "",
            "sql": "select 1284 as value",
            "goal": 2000,
            "caption": "of quarterly goal",
            "prefix": "",
            "suffix": "",
            "format": "compact",
            "refresh_seconds": 0,
        },
        fields={
            "sql": "Returns one row: `value`, and optionally `goal`.",
            "goal": "The target the value is measured against.",
            "caption": "Optional words shown after the percentage.",
        },
        thumbnail=_thumb(
            _T_LABEL
            + '<rect x="8" y="19" width="40" height="12" rx="3" fill="#0b0b0b"/>'
            '<rect x="8" y="40" width="104" height="6" rx="3" fill="#cde2fb"/>'
            '<rect x="8" y="40" width="66" height="6" rx="3" fill="#2a78d6"/>'
            '<rect x="8" y="52" width="28" height="4" rx="2" fill="#c9c8c2"/>'
        ),
    ),
    PanelTemplate(
        id="calendar",
        name="Activity calendar",
        description=(
            "A heatmap of days, darker where more happened - good for "
            "spotting gaps and busy periods."
        ),
        sql_shape="Rows of `day` (YYYY-MM-DD) and `value`.",
        width=12,
        height=170,
        config={
            "label": "Orders per day",
            "database": "",
            "table": "",
            "sql": (
                "with recursive days(day, n) as (select date('2026-05-31'), 0 "
                "union all select date(day, '-1 day'), n + 1 from days where n < 180) "
                "select day, (n * 7919) % 9 as value from days"
            ),
            "end": "latest",
            "format": "compact",
            "refresh_seconds": 0,
        },
        fields={
            "sql": "Rows of `day`, `value`. Days with no row count as zero.",
            "end": (
                '"latest" ends the calendar on the newest day in the data, '
                '"today" ends it on the current date.'
            ),
        },
        thumbnail=_thumb(
            "".join(
                f'<rect x="{8 + col * 9}" y="{8 + row * 9}" width="7" height="7" rx="1.5" '
                f'fill="{("#efeeeb", "#b7d3f6", "#6da7ec", "#2a78d6", "#184f95")[(col * 5 + row * 3 + col * row) % 5]}"/>'
                for col in range(12)
                for row in range(6)
            )
        ),
    ),
    PanelTemplate(
        id="note",
        name="Note",
        description=(
            "A short text callout - explain where the data came from or what "
            "to watch out for."
        ),
        sql_shape="No query: this template only shows CONFIG.text.",
        width=12,
        height=88,
        config={
            "label": "About this data",
            "text": (
                "Describe where this data comes from and how often it updates.\n\n"
                "- Use **bold** and `code` for emphasis\n"
                "- Start lines with a dash to make a list"
            ),
            "tone": "info",
            "fit_height": True,
        },
        fields={
            "label": "Optional heading.",
            "text": (
                'Blank lines separate paragraphs, lines starting with "- " '
                "make a list, **bold** and `code` are supported."
            ),
            "tone": '"info" (blue rule), "warning" or "plain".',
            "fit_height": "Resize the panel frame to fit the text.",
        },
        thumbnail=_thumb(
            '<rect x="8" y="12" width="3" height="40" fill="#2a78d6"/>'
            '<rect x="18" y="14" width="40" height="5" rx="2" fill="#0b0b0b"/>'
            '<rect x="18" y="26" width="92" height="4" rx="2" fill="#c9c8c2"/>'
            '<rect x="18" y="35" width="84" height="4" rx="2" fill="#c9c8c2"/>'
            '<rect x="18" y="44" width="60" height="4" rx="2" fill="#c9c8c2"/>'
        ),
    ),
]

_TEMPLATES_BY_ID = {template.id: template for template in TEMPLATES}

_TEMPLATE_COMMENT = "<!-- datasette-apps panel template: "


def template_of_html(source):
    """The panel template an app's HTML was built from, if any."""
    start = (source or "").find(_TEMPLATE_COMMENT)
    if start == -1:
        return None
    start += len(_TEMPLATE_COMMENT)
    end = source.find(" -->", start)
    return _TEMPLATES_BY_ID.get(source[start:end]) if end != -1 else None


def get_panel_template(template_id):
    return _TEMPLATES_BY_ID.get(template_id)


class UnknownPanelTemplate(ValueError):
    pass


@lru_cache(maxsize=None)
def _read(name):
    return (_TEMPLATE_DIR / name).read_text("utf-8").strip("\n")


def _js_value(value):
    """A JavaScript literal for a CONFIG value. Multi-line strings become
    template literals so SQL stays readable and editable."""
    if isinstance(value, str) and "\n" in value:
        escaped = value.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")
        return "`" + escaped + "`"
    return json.dumps(value, ensure_ascii=False)


def _js_config(config):
    lines = ["{"]
    items = list(config.items())
    for index, (key, value) in enumerate(items):
        name = key if re.fullmatch(r"[A-Za-z_$][\w$]*", key) else json.dumps(key)
        comma = "," if index < len(items) - 1 else ""
        lines.append(f"  {name}: {_js_value(value)}{comma}")
    lines.append("}")
    # Never let a config value close the surrounding <script> element
    return "\n".join(lines).replace("</", "<\\/")


def _config_docs(template):
    lines = []
    if template.sql_shape and "sql" in template.config:
        lines.append(f"// The query returns: {template.sql_shape}")
        lines.append(
            "// In the SQL, {{table}} means the table the panel is pinned to and"
        )
        lines.append(
            "// {{filtered}} means that table with the page's current filters applied."
        )
    for key, doc in template.field_docs().items():
        if doc:
            lines.append(f"//   {key}: {doc}")
    return "\n".join(lines)


def merged_panel_config(template_id, config=None):
    template = get_panel_template(template_id)
    if template is None:
        raise UnknownPanelTemplate(
            "Unknown panel template {!r}. Available: {}".format(
                template_id, ", ".join(t.id for t in TEMPLATES)
            )
        )
    merged = dict(template.config)
    for key, value in (config or {}).items():
        if value is None:
            continue
        if key in merged and isinstance(merged[key], bool):
            value = bool(value)
        merged[key] = value
    return merged


def render_panel_template(template_id, config=None, title=None):
    """Return complete app HTML for a template, with config merged over the
    template's defaults."""
    merged = merged_panel_config(template_id, config)
    template = get_panel_template(template_id)
    css, _, js = _read(f"{template.id}.html").partition(_SPLIT_MARKER)
    replacements = {
        "__TITLE__": html.escape(title or merged.get("label") or template.name),
        "__CONFIG_DOCS__": _config_docs(template),
        "__CONFIG__": _js_config(merged),
        "__KIT_CSS__": _read("_kit.css"),
        "__TEMPLATE_CSS__": css.strip("\n"),
        "__KIT_JS__": _read("_kit.js"),
        "__TEMPLATE_JS__": js.strip("\n"),
        "__TEMPLATE_ID__": template.id,
    }
    # Single pass so text inside one replacement is never re-expanded
    pattern = re.compile("|".join(re.escape(key) for key in replacements))
    return (
        pattern.sub(lambda match: replacements[match.group(0)], _read("_base.html"))
        + "\n"
    )


_DATE_NAME = re.compile(
    r"(date|time|created|updated|modified|timestamp|_at$|^at$|^day$|^when$)", re.I
)
_DATE_TYPE = re.compile(r"(date|time)", re.I)
_ID_NAME = re.compile(r"(^id$|_id$|^pk$|uuid|url|href|email|hash)", re.I)


async def describe_table(datasette, database, table):
    """Guess which columns make a useful default panel: a date-like column
    to trend over, a low-cardinality text column to group by and a label
    column to list."""
    db = datasette.databases[database]
    columns = await db.table_column_details(table)
    date_column = None
    for column in columns:
        if _DATE_TYPE.search(column.type or "") or _DATE_NAME.search(column.name):
            date_column = column.name
            break
    label_column = await db.label_column_for_table(table)
    category_column = None
    for column in columns:
        if column.is_pk or column.name == date_column or _ID_NAME.search(column.name):
            continue
        if (column.type or "").upper() not in {"TEXT", "VARCHAR", ""}:
            continue
        try:
            distinct = (
                await db.execute(
                    "select count(distinct {col}) from (select {col} from {table} limit 5000)".format(
                        col=escape_sqlite(column.name), table=escape_sqlite(table)
                    )
                )
            ).single_value()
        except Exception:
            continue
        if 1 < distinct <= 40:
            category_column = column.name
            break
    if label_column is None:
        for column in columns:
            if not column.is_pk and (column.type or "").upper() in {"TEXT", "VARCHAR"}:
                label_column = column.name
                break
    return {
        "date_column": date_column,
        "category_column": category_column,
        "label_column": label_column,
        "first_column": columns[0].name if columns else None,
    }


async def suggest_panel_config(datasette, template_id, database=None, table=None):
    """Config overrides that point a template at real data. With no table
    the template keeps its built-in sample query, so it still renders."""
    template = get_panel_template(template_id)
    if template is None:
        raise UnknownPanelTemplate(f"Unknown panel template {template_id!r}")
    config = {}
    if "database" in template.config and database:
        config["database"] = database
    if not (database and table) or database not in datasette.databases:
        return config
    try:
        info = await describe_table(datasette, database, table)
    except Exception:
        return config
    # {{filtered}} follows the table page's filters when the panel is allowed
    # to see them, and is the whole of CONFIG.table everywhere else
    t = "{{filtered}}"
    if "table" in template.config:
        config["table"] = table
    title = table[:1].upper() + table[1:]
    date_col = escape_sqlite(info["date_column"]) if info["date_column"] else None
    cat_col = (
        escape_sqlite(info["category_column"]) if info["category_column"] else None
    )
    title_col = escape_sqlite(info["label_column"] or info["first_column"] or "rowid")
    by_month = (
        f"select x, value from (\n"
        f"  select strftime('%Y-%m', {date_col}) as x, count(*) as value\n"
        f"  from {t}\n"
        f"  where {date_col} is not null\n"
        f"  group by 1 order by 1 desc limit 36\n"
        f") where x is not null order by x"
    )
    if template.id == "stat":
        config.update(
            label=f"Rows in {table}",
            sql=f"select count(*) as value\nfrom {t}",
            delta_label="",
        )
    elif template.id == "stat-row":
        parts = ['count(*) as "Rows"']
        if cat_col:
            parts.append(
                f'count(distinct {cat_col}) as "Distinct {info["category_column"]}"'
            )
        if date_col:
            parts.append(f'min(date({date_col})) as "Earliest"')
            parts.append(f'max(date({date_col})) as "Latest"')
        config["sql"] = "select\n  " + ",\n  ".join(parts) + f"\nfrom {t}"
    elif template.id in {"sparkline", "timeseries"} and date_col:
        config.update(label=f"{title} per month", sql=by_month)
        if template.id == "sparkline":
            config["delta_label"] = "vs previous month"
    elif template.id == "bars" and cat_col:
        config.update(
            label=f"{title} by {info['category_column']}",
            sql=(
                f"select {cat_col} as label, count(*) as value\n"
                f"from {t}\ngroup by 1 order by 2 desc limit 8"
            ),
        )
    elif template.id == "recent":
        columns = [f"{title_col} as title"]
        if cat_col:
            columns.append(f"{cat_col} as subtitle")
        if date_col:
            columns.append(f"{date_col} as meta")
        config.update(
            label=f"Latest {table}",
            sql=(
                "select " + ", ".join(columns)
                # rowid is not visible through the {{filtered}} subquery
                + f"\nfrom {t if date_col else '{{table}}'}\n"
                f"order by {date_col or 'rowid'} desc limit 8"
            ),
        )
    elif template.id == "meter":
        config.update(
            label=f"Rows in {table}",
            sql=f"select count(*) as value\nfrom {t}",
            caption="of goal",
        )
    elif template.id == "calendar" and date_col:
        config.update(
            label=f"{title} per day",
            sql=(
                f"select date({date_col}) as day, count(*) as value\n"
                f"from {t}\n"
                f"where {date_col} >= date((select max({date_col}) from {t}), '-370 days')\n"
                f"group by 1"
            ),
        )
    elif template.id == "note":
        config.update(label=f"About {table}")
    return config
