const Panel = (() => {
  const root = document.getElementById("panel");
  const context = (window.datasette && datasette.context) || {target: "app"};
  const quoteName = name => '"' + String(name).replace(/"/g, '""') + '"';

  // {{table}} in CONFIG.sql becomes the table this panel is pinned to, so
  // one app can be pinned to several tables.
  const expandSql = sql => String(sql).replace(/\{\{\s*table\s*\}\}/g, () => {
    if (!context.table) {
      throw new Error("This panel uses {{table}}: pin it to a table page");
    }
    return quoteName(context.table);
  });

  async function query(sql, params) {
    const database = CONFIG.database || context.database;
    if (!database) {
      throw new Error("Set CONFIG.database, or pin this panel to a database or table page");
    }
    const result = await datasette.query(database, expandSql(sql), params || {});
    return result.rows;
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    for (const child of children.flat()) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child);
    }
    return node;
  }

  function svg(tag, attrs, ...children) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (key === "text") node.textContent = value;
      else node.setAttribute(key, value);
    }
    node.append(...children.flat().filter(Boolean));
    return node;
  }

  // Add children to the panel, skipping anything null or false
  function put(...children) {
    root.append(...children.flat().filter(child => child !== null && child !== undefined && child !== false));
  }

  const isNumber = value => typeof value === "number" && isFinite(value);
  const compact = new Intl.NumberFormat("en", {notation: "compact", maximumFractionDigits: 1});

  // 1,284 / 12.9K / 4.2M, wrapped in CONFIG.prefix and CONFIG.suffix.
  // Text values (dates, names) pass through untouched.
  function number(value, options) {
    if (value === null || value === undefined || value === "") return "–";
    if (!isNumber(value)) return date(value);
    const opts = options || {};
    const abs = Math.abs(value);
    let text;
    if (CONFIG.format === "percent") {
      text = (value * 100).toLocaleString("en", {maximumFractionDigits: 1}) + "%";
    } else if (abs >= 10000 && CONFIG.format !== "full" && !opts.full) {
      text = compact.format(value);
    } else {
      text = value.toLocaleString("en", {maximumFractionDigits: abs < 10 ? 2 : abs < 100 ? 1 : 0});
    }
    return (CONFIG.prefix || "") + text + (CONFIG.suffix || "");
  }

  // "2026-03" -> "Mar 2026", "2026-03-09" -> "Mar 9, 2026"; other text unchanged
  function date(value) {
    const text = String(value === null || value === undefined ? "" : value);
    const match = text.match(/^(\d{4})-(\d{2})(?:-(\d{2}))?(?:[T ](\d{2}):(\d{2}))?/);
    if (!match) return text;
    const months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const month = months[Number(match[2]) - 1] || match[2];
    if (!match[3]) return month + " " + match[1];
    return month + " " + Number(match[3]) + ", " + match[1];
  }

  // Signed change between two numbers: {text: "+12.4%", direction: 1}
  function delta(current, previous) {
    if (!isNumber(current) || !isNumber(previous) || previous === 0) return null;
    const change = (current - previous) / Math.abs(previous);
    const sign = change > 0 ? "+" : change < 0 ? "−" : "";
    return {
      text: sign + Math.abs(change * 100).toLocaleString("en", {maximumFractionDigits: 1}) + "%",
      direction: Math.sign(change)
    };
  }

  function deltaLine(current, previous) {
    const change = delta(current, previous);
    if (!change) return null;
    const upIsGood = CONFIG.up_is_good !== false;
    const good = change.direction === 0 ? null : (change.direction > 0) === upIsGood;
    const arrow = change.direction > 0 ? "▲ " : change.direction < 0 ? "▼ " : "";
    return el("div", {class: "pk-delta" + (good === null ? "" : good ? " pk-good" : " pk-bad")},
      el("b", {text: arrow + change.text}),
      CONFIG.delta_label ? " " + CONFIG.delta_label : null);
  }

  const tip = el("div", {class: "pk-tip", hidden: ""});
  document.body.append(tip);
  function showTip(event, label, value) {
    tip.textContent = "";
    if (label !== null && label !== undefined && label !== "") tip.append(el("span", {text: label}));
    tip.append(String(value));
    tip.hidden = false;
    const box = tip.getBoundingClientRect();
    let x = event.clientX + 10;
    if (x + box.width > innerWidth - 4) x = event.clientX - box.width - 10;
    let y = event.clientY - box.height - 8;
    if (y < 2) y = event.clientY + 12;
    tip.style.left = Math.max(2, x) + "px";
    tip.style.top = y + "px";
  }
  function hideTip() { tip.hidden = true; }
  function hover(node, label, value) {
    node.addEventListener("mousemove", event => showTip(event, label, value));
    node.addEventListener("mouseleave", hideTip);
  }

  function state(kind, message) {
    root.textContent = "";
    if (kind === "loading") {
      root.append(el("div", {class: "pk-skeleton", "aria-label": "Loading"}, el("i"), el("i"), el("i")));
      return;
    }
    root.append(el("div", {class: "pk-state" + (kind === "error" ? " pk-error" : ""), role: kind === "error" ? "alert" : null},
      kind === "error" ? "⚠ " + message : message));
  }

  function niceTicks(max, count) {
    if (!(max > 0)) return [0, 1];
    const rough = max / count;
    const power = Math.pow(10, Math.floor(Math.log10(rough)));
    const step = [1, 2, 5, 10].map(m => m * power).find(s => s >= rough);
    const ticks = [];
    for (let tick = 0; tick < max + step - 1e-9; tick += step) ticks.push(Number(tick.toPrecision(12)));
    return ticks;
  }

  // Line with a soft area wash, an end dot and a crosshair tooltip.
  // points: [{x, value}]. options.axes adds gridlines and tick labels;
  // without it you get a bare sparkline.
  function lineChart(container, points, options) {
    const opts = options || {};
    const width = Math.max(40, container.clientWidth);
    const height = Math.max(24, container.clientHeight);
    const values = points.map(point => Number(point.value) || 0);
    const peak = Math.max(...values, 0);
    const ticks = opts.axes ? niceTicks(peak, height < 150 ? 2 : 4) : [0, peak || 1];
    const top = ticks[ticks.length - 1] || 1;
    const tickLabels = ticks.map(tick => number(tick));
    const margin = opts.axes
      ? {top: 8, right: 10, bottom: 20, left: 8 + 6.4 * Math.max(...tickLabels.map(label => label.length))}
      : {top: 6, right: 6, bottom: 4, left: 2};
    const innerWidth = Math.max(10, width - margin.left - margin.right);
    const innerHeight = Math.max(10, height - margin.top - margin.bottom);
    const xAt = index => margin.left + (points.length < 2 ? innerWidth / 2 : innerWidth * index / (points.length - 1));
    const yAt = value => margin.top + innerHeight - innerHeight * value / top;
    const chart = svg("svg", {class: "pk-chart", width: width, height: height, role: "img",
      "aria-label": opts.label || "Trend chart"});
    if (opts.axes) {
      ticks.forEach((tick, index) => {
        chart.append(svg("line", {class: "pk-grid", x1: margin.left, x2: width - margin.right, y1: yAt(tick), y2: yAt(tick)}));
        chart.append(svg("text", {x: margin.left - 6, y: yAt(tick) + 4, "text-anchor": "end", text: tickLabels[index]}));
      });
      // Evenly spaced x labels, always including both ends, never closer
      // together than a label is wide
      const labelCount = Math.max(2, Math.min(points.length, Math.floor(innerWidth / 110) + 1));
      const last = points.length - 1;
      let indexes = [];
      for (let i = 0; i < labelCount; i += 1) {
        indexes.push(Math.round(last * i / Math.max(1, labelCount - 1)));
      }
      indexes = [...new Set(indexes)].filter(index =>
        index === 0 || index === last || (xAt(index) - xAt(0) > 80 && xAt(last) - xAt(index) > 80));
      for (const index of indexes) {
        chart.append(svg("text", {x: xAt(index), y: height - 4,
          "text-anchor": index === 0 ? "start" : index === last ? "end" : "middle",
          text: date(points[index].x)}));
      }
    }
    const path = values.map((value, index) => (index ? "L" : "M") + xAt(index).toFixed(1) + " " + yAt(value).toFixed(1)).join(" ");
    if (points.length > 1) {
      chart.append(svg("path", {class: "pk-area", d: path + " L" + xAt(points.length - 1).toFixed(1) + " " + yAt(0) + " L" + xAt(0).toFixed(1) + " " + yAt(0) + " Z"}));
      chart.append(svg("path", {class: "pk-line", d: path}));
    }
    const cross = svg("line", {class: "pk-cross", y1: margin.top, y2: margin.top + innerHeight, visibility: "hidden"});
    const dot = svg("circle", {class: "pk-dot", r: 4, cx: xAt(points.length - 1), cy: yAt(values[values.length - 1])});
    chart.append(cross, dot);
    chart.addEventListener("mousemove", event => {
      const x = event.clientX - chart.getBoundingClientRect().left;
      const index = Math.max(0, Math.min(points.length - 1,
        Math.round((x - margin.left) / innerWidth * (points.length - 1))));
      cross.setAttribute("x1", xAt(index));
      cross.setAttribute("x2", xAt(index));
      cross.setAttribute("visibility", "visible");
      dot.setAttribute("cx", xAt(index));
      dot.setAttribute("cy", yAt(values[index]));
      showTip(event, date(points[index].x), number(values[index], {full: true}));
    });
    chart.addEventListener("mouseleave", () => {
      cross.setAttribute("visibility", "hidden");
      dot.setAttribute("cx", xAt(points.length - 1));
      dot.setAttribute("cy", yAt(values[values.length - 1]));
      hideTip();
    });
    container.textContent = "";
    container.append(chart);
  }

  // load() fetches data once (and again every CONFIG.refresh_seconds);
  // draw(data) renders it and is called again whenever the frame is resized.
  function run(template) {
    let data;
    let loaded = false;
    const draw = () => {
      if (!loaded) return;
      root.textContent = "";
      try {
        if (template.draw(data, root) === false) state("empty", CONFIG.empty_text || "No data");
      } catch (error) {
        state("error", error.message || String(error));
      }
    };
    const load = async () => {
      try {
        data = await template.load();
        loaded = true;
        draw();
      } catch (error) {
        loaded = false;
        state("error", error.message || String(error));
      }
    };
    state("loading");
    load();
    let timer;
    addEventListener("resize", () => { clearTimeout(timer); timer = setTimeout(draw, 80); });
    const refresh = Number(CONFIG.refresh_seconds);
    if (refresh > 0) setInterval(load, Math.max(5, refresh) * 1000);
  }

  return {root, context, query, put, el, svg, number, date, delta, deltaLine, hover, showTip, hideTip, state, lineChart, run};
})();
