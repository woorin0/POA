"""Run real validators in a fresh process; no inherited legacy module bridge."""
import os
from pathlib import Path
import subprocess
import sys


def test_four_venue_fill_confirmation_independent_e2e(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,'-B',str(root/'release_checks/run_fill_confirmation.py'),'-q','--tb=short',
        '--junitxml='+str(tmp_path/'fill_confirmation.xml')],cwd=root,env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1'),capture_output=True,text=True,timeout=900)
    assert result.returncode==0,result.stdout[-6000:]+result.stderr[-1500:]
    import xml.etree.ElementTree as ET
    suites=list(ET.parse(tmp_path/'fill_confirmation.xml').getroot().iter('testsuite'))
    assert sum(int(s.get('tests',0)) for s in suites)==292
    assert all(int(s.get('failures',0))==int(s.get('errors',0))==int(s.get('skipped',0))==0 for s in suites)
