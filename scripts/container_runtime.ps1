# Provision and run exactly the requested Windows container runtime.
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('ensure', 'compose')]
    [string]$Action,
    [Parameter(Mandatory)]
    [ValidateSet('podman', 'docker')]
    [string]$Runtime,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArgs
)

$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DefaultComposeFile = Join-Path $ProjectRoot 'docker-compose.yml'

# Avoid a conflicting user-profile .config file. Podman honors XDG_CONFIG_HOME;
# keeping its local metadata here also leaves the user's profile untouched.
if ($Runtime -eq 'podman') {
    $env:XDG_CONFIG_HOME = Join-Path $ProjectRoot '.podman-config'
    New-Item -ItemType Directory -Force -Path $env:XDG_CONFIG_HOME | Out-Null

    # Podman otherwise prefers the Windows App Execution Alias for
    # docker-compose.exe when it is on PATH. Prefer the native provider when
    # it is installed so that Podman handles Compose consistently.
    $podmanCompose = Get-Command podman-compose -ErrorAction SilentlyContinue
    if ($podmanCompose) {
        $env:PODMAN_COMPOSE_PROVIDER = $podmanCompose.Source
    }
}

function Install-WithWinget([string]$PackageId, [string]$Name) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "$Name is not installed and winget is unavailable. Install $Name manually, then rerun the just command."
    }
    Write-Host "$Name was not found. Installing it with winget..."
    & winget install --id $PackageId --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "$Name installation failed. Install it manually, then rerun the just command." }
}

function Get-Podman {
    $command = Get-Command podman -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    $path = Join-Path $env:ProgramFiles 'RedHat\Podman\podman.exe'
    if (-not (Test-Path -LiteralPath $path)) { Install-WithWinget 'RedHat.Podman' 'Podman' }
    $command = Get-Command podman -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    if (Test-Path -LiteralPath $path) { return $path }
    throw 'Podman was installed but is not available in this shell. Open a new PowerShell window and rerun just start.'
}

function Get-Docker {
    $path = Join-Path $env:ProgramFiles 'Docker\Docker\resources\bin\docker.exe'
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    if (-not (Test-Path -LiteralPath $desktop)) { Install-WithWinget 'Docker.DockerDesktop' 'Docker Desktop' }
    $command = Get-Command docker -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    if (Test-Path -LiteralPath $path) { return $path }
    throw 'Docker Desktop was installed but is not available in this shell. Open a new PowerShell window and rerun just start-d.'
}

$runtimePath = if ($Runtime -eq 'podman') { Get-Podman } else { Get-Docker }

if ($Runtime -eq 'podman') {
    $machines = @(& $runtimePath machine list --format '{{.Name}} {{.Running}}' 2>$null)
    if ($machines.Count -eq 0) {
        & $runtimePath machine init
        if ($LASTEXITCODE -ne 0) { throw 'Podman machine initialization failed. Enable its required virtualization feature, restart if prompted, then rerun just start.' }
        $machines = @(& $runtimePath machine list --format '{{.Name}} {{.Running}}' 2>$null)
    }
    $isRunning = @($machines | Where-Object { $_ -match '\btrue\b' }).Count -gt 0
    if (-not $isRunning) {
        & $runtimePath machine start
        if ($LASTEXITCODE -ne 0) { throw 'Podman machine failed to start. Enable its required virtualization feature, restart if prompted, then rerun just start.' }
    }
} else {
    $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
    & $runtimePath info 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0 -and (Test-Path -LiteralPath $desktop)) {
        Start-Process -FilePath $desktop
        $deadline = (Get-Date).AddSeconds(60)
        do {
            Start-Sleep -Seconds 3
            & $runtimePath info 2>$null | Out-Null
        } while ($LASTEXITCODE -ne 0 -and (Get-Date) -lt $deadline)
    }
}

& $runtimePath info | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "$Runtime is installed but not running. Enable its required virtualization feature and restart if prompted, then rerun the just command."
}

if ($Action -eq 'ensure') {
    Write-Host "Using $Runtime."
    exit 0
}

$SelectedComposeFiles = @()
$ForwardedComposeArgs = @()
for ($index = 0; $index -lt $ComposeArgs.Count; $index++) {
    $argument = $ComposeArgs[$index]
    if ($argument -in @('-f', '--file')) {
        if ($index + 1 -ge $ComposeArgs.Count) { throw "$argument requires a Compose file path." }
        $SelectedComposeFiles += $ComposeArgs[$index + 1]
        $index++
        continue
    }
    $ForwardedComposeArgs += $argument
}

if ($SelectedComposeFiles.Count -eq 0) { $SelectedComposeFiles = @($DefaultComposeFile) }
$ComposeInvocation = @()
foreach ($file in $SelectedComposeFiles) {
    $resolvedFile = if ([System.IO.Path]::IsPathRooted($file)) { $file } else { Join-Path $ProjectRoot $file }
    if (-not (Test-Path -LiteralPath $resolvedFile)) { throw "Compose file not found: $resolvedFile" }
    $ComposeInvocation += '-f'
    $ComposeInvocation += $resolvedFile
}

if ($Runtime -eq 'podman' -and $env:PODMAN_COMPOSE_PROVIDER -and $ForwardedComposeArgs -contains '--ignore-buildable') {
    # podman-compose 1.x does not support Docker Compose's pull-only flag.
    $ForwardedComposeArgs = @($ForwardedComposeArgs | Where-Object { $_ -ne '--ignore-buildable' })
}
& $runtimePath compose @ComposeInvocation @ForwardedComposeArgs
exit $LASTEXITCODE
