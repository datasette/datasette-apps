import pytest
from datasette.app import Datasette

APPS_LINK = '<a href="/-/apps" role="menuitem">Apps</a>'


@pytest.mark.asyncio
async def test_apps_link_appears_in_datasette_menu_for_signed_in_actor():
    datasette = Datasette(memory=True)

    response = await datasette.client.get("/", actor={"id": "alice"})

    assert response.status_code == 200
    assert APPS_LINK in response.text


@pytest.mark.asyncio
async def test_apps_link_not_shown_to_anonymous_users():
    datasette = Datasette(memory=True)

    response = await datasette.client.get("/")

    assert response.status_code == 200
    assert APPS_LINK not in response.text
