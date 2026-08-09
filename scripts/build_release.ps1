[CmdletBinding()]
param(
    [string]$OutputPath = "",
    [string]$PythonExecutable = "",
    [string]$RustBinaryPath = "",
    [switch]$SkipRustBuild,
    [switch]$WithoutWheelhouse,
    [switch]$NoArchive,
    [switch]$AllowDirtySource,
    [switch]$AllowUnsignedDevelopmentBinary
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
if ([string]::IsNullOrWhiteSpace($OutputPath)) {
    $OutputPath = Join-Path $RepoRoot "dist\bongus-release"
} elseif (-not [IO.Path]::IsPathRooted($OutputPath)) {
    $OutputPath = Join-Path $RepoRoot $OutputPath
}
$OutputPath = [IO.Path]::GetFullPath($OutputPath)
$ArchivePath = "$OutputPath.zip"

if ($OutputPath -eq $RepoRoot) {
    throw "The release output cannot be the repository root."
}
if (Test-Path -LiteralPath $OutputPath) {
    throw "Refusing to replace existing release directory: $OutputPath"
}
if ((-not $NoArchive) -and ((Test-Path -LiteralPath $ArchivePath) -or (Test-Path -LiteralPath "$ArchivePath.sha256"))) {
    throw "Refusing to replace an existing release archive or digest: $ArchivePath"
}

if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $venvPython = Join-Path $RepoRoot ".venv\Scripts\python.exe"
    $PythonExecutable = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { "python" }
}

$ExpectedPython = (Get-Content -LiteralPath (Join-Path $RepoRoot ".python-version") -Raw).Trim()
$ActualPython = (& $PythonExecutable -c "import platform; print(platform.python_version())").Trim()
if ($LASTEXITCODE -ne 0 -or $ActualPython -ne $ExpectedPython) {
    throw "Release packaging requires Python $ExpectedPython; got $ActualPython."
}

$ToolchainText = Get-Content -LiteralPath (Join-Path $RepoRoot "rust-toolchain.toml") -Raw
$ToolchainMatch = [regex]::Match($ToolchainText, 'channel\s*=\s*"([^"]+)"')
if (-not $ToolchainMatch.Success) {
    throw "rust-toolchain.toml does not declare a channel."
}
$ExpectedRust = $ToolchainMatch.Groups[1].Value

$GitStatus = @(& git -C $RepoRoot status --porcelain=v1 --untracked-files=normal)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the Git source revision."
}
if ($GitStatus.Count -gt 0 -and -not $AllowDirtySource) {
    throw "The worktree is dirty. Commit/review changes or pass -AllowDirtySource explicitly."
}
$SourceRevision = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $SourceRevision -notmatch '^[0-9a-f]{40}$') {
    throw "Unable to resolve the Git source revision."
}
if ($GitStatus.Count -gt 0) {
    $SourceRevision = "$SourceRevision-dirty"
}

if ([string]::IsNullOrWhiteSpace($RustBinaryPath)) {
    if (-not $SkipRustBuild) {
        & cargo build --manifest-path (Join-Path $RepoRoot "execution_engine\Cargo.toml") --locked --release
        if ($LASTEXITCODE -ne 0) {
            throw "cargo build --locked --release failed."
        }
    }
    $RustBinaryPath = Join-Path $RepoRoot "execution_engine\target\release\execution_engine.exe"
} elseif (-not [IO.Path]::IsPathRooted($RustBinaryPath)) {
    $RustBinaryPath = Join-Path $RepoRoot $RustBinaryPath
}
$RustBinaryPath = [IO.Path]::GetFullPath($RustBinaryPath)
if (-not (Test-Path -LiteralPath $RustBinaryPath -PathType Leaf)) {
    throw "The packaged Rust executable does not exist: $RustBinaryPath"
}
if ((Get-Item -LiteralPath $RustBinaryPath).Length -le 0) {
    throw "The packaged Rust executable is empty: $RustBinaryPath"
}
$Signature = Get-AuthenticodeSignature -LiteralPath $RustBinaryPath
if ((-not $AllowUnsignedDevelopmentBinary) -and $Signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw (
        "Production packaging requires a valid Authenticode-signed Rust executable. " +
        "Status=$($Signature.Status). Use -AllowUnsignedDevelopmentBinary only for a manifest-marked, non-production package."
    )
}
$ProductionEligible = (
    (-not $AllowUnsignedDevelopmentBinary) -and
    (-not $WithoutWheelhouse) -and
    ($GitStatus.Count -eq 0)
)
$SignatureStatus = [string]$Signature.Status
$SignerThumbprint = if ($null -ne $Signature.SignerCertificate) { [string]$Signature.SignerCertificate.Thumbprint } else { "" }
$SignerSubject = if ($null -ne $Signature.SignerCertificate) { [string]$Signature.SignerCertificate.Subject } else { "" }

