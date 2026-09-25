"""Stand-in end-to-end check for the brain."""

import json
import sys
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8799/health", timeout=5) as r:
    data = json.loads(r.read())
print("selftest", data)
sys.exit(0 if data.get("status") == "ok" else 1)
