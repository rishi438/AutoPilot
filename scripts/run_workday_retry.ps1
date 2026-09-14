[CmdletBinding()]
param(
    [Alias('AppId', 'Id')]
    [Guid]$ApplicationId,

    [string]$ApiUrl = 'http://127.0.0.1:8000',

    [ValidateRange(1, 65535)]
    [int]$VaultPort = 27118,

    [string]$LocalModel = 'dengcao/Qwen3-14B:Q5_K_M',

    [switch]$ResetToken
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$stateDirectory = Join-Path $repoRoot '.tmp\state'
$tokenCachePath = Join-Path $stateDirectory 'workday-worker-token.dpapi'
$testHandoffPath = Join-Path $repoRoot '.tmp\learnings.txt'
$tokenEnvironmentName = 'AUTOPILOT_WORKDAY_DEVICE_TOKEN'
$applicationEnvironmentName = 'AUTOPILOT_WORKDAY_APPLICATION_ID'

function Resolve-ApiUrl {
    param([string]$Value)

    $uri = [Uri]$Value
    if ($uri.Scheme -notin @('http', 'https') -or -not $uri.Host) {
        throw 'ApiUrl must be an absolute HTTP or HTTPS URL.'
    }
    if ($uri.Scheme -eq 'http' -and $uri.Host -notin @('localhost', '127.0.0.1', '::1')) {
        throw 'Plain HTTP is allowed only for a loopback API URL.'
    }
    if ($uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw 'ApiUrl must not contain credentials, query parameters, or fragments.'
    }
    return $Value.TrimEnd('/')
}

function Resolve-ApplicationId {
    param([AllowNull()][object]$ExplicitApplicationId)

    if ($null -ne $ExplicitApplicationId) {
        $parsedApplicationId = [Guid]::Empty
        if (-not [Guid]::TryParse(
            [string]$ExplicitApplicationId,
            [ref]$parsedApplicationId
        )) {
            throw 'ApplicationId must be a valid UUID.'
        }
        if ($parsedApplicationId -ne [Guid]::Empty) {
            return $parsedApplicationId
        }
    }
    if (-not (Test-Path -LiteralPath $testHandoffPath)) {
        throw 'No application ID was provided and the local handoff is missing.'
    }
    $handoffText = Get-Content -LiteralPath $testHandoffPath -Raw
    $applicationMatches = [regex]::Matches(
        $handoffText,
        '(?im)^\s*application under test\s*:\s*`?([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})`?\s*$'
    )
    if ($applicationMatches.Count -eq 0) {
        throw 'The application ID was not found in the local handoff.'
    }
    return [Guid]$applicationMatches[$applicationMatches.Count - 1].Groups[1].Value
}

function Read-CachedToken {
    if (-not (Test-Path -LiteralPath $tokenCachePath)) {
        return $null
    }
    try {
        $encrypted = (Get-Content -LiteralPath $tokenCachePath -Raw).Trim()
        if (-not $encrypted) {
            return $null
        }
        return ConvertTo-SecureString -String $encrypted
    }
    catch {
        Write-Warning 'The encrypted worker-token cache is unreadable; a new token is required.'
        return $null
    }
}

function Read-TestHandoffToken {
    if (-not (Test-Path -LiteralPath $testHandoffPath)) {
        return $null
    }
    $handoffText = Get-Content -LiteralPath $testHandoffPath -Raw
    $tokenMatches = [regex]::Matches(
        $handoffText,
        '(?im)^\s*application token\s*:\s*`?(apw_[0-9a-f]{32}_[A-Za-z0-9_-]{40,64})`?\s*$'
    )
    if ($tokenMatches.Count -eq 0) {
        return $null
    }
    return ConvertTo-SecureString `
        -String $tokenMatches[$tokenMatches.Count - 1].Groups[1].Value `
        -AsPlainText `
        -Force
}

function Save-CachedToken {
    param([Security.SecureString]$SecureToken)

    New-Item -ItemType Directory -Path $stateDirectory -Force | Out-Null
    $temporaryPath = Join-Path $stateDirectory ([IO.Path]::GetRandomFileName())
    try {
        $encrypted = ConvertFrom-SecureString -SecureString $SecureToken
        [IO.File]::WriteAllText($temporaryPath, $encrypted, [Text.Encoding]::UTF8)
        Move-Item -LiteralPath $temporaryPath -Destination $tokenCachePath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force
        }
    }
}

function ConvertTo-PlainText {
    param([Security.SecureString]$SecureToken)

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Invoke-RetryCheck {
    param(
        [string]$BaseUrl,
        [Guid]$TargetApplicationId,
        [string]$PlainToken
    )

    $retryUri = "$BaseUrl/api/v1/automation/worker/unit1/applications/$TargetApplicationId/retry-latest-review"
    $headers = @{ Authorization = "Bearer $PlainToken" }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri $retryUri -Headers $headers -TimeoutSec 20
        $retryStatus = $null
        try {
            $responseBody = $response.Content | ConvertFrom-Json
            $retryStatus = [string]$responseBody.retry_status
        }
        catch {
            $retryStatus = $null
        }
        return [PSCustomObject]@{
            StatusCode = [int]$response.StatusCode
            RetryStatus = $retryStatus
        }
    }
    catch {
        if ($null -ne $_.Exception.Response) {
            return [PSCustomObject]@{
                StatusCode = [int]$_.Exception.Response.StatusCode.value__
                RetryStatus = $null
            }
        }
        throw 'The local retry API is unavailable.'
    }
    finally {
        $headers.Clear()
    }
}

$resolvedApiUrl = Resolve-ApiUrl -Value $ApiUrl
$resolvedApplicationId = Resolve-ApplicationId -ExplicitApplicationId $ApplicationId

if ($ResetToken -and (Test-Path -LiteralPath $tokenCachePath)) {
    Remove-Item -LiteralPath $tokenCachePath -Force
}

$secureToken = Read-TestHandoffToken
$tokenCameFromTestHandoff = $null -ne $secureToken
if ($null -eq $secureToken) {
    $secureToken = Read-CachedToken
}
$plainToken = $null
$validated = $false
$unit1Complete = $false

try {
    for ($attempt = 1; $attempt -le 3 -and -not $validated; $attempt++) {
        $prompted = $false
        if ($null -eq $secureToken) {
            $secureToken = Read-Host 'Workday worker-device token' -AsSecureString
            $prompted = $true
        }
        $plainToken = (ConvertTo-PlainText -SecureToken $secureToken).Trim()
        if (-not $plainToken) {
            $secureToken = $null
            Write-Warning 'The worker-device token was empty.'
            continue
        }

        $retryCheck = Invoke-RetryCheck `
            -BaseUrl $resolvedApiUrl `
            -TargetApplicationId $resolvedApplicationId `
            -PlainToken $plainToken
        $statusCode = $retryCheck.StatusCode
        if ($statusCode -eq 401) {
            if (Test-Path -LiteralPath $tokenCachePath) {
                Remove-Item -LiteralPath $tokenCachePath -Force
            }
            $secureToken = $null
            $plainToken = $null
            $tokenCameFromTestHandoff = $false
            Write-Warning 'The worker token is invalid or expired; enter a replacement.'
            continue
        }
        if ($statusCode -ne 200) {
            throw "The retry API stopped with HTTP $statusCode."
        }

        if ($prompted -or $tokenCameFromTestHandoff) {
            Save-CachedToken -SecureToken $secureToken
        }
        $validated = $true
        $unit1Complete = $retryCheck.RetryStatus -eq 'unit1_complete'
        if (-not $unit1Complete) {
            Write-Host 'Retry check accepted; starting one visible worker attempt.'
        }
    }

    if (-not $validated) {
        throw 'A valid application-scope worker token is required.'
    }
    if ($unit1Complete) {
        Write-Host 'Workday Unit 1 is already complete; no retry or browser attempt is needed.'
        exit 0
    }

    [Environment]::SetEnvironmentVariable($tokenEnvironmentName, $plainToken, 'Process')
    [Environment]::SetEnvironmentVariable(
        $applicationEnvironmentName,
        $resolvedApplicationId.ToString(),
        'Process'
    )
    [Environment]::SetEnvironmentVariable('DEBUG', 'false', 'Process')
    & (Join-Path $repoRoot 'venv\Scripts\python.exe') `
        (Join-Path $repoRoot 'scripts\run_workday_account_gate.py') `
        --api-url $resolvedApiUrl `
        --vault-port $VaultPort `
        --local-model $LocalModel `
        --log-control-decisions `
        --accept-account-terms
    exit $LASTEXITCODE
}
finally {
    [Environment]::SetEnvironmentVariable($tokenEnvironmentName, $null, 'Process')
    [Environment]::SetEnvironmentVariable($applicationEnvironmentName, $null, 'Process')
    $plainToken = $null
    $secureToken = $null
}
