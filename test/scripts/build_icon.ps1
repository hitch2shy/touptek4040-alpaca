param(
    [Parameter(Mandatory = $true)]
    [string]$Source,
    [Parameter(Mandatory = $true)]
    [string]$Output
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Add-Type -AssemblyName System.Drawing

$sourcePath = [System.IO.Path]::GetFullPath($Source)
$outputPath = [System.IO.Path]::GetFullPath($Output)
if (-not [System.IO.File]::Exists($sourcePath)) {
    throw "Icon source does not exist: $sourcePath"
}
if ([System.IO.Path]::GetExtension($outputPath).ToLowerInvariant() -ne ".ico") {
    throw "Icon output must use the .ico suffix: $outputPath"
}

$outputDirectory = [System.IO.Path]::GetDirectoryName($outputPath)
[System.IO.Directory]::CreateDirectory($outputDirectory) | Out-Null
$temporaryPath = [System.IO.Path]::Combine(
    $outputDirectory,
    ".$([System.IO.Path]::GetFileName($outputPath)).$([System.Guid]::NewGuid().ToString('N')).tmp"
)
$sizes = @(16, 24, 32, 48, 64, 128, 256)
$sourceImage = $null
$payloads = [System.Collections.Generic.List[byte[]]]::new()

try {
    $sourceImage = [System.Drawing.Image]::FromFile($sourcePath)
    if ($sourceImage.Width -ne $sourceImage.Height) {
        throw "Icon source must be square: $($sourceImage.Width) x $($sourceImage.Height)"
    }

    foreach ($size in $sizes) {
        $bitmap = [System.Drawing.Bitmap]::new(
            $size,
            $size,
            [System.Drawing.Imaging.PixelFormat]::Format32bppArgb
        )
        $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
        $stream = [System.IO.MemoryStream]::new()
        try {
            $graphics.Clear([System.Drawing.Color]::Transparent)
            $graphics.CompositingMode = [System.Drawing.Drawing2D.CompositingMode]::SourceCopy
            $graphics.CompositingQuality = [System.Drawing.Drawing2D.CompositingQuality]::HighQuality
            $graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
            $graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
            $graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
            $graphics.DrawImage(
                $sourceImage,
                [System.Drawing.Rectangle]::new(0, 0, $size, $size)
            )
            $bitmap.Save($stream, [System.Drawing.Imaging.ImageFormat]::Png)
            $payloads.Add($stream.ToArray())
        }
        finally {
            $stream.Dispose()
            $graphics.Dispose()
            $bitmap.Dispose()
        }
    }

    $fileStream = [System.IO.File]::Open(
        $temporaryPath,
        [System.IO.FileMode]::CreateNew,
        [System.IO.FileAccess]::Write,
        [System.IO.FileShare]::None
    )
    $writer = [System.IO.BinaryWriter]::new($fileStream)
    try {
        $writer.Write([uint16]0)
        $writer.Write([uint16]1)
        $writer.Write([uint16]$sizes.Count)

        $payloadOffset = 6 + (16 * $sizes.Count)
        for ($index = 0; $index -lt $sizes.Count; $index++) {
            $size = $sizes[$index]
            $encodedSize = if ($size -eq 256) { [byte]0 } else { [byte]$size }
            $payload = $payloads[$index]
            $writer.Write($encodedSize)
            $writer.Write($encodedSize)
            $writer.Write([byte]0)
            $writer.Write([byte]0)
            $writer.Write([uint16]1)
            $writer.Write([uint16]32)
            $writer.Write([uint32]$payload.Length)
            $writer.Write([uint32]$payloadOffset)
            $payloadOffset += $payload.Length
        }
        foreach ($payload in $payloads) {
            $writer.Write($payload)
        }
        $writer.Flush()
        $fileStream.Flush($true)
    }
    finally {
        $writer.Dispose()
        $fileStream.Dispose()
    }

    if ([System.IO.File]::Exists($outputPath)) {
        [System.IO.File]::Replace($temporaryPath, $outputPath, $null)
    }
    else {
        [System.IO.File]::Move($temporaryPath, $outputPath)
    }
}
finally {
    if ($null -ne $sourceImage) {
        $sourceImage.Dispose()
    }
    if ([System.IO.File]::Exists($temporaryPath)) {
        [System.IO.File]::Delete($temporaryPath)
    }
}

Write-Output "Wrote $outputPath with sizes: $($sizes -join ', ')"
