#!/usr/bin/env python3
"""Scrape public data from openrouter.ai into JSON + CSV.

Four datasets:
  models     /api/v1/models                        catalog, pricing, context, modalities
  providers  /api/v1/providers                     provider metadata
  endpoints  /api/v1/models/{id}/endpoints         per-provider pricing & limits for each model
  rankings   /rankings (embedded RSC payload)      daily token/request usage leaderboard

Stdlib only. Usage:
    python3 scrape_openrouter.py                   # models + providers + rankings
    python3 scrape_openrouter.py --endpoints       # ...plus one request per model (slow)
    python3 scrape_openrouter.py --only models
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://openrouter.ai"
UA = "openrouter-scraper/1.0 (+public data collection; contact: kwonttol@gmail.com)"
OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


# ---------------------------------------------------------------- http

def fetch(path: str, tries: int = 4, timeout: int = 60) -> bytes:
    """GET with gzip, retries and exponential backoff. Honors Retry-After on 429."""
    url = path if path.startswith("http") else BASE + path
    delay = 1.5
    for attempt in range(1, tries + 1):
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", "Accept": "*/*"}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw
        except urllib.error.HTTPError as e:
            # 4xx other than rate limiting will not fix itself
            if e.code == 429:
                wait = float(e.headers.get("Retry-After") or delay)
            elif 400 <= e.code < 500:
                raise
            else:
                wait = delay
            if attempt == tries:
                raise
            time.sleep(wait)
            delay *= 2
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == tries:
                raise
            time.sleep(delay)
            delay *= 2
    raise RuntimeError("unreachable")


def fetch_json(path: str):
    return json.loads(fetch(path).decode("utf-8"))


# ---------------------------------------------------------------- helpers

def per_million(value) -> str:
    """OpenRouter prices are USD per token; humans read USD per 1M tokens.

    A negative price is a sentinel (openrouter/auto uses -1) meaning the real cost
    depends on the model the request is routed to. Emit blank, not a bogus number.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return ""
    if f < 0:
        return ""
    return f"{f * 1_000_000:.6f}".rstrip("0").rstrip(".")


def is_variable(pricing: dict) -> bool:
    """True when the model has no fixed price of its own (dynamic routing)."""
    for key in ("prompt", "completion"):
        try:
            if float(pricing.get(key)) < 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def write_json(name: str, payload) -> str:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    return path


def write_csv(name: str, rows, fields) -> str:
    path = os.path.join(OUT_DIR, name)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return path


def report(label: str, n: int, *paths) -> None:
    print(f"  {label}: {n} rows -> " + ", ".join(os.path.basename(p) for p in paths))


# ---------------------------------------------------------------- models

MODEL_FIELDS = [
    "id", "canonical_slug", "name", "author", "created_utc", "context_length",
    "max_completion_tokens", "is_moderated", "modality", "input_modalities",
    "output_modalities", "tokenizer", "prompt_usd_per_mtok", "completion_usd_per_mtok",
    "request_usd", "image_usd", "web_search_usd", "cache_read_usd_per_mtok",
    "cache_write_usd_per_mtok", "is_free", "is_variable_pricing",
    "supports_tools", "supports_reasoning", "reasoning_mandatory",
    "reasoning_default_enabled", "reasoning_default_effort",
    "reasoning_supported_efforts", "reasoning_supports_max_tokens",
    "supports_structured_output", "hugging_face_id", "knowledge_cutoff", "description",
]


def flatten_model(m: dict) -> dict:
    pricing = m.get("pricing") or {}
    arch = m.get("architecture") or {}
    top = m.get("top_provider") or {}
    params = m.get("supported_parameters") or []
    reasoning = m.get("reasoning") or {}
    created = m.get("created")
    prompt = pricing.get("prompt")
    completion = pricing.get("completion")
    return {
        "id": m.get("id"),
        "canonical_slug": m.get("canonical_slug"),
        "name": m.get("name"),
        "author": (m.get("id") or "/").split("/")[0],
        "created_utc": datetime.fromtimestamp(created, timezone.utc).isoformat()
        if isinstance(created, (int, float)) else "",
        "context_length": m.get("context_length"),
        "max_completion_tokens": top.get("max_completion_tokens"),
        "is_moderated": top.get("is_moderated"),
        "modality": arch.get("modality"),
        "input_modalities": "|".join(arch.get("input_modalities") or []),
        "output_modalities": "|".join(arch.get("output_modalities") or []),
        "tokenizer": arch.get("tokenizer"),
        "prompt_usd_per_mtok": per_million(prompt),
        "completion_usd_per_mtok": per_million(completion),
        "request_usd": pricing.get("request", ""),
        "image_usd": pricing.get("image", ""),
        "web_search_usd": pricing.get("web_search", ""),
        "cache_read_usd_per_mtok": per_million(pricing.get("input_cache_read")),
        "cache_write_usd_per_mtok": per_million(pricing.get("input_cache_write")),
        "is_free": _is_zero(prompt) and _is_zero(completion),
        "is_variable_pricing": is_variable(pricing),
        "supports_tools": "tools" in params,
        "supports_reasoning": "reasoning" in params,
        # the reasoning object is absent on non-reasoning models and partially
        # populated on the rest, so every sub-field is independently optional
        "reasoning_mandatory": reasoning.get("mandatory", ""),
        "reasoning_default_enabled": reasoning.get("default_enabled", ""),
        "reasoning_default_effort": reasoning.get("default_effort", ""),
        "reasoning_supported_efforts": "|".join(reasoning.get("supported_efforts") or []),
        "reasoning_supports_max_tokens": reasoning.get("supports_max_tokens", ""),
        "supports_structured_output": "structured_outputs" in params
        or "response_format" in params,
        "hugging_face_id": m.get("hugging_face_id") or "",
        "knowledge_cutoff": m.get("knowledge_cutoff") or "",
        "description": " ".join((m.get("description") or "").split()),
    }


