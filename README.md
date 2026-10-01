# Fabric Tenant Settings Governance

A local, read-only governance dashboard for comparing Microsoft Fabric tenant settings with capacity-level overrides.

## Start

1. Install Python 3 and Azure CLI.
2. Double-click `Start-Fabric-Governance.cmd`, or run:

   ```powershell
   .\Start-Fabric-Governance.ps1
   ```

3. Open `http://127.0.0.1:8000`.
4. Enter the Microsoft Entra tenant GUID.
5. Select **Sign in & load**.

See `START-HERE.html` for API permissions, direct API tests, response examples, and troubleshooting.

## Security

- The server binds only to `127.0.0.1`.
- Microsoft authentication is handled by Azure CLI.
- Passwords, client secrets, and access tokens are not stored.
- The reporting workflow calls read-only Fabric and Power BI administration APIs.
- Generated API snapshots are written to the ignored `output/` directory.

## Requirements

- Fabric administrator role
- Azure CLI
- Python 3
- Delegated `Tenant.Read.All` or `Tenant.ReadWrite.All` permission
