import pytest
from datasette.app import Datasette

from datasette_apps import Registry


def app_matches(response):
    return {
        match["name"]: match
        for match in response.json()["matches"]
        if match["type"] == "app"
    }


@pytest.mark.asyncio
async def test_jump_lists_apps_the_actor_can_view():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    registry = Registry(datasette)

    owned = await registry.create_stored_app(
        actor_id="alice",
        name="Jumpvisible Owned",
        description="Owned app",
        html="<h1>Owned</h1>",
    )
    shared = await registry.create_stored_app(
        actor_id="carol",
        name="Jumpvisible Shared",
        description="Shared app",
        html="<h1>Shared</h1>",
    )
    await registry.set_access_mode(shared["id"], "not-private")
    await registry.create_stored_app(
        actor_id="carol",
        name="Jumpvisible Private",
        description="Private app",
        html="<h1>Private</h1>",
    )
    await registry.add_app(
        id="plugin:external",
        name="Jumpvisible External",
        description="External app",
        path="/-/external-app",
        source="plugin",
    )

    alice_response = await datasette.client.get(
        "/-/jump.json?q=jumpvisible", actor={"id": "alice"}
    )

    assert alice_response.status_code == 200
    alice_matches = app_matches(alice_response)
    assert sorted(alice_matches) == [
        "Jumpvisible External",
        "Jumpvisible Owned",
        "Jumpvisible Shared",
    ]
    assert alice_matches["Jumpvisible Owned"]["url"] == owned["path"]
    assert alice_matches["Jumpvisible Owned"]["description"] == "Owned app"
    assert alice_matches["Jumpvisible Shared"]["url"] == shared["path"]
    assert (
        alice_matches["Jumpvisible External"]["url"] == "/-/apps/plugin:external/launch"
    )

    bob_response = await datasette.client.get(
        "/-/jump.json?q=jumpvisible", actor={"id": "bob"}
    )

    assert bob_response.status_code == 200
    bob_matches = app_matches(bob_response)
    assert sorted(bob_matches) == [
        "Jumpvisible External",
        "Jumpvisible Shared",
    ]
