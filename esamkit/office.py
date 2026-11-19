"""
LibreOffice as an optional converter (never required).

ESAM reads .docx and .doc itself. When LibreOffice is installed it is used
for what the built-in readers cannot do: a full conversion of legacy .doc
and .rtf files (automatic numbering, vector pictures) and turning EMF/WMF
pictures into PNG. It runs headless, with a throw-away profile, on a copy of
the file in a temporary folder, and macros are not executed in this mode.
"""

import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import Settings, settings as default_settings

_CANDIDATES = ("soffice", "libreoffice", "/Applications/LibreOffice.app/Contents/MacOS/soffice",
               r"C:\Program Files\LibreOffice\program\soffice.exe")


def soffice(s: Settings = default_settings) -> str | None:
    """The soffice program to use, or None if LibreOffice is not installed."""
    if s.doc_converter == "builtin":
        return None
    for candidate in ((s.soffice_path,) if s.soffice_path else _CANDIDATES):
        found = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if found:
            return found
    return None


def convert(data: bytes, from_ext: str, to: str, s: Settings = default_settings, timeout: float = 180.0) -> bytes:
    """Convert a file's bytes with LibreOffice, e.g. convert(b, ".doc", "docx").
    Raises RuntimeError with the reason if it cannot."""
    exe = soffice(s)
    if exe is None:
        raise RuntimeError("LibreOffice (soffice) is not installed")
    with tempfile.TemporaryDirectory(prefix="esam-office-") as tmp:
        src = Path(tmp) / f"input{from_ext}"
        src.write_bytes(data)
        try:
            run = subprocess.run(
                [exe, f"-env:UserInstallation={(Path(tmp) / 'profile').as_uri()}", "--headless", "--norestore",
                 "--convert-to", to, "--outdir", tmp, str(src)],
                capture_output=True, timeout=timeout, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"LibreOffice did not finish within {timeout:.0f} s") from None
        except OSError as e:
            raise RuntimeError(f"LibreOffice could not be started: {e}") from None
        out = src.with_suffix("." + to.split(":")[0])
        if not out.exists() or not out.stat().st_size:
            reason = (run.stderr or run.stdout or b"").decode("utf-8", "replace").strip()[-300:]
            raise RuntimeError(f"LibreOffice produced no {to} file" + (f": {reason}" if reason else ""))
        return out.read_bytes()
