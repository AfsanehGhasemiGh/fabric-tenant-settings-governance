#!/usr/bin/env python3
"""Generate an HTML and CSV report from Fabric tenant-setting API snapshots."""

from __future__ import annotations

import argparse
import csv
import html
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with path.open(encoding="utf-8-sig") as handle:
        return json.load(handle)


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def canonical(value: Any) -> str:
    return json.dumps(value if value is not None else [], sort_keys=True, separators=(",", ":"))


def status_text(value: Any) -> str:
    if value is True:
        return "Enabled"
    if value is False:
        return "Disabled"
    return "Not specified"


def group_names(setting: dict[str, Any], key: str) -> str:
    groups = as_list(setting.get(key))
    return "; ".join(
        str(group.get("name") or group.get("graphId") or "")
        for group in groups
        if isinstance(group, dict)
    )


def property_text(setting: dict[str, Any]) -> str:
    return "; ".join(
        f"{item.get('name')}={item.get('value')}"
        for item in as_list(setting.get("properties"))
        if isinstance(item, dict)
    )


def build_rows(
    tenant_settings: list[dict[str, Any]],
    capacity_overrides: list[dict[str, Any]],
    capacities: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tenant_by_name = {item["settingName"]: item for item in tenant_settings}
    capacity_by_id = {item["id"]: item for item in capacities}
    rows: list[dict[str, Any]] = []

    for override_group in capacity_overrides:
        capacity_id = override_group["id"]
        capacity = capacity_by_id.get(capacity_id, {})
        for capacity_setting in as_list(override_group.get("tenantSettings")):
            setting_name = capacity_setting["settingName"]
            tenant_setting = tenant_by_name.get(setting_name, {})
            enabled_differs = tenant_setting.get("enabled") != capacity_setting.get("enabled")
            security_scope_differs = any(
                canonical(tenant_setting.get(key)) != canonical(capacity_setting.get(key))
                for key in ("enabledSecurityGroups", "excludedSecurityGroups")
            )
            properties_differ = canonical(tenant_setting.get("properties")) != canonical(
                capacity_setting.get("properties")
            )
            has_effective_drift = (
                enabled_differs or security_scope_differs or properties_differ
            )
            rows.append(
                {
                    "capacity_name": capacity.get("displayName", "Unknown capacity"),
                    "capacity_id": capacity_id,
                    "sku": capacity.get("sku", ""),
                    "state": capacity.get("state", ""),
                    "region": capacity.get("region", ""),
                    "setting_group": capacity_setting.get("tenantSettingGroup", ""),
                    "setting_title": capacity_setting.get("title", ""),
                    "setting_name": setting_name,
                    "tenant_enabled": status_text(tenant_setting.get("enabled")),
                    "capacity_enabled": status_text(capacity_setting.get("enabled")),
                    "classification": (
                        "Effective drift"
                        if has_effective_drift
                        else "Explicit override matches tenant"
                    ),
                    "enabled_state_differs": enabled_differs,
                    "tenant_enabled_groups": group_names(tenant_setting, "enabledSecurityGroups"),
                    "capacity_enabled_groups": group_names(
                        capacity_setting, "enabledSecurityGroups"
                    ),
                    "tenant_excluded_groups": group_names(tenant_setting, "excludedSecurityGroups"),
                    "capacity_excluded_groups": group_names(
                        capacity_setting, "excludedSecurityGroups"
                    ),
                    "security_scope_differs": security_scope_differs,
                    "tenant_properties": property_text(tenant_setting),
                    "capacity_properties": property_text(capacity_setting),
                    "properties_differ": properties_differ,
                    "delegate_to_workspace": status_text(
                        capacity_setting.get("delegateToWorkspace")
                    ),
                    "delegated_from": capacity_setting.get("delegatedFrom", ""),
                }
            )
    return sorted(rows, key=lambda row: (row["capacity_name"], row["setting_group"], row["setting_title"]))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)


def badge(value: bool, true_text: str, false_text: str) -> str:
    css_class = "danger" if value else "ok"
    text = true_text if value else false_text
    return f'<span class="badge {css_class}">{html.escape(text)}</span>'


