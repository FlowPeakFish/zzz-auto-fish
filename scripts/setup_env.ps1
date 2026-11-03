[CmdletBinding()]
param(
    [string]$IndexUrl = 'https://pypi.tuna.tsinghua.edu.cn/simple'
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$envRoot = Join-Path $projectRoot 'envs'
$cacheRoot = Join-Path $envRoot '.cache'
$pythonExe = Join-Path $envRoot 'python\python.exe'
$archiveName = 'cpython-3.12.16+20261026-x86_64-pc-windows-msvc-install_only.tar.gz'
$expectedHash = '590dfcbe3a34c07301e6332e13e2188b98655cd072b08afa45a7998dc4c22bc5'
$downloadUrls = @(
    "https://mirrors.nju.edu.cn/github-release/astral-sh/python-build-standalone/20261026/$archiveName",
    "https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/20261026/$archiveName",
    "https://github.com/astral-sh/python-build-standalone/releases/download/20261026/$archiveName"
)
if (-not [Environment]::Is64BitOperatingSystem -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {
    throw 'This setup supports Windows x64 only.'
}
New-Item -ItemType Directory -Path $envRoot,$cacheRoot,(Join-Path $envRoot 'tmp') -Force | Out-Null
# These variables affect this process and its children only. No registry/PATH changes.
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONPATH = ''
$env:PIP_CONFIG_FILE = 'NUL'
$env:TEMP = Join-Path $envRoot 'tmp'
# Prefer the NTFS short name for build temp paths, still pointing inside envs.
# Some older source packages fail at MAX_PATH when the project path is long.
try {
    $shortTemp = (New-Object -ComObject Scripting.FileSystemObject).GetFolder($env:TEMP).ShortPath
    if ($shortTemp) { $env:TEMP = $shortTemp }
} catch { }
$env:TMP = $env:TEMP
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $archive = Join-Path $cacheRoot $archiveName
    $validArchive = (Test-Path -LiteralPath $archive) -and ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -eq $expectedHash)
    if (-not $validArchive) {
        $downloaded = $false
        foreach ($url in $downloadUrls) {
            try {
                Write-Host "Downloading portable Python: $url"
                Invoke-WebRequest -Uri $url -OutFile "$archive.part" -UseBasicParsing -TimeoutSec 180
                if ((Get-FileHash -LiteralPath "$archive.part" -Algorithm SHA256).Hash -ne $expectedHash) {
                    throw 'Python archive SHA256 mismatch.'
                }
                Move-Item -LiteralPath "$archive.part" -Destination $archive -Force
                $downloaded = $true
                break
            } catch {
                Write-Warning $_.Exception.Message
            }
        }
        if (-not $downloaded) { throw 'Python download failed. Check your network and run setup again.' }
    }
    $tarExe = Get-Command tar.exe -ErrorAction SilentlyContinue
    if (-not $tarExe) { throw 'tar.exe is required (included in supported Windows 10/11 versions).' }
    Write-Host 'Extracting Python into envs\python...'
    & $tarExe.Source -xf $archive -C $envRoot
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonExe)) { throw 'Python extraction failed.' }
}
$runtimeCheck = @'
import sys, tkinter, ssl, venv
assert sys.version_info[:2] == (3, 12)
print(sys.version)
print('Tcl:', tkinter.Tcl().eval('info patchlevel'))
'@
$runtimeCheck | & $pythonExe -s -B -
if ($LASTEXITCODE -ne 0) { throw 'Portable Python is incomplete. Remove envs\python and run setup again.' }
& $pythonExe -s -B -m pip --isolated install --disable-pip-version-check --no-warn-script-location --index-url $IndexUrl --cache-dir (Join-Path $cacheRoot 'pip') -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Run setup again to retry.' }
& $pythonExe -s -B (Join-Path $projectRoot 'fish.py') --self-check
if ($LASTEXITCODE -ne 0) { throw 'Environment self-check failed.' }
Write-Host 'Environment is ready. Use start.bat to launch (run as administrator for the game).'
