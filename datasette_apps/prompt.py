from __future__ import annotations

from datasette.resources import TableResource

from .utils import sort_names_with_underscores_last


def _quote_identifier(identifier):
    return '"' + identifier.replace('"', '""') + '"'


async def _row_count(db, resource):
    try:
        result = await db.execute(f"select count(*) from {_quote_identifier(resource)}")
        return result.single_value()
    except Exception:
        return None


async def _schema_by_database(datasette, actor):
    schemas = {}
    for database_name, db in datasette.databases.items():
        if database_name == "_internal":
            continue
        lines = []
        for resource_type, names in (
            ("table", sort_names_with_underscores_last(await db.table_names())),
            ("view", await db.view_names()),
        ):
            for resource_name in names:
                if not await datasette.allowed(
                    action="view-table",
                    resource=TableResource(database_name, resource_name),
                    actor=actor,
                ):
                    continue
                lines.append(f"- {resource_type}: {resource_name}")
                columns = await db.table_column_details(resource_name)
                for column in columns:
                    column_type = column.type or "unknown"
                    lines.append(f"  - {column.name} {column_type}")
                primary_keys = await db.primary_keys(resource_name)
                if primary_keys:
                    lines.append(f"  primary key: {', '.join(primary_keys)}")
                foreign_keys = await db.foreign_keys_for_table(resource_name)
                for foreign_key in foreign_keys:
                    lines.append(
                        "  foreign key: "
                        f"{foreign_key['column']} -> "
                        f"{foreign_key['other_table']}.{foreign_key['other_column']}"
                    )
                count = await _row_count(db, resource_name)
                if count is not None:
                    lines.append(f"  row count: {count}")
        if lines:
            schemas[database_name] = f"Database: {database_name}\n" + "\n".join(lines)
    return schemas


async def _schema_lines(datasette, actor):
    schemas = await _schema_by_database(datasette, actor)
    if not schemas:
        return "No tables or views are visible to the current actor."
    return "\n".join(schemas.values())


async def build_llm_prompt_data(datasette, actor):
    schema_by_database = await _schema_by_database(datasette, actor)
    return {
        "schema": "\n".join(schema_by_database.values())
        or "No tables or views are visible to the current actor.",
        "schema_by_database": schema_by_database,
    }


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
