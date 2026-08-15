[CmdletBinding()]
param(
    [ValidateRange(30, 600)]
    [int]$TimeoutSeconds = 150
)

$ErrorActionPreference = 'Stop'
$containerName = 'autopilot-zap-baseline'
$arguments = @(
    'run', '--rm', '--name', $containerName,
    '--network', 'autopilot_default',
    'ghcr.io/zaproxy/zaproxy:stable',
    'zap-baseline.py', '-t', 'http://app:8000', '-m', '1', '-T', '1'
)

$process = Start-Process -FilePath 'podman' -ArgumentList $arguments -NoNewWindow -PassThru

if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
    & podman stop --time 10 $containerName | Out-Null
    throw "ZAP did not finish within $TimeoutSeconds seconds and was stopped."
}

if ($process.ExitCode -eq 2) {
    Write-Output 'ZAP completed with warnings; review the report above.'
    exit 0
}

exit $process.ExitCode
