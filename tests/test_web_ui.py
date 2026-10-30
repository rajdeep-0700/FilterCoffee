"""Real local-browser tests. No provider calls, microphone or live scraping."""

import threading

import pytest
from playwright.sync_api import sync_playwright

from scout.web import ScoutServer
from scout.web_service import JobStore, WebRequest


def sample_result(request):
    return {
        "url": "https://example.com/source",
        "instruction": request.request,
        "plan": {
            "mode": "single",
            "fields": [
                {"name": "title", "type": "string", "description": "Title"},
                {"name": "captions", "type": "string", "description": "Captions"},
                {"name": "source_url", "type": "url", "description": "Source"},
            ],
        },
        "items": [
            {
                "data": {
                    "title": "Café বাংলা <img src=x onerror=alert(1)>",
                    "captions": "Hello " * 300,
                    "source_url": None,
                },
                "evidence": [{"field": "title", "quote": "Café বাংলা"}],
                "confidence": 0.9,
            }
        ],
        "meta": {
            "fetch_tier": "manual",
            "model_calls": [],
            "warnings": ["Source note"],
            "sources": [],
            "cache_hit": False,
            "estimated_cost_usd": 0,
        },
    }


@pytest.fixture
def ui(tmp_path, monkeypatch):
    # Only installation discovery is made deterministic; no real providers are queried.
    monkeypatch.setattr(
        "scout.web.quota_status",
        lambda: {"free_requests_used": 0, "free_requests_limit": 50},
    )
    monkeypatch.setattr(
        "scout.web.capabilities",
        lambda: {
            "keys": {"openrouter": True, "gemini": True, "web": True},
            "opencli": {"available": True, "site": "hackernews", "reason": ""},
            "cloak": True,
            "countries": [
                {"code": "CA", "name": "Canada"},
                {"code": "IN", "name": "India"},
                {"code": "US", "name": "United States"},
            ],
        },
    )
    requests = []

    def work(request, emit):
        requests.append(request)
        emit("Extracting", "Offline model response")
        result = sample_result(request)
        if request.request == "Compare products":
            result["research"] = {
                "partial": True,
                "stop_reason": "Budget reached",
                "country": request.country,
                "constraints": [
                    {"field": "price", "operator": "max", "value": "80000", "unit": "INR"}
                ],
                "searches": 40,
                "fetches": 160,
                "limit": 200,
                "search_queries": [
                    {"domain": "amazon.in", "query": "site:amazon.in laptop DDR5 India"}
                ],
                "domains": [{"domain": "example.com", "searches": 2, "pages_read": 10}],
                "excluded": [
                    {
                        "title": "Test laptop",
                        "url": "https://example.com/item",
                        "reasons": ["price: unknown hard requirement"],
                    }
                ],
            }
        if request.request == "No product fits":
            result["items"] = []
            result["research"] = {
                "partial": False,
                "stop_reason": "No products met all hard requirements based on the pages checked.",
                "country": "India",
                "constraints": [
                    {"field": "price", "operator": "max", "value": "80000", "unit": "INR"}
                ],
                "searches": 2,
                "fetches": 1,
                "limit": 200,
                "search_queries": [
                    {"domain": "amazon.in", "query": "site:amazon.in laptop DDR5 India"}
                ],
                "domains": [{"domain": "example.com", "searches": 1, "pages_read": 1}],
                "excluded": [
                    {
                        "id": "p1",
                        "title": "Café laptop",
                        "url": "https://example.com/item",
                        "observed": {"price": {"value": "89999", "unit": "INR"}},
                        "reasons": ["price: hard requirement not met"],
                    }
                ],
            }
        return result

    store = JobStore(tmp_path / "ui.db", runner=work)
    server = ScoutServer(("127.0.0.1", 0), store=store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1050})
        page.goto(f"http://127.0.0.1:{server.server_port}/#token={server.token}")
        page.get_by_text("Local connection", exact=True).wait_for()
        yield page, requests, tmp_path
        browser.close()
    server.shutdown()
    server.server_close()
    thread.join()
    store.wait()


