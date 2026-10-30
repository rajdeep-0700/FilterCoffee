"use strict";

const $ = (id) => document.getElementById(id);
const state = {
  job: null,
  busy: false,
  active: null,
  viewing: null,
  sessionId: null,
  sessionRuns: [],
  timer: null,
  toast: null,
  countryNames: new Map(),
};
let preferredCountry = "";
const names = {
  web: "Web",
  github: "GitHub",
  hn: "Hacker News",
  stackexchange: "Stack Exchange",
  bluesky: "Bluesky",
  youtube: "YouTube",
  opencli: "OpenCLI",
};
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || "";
try {
  if (token) sessionStorage.setItem("scout-session", token);
  else token = sessionStorage.getItem("scout-session") || "";
} catch {
  /* The launch URL still works when browser storage is disabled. */
}
if (location.hash) history.replaceState(null, "", location.pathname);

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}
function icon(name) {
  const element = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  element.setAttribute("aria-hidden", "true");
  const use = document.createElementNS(element.namespaceURI, "use");
  use.setAttribute("href", `#i-${name}`);
  element.append(use);
  return element;
}
function notify(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  clearTimeout(state.toast);
  state.toast = setTimeout(() => {
    $("toast").hidden = true;
  }, 4500);
}
async function api(path, payload, method) {
  const options = { headers: { "X-Scout-Token": token }, cache: "no-store" };
  if (payload !== undefined) {
    options.method = method || "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(payload);
  } else if (method) {
    options.method = method;
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error(
      "FilterCoffee is disconnected. Keep the launcher window open, or reopen the app.",
    );
  }
  const value = response.status === 204 ? null : await response.json();
  if (!response.ok) throw new Error(value.error || "The request failed.");
  return value;
}
function safeLink(value, label) {
  try {
    const url = new URL(value);
    if (
      !["http:", "https:"].includes(url.protocol) ||
      url.username ||
      url.password
    )
      return node("span", label || value);
    const link = node("a", label || value);
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch {
    return node("span", label || value || "Not available");
  }
}
function display(value) {
  if (value === null || value === undefined) return "Not verified";
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value);
}
function label(value) {
  return value.replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());
}
function valueNode(value, field) {
  if (value === null || value === undefined)
    return node("span", "Not verified", "empty-value");
  if (typeof value === "string" && /^https?:\/\//i.test(value))
    return safeLink(value);
  const text = Array.isArray(value)
    ? value.map(display).join("\n")
    : display(value);
  if (text.length > 450 || /caption|transcript|subtitle/i.test(field)) {
    const details = node("details");
    details.append(
      node(
        "summary",
        `Read ${label(field).toLowerCase()} (${text.length.toLocaleString()} characters)`,
      ),
      node("p", text),
    );
    return details;
  }
  if (Array.isArray(value)) {
    const list = node("ul");
    value.forEach((item) => list.append(node("li", display(item))));
    return list;
  }
  return node("span", text);
}
function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $("theme").setAttribute(
    "aria-label",
    `Switch to ${theme === "dark" ? "light" : "dark"} mode`,
  );
  $("theme").title = $("theme").getAttribute("aria-label");
  try {
    localStorage.setItem("scout-theme", theme);
  } catch {
    /* Optional preference. */
  }
}
function selectedSources() {
  return Array.from(
    document.querySelectorAll(".source-toggle:checked"),
    (input) => input.value,
  );
}
function scopeChanged() {
  const mode = $("mode").value;
  $("source-options").hidden = mode !== "sources";
  $("url-options").hidden = mode !== "url";
  $("import-options").hidden = mode !== "manual";
  $("browser").disabled = mode === "manual" || mode === "sources";
  if (mode !== "sources") $("opencli").checked = false;
  const help = {
    auto: "Look up a page or video, or compare results across relevant websites.",
    sources:
      "Search the chosen sources and combine their hits into one result. YouTube runs separately.",
    url: "Read one public page or video. URLs and redirects still pass through the existing safety checks.",
    manual:
      "Read a file you saved yourself. The citation URL is metadata only and is not fetched.",
  };
  $("scope-help").textContent = help[mode];
  const sources = selectedSources();
  if (mode === "sources" && $("opencli").checked) sources.push("opencli");
  $("composer-scope").textContent =
    mode === "sources"
      ? sources.map((s) => names[s]).join(" + ") || "Choose a source"
      : {
          auto: "Auto · lookup or product comparison",
          url: "Direct URL · one page",
          manual: "Saved file · no page fetch",
        }[mode];
  savePreferences();
}
function savePreferences() {
  try {
    localStorage.setItem(
      "scout-preferences",
      JSON.stringify({
        mode: $("mode").value,
        sources: selectedSources(),
        browser: $("browser").value,
        max_items: $("max-items").value,
        country: $("country").value.trim(),
        source_limit: $("source-limit").value,
      }),
    );
  } catch {
    /* Browser preferences are optional, not the source of truth for secrets. */
  }
}
function setBusy(busy) {
  state.busy = busy;
  $("submit").disabled = busy;
  $("submit").setAttribute(
    "aria-label",
    busy ? "Research in progress" : "Start research",
  );
  $("new-run").disabled = busy;
  document
    .querySelectorAll(".settings input, .settings select")
    .forEach((element) => {
      element.disabled = busy || element.dataset.unavailable === "true";
    });
  if (!busy) scopeChanged();
}
function showView(name, focus = false) {
  ["response", "table", "evidence"].forEach((key) => {
    $(key).hidden = key !== name;
    $(`${key}-tab`).setAttribute("aria-selected", String(key === name));
    $(`${key}-tab`).tabIndex = key === name ? 0 : -1;
  });
  if (focus) $(`${name}-tab`).focus();
}
function clearResult() {
  ["response", "table", "evidence", "metrics", "diagnostic-content"].forEach(
    (id) => $(id).replaceChildren(),
  );
  ["error", "exports", "diagnostics"].forEach((id) => {
    $(id).hidden = true;
  });
}
function renderActivity(job) {
  $("activity").replaceChildren();
  const grouped = [];
  (job.events || []).forEach((event) => {
    if (grouped.at(-1)?.stage === event.stage)
      grouped[grouped.length - 1] = event;
    else grouped.push(event);
  });
  grouped.forEach((event, index) => {
    const item = node("li");
    const mark = icon(
      index === grouped.length - 1 && job.status === "running"
        ? "info"
        : "check",
    );
    mark.classList.add("tick");
    const content = node("div");
    content.append(node("strong", event.stage), node("small", event.detail));
    item.append(mark, content);
    $("activity").append(item);
  });
  if (!grouped.length)
    $("activity").append(node("li", "Preparing the request…", "muted"));
}
function renderJob(job) {
  state.job = job;
  state.viewing = job.id;
  $("welcome").hidden = true;
  $("run-heading").hidden = false;
  $("question").textContent = job.request;
  $("run-label").textContent =
    `Research / ${new Date(job.created * 1000).toLocaleString()}`;
  $("run-status").hidden = job.status !== "running";
  renderActivity(job);
  if (job.status === "running") {
    const latest = job.events?.at(-1);
    const stage = latest?.stage || "Starting research";
    if ($("run-status").dataset.stage !== stage) {
      $("run-status").replaceChildren(
        node("div", stage),
        node("div", null, "loading-line"),
        node(
          "small",
          "Your run is active. It may take a few minutes; no need to submit it again.",
        ),
      );
      $("run-status").dataset.stage = stage;
    }
    $("footer-status").textContent =
      `${stage} · ${Math.max(0, Math.floor(Date.now() / 1000 - job.created))}s`;
    $("query-meta").textContent = "In progress · free-tier providers";
    return;
  }
  clearResult();
  $("footer-status").textContent =
    job.status === "error"
      ? "Run failed"
      : job.status === "empty"
        ? "No verified rows returned"
        : "Research complete";
  if (job.error) {
    $("error").textContent = job.error;
    $("error").hidden = false;
  }
  const result = job.result;
  if (!result) return;
  const meta = result.meta || {};
  const items = result.items || [];
  const fields = result.plan?.fields || [];
  if (result.research) {
    const coverage = result.research;
    $("footer-status").textContent = coverage.partial
      ? "Partial comparison"
      : "Comparison complete";
    const note = node("section", null, "notice");
    note.append(
      node(
        "strong",
        coverage.partial ? "Partial research results" : "Product comparison",
      ),
    );
    note.append(
      node(
        "p",
        `${coverage.searches + coverage.fetches} / ${coverage.limit} requests · ${coverage.domains.length} websites · ${state.countryNames.get(coverage.country) || coverage.country || "Market unspecified"}`,
      ),
    );
    note.append(
      node(
        "p",
        "Search breakdown: " +
          coverage.searches +
          " searches · " +
          coverage.fetches +
          " pages · " +
          (coverage.discovery_searches || 0) +
          " broad searches",
      ),
    );
    if (coverage.partial) note.append(node("p", coverage.stop_reason));
    if (coverage.constraints && coverage.constraints.length) {
      const operators = {
        min: "at least",
        max: "at most",
        equals: "exactly",
        contains: "includes",
      };
      const required = coverage.constraints
        .map(
          (item) =>
            item.field.replaceAll("_", " ") +
            " " +
            (operators[item.operator] || item.operator) +
            " " +
            item.value +
            (item.unit ? " " + item.unit : ""),
        )
        .join(" · ");
      note.append(node("p", "Required: " + required));
    }
    note.append(
      node(
        "p",
        "Ranked by overall fit to your requirements. Coverage is limited to the pages checked; prices and availability can change.",
      ),
    );
    const searchDetails = node("details");
    const queries = coverage.search_queries || [];
    searchDetails.append(
      node("summary", "Search queries used (" + queries.length + ")"),
    );
    queries.forEach((entry) =>
      searchDetails.append(
        node("p", (entry.domain || "Open web") + ": " + entry.query),
      ),
    );
    note.append(searchDetails);
    const detail = node("details");
    detail.append(node("summary", "Websites checked and excluded matches"));
    coverage.domains.forEach((domain) =>
      detail.append(
        node(
          "p",
          `${domain.domain}: ${domain.searches} searches · ${domain.pages_read} pages read`,
        ),
      ),
    );
    coverage.excluded.forEach((entry) => {
      const row = node("p");
      row.append(node("strong", entry.title || "Unmatched listing"));
      if (entry.url) {
        row.append(
          document.createTextNode(" · "),
          safeLink(entry.url, "Source"),
        );
      }
      row.append(document.createTextNode(" — " + entry.reasons.join("; ")));
      const observed = Object.entries(entry.observed || {}).map(
        ([field, fact]) =>
          field.replaceAll("_", " ") +
          ": " +
          fact.value +
          (fact.unit ? " " + fact.unit : ""),
      );
      if (observed.length)
        row.append(
          document.createTextNode(" · Observed: " + observed.join(", ")),
        );
      detail.append(row);
    });
    note.append(detail);
    $("response").append(note);
  }
  $("query-meta").replaceChildren(
    node("span", meta.fetch_tier || "Source"),
    node("span", `${items.length} row${items.length === 1 ? "" : "s"}`),
    node("span", `${job.elapsed}s`),
  );
  if (meta.cache_hit) $("query-meta").append(node("span", "Cached extraction"));
  const source = safeLink(result.url, "Open source");
  if (source.tagName === "A" && !result.url.startsWith("https://scout.local/"))
    $("query-meta").append(source);
  const head = node("div", null, "answer-head");
  const emblem = node("span", null, "scout-symbol");
  emblem.append(icon("cup"));
  head.append(
    emblem,
    node("span", "FilterCoffee"),
    node("span", `${items.length} row${items.length === 1 ? "" : "s"}`, "pill"),
  );
  $("response").append(head);
  $("response").append(
    node(
      "p",
      items.length
        ? "The extracted values are below. Expand longer text and supporting quotes without making another model call."
        : result.research
          ? "No products passed evidence and requirement checks on the pages read. Expand Websites checked and excluded matches below to see what was rejected and why."
          : "No verified rows were returned. Review the source and warnings below, or try a more specific request.",
      "summary",
    ),
  );
  items.forEach((item, index) => {
    const card = node("article", null, "result");
    const header = node("div", null, "result-heading");
    header.append(node("span", `Result ${String(index + 1).padStart(2, "0")}`));
    if (item.evidence?.length) {
      const badge = node("span", "Evidence attached", "badge");
      badge.prepend(icon("check"));
      header.append(badge);
    }
    card.append(header);
    const titleKey = fields.find(
      (field) =>
        /^(title|name|headline|project_name|video_title)$/.test(field.name) &&
        item.data?.[field.name],
    );
    if (titleKey) card.append(node("h2", display(item.data[titleKey.name])));
    const list = node("dl", null, "fields");
    fields.forEach((field) => {
      if (field === titleKey) return;
      const row = node("div", null, "field");
      const value = node("dd");
      value.append(valueNode(item.data?.[field.name], field.name));
      row.append(node("dt", label(field.name)), value);
      list.append(row);
    });
    card.append(list);
    const foot = node("div", null, "result-footer");
    const details = node("details");
    details.append(
      node("summary", `Supporting evidence (${item.evidence?.length || 0})`),
    );
    if (result.research && item.data?.source_url)
      details.append(safeLink(item.data.source_url, "Read this listing"));
    (item.evidence || []).forEach((entry) =>
      details.append(
        node("strong", label(entry.field)),
        node(
          "blockquote",
          entry.quote || entry.selector || "Screenshot-derived evidence",
        ),
      ),
    );
    foot.append(
      node(
        "span",
        `Extraction confidence: ${Math.round((item.confidence || 0) * 100)}%`,
      ),
    );
    card.append(foot);
    $("response").append(card, details);
  });
  renderTable(items, fields);
  renderEvidence(items);
  const metrics = [
    [items.length, "Rows returned"],
    [job.elapsed + "s", "Elapsed"],
    [meta.model_calls?.length || 0, "Model attempts"],
    ["$" + Number(meta.estimated_cost_usd || 0).toFixed(4), "Recorded cost*"],
  ];
  metrics.forEach(([value, title]) => {
    const metric = node("div", value, "metric");
    metric.append(node("small", title));
    $("metrics").append(metric);
  });
  $("metrics").append(
    node(
      "p",
      "*Gemini is recorded as zero for free-tier testing; confirm your Google project billing tier.",
      "subtle",
    ),
  );
  const coverage = meta.sources || [];
  if (coverage.length) {
    const section = node("details");
    section.append(node("summary", "Source coverage"));
    coverage.forEach((report) =>
      section.append(
        node(
          "p",
          `${names[report.name] || report.name}: ${report.status} · ${report.hits} hits`,
        ),
      ),
    );
    $("response").append(section);
  }
  const warnings = meta.warnings || [];
  if (warnings.length) {
    const detail = node("details");
    detail.append(
      node("summary", `${warnings.length} warnings and source notes`),
    );
    warnings.forEach((warning) => detail.append(node("p", warning, "warning")));
    $("response").append(detail);
  }
  (meta.model_calls || []).forEach((call) => {
    const entry = node(
      "div",
      `${label(call.stage)} · ${call.ok ? "OK" : "Failed attempt"}`,
      "call",
    );
    entry.append(
      node("span", call.model),
      node(
        "span",
        `${call.prompt_tokens} in / ${call.completion_tokens} out · ${(call.latency_ms / 1000).toFixed(1)}s`,
      ),
    );
    $("diagnostic-content").append(entry);
  });
  warnings.forEach((warning) =>
    $("diagnostic-content").append(node("p", warning, "warning")),
  );
  $("diagnostics").hidden = false;
  $("exports").hidden = false;
}
function renderTable(items, fields) {
  if (!items.length) {
    $("table").append(node("p", "No rows to display.", "summary"));
    return;
  }
  const wrap = node("div", null, "table-wrap");
  const table = node("table");
  const thead = node("thead"),
    tr = node("tr"),
    body = node("tbody");
  fields.forEach((field) => {
    const th = node("th", label(field.name));
    th.scope = "col";
    tr.append(th);
  });
  thead.append(tr);
  items.forEach((item) => {
    const row = node("tr");
    fields.forEach((field) => {
      const td = node("td");
      td.append(valueNode(item.data?.[field.name], field.name));
      row.append(td);
    });
    body.append(row);
  });
  table.append(thead, body);
  wrap.append(table);
  $("table").append(
    node(
      "p",
      "Not verified means the source did not support that value. Wide tables can be scrolled horizontally.",
      "summary",
    ),
    wrap,
  );
}
function renderEvidence(items) {
  $("evidence").append(
    node(
      "p",
      "Quotes support the extracted values, not the factual accuracy of everything the original source says.",
      "summary",
    ),
  );
  items.forEach((item, index) => {
    const details = node("details");
    if (state.job?.result?.research && item.data?.source_url)
      details.append(safeLink(item.data.source_url, "Read this listing"));
    details.append(
      node(
        "summary",
        `Result ${index + 1} · ${item.evidence?.length || 0} evidence entries`,
      ),
    );
    (item.evidence || []).forEach((entry) =>
      details.append(
        node("strong", label(entry.field)),
        node(
          "blockquote",
          entry.quote || entry.selector || "Screenshot-derived evidence",
        ),
      ),
    );
    $("evidence").append(details);
  });
}
function renderSessionTurns(runs) {
  const nav = $("session-turns");
  nav.replaceChildren();
  nav.hidden = runs.length < 2;
  runs.forEach((run, index) => {
    const button = node(
      "button",
      null,
      "session-turn" + (run.id === state.viewing ? " active" : ""),
    );
    const count = run.result?.items?.length || 0;
    button.append(
      node("span", "Turn " + (run.turn || index + 1), "session-turn-number"),
      node("span", run.request, "session-turn-request"),
      node(
        "small",
        run.status + " · " + count + (count === 1 ? " result" : " results"),
      ),
    );
    button.setAttribute("aria-current", String(run.id === state.viewing));
    button.addEventListener("click", () => {
      state.viewing = run.id;
      clearResult();
      showView("response");
      renderJob(run);
      renderSessionTurns(state.sessionRuns);
    });
    nav.append(button);
  });
}

