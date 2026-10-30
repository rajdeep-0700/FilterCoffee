"""Product research uses real orchestration with only network/model boundaries replaced."""

import json

import httpx
import pytest
from pydantic import ValidationError

from scout.contracts import ModelCall
from scout.countries import country_code, country_options
from scout.discovery import resolve_request
from scout.errors import SecurityError
from scout.fetch import FetchedPage, Fetcher
from scout.llm import LLMResponse
from scout.product_contracts import Constraint, ProductCandidate, ProductPlan
from scout.product_research import (
    ProductResearch,
    ResearchBudget,
    country_location,
    domain_key,
    qualifying,
)
from scout.tinyfish import TinyFishClient


class Model:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []
        self.warnings = []
        self.stages = []
        self.messages = []

    def chat(self, stage, messages, **kwargs):
        self.stages.append(stage)
        self.messages.append(messages)
        value = next(self.replies)
        self.calls.append(
            ModelCall(
                stage=stage,
                model="fake:free",
                prompt_tokens=10,
                completion_tokens=5,
                latency_ms=1,
                cost_usd=0,
                ok=True,
            )
        )
        return LLMResponse(json.dumps(value), "fake:free", 10, 5, 1, 0)

    def close(self):
        pass


def product(page="p1", price="70000", ram="16"):
    return {
        "id": page,
        "page_id": page,
        "score": 90,
        "facts": {
            "title": {"value": "Café laptop", "quote": "Café laptop"},
            "price": {"value": price, "quote": f"INR {price}", "unit": "INR"},
            "configuration": {"value": f"{ram} GB RAM", "quote": f"{ram} GB RAM"},
            "ram": {"value": ram, "quote": f"{ram} GB RAM", "unit": "GB"},
        },
    }


def test_discovery_hands_comparisons_off_without_single_page_selection():
    plan = {
        "scope": "web",
        "goal": "Compare cameras",
        "site": None,
        "url": None,
        "country": "India",
        "constraints": [{"field": "price", "operator": "max", "value": "80000", "unit": None}],
    }
    llm = Model(
        [
            {
                "route": "web_search",
                "instruction": "Compare",
                "query": "cameras",
                "product_research": plan,
            }
        ]
    )
    result = resolve_request("Compare cameras", llm=llm, allow_product_research=True)
    assert result.product_research is not None
    assert result.product_research.goal == "Compare cameras"
    assert llm.stages == ["product_intent"]


def test_invalid_product_intent_shows_clean_guidance_not_validator_trace():
    from scout.errors import DiscoveryError

    llm = Model(
        [
            {
                "route": "web_search",
                "instruction": "Compare products",
                "query": "laptop",
                "product_research": {"scope": "site", "goal": "Compare laptops", "site": None},
            }
        ]
    )
    with pytest.raises(DiscoveryError, match="Restate the product") as error:
        resolve_request("Compare laptops", llm=llm, allow_product_research=True)
    assert "ValidationError" not in str(error.value)
    assert "input_value=None" not in str(error.value)


def test_real_loop_fetches_two_sites_checks_evidence_and_preserves_partial(tmp_path):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.host == "api.search.tinyfish.ai":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"url": "https://one.com/item", "title": "One", "snippet": "not evidence"},
                        {"url": "https://two.com/item", "title": "Two", "snippet": "not evidence"},
                    ]
                },
            )
        if request.url.host == "api.fetch.tinyfish.ai":
            url = json.loads(request.content)["urls"][0]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": url,
                            "format": "html",
                            "text": "<h1>Café laptop</h1><p>INR 70000</p><p>16 GB RAM</p>",
                        }
                    ]
                },
            )
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /\n")
        pytest.fail(str(request.url))

    transport = httpx.MockTransport(respond)
    from scout.cache import Cache

    with httpx.Client(transport=transport) as http:
        tiny = TinyFishClient(
            "offline-key",
            http_client=http,
            cache=Cache(tmp_path / "cache.db"),
            resolver=lambda *_: ["93.184.216.34"],
        )
        checker = Fetcher(client=http, resolver=lambda *_: ["93.184.216.34"])
        gemini = Model(
            [
                {"actions": [{"operation": "search", "query": "laptops"}]},
                {
                    "actions": [
                        {"operation": "fetch", "url": "https://one.com/item"},
                        {"operation": "fetch", "url": "https://two.com/item"},
                    ]
                },
                {"candidates": [product(), product("p2", price="90000")], "done": True},
            ]
        )
        nemotron = Model([{"candidate_ids": ["p1"], "highlight_fields": ["price", "title"]}])
        research = ProductResearch(
            ProductPlan(
                scope="web",
                goal="Find laptop",
                country="India",
                constraints=[Constraint(field="price", operator="max", value="80000", unit="INR")],
            ),
            gemini=gemini,
            nemotron=nemotron,
            tinyfish=tiny,
            robots_check=checker.check_robots,
            emit=lambda *_: None,
            resolver=lambda *_: ["93.184.216.34"],
        )
        result = research.run("Find laptop")
    assert len(result["items"]) == 1
    assert result["items"][0]["data"]["source_url"] == "https://one.com/item"
    assert result["research"]["searches"] == 1
    assert result["research"]["fetches"] == 2
    assert result["research"]["excluded"]
    assert result["research"]["partial"] is False
    assert nemotron.stages == ["product_present"]
    assert "India" in str(requests[0].url)