def test_workspace_runs_renders_escapes_exports_and_reopens_history(ui):
    page, requests, _ = ui
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.get_by_label("Your request", exact=True).fill("Find café বাংলা")
    page.get_by_role("button", name="Start research", exact=True).click()
    page.get_by_text("Research complete", exact=True).wait_for()
    assert len(requests) == 1
    assert requests[0].mode == "auto"
    assert page.locator("#response h2").inner_text() == "Café বাংলা <img src=x onerror=alert(1)>"
    assert page.locator("#response img").count() == 0
    page.locator("#response summary").filter(has_text="Read captions").click()
    assert "Hello Hello" in page.locator("#response").inner_text()
    page.get_by_role("tab", name="Table", exact=True).click()
    assert page.locator("#table").is_visible()
    assert "Not verified" in page.locator("#table").inner_text()
    with page.expect_download() as downloaded:
        page.get_by_role("button", name="Download JSON", exact=True).click()
    assert downloaded.value.suggested_filename.endswith(".json")
    page.get_by_role("button", name="New research", exact=True).click()
    assert page.locator("#welcome").is_visible()
    page.locator("#history button").first.click()
    page.locator("#response h2").wait_for()
    assert page.locator("#response h2").is_visible()
    assert len(requests) == 1  # History and view changes never run another query.
    assert not errors


def test_source_controls_theme_and_mobile_are_real_interactions(ui):
    page, requests, _ = ui
    page.get_by_role("button", name="Switch to light mode").click()
    page.reload()
    page.get_by_text("Local connection", exact=True).wait_for()
    assert page.locator("html").get_attribute("data-theme") == "light"
    page.get_by_label("OpenCLI", exact=True).check()
    assert page.get_by_label("Where to look").input_value() == "sources"
    page.get_by_label("GitHub", exact=True).check()
    page.get_by_label("Web", exact=True).uncheck()
    page.get_by_label("Your request", exact=True).fill("Find Python tools")
    page.get_by_role("button", name="Start research", exact=True).click()
    page.get_by_text("Research complete", exact=True).wait_for()
    assert requests[0].sources == ["github", "opencli"]
    assert requests[0].opencli is True
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.get_by_role("button", name="Show workspace controls").click()
    assert page.locator("#sidebar").is_visible()
    page.get_by_label("YouTube", exact=True).check()
    assert not page.get_by_label("GitHub", exact=True).is_checked()
    assert not page.get_by_label("OpenCLI", exact=True).is_checked()


def test_import_and_empty_request_validation(ui):
    page, requests, _ = ui
    page.get_by_label("Where to look").select_option("manual")
    page.get_by_label("Your request", exact=True).fill("Extract the title")
    page.get_by_role("button", name="Start research", exact=True).click()
    assert "Choose a saved" in page.locator("#form-error").inner_text()
    page.get_by_label("Saved text or HTML", exact=False).set_input_files(
        {
            "name": "notes.md",
            "mimeType": "text/markdown",
            "buffer": "# Café বাংলা".encode(),
        }
    )
    page.get_by_role("button", name="Start research", exact=True).click()
    page.get_by_text("Research complete", exact=True).wait_for()
    assert requests[0].content == "# Café বাংলা"
    assert requests[0].filename == "notes.md"


def test_filtercoffee_branding_and_simple_controls(ui):
    page, _, _ = ui
    assert "FilterCoffee" in page.title()
    assert "FilterCoffee" in page.locator(".brand").inner_text()
    assert page.locator("#quota-reset").inner_text() == "Resets daily at 00:00 UTC (GMT)."
    heading = page.get_by_role("heading", name="Page rendering", exact=True)
    assert heading.is_visible()
    assert page.get_by_label("Page rendering", exact=True).is_visible()
    assert page.locator(".local-note").inner_text() == "History is stored locally."
    assert page.locator("#opencli-help").inner_text() == "Use for site-specific searches."
    country = page.get_by_label("Country / market", exact=True)
    assert country.evaluate("el => el.tagName") == "SELECT"
    assert country.locator("option").all_text_contents() == [
        "Any country",
        "Canada",
        "India",
        "United States",
    ]
    icon_url = page.locator('link[rel="icon"]').evaluate("icon => icon.href")
    response = page.request.get(icon_url)
    assert response.status == 200
    assert response.headers["content-type"] == "image/svg+xml"


