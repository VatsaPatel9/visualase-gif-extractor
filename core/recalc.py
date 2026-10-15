"""Optional: let LibreOffice compute the workbook formulas so file previewers (Finder Quick Look,
pandas, web viewers) show numbers too. Excel itself recalculates on open either way."""
import shutil
import subprocess
import tempfile
from pathlib import Path

MACRO = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE script:module PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "module.dtd">
<script:module xmlns:script="http://openoffice.org/2000/script" script:name="Module1" script:language="StarBasic">
    Sub RecalculateAndSave()
      ThisComponent.calculateAll()
      ThisComponent.store()
      ThisComponent.close(True)
    End Sub
</script:module>"""


def soffice_path():
    for p in (shutil.which('soffice'), shutil.which('libreoffice'),
              '/Applications/LibreOffice.app/Contents/MacOS/soffice', '/opt/homebrew/bin/soffice'):
        if p and Path(p).exists():
            return p
    return None


def recalc(xlsx_path, timeout=90):
    """Returns True if LibreOffice recalculated and saved the file, False otherwise (never raises)."""
    exe = soffice_path()
    if not exe:
        return False
    try:
        with tempfile.TemporaryDirectory(prefix='vx-lo-') as prof:
            url = Path(prof).as_uri()
            subprocess.run([exe, '--headless', '--terminate_after_init', f'-env:UserInstallation={url}'],
                           capture_output=True, timeout=timeout)
            mdir = Path(prof) / 'user' / 'basic' / 'Standard'
            if not mdir.exists():
                return False
            (mdir / 'Module1.xba').write_text(MACRO)
            before = Path(xlsx_path).stat().st_mtime_ns
            subprocess.run([exe, '--headless', '--norestore', f'-env:UserInstallation={url}',
                            'vnd.sun.star.script:Standard.Module1.RecalculateAndSave?language=Basic&location=application',
                            str(Path(xlsx_path).resolve())], capture_output=True, timeout=timeout)
            return Path(xlsx_path).stat().st_mtime_ns != before
    except Exception:
        return False
