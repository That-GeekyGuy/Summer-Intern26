"""
Query API  –  fetches raw Prometheus text from METRICS_SOURCE,
parses it, and exposes filterable JSON endpoints.

Endpoints
---------
GET /metrics/names
    List every metric family (name, type, help).

GET /query?metric=<name>[&label=value…][&suffix=bucket|sum|count]
    Return all series for a family, optionally narrowed by label
    key=value pairs (AND logic, case-sensitive values, metric name
    is case-insensitive).

    Examples:
      /query?metric=port_bytes_count
      /query?metric=port_bytes_count&dir=rx
      /query?metric=port_bytes_count&dir=rx&iface=N3
      /query?metric=pfcp_messages_duration_seconds&suffix=bucket&le=0.001

GET /metrics   (proxy)
    Forwards the raw Prometheus text from METRICS_SOURCE so the
    dashboard can point at this service instead of the exporter
    directly (adds CORS header).

Configuration (environment variables)
--------------------------------------
METRICS_SOURCE   URL of the /metrics endpoint to pull from
                 default: http://localhost:8080/metrics
PORT             Port this service listens on
                 default: 8090
"""

import json
import os
import re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen
from urllib.error import URLError

METRICS_SOURCE = os.environ.get("METRICS_SOURCE", "http://localhost:8080/metrics")
PORT           = int(os.environ.get("PORT", "8090"))

# ── Prometheus text-format parser ─────────────────────────────────────────────

_LABEL_RE = re.compile(r'([a-zA-Z_]\w*)="([^"\\]*)"')
_LINE_RE  = re.compile(
    r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+([^\s]+)(?:\s+\d+)?$'
)
_SUFFIXES = frozenset({"bucket", "sum", "count", "created"})


def _fetch_raw():
    try:
        with urlopen(METRICS_SOURCE, timeout=5) as resp:
            return resp.read().decode("utf-8")
    except URLError as exc:
        raise RuntimeError(f"cannot reach metrics source {METRICS_SOURCE!r}: {exc}") from exc


def _parse(text):
    helps, types, families = {}, {}, {}

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue

        if line.startswith("# HELP "):
            parts = line[7:].split(" ", 1)
            helps[parts[0]] = parts[1] if len(parts) > 1 else ""
            continue

        if line.startswith("# TYPE "):
            parts = line[7:].split()
            if len(parts) == 2:
                types[parts[0]] = parts[1]
            continue

        if line.startswith("#"):
            continue

        m = _LINE_RE.match(line)
        if not m:
            continue

        full_name, lbls_str, value = m.group(1), m.group(2) or "", m.group(3)

        # Resolve base family name
        # (histogram _bucket/_sum/_count all fold back to their parent name)
        base = full_name
        for known in types:
            if full_name == known:
                base = known
                break
            if full_name.startswith(known + "_"):
                if full_name[len(known) + 1 :] in _SUFFIXES:
                    base = known
                    break

        if base not in families:
            families[base] = {
                "name":   base,
                "help":   helps.get(base, ""),
                "type":   types.get(base, "untyped"),
                "series": [],
            }

        labels = dict(_LABEL_RE.findall(lbls_str[1:-1] if lbls_str else ""))
        suffix = full_name[len(base) :].lstrip("_") or None

        families[base]["series"].append({
            "full_name": full_name,
            "suffix":    suffix,
            "labels":    labels,
            "value":     value,
        })

    return families


# ── Request handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)   # {"key": ["val"], ...}

        if parsed.path == "/metrics":
            self._proxy_metrics()

        elif parsed.path == "/query":
            self._handle_query(params)

        elif parsed.path == "/metrics/names":
            self._handle_names()

        else:
            self._json(404, {
                "error":     "not found",
                "endpoints": ["/metrics", "/metrics/names", "/query"],
            })

    # ── /metrics  ─────────────────────────────────────────────────────────────
    def _proxy_metrics(self):
        try:
            raw = _fetch_raw()
            self._respond(200, "text/plain; version=0.0.4; charset=utf-8", raw.encode())
        except RuntimeError as exc:
            self._json(502, {"error": str(exc)})

    # ── /metrics/names  ───────────────────────────────────────────────────────
    def _handle_names(self):
        try:
            families = _parse(_fetch_raw())
        except RuntimeError as exc:
            self._json(502, {"error": str(exc)})
            return

        names = sorted(
            [{"name": f["name"], "type": f["type"], "help": f["help"]}
             for f in families.values()],
            key=lambda x: x["name"],
        )
        self._json(200, {"count": len(names), "metrics": names})

    # ── /query  ───────────────────────────────────────────────────────────────
    def _handle_query(self, params):
        if "metric" not in params:
            self._json(400, {
                "error":   "required param 'metric' is missing",
                "example": "/query?metric=port_bytes_count&dir=rx",
            })
            return

        metric_name   = params["metric"][0].lower()          # case-insensitive lookup
        suffix_filter = params.get("suffix", [None])[0]      # optional sub-type filter
        label_filters = {                                     # everything else = label filter
            k: v[0] for k, v in params.items()
            if k not in ("metric", "suffix")
        }

        try:
            families = _parse(_fetch_raw())
        except RuntimeError as exc:
            self._json(502, {"error": str(exc)})
            return

        family = next(
            (f for name, f in families.items() if name.lower() == metric_name),
            None,
        )

        if family is None:
            self._json(404, {
                "error":     f"metric '{metric_name}' not found",
                "available": sorted(families.keys()),
            })
            return

        results = []
        for s in family["series"]:
            if suffix_filter and s["suffix"] != suffix_filter:
                continue
            if not all(s["labels"].get(k) == v for k, v in label_filters.items()):
                continue
            entry = {"labels": s["labels"], "value": s["value"]}
            if s["suffix"]:
                entry["suffix"] = s["suffix"]
            results.append(entry)

        active_filters = {
            **({"suffix": suffix_filter} if suffix_filter else {}),
            **label_filters,
        }
        self._json(200, {
            "metric":  family["name"],
            "type":    family["type"],
            "help":    family["help"],
            "filters": active_filters,
            "count":   len(results),
            "results": results,
        })

    # ── helpers  ──────────────────────────────────────────────────────────────

    def _json(self, code, data):
        self._respond(code, "application/json", json.dumps(data, indent=2).encode())

    def _respond(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    print(f"Query API  :  port {PORT}")
    print(f"Metrics source  :  {METRICS_SOURCE}")
    HTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
