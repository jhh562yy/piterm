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
            Set-NoBom $ini $content
            Write-Host "   enabled in ini"
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
