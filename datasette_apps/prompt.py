from __future__ import annotations

from datasette.resources import TableResource


def _quote_identifier(identifier):
    return '"' + identifier.replace('"', '""') + '"'


async def _row_count(db, resource):
    try:
        result = await db.execute(f"select count(*) from {_quote_identifier(resource)}")
        return result.single_value()
    except Exception:
        return None


async def _schema_lines(datasette, actor):
    lines = []
    for database_name, db in datasette.databases.items():
        if database_name == "_internal":
            continue
        resource_lines = []
        for resource_type, names in (
            ("table", await db.table_names()),
            ("view", await db.view_names()),
        ):
            for resource_name in names:
                if not await datasette.allowed(
                    action="view-table",
                    resource=TableResource(database_name, resource_name),
                    actor=actor,
                ):
                    continue
                resource_lines.append(f"- {resource_type}: {resource_name}")
                columns = await db.table_column_details(resource_name)
                for column in columns:
                    column_type = column.type or "unknown"
                    resource_lines.append(f"  - {column.name} {column_type}")
                primary_keys = await db.primary_keys(resource_name)
                if primary_keys:
                    resource_lines.append(f"  primary key: {', '.join(primary_keys)}")
                foreign_keys = await db.foreign_keys_for_table(resource_name)
                for foreign_key in foreign_keys:
                    resource_lines.append(
                        "  foreign key: "
                        f"{foreign_key['column']} -> "
                        f"{foreign_key['other_table']}.{foreign_key['other_column']}"
                    )
                count = await _row_count(db, resource_name)
                if count is not None:
                    resource_lines.append(f"  row count: {count}")
        if resource_lines:
            lines.append(f"Database: {database_name}")
            lines.extend(resource_lines)
    if not lines:
        return "No tables or views are visible to the current actor."
    return "\n".join(lines)


async def build_llm_prompt(datasette, actor):
    schema = await _schema_lines(datasette, actor)
    return f"""Build a Datasette HTML app.

Return a complete single-file HTML document. Include <!DOCTYPE html>, CSS, and JavaScript in the same file.

This app will run inside a sandboxed iframe protected by a strict Content Security Policy.

Important limitations:
- Direct network access is disabled by default.
- The app cannot fetch from Datasette, localhost, or arbitrary origins.
- External fetch() requests only work for exact https:// origins explicitly granted in the app's network access settings.
- Remote images are allowed from those same exact https:// origins. Local file previews using data: and blob: image URLs are allowed.
- CORS still applies even when an origin is granted.
- datasette.executeQuery() is not available.

Use this API for data access:
- await datasette.query(database, sql, params?)
- The SQL must be read-only.
- Query access is limited to databases enabled for this app and this actor's normal Datasette SQL permissions.
- If a database is not selected in the app's Data access settings, datasette.query() cannot query it.
- The returned value has this shape: {{columns: [...], rows: [{{...}}, ...]}}.

Plugin capabilities, if enabled for this app, are requested with:
- await datasette.request(capabilityName, input)

Available schema for this actor:

{schema}

Small example:

<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Example Datasette app</title>
</head>
<body>
  <h1>Recent rows</h1>
  <pre id="output">Loading...</pre>
  <script>
  async function main() {{
    const result = await datasette.query(
      "main",
      "select * from example_table limit 10"
    );
    document.getElementById("output").textContent =
      JSON.stringify(result.rows, null, 2);
  }}
  main().catch(error => {{
    document.getElementById("output").textContent = String(error);
  }});
  </script>
</body>
</html>
"""
