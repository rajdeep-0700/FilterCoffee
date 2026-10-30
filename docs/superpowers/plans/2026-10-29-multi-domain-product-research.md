# Multi-domain product research implementation plan

**Design:** [`../specs/2026-10-29-multi-domain-product-research-design.md`](../specs/2026-10-29-multi-domain-product-research-design.md)  
**Status:** Ready for implementation  
**Scope:** Product comparison workflow across public web pages, for any product category.

## Goal

For a plain-language product comparison request, classify its scope and criteria with Nemotron, investigate relevant actual product pages with Gemini-directed TinyFish Search/Fetch, and present evidence-backed comparisons with a second and final Nemotron call. Open-web requests inspect up to 15 relevant domains; each domain can contribute up to 10 fetched listings. One run is capped at 200 combined TinyFish Search and Fetch attempts.

## Non-goals

- No TinyFish paid agent/research mode or paid model fallback.
- No claim to exhaustive crawling or guaranteed global lowest price.
- No login, shopping cart, checkout, CAPTCHA solving, paywall bypass, proxy evasion, or private data access.
- No change to ordinary single-page extraction behavior except explicit request classification into product comparison.
- No country inference from IP, device location, or browser locale.

## Constraints and agreed behavior

- Nemotron: exactly two calls on a successful product-research run—request distillation, final presentation. No implicit planning/repair/retry calls to Nemotron.
- Gemini: owns bounded research planning, search iteration, product extraction, comparisons, and partial-result synthesis. Its limits must be explicit and partial results reported on exhaustion.
- TinyFish: Search/Fetch only; backend-owned, validated tool execution. Count successful and failed attempts toward the shared 200-call run budget.
- Search bounds: at least 10 and at most 20 broad domain-discovery searches for open-web product research, continuing until three consecutive queries reveal no new domains; no more than two targeted searches per selected domain; no more than 10 fetched listing pages per domain. This arithmetic fits the 200-call maximum at 15 domains.
- User-facing bounds: up to 15 relevant domains and up to 10 qualifying products per domain; fewer are valid. Never pad with irrelevant domains or non-matches.
- Scope precedence: URL starts within its same main domain and subdomains; named retailer stays on that site; otherwise open-web discovery. Explicit country selection overrides country stated in the request. A country-domain variant is a separate market.
- Hard constraints (including budget) exclude a product when contradicted or unknown. Preferences only affect ranking among qualifying candidates.
- Reuse existing SSRF, robots, response-size, and URL protections for every result and follow-up fetch.
- No secrets in browser state or model prompts. The user is told request/page text is sent to Gemini/TinyFish.

## Slices (ordered; keep each independently testable)

### 1. Pin the request and usage contracts

**Files:** `scout/contracts.py`, `scout/web_service.py`, focused tests.

- Add strict Pydantic contracts for the first Nemotron intent, research scope, hard constraints/preferences, candidate listing, normalized product facts, evidence, domain coverage, and search/fetch usage.
- Separate ordinary extraction, open-web product comparison, named-site comparison, and URL-seeded comparison without changing existing direct-URL semantics.
- Add a per-run usage budget object that reserves every TinyFish search/fetch attempt before the network request and refuses work once 200 combined attempts are reserved. Include failures in usage.
- Keep TinyFish provider daily safeguards separate. Verify Search and Fetch daily limits from current provider/account docs, then replace the local 100/day-per-operation cap only as justified; never raise it based solely on the Search limit if Fetch has a different limit.

**Verification:** contract tests for malformed/extra fields, Unicode, URL/site/country precedence, limits at 199/200/201, failed-attempt reservation, and separate daily-vs-per-run accounting. No network calls.

### 2. Add Nemotron request distillation and preserve the old route

**Files:** `prompts/discover.md` or a product-specific prompt, `scout/discovery.py`, `scout/web_service.py`, tests.

