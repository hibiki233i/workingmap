# ==============================================================================
# ANSYS CFD-Post 独立批量后处理与数据提取脚本 (最终修复版)
# ==============================================================================

param(
    [double]$BladeCount = 1.0,
    [string]$MassFlowUnit = "kg/s"
)

if ($BladeCount -le 0) {
    throw "BladeCount 必须大于 0。"
}

$normalizedMassFlowUnit = $MassFlowUnit.Trim().ToLowerInvariant()
if ($normalizedMassFlowUnit -notin @("kg/s", "g/s")) {
    throw "MassFlowUnit 只能是 kg/s 或 g/s。"
}
$massFlowToKgFactor = if ($normalizedMassFlowUnit -eq "g/s") { 0.001 } else { 1.0 }

$csvFile = "Extracted_Compressor_Data.csv"
$cseFile = "Batch_Extract_Macro.cse"

# 查找当前目录下所有的 .res 文件
$resFiles = Get-ChildItem -Filter "*.res" | Sort-Object Name

if ($resFiles.Count -eq 0) {
    Write-Host "当前目录下未找到任何 .res 文件！" -ForegroundColor Red
    exit
}

Write-Host "找到 $($resFiles.Count) 个结果文件，准备开始提取..." -ForegroundColor Cyan

# 初始化 CSV 表头 (如果文件不存在)
if (-not (Test-Path $csvFile)) {
    "Result_File,Mass_Flow_kg_s,Static_PR,P_in_Pa,P_out_Pa,T_in_K,T_out_K,Isentropic_Efficiency,Blade_Count,Mass_Flow_Unit" | Out-File -FilePath $csvFile -Encoding ASCII
}

foreach ($file in $resFiles) {
    $resFileName = $file.Name
    Write-Host "正在提取: $resFileName ..." -ForegroundColor Yellow

    # ------------------------------------------------------------------
    # 动态生成 CFD-Post 提取宏
    # ------------------------------------------------------------------
    $cseContent = @"
! `$outFile = "$csvFile";
! `$currentRes = "$resFileName"; 
! open(MYCSV, ">>", `$outFile) or die "无法打开文件\n";

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

# 写入 CSV
! printf MYCSV ("%s, %.6f, %.4f, %.4f, %.4f, %.4f, %.4f, %.6f, %.6f, %s\n", `$currentRes, `$massFlow * $BladeCount, `$PR, `$p_in, `$p_out, `$t_in, `$t_out, `$is_eff, $BladeCount, "$MassFlowUnit");
! close(MYCSV);
> quit
"@
    
    # 写入临时的 CSE 宏文件
    $cseContent | Out-File -FilePath $cseFile -Encoding ASCII

    # 调用 CFD-Post 后台执行宏
    $postCmd = "cfdpost -batch $cseFile -res `"$resFileName`""
    Invoke-Expression $postCmd | Out-Null
}

# 清理临时宏文件
if (Test-Path $cseFile) { Remove-Item $cseFile }

Write-Host "`n=====================================================" -ForegroundColor Cyan
Write-Host "所有数据提取完成！已保存至: $csvFile" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
