<#
.SYNOPSIS
  Renew the Tailscale (Let's Encrypt) TLS certificate Jarvis serves the phone
  PWA and the geofence presence webhook over.

.WHY THIS EXISTS
  The cert was issued once on 2026-05-19 and nothing renewed it. Let's Encrypt
  certs are 90 days, so it expired on 2026-08-17 and the ONLY symptom was an
  iOS error on the user's phone:

      "The certificate for this server is invalid. You might be connecting to
       a server that is pretending to be hpdesktop.taild603df.ts.net"

  Nothing in jarvis.log said a word, because the failure is entirely on the
  client side of a TLS handshake. That is the same silent-failure shape as the
  M100 mute Announcer: a capability stops working and produces no symptom
  anyone can report from this machine. Hence automation plus a doctor check.

.WHAT IT DOES
  Reads the cert/key paths straight out of .env (so it can never drift from
  what Jarvis actually loads), checks the expiry, and renews only when inside
  the threshold. Safe to run daily; a no-op on most days.

.PARAMETER DaysBefore
  Renew when fewer than this many days remain. Default 30 — comfortably inside
  Let's Encrypt's 90-day window, and leaves three weeks of slack for a machine
  that happens to be off.

.PARAMETER Force
  Renew regardless of remaining days.

.EXAMPLE
  pwsh -File scripts\renew_tls_cert.ps1
  pwsh -File scripts\renew_tls_cert.ps1 -Force
  pwsh -File scripts\renew_tls_cert.ps1 -Install    # register the daily task
#>
[CmdletBinding()]
param(
    [int]$DaysBefore = 30,
    [switch]$Force,
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile  = Join-Path $RepoRoot '.env'
$LogDir   = Join-Path $env:LOCALAPPDATA 'Jarvis'
$LogFile  = Join-Path $LogDir 'tls_renew.log'

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    Write-Host $line
    try {
        if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
        Add-Content -Path $LogFile -Value $line -Encoding utf8
    } catch { }   # logging must never be the thing that fails the renewal
}

# --- register the scheduled task and exit ---------------------------------
if ($Install) {
    $pwsh = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
    if (-not $pwsh) { $pwsh = (Get-Command powershell).Source }
    $action  = New-ScheduledTaskAction -Execute $pwsh `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
    # Daily. NOT /sc onstart: `tailscale cert` talks to the local tailscaled
    # LocalAPI as the logged-in user, and a Session-0 SYSTEM task is a poor fit
    # for that (cf. the Session-0 CUDA gotcha this project already hit once).
    $trigger  = New-ScheduledTaskTrigger -Daily -At 3:30am
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
        -DontStopOnIdleEnd -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
    Register-ScheduledTask -TaskName 'JarvisTlsCertRenew' -Action $action `
        -Trigger $trigger -Settings $settings -Description `
        'Renew the Tailscale TLS cert Jarvis serves the phone PWA over.' -Force | Out-Null
    Write-Log "installed scheduled task 'JarvisTlsCertRenew' (daily 03:30, runs as $env:USERNAME)"
    exit 0
}

# --- resolve paths from .env ----------------------------------------------
if (-not (Test-Path $EnvFile)) { Write-Log "no .env at $EnvFile - nothing to do"; exit 0 }

$certPath = $null; $keyPath = $null
foreach ($line in Get-Content $EnvFile) {
    if ($line -match '^\s*JARVIS_TLS_CERT_FILE\s*=\s*(.+?)\s*$') { $certPath = $Matches[1] }
    if ($line -match '^\s*JARVIS_TLS_KEY_FILE\s*=\s*(.+?)\s*$')  { $keyPath  = $Matches[1] }
}
if (-not $certPath -or -not $keyPath) {
    Write-Log "JARVIS_TLS_CERT_FILE / _KEY_FILE not set - TLS is off, nothing to renew"
    exit 0
}

# The Tailscale hostname IS the cert filename stem, by how it was provisioned.
$hostName = [System.IO.Path]::GetFileNameWithoutExtension($certPath)

# --- decide -----------------------------------------------------------------
$needed = $true
if ((Test-Path $certPath) -and -not $Force) {
    try {
        $c = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($certPath)
        $daysLeft = [math]::Round(($c.NotAfter - (Get-Date)).TotalDays, 1)
        if ($daysLeft -gt $DaysBefore) {
            Write-Log "cert valid for $daysLeft more days (threshold $DaysBefore) - no action"
            $needed = $false
        } else {
            Write-Log "cert has $daysLeft days left (threshold $DaysBefore) - renewing"
        }
    } catch {
        Write-Log "could not read existing cert ($($_.Exception.Message)) - renewing"
    }
} elseif ($Force) {
    Write-Log "-Force given - renewing regardless"
} else {
    Write-Log "no cert file at $certPath - issuing"
}
if (-not $needed) { exit 0 }

# --- renew ------------------------------------------------------------------
$ts = (Get-Command tailscale -ErrorAction SilentlyContinue).Source
if (-not $ts) { $ts = 'C:\Program Files\Tailscale\tailscale.exe' }
if (-not (Test-Path $ts)) { Write-Log "tailscale.exe not found - cannot renew"; exit 1 }

# Keep the outgoing pair until the new one verifies: a half-written cert would
# take the phone client down harder than an expired one.
$backup = Join-Path (Split-Path $certPath) ("previous-" + (Get-Date -Format 'yyyyMMdd'))
try {
    if (Test-Path $certPath) {
        New-Item -ItemType Directory -Force -Path $backup | Out-Null
        Copy-Item $certPath, $keyPath -Destination $backup -Force -ErrorAction SilentlyContinue
    }
} catch { }

& $ts cert --cert-file $certPath --key-file $keyPath $hostName 2>&1 | ForEach-Object { Write-Log "  tailscale: $_" }
if ($LASTEXITCODE -ne 0) { Write-Log "RENEWAL FAILED (exit $LASTEXITCODE) - old cert left in place"; exit 1 }

$c2 = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($certPath)
Write-Log ("renewed: valid {0} -> {1} ({2} days)" -f `
    $c2.NotBefore.ToString('yyyy-MM-dd'), $c2.NotAfter.ToString('yyyy-MM-dd'),
    [math]::Round(($c2.NotAfter - (Get-Date)).TotalDays, 0))

# Jarvis builds its SSLContext ONCE at startup (src/bootstrap.py), so a running
# instance keeps serving the OLD cert until it restarts. Say so loudly rather
# than leaving a renewed-but-not-applied cert looking like success.
$running = Get-Process pythonw -ErrorAction SilentlyContinue
if ($running) {
    Write-Log "NOTE: Jarvis is running and loaded the previous cert at startup - restart it (tray > Restart Jarvis) for the new cert to take effect"
} else {
    Write-Log "Jarvis is not running - the new cert will be picked up on next start"
}
exit 0
