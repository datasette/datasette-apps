from dataclasses import dataclass
import json
import sqlite3
from typing import Callable

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.agent_references import get_app_reference_types
from datasette_apps.agent_tools import get_app_edit_tools
from datasette_apps.links import parse_app_links
from datasette_apps.rendering import iframe_bridge_script, parent_bridge_script


@dataclass
class FakeAgentTool:
    name: str
    description: str
    input_schema: dict
    fn: object
    required_permission: str | None = None


@dataclass
class FakeReference:
    type: str
    id: str
    name: str
    description: str = ""


@dataclass
class FakeReferenceType:
    type: str
    label: str
    search: Callable
    url: Callable
    instructions: str
    context: Callable


def _tools():
    return {tool.name: tool for tool in get_app_edit_tools(FakeAgentTool)}


def _manifest(links):
    return (
        '<script type="application/json" id="datasette-app-links">'
        + json.dumps(links)
        + "</script>"
    )


CUSTOMER_LINKS = [
    {
        "template": "#/customer/{id}",
        "description": "Detail view for one customer",
        "params": {"id": {"column": "sales/customers/id"}},
    },
    {"template": "#/overview", "description": "All customers"},
]


def test_parse_app_links_reads_manifest():
    links, errors = parse_app_links("<h1>CRM</h1>" + _manifest(CUSTOMER_LINKS))
    assert errors == []
    assert links == [
        {
            "template": "#/customer/{id}",
            "description": "Detail view for one customer",
            "params": {"id": {"column": "sales/customers/id"}},
        },
        {"template": "#/overview", "description": "All customers", "params": {}},
    ]


def test_parse_app_links_without_manifest():
    assert parse_app_links("<h1>No links</h1>") == ([], [])
    assert parse_app_links("") == ([], [])


@pytest.mark.parametrize(
    "attrs",
    [
        "id='datasette-app-links' type='application/json'",
        "id=datasette-app-links",
        'TYPE="application/json" ID="datasette-app-links"',
    ],
)
def test_parse_app_links_accepts_attribute_variations(attrs):
    links, errors = parse_app_links(
        f'<script>var x = 1;</script><script {attrs}>[{{"template": "#/a"}}]</script>'
    )
    assert errors == []
    assert [link["template"] for link in links] == ["#/a"]


def test_parse_app_links_reports_invalid_json():
    links, errors = parse_app_links(
        '<script id="datasette-app-links">[{"template": </script>'
    )
    assert links == []
    assert len(errors) == 1
    assert "not valid JSON" in errors[0]


def test_parse_app_links_drops_bad_entries_and_keeps_good_ones():
    links, errors = parse_app_links(
        _manifest(
            [
                {"template": "/customer/{id}"},
                "nope",
                {"template": "#/ok/{id}", "params": {"id": "sales/customers/id"}},
                {"template": "#/ok/{id}"},
                {
                    "template": "#/order/{order}",
                    "params": {
                        "order": {"column": "not-a-column"},
                        "extra": {"column": "sales/orders/id"},
                    },
                },
            ]
        )
    )
    assert [link["template"] for link in links] == ["#/ok/{id}", "#/order/{order}"]
    # The string shorthand for a param is expanded
    assert links[0]["params"] == {"id": {"column": "sales/customers/id"}}
    assert links[1]["params"] == {"order": {}}
    assert len(errors) == 5
    assert any("starting with #" in error for error in errors)
    assert any("duplicate template" in error for error in errors)
    assert any("database/table/column" in error for error in errors)
    assert any("params.extra is not a {placeholder}" in error for error in errors)


def test_parse_app_links_caps_untrusted_text():
    links, errors = parse_app_links(
        _manifest(
            [
                {"template": "#/a", "description": "x" * 5000},
                {"template": "#/" + "y" * 5000},
            ]
        )
    )
    assert len(links) == 1
    assert len(links[0]["description"]) == 500
    assert any("too long" in error for error in errors)



