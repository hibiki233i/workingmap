# ==============================================================================
# ANSYS CFX 离心压缩机特性图全自动扫点脚本
# 改进点：
# 1. 使用“成功推进 + 失败回退”的自适应步长，而不是单一流量阈值砍半。
# 2. 每个工况单独后处理，避免从总 CSV 最后一行回读带来的串行污染。
# 3. 零流量/异常结果不写入最终曲线，只作为喘振边界判据。
# ==============================================================================

param(
    [string]$DefFile = "",
    [string]$BaseCclFile = "",
    [string]$InitialResFile = "",
    [string]$CsvFile = "Compressor_Map_Data.csv",
    [string]$SpeedPressureTablePath = "scan_points.csv",
    [int]$Cores = 8,
    [string]$WorkingDirectory
)

$scriptRoot = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrWhiteSpace($WorkingDirectory)) {
    $WorkingDirectory = $scriptRoot
}

if (-not (Test-Path -LiteralPath $WorkingDirectory -PathType Container)) {
    throw "WorkingDirectory 不存在: $WorkingDirectory"
}

Set-Location -LiteralPath $WorkingDirectory

function Resolve-ConfigPath {
    param(
        [string]$Path,
        [switch]$AllowMissing
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $null
    }

    $candidate = $Path
    if (-not [System.IO.Path]::IsPathRooted($candidate)) {
        $candidate = Join-Path -Path (Get-Location) -ChildPath $candidate
    }

    if (-not $AllowMissing -and -not (Test-Path -LiteralPath $candidate)) {
        throw "找不到文件: $candidate"
    }

    return [System.IO.Path]::GetFullPath($candidate)
}

$defFile = Resolve-ConfigPath -Path $DefFile
$baseCclFile = Resolve-ConfigPath -Path $BaseCclFile
$initialResFile = Resolve-ConfigPath -Path $InitialResFile -AllowMissing
$csvFile = Resolve-ConfigPath -Path $CsvFile -AllowMissing
$speedPressureTablePath = Resolve-ConfigPath -Path $SpeedPressureTablePath

if ([string]::IsNullOrWhiteSpace($defFile)) {
    throw "未指定 DEF 文件。请通过 -DefFile 传入有效的 .def 文件路径。"
}

if ([string]::IsNullOrWhiteSpace($baseCclFile)) {
    throw "未指定 Base CCL 文件。请通过 -BaseCclFile 传入有效的 .ccl 文件路径。"
}

if ($Cores -le 0) {
    throw "Cores 必须大于 0。"
}

# 自适应步长参数
$initialDeltaP = 1.0
$minDeltaP = 0.1
$maxDeltaP = $initialDeltaP
$stepGrowFactor = 1.25
$stepShrinkFactor = 0.5
$relativeDropWarn = 0.03      # 流量相对下降 3% 开始缩步
$relativeDropStrong = 0.08    # 流量相对下降 8% 视为强烈接近喘振
$absoluteDropWarn_kg = 0.000005   # 0.005 g/s
$absoluteDropStrong_kg = 0.000020 # 0.020 g/s
$curvatureWarn = 0.000004         # 二阶差分阈值 [kg/s/Pa^2]
$curvatureStrong = 0.000010       # 强曲率阈值 [kg/s/Pa^2]
$slopeAmplificationWarn = 1.5     # 末段斜率相对前段放大倍数
$slopeAmplificationStrong = 2.5
$zeroFlowThreshold_kg = 0.0001    # 0.1 g/s
$maxSafePressure = 16.0
$firstPointSearchStep = $initialDeltaP
$maxRefineAttempts = 12
$outTailBytes = 50000
$wallNoticePairThreshold = 3      # 连续命中 3 次才判失稳
$monitorPollSeconds = 30
$monitorWarmupSeconds = 120

# ----------------- 2. 公共函数 -----------------
function Format-PressureValue {
    param([double]$Pressure)

    $text = $Pressure.ToString("0.###", [System.Globalization.CultureInfo]::InvariantCulture)
    if ($text.Contains(".")) {
        return $text.TrimEnd("0").TrimEnd(".")
    }

    return $text
}

