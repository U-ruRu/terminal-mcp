[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidateSet('Install','CleanInstall','Update','Resume','Rollback','Status','Uninstall')]
    [string]$Action,
    [string]$Root = "$env:ProgramData\TerminalMCP",
    [string]$SourcePath = (Resolve-Path "$PSScriptRoot\..\.."),
    [string]$Python = 'python.exe',
    [string]$WinSWPath,
    [string]$WinSWSha256,
    [string]$ExpectedCommit,
    [string]$PublicUrl,
    [string]$TunnelName,
    [string]$ListenHost = '127.0.0.1',
    [ValidateRange(1,65535)][int]$Port = 8080
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-Checked([string]$File, [string[]]$Arguments) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $File @Arguments 2>&1 | ForEach-Object { $_.ToString() }
        $exitCode = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousPreference }
    if ($exitCode -ne 0) { throw "Operation failed: $File (exit $exitCode)" }
}
function Assert-Administrator {
    $principal = New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this script in an elevated PowerShell session.'
    }
}
function Protect-Directory([string]$Path) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
    if ((Get-Item -LiteralPath $Path).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Service directory requires a regular local path: $Path"
    }
    $acl = New-Object Security.AccessControl.DirectorySecurity
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sidText in 'S-1-5-18','S-1-5-32-544') {
        $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
        $rule = New-Object Security.AccessControl.FileSystemAccessRule(
            $sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
function Wait-Healthy {
    param([string]$ExpectedRelease = '')
    foreach ($line in [IO.File]::ReadLines((Join-Path $Root 'config\terminal-mcp.env'))) {
        if ($line -match '^TERMINAL_MCP_HOST=(.*)$') {
            $ListenHost = $Matches[1].Trim().Trim('"').Trim("'")
        } elseif ($line -match '^TERMINAL_MCP_PORT=(.*)$') {
            $Port = [int]$Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    $healthHost = if ($ListenHost -in '0.0.0.0','127.0.0.1') { '127.0.0.1' } else { $ListenHost }
    if ($healthHost -eq '::') { $healthHost = '::1' }
    if ($healthHost.Contains(':')) { $healthHost = "[$healthHost]" }
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        try {
            $health = Invoke-RestMethod "http://${healthHost}:$Port/health/ready" -TimeoutSec 3
            if ($health.ready -and (-not $ExpectedRelease -or $health.release -eq $ExpectedRelease)) { return }
        } catch { }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Windows service health deadline expired.'
}

$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($Root -eq [IO.Path]::GetPathRoot($Root).TrimEnd('\')) {
    throw 'Service root requires a dedicated directory.'
}
$wrapper = Join-Path $Root 'service\TerminalMCP.exe'
$xmlPath = Join-Path $Root 'service\TerminalMCP.xml'
$statePath = Join-Path $Root 'service\release.json'
if ($Action -eq 'Status') {
    Get-Service TerminalMCP -ErrorAction SilentlyContinue | Select-Object Name,Status,StartType
    return
}
Assert-Administrator
if ($Action -eq 'Uninstall') {
    Invoke-Checked $wrapper @('stop')
    Invoke-Checked $wrapper @('uninstall')
    Get-ScheduledTask -TaskName 'TerminalMCP-Maintenance' -ErrorAction SilentlyContinue |
        Unregister-ScheduledTask -Confirm:$false
    return
}
# Hold one exclusive handle across staging, stop, backup and activation.
Protect-Directory $Root
$lock = [IO.File]::Open((Join-Path $Root 'deployment.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
try {
    if ($Action -eq 'Resume') {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if (-not $state.pendingPython -or -not $state.backup) { throw 'Pending update is required.' }
        Invoke-Checked $state.pendingPython @('-m','terminal_mcp.deployment.windows',
            'check-migration','--root',$Root,'--output',$state.backup)
        Invoke-Checked $state.pendingPython @('-m','terminal_mcp.deployment.windows','render',
            '--root',$Root,'--python',$state.pendingPython,'--output',$xmlPath)
        Invoke-Checked $wrapper @('start')
        $metaPath = Join-Path (Split-Path (Split-Path (Split-Path $state.pendingPython))) 'RELEASE_META.json'
        $metadata = Get-Content -LiteralPath $metaPath -Raw | ConvertFrom-Json
        Wait-Healthy -ExpectedRelease $metadata.release_id
        Invoke-Checked $state.pendingPython @('-m','terminal_mcp.deployment.windows_runtime',
            'configure','--root',$Root,'--python',$state.pendingPython)
        @{ currentPython=$state.pendingPython; previousPython=$state.currentPython;
            commit=$state.pendingCommit; backup=$state.backup; phase='applied' } |
            ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
        return
    }
    if ($Action -eq 'Rollback') {
        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        if (-not $state.previousPython) { throw 'Previous release is required.' }
        Invoke-Checked $state.currentPython @('-m','terminal_mcp.deployment.windows',
            'check-rollback','--root',$Root,'--output',$state.backup)
        Invoke-Checked $wrapper @('stop')
        Invoke-Checked $state.previousPython @('-m','terminal_mcp.deployment.windows','render',
            '--root',$Root,'--python',$state.previousPython,'--output',$xmlPath)
        Invoke-Checked $wrapper @('start')
        Wait-Healthy
        Invoke-Checked $state.previousPython @('-m','terminal_mcp.deployment.windows_runtime',
            'configure','--root',$Root,'--python',$state.previousPython)
        $old = $state.currentPython
        $state.currentPython = $state.previousPython
        $state.previousPython = $old
        $state.commit = (Get-Item -LiteralPath $state.currentPython).Directory.Parent.Parent.Name
        $state | ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
        return
    }
    if ($ExpectedCommit -notmatch '^[a-f0-9]{40}$') { throw 'ExpectedCommit requires a full SHA.' }
    $SourcePath = (Resolve-Path -LiteralPath $SourcePath).Path
    $actual = (& git -C $SourcePath rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $actual -ne $ExpectedCommit) { throw 'Source SHA mismatch.' }
    $dirty = & git -C $SourcePath status --porcelain
    if ($LASTEXITCODE -ne 0 -or $dirty) { throw 'Release source requires a clean checkout.' }
    $existing = Get-Service TerminalMCP -ErrorAction SilentlyContinue
    if ($Action -eq 'Install' -and $existing) { throw 'Use Update for an installed service.' }
    if ($Action -eq 'Update' -and -not $existing) { throw 'Install the service first.' }
    $previousPython = $null
    if ($existing) {
        $oldState = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        $previousPython = $oldState.currentPython
    }
    if (-not $existing -or $Action -eq 'CleanInstall') {
        if (-not $PublicUrl) { throw 'PublicUrl is required for installation.' }
        if ($WinSWSha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'WinSW checksum is required.' }
        $observed = (Get-FileHash -LiteralPath $WinSWPath -Algorithm SHA256).Hash
        if ($observed -ne $WinSWSha256) { throw 'WinSW checksum mismatch.' }
    }
    foreach ($name in 'service','releases','work','logs') {
        Protect-Directory (Join-Path $Root $name)
    }
    $releaseId = $ExpectedCommit
    if ($Action -eq 'CleanInstall') {
        $releaseId += '-clean-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ')
    }
    $release = Join-Path $Root "releases\$releaseId"
    if (Test-Path -LiteralPath $release) { throw 'Release directory already exists.' }
    Protect-Directory $release
    $sourceCopy = Join-Path $release 'source'
    New-Item -ItemType Directory -Path $sourceCopy | Out-Null
    $archive = Join-Path $release 'source.tar'
    Invoke-Checked 'git' @('-C',$SourcePath,'archive','--format=tar',"--output=$archive",$ExpectedCommit)
    Invoke-Checked 'tar.exe' @('-xf',$archive,'-C',$sourceCopy)
    $versionArguments = @((Join-Path $sourceCopy 'scripts\release_version.py'),
        '--source',$sourceCopy,'--sha',$ExpectedCommit,'--stamp',
        '--output',(Join-Path $release 'RELEASE_META.json'))
    if ($previousPython) {
        $previousVenv = (Get-Item -LiteralPath $previousPython).Directory.Parent.FullName
        $versionArguments += @('--prior-package',(Join-Path $previousVenv 'Lib\site-packages\terminal_mcp'))
    }
    Invoke-Checked $Python $versionArguments
    Invoke-Checked $Python @('-m','venv',(Join-Path $release 'venv'))
    $runtime = Join-Path $release 'venv\Scripts\python.exe'
    Invoke-Checked $runtime @('-m','pip','install',"$sourceCopy`[compact`]")
    Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','probe','--root',$Root)
    if ($Action -eq 'Install') {
        Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','check-install',
            '--root',$Root,'--public-url',$PublicUrl)
    }
    if ($Action -eq 'CleanInstall') {
        Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','clean',
            '--root',$Root,'--public-url',$PublicUrl)
        if ($existing) {
            Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','check-idle','--root',$Root)
            Invoke-Checked $wrapper @('stop')
            Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','check-idle','--root',$Root)
            Invoke-Checked $wrapper @('uninstall')
        }
        Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','clean',
            '--root',$Root,'--public-url',$PublicUrl,'--apply')
        $existing = $null
        $previousPython = $null
    }
    if (-not $existing) {
        Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','initialize',
            '--root',$Root,'--public-url',$PublicUrl,'--host',$ListenHost,'--port',"$Port")
        Copy-Item -LiteralPath $WinSWPath -Destination $wrapper
    }
    $backup = $null
    $activated = $false
    $migrationStarted = $false
    try {
        $taskArguments = @('-m','terminal_mcp.deployment.windows_runtime','configure',
            '--root',$Root,'--python',$runtime)
        if ($PSBoundParameters.ContainsKey('TunnelName')) { $taskArguments += @('--tunnel-name',$TunnelName) }
        Invoke-Checked $runtime $taskArguments
        if ($existing) {
            Invoke-Checked $wrapper @('stop')
            $backup = Join-Path $Root ('backups\' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
            Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','backup',
                '--root',$Root,'--output',$backup)
            @{ currentPython=$previousPython; previousPython=$null; commit=$oldState.commit;
                pendingPython=$runtime; pendingCommit=$ExpectedCommit; backup=$backup;
                phase='migrating' } | ConvertTo-Json |
                Set-Content -LiteralPath $statePath -Encoding UTF8
            $migrationStarted = $true
            Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','migrate',
                '--root',$Root,'--output',$backup)
            Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','check-migration',
                '--root',$Root,'--output',$backup)
        }
        Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','render',
            '--root',$Root,'--python',$runtime,'--output',$xmlPath)
        if (-not $existing) { Invoke-Checked $wrapper @('install') }
        Invoke-Checked $wrapper @('start')
        $activated = $true
        $metadata = Get-Content -LiteralPath (Join-Path $release 'RELEASE_META.json') -Raw | ConvertFrom-Json
        Wait-Healthy -ExpectedRelease $metadata.release_id
    } catch {
        if ($migrationStarted) {
            throw 'Update requires forward recovery. Inspect the migration receipt and use Resume after a completed migration.'
        }
        if ($previousPython) {
            & $wrapper stop | Out-Null
            if ($activated) {
                Invoke-Checked $runtime @('-m','terminal_mcp.deployment.windows','check-rollback',
                    '--root',$Root,'--output',$backup)
            }
            Invoke-Checked $previousPython @('-m','terminal_mcp.deployment.windows','render',
                '--root',$Root,'--python',$previousPython,'--output',$xmlPath)
            Invoke-Checked $wrapper @('start')
        }
        throw
    }
    @{ currentPython=$runtime; previousPython=$previousPython; backup=$backup; commit=$ExpectedCommit; phase='applied' } |
        ConvertTo-Json | Set-Content -LiteralPath $statePath -Encoding UTF8
    Get-Service TerminalMCP | Select-Object Name,Status,StartType
} finally {
    $lock.Dispose()
}
