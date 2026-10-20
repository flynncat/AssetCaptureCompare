param([Parameter(Mandatory=$true)][string]$Destination)
$ErrorActionPreference = 'Stop'
$pluginRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$destinationPath = [IO.Path]::GetFullPath($Destination)
if (Test-Path $destinationPath) { throw 'Destination must not exist; an existing package will not be overwritten.' }
if (-not $destinationPath.EndsWith('.zip', [StringComparison]::OrdinalIgnoreCase)) { throw 'Destination must be a .zip file' }
if (-not (Test-Path (Join-Path $pluginRoot 'Binaries/Win64/UE4Editor-AssetCaptureCompareNative.dll'))) {
    throw 'Build the native module with Scripts/build_native.ps1 first'
}
& (Join-Path $pluginRoot '.runtime/python.exe') (Join-Path $PSScriptRoot 'verify_runtime.py')
if ($LASTEXITCODE -ne 0) { throw 'Install a valid portable runtime before packaging' }
$parent = Split-Path $destinationPath -Parent
New-Item -ItemType Directory -Force $parent | Out-Null
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::Open($destinationPath, [IO.Compression.ZipArchiveMode]::Create)
try {
    foreach ($name in @('AssetCaptureCompare.uplugin', 'README.md', 'Content', 'Source', 'Binaries', 'Scripts', 'Web', 'Docs', 'Tools', 'Tests', '.runtime')) {
        $source = Join-Path $pluginRoot $name
        $files = if ((Get-Item -LiteralPath $source -Force).PSIsContainer) {
            Get-ChildItem -LiteralPath $source -Recurse -File -Force
        } else { Get-Item -LiteralPath $source -Force }
        foreach ($file in $files) {
            $relative = $file.FullName.Substring($pluginRoot.Length + 1).Replace('\', '/')
            if ($relative -match '(^|/)(__pycache__|ACCWork|artifacts)(/|$)|\.(pyc|pdb|lib|exp)$') { continue }
            [IO.Compression.ZipFileExtensions]::CreateEntryFromFile($archive, $file.FullName,
                ('AssetCaptureCompare/' + $relative), [IO.Compression.CompressionLevel]::Optimal) | Out-Null
        }
    }
} finally { $archive.Dispose() }
Write-Output "Portable plugin package created: $destinationPath"
