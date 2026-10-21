"""Start-with-Windows via Task Scheduler.

A Run-key entry cannot be used: Windows silently skips programs that require elevation at logon. A logon task
with RunLevel=HighestAvailable starts the app already elevated, with no UAC prompt.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.sax.saxutils import escape

AUTOSTART_ARG = "--autostart"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Task Scheduler defaults would kill the app after 72 h and skip it on battery, so both are overridden.
_TASK_XML = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{description}</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId><Delay>PT10S</Delay></LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["schtasks", *args], capture_output=True, text=True, creationflags=_NO_WINDOW)


def _launch_command() -> tuple[str, str]:
    if getattr(sys, "frozen", False):
        return sys.executable, AUTOSTART_ARG
    # Running from source: pythonw avoids a console window at logon.
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw if pythonw.is_file() else sys.executable), f'"{Path(sys.argv[0]).resolve()}" {AUTOSTART_ARG}'


def is_enabled(task_name: str) -> bool:
    return _schtasks("/Query", "/TN", task_name).returncode == 0


def enable(task_name: str, description: str) -> None:
    """Create or refresh the logon task (refreshing keeps the path right if the app folder moved)."""
    command, arguments = _launch_command()
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}"
    xml = _TASK_XML.format(
        description=escape(description), user=escape(user), command=escape(f'"{command}"'),
        arguments=escape(arguments), workdir=escape(str(Path(command).parent)),
    )
    fd, path = tempfile.mkstemp(suffix=".xml")
    try:
        with os.fdopen(fd, "w", encoding="utf-16") as f:
            f.write(xml)
        result = _schtasks("/Create", "/TN", task_name, "/XML", path, "/F")
    finally:
        os.unlink(path)
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout).strip() or "schtasks /Create thất bại")


def disable(task_name: str) -> None:
    if not is_enabled(task_name):
        return
    result = _schtasks("/Delete", "/TN", task_name, "/F")
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout).strip() or "schtasks /Delete thất bại")
