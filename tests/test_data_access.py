import json
import sqlite3

import pytest
from datasette.app import Datasette

from datasette_apps import Registry


def create_database(tmp_path):
    db_path = tmp_path / "content.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        create table news (id integer primary key, title text);
        insert into news (title) values ('First'), ('Second');
        create table private_notes (id integer primary key, body text);
        insert into private_notes (body) values ('Secret');
        """
    )
    conn.close()
    return db_path


async def create_app_with_news_grant(datasette):
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="News app",
        description="",
        html="",
    )
    await registry.set_data_permissions(
        app["id"],
        [
            {
                "database_name": "content",
                "resource_type": "table",
                "resource_name": "news",
                "columns": None,
            }
        ],
    )
    return app


@pytest.mark.asyncio
async def test_datasette_query_capability_reads_granted_table(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/capabilities/datasette.query",
        actor={"id": "alice"},
        content=json.dumps(
            {
                "database": "content",
                "sql": "select title from news order by id",
            }
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "result": {
            "columns": ["title"],
            "rows": [{"title": "First"}, {"title": "Second"}],
        },
    }


@pytest.mark.asyncio
async def test_datasette_query_capability_denies_ungranted_table(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/capabilities/datasette.query",
        actor={"id": "alice"},
        content=json.dumps(
            {
                "database": "content",
                "sql": "select body from private_notes",
            }
        ),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "not allowed" in response.json()["error"]


@pytest.mark.asyncio
async def test_datasette_query_capability_denies_writes(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/capabilities/datasette.query",
        actor={"id": "alice"},
        content=json.dumps(
            {
                "database": "content",
                "sql": "insert into news (title) values ('Nope')",
            }
        ),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "not allowed" in response.json()["error"]


@pytest.mark.asyncio
async def test_datasette_query_capability_intersects_actor_permissions(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))], default_deny=True)
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/capabilities/datasette.query",
        actor={"id": "alice"},
        content=json.dumps(
            {
                "database": "content",
                "sql": "select title from news",
            }
        ),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "actor" in response.json()["error"]