function Copy-ReleaseFile {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$RelativeDestination
    )
    $SourceItem = Get-Item -LiteralPath $Source
    if (($SourceItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Release inputs cannot be links/reparse points: $Source"
    }
    $Destination = Join-Path $OutputPath $RelativeDestination
    $DestinationDirectory = Split-Path -Parent $Destination
    if (-not (Test-Path -LiteralPath $DestinationDirectory)) {
        New-Item -ItemType Directory -Path $DestinationDirectory | Out-Null
    }
    Copy-Item -LiteralPath $Source -Destination $Destination
}

New-Item -ItemType Directory -Path $OutputPath | Out-Null

# Python runtime package and static dashboard assets. Bytecode, tests, data,
# caches, and build outputs are excluded by the extension allowlist.
$BongusRoot = Join-Path $RepoRoot "bongus"
$AllowedExtensions = @(".py", ".json", ".html")
$ExcludedPackagePrefixes = @(
    ([IO.Path]::GetFullPath((Join-Path $BongusRoot "research")).TrimEnd('\') + '\'),
    ([IO.Path]::GetFullPath((Join-Path $BongusRoot "testing")).TrimEnd('\') + '\')
)
Get-ChildItem -LiteralPath $BongusRoot -Recurse -File | ForEach-Object {
    if (($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "Release inputs cannot be links/reparse points: $($_.FullName)"
    }
    $IsExcluded = $false
    foreach ($ExcludedPrefix in $ExcludedPackagePrefixes) {
        if ($_.FullName.StartsWith($ExcludedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            $IsExcluded = $true
            break
        }
    }
    if ((-not $IsExcluded) -and ($AllowedExtensions -contains $_.Extension.ToLowerInvariant())) {
        $Relative = $_.FullName.Substring($RepoRoot.Length).TrimStart('\', '/')
        Copy-ReleaseFile -Source $_.FullName -RelativeDestination $Relative
    }
}

Copy-ReleaseFile -Source (Join-Path $RepoRoot "scripts\__init__.py") -RelativeDestination "scripts\__init__.py"
Copy-ReleaseFile -Source (Join-Path $RepoRoot "scripts\live_trader_v2.py") -RelativeDestination "scripts\live_trader_v2.py"
Copy-ReleaseFile -Source (Join-Path $RepoRoot "scripts\release_manifest.py") -RelativeDestination "scripts\release_manifest.py"
Copy-ReleaseFile -Source (Join-Path $RepoRoot "requirements-runtime.txt") -RelativeDestination "requirements-runtime.txt"
Copy-ReleaseFile -Source (Join-Path $RepoRoot "live_config.json") -RelativeDestination "live_config.json"
Copy-ReleaseFile -Source (Join-Path $RepoRoot "LICENSE") -RelativeDestination "LICENSE"
Copy-ReleaseFile -Source (Join-Path $RepoRoot "deployment\Install-BongusRelease.ps1") -RelativeDestination "Install-BongusRelease.ps1"
Copy-ReleaseFile -Source (Join-Path $RepoRoot "deployment\README.md") -RelativeDestination "README.md"
Copy-ReleaseFile -Source $RustBinaryPath -RelativeDestination "bin\execution_engine.exe"

# Rewrite only the staged manifest. The development tree points at Cargo's
# release output; the runtime package points at its contained binary.
$StagedProcessManifestPath = Join-Path $OutputPath "bongus\runtime\process_manifest.json"
$StagedProcessManifest = Get-Content -LiteralPath $StagedProcessManifestPath -Raw | ConvertFrom-Json
if ($StagedProcessManifest.schema_version -ne 1 -or $StagedProcessManifest.processes.rust.kind -ne "binary") {
    throw "The source process manifest does not describe a schema-v1 Rust binary."
}
$StagedProcessManifest.processes.rust.target = "bin/execution_engine"
$StagedProcessManifestJson = $StagedProcessManifest | ConvertTo-Json -Depth 20
[IO.File]::WriteAllText($StagedProcessManifestPath, "$StagedProcessManifestJson`n", [Text.UTF8Encoding]::new($false))

if (-not $WithoutWheelhouse) {
    $Wheelhouse = Join-Path $OutputPath "wheelhouse"
    New-Item -ItemType Directory -Path $Wheelhouse | Out-Null
    # Materialize wheels on the build host, including for any source-only
    # dependency. The runtime installer accepts wheels only and never compiles.
    & $PythonExecutable -m pip wheel `
        --disable-pip-version-check `
        --no-deps `
        --requirement (Join-Path $RepoRoot "requirements-runtime.txt") `
        --wheel-dir $Wheelhouse
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to build the offline Python wheelhouse."
    }
}

$ManifestArguments = @(
    (Join-Path $RepoRoot "scripts\release_manifest.py"),
    "create",
    $OutputPath,
    "--source-revision", $SourceRevision,
    "--python-version", $ExpectedPython,
    "--rust-toolchain", $ExpectedRust,
    "--rust-signature-status", $SignatureStatus
)
if (-not [string]::IsNullOrWhiteSpace($SignerThumbprint)) {
    $ManifestArguments += @("--rust-signer-thumbprint", $SignerThumbprint)
}
if (-not [string]::IsNullOrWhiteSpace($SignerSubject)) {
    $ManifestArguments += @("--rust-signer-subject", $SignerSubject)
}
if ($ProductionEligible) {
    $ManifestArguments += "--production-eligible"
}
if ($WithoutWheelhouse) {
    $ManifestArguments += "--allow-missing-wheelhouse"
}
& $PythonExecutable @ManifestArguments | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Release manifest creation failed."
}

$VerifyArguments = @((Join-Path $RepoRoot "scripts\release_manifest.py"), "verify", $OutputPath)
if (-not $WithoutWheelhouse) {
    $VerifyArguments += "--require-offline"
}
if ($ProductionEligible) {
    $VerifyArguments += "--require-production"
}
& $PythonExecutable @VerifyArguments | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Release package verification failed."
}

if (-not $NoArchive) {
    & $PythonExecutable (Join-Path $RepoRoot "scripts\release_manifest.py") archive $OutputPath $ArchivePath | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Release archive creation failed."
    }
}

$Manifest = Get-Content -LiteralPath (Join-Path $OutputPath "release-manifest.json") -Raw | ConvertFrom-Json
[pscustomobject]@{
    ReleaseDirectory = $OutputPath
    Archive = if ($NoArchive) { $null } else { $ArchivePath }
    SourceRevision = $Manifest.source_revision
    FileCount = $Manifest.file_count
    TotalBytes = $Manifest.total_bytes
    ApplicationBytes = $Manifest.size_contract.application_bytes
    OfflineInstallable = $Manifest.offline_installable
    ProductionEligible = $Manifest.production_eligible
    RustBinarySha256 = $Manifest.rust_binary.sha256
}
