# Keep raw arguments: advanced-script common parameters would consume Docker's -d.
$ComposeArgs = @($args)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path $PSScriptRoot -Parent
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
exit $LASTEXITCODE
