param(
    [int]$Count = 20,
    [double]$Hz = 1,
    [string]$Sequences,
    [ValidateSet('lora', 'wifi', '4g')]
    [string[]]$Omit = @()
)

$ErrorActionPreference = 'Stop'
$arguments = @('fusion.py', 'send', 'windows.json', '--count', "$Count", '--hz', $Hz.ToString([cultureinfo]::InvariantCulture))
if ($Sequences) { $arguments += @('--sequences', $Sequences) }
foreach ($link in $Omit) { $arguments += @('--omit', $link) }

Push-Location -LiteralPath $PSScriptRoot
try {
    python @arguments
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
