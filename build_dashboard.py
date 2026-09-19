#!/usr/bin/env python3
"""Splice payload.json into dashboard.src.html -> dashboard.html."""
import json, os

here = os.path.dirname(os.path.abspath(__file__))
src = open(os.path.join(here, "dashboard.src.html"), encoding="utf-8").read()
payload = open(os.path.join(here, "data", "payload.json"), encoding="utf-8").read()

# </script> inside a JSON island would close the block early; escape defensively
payload = payload.replace("</", "<\\/")
assert "__PAYLOAD__" in src, "placeholder missing"
out = src.replace("__PAYLOAD__", payload)

dest = os.path.join(here, "dashboard.html")
with open(dest, "w", encoding="utf-8") as fh:
    fh.write(out)
print(f"dashboard.html {os.path.getsize(dest)/1024:.0f} KB")
