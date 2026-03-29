"""
Test report generator for virtual charger farm.
Collects metrics, validates sessions, generates JSON + branded HTML reports.
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp

from metrics import farm_metrics

log = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).parent / "reports"
REPORTS_DIR.mkdir(exist_ok=True)


class ReportGenerator:
    """Generates test reports after scenario runs."""

    def __init__(self, cpo_api_url: str = "", redis_host: str = ""):
        self.cpo_api_url = cpo_api_url
        self.redis_host = redis_host

    async def generate(
        self,
        scenario_name: str,
        scenario_result: dict,
        metrics_snapshot: dict,
    ) -> dict:
        """Generate a full report. Returns report dict and saves to disk."""

        report_id = f"{scenario_name}-{int(time.time())}"
        report = {
            "id": report_id,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "scenario": scenario_name,
            "scenario_result": scenario_result,

            "performance": self._performance_section(metrics_snapshot),
            "session_integrity": await self._session_integrity(),
            "vulnerability_assessment": await self._vulnerability_assessment(),
            "ocpp_compliance": self._ocpp_compliance(metrics_snapshot),
            "recommendations": [],
        }

        # Generate recommendations
        report["recommendations"] = self._generate_recommendations(report)

        # Save JSON
        json_path = REPORTS_DIR / f"{report_id}.json"
        with open(json_path, "w") as f:
            json.dump(report, f, indent=2, default=str)

        # Save HTML
        html_path = REPORTS_DIR / f"{report_id}.html"
        with open(html_path, "w") as f:
            f.write(self._render_html(report))

        log.info("Report generated: %s", report_id)
        return report

    def _performance_section(self, metrics: dict) -> dict:
        latencies = [s.latency_ms for s in farm_metrics._latencies]
        latencies.sort()

        def percentile(data, p):
            if not data:
                return 0.0
            k = (len(data) - 1) * p / 100.0
            f = int(k)
            c = f + 1
            if c >= len(data):
                return data[f]
            return data[f] + (k - f) * (data[c] - data[f])

        return {
            "messages_per_sec": metrics.get("messages_per_sec", 0),
            "total_messages_sent": metrics.get("total_messages_sent", 0),
            "total_messages_received": metrics.get("total_messages_received", 0),
            "avg_latency_ms": metrics.get("avg_latency_ms", 0),
            "p50_latency_ms": round(percentile(latencies, 50), 1),
            "p95_latency_ms": round(percentile(latencies, 95), 1),
            "p99_latency_ms": round(percentile(latencies, 99), 1),
            "total_connections": metrics.get("total_connections", 0),
            "total_disconnections": metrics.get("total_disconnections", 0),
            "total_errors": metrics.get("total_errors", 0),
            "uptime_sec": metrics.get("uptime_sec", 0),
        }

    async def _session_integrity(self) -> dict:
        """Check session integrity via CPO API if configured."""
        result = {
            "sessions_started": farm_metrics.total_sessions_started,
            "sessions_ended": farm_metrics.total_sessions_ended,
            "orphaned_sessions": max(0, farm_metrics.total_sessions_started - farm_metrics.total_sessions_ended),
            "cpo_validation": None,
            "stale_redis_keys": None,
        }

        # CPO API validation
        if self.cpo_api_url:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(f"{self.cpo_api_url}/api/sessions", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            result["cpo_validation"] = {
                                "reachable": True,
                                "active_sessions": len(data) if isinstance(data, list) else data.get("count", 0),
                            }
            except Exception as e:
                result["cpo_validation"] = {"reachable": False, "error": str(e)}

        # Redis stale key check
        if self.redis_host:
            try:
                import redis
                r = redis.Redis(host=self.redis_host, port=6379, decode_responses=True)
                keys = r.keys("ocpp:*")
                result["stale_redis_keys"] = {
                    "total_keys": len(keys),
                    "keys": keys[:50],  # First 50
                }
            except Exception as e:
                result["stale_redis_keys"] = {"error": str(e)}

        return result

    async def _vulnerability_assessment(self) -> dict:
        """Assess potential vulnerabilities found during testing."""
        issues = []

        orphaned = farm_metrics.total_sessions_started - farm_metrics.total_sessions_ended
        if orphaned > 0:
            issues.append({
                "severity": "high",
                "category": "session_integrity",
                "title": "Orphaned Sessions",
                "description": f"{orphaned} sessions were started but never properly ended. This could indicate race conditions in transaction handling.",
            })

        if farm_metrics.total_errors > farm_metrics.total_messages_sent * 0.1:
            issues.append({
                "severity": "high",
                "category": "reliability",
                "title": "High Error Rate",
                "description": f"Error rate exceeds 10% ({farm_metrics.total_errors} errors / {farm_metrics.total_messages_sent} messages).",
            })

        if farm_metrics.total_disconnections > farm_metrics.total_connections * 0.5:
            issues.append({
                "severity": "medium",
                "category": "connectivity",
                "title": "High Disconnect Rate",
                "description": f"Disconnect rate exceeds 50% of connections ({farm_metrics.total_disconnections}/{farm_metrics.total_connections}).",
            })

        return {"issues": issues, "total_issues": len(issues)}

    def _ocpp_compliance(self, metrics: dict) -> dict:
        return {
            "total_sent": metrics.get("total_messages_sent", 0),
            "total_received": metrics.get("total_messages_received", 0),
            "acceptance_rate": round(
                metrics.get("total_messages_received", 0) / max(1, metrics.get("total_messages_sent", 1)) * 100, 1
            ),
        }

    def _generate_recommendations(self, report: dict) -> list:
        recs = []

        perf = report["performance"]
        if perf["p99_latency_ms"] > 5000:
            recs.append({"severity": "critical", "message": f"P99 latency is {perf['p99_latency_ms']}ms — server may be overloaded"})
        elif perf["p95_latency_ms"] > 2000:
            recs.append({"severity": "high", "message": f"P95 latency is {perf['p95_latency_ms']}ms — investigate server performance"})

        integrity = report["session_integrity"]
        if integrity["orphaned_sessions"] > 0:
            recs.append({"severity": "critical", "message": f"{integrity['orphaned_sessions']} orphaned sessions — check StopTransaction handling"})

        vuln = report["vulnerability_assessment"]
        for issue in vuln["issues"]:
            recs.append({"severity": issue["severity"], "message": issue["title"] + ": " + issue["description"]})

        compliance = report["ocpp_compliance"]
        if compliance["acceptance_rate"] < 90:
            recs.append({"severity": "high", "message": f"OCPP acceptance rate only {compliance['acceptance_rate']}%"})

        recs.sort(key=lambda r: {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(r["severity"], 4))
        return recs

    def _render_html(self, report: dict) -> str:
        perf = report["performance"]
        integrity = report["session_integrity"]
        recs = report["recommendations"]

        recs_html = ""
        for r in recs:
            color = {"critical": "#ff4444", "high": "#ff8800", "medium": "#ffcc00", "low": "#22c55e"}.get(r["severity"], "#888")
            recs_html += f'<div class="rec" style="border-left:4px solid {color}"><b>[{r["severity"].upper()}]</b> {r["message"]}</div>'

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Test Report — {report['scenario']}</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#1a1a2e;color:#e0e0e0;padding:24px}}
.header{{background:linear-gradient(135deg,#1a1a2e,#1a3a5c);padding:32px;border-radius:12px;margin-bottom:24px;border:1px solid #22c55e}}
.header h1{{color:#22c55e;font-size:28px;margin-bottom:8px}}
.header .meta{{color:#999;font-size:14px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:24px}}
.card{{background:#132d4a;border-radius:8px;padding:20px;border:1px solid #1e4060}}
.card h3{{color:#00B0E4;margin-bottom:12px;font-size:16px}}
.stat{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #1e4060}}
.stat:last-child{{border:none}}
.stat .label{{color:#999}}
.stat .value{{color:#fff;font-weight:600}}
.rec{{background:#132d4a;padding:12px 16px;margin-bottom:8px;border-radius:6px}}
h2{{color:#22c55e;margin:24px 0 12px;font-size:20px}}
.footer{{text-align:center;color:#666;padding:24px;font-size:12px}}
</style>
</head>
<body>
<div class="header">
<h1>Virtual Charger Farm — Test Report</h1>
<div class="meta">Scenario: <b>{report['scenario']}</b> | Generated: {report['generated_at']} | ID: {report['id']}</div>
</div>

<div class="grid">
<div class="card"><h3>Performance</h3>
<div class="stat"><span class="label">Messages/sec</span><span class="value">{perf['messages_per_sec']}</span></div>
<div class="stat"><span class="label">Avg Latency</span><span class="value">{perf['avg_latency_ms']:.1f}ms</span></div>
<div class="stat"><span class="label">P50 Latency</span><span class="value">{perf['p50_latency_ms']}ms</span></div>
<div class="stat"><span class="label">P95 Latency</span><span class="value">{perf['p95_latency_ms']}ms</span></div>
<div class="stat"><span class="label">P99 Latency</span><span class="value">{perf['p99_latency_ms']}ms</span></div>
<div class="stat"><span class="label">Total Sent</span><span class="value">{perf['total_messages_sent']}</span></div>
<div class="stat"><span class="label">Total Received</span><span class="value">{perf['total_messages_received']}</span></div>
</div>

<div class="card"><h3>Connections</h3>
<div class="stat"><span class="label">Connections</span><span class="value">{perf['total_connections']}</span></div>
<div class="stat"><span class="label">Disconnections</span><span class="value">{perf['total_disconnections']}</span></div>
<div class="stat"><span class="label">Errors</span><span class="value">{perf['total_errors']}</span></div>
<div class="stat"><span class="label">Uptime</span><span class="value">{perf['uptime_sec']:.0f}s</span></div>
</div>

<div class="card"><h3>Session Integrity</h3>
<div class="stat"><span class="label">Started</span><span class="value">{integrity['sessions_started']}</span></div>
<div class="stat"><span class="label">Ended</span><span class="value">{integrity['sessions_ended']}</span></div>
<div class="stat"><span class="label">Orphaned</span><span class="value" style="color:{'#ff4444' if integrity['orphaned_sessions']>0 else '#22c55e'}">{integrity['orphaned_sessions']}</span></div>
</div>

<div class="card"><h3>OCPP Compliance</h3>
<div class="stat"><span class="label">Acceptance Rate</span><span class="value">{report['ocpp_compliance']['acceptance_rate']}%</span></div>
</div>
</div>

<h2>Recommendations ({len(recs)})</h2>
{recs_html if recs_html else '<div class="rec" style="border-left:4px solid #22c55e"><b>All clear!</b> No issues found.</div>'}

<div class="footer">OCPP Virtual Charger Farm — Stress Test Report</div>
</body>
</html>"""


def list_reports() -> list[dict]:
    """List all generated reports."""
    reports = []
    for f in sorted(REPORTS_DIR.glob("*.json"), reverse=True):
        try:
            with open(f) as fh:
                data = json.load(fh)
                reports.append({
                    "id": data.get("id", f.stem),
                    "scenario": data.get("scenario", "unknown"),
                    "generated_at": data.get("generated_at", ""),
                })
        except Exception:
            pass
    return reports


def get_report(report_id: str, fmt: str = "json") -> Optional[str]:
    """Get a report by ID. fmt: 'json' or 'html'."""
    ext = "html" if fmt == "html" else "json"
    path = REPORTS_DIR / f"{report_id}.{ext}"
    if path.exists():
        return path.read_text()
    return None
