param(
    [ValidatePattern('^[a-zA-Z0-9_.:/-]+$')][string]$Model = 'qwen3:14b',
    [switch]$NoDemo,
    [switch]$SetupOnly
)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($Model -match 'cloud') { throw 'Choose a local model, not a cloud model.' }
$python = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create .venv and install requirements as described in README.md first.' }
$runtime = Join-Path $PSScriptRoot '.runtime'
New-Item -ItemType Directory -Force -Path $runtime | Out-Null
$ollama = Join-Path $runtime 'ollama/ollama.exe'
if (-not (Test-Path -LiteralPath $ollama)) {
    $installed = Get-Command ollama -ErrorAction SilentlyContinue
    if ($installed) {
        $ollama = $installed.Source
    } else {
        Write-Host 'Downloading official portable Ollama (about 1.47 GB)...'
        $downloadDir = Join-Path $runtime 'downloads'
        New-Item -ItemType Directory -Force -Path $downloadDir | Out-Null
        $archive = Join-Path $downloadDir 'ollama-windows-amd64-v0.40.6.zip'
        $expectedHash = 'e359637575302f288d76c0007c408a4be9c7376d59c4f28684d63b232a44b427'
        if (-not (Test-Path -LiteralPath $archive) -or (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) {
            Invoke-WebRequest 'https://github.com/ollama/ollama/releases/download/v0.40.6/ollama-windows-amd64.zip' -OutFile $archive
        }
        if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedHash) { throw 'Ollama checksum mismatch.' }
        Expand-Archive -LiteralPath $archive -DestinationPath (Join-Path $runtime 'ollama') -Force
    }
}
$env:OLLAMA_HOST = '127.0.0.1:11434'
$env:OLLAMA_NO_CLOUD = '1'
$env:AGRO_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:AGRO_LLM_MODEL = $Model
$running = $false
try { $null = Invoke-RestMethod "$env:AGRO_OLLAMA_URL/api/version" -TimeoutSec 2; $running = $true } catch { }
if (-not $running) {
    $env:OLLAMA_MODELS = Join-Path $runtime 'models'
    $ollamaProcess = Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtime 'ollama.stdout.log') `
        -RedirectStandardError (Join-Path $runtime 'ollama.stderr.log')
    $ollamaProcess.Id | Set-Content (Join-Path $runtime 'ollama.pid')
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try { $null = Invoke-RestMethod "$env:AGRO_OLLAMA_URL/api/version" -TimeoutSec 2; $running = $true; break } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $running) { throw 'Ollama did not start. See .runtime/ollama.stderr.log.' }
}
$present = $false
try {
    $info = Invoke-RestMethod "$env:AGRO_OLLAMA_URL/api/show" -Method Post -ContentType 'application/json' -Body (@{model=$Model} | ConvertTo-Json)
    $present = $true
} catch { }
if (-not $present) {
    Write-Host "Downloading local model $Model (qwen3:14b is about 9.3 GB)..."
    & $ollama pull $Model
    if ($LASTEXITCODE -ne 0) { throw 'Model download failed. Run this script again to resume.' }
    $info = Invoke-RestMethod "$env:AGRO_OLLAMA_URL/api/show" -Method Post -ContentType 'application/json' -Body (@{model=$Model} | ConvertTo-Json)
}
if ($info.remote_model -or $info.remote_host -or 'tools' -notin $info.capabilities) { throw 'A local model with tool support is required.' }
Write-Host "Local model ready: $Model. No API key required."
if ($SetupOnly) { return }
Write-Host 'Open http://127.0.0.1:8000 and select the AI agent tab. Ctrl+C stops the web server.'
$serveArgs = @('-m', 'agroguard.cli', 'serve')
if (-not $NoDemo) { $serveArgs += '--demo' }
& $python @serveArgs
