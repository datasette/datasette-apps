import pytest
from datasette import hookimpl
from datasette.app import Datasette
from datasette.plugins import pm

from datasette_apps import Registry
from datasette_apps.capabilities import AppCapability


async def echo_handler(datasette, request, app, actor, input, config):
    return {
        "app_id": app["id"],
        "actor_id": actor["id"],
        "input": input,
        "config": config,
    }


class EchoCapabilityPlugin:
    @hookimpl
    def register_app_capabilities(self, datasette):
        return [
            AppCapability(
                name="test.echo",
                description="Echo input",
                handler=echo_handler,
            )
        ]


@pytest.fixture
def echo_capability_plugin():
    plugin = EchoCapabilityPlugin()
    pm.register(plugin, name="datasette-apps-test-echo")
    try:
        yield
    finally:
        pm.unregister(name="datasette-apps-test-echo")


@pytest.mark.asyncio
async def test_plugin_capability_denied_without_grant(echo_capability_plugin):
    datasette = Datasette(memory=True)
    app = await Registry(datasette).create_stored_app(
        actor_id="alice", name="App", description="", html=""
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/capabilities/test.echo",
        actor={"id": "alice"},
        json={"message": "hi"},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert "not enabled" in response.json()["error"]


@pytest.mark.asyncio
async def test_plugin_capability_receives_app_actor_input_and_config(
    echo_capability_plugin,
):
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice", name="App", description="", html=""
    )
    await registry.set_capability_grant(
        app["id"],
        "test.echo",
        {"mode": "friendly"},
    )

    response = await datasette.client.post(
        f"/-/apps/{app['id']}/capabilities/test.echo",
        actor={"id": "alice"},
        json={"message": "hi"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "result": {
            "app_id": app["id"],
            "actor_id": "alice",
            "input": {"message": "hi"},
            "config": {"mode": "friendly"},
        },
    }
