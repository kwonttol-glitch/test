#!/usr/bin/env python3
"""Compact the scraped data into one JSON payload for the dashboard page."""
import csv, json, os

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def rows(name):
    with open(os.path.join(D, name), encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def num(v):
    if v in ("", None):
        return None
    try:
        f = float(v)
        return int(f) if f == int(f) else round(f, 6)
    except ValueError:
        return None


models = rows("models.csv")
endpoints = rows("endpoints.csv")
ranks = rows("rankings.csv")
meta = json.load(open(os.path.join(D, "_meta.json")))

# --- join rankings (dated permaslug) to the catalog (canonical_slug)
by_canon = {m["canonical_slug"]: m for m in models}
by_id = {m["id"]: m for m in models}
hits = sum(1 for r in ranks if r["model_permaslug"] in by_canon)
print(f"rankings joined to catalog: {hits}/{len(ranks)}")

# --- models: only the fields the page reads
M = []
for m in models:
    M.append({
        "id": m["id"],
        "name": m["name"],
        "author": m["author"],
        "ctx": num(m["context_length"]),
        "out": num(m["max_completion_tokens"]),
        "pin": num(m["prompt_usd_per_mtok"]),
        "pout": num(m["completion_usd_per_mtok"]),
        "mod": m["modality"],
        "tok": m["tokenizer"],
        "tools": m["supports_tools"] == "True",
        "reason": m["supports_reasoning"] == "True",
        "effort": m["reasoning_default_effort"],
        "efforts": m["reasoning_supported_efforts"],
        "free": m["is_free"] == "True",
        "var": m["is_variable_pricing"] == "True",
        "cache": num(m["cache_read_usd_per_mtok"]),
        "cutoff": m["knowledge_cutoff"],
        "canon": m["canonical_slug"],
    })
M.sort(key=lambda r: r["id"])

# --- endpoints grouped by model id
E = {}
for e in endpoints:
    E.setdefault(e["model_id"], []).append({
        "prov": e["provider_name"],
        "pin": num(e["prompt_usd_per_mtok"]),
        "pout": num(e["completion_usd_per_mtok"]),
        "ctx": num(e["context_length"]),
        "quant": e["quantization"],
        "up": num(e["uptime_last_1d"]),
        "tier": e["has_tiered_pricing"] == "True",
    })
for v in E.values():
    v.sort(key=lambda r: (r["pin"] is None, r["pin"]))

# --- rankings, carrying the catalog id where the join lands
R = []
for r in ranks:
    m = by_canon.get(r["model_permaslug"])
    R.append({
        "rank": num(r["rank"]),
        "slug": r["model_permaslug"],
        "author": r["author"],
        "id": m["id"] if m else None,
        "name": m["name"] if m else r["model_permaslug"].split("/")[-1],
        "tok": num(r["total_tokens"]),
        "pt": num(r["total_prompt_tokens"]),
        "ct": num(r["total_completion_tokens"]),
        "req": num(r["requests"]),
        "change": num(r["change"]),
        "calls": num(r["total_tool_calls"]),
    })
R.sort(key=lambda r: r["rank"])

payload = {
    "scraped_at": meta["scraped_at"],
    "models": M,
    "endpoints": E,
    "rankings": R,
    "counts": {"models": len(M), "providers": len(rows("providers.csv")),
               "endpoints": len(endpoints)},
}
out = os.path.join(D, "payload.json")
with open(out, "w", encoding="utf-8") as fh:
    json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
print(f"payload.json {os.path.getsize(out)/1024:.0f} KB  "
      f"({len(M)} models, {len(endpoints)} endpoints, {len(R)} rankings)")