def _is_zero(v) -> bool:
    try:
        return float(v) == 0.0
    except (TypeError, ValueError):
        return False


def scrape_models() -> list:
    models = fetch_json("/api/v1/models")["data"]
    rows = sorted((flatten_model(m) for m in models), key=lambda r: r["id"] or "")
    report("models", len(rows),
           write_json("models.json", models), write_csv("models.csv", rows, MODEL_FIELDS))
    return models


# ---------------------------------------------------------------- providers

PROVIDER_FIELDS = ["name", "slug", "headquarters", "datacenters",
                   "privacy_policy_url", "terms_of_service_url", "status_page_url"]


def scrape_providers() -> list:
    provs = fetch_json("/api/v1/providers")["data"]
    rows = [
        {**p, "datacenters": "|".join(p.get("datacenters") or [])}
        for p in sorted(provs, key=lambda p: (p.get("name") or "").lower())
    ]
    report("providers", len(rows),
           write_json("providers.json", provs),
           write_csv("providers.csv", rows, PROVIDER_FIELDS))
    return provs


# ---------------------------------------------------------------- endpoints

ENDPOINT_FIELDS = [
    "model_id", "model_name", "provider_name", "endpoint_name", "tag", "quantization",
    "context_length", "max_completion_tokens", "max_prompt_tokens", "status",
    "uptime_last_5m", "uptime_last_30m", "uptime_last_1d",
    "latency_last_30m_ms", "throughput_last_30m_tps",
    "prompt_usd_per_mtok", "completion_usd_per_mtok", "cache_read_usd_per_mtok",
    "cache_write_usd_per_mtok", "discount", "has_tiered_pricing",
    "supports_tools", "supports_tool_choice", "supports_implicit_caching",
    "supported_parameters",
]


def _endpoints_for(model_id: str):
    try:
        return model_id, fetch_json(f"/api/v1/models/{model_id}/endpoints")["data"]
    except Exception as e:  # a model may be delisted between the two calls
        print(f"    ! {model_id}: {type(e).__name__} {e}", file=sys.stderr)
        return model_id, None


def scrape_endpoints(models: list, workers: int = 4) -> list:
    ids = [m["id"] for m in models if m.get("id")]
    print(f"  endpoints: querying {len(ids)} models with {workers} workers...")
    raw, rows = {}, []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for model_id, data in pool.map(_endpoints_for, ids):
            done += 1
            if done % 50 == 0:
                print(f"    {done}/{len(ids)}")
            if not data:
                continue
            raw[model_id] = data
            for ep in data.get("endpoints") or []:
                pricing = ep.get("pricing") or {}
                params = ep.get("supported_parameters") or []
                rows.append({
                    "model_id": data.get("id"),
                    "model_name": data.get("name"),
                    "provider_name": ep.get("provider_name"),
                    "endpoint_name": ep.get("name"),
                    "tag": ep.get("tag"),
                    "quantization": ep.get("quantization"),
                    "context_length": ep.get("context_length"),
                    "max_completion_tokens": ep.get("max_completion_tokens"),
                    "max_prompt_tokens": ep.get("max_prompt_tokens"),
                    "status": ep.get("status"),
                    "uptime_last_5m": ep.get("uptime_last_5m"),
                    "uptime_last_30m": ep.get("uptime_last_30m"),
                    "uptime_last_1d": ep.get("uptime_last_1d"),
                    "latency_last_30m_ms": ep.get("latency_last_30m"),
                    "throughput_last_30m_tps": ep.get("throughput_last_30m"),
                    "prompt_usd_per_mtok": per_million(pricing.get("prompt")),
                    "completion_usd_per_mtok": per_million(pricing.get("completion")),
                    "cache_read_usd_per_mtok": per_million(pricing.get("input_cache_read")),
                    "cache_write_usd_per_mtok": per_million(pricing.get("input_cache_write")),
                    "discount": pricing.get("discount", ""),
                    "has_tiered_pricing": bool(pricing.get("overrides")),
                    "supports_tools": "tools" in params,
                    "supports_tool_choice": ep.get("supports_tool_choice"),
                    "supports_implicit_caching": ep.get("supports_implicit_caching"),
                    "supported_parameters": "|".join(params),
                })
    rows.sort(key=lambda r: (r["model_id"] or "", r["provider_name"] or ""))
    report("endpoints", len(rows),
           write_json("endpoints.json", raw),
           write_csv("endpoints.csv", rows, ENDPOINT_FIELDS))
    return rows


