#requires -Version 5.1
<#
.SYNOPSIS
  Deploy Piterm QGIS AI into QGIS user profiles: plugin copy (with backup),
  enable key in the profile ini (with backup), optional manifest copy, verify.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -All
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Profile default,custom
  powershell -ExecutionPolicy Bypass -File scripts\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string[]]$Profile = @(),
    [switch]$All,
    [switch]$NoEnable,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$PluginSource = Join-Path $Root 'plugin\piterm_qgis_ai'
$ManifestSource = Join-Path $Root 'mcp\piterm_qgis_mcp\manifest.json'
$MinimalSource = Join-Path $Root 'mcp\piterm_qgis_mcp\minimal.py'

function Get-ProfileRoots {
    $roots = @()
    foreach ($base in @(
            (Join-Path $env:APPDATA 'QGIS\QGIS3\profiles'),
            (Join-Path $env:APPDATA 'QGIS\QGIS4\profiles'),
            (Join-Path $env:APPDATA 'QGIS3\profiles'),
            (Join-Path $env:APPDATA 'QGIS4\profiles'))) {
        if (Test-Path $base) { $roots += $base }
    }
    return $roots
}

function Get-TargetProfiles {
    $list = @()
    foreach ($root in Get-ProfileRoots) {
        $appName = Split-Path (Split-Path $root -Parent) -Leaf
        foreach ($dir in Get-ChildItem $root -Directory -ErrorAction SilentlyContinue) {
            if ($Profile.Count -gt 0 -and ($Profile -notcontains $dir.Name)) { continue }
            if ($Profile.Count -eq 0 -and -not $All -and $dir.Name -ne 'default') { continue }
            $list += [pscustomobject]@{
                App      = $appName
                Name     = $dir.Name
                Path     = $dir.FullName
                Ini      = Join-Path $dir.FullName ("QGIS\{0}.ini" -f $appName)
                Plugins  = Join-Path $dir.FullName 'python\plugins'
            }
        }
    }
    return $list
}

function Backup-File([string]$Path) {
    if (Test-Path $Path) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $backup = "$Path.bak-piterm-$stamp"
        Copy-Item $Path $backup -Force
        return $backup
    }
    return $null
}

function Set-NoBom([string]$Path, [string]$Content) {
    [System.IO.File]::WriteAllText($Path, $Content,
        (New-Object System.Text.UTF8Encoding($false)))
}

function Set-IniKey([string]$Content, [string]$Section, [string]$Key, [string]$Value) {
    # Upsert "<Key>=<Value>" inside [<Section>] of an ini file (QgsSettings layout).
    $escSection = [regex]::Escape($Section)
    $escKey = [regex]::Escape($Key)
    $pattern = "(?ms)^\[$escSection\]\s*\r?\n(?<body>.*?)(?=^\[|\z)"
    $match = [regex]::Match($Content, $pattern)
    if ($match.Success) {
        $body = $match.Groups['body'].Value
        if ($body -match "(?m)^\s*$escKey\s*=") {
            $body = [regex]::Replace($body, "(?m)^\s*$escKey\s*=.*$", "$Key=$Value")
        }
        else {
            $body = $body.TrimEnd() + "`r`n$Key=$Value`r`n"
        }
        return $Content.Substring(0, $match.Index) + "[$Section]`r`n" + $body +
            $Content.Substring($match.Index + $match.Length)
    }
    return $Content.TrimEnd() + "`r`n`r`n[$Section]`r`n$Key=$Value`r`n"
}

