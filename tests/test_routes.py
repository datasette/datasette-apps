import re

import pytest
from datasette.app import Datasette

from datasette_apps import Registry


@pytest.mark.asyncio
async def test_apps_index_lists_apps_with_view_app_permission():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    await Registry(datasette).add_app(
        id="plugin:one",
        name="Plugin One",
        description="A plugin app",
        path="/-/plugin-one",
        source="plugin",
    )

    anonymous = await datasette.client.get("/-/apps")
    assert anonymous.status_code == 200
    assert "Plugin One" not in anonymous.text

    response = await datasette.client.get("/-/apps", actor={"id": "alice"})
    assert response.status_code == 200
    assert "Plugin One" in response.text
    assert "/-/apps/plugin:one/launch" in response.text
    assert 'class="datasette-app-button" href="/-/apps/create"' in response.text


@pytest.mark.asyncio
async def test_create_view_and_edit_stored_app():
    datasette = Datasette(memory=True)

    create = await datasette.client.post(
        "/-/apps/create",
        actor={"id": "alice"},
        data={
            "name": "Hello app",
            "description": "Says hello",
            "html": "<!DOCTYPE html><title>Hello</title><h1>Hello</h1>",
            "sql_databases_present": "1",
            "sql_databases": "_memory",
        },
    )
    assert create.status_code == 302
    location = create.headers["location"]
    assert re.match(r"^/-/apps/[0-9a-z]{26}$", location)

    app_id = location.rsplit("/", 1)[-1]
    assert await Registry(datasette).get_sql_databases(app_id) == ["_memory"]

    view = await datasette.client.get(location, actor={"id": "alice"})
    assert view.status_code == 200
    assert view.headers["content-security-policy"] == "frame-src 'none';"
    assert "Hello app" in view.text
    assert "iframe" in view.text
    assert 'sandbox="allow-scripts allow-forms"' in view.text
    assert "datasette-app-query" in view.text
    assert "datasette-app-stored-query" in view.text
    assert "storedQuery" in view.text
    assert "database.indexOf" not in view.text
    assert "runStoredQuery" not in view.text
    assert f"/-/apps/{app_id}/query" in view.text
    assert "window.datasette" in view.text
    assert "datasette.request" not in view.text
    assert "Hello" in view.text

    edit_form = await datasette.client.get(
        f"/-/apps/{app_id}/edit", actor={"id": "alice"}
    )
    assert edit_form.status_code == 200
    assert "datasette-app-form" in edit_form.text
    assert 'textarea id="app-description" name="description"' in edit_form.text
    assert "cm-editor-6.0.1.bundle.js" in edit_form.text
    assert 'textarea id="html-editor"' in edit_form.text
    assert "cm.editorFromTextArea" in edit_form.text

    state = await Registry(datasette).get_user_state("alice", app_id)
    assert state["access_count"] == 1

    edit = await datasette.client.post(
        f"/-/apps/{app_id}/edit",
        actor={"id": "alice"},
        data={
            "name": "Hello app",
            "description": "Updated",
            "html": "<!DOCTYPE html><title>Updated</title><h1>Updated</h1>",
        },
    )
    assert edit.status_code == 302
    assert edit.headers["location"] == f"/-/apps/{app_id}"

    version = await Registry(datasette).get_current_version(app_id)
    app = await Registry(datasette).get_app(app_id)
    assert app["description"] == "Updated"
    assert version["version"] == 2
    assert "Updated" in version["html"]

    edit_form = await datasette.client.get(
        f"/-/apps/{app_id}/edit", actor={"id": "alice"}
    )
    assert "Revision history" in edit_form.text
    assert f"/-/apps/{app_id}/revisions/2" in edit_form.text
    assert f"/-/apps/{app_id}/revisions/1" in edit_form.text
    assert ">v2</a>" in edit_form.text
    assert ">v1</a>" in edit_form.text
    assert (
        '<span class="datasette-app-revision-field-summary-label">Changed</span>'
        in edit_form.text
    )
    assert '<ul class="datasette-app-revision-fields">' in edit_form.text
    assert '<li class="datasette-app-revision-field">Description</li>' in edit_form.text
    assert '<li class="datasette-app-revision-field">HTML</li>' in edit_form.text
    v1_history = edit_form.text.split(">v1</a>", 1)[1].split("</li>", 1)[0]
    assert "Created" in v1_history
    assert '<ul class="datasette-app-revision-fields">' not in v1_history
    first_time_text = (
        edit_form.text.split("<time", 1)[1].split(">", 1)[1].split("</time>", 1)[0]
    )
    assert "T" not in first_time_text
    assert "+" not in first_time_text
    assert "current" in edit_form.text

    revision = await datasette.client.get(
        f"/-/apps/{app_id}/revisions/2", actor={"id": "alice"}
    )
    assert revision.status_code == 200
    assert "v2 of Hello app" in revision.text
    assert "compared with v1" in revision.text
    assert "Copy to clipboard" in revision.text
    assert 'id="revision-html-source" readonly' in revision.text
    assert "cm.editorFromTextArea" in revision.text
    assert "cm-readonly" in revision.text
    assert "&lt;title&gt;Updated&lt;/title&gt;" in revision.text
    assert "+++ v2" in revision.text
    assert (
        "+&lt;!DOCTYPE html&gt;&lt;title&gt;Updated&lt;/title&gt;&lt;h1&gt;Updated&lt;/h1&gt;"
        in revision.text
    )
    assert "<iframe" not in revision.text
    assert 'id="html-editor"' not in revision.text

    missing_revision = await datasette.client.get(
        f"/-/apps/{app_id}/revisions/99", actor={"id": "alice"}
    )
    assert missing_revision.status_code == 404


