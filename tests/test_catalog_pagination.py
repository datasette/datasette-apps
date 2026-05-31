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