def write_html(
    path: Path,
    rows: list[dict[str, Any]],
    tenant_settings: list[dict[str, Any]],
    capacities: list[dict[str, Any]],
    focus_capacity_id: str | None,
) -> None:
    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    capacities_with_overrides = {row["capacity_id"] for row in rows}
    drift_rows = [
        row
        for row in rows
        if row["enabled_state_differs"]
        or row["security_scope_differs"]
        or row["properties_differ"]
    ]
    same_state_rows = [row for row in rows if not row["enabled_state_differs"]]
    focus_capacity = next(
        (item for item in capacities if item.get("id") == focus_capacity_id), None
    )
    focus_override_count = sum(row["capacity_id"] == focus_capacity_id for row in rows)
    counts = Counter(row["capacity_id"] for row in rows)

    capacity_table = "".join(
        f"""
        <tr>
          <td>{html.escape(str(item.get("displayName", "")))}</td>
          <td><code>{html.escape(str(item.get("id", "")))}</code></td>
          <td>{html.escape(str(item.get("sku", "")))}</td>
          <td>{html.escape(str(item.get("state", "")))}</td>
          <td>{html.escape(str(item.get("region", "")))}</td>
          <td>{counts.get(item.get("id"), 0)}</td>
        </tr>"""
        for item in sorted(capacities, key=lambda item: str(item.get("displayName", "")))
    )

    detail_table = "".join(
        f"""
        <tr class="{"row-drift" if row["classification"] == "Effective drift" else ""}">
          <td>{html.escape(str(row["classification"]))}</td>
          <td>{html.escape(str(row["capacity_name"]))}<br><code>{html.escape(str(row["capacity_id"]))}</code></td>
          <td>{html.escape(str(row["setting_group"]))}</td>
          <td>{html.escape(str(row["setting_title"]))}<br><code>{html.escape(str(row["setting_name"]))}</code></td>
          <td>{html.escape(str(row["tenant_enabled"]))}</td>
          <td>{html.escape(str(row["capacity_enabled"]))}</td>
          <td>{badge(bool(row["enabled_state_differs"]), "Different", "Same")}</td>
          <td>{badge(bool(row["security_scope_differs"]), "Different", "Same")}</td>
          <td>{badge(bool(row["properties_differ"]), "Different", "Same")}</td>
          <td>{html.escape(str(row["delegate_to_workspace"]))}</td>
          <td>{html.escape(str(row["delegated_from"]))}</td>
        </tr>"""
        for row in rows
    )

    focus_html = ""
    if focus_capacity:
        focus_html = f"""
        <section class="callout">
          <h2>Current demo workspace capacity</h2>
          <p><strong>{html.escape(str(focus_capacity.get("displayName", "")))}</strong>
          ({html.escape(str(focus_capacity.get("sku", "")))},
          {html.escape(str(focus_capacity.get("region", "")))}) has
          <strong>{focus_override_count}</strong> capacity-level override records.</p>
        </section>"""

    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Fabric capacity tenant-setting override report</title>
  <script>
    (() => {{
      const param = new URLSearchParams(window.location.search).get("clawpilotTheme");
      const theme =
        param || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
      document.documentElement.setAttribute("data-theme", theme);
    }})();
  </script>
  <style>
    :root {{
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
    }}
    html[data-theme="dark"] {{
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
    }}
    body {{ font:14px/1.45 "Segoe UI",Aptos,Calibri,-apple-system,BlinkMacSystemFont,sans-serif;
      color:var(--cp-text); margin:0; background:var(--cp-bg); }}
    main {{ max-width:1500px; margin:auto; padding:32px; }}
    h1 {{ margin-bottom:4px; }} h2 {{ margin-top:30px; }}
    .muted {{ color:var(--cp-text-muted); }}
    .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; margin:24px 0; }}
    .card,.callout {{ border:1px solid var(--cp-border); border-radius:16px; padding:18px;
      background:var(--cp-surface); box-shadow:var(--cp-shadow); }}
    .number {{ font-size:30px; font-weight:700; color:var(--cp-accent); }}
    .callout {{ border-left:5px solid var(--cp-accent); }}
    .warning {{ border-left-color:var(--cp-warning); }}
    table {{ width:100%; border-collapse:collapse; margin:12px 0 26px; }}
    th,td {{ border:1px solid var(--cp-border); padding:9px; text-align:left; vertical-align:top; }}
    th {{ background:var(--cp-surface-soft); position:sticky; top:0; }}
    code {{ font-family:Consolas,"Courier New",Courier,monospace; font-size:12px; overflow-wrap:anywhere; }}
    .badge {{ display:inline-block; border-radius:0.625rem; padding:2px 8px; font-weight:600; }}
    .danger {{ color:var(--cp-danger); background:var(--cp-accent-soft); }}
    .ok {{ color:var(--cp-success); background:var(--cp-surface-soft); }}
    tr.row-drift td {{ background:var(--cp-accent-soft); }}
  </style>
