param([switch]$Offline)
$ErrorActionPreference = 'Stop'
$pluginRoot = Split-Path $PSScriptRoot -Parent
$runtime = Join-Path $pluginRoot '.runtime'
$lock = Get-Content (Join-Path $PSScriptRoot 'runtime-lock.json') -Raw | ConvertFrom-Json
if (Test-Path (Join-Path $runtime 'python.exe')) {
    & (Join-Path $runtime 'python.exe') (Join-Path $PSScriptRoot 'verify_runtime.py')
    if ($LASTEXITCODE -eq 0) { Write-Output 'Portable runtime already installed.'; exit 0 }
    throw 'Existing runtime is invalid or outdated. Close the viewer, move .runtime aside, then run setup again.'
}
if (Test-Path $runtime) { throw 'Move the old .runtime directory aside before installing the portable runtime.' }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$cache = Join-Path $pluginRoot '.downloads'
New-Item -ItemType Directory -Force $cache | Out-Null
$archive = Join-Path $cache $lock.python.filename
if ((Test-Path $archive) -and (Get-FileHash $archive -Algorithm SHA256).Hash.ToLowerInvariant() -ne $lock.python.sha256) {
    throw 'Cached Python archive checksum mismatch. Remove it before retrying.'
}
if (-not (Test-Path $archive)) {
    if ($Offline) { throw "Missing offline cache: $archive" }
    $partial = $archive + '.' + [Guid]::NewGuid().ToString('N') + '.partial'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $lock.python.url -OutFile $partial
        if ((Get-FileHash $partial -Algorithm SHA256).Hash.ToLowerInvariant() -ne $lock.python.sha256) {
            throw 'Python archive checksum mismatch'
        }
        Move-Item -LiteralPath $partial -Destination $archive
    } finally { if (Test-Path $partial) { Remove-Item -LiteralPath $partial } }
}
$staging = Join-Path $pluginRoot ('.runtime-install-' + [Guid]::NewGuid().ToString('N'))
if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($staging)) -ne [IO.Path]::GetFullPath($pluginRoot)) {
    throw 'Staging directory must stay inside the plugin'
}
try {
    Expand-Archive -LiteralPath $archive -DestinationPath $staging
    $pth = Join-Path $staging 'python313._pth'
    @('python313.zip', '.', 'Lib/site-packages', '../Content/Python', '../Scripts', '../Tools') | Set-Content -LiteralPath $pth -Encoding ascii
    $installArgs = @((Join-Path $PSScriptRoot 'install_runtime.py'))
    if ($Offline) { $installArgs += '--offline' }
    & (Join-Path $staging 'python.exe') @installArgs
    if ($LASTEXITCODE -ne 0) { throw 'Portable dependency installation failed' }
    & (Join-Path $staging 'python.exe') (Join-Path $PSScriptRoot 'verify_runtime.py')
    if ($LASTEXITCODE -ne 0) { throw 'Portable runtime verification failed' }
    Move-Item -LiteralPath $staging -Destination $runtime
} finally {
    if (Test-Path $staging) { Remove-Item -LiteralPath $staging -Recurse -Force }
}
Write-Output "Portable runtime installed: $runtime"
