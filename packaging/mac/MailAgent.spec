# PyInstaller recipe for "Mail Agent.app" (Apple Silicon). Build with packaging/mac/build-app.command
# or:  python -m PyInstaller --noconfirm --clean packaging/mac/MailAgent.spec
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).parent.parent
VERSION = re.search(r'__version__ = "([^"]+)"', (ROOT / "email_agent" / "__init__.py").read_text()).group(1)

a = Analysis(
    [str(ROOT / "packaging" / "mac" / "launcher.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "email_agent" / "web"), "email_agent/web")],
    hiddenimports=collect_submodules("email_agent"),
    excludes=["tkinter", "unittest", "pydoc", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="Mail Agent", console=False,
          target_arch="arm64", codesign_identity=None)
coll = COLLECT(exe, a.binaries, a.datas, name="Mail Agent")
app = BUNDLE(
    coll,
    name="Mail Agent.app",
    icon=str(ROOT / "packaging" / "mac" / "icon.icns"),
    bundle_identifier="dev.karska.mailagent",
    version=VERSION,
    info_plist={
        "CFBundleDisplayName": "Mail Agent",
        "CFBundleShortVersionString": VERSION,
        "LSUIElement": True,            # no Dock icon: the app lives in your browser tab
        "LSMinimumSystemVersion": "12.0",
        "NSHumanReadableCopyright": "MIT License, Karska-dev",
    },
)