# ---------------------------------------------------------------- rankings

RANKING_FIELDS = ["rank", "date", "model_permaslug", "author", "variant", "change",
                  "total_tokens",
                  "total_prompt_tokens", "total_completion_tokens",
                  "total_native_tokens_reasoning", "total_native_tokens_cached",
                  "requests", "total_tool_calls"]


def _flight_stream(html: str) -> str:
    """Rebuild the Next.js RSC payload from the __next_f.push() string literals."""
    chunks = []
    for m in re.finditer(r'self\.__next_f\.push\(\[\s*\d+\s*,\s*("(?:[^"\\]|\\.)*")', html):
        try:
            chunks.append(json.loads(m.group(1)))
        except json.JSONDecodeError:
            continue
    return "".join(chunks)


def _json_objects(text: str, start_key: str):
    """Yield every JSON object in text that begins with start_key, via brace matching."""
    for m in re.finditer(r'\{"' + re.escape(start_key) + r'"', text):
        i, depth, in_str, esc = m.start(), 0, False, False
        for j in range(i, len(text)):
            c = text[j]
            if esc:
                esc = False
                continue
            if c == "\\":
                esc = True
                continue
            if c == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        yield json.loads(text[i:j + 1])
                    except json.JSONDecodeError:
                        pass
                    break


def scrape_rankings() -> list:
    html = fetch("/rankings").decode("utf-8", errors="replace")
    flight = _flight_stream(html)
    if not flight:
        print("    ! no RSC payload found - the page structure changed", file=sys.stderr)
        return []

    recs = [r for r in _json_objects(flight, "date") if "model_permaslug" in r]
    # de-duplicate: the payload can repeat a (date, model, variant) record
    seen, uniq = set(), []
    for r in recs:
        key = (r.get("date"), r.get("model_permaslug"), r.get("variant"))
        if key not in seen:
            seen.add(key)
            uniq.append(r)

    rows = []
    for r in uniq:
        prompt = r.get("total_prompt_tokens") or 0
        completion = r.get("total_completion_tokens") or 0
        rows.append({
            "date": (r.get("date") or "")[:10],
            "model_permaslug": r.get("model_permaslug"),
            "author": (r.get("model_permaslug") or "/").split("/")[0],
            "variant": r.get("variant"),
            # served as a fraction (+4.84 = +484%); the comparison window is not
            # documented by OpenRouter, so pass it through unscaled
            "change": r.get("change"),
            "total_tokens": prompt + completion,
            "total_prompt_tokens": prompt,
            "total_completion_tokens": completion,
            "total_native_tokens_reasoning": r.get("total_native_tokens_reasoning"),
            "total_native_tokens_cached": r.get("total_native_tokens_cached"),
            # the field is "count" in the payload; it counts requests
            "requests": r.get("count", r.get("total_requests")),
            "total_tool_calls": r.get("total_tool_calls"),
        })
    rows.sort(key=lambda r: (r["date"], -r["total_tokens"]))
    # rank within each date
    per_date = {}
    for r in rows:
        per_date[r["date"]] = per_date.get(r["date"], 0) + 1
        r["rank"] = per_date[r["date"]]

    report("rankings", len(rows),
           write_json("rankings.json", uniq),
           write_csv("rankings.csv", rows, RANKING_FIELDS))
    return rows


# ---------------------------------------------------------------- main

def main() -> int:
    global OUT_DIR
    ap = argparse.ArgumentParser(description="Scrape public openrouter.ai data.")
    ap.add_argument("--only", choices=["models", "providers", "endpoints", "rankings"],
                    nargs="+", help="scrape just these datasets")
    ap.add_argument("--endpoints", action="store_true",
                    help="also scrape per-model endpoints (one request per model)")
    ap.add_argument("--workers", type=int, default=4,
                    help="parallel requests for --endpoints (default 4)")
    ap.add_argument("--out", default=OUT_DIR, help="output directory")
    args = ap.parse_args()

    OUT_DIR = args.out
    os.makedirs(OUT_DIR, exist_ok=True)

    wanted = set(args.only) if args.only else {"models", "providers", "rankings"}
    if args.endpoints:
        wanted |= {"models", "endpoints"}

    print(f"scraping {BASE} -> {OUT_DIR}")
    started = time.time()
    models = []
    if "models" in wanted:
        models = scrape_models()
    if "providers" in wanted:
        scrape_providers()
    if "endpoints" in wanted:
        if not models:
            models = fetch_json("/api/v1/models")["data"]
        scrape_endpoints(models, workers=args.workers)
    if "rankings" in wanted:
        scrape_rankings()

    write_json("_meta.json", {
        "scraped_at": datetime.now(timezone.utc).isoformat(),
        "source": BASE,
        "datasets": sorted(wanted),
    })
    print(f"done in {time.time() - started:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
