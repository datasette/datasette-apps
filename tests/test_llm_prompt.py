import sqlite3

import pytest
from datasette.app import Datasette
from datasette_apps import Registry


def create_database(tmp_path):
    db_path = tmp_path / "content.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        create table _drafts (id integer primary key, body text);
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
    await datasette.invoke_startup()
    await datasette.add_query(
        "content",
        "author_lookup",
        "select name from authors where id = :id",
        title="Author lookup",
        description="Find an author by ID",
        parameters=["id"],
        source="user",
        owner_id="alice",
    )

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert 'class="datasette-app-form datasette-app-edit-form"' in response.text
    assert 'textarea id="app-description" name="description"' in response.text
    assert "Read-only SQL query databases" in response.text
    assert 'name="sql_databases"' in response.text
    assert 'value="content"' in response.text
    assert "cm-editor-6.0.1.bundle.js" in response.text
    assert 'textarea id="html-editor"' in response.text
    assert "cm.editorFromTextArea" in response.text
    assert "max-height: calc(50lh + 2px)" in response.text
    assert ".cm-editor .cm-scroller" in response.text
    assert "Use AI to build this app" in response.text
    assert "Copy prompt" in response.text
    assert 'copyButton.textContent = "Copied"' in response.text
    assert "}, 1500);" in response.text
    assert (
        "Describe the app you want in an LLM chat, then copy this prompt in as context"
        in response.text
    )
    assert "<summary>Show full prompt</summary>" in response.text
    assert 'id="llm-prompt" rows="24" cols="100" readonly></textarea>' in response.text
    assert 'id="llm-prompt-data"' in response.text
    assert response.text.index('id="copy-llm-prompt"') < response.text.index(
        "<details>"
    )
    assert response.text.index("<details>") < response.text.index('id="llm-prompt"')
    assert "htmlInput.datasetteAppsEditorView = cm.editorFromTextArea" in response.text
    assert "datasette-app-editor-ready" in response.text
    assert "function buildPrompt()" in response.text
    assert "function markdownFenceFor(text)" in response.text
    assert "Current app HTML" in response.text
    assert "function selectedSchema(databases)" in response.text
    assert "schema_by_database" in response.text
    assert "Available schema for the selected read-only SQL databases" in response.text
    assert (
        "No read-only SQL databases are currently selected, so no table schema is included."
        in response.text
    )
    assert "Allowed external network and asset origins" in response.text
    assert "exact https:// origins for fetch()" in response.text
    assert "function starterHtml(databases, storedQueries, title)" in response.text
    assert "var storedQuerySection = storedQueries.length" in response.text
    assert "No stored queries are selected yet." not in response.text
    assert "function appTitle()" in response.text
    assert "function escapeHtml(value)" in response.text
    assert "function stopAutoUpdate()" in response.text
    assert "body { font-family: Helvetica }" in response.text
    assert "select name, type from sqlite_master" in response.text
    assert "function splitStoredQueryKey(key)" in response.text
    assert "jsString(parts.database)" in response.text
    assert "jsString(parts.query)" in response.text
    assert "datasette.query(database, sql, params?)" in response.text
    assert "datasette.storedQuery(database, query, params?)" in response.text
    assert 'datasette.storedQuery(\\"database/query\\", params?)' not in response.text
    assert "Stored queries are selected on the create/edit page" in response.text
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
    assert "history.replaceState()" in response.text
    assert "history.pushState()" in response.text
    assert '"schema_by_database": {"content": "Database: content' in response.text
    assert "table: news" in response.text
    assert response.text.index("table: news") < response.text.index("table: _drafts")
    assert "title TEXT" in response.text
    assert "author_id -\\u003e authors.id" in response.text
    assert "Currently selected stored queries" in response.text
    assert "content/author_lookup: Author lookup (read-only)" not in response.text


@pytest.mark.asyncio
async def test_edit_page_prompt_has_selected_stored_query_metadata(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))])
    await datasette.invoke_startup()
    await datasette.add_query(
        "content",
        "author_lookup",
        "select name from authors where id = :id",
        title="Author lookup",
        description="Find an author by ID",
        parameters=["id"],
        source="user",
        owner_id="alice",
    )
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Controlled app",
        description="",
        html="<pre>```stored```</pre>",
        stored_queries=["content/author_lookup"],
    )

    response = await datasette.client.get(
        f"/-/apps/{app['id']}/edit", actor={"id": "alice"}
    )

    assert response.status_code == 200
    assert 'data-query-key="content/author_lookup"' in response.text
    assert 'data-query-label="Author lookup"' in response.text
    assert 'data-query-description="Find an author by ID"' in response.text
    assert "data-query-parameters='[\"id\"]'" in response.text
    assert 'data-query-is-write="0"' in response.text
    assert "Use AI to edit this app" in response.text
    assert 'id="llm-prompt-data"' in response.text
    assert "function buildPrompt()" in response.text
    assert "&lt;pre&gt;```stored```&lt;/pre&gt;" in response.text


@pytest.mark.asyncio
async def test_create_page_prompt_does_not_leak_hidden_schema(tmp_path):
    datasette = Datasette([str(create_database(tmp_path))], default_deny=True)

    response = await datasette.client.get("/-/apps/create", actor={"id": "alice"})

    assert response.status_code == 200
    assert "table: news" not in response.text
    assert "table: authors" not in response.text
