param(
    [int]$N = 10000000,
    [int]$Repeats = 100,
    [int]$Batches = 200,
    [ValidateRange(1,10)][int]$Runs = 3,
    [ValidateRange(5,120)][int]$IdleSeconds = 20
)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$target = Join-Path $root ('results\energy-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $target -Force | Out-Null
Write-Host 'Start HWiNFO64 in Sensors-only mode.'
Write-Host 'Confirm CPU Package Power [W] is available; set sensor polling to 500 ms.'
Write-Host 'Start CSV logging using the Logging Start button, and save to:'
Write-Host (Join-Path $target 'hwinfo.csv') -ForegroundColor Cyan
Write-Host 'Keep HWiNFO logging throughout this script, including idle periods.'
Read-Host 'After logging has started, press Enter' | Out-Null
Start-Sleep -Seconds 3
$records = New-Object 'System.Collections.Generic.List[object]'
function UnixUs {
    return [long](([DateTime]::UtcNow.Ticks - [DateTime]::new(1970,1,1,0,0,0,[DateTimeKind]::Utc).Ticks) / 10)
}
for ($run = 1; $run -le $Runs; $run++) {
    $order = if ($run % 2) { @('Od','O2') } else { @('O2','Od') }
    foreach ($config in $order) {
        Write-Host "$config run $run - idle baseline $IdleSeconds seconds"
        $baselineStart = UnixUs
        Start-Sleep -Seconds $IdleSeconds
        $baselineEnd = UnixUs
        Write-Host "$config run $run - measuring workload"
        $exe = Join-Path $root "build\$config\benchmark.exe"
        $json = & $exe --n $N --repeats $Repeats --batches $Batches --mode sequential
        if ($LASTEXITCODE) { throw "Benchmark $config failed; keep the log for diagnosis." }
        $childFile = "$config-$run-child.json"
        $json | Set-Content -Encoding UTF8 (Join-Path $target $childFile)
        $child = ($json -join [Environment]::NewLine) | ConvertFrom-Json
        $records.Add([ordered]@{
            config=$config; run=$run; child_file=$childFile
            baseline_start_unix_us=$baselineStart; baseline_end_unix_us=$baselineEnd
            start_unix_us=$child.start_unix_us; end_unix_us=$child.end_unix_us
            workload_s=$child.workload_s
        })
        [ordered]@{
            schema_version=1; sensor_scope='cpu_package'; expected_sensor='CPU Package Power [W]'
            sensor_log='hwinfo.csv'; timestamp_timezone=[TimeZoneInfo]::Local.Id
            timestamp_utc_offset=[DateTimeOffset]::Now.ToString('zzz',[Globalization.CultureInfo]::InvariantCulture)
            parameters=@{n=$N;repeats=$Repeats;batches=$Batches;runs=$Runs;idle_seconds=$IdleSeconds}
            records=$records; energy_measurement_status='awaiting_sensor_log_analysis'
        } | ConvertTo-Json -Depth 6 | Set-Content -Encoding UTF8 (Join-Path $target 'manifest.json')
    }
}
Start-Sleep -Seconds 2
Write-Host 'All workload runs finished. Stop HWiNFO logging so the CSV is flushed.'
Write-Host "Results directory: $target"
Write-Host 'After the CSV is saved, run scripts\analyze-energy.py with the results directory above.'
