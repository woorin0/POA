"""Run independent real-code gates outside the legacy pytest bridge."""
import sys
from pathlib import Path
import isolation
import pytest
if __name__=='__main__':
    root=Path(__file__).resolve().parent
    raise SystemExit(pytest.main(['--confcutdir='+str(root), '-p','no:cacheprovider',str(root/'amount_contracts.py'),str(root/'staging_contracts.py'),*sys.argv[1:]],plugins=[isolation]))
