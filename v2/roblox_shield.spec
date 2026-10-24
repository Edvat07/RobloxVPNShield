# PyInstaller spec: Roblox Shield and Roblox Guard as one folder that shares a single _internal.
# One folder, not --onefile: a onefile exe unpacks Python into %TEMP%\_MEI..., and temp cleaners or a restart
# could delete it while the app runs ("No such file ... base_library.zip").
# Build with: python -m PyInstaller --noconfirm --clean roblox_shield.spec  (build.bat does this)
from pathlib import Path

HERE = Path(SPECPATH)
ICON = str(HERE.parent / "app_icon.ico")

shield = Analysis([str(HERE / "app.py")], pathex=[str(HERE)])
guard = Analysis([str(HERE / "guard_app.py")], pathex=[str(HERE)])


def exe(analysis, name):
    return EXE(PYZ(analysis.pure), analysis.scripts, [], exclude_binaries=True, name=name, icon=ICON,
               console=False, uac_admin=True, upx=False)


coll = COLLECT(exe(shield, "RobloxShield"), shield.binaries, shield.datas,
               exe(guard, "RobloxGuard"), guard.binaries, guard.datas,
               name="RobloxShield", upx=False)
