# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path

assets = Path(SPECPATH) / "src" / "all_protocol" / "assets"
icon = assets / "app-icon.ico"
a = Analysis(
    ["src/all_protocol/__main__.py"],
    datas=[(str(assets / name), "all_protocol/assets")
           for name in ("app-icon.ico", "chevron-up.svg", "chevron-down.svg")],
)

# Qt on Windows links to the OS ICU ABI. PATH can expose a same-named ICU
# from another application that exports versioned symbols instead.
windows_icu = {"icu.dll", "icuin.dll", "icuuc.dll"}
a.binaries = [
    entry for entry in a.binaries
    if entry[0].lower() not in windows_icu and not entry[0].lower().startswith("icudt")
]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AllProtocol", console=False, icon=str(icon))
coll = COLLECT(exe, a.binaries, a.datas, name="AllProtocol")