def test_provider_limits_are_search_12000_fetch_1000(tmp_path):
    from scout.cache import Cache

    tiny = TinyFishClient("x", cache=Cache(tmp_path / "c.db"))
    assert tiny.daily_limits == {"search": 12000, "fetch": 1000}
    tiny.close()


@pytest.fixture
def engine(tmp_path):
    from scout.cache import Cache

    seen = []

    def respond(request):
        seen.append(request)
        if request.url.host == "api.search.tinyfish.ai":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"title": "Product", "url": "https://shop.com/item", "snippet": "discover"}
                    ]
                },
            )
        if request.url.host == "api.fetch.tinyfish.ai":
            url = json.loads(request.content)["urls"][0]
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "url": url,
                            "format": "html",
                            "text": "<h1>Café laptop</h1><p>INR 70000</p><p>16 GB RAM</p>",
                        }
                    ]
                },
            )
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        tiny = TinyFishClient(
            "offline",
            cache=Cache(tmp_path / "cache.db"),
            http_client=http,
            resolver=lambda *_: ["93.184.216.34"],
        )
        checker = Fetcher(client=http, resolver=lambda *_: ["93.184.216.34"])
        yield (
            ProductResearch(
                ProductPlan(scope="web", goal="Compare cameras"),
                tinyfish=tiny,
                robots_check=checker.check_robots,
                emit=lambda *_: None,
                gemini=Model([]),
                nemotron=Model([{"candidate_ids": [], "highlight_fields": []}]),
                resolver=lambda *_: ["93.184.216.34"],
            ),
            seen,
        )


def test_undiscovered_url_cannot_be_fetched(engine):
    research, seen = engine
    with pytest.raises(SecurityError, match="Only search-result"):
        research.fetch("https://invented.com/item")
    assert not seen
    assert research.budget.fetches == 0


def test_seed_domain_restricts_queries_and_offsite_fetch(engine):
    research, seen = engine
    research.scope_domain = "shop.com"
    research.search("site:evil.com camera")
    assert seen[0].url.params["query"] == "site:shop.com camera"
    research.known["https://evil.com/item"] = {"url": "https://evil.com/item"}
    with pytest.raises(SecurityError, match="outside"):
        research.fetch("https://evil.com/item")
    assert research.budget.fetches == 0


def test_domain_and_page_limits_and_query_dedup(engine):
    research, _ = engine
    research.search("camera")
    with pytest.raises(ValueError, match="Repeated"):
        research.search("camera")
    research.search("camera cheap", "shop.com")
    research.search("camera lightweight", "shop.com")
    assert research.domains["shop.com"]["searches"] == 2
    with pytest.raises(ValueError, match="Two targeted"):
        research.search("camera better", "shop.com")
    for index in range(14):
        research._admit(f"site{index}.com")
    with pytest.raises(ValueError, match="15-domain"):
        research._admit("overflow.com")
    for index in range(10):
        url = f"https://shop.com/{index}"
        research.known[url] = {"url": url}
        research.fetch(url)
    research.known["https://shop.com/extra"] = {"url": "https://shop.com/extra"}
    with pytest.raises(ValueError, match="Ten fetched"):
        research.fetch("https://shop.com/extra")
    assert research.budget.fetches == 10


