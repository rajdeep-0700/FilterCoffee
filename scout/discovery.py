"""Turn a natural-language request into one safe page URL and an extract task."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from scout.config import Settings
from scout.errors import ConfigurationError, DiscoveryError, ScoutError
from scout.llm import LLMClient, LLMResponse, ModelClient, parse_json
from scout.product_contracts import ProductPlan

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
TAVILY_SEARCH_URL = "https://api.tavily.com/search"
MAX_SEARCH_RESULTS = 5


class RequestIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    route: Literal["direct_url", "site_search", "web_search"]
    url: str | None = None
    site_domain: str | None = None
    query: str = Field(default="", max_length=300)
    product_research: ProductPlan | None = None
    instruction: str = Field(min_length=1, max_length=500)

    @field_validator("instruction")
    @classmethod
    def nonblank_instruction(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("instruction must not be blank")
        return value.strip()

    @field_validator("query")
    @classmethod
    def normalized_query(cls, value: str) -> str:
        return value.strip()


class CandidateSelection(BaseModel):
    model_config = ConfigDict(extra="allow")

    candidate_index: int = Field(ge=0)


_SELECTION_KEYS = (
    "candidate_index",
    "index",
    "candidate",
    "candidate_id",
    "selected_index",
    "choice",
)


def _selection(payload: Any) -> CandidateSelection:
    """Coerce the selector model's reply into a candidate index."""
    if isinstance(payload, list) and payload:
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("Selection reply was not a JSON object.")
    if isinstance(payload.get("selection"), dict):
        payload = {**payload, **payload["selection"]}
    raw = next((payload[key] for key in _SELECTION_KEYS if key in payload), None)
    if isinstance(raw, dict):
        raw = next((raw[key] for key in _SELECTION_KEYS if key in raw), None)
    if raw is None or isinstance(raw, bool):
        raise ValueError("Selection reply had no candidate index.")
    if isinstance(raw, int):
        return CandidateSelection(candidate_index=raw)
    match = re.search(r"\d+", str(raw))
    if not match:
        raise ValueError("Selection reply had no candidate index.")
    return CandidateSelection(candidate_index=int(match.group()))


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str


@dataclass(frozen=True)
class DiscoveryResult:
    route: str
    query: str
    title: str
    url: str
    instruction: str
    search_result_count: int
    search_latency_ms: int
    intent_response: LLMResponse
    selection_response: LLMResponse | None
    search_provider: str = "none"
    warnings: tuple[str, ...] = ()
    product_research: ProductPlan | None = None


class TavilySearchClient:
    """Small adapter for Tavily's basic-search free tier."""

    def __init__(self, api_key: str, *, http_client: httpx.Client | None = None):
        if not api_key.strip():
            raise ConfigurationError("Set TAVILY_API_KEY in the project .env to search the web.")
        self.api_key = api_key.strip()
        self._owned = http_client is None
        self._client = http_client or httpx.Client(
            timeout=15,
            follow_redirects=False,
            trust_env=False,
        )

    def search(self, query: str, *, include_domains: list[str] | None = None) -> list[SearchResult]:
        payload: dict[str, Any] = {
            "query": query,
            "search_depth": "basic",
            "max_results": MAX_SEARCH_RESULTS,
            "include_answer": False,
        }
        if include_domains:
            payload["include_domains"] = include_domains
            payload["include_domains_mode"] = "restrict"
        try:
            response = self._client.post(
                TAVILY_SEARCH_URL,
                json=payload,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                },
            )
        except httpx.HTTPError:
            raise DiscoveryError(
                "Web search request failed; check connectivity and try again."
            ) from None
        if response.status_code in {401, 403}:
            raise DiscoveryError("Tavily rejected the search key; check TAVILY_API_KEY.")
        if response.status_code == 429:
            raise DiscoveryError("Tavily search rate limit reached; wait before trying again.")
        if response.status_code == 432:
            raise DiscoveryError(
                "Tavily's free search credits are exhausted; they reset monthly. "
                "No paid search was attempted."
            )
        if response.status_code == 433:
            raise DiscoveryError("Tavily rejected the request because its usage limit was reached.")
        if not 200 <= response.status_code < 300:
            raise DiscoveryError(f"Tavily search failed with HTTP {response.status_code}.")
        try:
            payload = response.json()
        except ValueError:
            raise DiscoveryError("Tavily returned an unreadable search response.") from None
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise DiscoveryError("Tavily returned no usable web search results.")
        results = []
        for row in rows[:MAX_SEARCH_RESULTS]:
            if not isinstance(row, dict):
                continue
            title, url = row.get("title"), row.get("url")
            if not isinstance(title, str) or not isinstance(url, str) or not _http_url(url):
                continue
            description = row.get("content")
            results.append(
                SearchResult(
                    title[:300],
                    url,
                    description[:1200] if isinstance(description, str) else "",
                )
            )
        return results

    def close(self) -> None:
        if self._owned:
            self._client.close()


