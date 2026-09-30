$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$Root = "C:\Users\Public\JSTBuild_V0525\JSTAutoPrint_V0.5.25_Windows_SourceRuntime"

function Convert-CodePoints {
    param([int[]]$Codes)
    return -join @($Codes | ForEach-Object { [char]$_ })
}

$Assistant = Convert-CodePoints @(0x805a, 0x6c34, 0x6f6d, 0x5b89, 0x5168, 0x6253, 0x5355, 0x52a9, 0x624b)
$Trial = "Win10_21H1_" + (Convert-CodePoints @(0x8bd5, 0x8fd0, 0x884c, 0x4e0d, 0x6253, 0x5370)) + ".bat"
$Offline = "Win10_" + (Convert-CodePoints @(0x79bb, 0x7ebf, 0x81ea, 0x68c0)) + ".bat"
$Manual = $Assistant + "_" + (Convert-CodePoints @(0x4f7f, 0x7528, 0x8bf4, 0x660e)) + ".md"
$Logic = $Assistant + "_V0.5.25_" + (Convert-CodePoints @(0x8fd0, 0x884c, 0x903b, 0x8f91, 0x4e0e, 0x6d41, 0x7a0b, 0x56fe)) + ".md"

Copy-Item -LiteralPath "C:\Users\Public\jst_trial.bat" -Destination (Join-Path $Root $Trial) -Force
Copy-Item -LiteralPath "C:\Users\Public\jst_offline.bat" -Destination (Join-Path $Root $Offline) -Force
Copy-Item -LiteralPath "C:\Users\Public\jst_manual.md" -Destination (Join-Path $Root $Manual) -Force
Copy-Item -LiteralPath "C:\Users\Public\jst_logic.md" -Destination (Join-Path $Root $Logic) -Force

$required = @($Trial, $Offline, $Manual, $Logic)
$result = foreach ($name in $required) {
    $path = Join-Path $Root $name
    $codes = -join @($name.ToCharArray() | ForEach-Object { "{0:x4}" -f [int]$_ })
    "{0}|{1}|{2}" -f (Test-Path -LiteralPath $path -PathType Leaf), $name, $codes
}
$result | Set-Content -LiteralPath "C:\Users\Public\jst_unicode_inputs_status.txt" -Encoding Ascii
