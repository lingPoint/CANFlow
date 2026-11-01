param(
    [string]$OutputDirectory = 'dist',
    [string]$WorkDirectory = 'build'
)
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$python = (Get-Command python -CommandType Application | Select-Object -First 1).Source
if (-not $python) { throw 'Python was not found on PATH' }
$pythonDir = Split-Path $python -Parent
$previousPath = $env:PATH
try {
    # Do not let unrelated applications on PATH supply transitive Qt DLLs.
    $env:PATH = "$pythonDir;$pythonDir\Scripts;$env:SystemRoot\System32;$env:SystemRoot"
    New-Item -ItemType Directory -Force -Path $WorkDirectory | Out-Null
    & $python -m PyInstaller --noconfirm --clean --onefile --windowed --name CANFlow `
        --add-data "$root\canflow\assets;canflow/assets" `
        --distpath $OutputDirectory --workpath $WorkDirectory --specpath $WorkDirectory `
        "$root\run.py"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
    $exe = Join-Path $OutputDirectory 'CANFlow.exe'
    $archive = & $python -m PyInstaller.utils.cliutils.archive_viewer -l $exe
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the EXE archive' }
    if ($archive -match "'icu(?:uc|in|dt[^']*)\.dll'") {
        throw 'Unexpected bundled ICU DLL: the build environment may be contaminated'
    }
} finally {
    $env:PATH = $previousPath
}
