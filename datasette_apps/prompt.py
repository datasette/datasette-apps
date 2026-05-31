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


async def build_llm_prompt_data(datasette, actor):
    return {"schema": await _schema_lines(datasette, actor)}


async def stored_query_options(datasette, stored_queries):
    options = []
    for key in stored_queries:
        if "/" not in key:
            continue
        database_name, query_name = key.split("/", 1)
        query = await datasette.get_query(database_name, query_name)
        if query is None:
            options.append(
                {
                    "key": key,
                    "label": key,
                    "description": "",
                    "parameters": [],
                    "is_write": False,
                }
            )
            continue
        options.append(
            {
                "key": key,
                "label": query.title or query.name,
                "description": query.description or "",
                "parameters": list(query.parameters or []),
                "is_write": bool(query.is_write),
            }
        )
    return options
