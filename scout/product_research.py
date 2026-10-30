"""Bounded Gemini-guided Search/Fetch research; no paid agent/browser endpoints."""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from tldextract import TLDExtract

from scout.clean import clean_page, normalize_text
from scout.contracts import (
    Evidence,
    ExtractedItem,
    FieldSpec,
    ModelCall,
    RunMeta,
)
from scout.countries import country_code as country_location
from scout.errors import ScoutError, SecurityError
from scout.llm import LLMClient, parse_json
from scout.product_contracts import (
    Presentation,
    ProductCandidate,
    ProductExtractionPlan,
    ProductPlan,
    ProductResult,
    ResearchTurn,
)
from scout.security import Resolver, resolve_host, validate_url
from scout.tinyfish import TinyFishClient

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
_SUFFIX = TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)


def domain_key(url: str) -> str:
    host = urlsplit(url).hostname
    if not host:
        raise ValueError("A full URL is required.")
    host = host.encode("idna").decode("ascii").lower().rstrip(".")
    return _SUFFIX(host).top_domain_under_public_suffix or host


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError("A public HTTP(S) URL is required.")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Credentials in URLs are forbidden.")
    # Preserve configuration/query identity; remove only common tracking fields.
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in {"gclid", "fbclid"}
    ]
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(query), "")
    )


class ResearchBudget:
    def __init__(self) -> None:
        self.searches = 0
        self.fetches = 0

    def reserve(self, operation: str) -> None:
        if operation not in {"search", "fetch"}:
            raise ValueError("Unknown research operation.")
        if self.searches + self.fetches >= 200:
            raise ValueError("200 combined TinyFish attempts reached.")
        if operation == "search":
            self.searches += 1
        else:
            self.fetches += 1


def _number(value: str) -> Decimal | None:
    text = value.strip().replace(",", "")
    match = re.fullmatch(r"(?:[^\d\s.,+-]{1,4}\s*)?(\d+(?:\.\d+)?)\s*(?:[A-Za-z%]+)?", text)
    if not match:
        return None
    try:
        return Decimal(match[1])
    except InvalidOperation:
        return None


_CURRENCY_ALIASES = {
    "INR": {"inr", "₹", "rs", "rs.", "rupee", "rupees"},
    "USD": {"usd", "us$", "us dollars"},
    "CAD": {"cad", "c$", "ca$", "canadian dollars"},
    "AUD": {"aud", "a$", "au$", "australian dollars"},
    "NZD": {"nzd", "nz$"},
    "GBP": {"gbp", "£", "pound", "pounds"},
    "EUR": {"eur", "€", "euro", "euros"},
    "JPY": {"jpy", "¥", "yen"},
    "CNY": {"cny", "rmb", "元", "yuan"},
    "AED": {"aed", "د.إ"},
}
_COUNTRY_CURRENCY = {
    "india": "INR",
    "in": "INR",
    "united states": "USD",
    "us": "USD",
    "usa": "USD",
    "canada": "CAD",
    "ca": "CAD",
    "australia": "AUD",
    "au": "AUD",
    "new zealand": "NZD",
    "nz": "NZD",
    "united kingdom": "GBP",
    "uk": "GBP",
    "gb": "GBP",
    "ireland": "EUR",
    "eurozone": "EUR",
    "japan": "JPY",
    "jp": "JPY",
    "china": "CNY",
    "cn": "CNY",
    "united arab emirates": "AED",
    "uae": "AED",
}


def _unit_key(unit: str, country: str) -> str:
    normalized = unit.strip().casefold()
    if normalized in {"$", "dollar", "dollars"}:
        return _COUNTRY_CURRENCY.get(country.strip().casefold(), "ambiguous-dollar")
    for code, aliases in _CURRENCY_ALIASES.items():
        if normalized in {alias.casefold() for alias in aliases}:
            if normalized in {"$", "dollar", "dollars"}:
                return _COUNTRY_CURRENCY.get(country.strip().casefold(), "ambiguous-dollar")
            return code
    return re.sub(r"\s+", "", normalized)


