"""Local workspace requests, real scraper execution, and bounded saved history."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import asdict
from importlib.util import find_spec
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from scout.browser import BrowserRenderer, CloakBrowserRenderer
from scout.cache import Cache, digest
from scout.config import load_environment, load_settings
from scout.contracts import ExtractResult
from scout.countries import country_name, country_options
from scout.discovery import resolve_request
from scout.errors import ConfigurationError, ScoutError
from scout.fetch import Fetcher
from scout.gemini import ExtractionRouter, GeminiExtractionClient
from scout.limits import get_openrouter_key_status
from scout.llm import ChatMessages, LLMClient, LLMResponse, ModelClient, Role
from scout.manual import ManualFetcher
from scout.pipeline import RunOptions, run
from scout.product_research import ProductResearch
from scout.research import research
from scout.sources.opencli import OpenCLISource
from scout.sources.registry import build_registry
from scout.tinyfish import TinyFishClient

Emit = Callable[[str, str], None]
SOURCES = {"web", "github", "hn", "stackexchange", "bluesky", "youtube", "opencli"}
SUFFIXES = {".txt", ".md", ".markdown", ".html", ".htm"}


def public_url(value: str) -> str:
    """Syntax check only; the extraction pipeline performs DNS/SSRF checks."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Use a complete HTTP(S) URL.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URLs must not contain credentials.")
    return value


class WebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    request: str = Field(min_length=1, max_length=6000)
    mode: Literal["auto", "sources", "url", "manual"] = "auto"
    country: str = Field(default="", max_length=80)
    sources: list[str] = Field(default_factory=list, max_length=7)
    url: str = Field(default="", max_length=2000)
    browser: Literal["auto", "off", "chromium", "cloak"] = "auto"
    max_items: int = Field(default=5, ge=1, le=100)
    source_limit: int = Field(default=5, ge=1, le=10)
    refresh: bool = False
    opencli: bool = False
    filename: str = Field(default="", max_length=255)
    content: str = Field(default="", max_length=5 * 1024**2)

    @field_validator("request")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Describe what you want Scout to find.")
        return value.strip()

    @model_validator(mode="after")
    def consistent(self) -> WebRequest:
        if any(name not in SOURCES for name in self.sources):
            raise ValueError("Unknown research source.")
        self.sources = list(dict.fromkeys(self.sources))
        if self.mode == "sources":
            if not self.sources:
                raise ValueError("Select at least one source.")
            if "youtube" in self.sources and len(self.sources) != 1:
                raise ValueError("YouTube is a separate video search; select it on its own.")
            if "opencli" in self.sources and not self.opencli:
                raise ValueError("Enable OpenCLI before choosing that source.")
        elif self.sources or self.opencli:
            raise ValueError("Source selections require Choose sources mode.")
        if self.mode in {"sources", "manual"} and self.browser not in {"auto", "off"}:
            raise ValueError("Forced browser rendering applies only to web page requests.")
        if self.mode in {"auto", "sources"} and self.url:
            raise ValueError("Use Read a specific URL mode to supply a page URL.")
        if self.mode in {"url", "manual"} and self.url:
            public_url(self.url)
        if self.mode == "url" and not self.url:
            raise ValueError("Enter the page URL.")
        if self.mode == "manual":
            if Path(self.filename).suffix.casefold() not in SUFFIXES:
                raise ValueError("Choose a .txt, .md, .markdown, .html or .htm file.")
            if not self.content.strip():
                raise ValueError("The imported file is empty.")
            if len(self.content.encode("utf-8")) > 5 * 1024**2:
                raise ValueError("The file must be 5 MiB or smaller.")
        elif self.content or self.filename:
            raise ValueError("Files require Import saved page mode.")
        return self


def opencli_environment(enabled: bool) -> dict[str, str]:
    values = dict(os.environ)
    values["SCOUT_ENABLE_OPENCLI"] = "1" if enabled else "0"
    # A safe, browserless default. Custom sites still require the .env allowlist.
    if not values.get("SCOUT_OPENCLI_SITE", "").strip():
        values.update(
            SCOUT_OPENCLI_SITE="hackernews",
            SCOUT_OPENCLI_COMMAND="search {query} --limit {limit} -f json",
            SCOUT_OPENCLI_ALLOWLIST="hackernews",
        )
    return values