function Install-Profile($profile) {
    Write-Host ("== {0} / {1} ==" -f $profile.App, $profile.Name)
    $target = Join-Path $profile.Plugins 'piterm_qgis_ai'
    if (Test-Path $target) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $backup = "$target.bak-$stamp"
        Move-Item $target $backup -Force
        Write-Host "   backup:  $backup"
    }
    New-Item -ItemType Directory -Force -Path $profile.Plugins | Out-Null
    Copy-Item $PluginSource $target -Recurse -Force
    Get-ChildItem $target -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

    if ((Test-Path $ManifestSource) -and -not (Test-Path (Join-Path $target 'manifest.json'))) {
        Copy-Item $ManifestSource (Join-Path $target 'manifest.json') -Force
    }

    # Bundle the zero-dependency client so "Client setup…" works even when the
    # checkout is not next to the installed plugin.
    if (Test-Path $MinimalSource) {
        $bundleDir = Join-Path $target 'mcp\piterm_qgis_mcp'
        New-Item -ItemType Directory -Force -Path $bundleDir | Out-Null
        Copy-Item $MinimalSource (Join-Path $bundleDir 'minimal.py') -Force
        Write-Host "   bundled client: mcp\piterm_qgis_mcp\minimal.py"
    }
    else {
        Write-Warning "   client script missing at $MinimalSource (Client setup will need meta.repo_root)"
    }

    if (-not $NoEnable) {
        $ini = $profile.Ini
        if (Test-Path $ini) {
            $backup = Backup-File $ini
            if ($backup) { Write-Host "   ini backup: $backup" }
            $content = Get-Content $ini -Raw
            if ($content -match '(?ms)^\[PythonPlugins\]\s*\r?\n') {
                if ($content -match '(?m)^piterm_qgis_ai\s*=') {
                    $content = $content -replace '(?m)^piterm_qgis_ai\s*=.*$', 'piterm_qgis_ai=true'
                }
                else {
                    $content = $content -replace '(?ms)(^\[PythonPlugins\]\s*\r?\n)', "`$1piterm_qgis_ai=true`r`n"
                }
            }
            else {
                $content = $content.TrimEnd() + "`r`n`r`n[PythonPlugins]`r`npiterm_qgis_ai=true`r`n"
            }
            # Record the checkout so the Client-setup dialog can offer the full
            # FastMCP server as well as the bundled stdlib client.
            $content = Set-IniKey $content 'piterm_qgis_ai' 'meta.repo_root' ($Root -replace '\\', '/')
            Set-NoBom $ini $content
            Write-Host "   enabled in ini (meta.repo_root=$($Root -replace '\\','/'))"
        }
        else {
            Write-Host "   (ini missing: $ini - enable via Plugins > Manage)"
        }
    }
    $files = (Get-ChildItem $target -Recurse -File | Measure-Object).Count
    Write-Host "   deployed: $target ($files files)"
    return $target
}

function Uninstall-Profile($profile) {
    Write-Host ("== uninstall {0} / {1} ==" -f $profile.App, $profile.Name)
    $target = Join-Path $profile.Plugins 'piterm_qgis_ai'
    if (Test-Path $target) {
        Remove-Item $target -Recurse -Force
        Write-Host "   removed: $target"
    }
    $ini = $profile.Ini
    if ((Test-Path $ini) -and -not $NoEnable) {
        $backup = Backup-File $ini
        if ($backup) { Write-Host "   ini backup: $backup" }
        $content = Get-Content $ini -Raw
        $content = $content -replace '(?m)^piterm_qgis_ai\s*=.*$', 'piterm_qgis_ai=false'
        $content = Set-IniKey $content 'piterm_qgis_ai' 'meta.repo_root' ''
        Set-NoBom $ini $content
        Write-Host "   disabled in ini"
    }
}

# ---------------------------------------------------------------------------
$profiles = Get-TargetProfiles
if ($profiles.Count -eq 0) {
    Write-Warning "No QGIS profiles found. Use -Profile <name> or -All after starting QGIS once."
    exit 2
}
if ($Uninstall) {
    foreach ($p in $profiles) { Uninstall-Profile $p }
    Write-Host "Uninstalled. Restart QGIS."
    exit 0
}
foreach ($p in $profiles) { Install-Profile $p | Out-Null }
Write-Host ""
Write-Host "Done. Next steps:"
Write-Host "  1. Start QGIS - the 'Piterm QGIS AI' toolbar button appears."
Write-Host "  2. Click it (or use the dock) to start the bridge (default 9877/TCP, 9879/HTTP)."
Write-Host "  3. Open 'Client setup…' in the plugin menu to configure your AI client."
Write-Host "  CLI check:  python `"$Root\mcp\piterm_qgis_mcp\minimal.py`" ping"
