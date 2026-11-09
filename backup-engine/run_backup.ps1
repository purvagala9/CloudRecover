
$ErrorActionPreference = "Stop"

# CloudRecover project paths
$ProjectDir = "C:\Users\Lenovo\Desktop\ccbdi_project\CloudRecover\backup-engine"
$Python = Join-Path $ProjectDir "venv\Scripts\python.exe"
$LogDir = Join-Path $ProjectDir "logs"
$LogFile = Join-Path $LogDir "backup.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# Send all output to the log file
"[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Backup job started" |
    Out-File -FilePath $LogFile -Append -Encoding utf8

try {
    Set-Location $ProjectDir

    # Select the AWS profile used by CloudRecover
    $env:AWS_PROFILE = "cloudrecover-dev"

    # Verify Docker is available
    & docker info 2>&1 |
        Out-File -FilePath $LogFile -Append -Encoding utf8

    if ($LASTEXITCODE -ne 0) {
        throw "Docker is unavailable. Start Docker Desktop and retry."
    }

    # Start MySQL if its container exists but is stopped
    $containerState = & docker inspect `
        --format '{{.State.Running}}' cloudrecover-mysql 2>&1

    if ($LASTEXITCODE -ne 0) {
        throw "MySQL container cloudrecover-mysql was not found."
    }

    if ($containerState -ne "true") {
        & docker start cloudrecover-mysql 2>&1 |
            Out-File -FilePath $LogFile -Append -Encoding utf8

        if ($LASTEXITCODE -ne 0) {
            throw "Could not start the MySQL container."
        }
    }

    # Wait briefly for MySQL to initialize
    $ready = $false

    for ($i = 0; $i -lt 30; $i++) {
        & docker exec cloudrecover-mysql `
            mysqladmin ping -h 127.0.0.1 `
            --silent 2>$null | Out-Null

        if ($LASTEXITCODE -eq 0) {
            $ready = $true
            break
        }

        Start-Sleep -Seconds 2
    }

    if (-not $ready) {
        throw "MySQL did not become ready within 60 seconds."
    }

    # Run the existing Python backup engine
    & $Python backup.py 2>&1 |
        Out-File -FilePath $LogFile -Append -Encoding utf8

    if ($LASTEXITCODE -ne 0) {
        throw "Python backup engine failed."
    }

    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] BACKUP JOB SUCCEEDED" |
        Out-File -FilePath $LogFile -Append -Encoding utf8
}
catch {
    "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] BACKUP JOB FAILED: $_" |
        Out-File -FilePath $LogFile -Append -Encoding utf8

    exit 1
}
