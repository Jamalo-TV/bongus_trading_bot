[CmdletBinding()]
param(
    [string]$PythonExecutable = "py",
    [string]$EnvironmentPath = ".venv"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ReleaseRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$ManifestTool = Join-Path $ReleaseRoot "scripts\release_manifest.py"
if (-not (Test-Path -LiteralPath $ManifestTool -PathType Leaf)) {
    throw "Release verifier is missing: $ManifestTool"
}

if ($PythonExecutable -eq "py") {
    $Launcher = @("py", "-3.11")
} else {
    $Launcher = @($PythonExecutable)
}

if ($Launcher.Count -eq 2) {
    $ManifestJson = & $Launcher[0] $Launcher[1] $ManifestTool verify $ReleaseRoot --require-offline --require-production
} else {
    $ManifestJson = & $Launcher[0] $ManifestTool verify $ReleaseRoot --require-offline --require-production
}
if ($LASTEXITCODE -ne 0) { throw "Production release verification failed." }
$Manifest = $ManifestJson | ConvertFrom-Json

if ($Launcher.Count -eq 2) {
    $ActualPython = (& $Launcher[0] $Launcher[1] -c "import platform; print(platform.python_version())").Trim()
} else {
    $ActualPython = (& $Launcher[0] -c "import platform; print(platform.python_version())").Trim()
}
if ($LASTEXITCODE -ne 0 -or $ActualPython -ne [string]$Manifest.toolchains.python) {
    throw "Installer requires exact Python $($Manifest.toolchains.python); got $ActualPython."
}

$CanonicalEnvironmentPath = [IO.Path]::GetFullPath((Join-Path $ReleaseRoot ".venv"))
$EnvironmentPath = if ([IO.Path]::IsPathRooted($EnvironmentPath)) {
    [IO.Path]::GetFullPath($EnvironmentPath)
} else {
    [IO.Path]::GetFullPath((Join-Path $ReleaseRoot $EnvironmentPath))
}
if ($EnvironmentPath -ne $CanonicalEnvironmentPath) {
    throw "Production Python environment must be the storage-accounted path: $CanonicalEnvironmentPath"
}
if (Test-Path -LiteralPath $EnvironmentPath) {
    throw "Refusing to replace an existing Python environment: $EnvironmentPath"
}

$RustRelativePath = ([string]$Manifest.rust_binary.path).Replace('/', '\')
$RustBinaryPath = [IO.Path]::GetFullPath((Join-Path $ReleaseRoot $RustRelativePath))
$RustSignature = Get-AuthenticodeSignature -LiteralPath $RustBinaryPath
if ($RustSignature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Packaged Rust executable failed Authenticode verification: $($RustSignature.Status)"
}
$ObservedThumbprint = [string]$RustSignature.SignerCertificate.Thumbprint
$ExpectedThumbprint = [string]$Manifest.rust_binary.authenticode.signer_thumbprint
if ($ObservedThumbprint -ne $ExpectedThumbprint) {
    throw "Packaged Rust signer thumbprint does not match the release manifest."
}

$VolumeRoot = [IO.Path]::GetPathRoot($EnvironmentPath)
$Drive = [IO.DriveInfo]::new($VolumeRoot)
$PythonRuntimeMaxBytes = [int64]$Manifest.size_contract.python_runtime_max_bytes
$MinimumFreeAfterInstallBytes = [int64]$Manifest.size_contract.minimum_free_after_install_bytes
$RequiredFreeBeforeInstall = $PythonRuntimeMaxBytes + $MinimumFreeAfterInstallBytes
if ($Drive.AvailableFreeSpace -lt $RequiredFreeBeforeInstall) {
    throw (
        "Insufficient peak-space headroom for installation: " +
        "free=$($Drive.AvailableFreeSpace), required=$RequiredFreeBeforeInstall."
    )
}

if ($Launcher.Count -eq 2) {
    & $Launcher[0] $Launcher[1] -m venv $EnvironmentPath
} else {
    & $Launcher[0] -m venv $EnvironmentPath
}
if ($LASTEXITCODE -ne 0) { throw "Unable to create the Python environment." }

$VenvPython = Join-Path $EnvironmentPath "Scripts\python.exe"
& $VenvPython -m pip install `
    --disable-pip-version-check `
    --no-index `
    --no-cache-dir `
    --only-binary=:all: `
    --find-links (Join-Path $ReleaseRoot "wheelhouse") `
    --requirement (Join-Path $ReleaseRoot "requirements-runtime.txt")
if ($LASTEXITCODE -ne 0) { throw "Offline runtime dependency installation failed." }

& $VenvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "Installed runtime dependencies are inconsistent." }

$ReparseEntries = @(
    Get-ChildItem -LiteralPath $EnvironmentPath -Recurse -Force | Where-Object {
        ($_.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0
    }
)
if ($ReparseEntries.Count -gt 0) {
    throw "Installed Python runtime contains an unaccountable link/reparse point."
}
$RuntimeBytes = (Get-ChildItem -LiteralPath $EnvironmentPath -Recurse -File | Measure-Object -Property Length -Sum).Sum
if ($null -eq $RuntimeBytes) { $RuntimeBytes = 0 }
if ([int64]$RuntimeBytes -gt $PythonRuntimeMaxBytes) {
    throw "Installed Python runtime exceeds its 600 MB hard budget: $RuntimeBytes bytes."
}
$RemainingFree = ([IO.DriveInfo]::new($VolumeRoot)).AvailableFreeSpace
if ($RemainingFree -lt $MinimumFreeAfterInstallBytes) {
    throw "Installation violated the required 4 GB free-space headroom: $RemainingFree bytes remain."
}

Write-Output "Production release installed and verified (python_runtime_bytes=$RuntimeBytes). No service was started."
Write-Output "Start explicitly with: $VenvPython -m bongus.monitoring.king_watchdog"