def capabilities() -> dict[str, Any]:
    load_environment()
    source = OpenCLISource.from_env(opencli_environment(True))
    status = source.status()
    return {
        "keys": {
            "openrouter": bool(os.getenv("OPENROUTER_KEY") or os.getenv("OPENROUTER_API_KEY")),
            "gemini": bool(os.getenv("GEMINI_KEY")),
            "web": bool(os.getenv("TINYFISH_API_KEY") or os.getenv("TAVILY_API_KEY")),
        },
        "web_provider": "TinyFish" if os.getenv("TINYFISH_API_KEY") else "Tavily",
        "cloak": find_spec("cloakbrowser") is not None,
        "opencli": {"available": status.available, "reason": status.reason, "site": source.site},
        "countries": country_options(),
    }


def quota_status() -> dict[str, Any]:
    settings = load_settings()
    if settings.provider != "openrouter":
        raise ConfigurationError("Set SCOUT_PROVIDER=openrouter for the free-only workspace.")
    return asdict(get_openrouter_key_status(settings.api_key.get_secret_value(), settings.base_url))


class ProgressClient:
    """Observe actual model stages, without adding inference requests."""

    def __init__(self, inner: LLMClient, emit: Emit):
        self.inner, self.emit = inner, emit
        self.calls, self.warnings = inner.calls, inner.warnings

    def chat(
        self,
        stage: str,
        messages: ChatMessages,
        *,
        role: Role = "fast",
        max_tokens: int = 4096,
        require_vision: bool = False,
    ) -> LLMResponse:
        labels = {
            "discover": "Understanding request",
            "product_intent": "Understanding request",
            "product_present": "Formatting comparison",
            "product_research": "Comparing products",
            "discover_select": "Selecting a result",
            "research_plan": "Planning research",
            "plan": "Planning fields",
            "extract": "Extracting",
            "repair": "Checking evidence",
        }
        label = labels.get(stage, stage)
        self.emit(label, "Waiting for the model. Free providers can take a minute or more.")
        response = self.inner.chat(
            stage, messages, role=role, max_tokens=max_tokens, require_vision=require_vision
        )
        self.emit(label, f"Finished · {response.model}")
        return response

    def close(self) -> None:
        self.inner.close()


def current_usage(result: ExtractResult, client: LLMClient) -> dict[str, Any]:
    """A cached result must not display historical inference as new API usage."""
    result.meta.model_calls = list(client.calls)
    result.meta.prompt_tokens = sum(call.prompt_tokens for call in client.calls)
    result.meta.completion_tokens = sum(call.completion_tokens for call in client.calls)
    result.meta.estimated_cost_usd = sum(call.cost_usd for call in client.calls)
    return result.model_dump(mode="json")


