param(
    [string]$Background = "assets/social-preview-background.png",
    [string]$Output = "assets/lumen-marketing-os-social-preview.png"
)

Add-Type -AssemblyName System.Drawing

$root = Split-Path -Parent $PSScriptRoot
$backgroundPath = Join-Path $root $Background
$outputPath = Join-Path $root $Output
$source = [System.Drawing.Image]::FromFile($backgroundPath)
$canvas = New-Object System.Drawing.Bitmap 1280, 640
$graphics = [System.Drawing.Graphics]::FromImage($canvas)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
$graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$graphics.DrawImage($source, 0, 0, 1280, 640)

$overlay = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(150, 5, 9, 28))
$graphics.FillRectangle($overlay, 0, 0, 665, 640)

$labelFont = New-Object System.Drawing.Font("Segoe UI", 17, [System.Drawing.FontStyle]::Bold)
$titleFont = New-Object System.Drawing.Font("Segoe UI", 55, [System.Drawing.FontStyle]::Bold)
$subtitleFont = New-Object System.Drawing.Font("Segoe UI", 20, [System.Drawing.FontStyle]::Regular)
$white = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(247, 250, 255))
$blue = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(125, 211, 252))
$muted = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(191, 206, 228))

$graphics.DrawString("PRODUCTION-GRADE AI OPERATIONS", $labelFont, $blue, 66, 94)
$graphics.DrawString("LUMEN", $titleFont, $white, 60, 155)
$graphics.DrawString("MARKETING OS", $titleFont, $white, 60, 224)
$graphics.DrawString("Evidence-bound workflows. Durable state.", $subtitleFont, $muted, 66, 344)
$graphics.DrawString("Human-governed external actions.", $subtitleFont, $muted, 66, 380)

$linePen = New-Object System.Drawing.Pen ([System.Drawing.Color]::FromArgb(90, 125, 211, 252)), 2
$graphics.DrawLine($linePen, 66, 448, 550, 448)
$graphics.DrawString("9 DEPARTMENTS  ·  35 ROLES  ·  153 TESTS", $labelFont, $white, 66, 474)

$outputDirectory = Split-Path -Parent $outputPath
New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
$canvas.Save($outputPath, [System.Drawing.Imaging.ImageFormat]::Png)

$linePen.Dispose()
$muted.Dispose()
$blue.Dispose()
$white.Dispose()
$subtitleFont.Dispose()
$titleFont.Dispose()
$labelFont.Dispose()
$overlay.Dispose()
$graphics.Dispose()
$canvas.Dispose()
$source.Dispose()

Write-Output $outputPath
