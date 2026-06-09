from __future__ import annotations

from datasette.resources import TableResource

from .utils import sort_names_with_underscores_last


async def _schema_by_database(datasette, actor):
    schemas = {}
    for database_name, db in datasette.databases.items():
        if database_name == "_internal":
            continue
        result = await db.execute(
            "select name, sql from sqlite_master"
            " where type in ('table', 'view') and sql is not null"
        )
        sql_by_name = {row["name"]: row["sql"] for row in result.rows}
        statements = []
        for names in (
            sort_names_with_underscores_last(await db.table_names()),
            await db.view_names(),
        ):
            for resource_name in names:
                if resource_name not in sql_by_name:
                    continue
                if not await datasette.allowed(
                    action="view-table",
                    resource=TableResource(database_name, resource_name),
                    actor=actor,
                ):
                    continue
                statements.append(sql_by_name[resource_name].strip())
        if statements:
            schemas[database_name] = f"Database: {database_name}\n" + ";\n".join(
                statements
            )
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
