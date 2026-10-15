$ErrorActionPreference = 'Stop'
$repo = Split-Path $PSScriptRoot
. (Join-Path $repo 'map_data.ps1')
function Assert-Equal($Actual, $Expected) {
    if ($Expected -is [ValueType]) { $Actual = [double]$Actual }
    if ($Actual -ne $Expected) { throw "Expected '$Expected', got '$Actual'" }
}
$temp = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
New-Item -ItemType Directory $temp | Out-Null
try {
    $row = [pscustomobject]@{ Result_File = 'Map_Speed_50000_Press_12.5.res'; Mass_Flow_kg_s = '0.002'; Static_PR = '1.4' }
    Add-MapMetadata $row
    Assert-Equal $row.Speed_RPM '50000'
    Assert-Equal $row.Back_Pressure_Pa '12.5'
    Assert-Equal $row.schema_version ''
    $path = Join-Path $temp 'roundtrip.csv'
    $mapCsvHeader | Set-Content $path
    Write-MapCsvRecord $path $row
    $loaded = Import-Csv $path
    Assert-Equal $loaded.Total_PR ''
    Assert-Equal $loaded.Blade_Count ''
    Assert-Equal $loaded.Mass_Flow_kg_s '0.002'

    # Rewriting populated records must preserve explicit versions, including future ones.
    foreach ($version in @('2', '1', 'external-version', '', '   ')) {
        $versioned = [pscustomobject]@{
            schema_version = $version; Result_File = 'custom.res'
            Total_PR = '1.6'; Speed_RPM = '50000'; Back_Pressure_Pa = '12.5'
            Mass_Flow_kg_s = '0.002'; Static_PR = '1.4'; Blade_Count = '10'
        }
        $mapCsvHeader | Set-Content $path
        Write-MapCsvRecord $path $versioned
        $expectedVersion = if ([string]::IsNullOrWhiteSpace($version)) { '1' } else { $version }
        Assert-Equal (Import-Csv $path).schema_version $expectedVersion
    }

    # Load only functions from scan script: no solver or scan starts.
    $tokens = $null; $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $repo 'cfx.ps1'), [ref]$tokens, [ref]$errors)
    foreach ($func in $ast.FindAll({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $false)) {
        Invoke-Expression $func.Extent.Text
    }
    $resultCsvHeader = $mapCsvHeader
    $versioned.schema_version = '2'
    $versioned | Export-Csv $path -NoTypeInformation
    Ensure-MasterCsv $path
    Assert-Equal (Import-Csv $path).schema_version '2'
    $converted = Convert-ToTotalFlowRecord -PointData ([pscustomobject]@{
        Result_File='custom.res'; Mass_Flow_kg_s=0.002; Static_PR=1.4; Total_PR=1.6
        Speed_RPM=50000; Back_Pressure_Pa=12.5; schema_version='1'; Blade_Count=1
        P_in_Pa=10; P_out_Pa=14; T_in_K=4; T_out_K=5; Isentropic_Efficiency=0.8
        Mass_Flow_Unit='g/s'; Result_LastWriteUtcTicks=1; Result_Size_Bytes=2
    }) -BladeCountValue 10
    Assert-Equal $converted.Total_PR 1.6
    Assert-Equal $converted.Speed_RPM 50000
    Assert-Equal $converted.Mass_Flow_kg_s 0.02

    # Legacy header upgrade preserves missing Blade_Count/Total_PR as blanks.
    'Result_File,Mass_Flow_kg_s,Static_PR', 'Map_Speed_50000_Press_12.5.res,0.002,1.4' | Set-Content $path
    Ensure-MasterCsv $path
    $upgraded = Import-Csv $path
    Assert-Equal $upgraded.Blade_Count ''
    Assert-Equal $upgraded.Total_PR ''
    Assert-Equal $upgraded.Speed_RPM 50000

    # Run actual data_only with CFD-Post expressions evaluated by a local Perl stub.
    # This exercises the generated Perl macro, units, new columns, cache and CSV writer.
    function cfdpost {
        param([Alias("batch")]$macro, [Alias("res")]$result)
        $postCounter.Value++
        $body = (Get-Content $macro | Where-Object { $_ -match '^!' } | ForEach-Object { $_ -replace '^!\s?', '' }) -join "`n"
        $stub = @'
sub evaluate {
    my $expr = shift;
    return 30 if $expr =~ /Total Pressure.*Outlet/;
    return 15 if $expr =~ /Total Pressure.*Inlet/;
    return 12 if $expr =~ /Pressure.*Outlet/;
    return 10 if $expr =~ /Pressure.*Inlet/;
    return 0.8 if $expr =~ /Efficiency/;
    return 2 if $expr =~ /massFlow\(\)/;
    return 4;
}
'@
        $perlFile = Join-Path $temp 'post.pl'
        ($stub + "`n" + $body) | Set-Content $perlFile
        & perl $perlFile
        if ($LASTEXITCODE -ne 0) { throw 'Generated CSE Perl failed' }
    }
    Push-Location $temp
    try {
        'fake result' | Set-Content 'Map_Speed_50000_Press_12.5.res'
        $postCounter = @{ Value = 0 }
        & (Join-Path $repo 'data_only.ps1') -BladeCount 10 -MassFlowUnit 'g/s'
        $data = Import-Csv 'Extracted_Compressor_Data.csv'
        Assert-Equal $data.Total_PR 2
        Assert-Equal $data.Static_PR 1.2
        Assert-Equal $data.Mass_Flow_kg_s 0.02
        Assert-Equal $data.Speed_RPM 50000
        Assert-Equal $data.Back_Pressure_Pa 12.5
        Assert-Equal $data.schema_version '1'
        & (Join-Path $repo 'data_only.ps1') -BladeCount 10 -MassFlowUnit 'g/s'
        Assert-Equal $postCounter.Value 1
        $data.Total_PR = ''; $data | Export-Csv 'Extracted_Compressor_Data.csv' -NoTypeInformation
        & (Join-Path $repo 'data_only.ps1') -BladeCount 10 -MassFlowUnit 'g/s'
        Assert-Equal $postCounter.Value 2
        Assert-Equal @(Import-Csv 'Extracted_Compressor_Data.csv').Count 1
        foreach ($invalidRatio in @('0.000000', '-1', 'NaN', 'Infinity', '-Infinity', 'not-a-number')) {
            $invalid = Import-Csv 'Extracted_Compressor_Data.csv'
            $invalid.Total_PR = $invalidRatio
            $invalid | Export-Csv 'Extracted_Compressor_Data.csv' -NoTypeInformation
            $callsBefore = $postCounter.Value
            & (Join-Path $repo 'data_only.ps1') -BladeCount 10 -MassFlowUnit 'g/s'
            Assert-Equal $postCounter.Value ($callsBefore + 1)
            Assert-Equal (Import-Csv 'Extracted_Compressor_Data.csv').Total_PR 2
        }

        $csvFile = Join-Path $temp 'scan.csv'
        $BladeCount = 10; $MassFlowUnit = 'g/s'; $massFlowToKgFactor = 0.001
        Ensure-MasterCsv $csvFile
        $point = Invoke-PostProcessForPoint 'Map_Speed_50000_Press_12.5.res'
        Assert-Equal $point.Total_PR 2
        Assert-Equal $point.schema_version '1'
        Assert-Equal $point.Speed_RPM 50000
        $point = Convert-ToTotalFlowRecord $point 10
        Save-ResultRecord $csvFile $point
        $callsBefore = $postCounter.Value
        $cached = Invoke-PostProcessForPoint 'Map_Speed_50000_Press_12.5.res'
        Assert-Equal $cached.Total_PR 2
        Assert-Equal $cached.Mass_Flow_kg_s 0.02
        Assert-Equal $postCounter.Value $callsBefore
        $badCache = Import-Csv $csvFile
        $badCache.Total_PR = ''
        $badCache | Export-Csv $csvFile -NoTypeInformation
        $refreshed = Invoke-PostProcessForPoint 'Map_Speed_50000_Press_12.5.res'
        Assert-Equal $refreshed.Total_PR 2
        Assert-Equal $postCounter.Value ($callsBefore + 1)
        foreach ($invalidRatio in @('0.000000', '-1', 'NaN', 'Infinity', '-Infinity', 'not-a-number')) {
            $badCache.Total_PR = $invalidRatio
            $badCache | Export-Csv $csvFile -NoTypeInformation
            $callsBefore = $postCounter.Value
            $refreshed = Invoke-PostProcessForPoint 'Map_Speed_50000_Press_12.5.res'
            Assert-Equal $refreshed.Total_PR 2
            Assert-Equal $postCounter.Value ($callsBefore + 1)
        }
        & (Join-Path $repo 'extract_existing_map_data.ps1') -CsvFile rebuilt.csv -BladeCount 10 -MassFlowUnit 'g/s'
        Assert-Equal (Import-Csv rebuilt.csv).Total_PR 2

        # Arbitrary result names have no plotting metadata, but physical data is cacheable.
        $arbitraryDir = Join-Path $temp 'arbitrary'
        New-Item -ItemType Directory $arbitraryDir | Out-Null
        Push-Location $arbitraryDir
        try {
            'fake result' | Set-Content 'custom.res'
            $callsBefore = $postCounter.Value
            & (Join-Path $repo 'data_only.ps1') -BladeCount 10 -MassFlowUnit 'g/s'
            $custom = Import-Csv 'Extracted_Compressor_Data.csv'
            Assert-Equal $custom.schema_version ''
            Assert-Equal $custom.Total_PR 2
            & (Join-Path $repo 'data_only.ps1') -BladeCount 10 -MassFlowUnit 'g/s'
            Assert-Equal $postCounter.Value ($callsBefore + 1)
            # Manually supplied metadata must survive a cache hit without editing the version.
            $custom.Speed_RPM = '45000'; $custom.Back_Pressure_Pa = '11'
            $custom | Export-Csv 'Extracted_Compressor_Data.csv' -NoTypeInformation
            & (Join-Path $repo 'data_only.ps1') -BladeCount 10 -MassFlowUnit 'g/s'
            Assert-Equal $postCounter.Value ($callsBefore + 1)
            $custom = Import-Csv 'Extracted_Compressor_Data.csv'
            Assert-Equal $custom.Speed_RPM 45000
            Assert-Equal $custom.Back_Pressure_Pa 11
            $csvFile = Join-Path $arbitraryDir 'Extracted_Compressor_Data.csv'
            $cached = Invoke-PostProcessForPoint 'custom.res'
            Assert-Equal $postCounter.Value ($callsBefore + 1)
            Assert-Equal $cached.Speed_RPM 45000
        } finally { Pop-Location }
    } finally { Pop-Location }
    foreach ($file in Get-ChildItem $repo -Filter '*.ps1') {
        $errors = $null
        $null = [System.Management.Automation.Language.Parser]::ParseFile($file.FullName, [ref]$tokens, [ref]$errors)
        if ($errors.Count) { throw ($errors | Out-String) }
    }
    Write-Host 'PASS: metadata, CSV migration, flow scaling, CSE extraction, cache invalidation and PowerShell syntax'
} finally { Remove-Item $temp -Recurse -Force }