def _http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not any(char.isspace() for char in value)
        )
    except ValueError:
        return False


def _domain(value: str | None) -> str:
    if not value or any(char in value for char in "/:@?#"):
        raise DiscoveryError("The model did not identify a valid site domain.")
    try:
        normalized = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise DiscoveryError("The model returned an invalid site domain.") from None
    labels = normalized.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise DiscoveryError("The model returned an invalid site domain.")
    return normalized


def _matches_domain(url: str, domain: str) -> bool:
    if not _http_url(url):
        return False
    host = urlsplit(url).hostname or ""
    try:
        host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        return False
    return host == domain or host.endswith("." + domain)


_INDIA_HINT = re.compile(r"\b(india|indian|inr|rupee|rupees)\b|₹", re.I)
_NON_INDIA_PATH = re.compile(r"/(?:us|uk|au|ca|de|fr|sg|ae|en-us|en-gb)(?=[/.-]|$)", re.I)


def _india_result(request: str) -> bool:
    return bool(_INDIA_HINT.search(request))


def _prefer_locale(candidates: list[SearchResult], request: str) -> list[SearchResult]:
    """Rank Indian storefronts first for India/INR requests without dropping any."""
    if not _india_result(request):
        return candidates

    def rank(result: SearchResult) -> int:
        host = (urlsplit(result.url).hostname or "").lower()
        if host.endswith(".in") or "flipkart" in host:
            return 0
        if re.search(r"(?:^|[./-])(?:in-en|en-in)(?:[/.-]|$)", result.url, re.I):
            return 0
        if _NON_INDIA_PATH.search(f"{urlsplit(result.url).path}/"):
            return 2
        return 1

    return sorted(candidates, key=rank)


_VIDEO_HOSTS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "dailymotion.com",
    "facebook.com",
    "instagram.com",
    "tiktok.com",
)
_VIDEO_WORDS = re.compile(
    r"\b(video|videos|youtube|watch|clip|clips|tutorial|walkthrough|unboxing)\b", re.I
)
_PRODUCT_WORDS = re.compile(
    r"\b(price|prices|cheapest|buy|cost|deal|deals|specs|specifications|inr|rupees)\b",
    re.I,
)
_PRODUCT_URL_HINT = re.compile(
    r"/(?:shop|shopping|store|product|products|listing|listings|buy|catalog|catalogue|dp|p)/"
    r"|[?&](?:k|q|rh)=",
    re.I,
)


def _wants_video(request: str) -> bool:
    return bool(_VIDEO_WORDS.search(request))


def _is_video(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == name or host.endswith("." + name) for name in _VIDEO_HOSTS)


def _drop_videos(candidates: list[SearchResult], request: str) -> list[SearchResult]:
    """Remove videos from a non-video request; never empty the candidate list."""
    if _wants_video(request):
        return candidates
    kept = [result for result in candidates if not _is_video(result.url)]
    return kept or candidates


def _prefer_product_pages(candidates: list[SearchResult], request: str) -> list[SearchResult]:
    """Put shop/listings pages first for price requests without dropping any."""
    if not _PRODUCT_WORDS.search(request):
        return candidates

    def rank(result: SearchResult) -> int:
        if _PRODUCT_URL_HINT.search(result.url):
            return 0
        if _PRODUCT_WORDS.search(result.title) or _PRODUCT_WORDS.search(result.description):
            return 1
        return 2

    return sorted(candidates, key=rank)


