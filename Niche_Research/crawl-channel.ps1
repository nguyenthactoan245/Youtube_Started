param(
    [string]$Channel = '@InfinitePlanet4K',
    [ValidateRange(0, 1000000)][int]$Limit = 0
)
$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'tools\yt-dlp.exe'
if (!(Test-Path -LiteralPath $exe)) { throw 'Missing tools/yt-dlp.exe. See README.md.' }
if ($Channel -match '^@?([A-Za-z0-9_.-]+)$') {
    $url = 'https://www.youtube.com/@' + $Matches[1]
} elseif ($Channel -match '^https://(www\.)?youtube\.com/(\@[^/?#]+|channel/[^/?#]+)/?$') {
    $url = $Channel.TrimEnd('/')
} else { throw 'Use a channel handle (@name) or a YouTube channel URL.' }
$name = ($url.Split('/')[-1] -replace '[^A-Za-z0-9_.-]', '_').Trim('_')
$run = Join-Path $PSScriptRoot ($name + '\' + (Get-Date -Format 'yyyyMMdd_HHmmss_fff'))
$metadata = Join-Path $run 'metadata'
$thumbs = Join-Path $run 'thumbnails'
New-Item -ItemType Directory -Force -Path $metadata,$thumbs | Out-Null
$started = [DateTime]::UtcNow.ToString('o')
$cli = @('--ignore-config','--skip-download','--write-info-json','--write-thumbnail',
    '--ignore-errors','--ignore-no-formats-error','--no-progress','--windows-filenames',
    '--socket-timeout','30','--retries','3','--extractor-retries','3',
    '--sleep-requests','1','--encoding','utf-8',
    '-o',(Join-Path $metadata '%(id)s.%(ext)s'),
    '-o',('thumbnail:' + (Join-Path $thumbs '%(id)s.%(ext)s')))
if ($Limit -gt 0) { $cli += @('--playlist-end',"$Limit") }
$cli += $url
# Native warnings on stderr are logged; they must not terminate Windows PowerShell.
$ErrorActionPreference = 'Continue'
& $exe @cli 2>&1 | Tee-Object -FilePath (Join-Path $run 'crawl.log') | ForEach-Object { Write-Host $_ }
$crawlExit = $LASTEXITCODE
$ErrorActionPreference = 'Stop'
$rows = @(Get-ChildItem -LiteralPath $metadata -Filter '*.info.json' | ForEach-Object {
    $v = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($v.id -and $v._type -ne 'playlist' -and $v.id -match '^[A-Za-z0-9_-]{11}$') {
        $published = $null
        $precision = 'unavailable'
        if ($null -ne $v.timestamp) {
            $published = [DateTimeOffset]::FromUnixTimeSeconds([long]$v.timestamp).UtcDateTime.ToString('o')
            $precision = 'timestamp_utc'
        } elseif ($v.upload_date -match '^\d{8}$') {
            $published = [DateTime]::ParseExact($v.upload_date,'yyyyMMdd',[Globalization.CultureInfo]::InvariantCulture).ToString('yyyy-MM-dd')
            $precision = 'date_only'
        }
        $file = Get-ChildItem -LiteralPath $thumbs -File | Where-Object { $_.BaseName -eq $v.id } | Select-Object -First 1
        [PSCustomObject]@{
            video_id = $v.id; title = $v.title; view_count = $v.view_count
            created_time = $published; created_time_precision = $precision
            upload_date = $v.upload_date; video_url = $v.webpage_url
            thumbnail_url = $v.thumbnail
            thumbnail_file = $(if ($file) { 'thumbnails/' + $file.Name } else { $null })
            channel = $v.channel; collected_at_utc = $_.LastWriteTimeUtc.ToString('o')
        }
    }
} | Sort-Object video_id -Unique)
if ($rows.Count -gt 0) {
    $rows | Export-Csv -LiteralPath (Join-Path $run 'videos.csv') -NoTypeInformation -Encoding UTF8
    ConvertTo-Json -InputObject $rows -Depth 8 | Set-Content -LiteralPath (Join-Path $run 'videos.json') -Encoding UTF8
}
$summary = [ordered]@{
    channel_url = $url; started_at_utc = $started; finished_at_utc = [DateTime]::UtcNow.ToString('o')
    video_count = $rows.Count; missing_thumbnails = @($rows | Where-Object { !$_.thumbnail_file }).Count
    missing_created_time = @($rows | Where-Object { !$_.created_time }).Count
    extractor_exit_code = $crawlExit; limit_per_playlist = $Limit
    status = $(if ($rows.Count -eq 0) { 'failed' } elseif ($crawlExit -ne 0) { 'partial_check_log' } else { 'finished_check_log_for_skipped_videos' })
}
$summary | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $run 'summary.json') -Encoding UTF8
Write-Host "Saved $($rows.Count) videos to $run"
if ($rows.Count -eq 0) { throw "No video metadata collected. Read $run\crawl.log" }