@pytest.mark.asyncio
async def test_registry_extracts_links_on_every_html_save():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    app = await registry.create_stored_app(
        actor_id="alice",
        name="CRM",
        description="",
        html="<h1>CRM</h1>" + _manifest(CUSTOMER_LINKS),
    )
    assert [link["template"] for link in await registry.get_links(app["id"])] == [
        "#/customer/{id}",
        "#/overview",
    ]

    await registry.update_stored_app(
        app["id"],
        "CRM",
        "",
        "<h1>CRM</h1>" + _manifest([{"template": "#/orders"}]),
        actor_id="alice",
    )
    assert [link["template"] for link in await registry.get_links(app["id"])] == [
        "#/orders"
    ]

    await registry.edit_stored_app_html(
        app["id"], lambda html: html.replace("#/orders", "#/invoices")
    )
    assert [link["template"] for link in await registry.get_links(app["id"])] == [
        "#/invoices"
    ]

    # A metadata-only change leaves the links alone
    await registry.update_stored_app(
        app["id"],
        "Renamed",
        "",
        "<h1>CRM</h1>" + _manifest([{"template": "#/invoices"}]),
        actor_id="alice",
    )
    assert len(await registry.get_links(app["id"])) == 1

    await registry.edit_stored_app_html(app["id"], lambda html: "<h1>No links</h1>")
    assert await registry.get_links(app["id"]) == []


@pytest.mark.asyncio
async def test_external_apps_advertise_links_through_metadata():
    datasette = Datasette(memory=True)
    registry = Registry(datasette)
    await registry.add_app(
        id="plugin:map",
        name="Map",
        description="",
        path="/-/map",
        source="plugin",
        metadata={"links": [{"template": "#/place/{id}", "description": "A place"}]},
    )
    assert await registry.get_links("plugin:map") == [
        {"template": "#/place/{id}", "description": "A place", "params": {"id": {}}}
    ]
    await registry.remove_apps_for_source("plugin")
    assert await registry.get_links("plugin:map") == []


@pytest.mark.asyncio
async def test_links_are_backfilled_for_apps_saved_before_the_table_existed(tmp_path):
    internal = tmp_path / "internal.db"
    datasette = Datasette(memory=True, internal=str(internal))
    app = await Registry(datasette).create_stored_app(
        actor_id="alice",
        name="CRM",
        description="",
        html=_manifest(CUSTOMER_LINKS),
    )
    datasette.close()
    conn = sqlite3.connect(internal)
    conn.execute("DROP TABLE app_links")
    conn.commit()
    conn.close()

    datasette = Datasette(memory=True, internal=str(internal))
    links = await Registry(datasette).get_links(app["id"])
    assert [link["template"] for link in links] == ["#/customer/{id}", "#/overview"]
    datasette.close()


async def _datasette_with_linked_apps():
    datasette = Datasette(
        memory=True,
        config={"permissions": {"view-app": {"id": "*"}}},
    )
    registry = Registry(datasette)
    apps = {}
    apps["crm"] = await registry.create_stored_app(
        actor_id="bob",
        name="CRM",
        description="Customer records",
        html=_manifest(CUSTOMER_LINKS),
    )
    await registry.set_access_mode(apps["crm"]["id"], "not-private", actor_id="bob")
    apps["orders"] = await registry.create_stored_app(
        actor_id="alice",
        name="Order tracker",
        description="",
        html=_manifest(
            [
                {
                    "template": "#/order/{order_id}",
                    "description": "One order",
                    "params": {"order_id": {"column": "sales/orders/id"}},
                }
            ]
        ),
    )
    apps["secret"] = await registry.create_stored_app(
        actor_id="bob",
        name="Bob's private CRM",
        description="",
        html=_manifest(CUSTOMER_LINKS),
    )
    apps["plain"] = await registry.create_stored_app(
        actor_id="alice", name="No links", description="", html="<h1>Plain</h1>"
    )
    await datasette.invoke_startup()
    return datasette, apps


@pytest.mark.asyncio
async def test_app_links_tool_lists_links_for_viewable_apps():
    datasette, apps = await _datasette_with_linked_apps()
    result = json.loads(
        await _tools()["app_links"].fn(datasette=datasette, actor={"id": "alice"})
    )
    by_id = {app["app_id"]: app for app in result["apps"]}
    # Not Bob's private app, and not the app with nothing to link to
    assert set(by_id) == {apps["crm"]["id"], apps["orders"]["id"]}
    crm = by_id[apps["crm"]["id"]]
    assert crm["name"] == "CRM"
    assert crm["is_private"] is False
    assert crm["links"] == [
        {
            "url_template": f"/-/apps/{apps['crm']['id']}#/customer/{{id}}",
            "description": "Detail view for one customer",
            "params": {"id": {"column": "sales/customers/id"}},
        },
        {
            "url_template": f"/-/apps/{apps['crm']['id']}#/overview",
            "description": "All customers",
        },
    ]
    # Alice's own app is private: flagged so the model can warn
    assert by_id[apps["orders"]["id"]]["is_private"] is True
    assert result["returned_count"] == 2
    assert result["has_more"] is False