def resolve_request(
    request: str,
    *,
    settings: Settings | None = None,
    llm: LLMClient | None = None,
    search: Any = None,
    search_provider: str = "tinyfish",
    allow_product_research: bool = False,
    session_context: dict[str, Any] | None = None,
) -> DiscoveryResult:
    """Classify one request, search if needed, and select one real result URL."""
    if not request.strip():
        raise DiscoveryError("Enter a URL or describe what you want to find.")
    owned_llm = llm is None
    if llm is None:
        if settings is None:
            raise ConfigurationError("Model settings are required to interpret a request.")
        llm = ModelClient(settings)
    owned_search = search is None
    try:
        intent_response: LLMResponse
        prompt = (PROMPTS / "discover.md").read_text(encoding="utf-8")
        if allow_product_research:
            prompt = (PROMPTS / "product_intent.md").read_text(encoding="utf-8")
        try:
            user_content = request
            if session_context and session_context.get("turns"):
                user_content = json.dumps(
                    {"current_request": request, "prior_session_context": session_context},
                    ensure_ascii=False,
                )
            intent_response = llm.chat(
                "product_intent" if allow_product_research else "discover",
                [
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": user_content},
                ],
                role="strong",
                max_tokens=4096,
            )
            intent = RequestIntent.model_validate(parse_json(intent_response.text))
        except (ScoutError, ValidationError, ValueError) as error:
            if allow_product_research:
                raise DiscoveryError(
                    "Could not understand the product request. Restate the product, required "
                    "specs, budget, and country if relevant."
                ) from None
            raise DiscoveryError(f"Could not understand that request: {error}") from None

        if allow_product_research and intent.product_research is not None:
            plan = intent.product_research
            if plan.scope == "url" and plan.url not in request:
                raise DiscoveryError("A research URL must appear in the original request.")
            return DiscoveryResult(
                intent.route,
                intent.query,
                "",
                plan.url,
                intent.instruction,
                0,
                0,
                intent_response,
                None,
                product_research=plan,
            )

        if intent.route == "direct_url":
            if not intent.url or intent.url not in request or not _http_url(intent.url):
                raise DiscoveryError(
                    "A direct URL must be a public HTTP(S) URL included in your request."
                )
            return DiscoveryResult(
                intent.route, "", "", intent.url, intent.instruction, 0, 0, intent_response, None
            )

        if not intent.query:
            raise DiscoveryError("Could not form a useful search query from that request.")
        query = intent.query
        domain = None
        if intent.route == "site_search":
            domain = _domain(intent.site_domain)
            if domain in {"www.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}:
                domain = "youtube.com"

        if search is None:
            if domain == "youtube.com":
                from scout.youtube import YouTubeClient

                search = YouTubeClient()
                search_provider = "youtube"
            elif search_provider in {"tinyfish", "auto"}:
                tinyfish_key = os.getenv("TINYFISH_API_KEY", "").strip()
                if tinyfish_key:
                    from scout.tinyfish import TinyFishClient

                    search = TinyFishClient(tinyfish_key)
                    search_provider = "tinyfish"
                elif os.getenv("TAVILY_API_KEY", "").strip():
                    search = TavilySearchClient(os.getenv("TAVILY_API_KEY", ""))
                    search_provider = "tavily"
                elif search_provider == "auto":
                    from scout.tinyfish import TinyFishClient

                    search = TinyFishClient("")
                else:
                    from scout.tinyfish import TinyFishClient

                    search = TinyFishClient("")
            elif search_provider == "tavily":
                search = TavilySearchClient(os.getenv("TAVILY_API_KEY", ""))
            else:
                raise ConfigurationError("Search provider must be tinyfish, tavily, or auto.")

        started = time.perf_counter()
        results = search.search(query, include_domains=[domain] if domain else None)
        search_latency_ms = int((time.perf_counter() - started) * 1000)
        candidates = [result for result in results if _http_url(result.url)]
        if domain:
            candidates = [result for result in candidates if _matches_domain(result.url, domain)]
        candidates = _drop_videos(candidates, request)
        candidates = _prefer_locale(candidates, request)
        candidates = _prefer_product_pages(candidates, request)
        if not candidates:
            raise DiscoveryError("No suitable page was found for that request.")

        selection_prompt = (PROMPTS / "select_page.md").read_text(encoding="utf-8")
        candidate_data = [
            {
                "index": index,
                "title": result.title,
                "url": result.url,
                "description": result.description,
            }
            for index, result in enumerate(candidates)
        ]
        selection_warnings: list[str] = []
        selection_response: LLMResponse | None = None
        try:
            selection_response = llm.chat(
                "discover_select",
                [
                    {"role": "system", "content": selection_prompt},
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"request": request, "candidates": candidate_data},
                            ensure_ascii=False,
                        ),
                    },
                ],
                role="fast",
                max_tokens=1024,
            )
            selection = _selection(parse_json(selection_response.text))
        except (ScoutError, ValidationError, ValueError) as error:
            selection_warnings.append(
                f"Could not choose among search results ({error}); using the top result instead."
            )
            chosen = candidates[0]
        else:
            if selection.candidate_index >= len(candidates):
                selection_warnings.append(
                    "The selected result was invalid; using the top result instead."
                )
                chosen = candidates[0]
            else:
                chosen = candidates[selection.candidate_index]
        return DiscoveryResult(
            intent.route,
            f"site:{domain} {query}" if domain else query,
            chosen.title,
            chosen.url,
            intent.instruction,
            len(candidates),
            search_latency_ms,
            intent_response,
            selection_response,
            search_provider,
            tuple(selection_warnings),
        )
    finally:
        if owned_search and search is not None:
            search.close()
        if owned_llm:
            llm.close()