@pytest.mark.parametrize("mutate", ["quote", "price", "unit", "missing", "variant"])
def test_evidence_and_hard_constraints_fail_closed(mutate):
    from scout.product_research import qualifying

    value = product()
    if mutate == "quote":
        value["facts"]["title"]["quote"] = "Invented laptop"
    elif mutate == "price":
        value["facts"]["price"]["value"] = "90000"
        value["facts"]["price"]["quote"] = "INR 90000"
    elif mutate == "unit":
        value["facts"]["price"]["unit"] = "USD"
    elif mutate == "missing":
        del value["facts"]["ram"]
    else:
        value["facts"]["configuration"]["value"] = "32 GB RAM"
    plan = ProductPlan(
        scope="web",
        goal="Find good laptop",
        constraints=[
            Constraint(field="price", operator="max", value="80000", unit="INR"),
            Constraint(field="ram", operator="min", value="16", unit="GB"),
        ],
    )
    assert qualifying(
        ProductCandidate.model_validate(value), plan, "Café laptop INR 70000 INR 90000 16 GB RAM"
    )


def test_rupee_symbol_matches_inr_and_formatted_price_is_still_evidence():
    candidate = ProductCandidate.model_validate(
        {
            "id": "p1",
            "page_id": "p1",
            "score": 90,
            "facts": {
                "title": {"value": "Laptop X", "quote": "Laptop X"},
                "price": {"value": "74999", "quote": "Laptop X price ₹ 74,999", "unit": "INR"},
                "ram": {"value": "16", "quote": "16 GB DDR5 RAM", "unit": "GB"},
                "storage": {"value": "512", "quote": "512 GB SSD", "unit": "GB"},
                "processor": {"value": "Core Ultra 7", "quote": "Intel Core Ultra 7 processor"},
            },
        }
    )
    plan = ProductPlan(
        scope="web",
        goal="Compare laptops",
        country="India",
        constraints=[
            Constraint(field="price", operator="max", value="100000", unit="INR"),
            Constraint(field="ram", operator="min", value="16", unit="GB"),
            Constraint(field="storage", operator="min", value="512", unit="GB"),
        ],
    )
    assert (
        qualifying(
            candidate,
            plan,
            "Laptop X price ₹ 74,999 16 GB DDR5 RAM 512 GB SSD Intel Core Ultra 7 processor",
        )
        == []
    )


def test_dollar_price_requires_country_context_to_disambiguate():
    candidate = ProductCandidate.model_validate(
        {
            "id": "p1",
            "page_id": "p1",
            "score": 80,
            "facts": {
                "title": {"value": "Laptop Y", "quote": "Laptop Y"},
                "price": {"value": "$99", "quote": "Laptop Y price $99", "unit": "CAD"},
            },
        }
    )
    canadian_plan = ProductPlan(
        scope="web",
        goal="Find laptop",
        country="Canada",
        constraints=[Constraint(field="price", operator="max", value="120", unit="CAD")],
    )
    assert qualifying(candidate, canadian_plan, "Laptop Y price $99") == []
    unspecified_plan = canadian_plan.model_copy(update={"country": ""})
    assert qualifying(candidate, unspecified_plan, "Laptop Y price $99")


def test_exclusion_reason_explains_over_budget_price():
    candidate = ProductCandidate.model_validate(
        {
            "id": "p1",
            "page_id": "p1",
            "score": 90,
            "facts": {
                "title": {"value": "Laptop X", "quote": "Laptop X"},
                "price": {
                    "value": "₹ 89,999",
                    "quote": "Laptop X price ₹ 89,999",
                    "unit": "INR",
                },
                "ram": {"value": "16 GB DDR5", "quote": "16 GB DDR5 RAM"},
                "storage": {"value": "512 GB SSD", "quote": "512 GB SSD"},
                "processor": {
                    "value": "Intel Core Ultra 7",
                    "quote": "Intel Core Ultra 7 processor",
                },
            },
        }
    )
    plan = ProductPlan(
        scope="web",
        goal="Laptop under budget",
        country="India",
        constraints=[Constraint(field="price", operator="max", value="80000", unit="INR")],
    )
    reasons = qualifying(
        candidate,
        plan,
        "Laptop X price ₹ 89,999 16 GB DDR5 RAM 512 GB SSD Intel Core Ultra 7 processor",
    )
    assert any("89,999" in reason and "maximum 80000 INR" in reason for reason in reasons)