@pytest.mark.asyncio
async def test_revision_pages_show_non_html_changes_without_empty_diff():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="Hello app",
        description="Initial",
        html="<h1>Hello</h1>",
    )

    await registry.set_access_mode(app["id"], "not-private", actor_id="alice")
    await registry.set_sql_databases(app["id"], ["_memory"], actor_id="alice")
    await registry.set_csp_origins(
        app["id"], ["https://api.github.com"], actor_id="alice"
    )
    await registry.update_stored_app(
        app["id"],
        "Renamed app",
        "Updated description",
        "<h1>Hello</h1>",
        actor_id="alice",
    )

    async def assert_settings_revision(version, expected):
        response = await datasette.client.get(
            f"/-/apps/{app['id']}/revisions/{version}", actor={"id": "alice"}
        )
        assert response.status_code == 200
        assert "Changes" in response.text
        for text in expected:
            assert text in response.text
        assert "HTML diff" not in response.text
        assert 'class="datasette-app-diff"' not in response.text
        assert "Copy to clipboard" not in response.text
        assert 'id="revision-html-source"' not in response.text
        assert "cm.editorFromTextArea" not in response.text

    await assert_settings_revision(
        2,
        [
            "Privacy",
            "Private",
            "Not private",
        ],
    )
    await assert_settings_revision(
        3,
        [
            "Read-only data access",
            'datasette-app-revision-empty-value">- unset -</span>',
            "_memory",
        ],
    )
    await assert_settings_revision(
        4,
        [
            "Network access",
            'datasette-app-revision-empty-value">- unset -</span>',
            "https://api.github.com",
        ],
    )
    await assert_settings_revision(
        5,
        [
            "Name",
            "Hello app",
            "Renamed app",
            "Description",
            "Initial",
            "Updated description",
        ],
    )


@pytest.mark.asyncio
async def test_capability_system_removed():
    datasette = Datasette(memory=True)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="App",
        description="",
        html="",
    )

    # The generic capability endpoint no longer exists.
    old = await datasette.client.post(
        f"/-/apps/{app['id']}/capabilities/datasette.query",
        actor={"id": "alice"},
        json={"database": "_memory", "sql": "select 1"},
    )
    assert old.status_code == 404

    # The capability module and hookspec are gone.
    with pytest.raises(ImportError):
        import datasette_apps.capabilities  # noqa: F401
    with pytest.raises(ImportError):
        import datasette_apps.hookspecs  # noqa: F401
