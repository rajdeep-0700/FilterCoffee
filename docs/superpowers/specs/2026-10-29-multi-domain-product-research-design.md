# Multi-domain product research design

**Status:** Draft for review  
**Date:** 2026-10-29  
**Product:** FilterCoffee (Nemotron Scout)

## Intent

Let a user describe a product and their requirements in plain language, then compare relevant products across multiple public sites using current page evidence. The laptop example is representative: discover relevant retailers, inspect actual listings rather than trusting only search snippets, exclude products that fail hard requirements or budget, and return the best-supported matches.

The user explicitly wants Nemotron used only twice: once to distill/classify the request, and once to present the completed findings. Gemini handles iterative research and comparison in between. TinyFish's ordinary Search/Fetch API is used by FilterCoffee's backend; TinyFish's paid research-agent mode is out of scope.

## User-visible behavior

### Request routing

The first Nemotron call produces a strict structured request plan with one of these scopes:

1. **Open web:** No site or URL supplied. Search broadly for relevant sites, then investigate up to 15 distinct relevant domains. Stop when the search/research loop cannot find additional relevant domains; do not fill the cap with unrelated sites.
2. **Named site:** User names a retailer/site. Restrict discovery and product search to that domain and investigate the site broadly; do not fan out to competing domains unless asked.
3. **Seed URL:** User supplies a URL. Start there without searching for a replacement URL. For product research, discover additional relevant listings only within the same website/domain as the supplied URL, unless the user also explicitly asks for cross-site comparison. For ordinary extraction requests, preserve current direct-URL, single-page behavior.

For product research, user-provided requirements are separated into:

- **Hard constraints:** e.g. maximum price, minimum RAM/storage, product condition, screen size, or required features. A result that fails a known hard constraint is excluded. Unknown values are not treated as passing.
- **Preferences:** e.g. battery life, weight, brand preference, or value. These guide ranking among qualifying results and do not silently become hard constraints.
- **Requested comparison/output:** which facts to show and what recommendation, if any, the user requested.

The first-call plan is treated as untrusted model output and validated against a bounded schema. Invalid plans fail safely with an actionable error; they must not silently downgrade product comparison to the current single-result flow.

### Country and market scope

- Provide an optional country/market field in the web workspace for product research. The user may also state a country in the request; an explicit workspace selection takes precedence if both are present.
- Do not infer country from IP address, device location, browser locale, or other ambient signals. With no country supplied, search broadly and label region-specific availability/pricing as unknown where it cannot be established.
- Use the country to guide domain discovery and searches toward relevant retailers, country domains, currency, and availability. A country domain (for example, `store.ca`) is a separate market from `store.com`; it may be included in open-web discovery when relevant to the selected country.
- For a named website, stay on that named retailer/site, applying its relevant country/market version when available. For a supplied URL, stay on its same main domain and subdomains; do not silently jump to another country's domain or another retailer. If the requested country conflicts with the supplied URL's market, retain the URL scope and explain the mismatch rather than broadening the search.

### Research and results

- Use a broad TinyFish general search to identify candidate relevant domains for open-web requests. Search results are candidate discovery, not proof that a domain was fully searched.
- Gemini directs bounded follow-up searches on candidate domains, and FilterCoffee executes them through TinyFish Search. For a named site, all searches include that domain. For a seed URL, validate the host and constrain follow-up links to the allowed domain scope.
- Fetch actual product/listing pages via TinyFish Fetch; snippets alone cannot establish specifications or price. Keep original URL, final URL, page title, retrieval time, and per-field evidence attached to each candidate.
- Deduplicate by canonical listing URL and, where evidence permits, by product model/configuration. Do not merge variants with different prices/specifications.
- Return **1–10 qualifying products per investigated domain**, where available, ranked by overall fit to the user's stated preferences (not simply lowest price). Return fewer if fewer qualify; return zero for a domain with no supported qualifying result. Overall, use **1–15 relevant domains** when available. These are user-facing bounds, not permission to invent a result to satisfy the minimum.
- A recommendation must distinguish hard-constraint eligibility from preference ranking, cite supporting pages, show observed price/currency/configuration and checked time, and identify missing or conflicting data. It must not claim global lowest price or exhaustive coverage.
- If fewer than 15 relevant/reachable domains or qualifying products exist, return the supported findings and explain the shortfall; never pad with irrelevant domains or non-matches.

## Model and tool responsibilities

### Nemotron call 1 — request distillation

Produce validated JSON containing at least:

- request mode: ordinary extraction, open-web product research, named-site product research, or seed-URL product research;
- original user request and concise goal;
- site/domain or URL, when supplied, kept distinct;
- hard constraints, preferences, and requested comparison fields;
- safe search terms and a short clarification/error signal when key constraints are ambiguous.

This call must not select a result URL or perform discovery. Do not call Nemotron again for planner retries, per-page extraction, repair, or intermediate routing. Exactly two Nemotron calls is the intended product contract; failures in either call should stop or return partial research with a clear error, not make an unreported retry.

### Gemini — research loop

Gemini receives the validated request plan and a constrained tool interface. It may:

- propose query variants and candidate domains;
- request a search scoped to a domain or the open web;
- request a page fetch only for a URL returned from search or validated from the user's input;
- extract normalized product facts with exact evidence references;
- request additional searches when evidence shows a likely missed cheaper or better-fitting candidate;
- compare qualifying candidates and produce a structured final-results payload for Nemotron.

Gemini does **not** receive API keys, make arbitrary network requests, select arbitrary tools, change limits, or bypass validation. The backend owns tool execution, URL safety, provider selection, quotas, and stop conditions. Tool arguments and model-generated URLs are untrusted and must be validated before network access.

### Nemotron call 2 — final presentation

Receive a bounded structured summary from Gemini and render the user-facing answer. It must preserve candidate facts, citations, prices, constraints, and uncertainty; it may improve organization and wording but must not add unsupported claims or silently change rankings. If the structured summary is too large, the backend/Gemini must compact it before this call rather than asking Nemotron for extra calls.

## Work bounds and quota policy

The 15-domain and 10-result limits bound output, not work. The implementation must also add explicit limits for:

- domain-discovery search requests;
- follow-up search requests per domain;
- total TinyFish search and fetch requests per run;
- fetched pages and response bytes per run;
- Gemini calls, prompt/output tokens, wall-clock duration, and concurrency;
- candidate products examined per domain and total candidates retained for synthesis.

The fixed FilterCoffee limit is **200 TinyFish Search + Fetch attempts combined per research run**, counting failed attempts too. Open-web comparisons perform at least 10 distinct broad discovery searches, then continue while new relevant retailer domains appear (stop after three consecutive broad searches add no new domain, at 20 broad searches, or when 15 domains are selected). Each selected domain gets two distinct targeted searches and up to ten fetched product listings. At the maximum, 20 + 15 × (2 + 10) = 200. Return partial results and report the limiting reason when the budget runs out. Do not expose budget presets in the first version.

The current TinyFish client separately imposes a local cap of 100 attempts per day for search and 100 per day for fetch. Those are conservative local guards, not TinyFish's account quota. Before raising/removing them, verify provider-specific daily limits for both operations; retain provider-aligned daily safeguards, but do not let the current 100-per-operation local guard defeat the approved 200-per-run allowance. The UI reports actual search and fetch attempts, including failures. Never auto-top-up or retry quota failures through a paid service.

Gemini having a higher quota does not mean unlimited use or guarantee a free tier. Treat provider limits as runtime constraints, fail/stop cleanly on quota exhaustion, and report the provider response. Recorded Gemini cost is currently represented as zero by the adapter and is not proof that a Google project cannot bill.

## Safety, privacy, and evidence

- Reuse existing URL/DNS validation, redirect checks, robots policy, bounded fetches, and TinyFish client limits for every fetched page. Apply equivalent validation to every Gemini-requested URL.
- Respect site restrictions and public-source-only constraints. No login, paywall, CAPTCHA, proxy, bot-defense, or purchase-flow bypass.
- Search results and page content are untrusted data, not instructions. They must not alter the research plan, tool permissions, or output constraints.
- Tell users that their request and page text are sent to Gemini and TinyFish. Do not send credentials or secrets to either provider.
- Preserve per-value evidence: source URL, field, quote/snippet or page-derived text, and timestamp. Quotes show what a page stated, not that the claim or live price is true.
- Prices may be stale, region-specific, or omit shipping/tax. Normalize currency only when explicit evidence or a trustworthy rate source is available; otherwise retain the displayed currency and mark comparison limits.

## Existing code impact