def test_discovered_result_urls_persist_between_gemini_turns(engine):
    research, _ = engine
    research.search("laptops India")
    research.last_results = [{"error": "A previous target domain was not discovered."}]
    research.gemini = Model([{"done": True}])
    research._turn()
    payload = json.loads(research.gemini.messages[0][1]["content"])
    assert payload["discovered_candidates"][0]["domain"] == "shop.com"


def test_domain_action_errors_mark_coverage_partial(engine):
    research, _ = engine
    research.gemini = Model(
        [
            {
                "actions": [{"operation": "fetch", "url": "https://invented.com/item"}],
                "done": True,
            },
            {"done": True},
        ]
    )
    result = research.run("Compare laptops")
    assert result["research"]["partial"] is True
    assert any("Only search-result URLs" in warning for warning in result["meta"]["warnings"])


def test_unknown_domain_action_is_downgraded_to_broad_discovery_search(engine):
    research, requests = engine
    result = research.search("Core Ultra laptop under 80000", "invented-retailer.com")
    query = requests[0].url.params["query"]
    assert "invented-retailer.com" in query
    assert "include_domains" not in requests[0].url.params
    assert result["results"][0]["domain"] == "shop.com"
    assert research.discovery_searches == 1
    assert research.domains == {}
    research.search("Core Ultra laptop under 80000", "shop.com")
    assert research.domains["shop.com"]["searches"] == 1
    assert research.budget.searches == 2


def test_selected_country_is_passed_as_tinyfish_geo_location(engine):
    research, requests = engine
    research.plan.country = "India"
    research.location = country_location("India")
    research.search("laptops India")
    assert requests[0].url.params["location"] == "IN"


def test_unknown_domain_recovers_then_scopes_and_reads_listing(engine):
    research, _ = engine
    research.nemotron = Model([{"candidate_ids": ["p1"], "highlight_fields": ["price"]}])
    research.gemini = Model(
        [
            {
                "actions": [{"operation": "search", "query": "laptop", "domain": "shop.com"}],
                "done": True,
            },
            {
                "actions": [
                    {"operation": "search", "query": "DDR5 16GB under budget", "domain": "shop.com"}
                ],
                "done": True,
            },
            {"actions": [{"operation": "fetch", "url": "https://shop.com/item"}], "done": True},
            {"candidates": [product()], "done": True},
        ]
    )
    result = research.run("Compare laptops")
    assert len(result["items"]) == 1
    assert result["research"]["partial"] is False
    assert result["research"]["searches"] == 2
    assert result["research"]["fetches"] == 1
    assert any("searching broadly to verify it" in w for w in result["meta"]["warnings"])
    assert len(research.gemini.stages) == 4


def test_search_snippets_alone_cannot_be_reported_as_complete_product_research(engine):
    research, _ = engine
    research.search("laptops India")
    research.gemini = Model([{"done": True}])
    result = research.run("Compare laptops")
    assert result["items"] == []
    assert result["research"]["partial"] is True
    assert result["research"]["stop_reason"] == (
        "Search results were found, but no product pages were checked."
    )
    assert result["research"]["constraints"] == []


def test_done_turn_still_fetches_remaining_results_from_selected_domain(engine):
    research, _ = engine
    research.domains["shop.com"] = {
        "domain": "shop.com",
        "searches": 1,
        "fetches": 0,
        "pages_read": 0,
        "qualifying": 0,
    }
    research.known["https://shop.com/first"] = {
        "url": "https://shop.com/first",
        "domain": "shop.com",
        "title": "First",
    }
    research.known["https://shop.com/second"] = {
        "url": "https://shop.com/second",
        "domain": "shop.com",
        "title": "Second",
    }
    research.gemini = Model(
        [
            {"done": True},
            {"candidates": [product("p1"), product("p2")], "done": True},
        ]
    )
    research.nemotron = Model([{"candidate_ids": ["p1", "p2"], "highlight_fields": ["price"]}])
    result = research.run("Compare laptops")
    assert result["research"]["fetches"] == 2
    assert result["research"]["domains"][0]["pages_read"] == 2
    assert len(result["items"]) == 2
    assert research.gemini.stages == ["product_research", "product_research"]