def execute_request(
    request: WebRequest, emit: Emit, *, cache_path: str | Path = "data/cache.db"
) -> dict[str, Any]:
    settings = load_settings()
    if settings.provider != "openrouter":
        raise ConfigurationError("Set SCOUT_PROVIDER=openrouter; the workspace is free-only.")
    with ExitStack() as stack:
        model = ModelClient(settings)
        stack.callback(model.close)
        gemini = GeminiExtractionClient(os.getenv("GEMINI_KEY", ""))
        stack.callback(gemini.close)
        client = ProgressClient(ExtractionRouter(model, gemini), emit)
        options = RunOptions(
            max_items=request.max_items,
            force_refresh=request.refresh,
            routing="gemini",
            use_browser=request.browser != "off",
            force_browser=request.browser in {"chromium", "cloak"},
            fetch_backend="http" if request.browser in {"chromium", "cloak"} else "auto",
        )
        if request.mode == "sources" and request.sources != ["youtube"]:
            env = opencli_environment(request.opencli)
            registry = build_registry(user_agent=settings.user_agent, env=env)
            stack.callback(registry.close)
            emit("Searching sources", ", ".join(request.sources))
            result = research(
                request.request,
                settings=settings,
                llm=client,
                registry=registry,
                source_names=request.sources,
                limit=request.source_limit,
                options=options,
                cache_path=cache_path,
            )
            return current_usage(result, client)

        discovery = None
        discovery_payload: dict[str, Any] | None = None
        fetcher = None
        instruction = request.request
        url = request.url
        if request.mode == "manual":
            folder = stack.enter_context(TemporaryDirectory(prefix="scout-import-"))
            file = Path(folder) / ("import" + Path(request.filename).suffix.casefold())
            file.write_text(request.content, encoding="utf-8")
            fetcher = ManualFetcher(file, citation_url=url or None, title=request.filename)
            url = fetcher.url
            emit("Reading saved file", request.filename)
        elif request.mode != "url" or request.url:
            query = request.request + (" " + request.url if request.mode == "url" else "")
            if request.country:
                query += (
                    "\nSelected country/market (overrides country in request): " + request.country
                )
            if request.sources == ["youtube"]:
                query = f"Search YouTube for a video: {query}"
            emit("Searching", "Finding a public page or video for your request.")
            cache = Cache(cache_path)
            discovery_key = digest("discover:v2:" + " ".join(query.casefold().split()))
            saved = None if request.refresh else cache.get_discovery(discovery_key)
            if saved:
                url = saved["url"]
                instruction = saved.get("instruction") or request.request
                discovery_payload = saved
                emit("Selected source", f"{saved.get('title') or url} · {url}")
                emit(
                    "Reused saved search",
                    "Same request, same source. Enable Refresh to search again.",
                )
            else:
                discovery = resolve_request(
                    query,
                    settings=settings,
                    llm=client,
                    search_provider="auto",
                    allow_product_research=request.mode in {"auto", "url"},
                )
                if discovery.product_research is not None:
                    product_plan = discovery.product_research.model_copy(deep=True)
                    if request.country:
                        product_plan.country = country_name(request.country)
                    if request.mode == "url":
                        product_plan.scope, product_plan.url = "url", request.url
                    tiny = TinyFishClient(os.getenv("TINYFISH_API_KEY", ""), cache=cache)
                    stack.callback(tiny.close)
                    checker = stack.enter_context(Fetcher(user_agent=settings.user_agent))
                    # Router leaves final formatting on Nemotron; only this loop uses Gemini.
                    return ProductResearch(
                        product_plan,
                        gemini=ProgressClient(gemini, emit),
                        nemotron=client,
                        tinyfish=tiny,
                        robots_check=checker.check_robots,
                        resolver=tiny.resolver,
                        emit=emit,
                    ).run(request.request)
                url, instruction = discovery.url, discovery.instruction
                emit("Selected source", f"{discovery.title or url} · {url}")
                discovery_payload = {
                    "provider": discovery.search_provider,
                    "query": discovery.query,
                    "candidates": discovery.search_result_count,
                    "title": discovery.title,
                    "url": discovery.url,
                    "instruction": discovery.instruction,
                    "warnings": list(discovery.warnings),
                }
                cache.set_discovery(discovery_key, discovery_payload)
        renderer = None
        if request.browser in {"chromium", "cloak"}:
            if request.browser == "cloak" and find_spec("cloakbrowser") is None:
                raise ConfigurationError(
                    "CloakBrowser is not installed. Run uv sync --extra cloak."
                )
            checker = stack.enter_context(Fetcher(user_agent=settings.user_agent))
            renderer = (
                CloakBrowserRenderer(
                    user_agent=settings.user_agent,
                    robots_check=checker.check_robots,
                    license_key=os.getenv("CLOAKBROWSER_LICENSE_KEY") or None,
                )
                if request.browser == "cloak"
                else BrowserRenderer(user_agent=settings.user_agent)
            )
        elif request.browser == "auto":
            # UI controls, not the process-wide environment, choose the browser for this run.
            renderer = BrowserRenderer(user_agent=settings.user_agent)
        emit("Reading source", "Fetching content, then extracting and checking values.")
        result = run(
            url,
            instruction,
            options=options,
            settings=settings,
            fetcher=fetcher,
            llm=client,
            browser_renderer=renderer,
            cache_path=cache_path,
        )
        if discovery:
            result.meta.warnings = list(dict.fromkeys([*discovery.warnings, *result.meta.warnings]))
        elif discovery_payload:
            result.meta.warnings = list(
                dict.fromkeys([*discovery_payload.get("warnings", []), *result.meta.warnings])
            )
        if request.mode == "manual" and not request.url:
            result.url = result.meta.url = "manual:" + request.filename
        output = current_usage(result, client)
        if discovery_payload:
            output["discovery"] = {
                "provider": discovery_payload.get("provider", "cache"),
                "query": discovery_payload.get("query", ""),
                "candidates": discovery_payload.get("candidates", 0),
                "title": discovery_payload.get("title", ""),
                "url": discovery_payload.get("url", url),
                "reused": discovery is None,
            }
        return output


