# -*- mode: python ; coding: utf-8 -*-

a = Analysis(["src/all_protocol/__main__.py"])

# Qt on Windows links to the OS ICU ABI. PATH can expose a same-named ICU
# from another application that exports versioned symbols instead.
windows_icu = {"icu.dll", "icuin.dll", "icuuc.dll"}
a.binaries = [
    entry for entry in a.binaries
    if entry[0].lower() not in windows_icu and not entry[0].lower().startswith("icudt")
]

pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AllProtocol", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="AllProtocol")