def test_search_results_page_is_not_a_product_and_its_product_link_is_used(engine):
    research, _ = engine
    search_url = "https://www.amazon.in/intel-core-ultra-7-laptop/s?k=intel+core+ultra+7"
    product_url = "https://www.amazon.in/HP-14-gr1023TU/dp/B0D8J85F5X"
    research.known[search_url] = {
        "url": search_url,
        "domain": "amazon.in",
        "title": "Search results for laptops",
    }
    research.robots_check = lambda _: None
    research.tinyfish.fetch = lambda url, **_: FetchedPage(
        url,
        '<h1>Results for laptop</h1><a href="/HP-14-gr1023TU/dp/B0D8J85F5X">'
        "HP laptop Intel Core Ultra 7, 16GB DDR5, 512GB SSD</a>",
        200,
        "text/html",
    )
    search_page = research.fetch(search_url)["page"]
    assert search_page["page_type"] == "search_results"
    assert product_url in research.known

    research.accept(
        ProductCandidate.model_validate(
            {
                "id": "serp-product",
                "page_id": search_page["page_id"],
                "score": 99,
                "facts": {
                    "title": {"value": "HP Laptop", "quote": "HP laptop"},
                    "price": {"value": "70000", "quote": "INR 70000", "unit": "INR"},
                },
            }
        )
    )
    assert not research.candidates
    assert any("Search results page" in r for e in research.excluded for r in e["reasons"])

    product_text = "HP 14 Laptop Intel Core Ultra 7 155H (16GB DDR5, 512GB SSD) price ₹ 89,349"
    research.tinyfish.fetch = lambda url, **_: FetchedPage(
        url, "<h1>" + product_text + "</h1>", 200, "text/html"
    )
    research.domains["amazon.in"]["searches"] = 1
    research.gemini = Model(
        [
            {"done": True},
            {
                "candidates": [
                    {
                        "id": "product-1",
                        "page_id": "p2",
                        "score": 90,
                        "facts": {
                            "title": {"value": "HP 14 Laptop", "quote": "HP 14 Laptop"},
                            "price": {
                                "value": "₹ 89,349",
                                "quote": "price ₹ 89,349",
                                "unit": "INR",
                            },
                            "processor": {
                                "value": "Intel Core Ultra 7 155H",
                                "quote": "Intel Core Ultra 7 155H",
                            },
                        },
                    }
                ],
                "done": True,
            },
        ]
    )
    research.nemotron = Model([{"candidate_ids": ["p2"], "highlight_fields": ["price"]}])
    output = research.run("Compare laptop prices")
    assert output["items"][0]["data"]["source_url"] == product_url
    assert output["research"]["fetches"] == 2  # SERP plus the actual product page.


def test_tool_actions_are_processed_even_when_gemini_says_done(engine):
    research, _ = engine
    research.gemini = Model(
        [
            {"actions": [{"operation": "fetch", "url": "https://invented.com/item"}], "done": True},
            {"done": True},
        ]
    )
    result = research.run("Compare laptops")
    assert research.gemini.stages == ["product_research", "product_research"]
    assert result["research"]["partial"] is True
    assert result["research"]["stop_reason"] != "completed"


def test_failed_formatter_retains_verified_rows(engine):
    research, _ = engine
    research.search("camera")
    research.fetch("https://shop.com/item")
    research.accept(ProductCandidate.model_validate(product()))
    # Engine's formatter returns no IDs, which is an invalid omission of the verified row.
    result = research.result("Find products")
    assert result["research"]["partial"] is True
    assert len(result["items"]) == 1
    assert "Presentation failed" in result["research"]["stop_reason"]


def test_deadline_returns_partial_not_another_model_loop(engine):
    research, _ = engine
    research.clock = lambda: research.started + 1201
    result = research.run("Find products")
    assert result["research"]["partial"] is True
    assert research.gemini.stages == []


def test_quota_exhaustion_stops_without_retry_or_other_provider(engine):
    research, _ = engine
    research.gemini = Model([{"actions": [{"operation": "search", "query": "camera"}]}])
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(402))) as http:
        research.tinyfish._client = http
        result = research.run("Compare cameras")
    assert result["research"]["partial"] is True
    assert result["research"]["searches"] == 1
    assert result["research"]["fetches"] == 0
    assert "allowance exhausted" in result["research"]["stop_reason"]


def test_robots_denial_prevents_tinyfish_fetch(engine):
    from scout.errors import RobotsError

    research, _ = engine
    research.known["https://shop.com/item"] = {"url": "https://shop.com/item"}
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text="User-agent: *\nDisallow: /\n")
        )
    ) as http:
        checker = Fetcher(client=http, resolver=research.resolver)
        research.robots_check = checker.check_robots
        with pytest.raises(RobotsError):
            research.fetch("https://shop.com/item")
    assert research.budget.fetches == 0


