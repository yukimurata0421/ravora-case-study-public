#requires -Version 7.0
[CmdletBinding()]
param(
    [Parameter(Mandatory, Position=0)]
    [ValidatePattern('^[a-zA-Z0-9_][a-zA-Z0-9_.@-]*$')]
    [string]$RayHost,

    [ValidateSet('twitch', 'youtube')]
    [string]$Destination = 'twitch'
)

$ErrorActionPreference = 'Stop'
$Destination = $Destination.ToLowerInvariant()
$port = if ($Destination -eq 'twitch') { 8793 } else { 8790 }
$forward = "127.0.0.1:${port}:127.0.0.1:${port}"

function Get-RayTunnel {
    $listeners = @(Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue |
        Where-Object LocalAddress -in '127.0.0.1', '0.0.0.0', '::')
    if (!$listeners.Count) { return }
    $executable = '^(?:"[^"]*ssh\.exe"|[^"\s]*ssh(?:\.exe)?)\s+'
    $options = '(?:(?:-[NTtnf]+|-o\s+(?:"[^"]+"|[^"\s]+))\s+)*'
    $route = '-L\s*"?' + [regex]::Escape($forward) + '"?\s+'
    $destinationHost = '"?' + [regex]::Escape($RayHost) + '"?(?:\s|$)'
    if ($listeners.Count -eq 1 -and $listeners[0].LocalAddress -eq '127.0.0.1') {
        $owner = Get-CimInstance Win32_Process -Filter "ProcessId = $($listeners[0].OwningProcess)"
        if ($owner.Name -eq 'ssh.exe' -and $owner.CommandLine -match ($executable + $options + $route + $destinationHost)) {
            return $listeners[0].OwningProcess
        }
    }
    throw "Port $port is already in use by another listener."
}

$tunnel = $null
try {
    if (!(Get-RayTunnel)) {
        $arguments = @('-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
            '-o', 'ServerAliveInterval=20', '-o', 'ServerAliveCountMax=3', '-L', $forward, $RayHost)
        $tunnel = Start-Process -FilePath 'ssh.exe' -ArgumentList $arguments -PassThru
        $wait = [Diagnostics.Stopwatch]::StartNew()
        while (!(Get-RayTunnel)) {
            if ($tunnel.HasExited) { throw 'Could not open the SSH tunnel. Check key authentication and the host alias.' }
            if ($wait.Elapsed.TotalSeconds -ge 30) { throw 'Timed out opening the SSH tunnel.' }
            Start-Sleep -Milliseconds 350
        }
    }
    $remote = 'sh "$HOME/ray-studio/ray-studio-console.sh" ' + $Destination
    $reply = @(ssh.exe -T $RayHost $remote)
    if ($LASTEXITCODE -ne 0) { throw 'The studio did not provide an operator endpoint.' }
    $pattern = '\Ahttp://127\.0\.0\.1:' + $port + '/#([A-Za-z0-9_-]{43})\z'
    if ($reply.Count -ne 1 -or $reply[0] -cnotmatch $pattern) { throw 'Invalid studio operator endpoint.' }
    $operatorUrl = $reply[0]
    $headers = @{ Authorization = 'Bearer ' + $Matches[1] }
    if (!(Get-RayTunnel)) { throw 'The SSH tunnel closed before the connection was ready.' }
    try {
        $status = Invoke-RestMethod -Uri "http://127.0.0.1:$port/v1/status" -Headers $headers -NoProxy -MaximumRedirection 0 -TimeoutSec 5
        if ($status.mic_source -notin 'browser', 'dell') { throw 'Invalid studio response.' }
    } catch {
        throw 'The studio did not respond over the SSH tunnel.'
    }
    Start-Process -FilePath $operatorUrl
} catch {
    if ($tunnel -and !$tunnel.HasExited) { $tunnel.Kill() }
    throw
}