def test_comparison_country_and_partial_coverage_are_visible(ui):
    page, requests, _ = ui
    page.get_by_label("Country / market", exact=True).select_option("IN")
    page.get_by_label("Your request", exact=True).fill("Compare products")
    page.get_by_role("button", name="Start research", exact=True).click()
    page.get_by_text("Partial comparison", exact=True).wait_for()
    assert requests[0].country == "IN"
    assert "200 / 200" in page.locator("#response").inner_text()
    assert "Budget reached" in page.locator("#response").inner_text()
    assert "India" in page.locator("#response").inner_text()
    assert "price at most 80000 INR" in page.locator("#response").inner_text()
    assert "Search queries used (1)" in page.locator("#response").inner_text()
    assert page.locator("#response").get_by_text("price: unknown hard requirement").count() == 1


def test_empty_product_comparison_explains_excluded_listing(ui):
    page, _, _ = ui
    page.get_by_label("Your request", exact=True).fill("No product fits")
    page.get_by_role("button", name="Start research", exact=True).click()
    page.get_by_text("No products passed evidence and requirement checks", exact=False).wait_for()
    assert (
        "No products passed evidence and requirement checks"
        in page.locator("#response").inner_text()
    )
    page.get_by_text("Websites checked and excluded matches", exact=True).click()
    assert "Café laptop" in page.locator("#response").inner_text()
    assert "price: hard requirement not met" in page.locator("#response").inner_text()
    assert "Observed: price: 89999 INR" in page.locator("#response").inner_text()
    assert page.locator('#response a[href="https://example.com/item"]').count() == 1


def test_center_and_inspector_scroll_independently_without_page_scroll(ui):
    page, _, _ = ui
    page.set_viewport_size({"width": 1920, "height": 900})
    page.locator("#welcome").evaluate("el => el.style.minHeight = '1800px'")
    page.locator("#activity").evaluate(
        """el => el.replaceChildren(...Array.from({length: 45}, () => {
          const li = document.createElement('li');
          li.textContent = 'Long activity detail';
          return li;
        }))"""
    )
    metrics = page.evaluate("""() => {
      const center = document.querySelector('.conversation');
      const inspector = document.querySelector('.right');
      return {
        page: document.documentElement.scrollHeight > innerHeight,
        center: center.scrollHeight > center.clientHeight,
        inspector: inspector.scrollHeight > inspector.clientHeight,
        workspace: document.querySelector('.workspace').clientHeight,
        viewport: innerHeight
      };
    }""")
    assert metrics["center"] is True
    assert metrics["inspector"] is True
    assert metrics["page"] is False


def test_welcome_example_explicitly_demonstrates_multi_site_product_research(ui):
    page, _, _ = ui
    example = page.get_by_role("button", name="Compare laptops across sites", exact=False)
    assert example.is_visible()
    assert "15 relevant websites" in example.inner_text()
    example.click()
    text = page.get_by_label("Your request", exact=True).input_value()
    assert "budget" in text.lower()
    assert "RAM" in text
    assert page.get_by_label("Where to look").input_value() == "auto"


def test_completed_session_can_be_deleted_from_history(ui):
    page, requests, _ = ui
    page.get_by_label("Your request", exact=True).fill("Delete this saved session")
    page.get_by_role("button", name="Start research", exact=True).click()
    page.get_by_text("Research complete", exact=True).wait_for()
    assert len(requests) == 1
    session = page.locator(".history-entry").filter(has_text="Delete this saved session")
    assert session.count() == 1
    assert session.get_by_role("button", name="Delete session").is_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.request.method == "DELETE") as deleted:
        session.get_by_role("button", name="Delete session").evaluate("el => el.click()")
    assert deleted.value.status == 204
    page.get_by_text("Your research will appear here.", exact=True).wait_for()
    assert page.locator("#history .history-entry").count() == 0
    assert page.locator("#welcome").is_visible()


def test_active_session_is_not_deleted(tmp_path):
    started = threading.Event()
    release = threading.Event()

    def work(request, emit):
        started.set()
        release.wait(5)
        return {"items": []}

    store = JobStore(tmp_path / "active.db", runner=work)
    job = store.start(WebRequest(request="Active work"))
    try:
        assert started.wait(2)
        with pytest.raises(RuntimeError, match="active"):
            store.delete(job["id"])
    finally:
        release.set()
        store.wait()
    store.delete(job["id"])
    with pytest.raises(KeyError):
        store.get(job["id"])
