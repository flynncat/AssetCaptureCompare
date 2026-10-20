param([Parameter(Mandatory=$true)][string]$Engine, [Parameter(Mandatory=$true)][string]$Project)
$ErrorActionPreference = "Stop"
$pluginRoot = Split-Path $PSScriptRoot -Parent
$targetName = [IO.Path]::GetFileNameWithoutExtension($Project) + 'Editor'
& (Join-Path $Engine 'Engine\Build\BatchFiles\Build.bat') $targetName Win64 Development "-Project=$Project" '-Module=AssetCaptureCompareNative' -NoHotReloadFromIDE -WaitMutex
if ($LASTEXITCODE -ne 0) { throw 'Native capture module compilation failed' }
$engineManifest = Get-Content (Join-Path $Engine 'Engine\Binaries\Win64\UE4Editor.modules') -Raw | ConvertFrom-Json
$manifest = @{ BuildId = $engineManifest.BuildId; Modules = @{ AssetCaptureCompareNative = 'UE4Editor-AssetCaptureCompareNative.dll' } }
$manifest | ConvertTo-Json | Set-Content (Join-Path $pluginRoot 'Binaries\Win64\UE4Editor.modules') -Encoding ascii
