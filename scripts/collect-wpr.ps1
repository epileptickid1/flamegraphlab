param(
    [ValidateSet('CPU','Counters')][string]$Profile = 'Counters',
    [int]$N = 10000000,
    [int]$Repeats = 100,
    [int]$Batches = 40
)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (!$principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from a PowerShell terminal opened as Administrator. No elevation is attempted automatically.'
}
$root = Split-Path $PSScriptRoot -Parent
$target = Join-Path $root ('results\wpr-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $target -Force | Out-Null
$wpr = Join-Path $env:SystemRoot 'System32\wpr.exe'
& $wpr -pmcsources | Set-Content -Encoding UTF8 (Join-Path $target 'available-counters.txt')
foreach ($config in @('Od','O2')) {
    $symbolTarget = Join-Path $target "symbols\$config"
    New-Item -ItemType Directory -Path $symbolTarget -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $root "build\$config\benchmark.exe"),(Join-Path $root "build\$config\benchmark.pdb") -Destination $symbolTarget
    $instance = 'FlamegraphLab_' + [guid]::NewGuid().ToString('N')
    $started = $false
    $etl = Join-Path $target "$config.etl"
    $profileName = if ($Profile -eq 'Counters') { (Join-Path $PSScriptRoot 'counters.wprp') + '!LabCounters' } else { 'CPU' }
    try {
        $startLog = & $wpr -start $profileName -filemode -recordtempto $target -instancename $instance 2>&1
        $startExit = $LASTEXITCODE
        $startLog | Set-Content -Encoding UTF8 (Join-Path $target "$config-start.txt")
        if ($startExit) { throw "WPR start failed for $config; see saved start log. No recording claimed." }
        $started = $true
        $exe = Join-Path $root "build\$config\benchmark.exe"
        & $exe --n $N --repeats $Repeats --batches $Batches --mode sequential |
            Set-Content -Encoding UTF8 (Join-Path $target "$config-child.json")
        if ($LASTEXITCODE) { throw "Benchmark $config failed." }
    } finally {
        if ($started) {
            & $wpr -stop $etl -instancename $instance |
                Set-Content -Encoding UTF8 (Join-Path $target "$config-stop.txt")
            if ($LASTEXITCODE) { Write-Warning "Stopping own session failed. Session: $instance. See saved logs." }
        }
    }
}
Write-Host "Saved traces to $target"
Write-Host 'Open ETLs in WPA. Load symbols from build\Od or build\O2, filter to benchmark.exe and its measured interval.'
Write-Host 'CPU Usage (Sampled): stacks/hotspots. CPU Usage (Precise)/PMC views: counters and context switches.'
Write-Host 'These profiled timings are not substitutes for results\measurements.json. WPR CPU traces do not measure Joules.'
