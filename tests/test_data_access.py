import json
import sqlite3

import pytest
from datasette.app import Datasette

from datasette_apps import Registry


def create_database(tmp_path):
    db_path = tmp_path / "content.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        create table news (id integer primary key, title text);
        insert into news (title) values ('First'), ('Second');
        create table private_notes (id integer primary key, body text);
        insert into private_notes (body) values ('Secret');
        create view recent_news as select title from news;
        """)
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
    await registry.set_sql_databases(app["id"], ["content"])
    return app


@pytest.mark.asyncio
async def test_datasette_query_reads_from_allowed_database(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
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
async def test_datasette_query_allows_other_tables_in_allowed_database(
    tmp_path,
):
    datasette = Datasette([str(create_database(tmp_path))])
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
        actor={"id": "alice"},
        content=json.dumps(
            {
                "database": "content",
                "sql": "select body from private_notes",
            }
        ),
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "result": {
            "columns": ["body"],
            "rows": [{"body": "Secret"}],
        },
    }


@pytest.mark.asyncio
async def test_datasette_query_denies_unallowed_database(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))], memory=True)
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
        actor={"id": "alice"},
        content=json.dumps(
            {
                "database": "_memory",
                "sql": "select 1",
            }
        ),
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "not allowed" in response.json()["error"]


@pytest.mark.asyncio
async def test_datasette_query_denies_writes(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
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
    assert "SELECT" in response.json()["error"]


@pytest.mark.asyncio
async def test_datasette_query_intersects_actor_permissions(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))], default_deny=True)
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
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
    assert "Permission denied" in response.json()["error"]


@pytest.mark.asyncio
async def test_datasette_query_accepts_named_parameters(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    app = await create_app_with_news_grant(datasette)

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
        actor={"id": "alice"},
        json={
            "database": "content",
            "sql": "select title from news where id = :id",
            "params": {"id": 2},
        },
    )
    assert response.json()["ok"] is True
    assert response.json()["result"] == {
        "columns": ["title"],
        "rows": [{"title": "Second"}],
    }


@pytest.mark.asyncio
async def test_datasette_query_allows_granted_views(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    app = await create_app_with_news_grant(datasette)

    allowed = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
        actor={"id": "alice"},
        json={"database": "content", "sql": "select title from recent_news"},
    )
    assert allowed.json()["ok"] is True
    assert allowed.json()["result"]["rows"] == [{"title": "First"}, {"title": "Second"}]


@pytest.mark.asyncio
async def test_datasette_stored_query_runs_allow_listed_query(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    await datasette.invoke_startup()
    await datasette.add_query(
        "content",
        "news_by_id",
        "select title from news where id = :id",
        source="user",
        owner_id="alice",
    )
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Stored query app",
        description="",
        html="",
        stored_queries=["content/news_by_id"],
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
        actor={"id": "alice"},
        json={
            "database": "content",
            "query": "news_by_id",
            "params": {"id": 2},
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "result": {
            "columns": ["title"],
            "rows": [{"title": "Second"}],
        },
    }


@pytest.mark.asyncio
async def test_datasette_stored_query_denies_unallow_listed_query(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    await datasette.invoke_startup()
    await datasette.add_query(
        "content",
        "news_by_id",
        "select title from news where id = :id",
        source="user",
        owner_id="alice",
    )
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Stored query app",
        description="",
        html="",
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
        actor={"id": "alice"},
        json={
            "database": "content",
            "query": "news_by_id",
            "params": {"id": 2},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "not allowed" in response.json()["error"]


@pytest.mark.asyncio
async def test_datasette_stored_query_intersects_actor_permissions(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))], default_deny=True)
    await datasette.invoke_startup()
    await datasette.add_query(
        "content",
        "news_by_id",
        "select title from news where id = :id",
        source="user",
        owner_id="alice",
    )
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Stored query app",
        description="",
        html="",
        stored_queries=["content/news_by_id"],
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/query",
        actor={"id": "alice"},
        json={
            "database": "content",
            "query": "news_by_id",
            "params": {"id": 2},
        },
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "Permission denied" in response.json()["error"]