async function openSession(sessionId, runId = null) {
  const saved = await api("/api/sessions/" + sessionId);
  state.sessionId = sessionId;
  state.sessionRuns = saved.runs;
  const selected = runId && saved.runs.find((run) => run.id === runId);
  const run = selected || saved.runs.at(-1);
  const savedCountry = run?.result?.research?.country || run?.country || "";
  const countryEntry = Array.from(state.countryNames.entries()).find(
    ([code, name]) =>
      code === savedCountry ||
      name.toLowerCase() === savedCountry.toLowerCase(),
  );
  $("country").value = countryEntry?.[0] || "";
  scopeChanged();
  renderSessionTurns(saved.runs);
  if (run) {
    clearResult();
    showView("response");
    renderJob(run);
    renderSessionTurns(saved.runs);
  } else {
    state.viewing = null;
    $("welcome").hidden = false;
    $("run-heading").hidden = true;
  }
  return saved;
}

async function loadHistory() {
  const sessions = await api("/api/history");
  $("history").replaceChildren();
  if (!sessions.length)
    $("history").append(node("p", "Your research will appear here.", "subtle"));
  sessions.forEach((session) => {
    const row = node("div", null, "history-entry");
    const button = node(
      "button",
      null,
      "history" + (session.id === state.sessionId ? " active" : ""),
    );
    const text = node("span", session.request);
    text.append(
      node(
        "time",
        new Date(session.updated * 1000).toLocaleDateString() +
          " · " +
          session.turn_count +
          (session.turn_count === 1 ? " turn" : " turns") +
          " · " +
          session.status,
      ),
    );
    button.append(icon("chat"), text);
    button.title = session.request;
    button.addEventListener("click", async () => {
      try {
        await openSession(session.id);
        await loadHistory();
      } catch (error) {
        notify(error.message);
      }
    });
    const remove = node("button", null, "delete-session");
    remove.append(icon("trash"));
    remove.setAttribute("aria-label", "Delete session");
    remove.title =
      session.status === "running"
        ? "Wait for this session to finish before deleting it."
        : "Delete saved session and all its turns";
    remove.disabled = session.status === "running";
    remove.addEventListener("click", async () => {
      if (
        !window.confirm("Delete this session and all of its turns and results?")
      )
        return;
      remove.disabled = true;
      try {
        await api("/api/sessions/" + session.id, undefined, "DELETE");
        if (state.sessionId === session.id) {
          state.job = null;
          state.sessionId = null;
          state.sessionRuns = [];
          state.viewing = null;
          clearResult();
          $("session-turns").replaceChildren();
          $("session-turns").hidden = true;
          $("welcome").hidden = false;
          $("run-heading").hidden = true;
          $("run-status").hidden = true;
          $("activity").replaceChildren(
            node("li", "Steps for your next run will appear here.", "muted"),
          );
          $("footer-status").textContent = "Ready when you are";
          showView("response");
        }
        await loadHistory();
      } catch (error) {
        remove.disabled = false;
        notify(error.message);
      }
    });
    row.append(button, remove);
    $("history").append(row);
  });
  return sessions;
}
async function poll() {
  if (!state.active) return;
  try {
    const job = await api(`/api/runs/${state.active}`);
    if (state.viewing === state.active) renderJob(job);
    if (job.status === "running") state.timer = setTimeout(poll, 1200);
    else {
      state.active = null;
      setBusy(false);
      if (state.sessionId === job.session_id) {
        await openSession(job.session_id, job.id);
      }
      await loadHistory();
    }
  } catch (error) {
    $("connection").textContent = "Disconnected";
    $("footer-status").textContent = error.message;
    notify(
      "Connection lost. Reconnecting to the existing run; no duplicate request will be sent.",
    );
    state.timer = setTimeout(poll, 5000);
  }
}
async function submit(event) {
  event.preventDefault();
  if (state.busy) return;
  $("form-error").hidden = true;
  const mode = $("mode").value;
  const payload = {
    request: $("request").value.trim(),
    session_id: state.sessionId,
    mode,
    country: $("country").value.trim(),
    max_items: Number($("max-items").value),
    source_limit: Number($("source-limit").value),
    refresh: $("refresh").checked,
    sources: mode === "sources" ? selectedSources() : [],
    opencli: mode === "sources" && $("opencli").checked,
    browser:
      mode === "sources" || mode === "manual" ? "auto" : $("browser").value,
    url:
      mode === "url"
        ? $("page-url").value.trim()
        : mode === "manual"
          ? $("citation-url").value.trim()
          : "",
  };
  if (payload.opencli) payload.sources.push("opencli");
  try {
    if (!payload.request)
      throw new Error("Describe what you want FilterCoffee to find.");
    if (mode === "sources" && !payload.sources.length)
      throw new Error("Choose at least one source in the sidebar.");
    if (mode === "manual") {
      const file = $("import-file").files[0];
      if (!file)
        throw new Error("Choose a saved text, Markdown or HTML file first.");
      if (file.size > 5 * 1024 * 1024)
        throw new Error("The file must be 5 MiB or smaller.");
      payload.filename = file.name;
      payload.content = await file.text();
    }
    setBusy(true);
    const job = await api("/api/runs", payload);
    state.active = job.id;
    state.sessionId = job.session_id;
    clearResult();
    showView("response");
    await openSession(job.session_id, job.id);
    $("request").value = "";
    document.querySelector(".workspace").classList.remove("menu-open");
    $("menu-toggle").setAttribute("aria-expanded", "false");
    await loadHistory().catch(() =>
      notify("History could not refresh. Your run is still active."),
    );
    await poll();
  } catch (error) {
    if (!state.active) setBusy(false);
    $("form-error").textContent = error.message;
    $("form-error").hidden = false;
  }
}
function markdown() {
  const result = state.job?.result;
  if (!result) return "";
  const lines = [`# ${state.job.request}`, "", `Source: ${result.url}`, ""];
  if (result.research) {
    const r = result.research;
    lines.push(
      `Status: ${r.partial ? "Partial comparison" : "Comparison complete"}`,
      `Country / market: ${r.country || "Unspecified"}`,
      `Search/page requests: ${r.searches + r.fetches} / ${r.limit}`,
      `Coverage: ${r.domains.length} websites. ${r.stop_reason}`,
      "",
    );
  }
  (result.items || []).forEach((item, index) => {
    lines.push(`## Result ${index + 1}`, "");
    Object.entries(item.data || {}).forEach(([key, value]) =>
      lines.push(`### ${label(key)}`, "", display(value), ""),
    );
    (item.evidence || []).forEach((entry) =>
      lines.push(
        `> ${entry.field}: ${(entry.quote || entry.selector || "").replaceAll("\n", "\n> ")}`,
        "",
      ),
    );
  });
  return lines.join("\n");
}
function download(text, type, extension) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `scout-${state.job?.id || "result"}.${extension}`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function csv() {
  const result = state.job.result;
  const fields = result.plan.fields.map((f) => f.name);
  const extraFields = result.research
    ? ["research_partial", "research_stop_reason"]
    : [];
  const escape = (value) => {
    let text = value === null || value === undefined ? "" : display(value);
    // Keep untrusted spreadsheet text from becoming executable formulas.
    if (/^[\s]*[=+@-]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  return (
    "\ufeff" +
    [
      [...fields, ...extraFields],
      ...result.items.map((item) => [
        ...fields.map((f) => item.data[f]),
        ...(result.research
          ? [result.research.partial, result.research.stop_reason]
          : []),
      ]),
    ]
      .map((row) => row.map(escape).join(","))
      .join("\r\n")
  );
}

$("theme").addEventListener("click", () =>
  setTheme(
    document.documentElement.dataset.theme === "light" ? "dark" : "light",
  ),
);
$("menu-toggle").addEventListener("click", () => {
  const open = document
    .querySelector(".workspace")
    .classList.toggle("menu-open");
  $("menu-toggle").setAttribute("aria-expanded", String(open));
});
document
  .querySelectorAll(".settings input:not(#opencli), .settings select")
  .forEach((input) => input.addEventListener("change", scopeChanged));
document.querySelectorAll(".source-toggle").forEach((input) =>
  input.addEventListener("change", () => {
    if (input.checked && input.value === "youtube") {
      document.querySelectorAll(".source-toggle").forEach((other) => {
        if (other !== input) other.checked = false;
      });
      $("opencli").checked = false;
    } else if (input.checked)
      document.querySelector('.source-toggle[value="youtube"]').checked = false;
    scopeChanged();
  }),
);
$("opencli").addEventListener("change", () => {
  if ($("opencli").checked) {
    $("mode").value = "sources";
    document.querySelector('.source-toggle[value="youtube"]').checked = false;
  }
  scopeChanged();
});
$("composer").addEventListener("submit", submit);
$("request").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});
document.querySelectorAll("[data-example]").forEach((button) =>
  button.addEventListener("click", () => {
    $("request").value = button.dataset.example;
    $("mode").value = "auto";
    scopeChanged();
    $("request").focus();
  }),
);
$("new-run").addEventListener("click", () => {
  state.job = null;
  state.sessionId = null;
  state.sessionRuns = [];
  state.viewing = null;
  $("session-turns").replaceChildren();
  $("session-turns").hidden = true;
  clearResult();
  $("welcome").hidden = false;
  $("run-heading").hidden = true;
  $("run-status").hidden = true;
  $("activity").replaceChildren(
    node("li", "The steps of your next run will appear here.", "muted"),
  );
  $("request").value = "";
  $("request").focus();
  showView("response");
  $("footer-status").textContent = "Ready when you are";
});
document.querySelectorAll('[role="tab"]').forEach((tab, index, tabs) => {
  tab.addEventListener("click", () => showView(tab.id.replace("-tab", "")));
  tab.addEventListener("keydown", (event) => {
    let next;
    if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
    else if (event.key === "ArrowLeft")
      next = (index + tabs.length - 1) % tabs.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = tabs.length - 1;
    else return;
    event.preventDefault();
    showView(tabs[next].id.replace("-tab", ""), true);
  });
});
async function updateQuota() {
  try {
    const quota = await api("/api/quota", {});
    const used = quota.free_requests_used,
      limit = quota.free_requests_limit;
    $("quota-value").textContent =
      limit == null
        ? "Not reported by provider"
        : `${used ?? "?"} / ${limit} used`;
    $("quota-note").textContent =
      `Live · updated ${new Date().toLocaleTimeString()}. Usage can lag.`;
    $("quota-meter").hidden = used == null || limit == null || limit <= 0;
    if (!$("quota-meter").hidden) {
      $("quota-meter").max = limit;
      $("quota-meter").value = used;
    }
  } catch (error) {
    $("quota-note").textContent = error.message;
  }
}
updateQuota();
setInterval(updateQuota, 60000);
$("copy").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(markdown());
    notify("Result copied as Markdown.");
  } catch {
    notify(
      "Clipboard permission is unavailable. Download JSON or select the result text.",
    );
  }
});
$("json-export").addEventListener("click", () => {
  if (state.job?.result)
    download(
      JSON.stringify(state.job.result, null, 2),
      "application/json",
      "json",
    );
});
$("csv-export").addEventListener("click", () => {
  if (state.job?.result) download(csv(), "text/csv;charset=utf-8", "csv");
});

