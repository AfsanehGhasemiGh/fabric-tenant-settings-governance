param(
    [string]$OutputDirectory = "",
    [string]$FocusCapacityId = ""
)

$ErrorActionPreference = "Stop"
$fabricResource = "https://api.fabric.microsoft.com"
$powerBiResource = "https://analysis.windows.net/powerbi/api"
$skillHeader = "x-ms-fabric-skill=fabriciq-ontology-cli"

if (-not $OutputDirectory) {
    $OutputDirectory = Join-Path $PSScriptRoot "output"
}

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Install it from https://aka.ms/installazurecliwindows."
}
if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw "Python 3 is required to generate the HTML and CSV files."
}

function Get-PagedFabricData {
    param([Parameter(Mandatory = $true)][string]$Url)

    $records = @()
    do {
        $responseJson = az rest `
            --method get `
            --resource $fabricResource `
            --url $Url `
            --headers $skillHeader `
            --output json
        if ($LASTEXITCODE -ne 0) {
            throw "Fabric API request failed: $Url"
        }

        $response = $responseJson | ConvertFrom-Json
        $records += @($response.value)
        $Url = $response.continuationUri
    } while ($Url)

    return $records
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$resolvedOutputDirectory = (Resolve-Path $OutputDirectory).Path

$tenantSettings = Get-PagedFabricData `
    "$fabricResource/v1/admin/tenantsettings"
$capacityOverrides = Get-PagedFabricData `
    "$fabricResource/v1/admin/capacities/delegatedTenantSettingOverrides"

$adminCapacitiesJson = az rest `
    --method get `
    --resource $powerBiResource `
    --url "https://api.powerbi.com/v1.0/myorg/admin/capacities" `
    --output json
if ($LASTEXITCODE -ne 0) {
    throw "Power BI admin capacity inventory request failed."
}

[IO.File]::WriteAllText(
    (Join-Path $resolvedOutputDirectory "tenant-settings.json"),
    ($tenantSettings | ConvertTo-Json -Depth 30),
    [Text.UTF8Encoding]::new($false)
)
[IO.File]::WriteAllText(
    (Join-Path $resolvedOutputDirectory "capacity-overrides.json"),
    ($capacityOverrides | ConvertTo-Json -Depth 30),
    [Text.UTF8Encoding]::new($false)
)
[IO.File]::WriteAllText(
    (Join-Path $resolvedOutputDirectory "admin-capacities.json"),
    $adminCapacitiesJson,
    [Text.UTF8Encoding]::new($false)
)

$generator = Join-Path $PSScriptRoot "generate_fabric_tenant_settings_report.py"
$arguments = @($generator, $resolvedOutputDirectory)
if ($FocusCapacityId) {
    $arguments += @("--focus-capacity-id", $FocusCapacityId)
}

python @arguments
if ($LASTEXITCODE -ne 0) {
    throw "Report generation failed."
}

Write-Host "Report: $(Join-Path $resolvedOutputDirectory 'fabric-capacity-tenant-setting-overrides.html')"
Write-Host "CSV:    $(Join-Path $resolvedOutputDirectory 'fabric-capacity-tenant-setting-overrides.csv')"
