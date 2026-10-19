"""Actual Windows PowerShell 5.1 native stderr and exit-code handling."""

import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("exit_code", [0, 7])
def test_installer_uses_native_exit_code_with_stderr(tmp_path, exit_code):
    script = Path(__file__).resolve().parents[2] / "deploy/windows/Manage-TerminalMCP.ps1"
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    child = tmp_path / "child.ps1"
    child.write_text(f"[Console]::Error.WriteLine('diagnostic'); exit {exit_code}\n")
    command = r"""
$ErrorActionPreference = 'Stop'
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:TMCP_SCRIPT, [ref]$null, [ref]$null)
$functionType = [System.Management.Automation.Language.FunctionDefinitionAst]
$function = $ast.Find({param($n) $n -is $functionType -and $n.Name -eq 'Invoke-Checked'}, $true)
Invoke-Expression $function.Extent.Text
try {
    Invoke-Checked $env:TMCP_SHELL @('-NoProfile','-NonInteractive','-File',
        $env:TMCP_CHILD) *> $env:TMCP_LOG
    exit 0
} catch { Write-Output $_.Exception.Message; exit 11 }
"""
    result = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
        env={
            **os.environ,
            "TMCP_SCRIPT": str(script),
            "TMCP_SHELL": str(shell),
            "TMCP_CHILD": str(child),
            "TMCP_LOG": str(tmp_path / "install.log"),
        },
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == (0 if exit_code == 0 else 11), (result.stdout, result.stderr)
    assert "diagnostic" in (tmp_path / "install.log").read_text(encoding="utf-16")


@pytest.mark.parametrize("first_ready,first_release", [(False, "expected"), (True, "old")])
def test_installer_health_uses_persisted_bind_address_and_port(
    tmp_path, first_ready, first_release
):
    script = Path(__file__).resolve().parents[2] / "deploy/windows/Manage-TerminalMCP.ps1"
    shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    config = tmp_path / "config"
    config.mkdir()
    (config / "terminal-mcp.env").write_text(
        'TERMINAL_MCP_HOST="10.77.17.2"\nTERMINAL_MCP_PORT="18080"\n'
    )
    command = r"""
$ErrorActionPreference = 'Stop'
$Root = $env:TMCP_ROOT
$ListenHost = '127.0.0.1'
$Port = 8080
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $env:TMCP_SCRIPT, [ref]$null, [ref]$null)
$functionType = [System.Management.Automation.Language.FunctionDefinitionAst]
$function = $ast.Find({param($n) $n -is $functionType -and $n.Name -eq 'Wait-Healthy'}, $true)
Invoke-Expression $function.Extent.Text
function Invoke-RestMethod([string]$Uri, [int]$TimeoutSec) {
    $script:observed = $Uri
    $script:calls += 1
    if ($script:calls -eq 1) {
        return @{ok=$true; ready=($env:TMCP_FIRST_READY -eq 'True');
            release=$env:TMCP_FIRST_RELEASE}
    }
    return @{ok=$true; ready=$true; release='expected'}
}
function Start-Sleep([int]$Milliseconds) { }
$script:calls = 0
Wait-Healthy -ExpectedRelease 'expected'
if ($script:calls -ne 2) { exit 10 }
if ($script:observed -ne 'http://10.77.17.2:18080/health/ready') { exit 9 }
"""
    result = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", command],
        env={
            **os.environ,
            "TMCP_SCRIPT": str(script),
            "TMCP_ROOT": str(tmp_path),
            "TMCP_FIRST_READY": str(first_ready),
            "TMCP_FIRST_RELEASE": first_release,
        },
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
