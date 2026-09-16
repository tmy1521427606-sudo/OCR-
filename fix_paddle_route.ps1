$ErrorActionPreference = "Continue"
$logPath = Join-Path $PSScriptRoot "paddle_route_helper.log"

function Write-RouteLog([string]$message) {
    $line = "$(Get-Date -Format s) $message"
    Write-Host $line
    Add-Content -LiteralPath $logPath -Value $line -Encoding utf8
}

$currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($currentUser)
$isAdministrator = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Write-RouteLog "START admin=$isAdministrator"

if (-not $isAdministrator) {
    Write-RouteLog "REQUESTING_ELEVATION"
    try {
        $arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit", "-File", $PSCommandPath)
        Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList $arguments -ErrorAction Stop
        Write-RouteLog "ELEVATED_WINDOW_STARTED"
    }
    catch {
        Write-RouteLog "ELEVATION_FAILED $($_.Exception.Message)"
    }
    Read-Host "Press Enter to close this window"
    return
}

Write-RouteLog "ADDING_ROUTE 192.168.1.115 via 192.168.2.1 interface=13"
$routeOutput = & route.exe add 192.168.1.115 mask 255.255.255.255 192.168.2.1 metric 10 if 13 2>&1 | Out-String
Write-RouteLog "ROUTE_EXIT_CODE=$LASTEXITCODE"
Write-Host $routeOutput
Add-Content -LiteralPath $logPath -Value $routeOutput -Encoding utf8

Write-RouteLog "TESTING_PORT 192.168.1.115:8870"
$testOutput = Test-NetConnection 192.168.1.115 -Port 8870 | Format-List * | Out-String
Write-Host $testOutput
Add-Content -LiteralPath $logPath -Value $testOutput -Encoding utf8
Write-RouteLog "FINISHED"
Read-Host "Press Enter to close this window"
