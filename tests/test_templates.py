import re

import pytest
from datasette.app import Datasette

from datasette_apps import Registry


def crumb_links(response):
    match = re.search(r'<p class="crumbs">(.*?)</p>', response.text, flags=re.S)
    assert match is not None
    return re.findall(r'<a href="([^"]+)">\s*([^<\s][^<]*?)\s*</a>', match.group(1))


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
    await assert_extends_datasette_base(
        await datasette.client.get(
            f"/-/apps/{app['id']}/revisions/1", actor={"id": "alice"}
        )
    )


@pytest.mark.asyncio
async def test_app_pages_show_breadcrumbs_to_apps_list():
    datasette = Datasette(memory=True)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Crumb app",
        description="",
        html="<h1>Hello</h1>",
    )

    list_response = await datasette.client.get("/-/apps", actor={"id": "alice"})
    create_response = await datasette.client.get(
        "/-/apps/create", actor={"id": "alice"}
    )
    view_response = await datasette.client.get(
        f"/-/apps/{app['id']}", actor={"id": "alice"}
    )
    edit_response = await datasette.client.get(
        f"/-/apps/{app['id']}/edit", actor={"id": "alice"}
    )

    assert crumb_links(list_response) == [("/", "home")]
    assert crumb_links(create_response) == [("/", "home"), ("/-/apps", "apps")]
    assert crumb_links(view_response) == [("/", "home"), ("/-/apps", "apps")]
    assert crumb_links(edit_response) == [
        ("/", "home"),
        ("/-/apps", "apps"),
        (f"/-/apps/{app['id']}", "Crumb app"),
    ]


@pytest.mark.asyncio
async def test_app_links_respect_base_url():
    datasette = Datasette(memory=True, settings={"base_url": "/prefix/"})
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="Base URL app",
        description="",
        html="<h1>Hello</h1>",
    )
    await Registry(datasette).update_stored_app(
        app["id"], "Base URL app", "", "<h1>Updated</h1>"
    )

    list_response = await datasette.client.get("/-/apps", actor={"id": "alice"})
    view_response = await datasette.client.get(
        f"/-/apps/{app['id']}", actor={"id": "alice"}
    )
    edit_response = await datasette.client.get(
        f"/-/apps/{app['id']}/edit", actor={"id": "alice"}
    )
    revision_response = await datasette.client.get(
        f"/-/apps/{app['id']}/revisions/2", actor={"id": "alice"}
    )

    assert 'href="/prefix/-/apps/create"' in list_response.text
    assert 'action="/prefix/-/apps"' in list_response.text
    assert f'href="/prefix/-/apps/{app["id"]}"' in list_response.text

    assert 'href="/prefix/-/apps"' in view_response.text
    assert f'href="/prefix/-/apps/{app["id"]}?full=1"' in view_response.text
    assert f'href="/prefix/-/apps/{app["id"]}/edit"' in view_response.text
    assert f'action="/prefix/-/apps/{app["id"]}/pin"' in view_response.text

    assert f'href="/prefix/-/apps/{app["id"]}/revisions/2"' in edit_response.text
    assert f'href="/prefix/-/apps/{app["id"]}/revisions/1"' in edit_response.text

    assert f'href="/prefix/-/apps/{app["id"]}/edit"' in revision_response.text
    assert f'href="/prefix/-/apps/{app["id"]}"' in revision_response.text
    assert 'href="/-/apps' not in (
        list_response.text
        + view_response.text
        + edit_response.text
        + revision_response.text
    )


@pytest.mark.asyncio
async def test_apps_list_marks_private_apps():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    registry = Registry(datasette)
    await registry.create_stored_app(
        actor_id="alice",
        name="Marked private app",
        description="",
        html="",
    )
    public_app = await registry.create_stored_app(
        actor_id="alice",
        name="Marked public app",
        description="",
        html="",
    )
    await registry.set_access_mode(public_app["id"], "not-private")

    response = await datasette.client.get("/-/apps", actor={"id": "alice"})

    assert response.status_code == 200
    private_item = response.text.split("Marked private app", 1)[1].split("</li>", 1)[0]
    public_item = response.text.split("Marked public app", 1)[1].split("</li>", 1)[0]
    assert "datasette-app-private-badge" in private_item
    assert "datasette-app-private-badge" not in public_item
    assert response.text.count("datasette-app-private-badge") == 1
