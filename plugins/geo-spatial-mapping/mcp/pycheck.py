"""1.12.3: is this Python fit to build the plugin's private environment on?  Run by findpython, setup and launch with the Python being judged:

    <python> pycheck.py            exit 0 when it is fit; exit 1 and one plain line on stdout (the reason) when it is not

A Python that starts is not always a whole Python. Seen on a Windows computer: a folder holding only python.exe and its DLLs, whose standard
library lived in a second install that the registry pointed at. It ran, printed "Could not find platform independent libraries <prefix>" on every
start, and an environment built on it carried the warning (and depended on the second install staying put). Judged here:

  1. version 3.11 or newer (the pinned packages need it);
  2. its own standard library is where it says (Lib\\os.py under the base prefix on Windows; lib/pythonX.Y/os.py elsewhere);
  3. the venv module and ensurepip are there (Debian and Ubuntu ship them in a separate package).

Standard library only, and plain syntax, so that an old Python reaches the version line instead of failing to parse this file.
"""
import os
import sys


def stdlib_candidates(base_prefix, version, name, platlibdir):
    """Where os.py should be under base_prefix for this kind of Python."""
    if name == "nt":
        return [os.path.join(base_prefix, "Lib", "os.py")]
    tag = "python%d.%d" % (version[0], version[1])
    dirs = {platlibdir or "lib", "lib", "lib64"}
    return [os.path.join(base_prefix, d, tag, "os.py") for d in sorted(dirs)]


def check(version=None, base_prefix=None, name=None, platlibdir=None, exe=None, have_venv=None, exists=os.path.isfile):
    """The reason this Python is not fit, or None. Every argument defaults to the running interpreter; the tests pass their own."""
    version = tuple(version or sys.version_info[:3])
    base_prefix = base_prefix if base_prefix is not None else sys.base_prefix
    name = name or os.name
    exe = exe or sys.executable or "python"
    if platlibdir is None:
        platlibdir = getattr(sys, "platlibdir", "lib")
    if version[:2] < (3, 11):
        return "%s is Python %d.%d; 3.11 or newer is needed" % (exe, version[0], version[1])
    found = stdlib_candidates(base_prefix, version, name, platlibdir)
    if not any(exists(p) for p in found):
        fix = ("Install Python 3.13 from python.org (or run: winget install Python.Python.3.13)" if name == "nt"
               else "Install Python 3.13 (python.org, Homebrew or your package manager)")
        return ("%s cannot find its own standard library (no %s); this Python install is incomplete or damaged. %s and run the setup again"
                % (exe, found[0], fix))
    if have_venv is None:
        try:
            import ensurepip  # noqa: F401
            import venv  # noqa: F401
            have_venv = True
        except ImportError:
            have_venv = False
    if not have_venv:
        return "%s has no venv or ensurepip module; on Debian and Ubuntu install the python3-venv package" % exe
    return None


def main():
    why = check()
    if why:
        print(why)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
