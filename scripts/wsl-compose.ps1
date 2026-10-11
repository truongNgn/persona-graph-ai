# Keep raw arguments: advanced-script common parameters would consume Docker's -d.
$ComposeArgs = @($args)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
# A foreground WSL client keeps the distro alive when Linux systemd services
# alone do not prevent VM idle shutdown. Manage only this project's helper.
$keepAliveFile = Join-Path $projectRoot 'backend/data/wsl-keepalive.pid'
if ($ComposeArgs -contains 'up') {
    $keepAliveProcess = $null
    if (Test-Path -LiteralPath $keepAliveFile) {
        $keepAliveId = Get-Content -LiteralPath $keepAliveFile -ErrorAction SilentlyContinue
        if ($keepAliveId -match '^\d+$') {
            $keepAliveProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$keepAliveId" -ErrorAction SilentlyContinue
        }
    }
    if (!$keepAliveProcess -or $keepAliveProcess.Name -ne 'wsl.exe' -or $keepAliveProcess.CommandLine -notmatch '/usr/bin/sleep infinity') {
        $keepAliveProcess = Start-Process wsl.exe -ArgumentList @('-d', 'Ubuntu', '-u', 'root', '--exec', '/usr/bin/sleep', 'infinity') -WindowStyle Hidden -PassThru
        New-Item -ItemType Directory -Force -Path (Split-Path $keepAliveFile) | Out-Null
        Set-Content -LiteralPath $keepAliveFile -Value $keepAliveProcess.Id
    }
}
$resolvedPath = & wsl.exe -d Ubuntu --exec /usr/bin/wslpath -a $projectRoot.Replace('\', '/')
if ($LASTEXITCODE -ne 0 -or !$resolvedPath) { throw 'Unable to resolve the project path in Ubuntu.' }
$wslProjectRoot = $resolvedPath.Trim()
$extraFiles = @()
if ($ComposeArgs -contains 'build' -or $ComposeArgs -contains '--build') {
    $overrideFile = & wsl.exe -d Ubuntu -u root --exec bash "$wslProjectRoot/scripts/stage-wsl-build.sh"
    if ($LASTEXITCODE -ne 0) { throw 'Unable to stage the Linux build context.' }
    $extraFiles = @('-f', "$wslProjectRoot/docker-compose.yml", '-f', $overrideFile.Trim())
}
& wsl.exe -d Ubuntu -u root --cd $wslProjectRoot --exec /usr/bin/docker --host unix:///run/persona-docker.sock compose @extraFiles @ComposeArgs
$composeExitCode = $LASTEXITCODE
if ($ComposeArgs -contains 'down' -and $composeExitCode -eq 0 -and (Test-Path -LiteralPath $keepAliveFile)) {
    $keepAliveId = Get-Content -LiteralPath $keepAliveFile
    if ($keepAliveId -match '^\d+$') {
        $keepAliveProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$keepAliveId" -ErrorAction SilentlyContinue
        if ($keepAliveProcess.Name -eq 'wsl.exe' -and $keepAliveProcess.CommandLine -match '/usr/bin/sleep infinity') {
            Stop-Process -Id $keepAliveId -ErrorAction SilentlyContinue
        }
    }
    Remove-Item -LiteralPath $keepAliveFile
}
exit $composeExitCode
