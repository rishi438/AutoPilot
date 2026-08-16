# Fetch locally installed Ollama model names.
$ErrorActionPreference = 'Stop'

function Get-OllamaModels {
    [OutputType([string[]])]
    param()

    if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
        throw 'Ollama was not found on PATH.'
    }

    $ollamaModels = @(& ollama list)
    if ($LASTEXITCODE -ne 0) {
        throw 'Ollama model listing failed.'
    }

    return @($ollamaModels | Select-Object -Skip 1 | ForEach-Object {
        $line = $_.Trim()
        if ($line) { ($line -split '\s+')[0] }
    })
}

if ($MyInvocation.InvocationName -ne '.') {
    Get-OllamaModels
}