@pytest.mark.asyncio
async def test_app_links_tool_filters_by_table_and_query():
    datasette, apps = await _datasette_with_linked_apps()
    tool = _tools()["app_links"]

    result = json.loads(
        await tool.fn(
            datasette=datasette,
            actor={"id": "alice"},
            database="sales",
            table="customers",
        )
    )
    assert [app["app_id"] for app in result["apps"]] == [apps["crm"]["id"]]
    # Only the link that takes a customers value, not #/overview
    assert [link["url_template"] for link in result["apps"][0]["links"]] == [
        f"/-/apps/{apps['crm']['id']}#/customer/{{id}}"
    ]

    result = json.loads(
        await tool.fn(datasette=datasette, actor={"id": "alice"}, database="sales")
    )
    assert {app["app_id"] for app in result["apps"]} == {
        apps["crm"]["id"],
        apps["orders"]["id"],
    }

    result = json.loads(
        await tool.fn(datasette=datasette, actor={"id": "alice"}, query="one order")
    )
    assert [app["app_id"] for app in result["apps"]] == [apps["orders"]["id"]]

    result = json.loads(
        await tool.fn(datasette=datasette, actor={"id": "alice"}, table="customers")
    )
    assert result == {"error": "Pass database along with table"}


@pytest.mark.asyncio
async def test_app_links_tool_respects_view_permission():
    datasette, apps = await _datasette_with_linked_apps()
    result = json.loads(
        await _tools()["app_links"].fn(datasette=datasette, actor={"id": "bob"})
    )
    assert {app["app_id"] for app in result["apps"]} == {
        apps["crm"]["id"],
        apps["secret"]["id"],
    }


@pytest.mark.asyncio
async def test_app_create_tool_reports_advertised_links_and_manifest_errors():
    datasette = Datasette(
        memory=True, config={"permissions": {"create-app": {"id": "alice"}}}
    )
    await datasette.invoke_startup()
    tools = _tools()
    result = json.loads(
        await tools["app_create"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            name="CRM",
            html=_manifest(CUSTOMER_LINKS + [{"template": "no-hash"}]),
        )
    )
    app_id = result["app_id"]
    assert result["advertised_links"] == [
        f"/-/apps/{app_id}#/customer/{{id}}",
        f"/-/apps/{app_id}#/overview",
    ]
    assert len(result["link_manifest_errors"]) == 1

    # Editing reports the same, so the model sees a manifest it just broke
    edited = json.loads(
        await tools["app_str_replace"].fn(
            datasette=datasette,
            actor={"id": "alice"},
            app_id=app_id,
            old_str="#/overview",
            new_str="#/everyone",
        )
    )
    assert f"/-/apps/{app_id}#/everyone" in edited["advertised_links"]
    assert len(edited["link_manifest_errors"]) == 1


def test_app_tool_descriptions_explain_fragment_links():
    tools = _tools()
    for name in ("app_create", "app_edit"):
        description = tools[name].description
        assert 'id="datasette-app-links"' in description
        assert "location.hash" in description
        assert "app_links" in description
    assert "#/customer/42" in tools["app_debug"].description


@pytest.mark.asyncio
async def test_app_reference_context_lists_advertised_links():
    datasette, apps = await _datasette_with_linked_apps()
    reference_type = get_app_reference_types(FakeReferenceType, FakeReference)[0]
    crm_id = apps["crm"]["id"]
    context = await reference_type.context(
        datasette, {"id": "alice"}, FakeReference(type="app", id=crm_id, name="CRM")
    )
    assert (
        f"- /-/apps/{crm_id}#/customer/{{id}} - Detail view for one customer"
        " (id is sales/customers/id)"
    ) in context
    assert f"- /-/apps/{crm_id}#/overview - All customers" in context

    plain = await reference_type.context(
        datasette,
        {"id": "alice"},
        FakeReference(type="app", id=apps["plain"]["id"], name="No links"),
    )
    assert "can be linked to" not in plain


def test_iframe_bridge_initial_hash_is_null_unless_given():
    assert "var bakedInitialHash = null;" in iframe_bridge_script("token")
    script = iframe_bridge_script("token", initial_hash="#/a</script>")
    assert 'var bakedInitialHash = "#/a<\\/script>";' in script
    assert 'var bakedInitialHash = "";' in iframe_bridge_script(
        "token", initial_hash=""
    )


def test_bridge_scripts_exchange_hash_messages():
    iframe = iframe_bridge_script("token")
    parent = parent_bridge_script("app-id", channel_token="token")
    for message_type in ("datasette-app-hash", "datasette-app-set-hash"):
        assert message_type in iframe
        assert message_type in parent