function Ensure-MasterCsv {
    param([string]$Path)

    if (-not (Test-Path $Path)) {
        "Result_File,Mass_Flow_kg_s,Static_PR,P_in_Pa,P_out_Pa,T_in_K,T_out_K,Isentropic_Efficiency" | Out-File -FilePath $Path -Encoding ASCII
        return
    }

    $header = Get-Content -Path $Path -TotalCount 1 -ErrorAction SilentlyContinue
    if ($header -and ($header -notmatch "Isentropic_Efficiency")) {
        $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
        $backupPath = "$Path.legacy_no_eff_$timestamp.bak"
        Move-Item -Path $Path -Destination $backupPath
        Write-Host "  -> [CSV 升级] 旧版结果表不含等熵效率，已备份为: $backupPath" -ForegroundColor Yellow
        "Result_File,Mass_Flow_kg_s,Static_PR,P_in_Pa,P_out_Pa,T_in_K,T_out_K,Isentropic_Efficiency" | Out-File -FilePath $Path -Encoding ASCII
    }
}

function Get-CsvRecordByResultFile {
    param(
        [string]$Path,
        [string]$ResultFile
    )

    if (-not (Test-Path $Path)) {
        return $null
    }

    return Import-Csv -Path $Path | Where-Object { $_.Result_File -eq $ResultFile } | Select-Object -First 1
}

function Append-ResultRecord {
    param(
        [string]$Path,
        [pscustomobject]$Record
    )

    Ensure-MasterCsv -Path $Path

    $existing = Get-CsvRecordByResultFile -Path $Path -ResultFile $Record.Result_File
    if ($null -ne $existing) {
        return
    }

    $line = "{0},{1},{2},{3},{4},{5},{6},{7}" -f `
        $Record.Result_File, `
        ([double]$Record.Mass_Flow_kg_s).ToString("0.000000", [System.Globalization.CultureInfo]::InvariantCulture), `
        ([double]$Record.Static_PR).ToString("0.0000", [System.Globalization.CultureInfo]::InvariantCulture), `
        ([double]$Record.P_in_Pa).ToString("0.0000", [System.Globalization.CultureInfo]::InvariantCulture), `
        ([double]$Record.P_out_Pa).ToString("0.0000", [System.Globalization.CultureInfo]::InvariantCulture), `
        ([double]$Record.T_in_K).ToString("0.0000", [System.Globalization.CultureInfo]::InvariantCulture), `
        ([double]$Record.T_out_K).ToString("0.0000", [System.Globalization.CultureInfo]::InvariantCulture), `
        ([double]$Record.Isentropic_Efficiency).ToString("0.000000", [System.Globalization.CultureInfo]::InvariantCulture)

    Add-Content -Path $Path -Value $line -Encoding ASCII
}

function Import-ScanConfig {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "扫描配置 CSV 不存在: $Path"
    }

    $rows = @(Import-Csv -Path $Path)
    if ($rows.Count -eq 0) {
        throw "扫描配置 CSV 为空: $Path"
    }

    $requiredColumns = @("SpeedRPM", "InitialPressurePa")
    foreach ($column in $requiredColumns) {
        if (-not ($rows[0].PSObject.Properties.Name -contains $column)) {
            throw "扫描配置 CSV 缺少必需列: $column"
        }
    }

    $config = @()
    foreach ($row in $rows) {
        if ([string]::IsNullOrWhiteSpace([string]$row.SpeedRPM) -or [string]::IsNullOrWhiteSpace([string]$row.InitialPressurePa)) {
            throw "扫描配置 CSV 中存在空的 SpeedRPM 或 InitialPressurePa。"
        }

        $speed = 0
        $pressure = 0.0
        if (-not [int]::TryParse([string]$row.SpeedRPM, [ref]$speed)) {
            throw "SpeedRPM 不是有效整数: $($row.SpeedRPM)"
        }

        if (-not [double]::TryParse([string]$row.InitialPressurePa, [System.Globalization.NumberStyles]::Float, [System.Globalization.CultureInfo]::InvariantCulture, [ref]$pressure)) {
            throw "InitialPressurePa 不是有效数字: $($row.InitialPressurePa)"
        }

        $config += [pscustomobject]@{
            SpeedRPM = $speed
            InitialPressurePa = $pressure
        }
    }

    if ($config.Count -le 0) {
        throw "扫描配置 CSV 中没有有效扫描点。"
    }

    return $config
}

function New-TempCcl {
    param(
        [int]$Speed,
        [double]$Pressure,
        [string]$OutputPath
    )

    $cclContent = Get-Content $baseCclFile
    $cclContent = $cclContent -replace "MySpeed\s*=\s*.*", "MySpeed = $Speed [rev min^-1]"
    $cclContent = $cclContent -replace "MyBackPressure\s*=\s*.*", "MyBackPressure = $Pressure [Pa]"
    $cclContent | Out-File -FilePath $OutputPath -Encoding ASCII
}

