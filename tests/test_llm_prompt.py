import sqlite3

import pytest
from datasette.app import Datasette


def create_database(tmp_path):
    db_path = tmp_path / "content.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        create table authors (id integer primary key, name text);
        create table news (
            id integer primary key,
            title text,
            author_id integer references authors(id)
        );
        insert into authors (name) values ('Ada');
        insert into news (title, author_id) values ('Launch', 1);
        """)
    conn.close()
    return db_path


@pytest.mark.asyncio
async def test_create_page_includes_copyable_llm_prompt_with_schema(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert 'class="datasette-app-form"' in response.text
    assert 'textarea id="app-description" name="description"' in response.text
    assert "Read-only SQL query databases" in response.text
    assert 'name="sql_databases"' in response.text
    assert 'value="content"' in response.text
    assert "cm-editor-6.0.1.bundle.js" in response.text
    assert 'textarea id="html-editor"' in response.text
    assert "cm.editorFromTextArea" in response.text
    assert "Copy prompt" in response.text
    assert 'id="llm-prompt"' in response.text
    assert "datasette.query(database, sql, params?)" in response.text
    assert "databases enabled for this app" in response.text
    assert "Content Security Policy" in response.text
    assert (
        "External script tags are allowed from those same exact https:// origins"
        in response.text
    )
    assert (
        "External stylesheet links and style elements are allowed from those same exact https:// origins"
        in response.text
    )
    assert "Database: content" in response.text
    assert "table: news" in response.text
    assert "title TEXT" in response.text
    assert "author_id -&gt; authors.id" in response.text


@pytest.mark.asyncio
async def test_create_page_prompt_does_not_leak_hidden_schema(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))], default_deny=True)

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert "table: news" not in response.text
    assert "table: authors" not in response.text
