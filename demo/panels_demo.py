"""Build a small demo instance that shows off pinned app panels.

    uv run python demo/panels_demo.py /tmp/panels-demo
    uv run datasette /tmp/panels-demo/shop.db \\
        --internal /tmp/panels-demo/internal.db \\
        -c /tmp/panels-demo/datasette.yaml --root

Then open the homepage, the shop database page and the shop/orders table.
Sign in with the --root link to add, resize and remove panels.
"""

import asyncio
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

from datasette.app import Datasette

from datasette_apps import Registry
from datasette_apps.panel_kit import (
    get_panel_template,
    render_panel_template,
    suggest_panel_config,
)
from datasette_apps.panels import create_panel

ACTOR = {"id": "root"}
CONFIG_YAML = """\
permissions:
  create-app:
    id: "*"
  view-app: true
  apps-share-panels:
    id: root
"""


def build_shop(path):
    random.seed(7)
    conn = sqlite3.connect(path)
    conn.executescript("""
        drop table if exists orders;
        create table orders (
            id integer primary key,
            customer text,
            status text,
            channel text,
            total real,
            created_at text
        );
        """)
    customers = [
        "Ada Lovelace",
        "Grace Hopper",
        "Alan Turing",
        "Katherine Johnson",
        "Edsger Dijkstra",
        "Barbara Liskov",
        "Donald Knuth",
        "Margaret Hamilton",
    ]
    statuses = ["Delivered"] * 12 + ["Shipped"] * 4 + ["Processing"] * 2 + ["Returned"]
    channels = ["Web"] * 6 + ["Mobile app"] * 4 + ["Wholesale"] * 2 + ["Phone"]
    end = date(2026, 11, 30)
    rows = []
    for offset in range(420, -1, -1):
        day = end - timedelta(days=offset)
        # Growing trend, quieter weekends, a few dead days
        base = 2 + (420 - offset) / 60
        if day.weekday() >= 5:
            base *= 0.45
        count = 0 if random.random() < 0.04 else max(0, int(random.gauss(base, 1.6)))
        for _ in range(count):
            rows.append(
                (
                    random.choice(customers),
                    random.choice(statuses),
                    random.choice(channels),
                    round(random.lognormvariate(3.6, 0.6), 2),
                    f"{day.isoformat()} {random.randint(8, 21):02d}:{random.randint(0, 59):02d}",
                )
            )
    conn.executemany(
        "insert into orders (customer, status, channel, total, created_at) values (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


async def seed(directory):
    shop = directory / "shop.db"
    build_shop(shop)
    internal = directory / "internal.db"
    if internal.exists():
        internal.unlink()
    (directory / "datasette.yaml").write_text(CONFIG_YAML)
    datasette = Datasette(
        [str(shop)],
        internal=str(internal),
        config_dir=None,
        config={"permissions": {"view-app": True, "apps-share-panels": {"id": "root"}}},
    )
    await datasette.invoke_startup()
    registry = Registry(datasette)

    async def panel(template_id, name, target, table=None, config=None, **size):
        suggested = await suggest_panel_config(datasette, template_id, "shop", "orders")
        suggested.update(config or {})
        app = await registry.create_stored_app(
            actor_id="root",
            name=name,
            description=f"{get_panel_template(template_id).name} panel",
            html=render_panel_template(template_id, suggested, title=name),
            is_private=False,
            sql_databases=["shop"],
        )
        template = get_panel_template(template_id)
        await create_panel(
            datasette,
            ACTOR,
            app["id"],
            target,
            "shop",
            table,
            width=size.get("width", template.width),
            height=size.get("height", template.height),
            is_shared=True,
            # Table panels follow whatever filters the page has applied
            share_filters=target == "table",
        )

    # --- shop/orders table: a full dashboard strip ---
    await panel(
        "stat",
        "Orders",
        "table",
        "orders",
        width=3,
        config={
            "label": "Orders, last 30 days",
            "sql": (
                "select\n"
                "  count(*) filter (where created_at >= date(m, '-30 days')) as value,\n"
                "  count(*) filter (where created_at < date(m, '-30 days')\n"
                "    and created_at >= date(m, '-60 days')) as previous\n"
                "from {{filtered}}, (select max(created_at) as m from orders)"
            ),
            "delta_label": "vs prior 30 days",
        },
    )
    await panel(
        "stat",
        "Revenue",
        "table",
        "orders",
        width=3,
        config={
            "label": "Revenue, last 30 days",
            "prefix": "$",
            "sql": (
                "select\n"
                "  sum(total) filter (where created_at >= date(m, '-30 days')) as value,\n"
                "  sum(total) filter (where created_at < date(m, '-30 days')\n"
                "    and created_at >= date(m, '-60 days')) as previous\n"
                "from {{filtered}}, (select max(created_at) as m from orders)"
            ),
            "delta_label": "vs prior 30 days",
        },
    )
    await panel(
        "stat",
        "Return rate",
        "table",
        "orders",
        width=3,
        config={
            "label": "Return rate",
            "format": "percent",
            "up_is_good": False,
            "sql": (
                "select\n"
                "  avg(status = 'Returned') filter (where created_at >= date(m, '-90 days')) as value,\n"
                "  avg(status = 'Returned') filter (where created_at < date(m, '-90 days')) as previous\n"
                "from {{filtered}}, (select max(created_at) as m from orders)"
            ),
            "delta_label": "last 90 days vs before",
        },
    )
    await panel(
        "meter",
        "Quarter goal",
        "table",
        "orders",
        width=3,
        config={
            "label": "Orders this quarter",
            "sql": "select count(*) as value from {{filtered}} where created_at >= '2026-10-01'",
            "goal": 500,
            "caption": "of quarterly goal",
        },
    )
    await panel(
        "timeseries", "Orders per month", "table", "orders", width=8, height=230
    )
    await panel(
        "bars",
        "Orders by channel",
        "table",
        "orders",
        width=4,
        height=230,
        config={
            "label": "Orders by channel",
            "sql": "select channel as label, count(*) as value\nfrom {{filtered}}\ngroup by 1 order by 2 desc",
        },
    )
    await panel("calendar", "Order activity", "table", "orders")

    # --- shop database page ---
    await panel(
        "note",
        "About the shop database",
        "database",
        config={
            "label": "About this database",
            "text": (
                "Synthetic orders for the panels demo, regenerated by `demo/panels_demo.py`.\n\n"
                "- **orders** has one row per order, newest last\n"
                "- Totals are in US dollars"
            ),
        },
    )
    await panel(
        "stat-row",
        "Shop at a glance",
        "database",
        config={
            "sql": (
                'select count(*) as "Orders", count(distinct customer) as "Customers",\n'
                '  round(sum(total)) as "Revenue ($)", round(avg(total), 2) as "Avg order ($)",\n'
                '  max(date(created_at)) as "Latest order"\nfrom orders'
            ),
        },
    )
    await panel(
        "sparkline",
        "Weekly orders",
        "database",
        width=4,
        height=200,
        config={
            "label": "Orders per week",
            "sql": (
                "select x, value from (\n"
                "  select strftime('%Y-%W', created_at) as x, count(*) as value\n"
                "  from orders group by 1 order by 1 desc limit 26 offset 1\n"
                ") order by x"
            ),
            "delta_label": "vs previous week",
        },
    )
    await panel(
        "recent",
        "Latest orders",
        "database",
        width=4,
        height=200,
        config={
            "sql": (
                "select customer as title, status || ' · $' || total as subtitle, created_at as meta\n"
                "from orders order by created_at desc limit 6"
            ),
        },
    )
    await panel(
        "bars",
        "Orders by status",
        "database",
        width=4,
        height=200,
        config={
            "label": "Orders by status",
            "sql": "select status as label, count(*) as value\nfrom orders\ngroup by 1 order by 2 desc",
        },
    )

    # --- homepage ---
    await panel(
        "stat-row",
        "Shop headline numbers",
        "homepage",
        config={
            "sql": (
                'select count(*) as "Orders", round(sum(total)) as "Revenue ($)",\n'
                '  count(distinct customer) as "Customers"\nfrom orders'
            ),
        },
        width=8,
    )
    await panel(
        "sparkline",
        "Orders trend",
        "homepage",
        width=4,
        height=96,
        config={
            "label": "Orders per month",
            "delta_label": "vs previous month",
        },
    )
    print(f"Demo written to {directory}")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "panels-demo")
    target.mkdir(parents=True, exist_ok=True)
    asyncio.run(seed(target))