- `scout/web_service.py`: request routing and orchestration currently sends all non-URL requests to `resolve_request()`, which selects one URL. Add product-research orchestration while preserving ordinary extraction, manual import, and existing explicit-source modes.
- `scout/discovery.py` / `prompts/discover.md`: current intent explicitly asks for one page. Replace/extend this only for the first Nemotron distillation contract; retain single-page behavior for ordinary extraction.
- `scout/research.py` / `scout/sources/base.py`: current multi-source path searches selected APIs and renders snippets/data into a synthetic page. It does not fetch individual result URLs. Do not mistake this path for product-page research; create/reuse a separate page-candidate fetch-and-evidence flow.
- `scout/tinyfish.py`: use only public Search/Fetch calls. Its current search returns at most five results per call; fetch accepts one URL per call; the client currently enforces a conservative local cap of 100 attempts/day separately for search and fetch. Reconcile orchestration bounds with verified provider quotas rather than bypassing the client blindly.
- `scout/gemini.py`: current `ExtractionRouter` sends only the `extract` stage to Gemini and sends planning/repair to Nemotron. Product mode needs an explicit constrained research loop rather than routing the existing generic planner/repair stages implicitly.
- `scout/contracts.py`: add validated plan, candidate, field evidence, per-domain coverage, and usage structures (or equivalent) so model output cannot erase provenance.
- `scout/web_assets/`: expose a distinct product-research status/progress, domain/page coverage, hard-constraint failures/unknowns, and per-result evidence. Avoid promising exhaustive search.
- `tests/`: tests are gitignored in this repository and not present in a fresh clone. The implementation needs to add/maintain tests locally and separately ensure the test suite is available to contributors/CI; do not weaken existing tests.

## Acceptance criteria

1. An ordinary one-page request still follows the existing single-page route and does not enter the multi-domain loop.
2. An open-web comparison request produces no more than 15 distinct relevant domains, stops early when discovery is exhausted, and does not pad the result.
3. A named-site request never includes off-domain candidates; a seed-URL request starts from the supplied validated URL and follows only the approved domain scope.
4. An optional country selection guides market/domain discovery; no country is inferred from IP or device signals.
5. All included product results meet every evidenced hard requirement and budget. Failed or unknown hard constraints are excluded or explicitly marked non-qualifying, never scored as passing.
6. Per domain, results are ranked by the declared overall fit and capped at 10; fewer results are returned when fewer qualify. Evidence shows why a more expensive option ranks higher than a cheaper qualifying one.
7. Page content is fetched for candidates; search snippets alone are not presented as confirmed product specifications/prices.
8. Each candidate preserves product variant, price/currency, source URL, checked time, and evidence by field. Conflicting/missing/stale values are visible.
9. Exactly two Nemotron calls are made on successful product-research runs: distillation and final presentation. Intermediate retries/repairs use Gemini or stop; the call ledger verifies provider and stage counts.
10. Every provider/tool request, page, candidate, token budget, and wall-clock limit is bounded and reported. Quota exhaustion returns useful partial findings when possible without paid fallback.
11. Existing SSRF, robots, credential, and untrusted-content protections apply to all discovered/follow-up URLs.
12. UI progress/history identifies relevant domains and pages attempted, successful fetches, excluded candidates with reason, and incomplete coverage without exposing API keys.
13. Offline tests prove the above with mocked provider/network boundaries, including malformed model plans, duplicates, pagination exhaustion, blocked URLs, robots denial, price/config variant confusion, unknown constraints, quota exhaustion, Unicode, and partial results. A controlled live smoke test is separate and clearly marked as quota-consuming.

## Out of scope

- Guaranteeing the absolute cheapest/best product across every page or retailer.
- Purchases, cart interactions, login, personal accounts, review sentiment manipulation, or private data extraction.
- TinyFish paid Agent/Research mode, paid OpenRouter models, or automatic paid fallback.
- Unbounded crawling, arbitrary whole-site scraping, infinite scrolling, or following unrelated links.
- Treating the output cap as the crawl cap, or returning irrelevant results merely to reach a minimum.

## Implementation choices delegated to the builder

- Choose a bounded Gemini iteration/token and wall-clock safety limit that allows the 200-call TinyFish budget to be useful. On reaching a Gemini/provider limit, preserve supported partial findings and explain the stop reason; do not make hidden Nemotron retries.
- Confirm the actual TinyFish Search and Fetch account quotas before changing the local daily guards. Keep the product's 200 combined per-run limit independent of those daily account safeguards.
- Use the optional country value to guide market search; do not use geolocation or infer a country. Explicit country field overrides country parsed from the request.

The user approved the behavior and the 200 combined TinyFish-attempts-per-run cap. Implementation may proceed against this design.
