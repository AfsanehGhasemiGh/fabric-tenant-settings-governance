<#
.SYNOPSIS
Disables capacity delegation for explicitly selected Microsoft Fabric tenant settings.

.DESCRIPTION
Reads the current tenant setting, preserves its enabled state, security-group scopes,
properties, and other delegation values, and sets delegateToCapacity to false.

Use -WhatIf to preview the request without sending the update.

.EXAMPLE
.\Disable-FabricCapacityDelegation.ps1 `
    -TenantId "00000000-0000-0000-0000-000000000000" `
    -TenantSettingName "EnableAOAI" `
    -WhatIf
#>

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [Parameter(Mandatory = $true)]
    [Guid]$TenantId,

    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string[]]$TenantSettingName
)

$ErrorActionPreference = "Stop"
$fabricResource = "https://api.fabric.microsoft.com"
$skillHeader = "x-ms-fabric-skill=fabriciq-ontology-cli"

if (-not (Get-Command az -ErrorAction SilentlyContinue)) {
    throw "Azure CLI is required. Install it from https://aka.ms/installazurecliwindows."
}

$tokenCheck = az account get-access-token `
    --tenant $TenantId `
    --resource $fabricResource `
    --query expiresOn `
    --output tsv 2>&1

if ($LASTEXITCODE -ne 0) {
    throw @"
Azure CLI is not authenticated to tenant '$TenantId'.
Run:
az login --tenant $TenantId --allow-no-subscriptions

Azure CLI response:
$($tokenCheck -join "`n")
"@
}

$tenantSettings = @()
$url = "$fabricResource/v1/admin/tenantsettings"

do {
    $responseJson = az rest `
        --method get `
        --resource $fabricResource `
        --url $url `
        --headers $skillHeader `
        --output json

    if ($LASTEXITCODE -ne 0) {
        throw "Unable to retrieve Fabric tenant settings."
    }

    $response = $responseJson | ConvertFrom-Json
    $tenantSettings += @($response.value)
    $url = $response.continuationUri
} while ($url)

foreach ($settingName in $TenantSettingName) {
    $current = $tenantSettings |
        Where-Object settingName -eq $settingName |
        Select-Object -First 1

    if (-not $current) {
        Write-Error "Tenant setting '$settingName' was not found."
        continue
    }

    if ($current.delegateToCapacity -ne $true) {
        Write-Host "'$settingName' is already not delegated to capacities." `
            -ForegroundColor Yellow
        continue
    }

    $body = [ordered]@{
        enabled            = [bool]$current.enabled
        delegateToCapacity = $false
    }

    foreach ($propertyName in @(
        "delegateToDomain",
        "delegateToWorkspace",
        "enabledSecurityGroups",
        "excludedSecurityGroups",
        "properties"
    )) {
        if ($current.PSObject.Properties[$propertyName]) {
            $body[$propertyName] = $current.$propertyName
        }
    }

    $json = $body | ConvertTo-Json -Depth 20
    $encodedSettingName = [Uri]::EscapeDataString($settingName)
    $updateUrl = "$fabricResource/v1/admin/tenantsettings/$encodedSettingName/update"

    Write-Host "`nSetting: $settingName"
    Write-Host "Current delegateToCapacity: $($current.delegateToCapacity)"
    Write-Host "Requested delegateToCapacity: false"
    Write-Host "Request body:"
    Write-Host $json

    if (-not $PSCmdlet.ShouldProcess(
        $settingName,
        "Disable Fabric capacity delegation"
    )) {
        continue
    }

    $updateResponseJson = az rest `
        --method post `
        --resource $fabricResource `
        --url $updateUrl `
        --headers "Content-Type=application/json" $skillHeader `
        --body $json `
        --output json

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to update tenant setting '$settingName'."
    }

    $updateResponse = $updateResponseJson | ConvertFrom-Json
    $updated = @($updateResponse.tenantSettings) |
        Where-Object settingName -eq $settingName |
        Select-Object -First 1

    if ($updated.delegateToCapacity -ne $false) {
        throw "The API response did not confirm delegateToCapacity=false."
    }

    Write-Host "Disabled capacity delegation for '$settingName'." `
        -ForegroundColor Green
}
