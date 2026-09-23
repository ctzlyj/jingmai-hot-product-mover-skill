param(
  [string]$Root = $env:NO_INVENTORY_ROOT,
  [string]$Queue = "",
  [string]$Out = ""
)
$ErrorActionPreference = "Continue"
if (-not $Root) { throw "Missing -Root (batch root directory) or NO_INVENTORY_ROOT" }
if (-not $Queue) { $Queue = Join-Path $Root "reports\ocr-box-queue-final.tsv" }
if (-not $Out) { $Out = Join-Path $Root "reports\ocr-boxes-final.ndjson" }
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq "AsTask" -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq "IAsyncOperation``1" })[0]
function Await($op, $t) { $m = $asTaskGeneric.MakeGenericMethod($t); $task = $m.Invoke($null, @($op)); $task.Wait(-1) | Out-Null; $task.Result }
[void][Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
[void][Windows.Graphics.Imaging.BitmapDecoder,Windows.Foundation,ContentType=WindowsRuntime]
[void][Windows.Storage.StorageFile,Windows.Foundation,ContentType=WindowsRuntime]
[void][Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]
$lang = New-Object Windows.Globalization.Language "zh-Hans-CN"
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($lang)
$ROOT = $Root
$queue = $Queue
$out = $Out
$sw = New-Object System.IO.StreamWriter($out, $false, (New-Object System.Text.UTF8Encoding($false)))
foreach ($line in [System.IO.File]::ReadAllLines($queue, [System.Text.Encoding]::UTF8)) {
  if (-not $line.Trim()) { continue }
  $parts = $line.Split("`t"); $md5 = $parts[0]; $path = $parts[1]
  $rec = [ordered]@{ md5 = $md5; path = $path; ok = $false; width = 0; height = 0; lines = @() }
  try {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync(0)) ([Windows.Storage.Streams.IRandomAccessStream])
    $dec = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $rec.width = [int]$dec.PixelWidth; $rec.height = [int]$dec.PixelHeight
    $bmp = Await ($dec.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $res = Await ($engine.RecognizeAsync($bmp)) ([Windows.Media.Ocr.OcrResult])
    $ls = @()
    foreach ($ln in $res.Lines) {
      $x = $null; $y = $null; $x2 = 0; $y2 = 0; $t = ""
      foreach ($wd in $ln.Words) {
        $r = $wd.BoundingRect
        if ($x -eq $null -or $r.X -lt $x) { $x = [int]$r.X }
        if ($y -eq $null -or $r.Y -lt $y) { $y = [int]$r.Y }
        $xe = [int]($r.X + $r.Width); $ye = [int]($r.Y + $r.Height)
        if ($xe -gt $x2) { $x2 = $xe }
        if ($ye -gt $y2) { $y2 = $ye }
        $t = $t + $wd.Text
      }
      if ($x -eq $null) { continue }
      $ls += [ordered]@{ text = $t; x = $x; y = $y; w = ($x2 - $x); h = ($y2 - $y) }
    }
    $rec.lines = $ls
    $rec.ok = $true
    $bmp.Dispose(); $stream.Dispose()
  } catch { $rec.lines = @() }
  $sw.WriteLine(($rec | ConvertTo-Json -Compress -Depth 5))
  $sw.Flush()
}
$sw.Close()
Write-Output "BOXES_DONE"
