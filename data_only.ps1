# ==============================================================================
# ANSYS CFD-Post 独立批量后处理与数据提取脚本
# 增量提取：结果表中已有且 .res 未变化（修改时间、大小、叶片数、流量单位一致）的记录直接跳过；
# 需要重新提取时覆盖同名记录，重复运行不会产生重复行。使用 -Force 强制重新提取全部 .res。
# ==============================================================================

param(
    [double]$BladeCount = 1.0,
    [string]$MassFlowUnit = "kg/s",
    [string]$CsvFile = "Extracted_Compressor_Data.csv",
    [switch]$Force
)

$ProgressPreference = "SilentlyContinue"

if ($BladeCount -le 0) {
    throw "BladeCount 必须大于 0。"
}

$MassFlowUnit = $MassFlowUnit.Trim()
$normalizedMassFlowUnit = $MassFlowUnit.ToLowerInvariant()
if ($normalizedMassFlowUnit -notin @("kg/s", "g/s")) {
    throw "MassFlowUnit 只能是 kg/s 或 g/s。"
}
$massFlowToKgFactor = if ($normalizedMassFlowUnit -eq "g/s") { 0.001 } else { 1.0 }

$cseFile = "__data_only_extract.cse"
$tempCsv = "__data_only_extract.csv"
$csvHeader = "Result_File,Mass_Flow_kg_s,Static_PR,P_in_Pa,P_out_Pa,T_in_K,T_out_K,Isentropic_Efficiency,Blade_Count,Mass_Flow_Unit,Result_LastWriteUtcTicks,Result_Size_Bytes"
$invariant = [System.Globalization.CultureInfo]::InvariantCulture

function Get-FieldText {
    param(
        [object]$Record,
        [string]$Name
    )

    if ($null -eq $Record -or -not ($Record.PSObject.Properties.Name -contains $Name)) {
        return ""
    }

    return ([string]$Record.$Name).Trim()
}

function Format-Number {
    param(
        [string]$Text,
        [string]$Format
    )

    $number = 0.0
    if ([double]::TryParse($Text, [System.Globalization.NumberStyles]::Float, $invariant, [ref]$number)) {
        return $number.ToString($Format, $invariant)
    }

    return $Text
}

function Test-RecordCurrent {
    param(
        [object]$Record,
        [System.IO.FileInfo]$File
    )

    if ($null -eq $Record) {
        return $false
    }

    $recordBladeCount = 0.0
    if (-not [double]::TryParse((Get-FieldText $Record "Blade_Count"), [System.Globalization.NumberStyles]::Float, $invariant, [ref]$recordBladeCount)) {
        return $false
    }

    return ([math]::Abs($recordBladeCount - $BladeCount) -le 1e-9) -and
        ((Get-FieldText $Record "Mass_Flow_Unit") -eq $MassFlowUnit) -and
        ((Get-FieldText $Record "Result_LastWriteUtcTicks") -eq [string]$File.LastWriteTimeUtc.Ticks) -and
        ((Get-FieldText $Record "Result_Size_Bytes") -eq [string]$File.Length)
}

function Save-ExtractedCsv {
    param(
        [System.Collections.Specialized.OrderedDictionary]$Records,
        [string]$Path
    )

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add($csvHeader)
    foreach ($record in $Records.Values) {
        $fields = @(
            (Get-FieldText $record "Result_File"),
            (Format-Number (Get-FieldText $record "Mass_Flow_kg_s") "0.000000"),
            (Format-Number (Get-FieldText $record "Static_PR") "0.0000"),
            (Format-Number (Get-FieldText $record "P_in_Pa") "0.0000"),
            (Format-Number (Get-FieldText $record "P_out_Pa") "0.0000"),
            (Format-Number (Get-FieldText $record "T_in_K") "0.0000"),
            (Format-Number (Get-FieldText $record "T_out_K") "0.0000"),
            (Format-Number (Get-FieldText $record "Isentropic_Efficiency") "0.000000"),
            (Format-Number (Get-FieldText $record "Blade_Count") "0.######"),
            (Get-FieldText $record "Mass_Flow_Unit"),
            (Get-FieldText $record "Result_LastWriteUtcTicks"),
            (Get-FieldText $record "Result_Size_Bytes")
        )
        $lines.Add($fields -join ",")
    }

    $tempPath = "$Path.tmp"
    $lines | Set-Content -LiteralPath $tempPath -Encoding UTF8
    Move-Item -LiteralPath $tempPath -Destination $Path -Force
}

# 查找当前目录下所有的 .res 文件
$resFiles = @(Get-ChildItem -Filter "*.res" -File | Sort-Object Name)

if ($resFiles.Count -eq 0) {
    Write-Host "当前目录下未找到任何 .res 文件！" -ForegroundColor Red
    exit
}

# 读取已有结果，以 Result_File 去重（同名保留最后一条）
$records = [ordered]@{}
$duplicateCount = 0
if (Test-Path -LiteralPath $CsvFile) {
    foreach ($row in @(Import-Csv -LiteralPath $CsvFile)) {
        $name = Get-FieldText $row "Result_File"
        if (-not $name) {
            continue
        }
        if ($records.Contains($name)) {
            $duplicateCount++
        }
        $records[$name] = $row
    }
}

