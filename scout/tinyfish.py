"""TinyFish Search/Fetch only. Never invokes Agent, Browser, or wallet operations."""

from html import escape
from typing import Any

import httpx

from scout.cache import Cache, digest
from scout.discovery import SearchResult, _http_url, _matches_domain
from scout.errors import ConfigurationError, DiscoveryError, FetchError
from scout.fetch import FetchedPage
from scout.security import Resolver, resolve_host, validate_url


class TinyFishClient:
    def __init__(
        self,
        api_key: str,
        *,
        http_client: httpx.Client | None = None,
        cache: Cache | None = None,
        resolver: Resolver = resolve_host,
    ):
        if not api_key.strip():
            raise ConfigurationError("Set TINYFISH_API_KEY in the project .env.")
        self._key = api_key.strip()
        self._owned = http_client is None
        self._client = http_client or httpx.Client(trust_env=False, follow_redirects=False)
        self.cache = cache if cache is not None else Cache()
        self.resolver = resolver
        # Conservative local cap, well below published daily free allowances.
        # Other apps/accounts using the same key are outside this counter's scope.
        self.daily_limits = {"search": 12000, "fetch": 1000}
        self.daily_limit: int | None = None  # Optional stricter local override.

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def _request(self, operation: str, **kwargs: Any) -> dict[str, Any]:
        error_type = DiscoveryError if operation == "search" else FetchError
        scope = f"tinyfish:{operation}:{digest(self._key)}"
        if not self.cache.reserve_request(scope, self.daily_limit or self.daily_limits[operation]):
            raise error_type("TinyFish local daily limit reached; wait for the UTC daily reset.")
        try:
            with self._client.stream(
                "GET" if operation == "search" else "POST",
                f"https://api.{operation}.tinyfish.ai",
                headers={"X-API-Key": self._key, "Accept": "application/json"},
                timeout=150 if operation == "fetch" else 20,
                **kwargs,
            ) as response:
                if response.status_code != 200:
                    messages = {
                        401: "TinyFish rejected the key; check TINYFISH_API_KEY.",
                        402: "TinyFish allowance exhausted; stopped without top-up or retry.",
                        403: "TinyFish denied access; no alternate provider was attempted.",
                        429: "TinyFish rate limit reached; wait before retrying.",
                    }
                    raise error_type(
                        messages.get(
                            response.status_code, f"TinyFish returned HTTP {response.status_code}."
                        )
                    )
                data = bytearray()
                for chunk in response.iter_bytes(chunk_size=65_536):
                    data.extend(chunk)
                    if len(data) > 6 * 1024 * 1024:
                        raise error_type("TinyFish response exceeds the 6 MiB limit.")
            import json

            value = json.loads(data)
        except httpx.HTTPError:
            raise error_type("TinyFish request failed or timed out; no automatic retry.") from None
        except (ValueError, RecursionError):
            raise error_type("TinyFish returned invalid JSON.") from None
        if not isinstance(value, dict):
            raise error_type("TinyFish returned an invalid response.")
        return value

    def search(
        self,
        query: str,
        *,
        include_domains: list[str] | None = None,
        location: str | None = None,
    ) -> list[SearchResult]:
        params = {"query": query}
        if location is not None:
            location = location.strip().upper()
            if len(location) != 2 or not location.isascii() or not location.isalpha():
                raise DiscoveryError("TinyFish search location must be a two-letter country code.")
            params["location"] = location
        if include_domains:
            params["include_domains"] = ",".join(include_domains)
        data = self._request("search", params=params)
        rows = data.get("results")
        if not isinstance(rows, list):
            raise DiscoveryError("TinyFish returned invalid search results.")
        output = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url, title, snippet = row.get("url"), row.get("title"), row.get("snippet")
            if not isinstance(url, str) or not _http_url(url) or not isinstance(title, str):
                continue
            if include_domains and not any(_matches_domain(url, d) for d in include_domains):
                continue
            output.append(
                SearchResult(title[:300], url, snippet[:1200] if isinstance(snippet, str) else "")
            )
            if len(output) == 5:
                break
        return output

    def fetch(self, url: str, *, force_refresh: bool = False) -> FetchedPage:
        target = validate_url(url, resolver=self.resolver)
        data = self._request(
            "fetch",
            json={
                "urls": [target.url],
                "format": "html",
                "ttl": 0 if force_refresh else 21600,
                "per_url_timeout_ms": 45000,
            },
        )
        if data.get("errors"):
            raise FetchError(
                "TinyFish could not read this page (blocked, unavailable, or login required); "
                "no alternate fetch attempted."
            )
        rows = data.get("results")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise FetchError("TinyFish returned no usable page.")
        row = rows[0]
        if row.get("url") != target.url or row.get("format") != "html":
            raise FetchError("TinyFish returned an unexpected URL or content format.")
        final_url = row.get("final_url") or target.url
        if not isinstance(final_url, str):
            raise FetchError("TinyFish returned an invalid final URL.")
        final = validate_url(final_url, resolver=self.resolver)
        content = row.get("text")
        if not isinstance(content, str) or not content.strip():
            raise FetchError("TinyFish returned empty page content.")
        title = row.get("title")
        if isinstance(title, str):
            content = (
                f"<html><head><title>{escape(title)}</title></head><body>{content}</body></html>"
            )
        return FetchedPage(
            final.url,
            content,
            200,
            "text/html",
            tier="tinyfish",
            warnings=[
                "Source: TinyFish extracted HTML, not a full original DOM; "
                "content is sent to TinyFish."
            ],
        )
