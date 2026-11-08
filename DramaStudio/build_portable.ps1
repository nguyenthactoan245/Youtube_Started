$ErrorActionPreference = "Stop"

$projectDir = $PSScriptRoot
$entryPoint = Join-Path $projectDir "phimora_ui.py"
$htmlFile = Join-Path $projectDir "studio_ui.html"
$distDir = Join-Path $projectDir "dist"
$buildDir = Join-Path $projectDir "build"
$specDir = Join-Path $projectDir "packaging"

foreach ($requiredFile in @($entryPoint, $htmlFile)) {
    if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
        throw "Missing required file: $requiredFile"
    }
}

$ffmpeg = (Get-Command ffmpeg.exe -ErrorAction Stop).Source
$ffprobe = (Get-Command ffprobe.exe -ErrorAction Stop).Source

Push-Location $projectDir
try {
    & py -3.14 -m PyInstaller `
        --noconfirm `
        --clean `
        --onefile `
        --windowed `
        --name "DramaStudio" `
        --distpath $distDir `
        --workpath $buildDir `
        --specpath $specDir `
        --add-data "$htmlFile;." `
        --add-binary "$ffmpeg;." `
        --add-binary "$ffprobe;." `
        --collect-all webview `
        $entryPoint

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