Write-Host "找到 $($resFiles.Count) 个结果文件，已有记录 $($records.Count) 条，准备开始提取..." -ForegroundColor Cyan
if ($Force) {
    Write-Host "已指定 -Force，将重新提取全部结果文件。" -ForegroundColor Yellow
}

$extractedCount = 0
$skippedCount = 0
$failedFiles = @()

foreach ($file in $resFiles) {
    $resFileName = $file.Name

    if (-not $Force -and (Test-RecordCurrent -Record $records[$resFileName] -File $file)) {
        $skippedCount++
        Write-Host "跳过（已提取且未变化）: $resFileName" -ForegroundColor DarkGray
        continue
    }

    Write-Host "正在提取: $resFileName ..." -ForegroundColor Yellow
    if (Test-Path -LiteralPath $tempCsv) {
        Remove-Item -LiteralPath $tempCsv -Force
    }

    # ------------------------------------------------------------------
    # 动态生成 CFD-Post 提取宏（每次只写单条结果到临时 CSV）
    # ------------------------------------------------------------------
    $cseContent = @"
! `$outFile = "$tempCsv";
! `$currentRes = "$resFileName";
! open(MYCSV, ">", `$outFile) or die "cannot open `$outFile\n";
! print MYCSV "Result_File,Mass_Flow_kg_s,Static_PR,P_in_Pa,P_out_Pa,T_in_K,T_out_K,Isentropic_Efficiency,Blade_Count,Mass_Flow_Unit\n";

# 安全的浮点数提取函数
! sub get_num {
!     my `$val = shift;
!     if (`$val =~ /^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)/) {
!         return `$1;
!     }
!     return 0;
! }

# 1. 提取基础气动参数
! `$massFlow = get_num(evaluate("-massFlow()\@R1 Outlet")) * $massFlowToKgFactor;
! `$p_in    = get_num(evaluate("massFlowAve(Pressure)\@R1 Inlet"));
! `$p_out   = get_num(evaluate("massFlowAve(Pressure)\@R1 Outlet"));
! `$PR      = (`$p_in == 0) ? 0 : (`$p_out / `$p_in);
! `$t_in    = get_num(evaluate("massFlowAve(Temperature)\@R1 Inlet"));
! `$t_out   = get_num(evaluate("massFlowAve(Temperature)\@R1 Outlet"));

# 2. 提取等熵压缩效率
! `$is_eff  = get_num(evaluate("massFlowAve(Isentropic Compression Efficiency)\@R1 Outlet"));

# 写入 CSV（整机流量 = 单流道流量 × 叶片数）
! printf MYCSV ("%s,%.6f,%.4f,%.4f,%.4f,%.4f,%.4f,%.6f,%.6f,%s\n", `$currentRes, `$massFlow * $BladeCount, `$PR, `$p_in, `$p_out, `$t_in, `$t_out, `$is_eff, $BladeCount, "$MassFlowUnit");
! close(MYCSV);
> quit
"@

    # 写入临时的 CSE 宏文件
    $cseContent | Out-File -FilePath $cseFile -Encoding ASCII

    # 调用 CFD-Post 后台执行宏
    $postCmd = "cfdpost -batch $cseFile -res `"$resFileName`""
    Invoke-Expression $postCmd | Out-Null

    $record = $null
    if (Test-Path -LiteralPath $tempCsv) {
        $record = Import-Csv -LiteralPath $tempCsv | Select-Object -First 1
        Remove-Item -LiteralPath $tempCsv -ErrorAction SilentlyContinue
    }

    if ($null -eq $record) {
        $failedFiles += $resFileName
        Write-Host "  -> 提取失败，CFD-Post 未生成结果: $resFileName" -ForegroundColor Red
        continue
    }

    $record | Add-Member -NotePropertyName Result_LastWriteUtcTicks -NotePropertyValue ([string]$file.LastWriteTimeUtc.Ticks) -Force
    $record | Add-Member -NotePropertyName Result_Size_Bytes -NotePropertyValue ([string]$file.Length) -Force
    $records[$resFileName] = $record
    $extractedCount++

    # 每提取一个就落盘，中途中断也不会丢失已完成的结果
    Save-ExtractedCsv -Records $records -Path $CsvFile
}

if ($duplicateCount -gt 0 -and $extractedCount -eq 0) {
    Save-ExtractedCsv -Records $records -Path $CsvFile
}

# 清理临时文件
foreach ($tempFile in @($cseFile, $tempCsv)) {
    if (Test-Path -LiteralPath $tempFile) {
        Remove-Item -LiteralPath $tempFile -ErrorAction SilentlyContinue
    }
}

Write-Host "`n=====================================================" -ForegroundColor Cyan
Write-Host "提取完成：新提取 $extractedCount 个，跳过 $skippedCount 个，失败 $($failedFiles.Count) 个。结果已保存至: $CsvFile" -ForegroundColor Cyan
if ($duplicateCount -gt 0) {
    Write-Host "已清理旧结果表中的 $duplicateCount 条重复记录。" -ForegroundColor Cyan
}
Write-Host "=====================================================" -ForegroundColor Cyan

if ($failedFiles.Count -gt 0) {
    Write-Host ("提取失败的结果文件: " + ($failedFiles -join ", ")) -ForegroundColor Red
    exit 1
}
