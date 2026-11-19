param()
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot -Parent
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (!(Test-Path -LiteralPath $vswhere)) { throw 'Install Visual Studio C++ build tools.' }
$vs = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (!$vs) { throw 'MSVC x64 build tools were not found.' }
$devcmd = Join-Path $vs 'Common7\Tools\VsDevCmd.bat'
$envScript = Join-Path $root 'build\msvc-env.cmd'
[IO.File]::WriteAllLines($envScript, @(('@call "' + $devcmd + '" -no_logo -arch=x64 -host_arch=x64 >nul'), '@set'), [Text.Encoding]::ASCII)
$environment = & $env:ComSpec /d /c $envScript
if ($LASTEXITCODE) { throw 'VsDevCmd failed.' }
foreach ($line in $environment) {
    if ($line -match '^([^=]+)=(.*)$') { [Environment]::SetEnvironmentVariable($matches[1], $matches[2], 'Process') }
}
$common = @('/nologo','/std:c++20','/EHsc','/W4','/Zi','/MD','/DNDEBUG','/D_ITERATOR_DEBUG_LEVEL=0','/arch:AVX2','/utf-8')
Push-Location $root
try {
    foreach ($config in @('Od','O2')) {
        New-Item -ItemType Directory -Path "build\$config","asm\$config" -Force | Out-Null
        $options = $common + @("/$config","/Fdbuild\$config\compiler.pdb","/Fobuild\$config\",'/FAs',"/Faasm\$config\")
        & cl @options 'src\benchmark.cpp' 'src\kernels.cpp' "/Febuild\$config\benchmark.exe" /link /DEBUG:FULL /INCREMENTAL:NO psapi.lib 2>&1 |
            Tee-Object -FilePath "build\$config\build.log"
        if ($LASTEXITCODE) { throw "Build failed: $config" }
        & dumpbin /nologo /disasm "build\$config\benchmark.exe" | Set-Content -Encoding UTF8 "asm\$config\benchmark.disasm.txt"
    }
    if (Test-Path 'src\sampler.cpp') {
        & cl @common /O2 'src\sampler.cpp' '/Fobuild\sampler.obj' '/Fdbuild\sampler.pdb' '/Febuild\sampler.exe' /link /DEBUG /INCREMENTAL:NO dbghelp.lib psapi.lib
        if ($LASTEXITCODE) { throw 'Sampler build failed.' }
    }
    @{ visual_studio=$vs; compiler=(Get-Command cl).Source; common_flags=$common; optimization=@('/Od','/O2'); timestamp=(Get-Date -Format o) } |
        ConvertTo-Json -Depth 3 | Set-Content -Encoding UTF8 'results\build-environment.json'
} finally { Pop-Location }
