# Shared CSV contract for scan and batch extraction. Numeric strings use invariant culture.
$mapCsvHeader = 'Result_File,Mass_Flow_kg_s,Static_PR,P_in_Pa,P_out_Pa,T_in_K,T_out_K,Isentropic_Efficiency,Blade_Count,Mass_Flow_Unit,Result_LastWriteUtcTicks,Result_Size_Bytes,schema_version,Speed_RPM,Back_Pressure_Pa,Total_PR'

function Test-MapTotalRatio {
    param([string]$Value)
    $ratio = 0.0
    return [double]::TryParse($Value, [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture, [ref]$ratio) -and
        -not [double]::IsNaN($ratio) -and -not [double]::IsInfinity($ratio) -and $ratio -gt 0
}

function Add-MapMetadata {
    param([object]$Record)
    foreach ($name in @('schema_version', 'Speed_RPM', 'Back_Pressure_Pa', 'Total_PR')) {
        if (-not ($Record.PSObject.Properties.Name -contains $name)) {
            $Record | Add-Member -NotePropertyName $name -NotePropertyValue ''
        }
    }
    if ([string]::IsNullOrWhiteSpace([string]$Record.Speed_RPM) -or
        [string]::IsNullOrWhiteSpace([string]$Record.Back_Pressure_Pa)) {
        if ([string]$Record.Result_File -match 'Map_Speed_(\d+)_Press_([-+]?\d+(?:\.\d+)?)') {
            if ([string]::IsNullOrWhiteSpace([string]$Record.Speed_RPM)) { $Record.Speed_RPM = $Matches[1] }
            if ([string]::IsNullOrWhiteSpace([string]$Record.Back_Pressure_Pa)) { $Record.Back_Pressure_Pa = $Matches[2] }
        } else {
            Write-Warning "Cannot infer Speed_RPM / Back_Pressure_Pa from '$($Record.Result_File)'; fill these CSV columns before plotting."
        }
    }
    # Only fill an absent version; explicit versions keep their original semantics.
    if ([string]::IsNullOrWhiteSpace([string]$Record.schema_version) -and
        (Test-MapTotalRatio ([string]$Record.Total_PR)) -and $Record.Speed_RPM -and
        ([string]$Record.Back_Pressure_Pa -ne '')) {
        $Record.schema_version = '1'
    }
}

# A re-extracted record only carries physics; keep operating points the previous row already had
# (e.g. filled by hand for arbitrarily named results). Call before Add-MapMetadata.
function Copy-MapOperatingPoint {
    param([object]$Record, [object]$Previous)
    if ($null -eq $Previous) { return }
    foreach ($name in @('Speed_RPM', 'Back_Pressure_Pa')) {
        $old = if ($Previous.PSObject.Properties.Name -contains $name) { [string]$Previous.$name } else { '' }
        $new = if ($Record.PSObject.Properties.Name -contains $name) { [string]$Record.$name } else { '' }
        if ([string]::IsNullOrWhiteSpace($new) -and -not [string]::IsNullOrWhiteSpace($old)) {
            $Record | Add-Member -NotePropertyName $name -NotePropertyValue $old.Trim() -Force
        }
    }
}

function Write-MapCsvRecord {
    param([string]$Path, [object]$Record)
    Add-MapMetadata $Record
    $fields = foreach ($name in $mapCsvHeader.Split(',')) {
        $value = $Record.$name
        if ($value -is [System.IFormattable]) {
            $value = $value.ToString($null, [System.Globalization.CultureInfo]::InvariantCulture)
        }
        '"' + ([string]$value).Replace('"', '""') + '"'
    }
    Add-Content -LiteralPath $Path -Value ($fields -join ',') -Encoding UTF8
}
