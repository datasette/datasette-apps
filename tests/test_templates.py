import pytest
from datasette.app import Datasette

from datasette_apps import Registry


async def assert_extends_datasette_base(response):
    assert response.status_code == 200
    assert '<section class="content">' in response.text
    assert "datasette-manager.js" in response.text
    assert "navigation-search.js" in response.text


@pytest.mark.asyncio
async def test_app_pages_extend_datasette_base_template():
    datasette = Datasette(memory=True)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Template app",
        description="",
        html="<h1>Hello</h1>",
    )

    await assert_extends_datasette_base(
        await datasette.client.get("/-/apps", actor={"id": "alice"})
    )
    await assert_extends_datasette_base(
        await datasette.client.get("/-/apps/create", actor={"id": "alice"})
    )
    await assert_extends_datasette_base(
        await datasette.client.get(f"/-/apps/{app['id']}", actor={"id": "alice"})
    )
    await assert_extends_datasette_base(
        await datasette.client.get(f"/-/apps/{app['id']}/edit", actor={"id": "alice"})
    )
