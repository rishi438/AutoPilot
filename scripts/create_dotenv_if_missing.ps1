# Create .env from .env.local.example with random secrets (Windows, no Python required).
$ErrorActionPreference = 'Stop'
if (-not $PSScriptRoot) {
    Write-Error 'This script must be run with powershell -File (e.g. from just).'
    exit 1
}
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
. (Join-Path $PSScriptRoot 'ollama_list.ps1')
$envPath = Join-Path $root '.env'
$example = Join-Path $root '.env.local.example'
if (Test-Path $envPath) {
    Write-Output '  .env already exists — skipping.'
    exit 0
}
$content = Get-Content -Path $example -Raw -Encoding UTF8
$localLlmModels = [ordered]@{}
$defaultLocalLlmModel = ''

try {
    $detectedModels = @(Get-OllamaModels)
    if ($detectedModels.Count -gt 0) {
        $defaultLocalLlmModel = $detectedModels[0]
        foreach ($model in $detectedModels) {
            $localLlmModels[$model] = $model
        }
        Write-Output "  Auto-set LOCAL_LLM_MODELS and LOCAL_LLM_MODEL from $($detectedModels.Count) installed Ollama model(s)."
    } else {
        Write-Warning 'No Ollama models were detected; LOCAL_LLM_MODELS will be empty.'
    }
} catch {
    Write-Warning 'Ollama model detection failed; LOCAL_LLM_MODELS will be empty.'
}

$localLlmModelsJson = $localLlmModels | ConvertTo-Json -Compress
$content = [regex]::Replace(
    $content,
    '(?m)^LOCAL_LLM_MODELS=.*$',
    "LOCAL_LLM_MODELS=$localLlmModelsJson"
)
$content = [regex]::Replace(
    $content,
    '(?m)^LOCAL_LLM_MODEL=.*$',
    "LOCAL_LLM_MODEL=$defaultLocalLlmModel"
)
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$jwtBytes = New-Object byte[] 48
$encBytes = New-Object byte[] 32
$rng.GetBytes($jwtBytes)
$rng.GetBytes($encBytes)
$jwt = [Convert]::ToBase64String($jwtBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
$enc = [Convert]::ToBase64String($encBytes).Replace('+', '-').Replace('/', '_')
$content = $content.Replace('REPLACE_WITH_STRONG_SECRET_AT_LEAST_32_CHARS', $jwt)
$content = $content.Replace('REPLACE_WITH_FERNET_KEY', $enc)
[System.IO.File]::WriteAllText($envPath, $content, [System.Text.UTF8Encoding]::new($false))
Write-Output '  .env created with auto-generated secrets.'
