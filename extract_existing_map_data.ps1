# ==============================================================================
# 从已有 .res 文件离线重建压气机特性图数据
# 不调用 cfx5solve，只调用 cfdpost 做后处理提取
# 输出字段:
#   Result_File,Mass_Flow_kg_s,Static_PR,P_in_Pa,P_out_Pa,T_in_K,T_out_K,Isentropic_Efficiency
# ==============================================================================

param(
    [string]$CsvFile = "Compressor_Map_Data.csv",
    [double]$BladeCount = 1.0
)

if ($BladeCount -le 0) {
    throw "BladeCount 必须大于 0。"
}

$cseFile = "__Batch_Extract_Map_Data.cse"

$resFiles = Get-ChildItem -Filter "*.res" -ErrorAction SilentlyContinue | Sort-Object Name

if ($resFiles.Count -eq 0) {
    Write-Host "当前目录下未找到任何 .res 文件，无法执行离线提取。" -ForegroundColor Red
    exit 1
}

if (Test-Path $CsvFile) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $backupPath = "$CsvFile.backup_$timestamp"
    Move-Item -Path $CsvFile -Destination $backupPath
    Write-Host "已备份旧数据表到: $backupPath" -ForegroundColor Yellow
}

"Result_File,Mass_Flow_kg_s,Static_PR,P_in_Pa,P_out_Pa,T_in_K,T_out_K,Isentropic_Efficiency,Blade_Count" | Out-File -FilePath $CsvFile -Encoding ASCII

Write-Host "找到 $($resFiles.Count) 个 .res 文件，开始离线提取..." -ForegroundColor Cyan

foreach ($file in $resFiles) {
    $resFileName = $file.Name
    Write-Host "  -> 正在提取: $resFileName" -ForegroundColor Yellow

    $cseContent = @"
! `$outFile = "$CsvFile";
! `$currentRes = "$resFileName";
! open(MYCSV, ">>", `$outFile) or die "无法打开文件\n";

! sub get_num {
!     my `$val = shift;
!     if (`$val =~ /^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)/) {
!         return `$1;
!     }
!     return 0;
! }

! sub get_mass_flow_kg_s {
!     my `$raw = shift // "";
!     my `$value = get_num(`$raw);
!     my `$unit = `$raw;
!     `$unit =~ s/^\s*[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?\s*//;
!     `$unit = lc(`$unit);
!     `$unit =~ s/[\[\]\(\)\{\}\s]//g;
!
!     if (`$unit =~ /^g(?:\/s|s-1|s\^-1|persecond|\/sec|\/second)$/) {
!         return `$value / 1000.0;
!     }
!     return `$value;
! }

! `$massFlow = get_mass_flow_kg_s(evaluate("-massFlow()\@R1 Outlet"));
! `$p_in    = get_num(evaluate("massFlowAve(Pressure)\@R1 Inlet"));
! `$p_out   = get_num(evaluate("massFlowAve(Pressure)\@R1 Outlet"));
! `$PR      = (`$p_in == 0) ? 0 : (`$p_out / `$p_in);
! `$t_in    = get_num(evaluate("massFlowAve(Temperature)\@R1 Inlet"));
! `$t_out   = get_num(evaluate("massFlowAve(Temperature)\@R1 Outlet"));
! `$is_eff  = get_num(evaluate("massFlowAve(Isentropic Compression Efficiency)\@R1 Outlet"));

! printf MYCSV ("%s,%.6f,%.4f,%.4f,%.4f,%.4f,%.4f,%.6f,%.6f\n", `$currentRes, `$massFlow * $BladeCount, `$PR, `$p_in, `$p_out, `$t_in, `$t_out, `$is_eff, $BladeCount);
! close(MYCSV);
> quit
"@

    $cseContent | Out-File -FilePath $cseFile -Encoding ASCII

    $postCmd = "cfdpost -batch $cseFile -res `"$resFileName`""
    Invoke-Expression $postCmd | Out-Null
}

if (Test-Path $cseFile) {
    Remove-Item $cseFile
}

Write-Host "`n=====================================================" -ForegroundColor Cyan
Write-Host "离线提取完成！结果已保存至: $CsvFile" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