@pytest.mark.parametrize("name", ["source_url", "domain", "checked_at", "bad name"])
def test_model_cannot_overwrite_source_provenance(name):
    value = product()
    value["facts"][name] = {"value": "fake", "quote": "fake"}
    with pytest.raises(ValidationError):
        ProductCandidate.model_validate(value)


def test_web_service_runs_actual_product_loop_with_two_nemotron_attempts(
    engine, monkeypatch, tmp_path
):
    from pydantic import SecretStr

    from scout.config import Settings
    from scout.web_service import WebRequest, execute_request

    research, _ = engine
    nemotron = Model(
        [
            {
                "route": "web_search",
                "query": "camera",
                "instruction": "Compare cameras",
                "product_research": {
                    "scope": "web",
                    "goal": "Compare cameras",
                    "country": "Canada",
                },
            },
            {"candidate_ids": ["p1"], "highlight_fields": ["price", "title"]},
        ]
    )
    gemini = Model(
        [
            {"actions": [{"operation": "search", "query": "camera"}]},
            {"actions": [{"operation": "fetch", "url": "https://shop.com/item"}]},
            {"candidates": [product()], "done": True},
        ]
    )
    settings = Settings(
        "openrouter",
        SecretStr("offline"),
        "https://openrouter.ai/api/v1/",
        "fast:free",
        "strong:free",
        "ultra:free",
        "OfflineTest",
    )
    monkeypatch.setattr("scout.web_service.load_settings", lambda: settings)
    monkeypatch.setattr("scout.web_service.ModelClient", lambda *_: nemotron)
    monkeypatch.setattr("scout.web_service.GeminiExtractionClient", lambda *_: gemini)
    monkeypatch.setattr("scout.web_service.TinyFishClient", lambda *a, **kw: research.tinyfish)
    monkeypatch.setattr(
        "scout.web_service.Fetcher",
        lambda **kw: Fetcher(client=research.tinyfish._client, resolver=research.resolver),
    )
    result = execute_request(
        WebRequest(request="Compare cameras", country="India"),
        lambda *_: None,
        cache_path=tmp_path / "service.db",
    )
    assert nemotron.stages == ["product_intent", "product_present"]
    assert result["research"]["country"] == "India"
    assert len(result["items"]) == 1
    assert result["research"]["partial"] is False
    assert len(result["meta"]["model_calls"]) == 5


def test_combined_budget_reserves_failures_and_refuses_attempt_201():
    budget = ResearchBudget()
    for _ in range(20):
        budget.reserve("search")
    for _ in range(180):
        budget.reserve("fetch")
    assert budget.searches + budget.fetches == 200
    with pytest.raises(ValueError, match="200"):
        budget.reserve("fetch")
    assert budget.fetches == 180


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://shop.example.co.uk/a", "example.co.uk"),
        ("https://www.amazon.in/item", "amazon.in"),
        ("https://store.blogspot.com/a", "store.blogspot.com"),
    ],
)
def test_domains_use_offline_public_suffix_rules(url, expected):
    assert domain_key(url) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {"scope": "web", "goal": ""},
        {"scope": "site", "goal": "Find cameras"},
        {"scope": "url", "goal": "Find cameras", "url": "file:///x"},
        {"scope": "web", "goal": "Find cameras", "country": "x" * 81},
    ],
)
def test_invalid_research_plans_fail_closed(payload):
    with pytest.raises(ValidationError):
        ProductPlan.model_validate(payload)


def test_model_nulls_for_optional_site_url_and_constraint_units_are_normalized():
    plan = ProductPlan.model_validate(
        {
            "scope": "web",
            "goal": "Compare laptops in India",
            "site": None,
            "url": None,
            "country": "India",
            "constraints": [
                {"field": "price", "operator": "max", "value": "100000", "unit": None},
                {"field": "ram", "operator": "min", "value": "16", "unit": None},
            ],
            "preferences": [],
        }
    )
    assert plan.site == ""
    assert plan.url == ""
    assert all(constraint.unit == "" for constraint in plan.constraints)


def test_country_dropdown_contains_exactly_195_countries_in_alphabetical_order():
    options = country_options()
    names = [option["name"] for option in options]
    assert len(options) == 195
    assert len({option["code"] for option in options}) == 195
    assert names == sorted(names, key=str.casefold)
    assert country_code("India") == "IN"
    assert country_code("IN") == "IN"
    assert country_code("not a country") is None
