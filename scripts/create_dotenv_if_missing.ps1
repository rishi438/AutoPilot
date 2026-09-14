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
$minioRootUserBytes = New-Object byte[] 18
$minioRootPasswordBytes = New-Object byte[] 48
$minioAccessKeyBytes = New-Object byte[] 18
$minioSecretKeyBytes = New-Object byte[] 48
$portalVaultRootUserBytes = New-Object byte[] 12
$portalVaultRootPasswordBytes = New-Object byte[] 48
$portalVaultAppUserBytes = New-Object byte[] 12
$portalVaultAppPasswordBytes = New-Object byte[] 48
$portalVaultEncryptionBytes = New-Object byte[] 32
$portalVaultPasswordRecoveryBytes = New-Object byte[] 32
$rng.GetBytes($minioRootUserBytes)
$rng.GetBytes($minioRootPasswordBytes)
$rng.GetBytes($minioAccessKeyBytes)
$rng.GetBytes($minioSecretKeyBytes)
$rng.GetBytes($portalVaultRootUserBytes)
$rng.GetBytes($portalVaultRootPasswordBytes)
$rng.GetBytes($portalVaultAppUserBytes)
$rng.GetBytes($portalVaultAppPasswordBytes)
$rng.GetBytes($portalVaultEncryptionBytes)
$rng.GetBytes($portalVaultPasswordRecoveryBytes)
$minioRootUser = [Convert]::ToBase64String($minioRootUserBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
$minioRootPassword = [Convert]::ToBase64String($minioRootPasswordBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
$minioAccessKey = [Convert]::ToBase64String($minioAccessKeyBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
$minioSecretKey = [Convert]::ToBase64String($minioSecretKeyBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
$portalVaultRootUser = 'vaultroot_' + [Convert]::ToBase64String($portalVaultRootUserBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
$portalVaultRootPassword = [Convert]::ToBase64String($portalVaultRootPasswordBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
$portalVaultAppUser = 'vaultapp_' + [Convert]::ToBase64String($portalVaultAppUserBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
$portalVaultAppPassword = [Convert]::ToBase64String($portalVaultAppPasswordBytes).Replace('+', '-').Replace('/', '_').TrimEnd('=')
$portalVaultEncryptionKey = [Convert]::ToBase64String($portalVaultEncryptionBytes).Replace('+', '-').Replace('/', '_')
$portalVaultPasswordRecoveryKey = [Convert]::ToBase64String($portalVaultPasswordRecoveryBytes).Replace('+', '-').Replace('/', '_')
$content = $content.Replace('REPLACE_WITH_STRONG_SECRET_AT_LEAST_32_CHARS', $jwt)
$content = $content.Replace('REPLACE_WITH_FERNET_KEY', $enc)
$content += "`nMINIO_ROOT_USER=$minioRootUser`nMINIO_ROOT_PASSWORD=$minioRootPassword`nMINIO_ACCESS_KEY=$minioAccessKey`nMINIO_SECRET_KEY=$minioSecretKey`n"
$content += "PORTAL_VAULT_ROOT_USERNAME=$portalVaultRootUser`nPORTAL_VAULT_ROOT_PASSWORD=$portalVaultRootPassword`nPORTAL_VAULT_APP_USERNAME=$portalVaultAppUser`nPORTAL_VAULT_APP_PASSWORD=$portalVaultAppPassword`nPORTAL_VAULT_MONGODB_DATABASE=autopilot_vault`nPORTAL_VAULT_ENCRYPTION_KEY=$portalVaultEncryptionKey`nPORTAL_VAULT_PASSWORD_RECOVERY_KEY=$portalVaultPasswordRecoveryKey`n"
[System.IO.File]::WriteAllText($envPath, $content, [System.Text.UTF8Encoding]::new($false))
Write-Output '  .env created with auto-generated secrets.'
