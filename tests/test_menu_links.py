import re

import pytest
from datasette.app import Datasette

# Datasette 1.0a42 added role="menuitem" to menu links
APPS_LINK_RE = re.compile(r'<a href="/-/apps"[^>]*>Apps</a>')


@pytest.mark.asyncio
async def test_apps_link_appears_in_datasette_menu_for_signed_in_actor():
    datasette = Datasette(memory=True)

    response = await datasette.client.get("/", actor={"id": "alice"})

    assert response.status_code == 200
    assert APPS_LINK_RE.search(response.text)


@pytest.mark.asyncio
async def test_apps_link_not_shown_to_anonymous_users():
    datasette = Datasette(memory=True)

    response = await datasette.client.get("/")

    assert response.status_code == 200
    assert not APPS_LINK_RE.search(response.text)