</head>
<body><main>
  <h1>Fabric capacity tenant-setting override report</h1>
  <p class="muted">Read-only configuration snapshot generated {generated_at}</p>
  <div class="cards">
    <div class="card"><div class="number">{len(tenant_settings)}</div><div>Tenant settings inspected</div></div>
    <div class="card"><div class="number">{len(capacities)}</div><div>Tenant capacities inventoried</div></div>
    <div class="card"><div class="number">{len(capacities_with_overrides)}</div><div>Capacities with explicit overrides</div></div>
    <div class="card"><div class="number">{len(rows)}</div><div>Explicit override records</div></div>
    <div class="card"><div class="number">{len(drift_rows)}</div><div>Overrides with effective drift</div></div>
    <div class="card"><div class="number">{len(same_state_rows)}</div><div>Overrides matching tenant enabled state</div></div>
  </div>
  <section class="callout">
    <h2>Conclusion</h2>
    <p><strong>Yes.</strong> The API reports capacity-level overrides. An override record means
    the capacity is no longer merely inheriting that setting. Only records marked
    <em>Different</em> currently produce observable value or scope drift from the tenant snapshot.</p>
  </section>
  {focus_html}
  <h2>Capacity inventory</h2>
  <table>
    <thead><tr><th>Capacity</th><th>ID</th><th>SKU</th><th>State</th><th>Region</th><th>Override records</th></tr></thead>
    <tbody>{capacity_table}</tbody>
  </table>
  <h2>Override details</h2>
  <table>
    <thead><tr><th>Result</th><th>Capacity</th><th>Group</th><th>Setting</th><th>Tenant</th><th>Capacity</th>
      <th>Enabled drift</th><th>Security scope</th><th>Properties</th>
      <th>Workspace delegation</th><th>Delegated from</th></tr></thead>
    <tbody>{detail_table}</tbody>
  </table>
  <section class="callout warning">
    <h2>Interpretation and audit limitation</h2>
    <ul>
      <li>This report used read-only APIs and did not call either tenant or capacity update endpoints.</li>
      <li>The override inventory proves that a capacity-level override exists; it does not identify the person who changed it or the change time.</li>
      <li>Use Microsoft Purview audit events such as <code>UpdateCapacityTenantSettingDelegation</code>
      for actor and timestamp attribution.</li>
      <li>A same-state override can become future drift if the tenant default changes while the capacity retains its explicit value.</li>
    </ul>
  </section>
</main></body></html>"""
    path.write_text(document, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot_directory", type=Path)
    parser.add_argument("--focus-capacity-id")
    args = parser.parse_args()

    directory = args.snapshot_directory
    tenant_settings = as_list(load_json(directory / "tenant-settings.json"))
    capacity_overrides = as_list(load_json(directory / "capacity-overrides.json"))
    capacities_response = load_json(directory / "admin-capacities.json")
    capacities = as_list(
        capacities_response.get("value")
        if isinstance(capacities_response, dict)
        else capacities_response
    )

    rows = build_rows(tenant_settings, capacity_overrides, capacities)
    write_csv(directory / "fabric-capacity-tenant-setting-overrides.csv", rows)
    write_html(
        directory / "fabric-capacity-tenant-setting-overrides.html",
        rows,
        tenant_settings,
        capacities,
        args.focus_capacity_id,
    )
    print(
        json.dumps(
            {
                "tenantSettings": len(tenant_settings),
                "capacities": len(capacities),
                "capacitiesWithOverrides": len({row["capacity_id"] for row in rows}),
                "overrideRecords": len(rows),
                "enabledStateDrift": sum(row["enabled_state_differs"] for row in rows),
                "securityScopeDrift": sum(row["security_scope_differs"] for row in rows),
                "propertyDrift": sum(row["properties_differ"] for row in rows),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