def _unit_in_quote(unit: str, quote: str, country: str) -> bool:
    normalized = unit.strip().casefold()
    code = _unit_key(unit, country)
    aliases = _CURRENCY_ALIASES.get(code, {normalized})
    if code == "ambiguous-dollar":
        aliases = {"$"}
    if _COUNTRY_CURRENCY.get(country.strip().casefold()) == code and "$" in quote:
        return True
    for alias in aliases:
        alias = alias.casefold()
        if alias in {"₹", "£", "€", "¥", "元", "د.إ", "$", "c$", "ca$", "a$", "au$", "nz$", "us$"}:
            if alias in quote:
                return True
        elif re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", quote):
            return True
    return False


def _value_supported(field: str, value: str, quote: str) -> bool:
    normalized = normalize_text(value).casefold()
    if normalized and normalized in quote:
        return True
    if field != "price":
        return False
    expected = _number(value)
    if expected is None:
        return False
    return any(
        (actual := _number(token)) is not None and actual == expected
        for token in re.findall(r"\d[\d,]*(?:\.\d+)?", quote)
    )


def _page_type(url: str, title: str) -> str:
    """Distinguish common retailer search/listing pages from individual product pages."""
    parsed = urlsplit(url)
    path_parts = [part.casefold() for part in parsed.path.split("/") if part]
    query_keys = {key.casefold() for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
    search_paths = {"s", "search", "search-results", "searchresults", "results"}
    if (
        path_parts
        and path_parts[-1] in search_paths
        and query_keys & {"q", "k", "query", "keywords", "search_query"}
    ):
        return "search_results"
    if re.search(r"\b(search results|results for|search results for)\b", title, re.I):
        return "search_results"
    return "product"


def qualifying(candidate: ProductCandidate, plan: ProductPlan, text: str) -> list[str]:
    """Verify page quotes and hard requirements without rejecting currency formats."""
    failures = []
    normalized = normalize_text(text).casefold()
    country = plan.country
    for name, fact in candidate.facts.items():
        quote = normalize_text(fact.quote).casefold()
        if quote not in normalized or not _value_supported(name, fact.value, quote):
            failures.append(f"{name}: unsupported evidence")
        if fact.unit and not _unit_in_quote(fact.unit, quote, country):
            failures.append(f"{name}: unsupported unit/currency")
    for required in ("title", "price"):
        if required not in candidate.facts:
            failures.append(f"{required}: unknown")
    for constraint in plan.constraints:
        fact = candidate.facts.get(constraint.field)
        if not fact:
            failures.append(f"{constraint.field}: unknown hard requirement")
            continue
        quote = normalize_text(fact.quote).casefold()
        observed_unit = fact.unit or constraint.unit
        unit_ok = not constraint.unit or (
            _unit_key(constraint.unit, country) == _unit_key(observed_unit, country)
            and _unit_in_quote(constraint.unit, quote, country)
        )
        actual, expected = _number(fact.value), _number(constraint.value)
        if constraint.operator in {"min", "max"}:
            passed = unit_ok and actual is not None and expected is not None
            if passed and actual is not None and expected is not None:
                passed = actual >= expected if constraint.operator == "min" else actual <= expected
        else:
            left, right = fact.value.casefold(), constraint.value.casefold()
            passed = unit_ok and (
                left == right if constraint.operator == "equals" else right in left
            )
        if not passed:
            if not unit_ok:
                reason = (
                    f"{constraint.field}: cannot compare {fact.unit or 'unverified unit'} "
                    f"with required {constraint.unit or 'unitless value'}"
                )
            elif (
                actual is not None
                and expected is not None
                and constraint.operator == "max"
                and actual > expected
            ):
                reason = (
                    f"{constraint.field}: {fact.value} {fact.unit} exceeds maximum "
                    f"{constraint.value} {constraint.unit}"
                )
            elif (
                actual is not None
                and expected is not None
                and constraint.operator == "min"
                and actual < expected
            ):
                reason = (
                    f"{constraint.field}: {fact.value} {fact.unit} is below minimum "
                    f"{constraint.value} {constraint.unit}"
                )
            else:
                reason = f"{constraint.field}: hard requirement not met or not comparable"
            failures.append(reason)
    return failures


class ProductResearch:
    """Sequential, bounded tool loop with append-only verified evidence and provenance."""

    def __init__(
        self,
        plan: ProductPlan,
        *,
        gemini: LLMClient,
        nemotron: LLMClient,
        tinyfish: TinyFishClient,
        robots_check: Callable[[str], None],
        emit: Callable[[str, str], None],
        resolver: Resolver = resolve_host,
        clock: Callable[[], float] = time.monotonic,
        session_context: dict | None = None,
    ):
        self.plan, self.gemini, self.nemotron, self.tinyfish = plan, gemini, nemotron, tinyfish
        self.robots_check, self.emit, self.resolver, self.clock = (
            robots_check,
            emit,
            resolver,
            clock,
        )
        self.budget = ResearchBudget()
        self.started = clock()
        self.domains: dict[str, dict] = {}
        self.dismissed_domains: set[str] = set()
        self.known: dict[str, dict] = {}
        self.pages: dict[str, dict] = {}
        self.attempted: set[str] = set()
        self.queries: set[tuple[str, str]] = set()
        self.search_log: list[dict[str, Any]] = []
        self.candidates: dict[str, dict] = {}
        self.excluded: list[dict] = []
        self.warnings: list[str] = []
        self.discovery_searches = 0
        self.discovery_stagnation = 0
        self.gemini_calls = 0
        self.input_chars = 0
        self.output_tokens = 0
        self.bytes_read = 0
        self.stop_reason = "completed"
        self.partial = False
        self.scope_domain = domain_key(plan.url) if plan.scope == "url" else ""
        self.session_context = session_context or {}
        self.location = country_location(plan.country)
        if plan.country and not self.location:
            self.warnings.append(
                "Country was not recognized for geo-targeted search; query terms still include it."
            )
        self.last_results: list[dict] = []

    def _check_time(self) -> None:
        # A single in-flight bounded provider request may finish after this deadline.
        if self.clock() - self.started >= 1200:
            raise TimeoutError("20-minute research deadline reached.")

    def _admit(self, domain: str) -> dict:
        if self.scope_domain and domain != self.scope_domain:
            raise SecurityError("Result is outside the selected website.")
        if domain not in self.domains:
            if len(self.domains) >= 15:
                raise ValueError("15-domain limit reached.")
            self.domains[domain] = {
                "domain": domain,
                "queries": [],
                "searches": 0,
                "fetches": 0,
                "pages_read": 0,
                "qualifying": 0,
            }
        return self.domains[domain]

    def _remember_candidate(self, url: str, details: dict) -> bool:
        if url in self.known:
            return True
        domain = details["domain"]
        domain_count = sum(item["domain"] == domain for item in self.known.values())
        if len(self.known) >= 300 or domain_count >= 30:
            return False
        self.known[url] = details
        return True

    def search(self, query: str, domain: str = "") -> dict:
        self._check_time()
        query = query.strip()
        if not query:
            raise ValueError("Search query is empty.")
        # Model-supplied site: operators must not override backend scope.
        query = re.sub(r"\bsite:\S+", "", query, flags=re.I).strip()
        requested_domain = ""
        if domain:
            if "://" in domain or "/" in domain or " " in domain:
                raise ValueError("Expected a domain, not a URL.")
            requested_domain = domain_key("https://" + domain)
            discovered = any(domain_key(url) == requested_domain for url in self.known)
            force_broad_discovery = self.plan.scope == "web" and self.discovery_searches < 10
            if discovered and not force_broad_discovery:
                domain = requested_domain
                requested_domain = ""
            elif self.scope_domain:
                raise SecurityError("Search target is outside the selected website.")
            else:
                # Never issue a domain-scoped request for an unobserved model-supplied host.
                # Search globally for it instead, then only target domains returned by search.
                target = requested_domain
                report = self.domains.get(target)
                if report and report["searches"] >= 2:
                    raise ValueError("Two targeted searches per domain reached.")
                if self.discovery_searches >= 20:
                    raise ValueError("20 discovery searches reached.")
                self.warnings.append(
                    f"{target} was not discovered; searching broadly to verify it."
                )
                query = f"{query} {target}".strip()
                requested_domain = target
                domain = ""
        if domain:
            report = self._admit(domain)
            if report["searches"] >= 2:
                raise ValueError("Two targeted searches per domain reached.")
        elif self.scope_domain:
            domain = self.scope_domain
            report = self._admit(domain)
            if report["searches"] >= 2:
                raise ValueError("Two targeted searches per domain reached.")
        elif self.discovery_searches >= 20:
            raise ValueError("20 discovery searches reached.")
        key = (domain, query.casefold())
        if key in self.queries:
            raise ValueError("Repeated search skipped.")
        self.queries.add(key)
        self.budget.reserve("search")
        if domain:
            self.domains[domain]["searches"] += 1
        else:
            self.discovery_searches += 1
        country = f" {self.plan.country}" if self.plan.country else ""
        site = f"site:{domain} " if domain else ""
        submitted_query = site + query + country
        entry = {"domain": domain, "query": submitted_query, "location": self.location or ""}
        self.search_log.append(entry)
        if domain:
            self.domains[domain]["queries"].append(submitted_query)
        self.emit("Searching products", domain or "Discovering relevant websites")
        known_domains_before = {item["domain"] for item in self.known.values()}
        found = self.tinyfish.search(
            submitted_query,
            include_domains=[domain] if domain else None,
            location=self.location,
        )
        rows = []
        for hit in found:
            try:
                url = canonical_url(hit.url)
                host = domain_key(url)
                if domain and host != domain:
                    continue
                validate_url(url, resolver=self.resolver)
            except (ScoutError, ValueError):
                continue
            details = {
                "url": url,
                "domain": host,
                "title": hit.title,
                "snippet": hit.description,
                "discovered_from": "search",
            }
            if self._remember_candidate(url, details):
                rows.append(self.known[url])
        # A broad search requested for an unverified target still counts as a site-search
        # slot only if its actual results prove that the site is relevant.
        if not domain:
            new_domains = {row["domain"] for row in rows} - known_domains_before
            self.discovery_stagnation = 0 if new_domains else self.discovery_stagnation + 1
            self.search_log[-1]["new_domains"] = len(new_domains)
        return {"operation": "search", "results": rows}

    def fetch(self, url: str) -> dict:
        self._check_time()
        url = canonical_url(url)
        if url not in self.known:
            raise SecurityError("Only search-result URLs or the supplied URL may be fetched.")
        if url in self.attempted:
            raise ValueError("Page already attempted; use its existing evidence.")
        domain = domain_key(url)
        report = self._admit(domain)
        if report["fetches"] >= 10:
            raise ValueError("Ten fetched listing pages per domain reached.")
        validate_url(url, resolver=self.resolver)
        self.robots_check(url)
        self.budget.reserve("fetch")
        self.attempted.add(url)
        report["fetches"] += 1
        self.emit("Reading product page", url)
        fetched = self.tinyfish.fetch(url, force_refresh=True)
        final = canonical_url(fetched.url)
        validate_url(final, resolver=self.resolver)
        if domain_key(final) != domain:
            raise SecurityError("Redirect left the selected website; content was discarded.")
        self.robots_check(final)
        self.bytes_read += len(fetched.html.encode("utf-8"))
        if self.bytes_read > 30 * 1024**2:
            raise TimeoutError("30 MiB research content limit reached.")
        cleaned = clean_page(fetched.html, final)
        if cleaned.blocked or not cleaned.source_text:
            raise ValueError("Page is empty or blocked; no alternate fetch attempted.")
        page_id = f"p{len(self.pages) + 1}"
        page_type = _page_type(final, cleaned.title or "")
        links = []
        for anchor in cleaned.tree.css("a[href]"):
            if len(links) >= 20:
                break
            href = anchor.attributes.get("href") or ""
            label = normalize_text(anchor.text(separator=" "))
            try:
                candidate_url = canonical_url(urljoin(final, href))
                if candidate_url == final or domain_key(candidate_url) != domain:
                    continue
                validate_url(candidate_url, resolver=self.resolver)
            except (ScoutError, ValueError):
                continue
            details = {
                "url": candidate_url,
                "domain": domain,
                "title": label[:300] or candidate_url,
                "snippet": "Linked from " + (cleaned.title or final),
                "discovered_from": "page_link",
                "parent_url": final,
            }
            if self._remember_candidate(candidate_url, details):
                links.append(self.known[candidate_url])
        # Store only the bounded text actually shown to Gemini for quote verification.
        page = {
            "page_id": page_id,
            "url": url,
            "final_url": final,
            "domain": domain,
            "checked_at": datetime.now(UTC).isoformat(),
            "title": cleaned.title,
            "page_type": page_type,
            "links": links,
            "text": cleaned.source_text[:16000],
        }
        self.pages[page_id] = page
        report["pages_read"] += 1
        return {"operation": "fetch", "page": page}

    def accept(self, candidate: ProductCandidate) -> None:
        page = self.pages.get(candidate.page_id)
        if not page:
            title = candidate.facts.get("title")
            self.excluded.append(
                {
                    "id": candidate.id,
                    "title": title.value if title else "Unidentified listing",
                    "reasons": ["Page was not fetched"],
                }
            )
            return
        title = candidate.facts.get("title")
        if page.get("page_type") == "search_results":
            self.excluded.append(
                {
                    "id": candidate.id,
                    "title": title.value if title else "Search results page",
                    "url": page["url"],
                    "reasons": ["Search results page, not an individual product listing."],
                }
            )
            return
        failures = qualifying(candidate, self.plan, page["text"])
        all_fields = set(candidate.facts)
        for accepted in self.candidates.values():
            all_fields.update(accepted["candidate"]["facts"])
        if len(all_fields) > 30:
            failures.append("Comparison field limit reached; use consistent requested fields.")
        if failures:
            title = candidate.facts.get("title")
            observed_fields = {constraint.field for constraint in self.plan.constraints}
            observed = {
                name: {"value": fact.value, "unit": fact.unit}
                for name, fact in candidate.facts.items()
                if name in observed_fields
            }
            self.excluded.append(
                {
                    "id": candidate.id,
                    "title": title.value if title else "Unidentified listing",
                    "url": page["url"],
                    "observed": observed,
                    "reasons": failures,
                }
            )
            return
        # One configuration per fetched listing; no combining prices from unrelated variants.
        key = page["final_url"]
        if key in self.candidates:
            return
        self.candidates[key] = {
            "candidate": candidate.model_dump(),
            **{k: v for k, v in page.items() if k != "text"},
        }

    def _unreviewed_domains(self) -> list[str]:
        found = {candidate["domain"] for candidate in self.known.values()}
        searched = {
            domain for domain, report in self.domains.items() if report.get("searches", 0) > 0
        }
        reviewed = searched | self.dismissed_domains
        if self.scope_domain:
            reviewed.add(self.scope_domain)
        return sorted(found - reviewed)

    def _completion_requirements(self) -> list[str]:
        requirements = []
        if self.plan.scope == "web" and self.discovery_searches < 10:
            requirements.append(
                f"Complete at least 10 broad discovery searches ({self.discovery_searches}/10)."
            )
        elif (
            self.plan.scope == "web"
            and self.discovery_searches < 20
            and len(self.domains) < 15
            and self.discovery_stagnation < 3
        ):
            requirements.append(
                "Continue varied broad searches until three consecutive searches add no new "
                "retailer domain, or the 20-search/15-domain limit is reached."
            )
        if self.plan.scope == "site" and not self.scope_domain:
            requirements.append("Discover and verify the named website before finishing.")
        if self.plan.scope == "site" and self.scope_domain:
            report = self.domains.get(self.scope_domain, {})
            if report.get("searches", 0) < 2:
                requirements.append("Run a second distinct search on the named site.")
        for domain, report in self.domains.items():
            if domain not in self.dismissed_domains and report.get("searches", 0) == 1:
                requirements.append("Run a second distinct search on " + domain + ".")
        if len(self.domains) < 15 and self._unreviewed_domains():
            requirements.append(
                "Review discovered websites: search each relevant site or list irrelevant "
                "ones in dismissed_domains."
            )
        return requirements

    def _turn(self) -> ResearchTurn:
        payload = {
            "plan": self.plan.model_dump(),
            "prior_session_context": self.session_context,
            "scope_domain": self.scope_domain,
            "usage": {
                "searches": self.budget.searches,
                "fetches": self.budget.fetches,
                "broad_discovery_searches": self.discovery_searches,
                "consecutive_no_new_domains": self.discovery_stagnation,
            },
            "domains": list(self.domains.values()),
            "discovered_candidates": list(self.known.values()),
            "unreviewed_domains": self._unreviewed_domains(),
            "dismissed_domains": sorted(self.dismissed_domains),
            "completion_requirements": self._completion_requirements(),
            "observations": self.last_results,
            "pages": [
                {k: v for k, v in page.items() if k != "text"} for page in self.pages.values()
            ],
            "accepted": [v["candidate"] for v in self.candidates.values()],
            "excluded": self.excluded[-20:],
        }
        content = json.dumps(payload, ensure_ascii=False)
        messages = [
            {"role": "system", "content": (PROMPTS / "product_research.md").read_text("utf-8")},
            {"role": "user", "content": content},
        ]
        input_size = len(json.dumps(messages, ensure_ascii=False))
        if self.gemini_calls >= 90 or self.input_chars + input_size > 2400000:
            raise TimeoutError("Gemini iteration/input budget reached.")
        if self.output_tokens >= 180000:
            raise TimeoutError("Gemini output-token budget reached.")
        self.gemini_calls += 1
        self.input_chars += input_size
        response = self.gemini.chat(
            "product_research",
            messages,
            max_tokens=min(8192, 180000 - self.output_tokens),
        )
        self.output_tokens += response.completion_tokens
        return ResearchTurn.model_validate(parse_json(response.text))

    def run(self, request: str) -> dict:
        idle = 0
        try:
            if self.plan.scope == "url":
                url = canonical_url(self.plan.url)
                self.known[url] = {"url": url, "domain": self.scope_domain}
                self.last_results = [self.fetch(url)]
            while True:
                self._check_time()
                turn = self._turn()
                for candidate in turn.candidates:
                    self.accept(candidate)
                before = (
                    len(self.known),
                    len(self.pages),
                    len(self.candidates),
                    self.budget.searches,
                    self.discovery_stagnation,
                    len(self.dismissed_domains),
                )
                self.last_results = []
                action_failed = False
                for requested in turn.dismissed_domains:
                    try:
                        if "://" in requested or "/" in requested or " " in requested:
                            raise ValueError("Dismiss a discovered domain name, not a URL.")
                        domain = domain_key("https://" + requested)
                        if not any(item["domain"] == domain for item in self.known.values()):
                            raise ValueError("Only discovered domains may be dismissed.")
                        if domain == self.scope_domain and self.plan.scope in {"site", "url"}:
                            raise ValueError("The user-selected website cannot be dismissed.")
                        self.dismissed_domains.add(domain)
                    except (ScoutError, ValueError) as error:
                        action_failed = True
                        self.partial = True
                        self.warnings.append(str(error))
                actions_executed = 0
                for action in turn.actions:
                    if (
                        action.operation == "search"
                        and not action.domain
                        and self.plan.scope == "web"
                        and self.discovery_searches >= 10
                        and self.discovery_stagnation >= 3
                        and len(self.domains) < 15
                    ):
                        self.last_results.append(
                            {
                                "notice": (
                                    "Broad discovery is saturated; review known domains instead."
                                )
                            }
                        )
                        continue
                    actions_executed += 1
                    try:
                        if action.operation == "search":
                            if self.plan.scope == "site" and not self.scope_domain:
                                if action.domain:
                                    chosen = domain_key("https://" + action.domain)
                                    if any(domain_key(u) == chosen for u in self.known):
                                        self.scope_domain = chosen
                                query = f"{self.plan.site} official website {self.plan.country}"
                                result = self.search(
                                    query if not self.scope_domain else action.query,
                                    action.domain if self.scope_domain else "",
                                )
                            else:
                                result = self.search(action.query, action.domain)
                        else:
                            if self.plan.scope == "site" and not self.scope_domain:
                                raise ValueError("Resolve and select the named site first.")
                            result = self.fetch(action.url)
                        self.last_results.append(result)
                    except (ScoutError, ValueError) as error:
                        action_failed = True
                        message = str(error)
                        self.last_results.append({"error": message})
                        self.warnings.append(message)
                        self.partial = True
                        if self.stop_reason == "completed":
                            self.stop_reason = "Some searches or pages could not be checked."
                        if self.budget.searches + self.budget.fetches >= 200:
                            raise TimeoutError("200 combined TinyFish attempts reached.") from error
                        if any(
                            word in message.lower()
                            for word in (
                                "allowance exhausted",
                                "daily limit",
                                "rate limit",
                                "rejected the key",
                            )
                        ):
                            raise TimeoutError(message) from error
                after = (
                    len(self.known),
                    len(self.pages),
                    len(self.candidates),
                    self.budget.searches,
                    self.discovery_stagnation,
                    len(self.dismissed_domains),
                )
                idle = idle + 1 if before == after else 0
                if turn.done and not actions_executed and not action_failed:
                    pending = [
                        url
                        for url, result in self.known.items()
                        if result["domain"] in self.domains
                        and (
                            self.domains[result["domain"]]["searches"] > 0
                            or result["domain"] == self.scope_domain
                        )
                        and self.domains[result["domain"]]["fetches"] < 10
                        and url not in self.attempted
                    ]
                    if pending:
                        # Search snippets are discovery only. Inspect every known candidate
                        # in selected domains, in bounded batches, before accepting done.
                        self.last_results = []
                        for url in pending[:5]:
                            try:
                                self.last_results.append(self.fetch(url))
                            except (ScoutError, ValueError) as error:
                                message = str(error)
                                self.last_results.append({"error": message})
                                self.warnings.append(message)
                                self.partial = True
                                action_failed = True
                                if self.stop_reason == "completed":
                                    self.stop_reason = "Some listing pages could not be checked."
                                if self.budget.searches + self.budget.fetches >= 200:
                                    self.stop_reason = "200 combined TinyFish attempts reached."
                                    break
                                if any(
                                    word in message.lower()
                                    for word in (
                                        "allowance exhausted",
                                        "daily limit",
                                        "rate limit",
                                        "rejected the key",
                                    )
                                ):
                                    self.stop_reason = message
                                    break
                        if any(item.get("operation") == "fetch" for item in self.last_results):
                            continue
                    requirements = self._completion_requirements()
                    if requirements:
                        self.last_results = [
                            {"notice": "Coverage is not complete.", "requirements": requirements}
                        ]
                        if idle >= 3:
                            self.partial = True
                            self.stop_reason = (
                                "Stopped before required research coverage was complete."
                            )
                            self.warnings.append(self.stop_reason)
                            break
                        continue
                    if self.known and not self.pages:
                        self.partial = True
                        self.stop_reason = (
                            "Search results were found, but no product pages were checked."
                        )
                    break
                if idle >= 3:
                    raise TimeoutError("Stopped after three rounds without new usable evidence.")
        except (ScoutError, ValueError, TimeoutError) as error:
            self.partial, self.stop_reason = True, str(error)
            self.warnings.append(self.stop_reason)
        if not self.candidates and self.excluded and not self.partial:
            self.stop_reason = "No products met all hard requirements based on the pages checked."
        return self.result(request)

    def result(self, request: str) -> dict:
        ordered = sorted(self.candidates.values(), key=lambda row: -row["candidate"]["score"])
        for row in ordered:
            self.domains[row["domain"]]["qualifying"] += 1
        # Internal page-derived identity avoids model ID collisions in the formatter.
        for row in ordered:
            row["candidate"]["id"] = row["page_id"]
        fields = sorted({name for row in ordered for name in row["candidate"]["facts"]})
        self.emit("Formatting comparison", f"{len(ordered)} supported product matches")
        try:
            response = self.nemotron.chat(
                "product_present",
                [
                    {
                        "role": "system",
                        "content": (PROMPTS / "product_present.md").read_text("utf-8"),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "request": request,
                                "candidates": [
                                    {
                                        "id": row["page_id"],
                                        "domain": row["domain"],
                                        "score": row["candidate"]["score"],
                                        "fields": list(row["candidate"]["facts"]),
                                    }
                                    for row in ordered
                                ],
                                "fields": fields,
                                "partial": self.partial,
                                "stop_reason": self.stop_reason,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                max_tokens=4096,
            )
            presentation = Presentation.model_validate(parse_json(response.text))
            if presentation.candidate_ids != [row["page_id"] for row in ordered]:
                raise ValueError("Formatter changed the verified ranking or candidate set.")
            if any(field not in fields for field in presentation.highlight_fields):
                raise ValueError("Formatter introduced unsupported fields.")
            fields = list(dict.fromkeys([*presentation.highlight_fields, *fields]))
        except (ScoutError, ValueError) as error:
            self.partial = True
            self.stop_reason = "Presentation failed; verified comparison retained."
            self.warnings.append(f"Presentation failed; verified table retained: {error}")
        columns = ["domain", "source_url", "checked_at", *fields, "match_score"]
        items = []
        for row in ordered:
            facts = row["candidate"]["facts"]
            items.append(
                ExtractedItem(
                    data={
                        "domain": row["domain"],
                        "source_url": row["final_url"],
                        "checked_at": row["checked_at"],
                        **{name: fact["value"] for name, fact in facts.items()},
                        "match_score": row["candidate"]["score"],
                    },
                    evidence=[
                        Evidence(field=name, quote=fact["quote"]) for name, fact in facts.items()
                    ],
                    confidence=0.8,
                )
            )
        calls: list[ModelCall] = [*self.nemotron.calls, *self.gemini.calls]
        result = ProductResult(
            url="research:products",
            instruction=request,
            plan=ProductExtractionPlan(
                mode="list",
                fields=[
                    FieldSpec(name=name, type="string", description=name.replace("_", " "))
                    for name in columns
                ],
            ),
            items=items,
            meta=RunMeta(
                run_id=uuid.uuid4().hex,
                url="research:products",
                fetch_tier="product_research",
                model_calls=calls,
                prompt_tokens=sum(c.prompt_tokens for c in calls),
                completion_tokens=sum(c.completion_tokens for c in calls),
                estimated_cost_usd=sum(c.cost_usd for c in calls),
                latency_ms=int((self.clock() - self.started) * 1000),
                warnings=[
                    *self.warnings,
                    "Research is bounded, not exhaustive; prices and stock may change.",
                ],
            ),
        )
        output = result.model_dump(mode="json")
        output["research"] = {
            "partial": self.partial,
            "stop_reason": self.stop_reason,
            "country": self.plan.country,
            "scope": self.plan.scope,
            "constraints": [item.model_dump() for item in self.plan.constraints],
            "preferences": self.plan.preferences,
            "searches": self.budget.searches,
            "discovery_searches": self.discovery_searches,
            "discovery_stagnation": self.discovery_stagnation,
            "search_queries": self.search_log,
            "fetches": self.budget.fetches,
            "limit": 200,
            "domains": list(self.domains.values()),
            "excluded": self.excluded,
            "gemini_calls": self.gemini_calls,
        }
        return output
