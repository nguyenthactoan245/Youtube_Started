param([switch]$SkipInstaller)

$ErrorActionPreference = 'Stop'

$projectDir = $PSScriptRoot
$innoCompiler = Join-Path $projectDir '..\Stock\.build-tools\inno\ISCC.exe'
$iconFile = Join-Path $projectDir 'assets\dramastudio-icon-v2.ico'
$iconPng = Join-Path $projectDir 'assets\dramastudio-icon-v2.png'
$htmlFile = Join-Path $projectDir 'studio_ui.html'
$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
$cudaRuntime = Join-Path $projectDir 'cuda-runtime'
$cudaDlls = @('cublas64_12.dll', 'cublasLt64_12.dll')
$cudaArgs = @()
foreach ($dll in $cudaDlls) {
    $dllPath = Join-Path $cudaRuntime $dll
    if (-not (Test-Path -LiteralPath $dllPath -PathType Leaf)) {
        throw "Missing CUDA runtime DLL: $dllPath"
    }
    $cudaArgs += @('--add-binary', "$dllPath;cuda-runtime")
}

foreach ($requiredFile in @($innoCompiler, $iconFile, $iconPng, $htmlFile)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Missing required file: $requiredFile"
    }
}

Push-Location $projectDir
try {
    & py -3.12 -m PyInstaller `
        --noconfirm --clean --onedir --windowed `
        --name 'DramaStudio' `
        --icon $iconFile `
        --distpath (Join-Path $projectDir 'build\installer-app') `
        --workpath (Join-Path $projectDir 'build\installer-work') `
        --specpath (Join-Path $projectDir 'packaging') `
        --add-data "$htmlFile;." `
        --add-data "$iconFile;assets" `
        --add-data "$iconPng;assets" `
        --add-binary "$ffmpeg;." `
        --collect-all webview `
        --collect-all ctranslate2 `
        --collect-all sentencepiece `
        --collect-all faster_whisper `
        --collect-all av `
        @cudaArgs `
        (Join-Path $projectDir 'phimora_ui.py')
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    $appDir = Join-Path $projectDir 'build\installer-app\DramaStudio'
    $internalDir = Join-Path $appDir '_internal'
    $duplicateCublas = Join-Path $internalDir 'cublasLt64_12.dll'
    $bundledCublas = Join-Path $internalDir 'cuda-runtime\cublasLt64_12.dll'
    if ((Test-Path -LiteralPath $duplicateCublas -PathType Leaf) -and
        (Test-Path -LiteralPath $bundledCublas -PathType Leaf) -and
        (Get-FileHash -LiteralPath $duplicateCublas).Hash -eq (Get-FileHash -LiteralPath $bundledCublas).Hash) {
        Remove-Item -LiteralPath $duplicateCublas -Force
    }
    if (Test-Path -LiteralPath (Join-Path $appDir 'models')) {
        throw 'Installer app unexpectedly contains a models folder.'
    }
    if (-not $SkipInstaller) {
        & $innoCompiler (Join-Path $projectDir 'installer.iss')
        if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }

        Get-Item -LiteralPath (Join-Path $projectDir 'releases\DramaStudio-Setup-v1.0.0-GPU.exe') |
            Select-Object FullName, Length, LastWriteTime
    }
}
finally {
    Pop-Location
}
