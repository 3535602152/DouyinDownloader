param(
    [switch]$KeepWorkpath,
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"

# Enforce the one-latest-build-per-branch rule under build/.
# Keeps:
#   build/<branch-slug>/...   for every known local/remote branch, plus current HEAD
#   build/_pyinstaller/       only with -KeepWorkpath
# Removes:
#   other top-level build/* directories (wheel lib/bdist.*, old monitor-*, preview aliases, etc.)
#   the entire repo-local dist/ tree (no longer used as a shortcut)

$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$BuildRoot = Join-Path $Repo "build"
$DistRoot = Join-Path $Repo "dist"
if ($env:DOUYIN_BUILD_ROOT) {
    $BuildRoot = [System.IO.Path]::GetFullPath($env:DOUYIN_BUILD_ROOT)
}
if ($env:DOUYIN_BUILD_DIST_ROOT) {
    $DistRoot = [System.IO.Path]::GetFullPath($env:DOUYIN_BUILD_DIST_ROOT)
}

function ConvertTo-BranchSlug {
    param([Parameter(Mandatory = $true)][string]$Name)
    $slug = $Name.ToLowerInvariant()
    $slug = $slug -replace '[\\/]+', '-'
    $slug = $slug -replace '[^a-z0-9._-]+', '-'
    return $slug.Trim('-')
}

function Get-KnownBranchSlugs {
    Push-Location $Repo
    try {
        $branches = New-Object System.Collections.Generic.List[string]

        $current = (git rev-parse --abbrev-ref HEAD 2>$null | Out-String).Trim()
        if ($current -and $current -ne "HEAD") {
            $branches.Add((ConvertTo-BranchSlug $current)) | Out-Null
        } else {
            $short = (git rev-parse --short HEAD 2>$null | Out-String).Trim()
            if ($short) { $branches.Add((ConvertTo-BranchSlug "detached-$short")) | Out-Null }
        }

        $local = @(git for-each-ref --format="%(refname:short)" refs/heads 2>$null)
        $remote = @(git for-each-ref --format="%(refname:short)" refs/remotes/origin 2>$null)
        foreach ($raw in ($local + $remote)) {
            if (-not $raw) { continue }
            $name = [string]$raw
            if ($name -eq "origin" -or $name -match 'HEAD$') { continue }
            $name = $name -replace '^origin/', ''
            $slug = ConvertTo-BranchSlug $name
            if ($slug) { $branches.Add($slug) | Out-Null }
        }

        return @($branches | Where-Object { $_ } | Select-Object -Unique)
    }
    finally {
        Pop-Location
    }
}

$keep = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
$slugs = @(Get-KnownBranchSlugs)
foreach ($slug in $slugs) { [void]$keep.Add($slug) }
if ($KeepWorkpath) { [void]$keep.Add("_pyinstaller") }

if ($slugs.Count -eq 0) {
    throw "Refusing to clean build/: could not resolve any git branch slugs. Fix git first, or pass an explicit keep list."
}

Write-Host ("Keeping branch build dirs: " + ($slugs -join ", "))

$removed = @()
if (Test-Path -LiteralPath $BuildRoot) {
    Get-ChildItem -LiteralPath $BuildRoot -Directory | ForEach-Object {
        if ($keep.Contains($_.Name)) { return }
        $removed += $_.FullName
        if (-not $WhatIf) {
            Remove-Item -LiteralPath $_.FullName -Recurse -Force
        }
    }
}

if (Test-Path -LiteralPath $DistRoot) {
    $removed += $DistRoot
    if (-not $WhatIf) {
        Remove-Item -LiteralPath $DistRoot -Recurse -Force
    }
}

if ($WhatIf) {
    Write-Host "Would remove $($removed.Count) path(s):"
} else {
    Write-Host "Removed $($removed.Count) path(s):"
}
$removed | ForEach-Object { Write-Host " - $_" }
