# Nemotron Scout (FilterCoffee)

A free-only research assistant that runs on your own computer. Ask in plain English
and get an answer with sources, tables, and evidence quotes instead of a pile of
search results.

It can compare products across retailer sites, extract facts from one page, search
several public sources at once, and read YouTube metadata or subtitles.

## What you can ask

| You want | Example |
|---|---|
| Compare products | "Find the best laptop with 16 GB DDR5 RAM, 512 GB SSD, Intel Core Ultra 7, under ₹1,00,000 in India." |
| Check one page | "Extract the release date from https://en.wikipedia.org/wiki/Minecraft" |
| Research a topic | "What are people saying about uv? Check Hacker News and GitHub." |
| Read a video | "Find a YouTube video about Python decorators and extract its title and channel." |
| Read a saved page | Import a `.txt`, `.md`, or `.html` file and ask about it (Reddit threads included). |

## Using the app

1. Double-click **`run_scout_web.bat`**. It opens `http://127.0.0.1:8765` in your
   browser. Keep the launcher window open while you use the app; `Ctrl+C` there
   stops it.
2. Choose **Where to look** in the sidebar:
   - **Auto** — one page or video for a normal question, or a product comparison
     for a shopping request.
   - **Choose sources** — tick Web, GitHub, Hacker News, Stack Exchange, Bluesky,
     and/or YouTube.
   - **Read a specific URL** — paste a public page address.
   - **Import saved page** — attach a file you saved yourself.
3. Optionally pick a **Country / market**. It is used for product searches and is
   never guessed from your computer.
4. Type your request and press **Enter** (or the round search button).
5. Read the result:
   - **Response** — the short answer and coverage notes.
   - **Table** — structured rows you can scan or export.
   - **Evidence** — quotes, source links, and captions behind each value.
6. Use **Copy Markdown**, **JSON**, or **CSV** to take the result with you.

### Follow-ups and sessions

- To refine a result, type the change in the same session: "okay, how about under
  1 lakh?" keeps the earlier specs and country.
- **New research** starts a clean session.
- The left sidebar lists recent sessions. Click one to reopen it, or click an
  earlier turn to review that answer. The trash icon deletes a session and all of
  its turns.
- Follow-ups keep unchanged requirements and update only what you explicitly change.

### Writing a good request

- **Products:** include the category, hard specs, budget with currency, and country.
  For example: "Compare 16 GB DDR5 laptops with a 512 GB SSD under 1 lakh rupees in India."
- **One site:** name it — "Check Amazon.in for..." or "Read this page: ...".
- **Follow-ups:** state only the change. "Same thing but 32 GB RAM."
- Vague requests still work, but specific hard requirements produce cleaner comparisons.

## How product research works

- It searches broadly first — at least 10 discovery searches — then visits up to
  15 retailer sites.
- Each site gets two targeted searches and up to 10 listing pages.
- A hard limit of **200 search/fetch requests per run** applies, including failures.
- Only facts quoted from an actual product page qualify. Missing or contradictory
  specs are excluded, with the observed values and reasons shown in the result.
- Prices and stock change. Coverage is bounded, not the whole web, and ranking is
  model-assisted — check the source link before buying.

## Optional tools

| Tool | What it adds | Setup |
|---|---|---|
| Chromium | Renders JavaScript-heavy pages | `uv run playwright install chromium` or `install_browser.bat` |
| CloakBrowser | Stealth Chromium renderer (opt-in) | `uv sync --extra cloak` |
| OpenCLI | Site-specific search via its Chrome extension | `npm install -g @jackwener/opencli` |

OpenCLI needs Node.js 20.18+. Its browserless Hacker News search works without the
extension; browser-backed sites need the OpenCLI Chrome extension connected. Use the
sidebar toggles; Scout never accepts arbitrary commands from a page.

## Limits and privacy

- **Free only.** No paid Nebius, Brave, TinyFish Agent/Browser, or paid OpenRouter models.
- Public pages only. No logins, paywalls, CAPTCHA solving, proxy rotation, or Reddit scraping.
- API keys stay in Python. They are never sent to the browser.
- Requests and page text are sent to OpenRouter (Nemotron), Google (Gemini), and
  TinyFish/Tavily as needed. Do not paste secrets into the request box.
