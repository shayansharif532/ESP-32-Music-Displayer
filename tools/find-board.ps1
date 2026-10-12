<#
.SYNOPSIS
    Print the serial port of the Waveshare ESP32-S3-Touch-LCD-4B (ESP32-S3 native USB-Serial/JTAG, VID 303A / PID 1001).

.DESCRIPTION
    The COM number differs between computers (it was COM5 on the original dev PC), so never hard-code it:

        $port = .\tools\find-board.ps1
        .\tools\idf.ps1 -p $port flash

    Read-only: it only reads Windows' device list and never opens the port.
    Exits 1 (and prints nothing on stdout) if no board or more than one board is found.
    The same VID:PID is used by the ROM bootloader, so this also finds a board held in download mode.
#>
$found = @(Get-CimInstance Win32_PnPEntity |
    Where-Object { $_.PNPDeviceID -match 'VID_303A&PID_1001' -and $_.Name -match '\((COM\d+)\)' } |
    ForEach-Object { [void]($_.Name -match '\((COM\d+)\)'); $Matches[1] } |
    Sort-Object -Unique)

if ($found.Count -eq 1) {
    $found[0]
    exit 0
}
if ($found.Count -eq 0) {
    [Console]::Error.WriteLine('No ESP32-S3 USB-Serial/JTAG device (VID 303A, PID 1001) found. Is the board plugged in with the USB-C port that is wired to the ESP32-S3 (the one that is NOT the UART bridge)? Check Device Manager > Ports.')
} else {
    [Console]::Error.WriteLine("More than one ESP32-S3 found: $($found -join ', '). Unplug the others or pass the port explicitly.")
}
exit 1
