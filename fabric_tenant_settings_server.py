#!/usr/bin/env python3
"""Local read-only Fabric tenant-settings governance API and dashboard."""

from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import shutil
import subprocess
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from generate_fabric_tenant_settings_report import as_list, build_rows

FABRIC_RESOURCE = "https://api.fabric.microsoft.com"
POWER_BI_RESOURCE = "https://analysis.windows.net/powerbi/api"
POWER_BI_CAPACITIES_URL = "https://api.powerbi.com/v1.0/myorg/admin/capacities"
SKILL_HEADER = "fabriciq-ontology-cli"


class GovernanceStore:
    def __init__(self, output_directory: Path) -> None:
        self.output_directory = output_directory
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {
            "status": "not_loaded",
            "lastRefreshedUtc": None,
            "error": None,
            "tenantId": None,
            "accountName": None,
            "tenantSettings": [],
            "capacityOverrideGroups": [],
            "capacities": [],
            "overrides": [],
        }

    @staticmethod
    def access_token(resource: str, tenant_id: str) -> tuple[str, dict[str, Any]]:
        azure_cli = shutil.which("az") or shutil.which("az.cmd")
        if not azure_cli:
            raise RuntimeError(
                "Azure CLI was not found. Install it and restart the terminal."
            )
        result = subprocess.run(
            [
                azure_cli,
                "account",
                "get-access-token",
                "--resource",
                resource,
                "--tenant",
                tenant_id,
                "--query",
                "accessToken",
                "--output",
                "tsv",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Azure CLI authentication failed: {message}")
        token = result.stdout.strip()
        if not token:
            raise RuntimeError("Azure CLI returned an empty access token.")
        try:
            payload = token.split(".")[1]
            payload += "=" * (-len(payload) % 4)
            claims = json.loads(base64.urlsafe_b64decode(payload))
        except (IndexError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("Azure CLI returned an invalid access token.") from error
        if claims.get("tid", "").lower() != tenant_id.lower():
            raise RuntimeError(
                f"Token tenant {claims.get('tid')} does not match requested tenant {tenant_id}."
            )
        return token, claims

    @staticmethod
    def validate_tenant_id(tenant_id: str) -> str:
        try:
            return str(uuid.UUID(tenant_id.strip()))
        except ValueError as error:
            raise RuntimeError("Tenant ID must be a valid Microsoft Entra tenant GUID.") from error

    @staticmethod
    def interactive_login(tenant_id: str) -> None:
        azure_cli = shutil.which("az") or shutil.which("az.cmd")
        if not azure_cli:
            raise RuntimeError(
                "Azure CLI was not found. Install it and restart the terminal."
            )
        result = subprocess.run(
            [
                azure_cli,
                "login",
                "--tenant",
                tenant_id,
                "--allow-no-subscriptions",
                "--output",
                "none",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip()
            raise RuntimeError(f"Azure CLI sign-in failed: {message}")

    @staticmethod
    def request_json(url: str, token: str, fabric_request: bool = False) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if fabric_request:
            headers["x-ms-fabric-skill"] = SKILL_HEADER
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GET {url} failed with HTTP {error.code}: {body}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"GET {url} failed: {error.reason}") from error

    def paged_fabric_data(self, url: str, token: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        while url:
            response = self.request_json(url, token, fabric_request=True)
            records.extend(as_list(response.get("value")))
            url = str(response.get("continuationUri") or "")
        return records

    def refresh(self, tenant_id: str | None = None) -> dict[str, Any]:
        with self.lock:
            self.data["status"] = "refreshing"
            self.data["error"] = None
            try:
                requested_tenant = self.validate_tenant_id(
                    tenant_id or str(self.data.get("tenantId") or "")
                )
                fabric_token, fabric_claims = self.access_token(
                    FABRIC_RESOURCE, requested_tenant
                )
                power_bi_token, _ = self.access_token(
                    POWER_BI_RESOURCE, requested_tenant
                )
                tenant_settings = self.paged_fabric_data(
                    f"{FABRIC_RESOURCE}/v1/admin/tenantsettings", fabric_token
                )
                override_groups = self.paged_fabric_data(
                    f"{FABRIC_RESOURCE}/v1/admin/capacities/delegatedTenantSettingOverrides",
                    fabric_token,
                )
                capacities_response = self.request_json(
                    POWER_BI_CAPACITIES_URL, power_bi_token
                )
                capacities = as_list(capacities_response.get("value"))
                overrides = build_rows(tenant_settings, override_groups, capacities)
                refreshed = datetime.now(timezone.utc).isoformat()

                self.output_directory.mkdir(parents=True, exist_ok=True)
                snapshots = {
                    "tenant-settings.json": tenant_settings,
                    "capacity-overrides.json": override_groups,
                    "admin-capacities.json": capacities_response,
                    "normalized-overrides.json": overrides,
                }
                for name, value in snapshots.items():
                    (self.output_directory / name).write_text(
                        json.dumps(value, indent=2), encoding="utf-8"
                    )

                self.data = {
                    "status": "ready",
                    "lastRefreshedUtc": refreshed,
                    "error": None,
                    "tenantId": requested_tenant,
                    "accountName": (
                        fabric_claims.get("preferred_username")
                        or fabric_claims.get("upn")
                        or fabric_claims.get("appid")
                        or "Unknown identity"
                    ),
                    "tenantSettings": tenant_settings,
                    "capacityOverrideGroups": override_groups,
                    "capacities": capacities,
                    "overrides": overrides,
                }
            except Exception as error:
                self.data["status"] = "error"
                self.data["error"] = str(error)
            return self.summary()

    def connect(self, tenant_id: str, interactive: bool) -> dict[str, Any]:
        requested_tenant = self.validate_tenant_id(tenant_id)
        if interactive:
            self.interactive_login(requested_tenant)
        return self.refresh(requested_tenant)

    def summary(self) -> dict[str, Any]:
        rows = self.data["overrides"]
        return {
            "status": self.data["status"],
            "lastRefreshedUtc": self.data["lastRefreshedUtc"],
            "error": self.data["error"],
            "tenantId": self.data["tenantId"],
            "accountName": self.data["accountName"],
            "tenantSettings": len(self.data["tenantSettings"]),
            "capacities": len(self.data["capacities"]),
            "capacitiesWithOverrides": len({row["capacity_id"] for row in rows}),
            "overrideRecords": len(rows),
            "enabledStateDrift": sum(row["enabled_state_differs"] for row in rows),
            "securityScopeDrift": sum(row["security_scope_differs"] for row in rows),
            "propertyDrift": sum(row["properties_differ"] for row in rows),
        }


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fabric Tenant Settings Governance</title>
  <script>
    (() => {
      const param = new URLSearchParams(window.location.search).get("clawpilotTheme");
      const theme =
        param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      document.documentElement.setAttribute("data-theme", theme);
    })();
  </script>
  <style>
    :root {
      color-scheme: light;
      --cp-bg: #f7f4ef;
      --cp-bg-elevated: #fcfbf8;
      --cp-surface: #ffffff;
      --cp-surface-soft: #f5f5f5;
      --cp-border: #dedede;
      --cp-border-strong: #919191;
      --cp-text: #242424;
      --cp-text-muted: #5c5c5c;
      --cp-text-soft: #6f6f6f;
      --cp-accent: #b11f4b;
      --cp-accent-hover: #9a1a41;
      --cp-accent-soft: rgba(177, 31, 75, 0.08);
      --cp-accent-fg: #ffffff;
      --cp-success: #16a34a;
      --cp-danger: #dc2626;
      --cp-warning: #f59e0b;
      --cp-link: #0078d4;
      --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.12);
      --cp-overlay: rgba(255, 255, 255, 0.8);
      --cp-panel: rgba(255, 255, 255, 0.86);
      --cp-panel-strong: rgba(255, 255, 255, 0.96);
      --cp-sheen: rgba(255, 255, 255, 0.55);
      --cp-highlight: rgba(177, 31, 75, 0.12);
    }
    html[data-theme="dark"] {
      color-scheme: dark;
      --cp-bg: #3d3b3a;
      --cp-bg-elevated: #343231;
      --cp-surface: #292929;
      --cp-surface-soft: #2e2e2e;
      --cp-border: #474747;
      --cp-border-strong: #5f5f5f;
      --cp-text: #dedede;
      --cp-text-muted: #919191;
      --cp-text-soft: #b0b0b0;
      --cp-accent: #fd8ea1;
      --cp-accent-hover: #fb7b91;
      --cp-accent-soft: rgba(253, 142, 161, 0.14);
      --cp-accent-fg: #1a1a1a;
      --cp-success: #4ade80;
      --cp-danger: #f87171;
      --cp-warning: #fbbf24;
      --cp-link: #4da6ff;
      --cp-shadow: 0 18px 48px rgba(0, 0, 0, 0.32);
      --cp-overlay: rgba(41, 41, 41, 0.88);
      --cp-panel: rgba(41, 41, 41, 0.72);
      --cp-panel-strong: rgba(41, 41, 41, 0.96);
      --cp-sheen: rgba(255, 255, 255, 0.04);
      --cp-highlight: rgba(253, 142, 161, 0.12);
    }
    * { box-sizing: border-box; }
    body { margin: 0; background: var(--cp-bg); color: var(--cp-text);
      font: 14px/1.45 "Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif; }
    main { max-width: 1500px; margin: auto; padding: 32px; }
    header { display: flex; justify-content: space-between; align-items: end; gap: 16px; margin-bottom: 24px; }
    h1 { margin: 0; font-size: 30px; } h2 { margin: 30px 0 12px; }
    .muted { color: var(--cp-text-muted); } .error { color: var(--cp-danger); }
    .cards { display: grid; grid-template-columns: repeat(auto-fit,minmax(180px,1fr)); gap: 12px; }
    .card { background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 16px;
      padding: 16px; box-shadow: var(--cp-shadow); }
    .number { font-size: 28px; font-weight: 700; color: var(--cp-accent); }
    button,.button { border: 1px solid var(--cp-accent); border-radius: 0.625rem; padding: 9px 14px;
      background: var(--cp-accent); color: var(--cp-accent-fg); font-weight: 600; cursor: pointer;
      text-decoration: none; display: inline-block; }
    button:hover,.button:hover { background: var(--cp-accent-hover); }
    button:disabled { opacity: .6; cursor: wait; }
    .secondary { background: var(--cp-surface); color: var(--cp-accent); }
    .actions { display: flex; flex-wrap: wrap; gap: 8px; }
    .connection { display: grid; grid-template-columns: minmax(280px,1fr) auto auto; gap: 8px;
      background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 16px;
      padding: 16px; margin-bottom: 20px; box-shadow: var(--cp-shadow); }
    label { grid-column: 1 / -1; font-weight: 600; }
    input { width: 100%; border: 1px solid var(--cp-border-strong); border-radius: 0.625rem;
      padding: 9px 11px; background: var(--cp-bg-elevated); color: var(--cp-text);
      font: inherit; }
    .session { grid-column: 1 / -1; color: var(--cp-text-muted); }
    .finding { display: none; margin: 16px 0; padding: 14px 16px; border: 1px solid var(--cp-accent);
      border-left: 5px solid var(--cp-danger); border-radius: 0.625rem;
      background: var(--cp-accent-soft); }
    .filters { display: flex; gap: 8px; margin: 12px 0; }
    .filters button[aria-pressed="false"] { background: var(--cp-surface); color: var(--cp-accent); }
    .panel { background: var(--cp-surface); border: 1px solid var(--cp-border); border-radius: 16px;
      overflow: auto; box-shadow: var(--cp-shadow); }
    table { width: 100%; border-collapse: collapse; min-width: 1100px; }
    th,td { padding: 10px; border-bottom: 1px solid var(--cp-border); text-align: left; vertical-align: top; }
    th { background: var(--cp-surface-soft); position: sticky; top: 0; }
    code { font-family: Consolas, "Courier New", Courier, monospace; font-size: 12px; }
    .badge { border-radius: 0.625rem; padding: 2px 7px; font-weight: 600; background: var(--cp-surface-soft); }
    .different { color: var(--cp-danger); background: var(--cp-accent-soft); }
    .same { color: var(--cp-success); }
    tr.row-drift td { background: var(--cp-accent-soft); }
    @media (max-width: 700px) { main { padding: 20px; } header { align-items: start; flex-direction: column; }
      .connection { grid-template-columns: 1fr; } }
  </style>
</head>
<body><main>
  <header>
    <div><h1>Fabric Tenant Settings Governance</h1>
      <div id="status" class="muted">Loading current configuration...</div></div>
    <div class="actions">
      <button id="refresh">Refresh from Microsoft APIs</button>
      <a class="button secondary" href="/api/export.csv">Export CSV</a>
      <a class="button secondary" href="/api/export.json">Export JSON</a>
    </div>
  </header>
  <section class="connection">
    <label for="tenantId">Microsoft Entra tenant ID</label>
    <input id="tenantId" autocomplete="off" inputmode="text"
      placeholder="00000000-0000-0000-0000-000000000000">
    <button id="loadTenant">Load using current sign-in</button>
    <button id="signInTenant" class="secondary">Sign in &amp; load</button>
    <div id="session" class="session">Enter a tenant ID to load its Fabric administration data.</div>
  </section>
  <section id="cards" class="cards"></section>
  <section id="finding" class="finding"></section>
  <h2>Capacity tenant-setting overrides</h2>
  <div class="filters">
    <button id="showAll" aria-pressed="true">All overrides</button>
    <button id="showDifferences" aria-pressed="false">Differences only</button>
  </div>
  <div class="panel"><table>
    <thead><tr><th>Result</th><th>Capacity</th><th>Setting</th><th>Group</th><th>Tenant</th><th>Capacity</th>
      <th>Enabled drift</th><th>Security scope</th><th>Properties</th><th>Region / SKU</th></tr></thead>
    <tbody id="rows"></tbody>
  </table></div>
</main>
<script>
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c =>
    ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
  const card = (number, label) => `<div class="card"><div class="number">${esc(number)}</div><div>${esc(label)}</div></div>`;
  const drift = value => `<span class="badge ${value ? "different" : "same"}">${value ? "Different" : "Same"}</span>`;
  let reportRows = [];
  let differencesOnly = false;
  const hasDrift = row => row.enabled_state_differs || row.security_scope_differs || row.properties_differ;
  function renderRows() {
    const rows = differencesOnly ? reportRows.filter(hasDrift) : reportRows;
    document.getElementById("rows").innerHTML = rows.length ? rows.map(row => `<tr class="${hasDrift(row) ? "row-drift" : ""}">
      <td><span class="badge ${hasDrift(row) ? "different" : "same"}">${esc(row.classification)}</span></td>
      <td>${esc(row.capacity_name)}<br><code>${esc(row.capacity_id)}</code></td>
      <td>${esc(row.setting_title)}<br><code>${esc(row.setting_name)}</code></td>
      <td>${esc(row.setting_group)}</td><td>${esc(row.tenant_enabled)}</td><td>${esc(row.capacity_enabled)}</td>
      <td>${drift(row.enabled_state_differs)}</td><td>${drift(row.security_scope_differs)}</td>
      <td>${drift(row.properties_differ)}</td><td>${esc(row.region)} / ${esc(row.sku)}<br>${esc(row.state)}</td>
    </tr>`).join("") : `<tr><td colspan="10">No ${differencesOnly ? "effective differences" : "capacity overrides"} returned.</td></tr>`;
  }
  async function load() {
    const [summaryResponse, rowsResponse] = await Promise.all([fetch("/api/summary"), fetch("/api/overrides")]);
    const summary = await summaryResponse.json();
    reportRows = await rowsResponse.json();
    const status = document.getElementById("status");
    status.className = summary.error ? "error" : "muted";
    status.textContent = summary.error || `Last refreshed: ${summary.lastRefreshedUtc || "not yet"}`;
    if (summary.tenantId) document.getElementById("tenantId").value = summary.tenantId;
    document.getElementById("session").textContent = summary.tenantId
      ? `Tenant: ${summary.tenantId} · Signed in as: ${summary.accountName || "Unknown identity"}`
      : "Enter a tenant ID to load its Fabric administration data.";
    document.getElementById("cards").innerHTML = [
      card(summary.tenantSettings, "Tenant settings"),
      card(summary.capacities, "Capacities"),
      card(summary.capacitiesWithOverrides, "Capacities with overrides"),
      card(summary.overrideRecords, "Explicit overrides"),
      card(summary.enabledStateDrift, "Enabled-state drift"),
      card(summary.securityScopeDrift + summary.propertyDrift, "Scope/property drift")
    ].join("");
    const changed = reportRows.filter(hasDrift);
    const finding = document.getElementById("finding");
    if (changed.length) {
      finding.style.display = "block";
      finding.innerHTML = `<strong>${changed.length} effective difference${changed.length === 1 ? "" : "s"} found.</strong> ` +
        changed.map(row => `${esc(row.setting_title)} on ${esc(row.capacity_name)}: ` +
          `tenant <strong>${esc(row.tenant_enabled)}</strong>, capacity <strong>${esc(row.capacity_enabled)}</strong>`).join("; ");
    } else {
      finding.style.display = "none";
      finding.textContent = "";
    }
    document.getElementById("showAll").textContent = `All overrides (${reportRows.length})`;
    document.getElementById("showDifferences").textContent = `Differences only (${changed.length})`;
    renderRows();
  }
  document.getElementById("showAll").addEventListener("click", () => {
    differencesOnly = false;
    document.getElementById("showAll").setAttribute("aria-pressed", "true");
    document.getElementById("showDifferences").setAttribute("aria-pressed", "false");
    renderRows();
  });
  document.getElementById("showDifferences").addEventListener("click", () => {
    differencesOnly = true;
    document.getElementById("showAll").setAttribute("aria-pressed", "false");
    document.getElementById("showDifferences").setAttribute("aria-pressed", "true");
    renderRows();
  });
  async function connect(interactive) {
    const tenantId = document.getElementById("tenantId").value.trim();
    const buttons = [document.getElementById("loadTenant"), document.getElementById("signInTenant")];
    buttons.forEach(button => button.disabled = true);
    document.getElementById("status").textContent = interactive
      ? "Waiting for Microsoft sign-in..."
      : "Loading tenant data...";
    try {
      const response = await fetch("/api/connect", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({tenantId, interactive})
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "Tenant connection failed.");
      await load();
    } catch (error) {
      document.getElementById("status").className = "error";
      document.getElementById("status").textContent = error.message;
    } finally {
      buttons.forEach(button => button.disabled = false);
    }
  }
  document.getElementById("loadTenant").addEventListener("click", () => connect(false));
  document.getElementById("signInTenant").addEventListener("click", () => connect(true));
  document.getElementById("refresh").addEventListener("click", async event => {
    event.currentTarget.disabled = true;
    event.currentTarget.textContent = "Refreshing...";
    try { await fetch("/api/refresh", {method:"POST"}); await load(); }
    finally { event.currentTarget.disabled = false; event.currentTarget.textContent = "Refresh from Microsoft APIs"; }
  });
  load().catch(error => { document.getElementById("status").textContent = error; });
</script>
</body></html>"""


class GovernanceHandler(BaseHTTPRequestHandler):
    store: GovernanceStore

    def log_message(self, format_string: str, *args: Any) -> None:
        print(f"{self.address_string()} - {format_string % args}")

    def send_bytes(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, value: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_bytes(
            json.dumps(value, indent=2).encode("utf-8"),
            "application/json; charset=utf-8",
            status,
        )

    def do_GET(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            self.send_bytes(DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/health":
            self.send_json({"status": "ok", "dataStatus": self.store.data["status"]})
        elif path == "/api/summary":
            self.send_json(self.store.summary())
        elif path == "/api/capacities":
            self.send_json(self.store.data["capacities"])
        elif path == "/api/overrides":
            self.send_json(self.store.data["overrides"])
        elif path == "/api/settings":
            self.send_json(self.store.data["tenantSettings"])
        elif path == "/api/export.json":
            self.send_json(
                {"summary": self.store.summary(), "overrides": self.store.data["overrides"]}
            )
        elif path == "/api/export.csv":
            rows = self.store.data["overrides"]
            output = io.StringIO()
            if rows:
                writer = csv.DictWriter(output, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
            self.send_bytes(
                output.getvalue().encode("utf-8-sig"),
                "text/csv; charset=utf-8",
            )
        else:
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/connect":
                length = int(self.headers.get("Content-Length", "0"))
                if length > 4096:
                    self.send_json(
                        {"error": "Request body is too large."},
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                    )
                    return
                body = json.loads(self.rfile.read(length) or b"{}")
                summary = self.store.connect(
                    str(body.get("tenantId") or ""),
                    bool(body.get("interactive")),
                )
                status = (
                    HTTPStatus.OK
                    if summary["status"] == "ready"
                    else HTTPStatus.BAD_GATEWAY
                )
                self.send_json(summary, status)
            elif path == "/api/refresh":
                summary = self.store.refresh()
                status = (
                    HTTPStatus.OK
                    if summary["status"] == "ready"
                    else HTTPStatus.BAD_GATEWAY
                )
                self.send_json(summary, status)
            else:
                self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--output-directory", type=Path, default=Path(__file__).parent / "output")
    parser.add_argument("--tenant-id")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("For safety, this application only binds to localhost.")

    store = GovernanceStore(args.output_directory)
    GovernanceHandler.store = store
    if args.tenant_id:
        store.refresh(args.tenant_id)
    server = ThreadingHTTPServer((args.host, args.port), GovernanceHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Fabric governance report running at {url}")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local server.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
