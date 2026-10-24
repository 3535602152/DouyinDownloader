param([switch]$DevelopmentBuild)
$ErrorActionPreference = 'Stop'
$Repo = [System.IO.Path]::GetFullPath($PSScriptRoot)
$BuildRoot = if ($env:DOUYIN_BUILD_ROOT) { [System.IO.Path]::GetFullPath($env:DOUYIN_BUILD_ROOT) } else { Join-Path $Repo 'build' }
$WorkPath = if ($env:DOUYIN_BUILD_WORKPATH) { [System.IO.Path]::GetFullPath($env:DOUYIN_BUILD_WORKPATH) } else { Join-Path $BuildRoot '_pyinstaller' }
Push-Location $Repo
try {
    $Dirty = [bool](git status --porcelain)
    if ($Dirty -and -not $DevelopmentBuild) { throw 'Release builds require a clean worktree.' }
    $Branch = (git branch --show-current).Trim()
    if (-not $Branch) { $Branch = 'detached-' + (git rev-parse --short HEAD).Trim() }
    $Slug = ($Branch.ToLowerInvariant() -replace '[^a-z0-9._-]+', '-').Trim('-')
    if (-not $Slug) { throw 'Cannot determine build folder.' }
    $Target = [System.IO.Path]::GetFullPath((Join-Path $BuildRoot $Slug))
    if (-not $Target.StartsWith($BuildRoot.TrimEnd('\') + '\', [System.StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe build target.' }
    New-Item -ItemType Directory -Force -Path $BuildRoot,$WorkPath | Out-Null
    $env:UV_PROJECT_ENVIRONMENT = Join-Path $BuildRoot '_environment312'
    & uv sync --locked --python 3.12 --extra browser --extra dev --extra build
    if ($LASTEXITCODE) { throw 'Locked build dependency installation failed.' }
    $Python = Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts\python.exe'
    & $Python scripts/verify_media.py .tools/ffmpeg
    if ($LASTEXITCODE) { throw 'Media verification failed.' }
    & $Python scripts/check_release.py
    if ($LASTEXITCODE) { throw 'Release contracts failed.' }
    & $Python -m PyInstaller --noconfirm --clean --workpath $WorkPath --distpath $Target DouyinDownloader.spec
    if ($LASTEXITCODE) { throw 'PyInstaller failed.' }
    $AppDir = Join-Path $Target 'DouyinDownloader'
    $Exe = Join-Path $AppDir 'DouyinDownloader.exe'
    if (-not (Test-Path -LiteralPath $Exe)) { throw 'Expected EXE missing.' }
    $oldDataDir = $env:DOUYIN_DOWNLOADER_DATA_DIR
    try {
        $env:DOUYIN_DOWNLOADER_DATA_DIR = Join-Path $BuildRoot '_selftest-data'
        $Process = Start-Process -FilePath $Exe -ArgumentList '--self-test' -PassThru -WindowStyle Hidden
        if (-not $Process.WaitForExit(120000)) { $Process.Kill(); throw 'EXE self-test timed out.' }
        if ($Process.ExitCode -ne 0) { throw 'EXE self-test failed; inspect desktop_self_test.log.' }
        $SelfTest = Join-Path $AppDir 'desktop_self_test.log'
        $Text = Get-Content -LiteralPath $SelfTest -Raw
        foreach ($Expected in @('sqlite=OK','playwright=OK','ffmpeg=OK')) {
            if (-not $Text.Contains($Expected)) { throw "Self-test missing $Expected" }
        }
        # The log includes the isolated local path, so it must not enter the ZIP.
        Remove-Item -LiteralPath $SelfTest
    } finally { $env:DOUYIN_DOWNLOADER_DATA_DIR = $oldDataDir }
    $Info = @{ version=(Get-Content VERSION -Raw).Trim(); commit=(git rev-parse HEAD).Trim(); branch=$Branch; dirty=$Dirty; exe='DouyinDownloader\DouyinDownloader.exe' }
    $Info | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Target 'BUILD_INFO.json') -Encoding UTF8
    Write-Output "Build verified: $Exe"
} finally { Pop-Location }
