# Rebuild map data using the same schema and CSE expressions as incremental extraction.
param(
    [string]$CsvFile = "Compressor_Map_Data.csv",
    [double]$BladeCount = 1.0,
    [string]$MassFlowUnit = "kg/s"
)

if ($BladeCount -le 0) { throw "BladeCount 必须大于 0。" }
if ($MassFlowUnit.Trim().ToLowerInvariant() -notin @('kg/s', 'g/s')) {
    throw "MassFlowUnit 只能是 kg/s 或 g/s。"
}
if (-not (Get-ChildItem -Filter '*.res' -File)) {
    throw "当前目录下未找到任何 .res 文件，无法执行离线提取。"
}
if (Test-Path -LiteralPath $CsvFile) {
    $backupPath = "$CsvFile.backup_$(Get-Date -Format 'yyyyMMdd_HHmmss')"
    Move-Item -LiteralPath $CsvFile -Destination $backupPath
    Write-Host "已备份旧数据表到: $backupPath" -ForegroundColor Yellow
}
& (Join-Path $PSScriptRoot 'data_only.ps1') -CsvFile $CsvFile -BladeCount $BladeCount -MassFlowUnit $MassFlowUnit -Force