async function boot() {
  try {
    setTheme(localStorage.getItem("scout-theme") || "dark");
    const prefs = JSON.parse(
      localStorage.getItem("scout-preferences") || "null",
    );
    if (prefs) {
      if (typeof prefs.country === "string")
        preferredCountry = prefs.country.slice(0, 80);
      if (["auto", "sources", "url", "manual"].includes(prefs.mode))
        $("mode").value = prefs.mode;
      if (["auto", "off", "chromium", "cloak"].includes(prefs.browser))
        $("browser").value = prefs.browser;
      if (Array.isArray(prefs.sources))
        document.querySelectorAll(".source-toggle").forEach((input) => {
          input.checked = prefs.sources.includes(input.value);
        });
      if (Number(prefs.max_items) >= 1 && Number(prefs.max_items) <= 100)
        $("max-items").value = prefs.max_items;
      if (Number(prefs.source_limit) >= 1 && Number(prefs.source_limit) <= 10)
        $("source-limit").value = prefs.source_limit;
    }
  } catch {
    setTheme("dark");
  }
  try {
    const config = await api("/api/config");
    state.countryNames = new Map(
      (config.countries || []).map((item) => [item.code, item.name]),
    );
    const country = $("country");
    country.replaceChildren(node("option", "Any country"));
    country.options[0].value = "";
    (config.countries || []).forEach((entry) => {
      const option = node("option", entry.name);
      option.value = entry.code;
      country.append(option);
    });
    const savedCountry = (config.countries || []).find(
      (entry) =>
        entry.code === preferredCountry.toUpperCase() ||
        entry.name.toLowerCase() === preferredCountry.toLowerCase(),
    );
    country.value = savedCountry?.code || "";
    scopeChanged();
    $("connection").replaceChildren(
      node("i", null, "dot"),
      node("span", "Local connection"),
    );
    $("opencli").disabled = !config.opencli.available;
    $("opencli").title = config.opencli.available
      ? ""
      : "OpenCLI is not available on this computer.";
    $("opencli").dataset.unavailable = String(!config.opencli.available);
    const cloak = document.querySelector('option[value="cloak"]');
    cloak.disabled = !config.cloak;
    if (!config.cloak) {
      cloak.textContent = "CloakBrowser · not installed";
      if ($("browser").value === "cloak") $("browser").value = "auto";
    }
    if (!config.keys.openrouter || !config.keys.gemini) {
      $("error").textContent =
        "Set OPENROUTER_KEY and GEMINI_KEY in the project's .env before starting research. Do not put API keys in the chat.";
      $("error").hidden = false;
    }
    const sessions = await loadHistory();
    const activeSession = sessions.find(
      (session) => session.status === "running",
    );
    if (activeSession) {
      const saved = await api("/api/sessions/" + activeSession.id);
      state.sessionId = activeSession.id;
      state.sessionRuns = saved.runs;
      renderSessionTurns(saved.runs);
      const activeRun = saved.runs.find((run) => run.status === "running");
      if (activeRun) {
        state.active = activeRun.id;
        state.viewing = activeRun.id;
        setBusy(true);
        renderJob(activeRun);
        await poll();
      } else if (saved.runs.length) {
        // A process restart interrupted the in-flight turn; reopen the last saved turn.
        await openSession(activeSession.id);
        await loadHistory();
      }
    }
  } catch (error) {
    $("connection").textContent = "Disconnected";
    $("error").textContent = error.message;
    $("error").hidden = false;
    $("submit").disabled = true;
  }
}
boot();
