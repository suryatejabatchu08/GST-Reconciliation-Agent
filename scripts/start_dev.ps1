# ============================================================
# GST Reconciliation Agent — PowerShell Dev Launcher
# Windows equivalent of the Makefile
#
# Usage (from project root):
#   .\scripts\start_dev.ps1          ← starts all 5 services
#   .\scripts\start_dev.ps1 gateway  ← starts single service
#   .\scripts\start_dev.ps1 stop     ← kills all service processes
#   .\scripts\start_dev.ps1 test     ← runs all tests
#   .\scripts\start_dev.ps1 validate ← checks .env keys
# ============================================================

param(
    [string]$Command = "start",
    [string]$Service = ""
)

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

# Colour helpers
function Write-Green($msg) { Write-Host $msg -ForegroundColor Green }
function Write-Yellow($msg) { Write-Host $msg -ForegroundColor Yellow }
function Write-Red($msg) { Write-Host $msg -ForegroundColor Red }
function Write-Cyan($msg) { Write-Host $msg -ForegroundColor Cyan }

# Service definitions: name → (module, port)
$Services = @{
    "ingestion"      = @{ Module = "ingestion_service.main:app";      Port = 8001 }
    "orchestration"  = @{ Module = "orchestration_service.main:app";  Port = 8002 }
    "notification"   = @{ Module = "notification_service.main:app";   Port = 8003 }
    "report"         = @{ Module = "report_service.main:app";         Port = 8004 }
    "gateway"        = @{ Module = "gateway_service.main:app";        Port = 8080 }
}

function Start-Service($name, $svc) {
    Write-Green "  Starting $name on port $($svc.Port)..."
    $logFile = Join-Path $ProjectRoot "logs\$name.log"
    New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "logs") | Out-Null

    $proc = Start-Process -FilePath $Venv `
        -ArgumentList "-m", "uvicorn", $svc.Module, "--host", "0.0.0.0", "--port", $svc.Port, "--reload" `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $logFile `
        -RedirectStandardError "$logFile.err" `
        -PassThru `
        -WindowStyle Hidden

    return $proc
}

function Stop-AllServices {
    Write-Yellow "Stopping all GST services..."
    Get-Process -Name "python" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -like "*uvicorn*" -or $_.CommandLine -like "*gst*"
    } | Stop-Process -Force -ErrorAction SilentlyContinue
    Write-Green "Done."
}

function Run-Tests {
    Write-Cyan "Running all tests..."
    & $Venv -m pytest `
        ingestion_service/tests/ `
        orchestration_service/tests/ `
        notification_service/tests/ `
        report_service/tests/ `
        gateway_service/tests/ `
        -v --tb=short
}

function Validate-Env {
    Write-Cyan "Validating .env configuration..."
    & $Venv (Join-Path $PSScriptRoot "validate_env.py")
}

function Show-Health {
    Write-Cyan "Checking service health..."
    $ports = @(8001, 8002, 8003, 8004, 8080)
    $names = @("Ingestion", "Orchestration", "Notification", "Report", "Gateway")

    for ($i = 0; $i -lt $ports.Length; $i++) {
        try {
            $resp = Invoke-WebRequest -Uri "http://localhost:$($ports[$i])/health" -TimeoutSec 2 -ErrorAction Stop
            Write-Green "  ✓ $($names[$i]) (port $($ports[$i])) — UP"
        } catch {
            Write-Red "  ✗ $($names[$i]) (port $($ports[$i])) — DOWN"
        }
    }
}

# ── Main dispatch ──────────────────────────────────────────

Write-Cyan "=============================================="
Write-Cyan "  GST Reconciliation Agent — Dev Launcher"
Write-Cyan "=============================================="

switch ($Command) {
    "validate" {
        Validate-Env
    }

    "test" {
        Run-Tests
    }

    "stop" {
        Stop-AllServices
    }

    "health" {
        Show-Health
    }

    "start" {
        # Validate env first
        & $Venv (Join-Path $PSScriptRoot "validate_env.py")
        if ($LASTEXITCODE -ne 0) {
            Write-Red "Environment validation failed. Fix your .env before starting."
            exit 1
        }

        New-Item -ItemType Directory -Force -Path (Join-Path $ProjectRoot "reports") | Out-Null

        if ($Service -and $Services.ContainsKey($Service)) {
            # Start single service
            $proc = Start-Service $Service $Services[$Service]
            Write-Green "Started $Service (PID $($proc.Id))"
            Write-Yellow "Logs: logs\$Service.log"
        } else {
            # Start all services
            Write-Cyan "`nStarting all 5 services...`n"
            $pids = @()
            foreach ($name in @("ingestion", "orchestration", "notification", "report", "gateway")) {
                $proc = Start-Service $name $Services[$name]
                $pids += $proc.Id
                Start-Sleep -Milliseconds 500
            }

            Write-Green "`n✅ All services started!"
            Write-Yellow "  PIDs: $($pids -join ', ')"
            Write-Yellow "  Logs: .\logs\"
            Write-Cyan "`nEndpoints:"
            Write-Host "  Gateway:       http://localhost:8080"
            Write-Host "  Ingestion:     http://localhost:8001/docs"
            Write-Host "  Orchestration: http://localhost:8002/docs"
            Write-Host "  Notification:  http://localhost:8003/docs"
            Write-Host "  Report:        http://localhost:8004/docs"
            Write-Yellow "`nRun '.\scripts\start_dev.ps1 stop' to stop all services."
        }
    }

    default {
        Write-Yellow "Usage: .\scripts\start_dev.ps1 [start|stop|test|validate|health] [service]"
    }
}
