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
$ast = [System.Management.Automation.Language.Parser]::ParseFile($env:TMCP_SCRIPT, [ref]$null, [ref]$null)
$function = $ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] `
    -and $n.Name -eq 'Invoke-Checked'}, $true)
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
