<#
.SYNOPSIS
  One-command verification for piterm-qgis-ai.

.DESCRIPTION
  Runs the full verification matrix from a single entry point:

    1) pytest                      - wire / security / manifest / round-trip +
                                     sandbox-interlock + client-path + settings-
                                     coercion unit tests (no QGIS)
    2) qgis_headless_smoke.py      - 63-check baseline per installed QGIS build
    3) qgis_command_coverage.py    - full 195-command sweep per build;
                                     with -Sandbox it also exercises
                                     db.connection_add / db.import_layer against
                                     a throwaway settings root
                                     (QGIS_CUSTOM_CONFIG_PATH inside
                                     _scratch\cov_sandbox - operator profiles
                                     are hash-verified untouched)
    4) qgis_live_coverage.py       - 33 live-GUI checks against running bridge
                                     instances (canvas / ui / themes / ...);
                                     per-instance tokens via
                                     -LiveTokens 9887=<secret> (or the
                                     PITERM_QGIS_TOKEN_<port> env vars)

  Every suite writes its own JSON evidence under verification\logs\; the full
  console output of every suite is additionally captured (Tee-Object) into
  verification\logs\verify_all_<stamp>.log. Exit code 0 only when every
  requested suite passed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\verify_all.ps1 -Sandbox

.EXAMPLE
  scripts\verify_all.ps1 -SkipCoverage -SkipLive     # smoke-only quick pass

.EXAMPLE
  scripts\verify_all.ps1 -Versions 4.2.3,4.3.0-dev -Sandbox

.EXAMPLE
  scripts\verify_all.ps1 -LivePorts 9877 -AllowMissing   # one GUI, no 4.3.0-dev
#>
param(
  [string[]]$Versions = @('3.44.15', '4.2.3', '4.3.0-dev'),
  [switch]$Sandbox,
  [switch]$SkipUnit,
  [switch]$SkipSmoke,
  [switch]$SkipCoverage,
  [switch]$SkipLive,
  [string[]]$LivePorts = @('9877', '9887'),
  [string[]]$LiveTokens = @(),
  [switch]$AllowMissing
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $PSScriptRoot
$logs = Join-Path $root 'verification\logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$script:logPath = Join-Path $logs "verify_all_$stamp.log"
$failures = New-Object System.Collections.Generic.List[string]

# `powershell -File script.ps1 -Versions 4.2.3,4.3.0` hands over ONE string
# ("4.2.3,4.3.0") - splitting it here also avoids the numeric-parse trap where
# "9877,9887" becomes the single integer 98779887.
function Split-List([string[]]$items) {
  $out = @()
  foreach ($item in $items) {
    foreach ($chunk in ([string]$item -split '[,\s;]+')) {
      if ($chunk) { $out += $chunk.Trim() }
    }
  }
  return $out
}

$Versions = Split-List $Versions
$livePorts = @(Split-List $LivePorts | ForEach-Object { [int]$_ })

# Per-instance tokens, e.g. -LiveTokens 9887=<secret>,9877=<secret>: instances
# are separate QGIS profiles, so one may enforce a token while another does not.
$liveTokenMap = @{}
foreach ($item in (Split-List $LiveTokens)) {
  $pair = [string]$item -split '=', 2
  if ($pair.Count -eq 2) { $liveTokenMap[$pair[0].Trim()] = $pair[1].Trim() }
}

function Write-Log([string]$text) {
  Write-Host $text
  Add-Content -LiteralPath $script:logPath -Value $text -Encoding UTF8
}

$interpreters = @{
  '3.44.15'   = 'C:\Program Files\QGIS 3.44.15\bin\python-qgis-ltr.bat'
  '4.2.3'     = 'C:\Program Files\QGIS 4.2.3\bin\python-qgis.bat'
  '4.3.0-dev' = 'D:\qgis\4.3.0\bin\python-qgis-dev.bat'
}
$sandboxSuffix = @{ '3.44.15' = '344'; '4.2.3' = '423'; '4.3.0-dev' = '430' }

Write-Log "verify_all  root=$root"
Write-Log "verify_all  versions=$($Versions -join ', ')  sandbox=$Sandbox"
try {
  Push-Location $root

  if (-not $SkipUnit) {
    Write-Log "`n=== unit tests (pytest) ==="
    $env:PYTHONPATH = Join-Path $root '_tools\py'
    python -m pytest tests -q 2>&1 |
      Tee-Object -FilePath $script:logPath -Append
    if ($LASTEXITCODE -ne 0) { $failures.Add('pytest') }
  }

  foreach ($version in $Versions) {
    $py = $interpreters[$version]
    if (-not $py -or -not (Test-Path $py)) {
      if ($AllowMissing) {
        Write-Log "`n=== ${version}: interpreter not found - skipped (-AllowMissing) ==="
        continue
      }
      Write-Log "`n=== ${version}: interpreter not found - FAILED (use -AllowMissing to skip) ==="
      $failures.Add("missing:$version")
      continue
    }
    if (-not $SkipSmoke) {
      Write-Log "`n=== smoke / ${version} ==="
      & $py -u (Join-Path $root 'scripts\qgis_headless_smoke.py') 2>&1 |
        Tee-Object -FilePath $script:logPath -Append
      if ($LASTEXITCODE -ne 0) { $failures.Add("smoke:$version") }
    }
    if (-not $SkipCoverage) {
      Write-Log "`n=== coverage / ${version}$(if ($Sandbox) { ' (sandboxed db)' }) ==="
      $covArgs = @('-u', (Join-Path $root 'scripts\qgis_command_coverage.py'))
      if ($Sandbox) {
        $covArgs += '--sandbox-db'
        $env:QGIS_CUSTOM_CONFIG_PATH = Join-Path (Split-Path -Parent $root) `
          ("_scratch\cov_sandbox\cfg_" + $sandboxSuffix[$version])
      }
      & $py @covArgs 2>&1 |
        Tee-Object -FilePath $script:logPath -Append
      if ($LASTEXITCODE -ne 0) { $failures.Add("coverage:$version") }
      if ($Sandbox) {
        Remove-Item Env:QGIS_CUSTOM_CONFIG_PATH -ErrorAction SilentlyContinue
      }
    }
  }

  if (-not $SkipLive) {
    foreach ($port in $livePorts) {
      Write-Log "`n=== live coverage / port $port ==="
      $tokenVar = "PITERM_QGIS_TOKEN_$port"
      $hadToken = $false
      if ($liveTokenMap.ContainsKey([string]$port)) {
        [Environment]::SetEnvironmentVariable($tokenVar, $liveTokenMap[[string]$port])
        $hadToken = $true
      }
      python (Join-Path $root 'scripts\qgis_live_coverage.py') $port 2>&1 |
        Tee-Object -FilePath $script:logPath -Append
      if ($LASTEXITCODE -ne 0) { $failures.Add("live:$port") }
      if ($hadToken) { [Environment]::SetEnvironmentVariable($tokenVar, $null) }
    }
  }
} finally {
  Pop-Location
}

Write-Log ""
Write-Log "verify_all  log: $script:logPath"
if ($failures.Count -eq 0) {
  Write-Log "VERIFY ALL OK - every requested suite passed"
  exit 0
}
Write-Log ("VERIFY ALL FAILED: " + ($failures -join ', '))
exit 1