- Have the first Nemotron request classify URL, named site, or open web; distinguish product category/brand/site names; parse hard constraints, preferences, requested fields, and country; return validated structured JSON.
- Do not ask this call to search, choose one result, or make a product recommendation.
- Keep existing non-comparison Auto, URL, manual-import, and explicit-source behavior unchanged.
- Ensure bad/empty distillation stops clearly without hidden Nemotron retry or accidental fallback to one-page product lookup.

**Verification:** offline tests for laptop example, other categories (e.g. camera and appliance), product brand vs retailer, direct URL, named site, explicit/request country precedence, non-product query route, missing budget/spec, and malformed intent. Assert exactly one Nemotron call at this stage and none for unchanged existing call paths beyond their current behavior.

### 3. Build the constrained Gemini research loop and TinyFish tools

**Files:** new focused orchestration module(s), `scout/tinyfish.py` as needed, `scout/web_service.py`, tests.

- Implement a JSON action protocol for Gemini to request bounded domain discovery, domain-scoped searches, candidate fetches, and completion/partial synthesis. Gemini does not issue arbitrary HTTP requests or receive API keys.
- Execute TinyFish Search/Fetch only in the Python backend. Validate all search arguments, result URLs, redirects/final URLs, host scope, and request budget through existing safeguards.
- Search the open web for relevant domains (up to 20 search requests); select no more than 15 relevant distinct domains; stop once new relevant domains are exhausted.
- For each selected domain, allow up to two distinct targeted queries and at most 10 candidate-page fetches. Deduplicate candidates and queries; honor the total 200-attempt budget across discovery/search/fetch.
- Add explicit Gemini call/token and wall-clock bounds as builder choices, with partial-result behavior and visible reason on stop. Batch actions/results where practical to avoid wasteful per-item model calls.
- Progress events identify discovery, per-domain search/fetch, extraction, and stop reasons without leaking model prompts, secrets, or full imported content.

**Verification:** fake Gemini action sequences + mocked TinyFish HTTP transport. Test per-domain scope, candidate deduplication, repeated actions, invalid model URLs, blocked/private IPs, redirect escape, robots denial, provider 401/402/403/429, timeout, empty results, request reservation, exact 200 maximum, early stop, and partial results. Assert no paid/provider switching.

### 4. Fetch and compare actual product pages with traceable evidence

**Files:** orchestration/data-contract modules, extraction validation, tests.

- Fetch candidate listing pages; never treat search snippets as confirmed specifications or price.
- Extract product identity/configuration, displayed price/currency, stock/condition when present, user-requested fields, and evidence per value.
- Match exact configurations; do not merge product variants. Preserve conflicting values and timestamps. Unknown hard constraints do not pass.
- Apply hard constraints first; rank only qualifying products using user preferences. Return up to 10 per domain, including one or fewer when that is all the evidence supports.
- Have Gemini produce a compact structured comparison and research coverage summary for Nemotron's final presentation.

**Verification:** fixture-based tests for different RAM/storage variants, price vs sale price, currency, missing/contradictory fields, refurbished vs new, requirements that no listing meets, fewer than 10 matches, duplicated URLs, and ranking where a more expensive product is a better fit. Ensure every displayed fact resolves to evidence and the requested source.

### 5. Add final Nemotron presentation and usage/progress reporting

**Files:** `scout/web_service.py`, LLM accounting, `scout/contracts.py`, tests.

- Send only the bounded structured comparison to Nemotron for final wording and organization. Preserve all product facts, evidence, ordering, uncertainty, and partial status; prohibit new facts and unsupported rankings.
- Record model provider/stage calls and TinyFish searches/fetches separately. Successful product runs must show exactly two Nemotron calls; Gemini calls may be multiple but bounded.
- Surface coverage: domains found/attempted, searches, pages fetched, qualifying listings, exclusions, unknowns, and why the run stopped.
- Preserve partial findings on provider quota exhaustion or deadline where possible. Never silently report a complete comparison when a budget stopped the run.