function Get-ExistingCasesForSpeed {
    param([int]$Speed)

    $pattern = "^Map_Speed_${Speed}_Press_(?<pressure>[-+]?\d+(?:\.\d+)?)_(?<index>\d+)\.res$"
    $cases = @()

    Get-ChildItem -Path "." -Filter "Map_Speed_${Speed}_Press_*.res" -ErrorAction SilentlyContinue |
        Sort-Object Name |
        ForEach-Object {
            if ($_.Name -match $pattern) {
                $pressure = [double]::Parse($matches.pressure, [System.Globalization.CultureInfo]::InvariantCulture)
                $runStem = $_.BaseName
                $outFile = Get-ChildItem -Path ".\${runStem}.out" -ErrorAction SilentlyContinue | Select-Object -First 1

                $cases += [pscustomobject]@{
                    Pressure = $pressure
                    RunName = "Map_Speed_${Speed}_Press_$(Format-PressureValue -Pressure $pressure)"
                    ResultFile = $_.Name
                    OutFile = if ($null -ne $outFile) { $outFile.Name } else { $null }
                }
            }
        }

    return @($cases | Sort-Object Pressure, ResultFile -Unique)
}

function Invoke-CfxSolveForPoint {
    param(
        [int]$Speed,
        [double]$Pressure,
        [string]$InitResFile
    )

    $pressureTag = Format-PressureValue -Pressure $Pressure
    $runName = "Map_Speed_${Speed}_Press_${pressureTag}"
    $tempCclFile = "temp_${runName}.ccl"
    $existingRes = Get-ChildItem -Path ".\${runName}*.res" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $existingOut = Get-ChildItem -Path ".\${runName}*.out" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1

    Write-Host "`n[准备提交] 工况: $runName" -ForegroundColor Yellow

    if ($null -ne $existingRes) {
        Write-Host "  -> [跳过计算] 发现已有结果文件: $($existingRes.Name)" -ForegroundColor Cyan
        return [pscustomobject]@{
            RunName = $runName
            ResultFile = $existingRes.Name
            Solved = $false
            TempCclFile = $tempCclFile
            OutFile = if ($null -ne $existingOut) { $existingOut.Name } else { $null }
            MonitorStopped = $false
        }
    }

    New-TempCcl -Speed $Speed -Pressure $Pressure -OutputPath $tempCclFile

    $argList = @("-def", $defFile, "-ccl", $tempCclFile)
    if ($InitResFile -and (Test-Path $InitResFile)) {
        $argList += @("-ini", $InitResFile)
        Write-Host "  -> [计算中] 调取初始场: $InitResFile | 并行核数: $cores"
    } else {
        Write-Host "  -> [计算中] 无可用初始场，从零场启动 | 并行核数: $cores"
    }
    $argList += @("-name", $runName,"double", "-par-local", "-part", "$cores", "-batch")

    $process = Start-Process -FilePath "cfx5solve" -ArgumentList $argList -PassThru
    $monitorStopped = $false
    $lastOutSize = 0L
    $consecutiveBlockedChecks = 0
    $monitorStartTime = (Get-Date).AddSeconds($monitorWarmupSeconds)

    while (-not $process.HasExited) {
        Start-Sleep -Seconds $monitorPollSeconds
        $process.Refresh()

        if ($process.HasExited) {
            break
        }

        if ((Get-Date) -lt $monitorStartTime) {
            continue
        }

        $liveOutFileObj = Get-ChildItem -Path ".\${runName}*.out" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($null -eq $liveOutFileObj) {
            continue
        }

        $currentOutSize = [int64]$liveOutFileObj.Length
        if ($currentOutSize -eq $lastOutSize) {
            continue
        }
        $lastOutSize = $currentOutSize

        $liveWallCheck = Test-WallBlockedBoundaryNotice -OutFile $liveOutFileObj.FullName
        if ($liveWallCheck.IsBlocked) {
            $consecutiveBlockedChecks++
            Write-Host ("  -> [实时监控] 检测到进出口锁墙（第 {0}/{1} 次）：Inlet={2}, Outlet={3}, 配对={4}" -f `
                $consecutiveBlockedChecks, $wallNoticePairThreshold, $liveWallCheck.InletCount, $liveWallCheck.OutletCount, $liveWallCheck.PairCount) -ForegroundColor Red

            if ($consecutiveBlockedChecks -ge $wallNoticePairThreshold) {
                Write-Host "  -> [实时监控] 确认持续堵塞，强制终止求解器进程树。" -ForegroundColor Red
                [void](Stop-CfxProcessTree -RootPid $process.Id -RunName $runName)
                Start-Sleep -Seconds 3
                $monitorStopped = $true
                break
            }
        } else {
            if ($consecutiveBlockedChecks -gt 0) {
                Write-Host "  -> [实时监控] 锁墙提示未持续出现，重置计数。" -ForegroundColor DarkGray
            }
            $consecutiveBlockedChecks = 0
        }
    }

    if (-not $process.HasExited) {
        $process.WaitForExit()
    }

    $latestRes = Get-ChildItem -Path ".\${runName}*.res" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $latestOut = Get-ChildItem -Path ".\${runName}*.out" -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($null -eq $latestRes) {
        return [pscustomobject]@{
            RunName = $runName
            ResultFile = $null
            Solved = $false
            TempCclFile = $tempCclFile
            OutFile = if ($null -ne $latestOut) { $latestOut.Name } else { $null }
            MonitorStopped = $monitorStopped
        }
    }

    Write-Host "  -> 求解收敛！已成功捕获结果文件: $($latestRes.Name)" -ForegroundColor Green
    return [pscustomobject]@{
        RunName = $runName
        ResultFile = $latestRes.Name
        Solved = $true
        TempCclFile = $tempCclFile
        OutFile = if ($null -ne $latestOut) { $latestOut.Name } else { $null }
        MonitorStopped = $monitorStopped
    }
}

function Get-FileTailText {
    param(
        [string]$Path,
        [int]$TailBytes
    )

    if (-not (Test-Path $Path)) {
        return ""
    }

    $fileInfo = Get-Item $Path
    $readBytes = [Math]::Min([int64]$TailBytes, $fileInfo.Length)
    $buffer = New-Object byte[] $readBytes

    $stream = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::ReadWrite)
    try {
        $stream.Seek(-1 * $readBytes, [System.IO.SeekOrigin]::End) | Out-Null
        [void]$stream.Read($buffer, 0, $readBytes)
    } finally {
        $stream.Dispose()
    }

    return [System.Text.Encoding]::UTF8.GetString($buffer)
}

function Test-WallBlockedBoundaryNotice {
    param([string]$OutFile)

    if (-not $OutFile -or -not (Test-Path $OutFile)) {
        return [pscustomobject]@{
            IsBlocked = $false
            InletCount = 0
            OutletCount = 0
            PairCount = 0
        }
    }

    $tailText = Get-FileTailText -Path $OutFile -TailBytes $outTailBytes
    $inletPattern = [regex]'A wall has been placed at portion\(s\) of an INLET[\s\S]{0,260}?100\.0% of the faces, 100\.0% of the area[\s\S]{0,260}?R1 Inlet'
    $outletPattern = [regex]'A wall has been placed at portion\(s\) of an OUTLET[\s\S]{0,260}?100\.0% of the faces, 100\.0% of the area[\s\S]{0,260}?R1 Outlet'

    $inletCount = $inletPattern.Matches($tailText).Count
    $outletCount = $outletPattern.Matches($tailText).Count
    $pairCount = [Math]::Min($inletCount, $outletCount)

    return [pscustomobject]@{
        IsBlocked = ($pairCount -ge $wallNoticePairThreshold)
        InletCount = $inletCount
        OutletCount = $outletCount
        PairCount = $pairCount
    }
}

function Get-ProcessDescendants {
    param([int]$ParentId)

    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    if ($allProcesses.Count -eq 0) {
        return @()
    }

    $directChildren = @($allProcesses | Where-Object { $_.ParentProcessId -eq $ParentId })
    $allChildren = @()

    foreach ($child in $directChildren) {
        $allChildren += $child
        $allChildren += Get-ProcessDescendants -ParentId ([int]$child.ProcessId)
    }

    return @($allChildren)
}

function Stop-CfxProcessTree {
    param(
        [int]$RootPid,
        [string]$RunName
    )

    $killedPids = @()

    try {
        $descendants = @(Get-ProcessDescendants -ParentId $RootPid | Sort-Object ProcessId -Unique)
        $root = Get-Process -Id $RootPid -ErrorAction SilentlyContinue

        $targets = @()
        foreach ($proc in $descendants) {
            $targets += [pscustomobject]@{
                ProcessId = [int]$proc.ProcessId
                Name = $proc.Name
            }
        }
        if ($null -ne $root) {
            $targets += [pscustomobject]@{
                ProcessId = [int]$root.Id
                Name = $root.ProcessName
            }
        }

        foreach ($target in ($targets | Sort-Object ProcessId -Descending -Unique)) {
            try {
                Write-Host "  -> [清理进程树] 终止: $($target.Name) (PID=$($target.ProcessId))" -ForegroundColor DarkYellow
                Stop-Process -Id $target.ProcessId -Force -ErrorAction SilentlyContinue
                $killedPids += $target.ProcessId
            } catch {
            }
        }
    } catch {
    }

    foreach ($procName in @("solver-mpi", "cfx5solve", "cfx5control")) {
        Get-Process -Name $procName -ErrorAction SilentlyContinue | ForEach-Object {
            try {
                Write-Host "  -> [按名称补杀] 终止: $($_.ProcessName) (PID=$($_.Id))" -ForegroundColor DarkYellow
                Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
                $killedPids += $_.Id
            } catch {
            }
        }
    }

    return @($killedPids | Sort-Object -Unique)
}

function Invoke-PostProcessForPoint {
    param([string]$ResultFile)

    $cached = Get-CsvRecordByResultFile -Path $csvFile -ResultFile $ResultFile
    if ($null -ne $cached) {
        if (-not ($cached.PSObject.Properties.Name -contains "Isentropic_Efficiency") -or [string]::IsNullOrWhiteSpace([string]$cached.Isentropic_Efficiency)) {
            $cached = $null
        }
    }

    if ($null -ne $cached) {
        return [pscustomobject]@{
            Result_File = $cached.Result_File
            Mass_Flow_kg_s = [double]$cached.Mass_Flow_kg_s
            Static_PR = [double]$cached.Static_PR
            P_in_Pa = [double]$cached.P_in_Pa
            P_out_Pa = [double]$cached.P_out_Pa
            T_in_K = [double]$cached.T_in_K
            T_out_K = [double]$cached.T_out_K
            Isentropic_Efficiency = [double]$cached.Isentropic_Efficiency
        }
    }

    $tempCsv = "__temp_extract.csv"
    $cseFile = "Extract_Map_Data.cse"
    if (Test-Path $tempCsv) {
        Remove-Item $tempCsv
    }

    $cseContent = @"
! `$outFile = "$tempCsv";
! open(MYCSV, ">", `$outFile) or die "无法打开文件\n";
! print MYCSV "Result_File,Mass_Flow_kg_s,Static_PR,P_in_Pa,P_out_Pa,T_in_K,T_out_K,Isentropic_Efficiency\n";
! `$resFileName = "$ResultFile";

! sub get_num {
!     my `$val = shift;
!     if (`$val =~ /^\s*([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)/) {
!         return `$1;
!     }
!     return 0;
! }

! `$massFlow = get_num(evaluate("-massFlow()\@R1 Outlet"));
! `$p_in    = get_num(evaluate("massFlowAve(Pressure)\@R1 Inlet"));
! `$p_out   = get_num(evaluate("massFlowAve(Pressure)\@R1 Outlet"));
! `$PR      = (`$p_in == 0) ? 0 : (`$p_out / `$p_in);
! `$t_in    = get_num(evaluate("massFlowAve(Temperature)\@R1 Inlet"));
! `$t_out   = get_num(evaluate("massFlowAve(Temperature)\@R1 Outlet"));
! `$is_eff  = get_num(evaluate("massFlowAve(Isentropic Compression Efficiency)\@R1 Outlet"));

! printf MYCSV ("%s,%.6f,%.4f,%.4f,%.4f,%.4f,%.4f,%.6f\n", `$resFileName, `$massFlow, `$PR, `$p_in, `$p_out, `$t_in, `$t_out, `$is_eff);
! close(MYCSV);
> quit
"@

    $cseContent | Out-File -FilePath $cseFile -Encoding ASCII

    $postCmd = "cfdpost -batch $cseFile -res `"$ResultFile`""
    Invoke-Expression $postCmd | Out-Null

    if (-not (Test-Path $tempCsv)) {
        return $null
    }

    $record = Import-Csv -Path $tempCsv | Select-Object -First 1
    Remove-Item $tempCsv -ErrorAction SilentlyContinue

    if ($null -eq $record) {
        return $null
    }

    return [pscustomobject]@{
        Result_File = $record.Result_File
        Mass_Flow_kg_s = [double]$record.Mass_Flow_kg_s
        Static_PR = [double]$record.Static_PR
        P_in_Pa = [double]$record.P_in_Pa
        P_out_Pa = [double]$record.P_out_Pa
        T_in_K = [double]$record.T_in_K
        T_out_K = [double]$record.T_out_K
        Isentropic_Efficiency = [double]$record.Isentropic_Efficiency
    }
}

function Get-NextDeltaP {
    param(
        [double]$CurrentDeltaP,
        [double]$PrevMassFlowKg,
        [double]$CurrentMassFlowKg,
        [double]$PressureStep,
        [object[]]$StableHistory
    )

    $nextDeltaP = $CurrentDeltaP
    $absDrop = [math]::Max($PrevMassFlowKg - $CurrentMassFlowKg, 0.0)
    $relDrop = if ($PrevMassFlowKg -gt 0) { $absDrop / $PrevMassFlowKg } else { 0.0 }
    $slope = if ($PressureStep -gt 0) { $absDrop / $PressureStep } else { 0.0 }
    $curvature = 0.0
    $slopeAmplification = 1.0

    Write-Host ("  -> 流量下降: {0:F6} kg/s | 相对下降: {1:P2} | 下降斜率: {2:F6} kg/s/Pa" -f $absDrop, $relDrop, $slope)

    if ($StableHistory.Count -ge 3) {
        $p0 = [double]$StableHistory[-3].Pressure
        $m0 = [double]$StableHistory[-3].MassFlowKg
        $p1 = [double]$StableHistory[-2].Pressure
        $m1 = [double]$StableHistory[-2].MassFlowKg
        $p2 = [double]$StableHistory[-1].Pressure
        $m2 = [double]$StableHistory[-1].MassFlowKg

        $dp1 = $p1 - $p0
        $dp2 = $p2 - $p1
        if ($dp1 -gt 0 -and $dp2 -gt 0) {
            $slope1 = ($m1 - $m0) / $dp1
            $slope2 = ($m2 - $m1) / $dp2
            $avgDp = ($dp1 + $dp2) / 2.0
            if ($avgDp -gt 0) {
                $curvature = [math]::Abs(($slope2 - $slope1) / $avgDp)
            }

            if ([math]::Abs($slope1) -gt 1e-12) {
                $slopeAmplification = [math]::Abs($slope2) / [math]::Abs($slope1)
            } elseif ([math]::Abs($slope2) -gt 0) {
                $slopeAmplification = [double]::PositiveInfinity
            }

            Write-Host ("  -> 三点曲率: {0:F6} kg/s/Pa^2 | 斜率放大倍数: {1:F2}" -f $curvature, $slopeAmplification)
        }
    }

    $strongByFirstOrder = $relDrop -ge $relativeDropStrong -or $absDrop -ge $absoluteDropStrong_kg
    $warnByFirstOrder = $relDrop -ge $relativeDropWarn -or $absDrop -ge $absoluteDropWarn_kg
    $strongByCurvature = $curvature -ge $curvatureStrong -or $slopeAmplification -ge $slopeAmplificationStrong
    $warnByCurvature = $curvature -ge $curvatureWarn -or $slopeAmplification -ge $slopeAmplificationWarn

    if ($strongByFirstOrder -or $strongByCurvature) {
        $nextDeltaP = [math]::Max($CurrentDeltaP * $stepShrinkFactor, $minDeltaP)
        Write-Host "  -> [强预警] 一阶跌幅或二阶曲率显示已明显逼近喘振边界，立即缩步。" -ForegroundColor Magenta
    } elseif ($warnByFirstOrder -or $warnByCurvature) {
        $nextDeltaP = [math]::Max($CurrentDeltaP * 0.7, $minDeltaP)
        Write-Host "  -> [预警] 流量曲线开始变陡或下弯，提前缩步。" -ForegroundColor DarkMagenta
    } else {
        $nextDeltaP = [math]::Min($CurrentDeltaP * $stepGrowFactor, $maxDeltaP)
        if ($nextDeltaP -gt $CurrentDeltaP) {
            Write-Host "  -> [放宽] 当前仍处于平稳区，步长小幅恢复。" -ForegroundColor DarkGray
        }
    }

    return $nextDeltaP
}

Ensure-MasterCsv -Path $csvFile
$scanConfig = @(Import-ScanConfig -Path $speedPressureTablePath)

Write-Host "工作目录: $(Get-Location)" -ForegroundColor DarkGray
Write-Host "DEF 文件: $defFile" -ForegroundColor DarkGray
Write-Host "基础 CCL: $baseCclFile" -ForegroundColor DarkGray
Write-Host "初始场文件: $(if ($initialResFile) { $initialResFile } else { '未指定' })" -ForegroundColor DarkGray
Write-Host "结果 CSV: $csvFile" -ForegroundColor DarkGray
Write-Host "扫描配置: $speedPressureTablePath" -ForegroundColor DarkGray
Write-Host "扫描点数量: $($scanConfig.Count)" -ForegroundColor DarkGray

# ----------------- 3. 主扫描流程 -----------------
for ($i = 0; $i -lt $scanConfig.Count; $i++) {
    $speed = [int]$scanConfig[$i].SpeedRPM
    $currentPressure = [double]$scanConfig[$i].InitialPressurePa
    $deltaP = [double]$initialDeltaP
    $currentInitRes = $initialResFile
    $lastStablePoint = $null
    $stableHistory = @()
    $refineCount = 0
    $lineFinished = $false
    $existingCases = Get-ExistingCasesForSpeed -Speed $speed
    $usedExistingResults = @{}

    Write-Host "`n=====================================================" -ForegroundColor Cyan
    Write-Host "开始扫描等转速线: $speed RPM | 初始背压: $currentPressure Pa" -ForegroundColor Cyan
    Write-Host "=====================================================" -ForegroundColor Cyan

    while (-not $lineFinished) {
        if ($currentPressure -gt $maxSafePressure) {
            Write-Host "  -> [停止] 当前背压 $currentPressure Pa 已超过安全上限 $maxSafePressure Pa。" -ForegroundColor Red
            break
        }

        $pressureTolerance = [Math]::Max($minDeltaP / 2.0, 0.001)
        $unusedExistingCases = @($existingCases | Where-Object { -not $usedExistingResults.ContainsKey($_.ResultFile) })
        $existingCandidate = $null

        if ($null -ne $lastStablePoint) {
            $existingCandidate = $unusedExistingCases |
                Where-Object {
                    $_.Pressure -gt ([double]$lastStablePoint.Pressure + $pressureTolerance) -and
                    $_.Pressure -le ([double]$currentPressure + $pressureTolerance)
                } |
                Sort-Object Pressure |
                Select-Object -First 1
        }

        if ($null -eq $existingCandidate) {
            $existingCandidate = $unusedExistingCases |
                Where-Object { $_.Pressure -ge ([double]$currentPressure - $pressureTolerance) } |
                Sort-Object Pressure |
                Select-Object -First 1
        }

        if ($null -ne $existingCandidate) {
            $targetPressure = [double]$existingCandidate.Pressure
            Write-Host "  -> 优先读取已有工况: $(Format-PressureValue -Pressure $targetPressure) Pa | 当前步长参考: $(Format-PressureValue -Pressure $deltaP) Pa" -ForegroundColor Yellow
            Write-Host "  -> [复用历史] 使用已有结果文件: $($existingCandidate.ResultFile)" -ForegroundColor Cyan
            $solveInfo = [pscustomobject]@{
                RunName = $existingCandidate.RunName
                ResultFile = $existingCandidate.ResultFile
                Solved = $false
                TempCclFile = ""
                OutFile = $existingCandidate.OutFile
                FromExisting = $true
                Pressure = $targetPressure
            }
            $usedExistingResults[$existingCandidate.ResultFile] = $true
        } else {
            $targetPressure = [double]$currentPressure
            Write-Host "  -> 当前试探背压: $(Format-PressureValue -Pressure $targetPressure) Pa | 步长: $(Format-PressureValue -Pressure $deltaP) Pa" -ForegroundColor Yellow
            $solveInfo = Invoke-CfxSolveForPoint -Speed $speed -Pressure $targetPressure -InitResFile $currentInitRes
            $solveInfo | Add-Member -NotePropertyName FromExisting -NotePropertyValue $false -Force
            $solveInfo | Add-Member -NotePropertyName Pressure -NotePropertyValue $targetPressure -Force
        }

        if ((-not $solveInfo.ResultFile) -or ($null -eq $solveInfo.ResultFile)) {
            if ($null -eq $lastStablePoint) {
                Write-Host "  -> [起始点未收敛] 先抬高背压继续搜索稳定工作点。" -ForegroundColor Yellow
                $currentPressure += $firstPointSearchStep
                continue
            }

            $refineCount++
            if ($deltaP -le $minDeltaP -or $refineCount -ge $maxRefineAttempts) {
                Write-Host "  -> [边界确认] 继续细分已无明显收益，上一稳定点视为喘振前最后有效点。" -ForegroundColor Red
                break
            }

            $deltaP = [math]::Max($deltaP * $stepShrinkFactor, $minDeltaP)
            $currentPressure = [double]$lastStablePoint.Pressure + $deltaP
            Write-Host "  -> [回退细分] 本点发散，回到上一稳定点后缩步重试。" -ForegroundColor Magenta
            continue
        }

        $wallNoticeCheck = Test-WallBlockedBoundaryNotice -OutFile $solveInfo.OutFile
        if ($wallNoticeCheck.IsBlocked) {
            Write-Host ("  -> [边界确认] .out 尾部检测到持续锁墙：Inlet={0}, Outlet={1}, 配对={2}。判定已堵塞或越过喘振边界。" -f `
                $wallNoticeCheck.InletCount, $wallNoticeCheck.OutletCount, $wallNoticeCheck.PairCount) -ForegroundColor Red

            if ($null -eq $lastStablePoint) {
                Write-Host "  -> [起始区异常] 该点虽生成结果，但尾部日志显示完全回流，继续抬高背压寻找首个稳定点。" -ForegroundColor Yellow
                $currentPressure = [double]$solveInfo.Pressure + $firstPointSearchStep
                continue
            }

            $refineCount++
            if ($deltaP -le $minDeltaP -or $refineCount -ge $maxRefineAttempts) {
                Write-Host "  -> [停止] 日志锁墙判据已持续触发，上一稳定点视为最后有效点。" -ForegroundColor Red
                break
            }

            $deltaP = [math]::Max($deltaP * $stepShrinkFactor, $minDeltaP)
            $currentPressure = [double]$lastStablePoint.Pressure + $deltaP
            Write-Host "  -> [回退细分] 不再后处理该点，直接缩步回退。" -ForegroundColor Magenta
            continue
        }

        $pointData = Invoke-PostProcessForPoint -ResultFile $solveInfo.ResultFile
        if ($solveInfo.Solved -and (Test-Path $solveInfo.TempCclFile)) {
            Remove-Item $solveInfo.TempCclFile -ErrorAction SilentlyContinue
        }

        if ($null -eq $pointData) {
            Write-Host "  -> [错误] CFD-Post 数据提取失败，终止该转速线扫描。" -ForegroundColor Red
            break
        }

        $currentMassFlowKg = [double]$pointData.Mass_Flow_kg_s
        $currentPR = [double]$pointData.Static_PR
        Write-Host ("  -> 实测流量: {0:F6} kg/s ({1:F3} g/s) | 静压比: {2:F4}" -f $currentMassFlowKg, ($currentMassFlowKg * 1000.0), $currentPR)

        if ($currentMassFlowKg -le $zeroFlowThreshold_kg -or $currentPR -le 0) {
            if ($null -eq $lastStablePoint) {
                Write-Host "  -> [异常] 起始点已出现零流量/非法压比，继续抬高背压搜索可收敛点。" -ForegroundColor Yellow
                $currentPressure = [double]$solveInfo.Pressure + $firstPointSearchStep
                continue
            }

            $refineCount++
            if ($deltaP -le $minDeltaP -or $refineCount -ge $maxRefineAttempts) {
                Write-Host "  -> [边界确认] 已逼近喘振极限，停止本条转速线。" -ForegroundColor Red
                break
            }

            $deltaP = [math]::Max($deltaP * $stepShrinkFactor, $minDeltaP)
            $currentPressure = [double]$lastStablePoint.Pressure + $deltaP
            Write-Host "  -> [回退细分] 本点流量异常或接近憋死，不写入曲线，缩步重试。" -ForegroundColor Magenta
            continue
        }

        Append-ResultRecord -Path $csvFile -Record $pointData

        if ($null -eq $lastStablePoint) {
            Write-Host "  -> 记录首个稳定点，保持初始步长继续推进。" -ForegroundColor DarkGray
        }

        $lastStablePoint = [pscustomobject]@{
            Pressure = [double]$solveInfo.Pressure
            MassFlowKg = [double]$currentMassFlowKg
            PR = [double]$currentPR
            ResultFile = $solveInfo.ResultFile
        }
        $stableHistory += $lastStablePoint
        if ($stableHistory.Count -gt 3) {
            $stableHistory = @($stableHistory[-3], $stableHistory[-2], $stableHistory[-1])
        }

        if ($stableHistory.Count -ge 2) {
            $deltaP = Get-NextDeltaP `
                -CurrentDeltaP $deltaP `
                -PrevMassFlowKg ([double]$stableHistory[-2].MassFlowKg) `
                -CurrentMassFlowKg $currentMassFlowKg `
                -PressureStep ([double]$lastStablePoint.Pressure - [double]$stableHistory[-2].Pressure) `
                -StableHistory $stableHistory
        }

        $currentInitRes = $solveInfo.ResultFile
        $currentPressure = [double]$lastStablePoint.Pressure + $deltaP
        $refineCount = 0
    }
}

Write-Host "`n=====================================================" -ForegroundColor Cyan
Write-Host "全图自动化扫点完成！有效数据已保存至: $csvFile" -ForegroundColor Cyan
Write-Host "=====================================================" -ForegroundColor Cyan
