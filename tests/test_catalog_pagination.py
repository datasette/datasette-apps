import pytest
from datasette.app import Datasette

from datasette_apps import Registry


@pytest.mark.asyncio
async def test_apps_index_paginates_with_next_cursor():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    registry = Registry(datasette)
    for i in range(21):
        await registry.add_app(
            id=f"plugin:{i:02d}",
            name=f"App {i:02d}",
            description="",
            path=f"/-/plugin-{i:02d}",
            source="plugin",
        )

    first_page = await datasette.client.get("/-/apps", actor={"id": "alice"})
    assert first_page.status_code == 200
    assert "App 20" in first_page.text
    assert "App 00" not in first_page.text
    assert "next=20" in first_page.text

    second_page = await datasette.client.get("/-/apps?next=20", actor={"id": "alice"})
    assert second_page.status_code == 200
    assert "App 00" in second_page.text
    assert "next=40" not in second_page.text


async def _datasette_with_apps(count):
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    registry = Registry(datasette)
    for i in range(count):
        await registry.add_app(
            id=f"plugin:{i:02d}",
            name=f"App {i:02d}",
            description="",
            path=f"/-/plugin-{i:02d}",
            source="plugin",
        )
    return datasette


@pytest.mark.asyncio
async def test_apps_index_shows_previous_link_and_page_number():
    datasette = await _datasette_with_apps(45)

    first_page = await datasette.client.get("/-/apps", actor={"id": "alice"})
    assert '<span aria-disabled="true">Previous</span>' in first_page.text
    assert "<span>Page 1</span>" in first_page.text
    assert '<a rel="next" href="/-/apps?next=20">Next</a>' in first_page.text
    assert 'rel="prev"' not in first_page.text

    second_page = await datasette.client.get("/-/apps?next=20", actor={"id": "alice"})
    assert '<a rel="prev" href="/-/apps">Previous</a>' in second_page.text
    assert "<span>Page 2</span>" in second_page.text
    assert '<a rel="next" href="/-/apps?next=40">Next</a>' in second_page.text

    third_page = await datasette.client.get("/-/apps?next=40", actor={"id": "alice"})
    assert '<a rel="prev" href="/-/apps?next=20">Previous</a>' in third_page.text
    assert "<span>Page 3</span>" in third_page.text
    assert '<span aria-disabled="true">Next</span>' in third_page.text


@pytest.mark.asyncio
async def test_apps_index_pagination_keeps_search_query():
    datasette = await _datasette_with_apps(45)

    second_page = await datasette.client.get(
        "/-/apps?next=20&q=App", actor={"id": "alice"}
    )
    assert '<a rel="prev" href="/-/apps?q=App">Previous</a>' in second_page.text
    assert '<a rel="next" href="/-/apps?next=40&amp;q=App">Next</a>' in second_page.text


@pytest.mark.asyncio
async def test_apps_index_hides_pagination_for_a_single_page():
    datasette = await _datasette_with_apps(3)

    response = await datasette.client.get("/-/apps", actor={"id": "alice"})
    assert "datasette-apps-pagination" not in response.text
