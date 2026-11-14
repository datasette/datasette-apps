import json
import sqlite3

import pytest
from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.agent_tools import get_app_edit_tools
from datasette_apps.panel_kit import (
    TEMPLATES,
    UnknownPanelTemplate,
    render_panel_template,
    suggest_panel_config,
    template_of_html,
)
from datasette_apps.panels import PanelError, create_panel, get_panel, list_panels
from datasette_apps.rendering import iframe_bridge_script

ALICE = {"id": "alice"}
BOB = {"id": "bob"}


@pytest.fixture
def datasette(tmp_path):
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        create table orders (
            id integer primary key, customer text, status text, created_at text
        );
        insert into orders (customer, status, created_at) values
            ('Ada', 'Delivered', '2026-01-03'),
            ('Grace', 'Shipped', '2026-02-10'),
            ('Ada', 'Delivered', '2026-02-11');
        """)
    conn.close()
    return Datasette(
        [str(path)],
        config={
            "permissions": {
                "create-app": {"id": "*"},
                "view-app": True,
                "apps-share-panels": {"id": "alice"},
            }
        },
    )


async def _app(datasette, actor_id="alice", html="<h1>Hi</h1>", is_private=False):
    return await Registry(datasette).create_stored_app(
        actor_id=actor_id,
        name="Order count",
        description="",
        html=html,
        is_private=is_private,
        sql_databases=["shop"],
    )


class _Tool:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _tools():
    return {tool.name: tool for tool in get_app_edit_tools(_Tool)}


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda t: t.id)
def test_every_template_renders_a_complete_document(template):
    html = render_panel_template(template.id, {"label": "My panel"})
    assert html.startswith("<!DOCTYPE html>")
    assert f"<!-- datasette-apps panel template: {template.id} -->" in html
    assert "const CONFIG = {" in html
    assert 'label: "My panel"' in html
    # Every template carries the same kit, which is what keeps them consistent
    assert "const Panel = (() => {" in html
    assert "--pk-accent" in html
    assert "__" not in html.replace("__proto__", "")
    assert template_of_html(html) is template


def test_template_config_cannot_break_out_of_the_script_element():
    html = render_panel_template(
        "stat", {"label": "</script><script>alert(1)</script>", "sql": "a\n`${b}`"}
    )
    assert "</script><script>alert(1)" not in html
    assert "<\\/script>" in html
    # Multi-line SQL is emitted as an escaped template literal
    assert "sql: `a\n\\`\\${b}\\``" in html


def test_unknown_template_is_an_error():
    with pytest.raises(UnknownPanelTemplate):
        render_panel_template("nope")
    assert template_of_html("<h1>Plain app</h1>") is None


@pytest.mark.asyncio
async def test_suggested_config_uses_the_table_columns(datasette):
    await datasette.invoke_startup()
    bars = await suggest_panel_config(datasette, "bars", "shop", "orders")
    assert bars["database"] == "shop"
    assert "customer as label" in bars["sql"]
    series = await suggest_panel_config(datasette, "timeseries", "shop", "orders")
    assert "strftime('%Y-%m', created_at)" in series["sql"]
    # No table: keep the template's built-in sample query
    assert await suggest_panel_config(datasette, "stat", "shop") == {"database": "shop"}
    # The suggested SQL really runs and has the shape the template wants
    assert series["table"] == "orders"
    assert "from {{filtered}}" in series["sql"]
    rows = (
        await datasette.get_database("shop").execute(
            series["sql"].replace("{{filtered}}", "orders")
        )
    ).rows
    assert [dict(row) for row in rows] == [
        {"x": "2026-01", "value": 1},
        {"x": "2026-02", "value": 2},
    ]


def test_bridge_exposes_context_to_the_app():
    script = iframe_bridge_script(
        "token", context={"target": "table", "database": "shop", "table": "orders"}
    )
    assert (
        'context: {"target": "table", "database": "shop", "table": "orders"}' in script
    )
    assert 'context: {"target": "app"}' in iframe_bridge_script("token")


@pytest.mark.asyncio
async def test_panel_renders_on_table_page_with_frame_csp(datasette):
    await datasette.invoke_startup()
    app = await _app(datasette)
    await create_panel(
        datasette, ALICE, app["id"], "table", "shop", "orders", width=4, height=132
    )

    response = await datasette.client.get("/shop/orders", actor=ALICE)
    assert response.status_code == 200
    assert 'class="datasette-app-panels"' in response.text
    assert "--panel-span: 4" in response.text
    assert "height: 132px" in response.text
    assert 'sandbox="allow-scripts allow-forms"' in response.text
    assert f"/-/apps/{app['id']}/query" in response.text
    assert "&quot;table&quot;: &quot;orders&quot;" in response.text
    # Same lockdown as the app page: the frame cannot navigate itself away
    assert response.headers["content-security-policy"] == "frame-src 'none';"

    # The database page and homepage have no panels and no extra header
    for path in ("/shop", "/"):
        other = await datasette.client.get(path, actor=ALICE)
        assert "datasette-app-panels" not in other.text
        assert "content-security-policy" not in other.headers


@pytest.mark.asyncio
async def test_personal_panels_are_private_and_shared_panels_are_not(datasette):
    await datasette.invoke_startup()
    app = await _app(datasette)
    personal = await create_panel(datasette, ALICE, app["id"], "database", "shop")
    assert (
        "datasette-app-panels"
        in (await datasette.client.get("/shop", actor=ALICE)).text
    )
    assert (
        "datasette-app-panels"
        not in (await datasette.client.get("/shop", actor=BOB)).text
    )
    assert "datasette-app-panels" not in (await datasette.client.get("/shop")).text

    # Bob lacks apps-share-panels
    with pytest.raises(PanelError):
        await create_panel(
            datasette, BOB, app["id"], "database", "shop", is_shared=True
        )
    await create_panel(datasette, ALICE, app["id"], "homepage", is_shared=True)
    homepage = await datasette.client.get("/", actor=BOB)
    assert "datasette-app-panels" in homepage.text
    # Bob can open the app but cannot manage Alice's shared panel
    assert ">Open</a>" in homepage.text
    assert ">Settings</a>" not in homepage.text
    assert (
        await datasette.client.get(f"/-/apps/panels/{personal['id']}", actor=BOB)
    ).status_code == 404


@pytest.mark.asyncio
async def test_shared_panel_of_private_app_stays_hidden(datasette):
    await datasette.invoke_startup()
    app = await _app(datasette, is_private=True)
    await create_panel(datasette, ALICE, app["id"], "homepage", is_shared=True)
    assert await list_panels(datasette, BOB, "homepage") == []
    assert len(await list_panels(datasette, ALICE, "homepage")) == 1


@pytest.mark.asyncio
async def test_panel_targets_are_validated(datasette):
    await datasette.invoke_startup()
    app = await _app(datasette)
    for args in (
        ("table", "shop", "missing"),
        ("database", "missing", None),
        ("table", "shop", None),
        ("row", "shop", "orders"),
    ):
        with pytest.raises(PanelError):
            await create_panel(datasette, ALICE, app["id"], *args)
    # Sizes are clamped and snapped rather than rejected
    panel = await create_panel(
        datasette, ALICE, app["id"], "homepage", width=5, height=99999
    )
    assert (panel["width"], panel["height"]) == (6, 1200)


@pytest.mark.asyncio
async def test_template_panels_hide_the_app_name_header_by_default(datasette):
    await datasette.invoke_startup()
    plain = await _app(datasette)
    templated = await _app(datasette, html=render_panel_template("stat"))
    assert (await create_panel(datasette, ALICE, plain["id"], "homepage"))[
        "show_header"
    ] == 1
    assert (await create_panel(datasette, ALICE, templated["id"], "homepage"))[
        "show_header"
    ] == 0


@pytest.mark.asyncio
async def test_add_edit_and_remove_panel_views(datasette):
    await datasette.invoke_startup()
    app = await _app(datasette)
    page = await datasette.client.get(
        "/-/apps/panels/add?target=table&database=shop&table=orders", actor=ALICE
    )
    assert page.status_code == 200
    assert "table shop / orders" in page.text
    assert (
        "/-/apps/create?template=stat&amp;panel_target=table"
        "&amp;panel_database=shop&amp;panel_table=orders"
    ) in page.text
    assert (
        await datasette.client.get("/-/apps/panels/add?target=homepage")
    ).status_code == 403

    response = await datasette.client.post(
        "/-/apps/panels/add",
        actor=ALICE,
        data={
            "app_id": app["id"],
            "target": "table/shop/orders",
            "width": "3",
            "height": "120",
        },
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/shop/orders"
    (panel,) = await list_panels(datasette, ALICE, "table", "shop", "orders")
    assert (panel["width"], panel["height"], panel["is_shared"]) == (3, 120, 0)

    response = await datasette.client.post(
        f"/-/apps/panels/{panel['id']}",
        actor=ALICE,
        data={"width": "12", "height": "300", "show_header": "1", "is_shared": "1"},
    )
    assert response.headers["location"] == "/shop/orders"
    updated = await get_panel(datasette, panel["id"])
    assert (updated["width"], updated["height"], updated["is_shared"]) == (12, 300, 1)

    await datasette.client.post(
        f"/-/apps/panels/{panel['id']}", actor=ALICE, data={"remove": "1"}
    )
    assert await get_panel(datasette, panel["id"]) is None


@pytest.mark.asyncio
async def test_create_page_offers_templates_and_pins_the_new_app(datasette):
    await datasette.invoke_startup()
    page = await datasette.client.get("/-/apps/create", actor=ALICE)
    assert "Start from a template" in page.text
    for template in TEMPLATES:
        assert f'data-template-id="{template.id}"' in page.text
    assert "data-template=" not in page.text

    page = await datasette.client.get(
        "/-/apps/create?template=bars&panel_target=table"
        "&panel_database=shop&panel_table=orders",
        actor=ALICE,
    )
    assert 'data-template="bars"' in page.text
    assert "datasette-apps panel template: bars" in page.text
    assert "customer as label" in page.text
    assert 'value="Orders by customer"' in page.text
    assert "Create and pin" in page.text

    response = await datasette.client.post(
        "/-/apps/create",
        actor=ALICE,
        data={
            "name": "Orders by customer",
            "html": render_panel_template("bars"),
            "panel_target": "table",
            "panel_database": "shop",
            "panel_table": "orders",
            "panel_width": "6",
            "panel_height": "240",
        },
    )
    assert response.headers["location"] == "/shop/orders"
    (panel,) = await list_panels(datasette, ALICE, "table", "shop", "orders")
    assert (panel["width"], panel["height"], panel["show_header"]) == (6, 240, 0)


@pytest.mark.asyncio
async def test_actions_menus_link_to_add_panel(datasette):
    await datasette.invoke_startup()
    table = await datasette.client.get("/shop/orders", actor=ALICE)
    assert (
        "/-/apps/panels/add?target=table&amp;database=shop&amp;table=orders"
        in table.text
    )
    assert "/-/apps/panels/add" not in (await datasette.client.get("/shop/orders")).text


@pytest.mark.asyncio
async def test_templates_json(datasette):
    response = await datasette.client.get("/-/apps/templates.json")
    templates = response.json()["templates"]
    assert [t["id"] for t in templates] == [t.id for t in TEMPLATES]
    assert templates[0]["config"]["sql"]


@pytest.mark.asyncio
async def test_agent_can_build_and_pin_a_template_panel(datasette):
    await datasette.invoke_startup()
    tools = _tools()
    listing = json.loads(await tools["app_panel_templates"].fn(datasette, ALICE))
    assert {"stat", "bars", "timeseries"} <= {t["id"] for t in listing["templates"]}

    created = json.loads(
        await tools["app_create"].fn(
            datasette,
            ALICE,
            name="Open orders",
            template="stat",
            template_config={
                "label": "Open orders",
                "database": "shop",
                "sql": "select count(*) as value from orders",
            },
        )
    )
    assert "app_pin_panel" in created["status"]
    registry = Registry(datasette)
    html = (await registry.get_current_version(created["app_id"]))["html"]
    assert 'sql: "select count(*) as value from orders"' in html
    # The template's database is added to the app's allow-list
    assert await registry.get_sql_databases(created["app_id"]) == ["shop"]

    pinned = json.loads(
        await tools["app_pin_panel"].fn(
            datasette,
            ALICE,
            app_id=created["app_id"],
            target="table",
            database="shop",
            table="orders",
        )
    )
    # Defaults to the template's own size
    assert (pinned["width"], pinned["height"], pinned["path"]) == (
        3,
        120,
        "/shop/orders",
    )
    assert (
        "datasette-app-panels"
        in (await datasette.client.get("/shop/orders", actor=ALICE)).text
    )

    error = json.loads(
        await tools["app_pin_panel"].fn(
            datasette, ALICE, app_id=created["app_id"], target="table", database="shop"
        )
    )
    assert "needs a table" in error["error"]
    bad = json.loads(
        await tools["app_create"].fn(datasette, ALICE, name="x", template="nope")
    )
    assert "Unknown panel template" in bad["error"]
    assert "error" in json.loads(
        await tools["app_create"].fn(datasette, ALICE, name="x")
    )

    # Bob cannot remove Alice's panel; Alice can
    denied = json.loads(
        await tools["app_unpin_panel"].fn(datasette, BOB, panel_id=pinned["panel_id"])
    )
    assert "not found" in denied["error"]
    await tools["app_unpin_panel"].fn(datasette, ALICE, panel_id=pinned["panel_id"])
    assert await get_panel(datasette, pinned["panel_id"]) is None


async def _page(datasette, app, actor=ALICE, **body):
    response = await datasette.client.post(
        f"/-/apps/{app['id']}/page", actor=actor, content=json.dumps(body)
    )
    assert response.status_code == 200
    return response.json()


@pytest.mark.asyncio
async def test_filter_sharing_panel_is_told_the_page_filters(datasette):
    await datasette.invoke_startup()
    app = await _app(datasette)
    await create_panel(
        datasette, ALICE, app["id"], "table", "shop", "orders", share_filters=True
    )
    page = await datasette.client.get(
        "/shop/orders?customer=Ada&_sort_desc=id&_size=1", actor=ALICE
    )
    # Display arguments are passed along raw but are not filters
    assert (
        "&quot;filters&quot;: {&quot;query_string&quot;: "
        "&quot;customer=Ada&amp;_sort_desc=id&amp;_size=1&quot;, "
        "&quot;params&quot;: [[&quot;customer&quot;, &quot;Ada&quot;]], "
        "&quot;active&quot;: true}"
    ) in page.text
    plain = await datasette.client.get("/shop/orders?_sort=id", actor=ALICE)
    assert "&quot;active&quot;: false" in plain.text


@pytest.mark.asyncio
async def test_panels_do_not_see_filters_unless_opted_in(datasette):
    await datasette.invoke_startup()
    app = await _app(datasette)
    panel = await create_panel(datasette, ALICE, app["id"], "table", "shop", "orders")
    page = await datasette.client.get("/shop/orders?customer=Ada", actor=ALICE)
    assert "&quot;filters&quot;" not in page.text
    refused = await _page(
        datasette, app, panel_id=panel["id"], kind="rows", query_string="customer=Ada"
    )
    assert refused["ok"] is False
    assert "not allowed to see the page filters" in refused["error"]
    # Only table panels can share filters
    database_panel = await create_panel(
        datasette, ALICE, app["id"], "database", "shop", share_filters=True
    )
    assert database_panel["share_filters"] == 0


@pytest.mark.asyncio
async def test_page_rows_and_page_query_apply_the_page_filters(datasette):
    await datasette.invoke_startup()
    app = await _app(datasette)
    panel = await create_panel(
        datasette, ALICE, app["id"], "table", "shop", "orders", share_filters=True
    )
    rows = await _page(
        datasette,
        app,
        panel_id=panel["id"],
        kind="rows",
        query_string="customer=Ada&_sort_desc=id&_size=1&_col=id",
    )
    assert rows["ok"] is True
    # Filter and sort follow the page; page size and column choice do not
    assert [row["id"] for row in rows["result"]["rows"]] == [3, 1]
    assert rows["result"]["count"] == 2
    assert "status" in rows["result"]["columns"]

    counted = await _page(
        datasette,
        app,
        panel_id=panel["id"],
        kind="sql",
        sql=(
            "select status, count(*) as n from {{filtered}} "
            "where id >= :min_id group by 1"
        ),
        params={"min_id": 1},
        query_string="customer=Ada&_size=1&_next=1",
    )
    assert counted["result"]["rows"] == [{"status": "Delivered", "n": 2}]
    # No filters: {{filtered}} is the whole table
    everything = await _page(
        datasette,
        app,
        panel_id=panel["id"],
        kind="sql",
        sql="select count(*) as n from {{filtered}}",
        query_string="",
    )
    assert everything["result"]["rows"] == [{"n": 3}]

    clash = await _page(
        datasette,
        app,
        panel_id=panel["id"],
        kind="sql",
        sql="select count(*) from {{filtered}} where id = :p0",
        params={"p0": 1},
        query_string="customer=Ada",
    )
    assert "reserved for the page filters" in clash["error"]
    # Still read-only SQL, run through the app's normal query path
    write = await _page(
        datasette,
        app,
        panel_id=panel["id"],
        kind="sql",
        sql="delete from orders",
        query_string="",
    )
    assert write["ok"] is False


@pytest.mark.asyncio
async def test_page_data_grants_no_access_the_app_does_not_already_have(datasette):
    await datasette.invoke_startup()
    registry = Registry(datasette)
    # This app has no database on its allow-list
    app = await registry.create_stored_app(
        actor_id="alice", name="No access", description="", html="", is_private=False
    )
    panel = await create_panel(
        datasette, ALICE, app["id"], "table", "shop", "orders", share_filters=True
    )
    refused = await _page(
        datasette, app, panel_id=panel["id"], kind="rows", query_string=""
    )
    assert refused == {
        "ok": False,
        "error": "This app is not allowed to query that database",
    }
    # A panel id belonging to a different app is not accepted either
    other = await _app(datasette)
    mismatch = await _page(
        datasette, other, panel_id=panel["id"], kind="rows", query_string=""
    )
    assert "only available to pinned panels" in mismatch["error"]
    # Bob cannot use Alice's personal panel
    theirs = await create_panel(
        datasette, ALICE, other["id"], "table", "shop", "orders", share_filters=True
    )
    assert (
        await _page(datasette, other, actor=BOB, panel_id=theirs["id"], kind="rows")
    )["ok"] is False
    assert (await _page(datasette, other, panel_id=theirs["id"], kind="rows"))[
        "ok"
    ] is True