**Verification:** stage/provider ledger tests for exactly two Nemotron calls, no Nemotron retries on malformed output, Gemini stop/partial flows, final formatter attempting unsupported changes, token/context bounding, and cached/partial usage accuracy.

### 6. Wire country input and evidence-led comparison UI

**Files:** `scout/web_assets/index.html`, `scout/web_assets/app.js`, `scout/web_assets/styles.css`, UI tests.

- Add an optional country/market selector or input for product research; pass it to the backend. Explicit UI value wins over country parsed from the request. Do not use geolocation.
- Keep the existing one-page and manual-import modes recognizable. Product comparison is selected from the request intent, not by forcing users to understand provider modes.
- Display ranked qualifying products per domain, exact variant, price/currency, checked time, source link, evidence, and unknown/failed hard constraints. Show the 15-domain/10-results ceilings and whether the 200-attempt budget curtailed coverage.
- Ensure activity/history and exports retain per-domain provenance and partial status; never expose credentials.

**Verification:** Playwright offline tests for country selection/precedence, one-page route unchanged, 15-domain and 10-result rendering, partial/error statuses, source/evidence links, keyboard and mobile layout, and secret-free UI. No provider calls.

## Acceptance criteria

- One-page requests still use the existing one-page path.
- Product comparisons use no more than two Nemotron calls on success and never silently add Nemotron calls for retries, per-page extraction, or repair.
- Open web considers 1–15 relevant domains; named-site and URL-seeded modes stay in their approved scope; results are country-aware only when the user supplies a country.
- TinyFish Search + Fetch attempts combined never exceed 200 in one run, including failed requests; domain discovery ≤20, targeted searches ≤2/domain, fetched listing pages ≤10/domain.
- Return no more than 10 qualifying results per domain and no invented minimum; enforce hard criteria and disclose unknowns.
- Search snippets are not presented as verified product-page facts; shown values retain per-field source evidence and checked time.
- All URLs pass existing safety controls. Provider quota exhaustion/deadline returns explicit partial status and no paid fallback.
- Tests cover ordinary route regression, malformed/empty model outputs, URL/site/country scope, budget boundaries, variants/pricing, blocked sources, quota failure, and partial findings.

## Risks and mitigations

- **Provider allowance mismatch:** verify Search and Fetch quotas separately; preserve provider-specific daily controls, with the agreed independent 200/run cap.
- **Search coverage is incomplete:** report domains/pages inspected and never claim exhaustive or guaranteed cheapest.
- **Cost/latency/context growth:** enforce the shared TinyFish cap plus explicit Gemini iteration/token/time bounds; compact evidence before final Nemotron call.
- **Retailer anti-bot or JS pages:** stop on denial; don't bypass controls. Report inaccessible domains and partial coverage.
- **Price/config mismatch:** require variant-aware evidence and exclude unknown hard constraints.
- **Tests are gitignored:** run local tests explicitly with `--no-respect-gitignore`; ensure contributor/CI test availability is resolved without weakening tests.

## Verification and delivery contract

1. For each slice, add a regression test first and run its focused tests.
2. Run `uv run pytest -q` once after integrating all slices; report any pre-existing failures distinctly. Existing known baseline: the prior focused command `uv run pytest -q tests/test_discovery.py tests/test_research.py tests/test_sources_pipeline.py` had three failures, including two TinyFish fallback expectation failures; re-establish the baseline before implementation.
3. Run Ruff, Pyright, and `node --check scout/web_assets/app.js`; run Playwright UI tests offline.
4. Do not use real provider quotas in tests. Any live smoke test requires explicit user approval and reports exact TinyFish/Gemini usage.
5. No commit/push. Do not overwrite the pre-existing uncommitted frontend changes; review them before touching the same files.

## Stop condition

Stop and ask for a product decision if actual TinyFish Fetch allowances cannot support the 200 combined per-run limit, if preserving exactly two Nemotron calls conflicts with the selected model/API, or if hard-constraint matching cannot be represented without silently guessing. Otherwise deliver the slices in order, keeping each runnable and verifiable before expanding coverage.
