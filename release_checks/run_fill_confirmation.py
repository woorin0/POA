"""Fresh-process execution, independent of the unchanged legacy bridge."""
import sys
from pathlib import Path
import isolation
import pytest

if __name__=='__main__':
    root=Path(__file__).resolve().parent
    raise SystemExit(pytest.main(['--confcutdir='+str(root),'-p','no:cacheprovider',str(root/'fill_confirmation_contracts.py'),str(root/'fill_capture_crash_contracts.py'),*sys.argv[1:]],plugins=[isolation]))