class JobStore:
    """One active run at a time; the most recent 30 completed runs stay on disk."""

    def __init__(self, path: str | Path, *, runner: Callable[..., dict] = execute_request):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.runner = runner
        self.lock = threading.RLock()
        self.active: dict[str, Any] | None = None
        self.worker: threading.Thread | None = None
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, data TEXT NOT NULL)")

    def start(self, request: WebRequest) -> dict[str, Any]:
        with self.lock:
            if self.active is not None:
                raise RuntimeError("A research run is already in progress. Wait for it to finish.")
            job = {
                "id": uuid.uuid4().hex,
                "request": request.request,
                "mode": request.mode,
                "created": time.time(),
                "status": "running",
                "events": [],
                "result": None,
                "error": None,
                "elapsed": 0,
            }
            self.active = job
            self.worker = threading.Thread(target=self._work, args=(job, request), daemon=True)
            self.worker.start()
            return deepcopy(job)

    def _work(self, job: dict, request: WebRequest) -> None:
        started = time.monotonic()

        def emit(stage: str, detail: str) -> None:
            with self.lock:
                job["events"].append({"stage": stage, "detail": detail, "at": time.time()})
                job["events"] = job["events"][-100:]

        try:
            output = self.runner(request, emit)
            with self.lock:
                job["result"] = output
                job["status"] = "complete" if output.get("items") else "empty"
        except Exception as error:
            with self.lock:
                job["status"] = "error"
                job["error"] = (
                    str(error)
                    if isinstance(error, (ScoutError, ValueError))
                    else (
                        f"The run failed ({type(error).__name__}). Check your setup and try again."
                    )
                )
        finally:
            with self.lock:
                job["elapsed"] = round(time.monotonic() - started, 2)
                try:
                    with closing(sqlite3.connect(self.path)) as db, db:
                        db.execute("INSERT INTO runs VALUES (?, ?)", (job["id"], json.dumps(job)))
                        db.execute(
                            "DELETE FROM runs WHERE rowid NOT IN "
                            "(SELECT rowid FROM runs ORDER BY rowid DESC LIMIT 30)"
                        )
                except sqlite3.Error:
                    job["error"] = "Result could not be saved to history; check free disk space."
                    job["status"] = "error"
                # Retain the last job in memory too, including history write errors.
                self.last = deepcopy(job)
                self.active = None

    def get(self, job_id: str) -> dict:
        with self.lock:
            for job in (self.active, getattr(self, "last", None)):
                if job and job["id"] == job_id:
                    return deepcopy(job)
            with closing(sqlite3.connect(self.path)) as db, db:
                row = db.execute("SELECT data FROM runs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            return json.loads(row[0])

    def history(self) -> list[dict]:
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            jobs = [
                json.loads(row[0])
                for row in db.execute("SELECT data FROM runs ORDER BY rowid DESC LIMIT 30")
            ]
            if self.active:
                jobs.insert(0, self.active)
            return [{k: job[k] for k in ("id", "request", "created", "status")} for job in jobs]

    def delete(self, job_id: str) -> None:
        """Delete one completed history entry; active research cannot be removed."""
        with self.lock:
            if self.active and self.active["id"] == job_id:
                raise RuntimeError("An active research session cannot be deleted.")
            with closing(sqlite3.connect(self.path)) as db, db:
                cursor = db.execute("DELETE FROM runs WHERE id = ?", (job_id,))
                if cursor.rowcount == 0:
                    raise KeyError(job_id)
            last = getattr(self, "last", None)
            if last and last["id"] == job_id:
                del self.last

    def wait(self) -> None:
        if self.worker:
            self.worker.join(timeout=10)
