import re

import pytest
from datasette.app import Datasette

from datasette_apps import Registry


@pytest.mark.asyncio
async def test_apps_index_requires_actor_and_lists_apps():
    datasette = Datasette(memory=True)
    await Registry(datasette).add_app(
        id="plugin:one",
        name="Plugin One",
        description="A plugin app",
        path="/-/plugin-one",
        source="plugin",
    )

    anonymous = await datasette.client.get("/-/apps")
    assert anonymous.status_code == 403

    response = await datasette.client.get("/-/apps", actor={"id": "alice"})
    assert response.status_code == 200
    assert "Plugin One" in response.text
    assert "/-/apps/plugin:one/launch" in response.text


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
        },
    )
    assert create.status_code == 302
    location = create.headers["location"]
    assert re.match(r"^/-/apps/[0-9a-z]{26}$", location)

    app_id = location.rsplit("/", 1)[-1]
    view = await datasette.client.get(location, actor={"id": "alice"})
    assert view.status_code == 200
    assert "Hello app" in view.text
    assert "iframe" in view.text
    assert "datasette-app-request" in view.text
    assert "capabilities/" in view.text
    assert "datasette.query" in view.text
    assert "Hello" in view.text

    edit_form = await datasette.client.get(
        f"/-/apps/{app_id}/edit", actor={"id": "alice"}
    )
    assert edit_form.status_code == 200
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
