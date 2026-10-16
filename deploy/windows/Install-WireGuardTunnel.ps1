[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ConfigPath,
    [Parameter(Mandatory)][string]$GatewayAddress,
    [Parameter(Mandatory)][string]$LocalAddress,
    [ValidateRange(1,65535)][int]$ApiPort = 8080,
    [string]$MsiPath,
    [string]$WireGuardPath = "$env:ProgramFiles\WireGuard\wireguard.exe"
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script in an elevated PowerShell session.'
}
foreach ($value in $GatewayAddress,$LocalAddress) { [void][Net.IPAddress]::Parse($value) }
if (-not (Test-Path -LiteralPath $WireGuardPath)) {
    $signature = Get-AuthenticodeSignature -LiteralPath $MsiPath
    if ($signature.Status -ne 'Valid' -or $signature.SignerCertificate.Subject -notmatch 'WireGuard') {
        throw 'WireGuard MSI requires a valid WireGuard publisher signature.'
    }
    $install = Start-Process msiexec.exe -ArgumentList @('/i',"`"$MsiPath`"",'/qn','DO_NOT_LAUNCH=1',
        '/norestart') -WindowStyle Hidden -PassThru -Wait
    if ($install.ExitCode -notin 0,3010) { throw "WireGuard MSI failed: $($install.ExitCode)" }
}
$ConfigPath = (Resolve-Path -LiteralPath $ConfigPath).Path
$name = [IO.Path]::GetFileNameWithoutExtension($ConfigPath)
if ($name -notmatch '^[A-Za-z0-9_=+.-]{1,32}$') { throw 'Invalid WireGuard tunnel name.' }
$acl = New-Object Security.AccessControl.FileSecurity
$acl.SetAccessRuleProtection($true, $false)
foreach ($sidText in 'S-1-5-18','S-1-5-32-544') {
    $sid = New-Object Security.Principal.SecurityIdentifier($sidText)
    $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $sid,'FullControl','Allow')))
}
Set-Acl -LiteralPath $ConfigPath -AclObject $acl
& $WireGuardPath /installtunnelservice $ConfigPath
if ($LASTEXITCODE -ne 0) { throw 'WireGuard tunnel installation failed.' }
$ruleName = "TerminalMCP-WireGuard-$name"
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) {
    throw 'Firewall rule already exists; review its current scope.'
}
New-NetFirewallRule -Name $ruleName -DisplayName $ruleName -Direction Inbound -Action Allow `
    -Protocol TCP -LocalPort $ApiPort -LocalAddress $LocalAddress -RemoteAddress $GatewayAddress `
    -Profile Any | Out-Null
Get-Service -Name "WireGuardTunnel`$$name" | Select-Object Name,Status,StartType