- Sessions live in `data/workspace.db`; page and result cache lives in `data/cache.db`.
- The app listens only on `127.0.0.1:8765` with a per-launch token. Do not expose it
  through a tunnel.
- Only one run executes at a time. Closing the tab does not cancel it.
- TinyFish's free allowance is tracked locally (12,000 searches and 1,000 fetches per
  key per UTC day), separate from the 200-request per-run cap.

## For developers

```powershell
uv run pytest -q
uv run ruff check --no-respect-gitignore tests scout run_scout.py scripts
uv run python -m scout models
```

The web app is the main interface. The console runner is still available with
`uv run python run_scout.py --request "..."`. Design notes live in `docs/superpowers/`.
The original starter package under `src/nebius_nvidia_web_scraper/` is preserved;
the active package is `scout`.

## Setup (first time)

### 1. Install Python and uv

Use Python **3.12 or 3.13** (the project currently runs on 3.13) and install
[uv](https://docs.astral.sh/uv/). Then, from the project folder:

```powershell
uv sync
```

### 2. Get the free API keys

| Variable | Needed for | Get it from |
|---|---|---|
| `OPENROUTER_KEY` | Nemotron planning, selection, repair, screenshot vision | https://openrouter.ai/keys |
| `GEMINI_KEY` | Text extraction with Gemini 3.5 Flash-Lite | https://aistudio.google.com/apikey |
| `TINYFISH_API_KEY` | Web search and page fetching | https://tinyfish.ai |
| `TAVILY_API_KEY` | Web search fallback when TinyFish is absent | https://tavily.com |
| `GITHUB_TOKEN` | Higher GitHub API limits (optional) | https://github.com/settings/tokens |
| `STACKEXCHANGE_KEY` | Higher Stack Exchange limits (optional) | https://stackapps.com |
| `CLOAKBROWSER_LICENSE_KEY` | CloakBrowser license (optional; the free binary works without it) | — |

You need `OPENROUTER_KEY` and `GEMINI_KEY` for normal use, plus `TINYFISH_API_KEY`
(or `TAVILY_API_KEY` as a fallback) for general web search and page reading.

### 3. Create the `.env` file

Create a file named `.env` in the project root, next to `README.md`:

```dotenv
SCOUT_PROVIDER=openrouter

OPENROUTER_KEY=your-openrouter-key
GEMINI_KEY=your-gemini-key
TINYFISH_API_KEY=your-tinyfish-key

# Optional fallback when TinyFish is not configured:
# TAVILY_API_KEY=your-tavily-key

# Optional, for higher API limits:
# GITHUB_TOKEN=...
# STACKEXCHANGE_KEY=...
```

`SCOUT_PROVIDER=openrouter` keeps the app on the free OpenRouter Nemotron models
even if you later add a Nebius or NVIDIA key. Restart the app after changing `.env`.

Check what is configured (it only prints present/absent, never the keys):

```powershell
uv run python scripts/check_env.py
```

### 4. Start the app

Double-click **`run_scout_web.bat`**, or:

```powershell
uv run python -m scout.web
```

Open `http://127.0.0.1:8765`. If the port is busy, close the older launcher or use
another port:

```powershell
uv run python -m scout.web --port 8766
```

### 5. Optional browser setup

Only needed for JavaScript-heavy pages:

```powershell
uv run playwright install chromium
```

or double-click `install_browser.bat`. This downloads browser binaries (a few hundred
MB). CloakBrowser and OpenCLI are separate, optional installs described above.

### Troubleshooting

- **"Set GEMINI_KEY..."** — the key is missing from `.env`, or `.env` is not in the
  project root.
- **401 / 403 from a provider** — the key is wrong, expired, or lacks access.
- **Port already in use** — close the earlier launcher window or start on `--port 8766`.
- **Empty product result** — open the coverage and excluded listings in the result.
  The requirements may be too strict, the market wrong, or the sites blocked.
- **Free-model rate limit** — wait for the daily reset. The **OpenRouter quota** panel
  shows key status without making a model call.
