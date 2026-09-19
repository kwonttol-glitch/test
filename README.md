# openrouter-scraper

Pulls public data from [openrouter.ai](https://openrouter.ai) into JSON + CSV. Stdlib
Python only — no `pip install`, works on the system Python 3.9.

```bash
python3 scrape_openrouter.py                    # models + providers + rankings (~1s)
python3 scrape_openrouter.py --endpoints        # ...plus per-provider pricing (~30-70s)
python3 scrape_openrouter.py --only rankings    # just one dataset
python3 scrape_openrouter.py --out /tmp/or      # different output dir
```

Output lands in `data/`: a `*.json` (raw API response, nothing dropped) and a flattened
`*.csv` (spreadsheet-friendly) per dataset, plus `_meta.json` with the scrape timestamp.

## Datasets

| Dataset | Source | Rows | Notes |
|---|---|---|---|
| `models` | `GET /api/v1/models` | 447 | Catalog: pricing, context, modalities, capabilities |
| `providers` | `GET /api/v1/providers` | 109 | Provider metadata, HQ, policy URLs |
| `endpoints` | `GET /api/v1/models/{id}/endpoints` | 1314 | Per-provider price/context/uptime for each model |
| `rankings` | `GET /rankings` (HTML) | 20 | Daily token + request leaderboard |

Three of the four are documented JSON APIs and need no key. Rankings is the exception —
see below.

## Notes on the data

**Prices are converted.** OpenRouter quotes USD *per token*; the CSVs use
`*_usd_per_mtok` (USD per million tokens), which is how pricing pages read. Raw
per-token values are preserved in the JSON.

**Five models have no fixed price.** The dynamic routers (`openrouter/auto`,
`auto-beta`, `bodybuilder`, `fusion`, `pareto-code`) report `-1` per token as a sentinel —
cost depends on where the request lands. Those rows get `is_variable_pricing=True` and
blank price columns rather than a nonsense `-$1000000`. Filter them out of price rankings.

**Tiered pricing is flagged, not expanded.** 167 endpoints charge more above a prompt-token
threshold (e.g. Claude Sonnet doubles past 200K). The CSV sets `has_tiered_pricing=True`
and the CSV price columns are the *base* tier; the full `pricing.overrides` array is in
`endpoints.json`.

**`latency_last_30m_ms` and `throughput_last_30m_tps` are always empty.** The API returns
null for these on every endpoint — they are served to the web UI from somewhere else. The
columns are kept so they fill in if that changes. `uptime_last_*` *is* populated (blank for
endpoints too new or idle to have a window).

**Reasoning columns are sparsely populated, by design.** `reasoning_*` comes from the
model's `reasoning` object, which is absent on non-reasoning models and only partly filled
on the rest: `mandatory` on 315/447, `default_effort` and `supported_efforts` on 176,
`supports_max_tokens` on just 10. Blank means "not declared", not "false".

**`change` in rankings is passed through unscaled.** It is served as a fraction — `+4.84`
is +484%, `-0.32` is -32% — but OpenRouter does not document the comparison window, so the
script does not convert it to a percentage or guess a period.

**Rankings is HTML scraping and is the fragile part.** There is no JSON endpoint for it
(`/api/frontend/*` variants all 404), so the script rebuilds the Next.js RSC flight stream
from the `self.__next_f.push()` literals in the page and brace-matches the usage records
out of it. Consequences:

- It yields the **top 20 models for the most recent day only** — what the page renders on
  first paint. It is a daily snapshot, not history; run it on a schedule to build a series.
- A Next.js upgrade or a page redesign can break it. It fails loudly (warns and returns 0
  rows) rather than silently emitting nothing, so a cron job will show it.
- `requests` comes from the payload's `count` field.

## The dashboard

`dashboard.html` is a self-contained page — all 447 models embedded, no server, no
build step to view it. Regenerate it after a scrape:

```bash
python3 scrape_openrouter.py --endpoints
python3 build_payload.py        # data/*.csv  -> data/payload.json  (~286 KB)
python3 build_dashboard.py      # dashboard.src.html + payload -> dashboard.html
```

Edit `dashboard.src.html`, never `dashboard.html` — the latter is generated, and its
`__PAYLOAD__` placeholder is where the JSON lands.

### The "Re-scrape live" button

The page can refresh itself from the browser, because all three JSON APIs send
`access-control-allow-origin: *`. It re-fetches the catalogue and providers (about a
second), re-renders, then walks the per-model endpoints five at a time with a progress
meter and a Stop button. The result is offered back as a `payload.json` download you can
drop into `data/` to make it permanent.

**Where it works:** a local copy — `open dashboard.html`, or any `localhost` server.

**Where it does not:** the published artifact. That page runs under a CSP that blocks
fetch to every host except a few script CDNs, so the button reports the block and points
at the CLI instead. No artifact capability grants outbound HTTP; the ones on offer cover
storage, comments, connectors and sampling. If you need the published page to show fresh
numbers, re-run the scraper and republish — that path is three commands, above.

**What the button cannot refresh at all:** the leaderboard. `/rankings` is an HTML page
with no CORS header and its data buried in an RSC payload, so a browser cannot read it
from another origin. The embedded snapshot stays put, and the page says so; only
`scrape_openrouter.py` updates it.

## Politeness

`robots.txt` allows everything except `/seo/`. The script sends a descriptive User-Agent,
retries with exponential backoff, honors `Retry-After` on HTTP 429, and defaults to 4
concurrent workers for the endpoints pass (`--workers` to change). Nothing here needs an
API key or touches private account data.

## Useful queries

```bash
# cheapest models that support tool calling, by prompt price
python3 -c "
import csv
rows=[r for r in csv.DictReader(open('data/models.csv'))
      if r['supports_tools']=='True' and r['is_free']=='False'
      and r['prompt_usd_per_mtok']]
for r in sorted(rows,key=lambda r: float(r['prompt_usd_per_mtok']))[:10]:
    print(f\"{r['id']:55} \${r['prompt_usd_per_mtok']}/\${r['completion_usd_per_mtok']}\")"

# price spread across providers for one model
python3 -c "
import csv
for r in csv.DictReader(open('data/endpoints.csv')):
    if r['model_id']=='anthropic/claude-sonnet-4.5':
        print(f\"{r['provider_name']:30} \${r['prompt_usd_per_mtok']:>8} ctx={r['context_length']}\")"
```
