$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$entryPoint = Join-Path $projectDir "phimora_ui.py"
$htmlFile = Join-Path $projectDir "studio_ui.html"
$iconFile = Join-Path $projectDir "assets/dramastudio-icon-v2.ico"
$iconPng = Join-Path $projectDir "assets/dramastudio-icon-v2.png"
$distDir = Join-Path $projectDir "dist"
$buildDir = Join-Path $projectDir "build"
$specDir = Join-Path $projectDir "packaging"

foreach ($requiredFile in @($entryPoint, $htmlFile, $iconFile, $iconPng)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Missing required file: $requiredFile"
    }
}

$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
$ffprobe = (Get-Command ffprobe.exe -ErrorAction Stop).Source
$cudaRuntime = Join-Path $projectDir "cuda-runtime"
$cudaArgs = @()
foreach ($cudaDll in @("cublas64_12.dll", "cublasLt64_12.dll")) {
    $cudaPath = Join-Path $cudaRuntime $cudaDll
    if (Test-Path -LiteralPath $cudaPath -PathType Leaf) {
        $cudaArgs += @("--add-binary", "$cudaPath;cuda-runtime")
    }
}

Push-Location $projectDir
try {
    & py -3.12 -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "DramaStudio" `
        --icon $iconFile `
        --distpath $distDir `
        --workpath $buildDir `
        --specpath $specDir `
        --add-data "$htmlFile;." `
        --add-data "$iconFile;assets" `
        --add-data "$iconPng;assets" `
        --add-binary "$ffmpeg;." `
        --add-binary "$ffprobe;." `
        --collect-all webview `
        --collect-all ctranslate2 `
        --collect-all sentencepiece `
        --collect-all faster_whisper `
        --collect-all av `
        @cudaArgs `
        $entryPoint

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }

    $portableExe = Join-Path $distDir "DramaStudio.exe"
    if (-not (Test-Path -LiteralPath $portableExe -PathType Leaf)) {
        throw "Build completed without producing $portableExe"
    }

    $sizeMb = [math]::Round((Get-Item -LiteralPath $portableExe).Length / 1MB, 1)
    Write-Host "Portable build created: $portableExe ($sizeMb MB)"
}
finally {
    Pop-Location
}
