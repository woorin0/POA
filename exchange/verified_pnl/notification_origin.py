"""Optional non-secret notification origin; never account/financial authority."""
import hashlib,json,re,subprocess
from functools import lru_cache
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
PREFIX='\nPOA_NOTIFICATION_ORIGIN:'

@lru_cache(maxsize=4)
def _deployment(root,key):
    root=Path(root)
    try:
        raw=(root/'release_manifest_sha256.json').read_bytes();entries=json.loads(raw)
        if not isinstance(entries,list) or not 1<=len(entries)<=200:raise ValueError()
        for item in entries:
            p=(root/item['Path']).resolve()
            if root not in p.parents or p.name=='.env':raise ValueError()
            data=p.read_bytes()
            if len(data)!=item['Bytes'] or hashlib.sha256(data).hexdigest().upper()!=item['SHA256'].upper():raise ValueError()
        manifest=hashlib.sha256(raw).hexdigest();record=root/'.poa-deployment.json'
        if record.exists():
            if record.stat().st_size>2048:raise ValueError()
            info=json.loads(record.read_text(encoding='utf-8'))
            if info.get('manifest_sha256')!=manifest or not re.fullmatch('[0-9a-f]{40}',info.get('commit','')):raise ValueError()
            return dict(commit=info['commit'],status='MANIFEST_MATCHED_LOCAL_RECORD',manifest_sha256=manifest)
        top=subprocess.check_output(['git','-C',str(root),'rev-parse','--show-toplevel'],stderr=subprocess.DEVNULL,timeout=2).decode().strip()
        if Path(top).resolve()!=root:raise ValueError()
        sha=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],stderr=subprocess.DEVNULL,timeout=2).decode().strip()
        paths=[item['Path'] for item in entries]+['release_manifest_sha256.json']
        if not re.fullmatch('[0-9a-f]{40}',sha) or subprocess.run(['git','-C',str(root),'diff','--no-ext-diff','--no-textconv','--quiet','HEAD','--',*paths],stderr=subprocess.DEVNULL,timeout=2).returncode:raise ValueError()
        return dict(commit=sha,status='CLEAN_PRODUCT_GIT',manifest_sha256=manifest)
    except Exception:return dict(commit=None,status='UNVERIFIED')

def deployment(root=None):
    root=Path(root or ROOT).resolve()
    try:
        p=root/'release_manifest_sha256.json'
        if p.stat().st_size>131072:raise ValueError()
        entries=json.loads(p.read_bytes())
        if not isinstance(entries,list) or not 1<=len(entries)<=200:raise ValueError()
        paths=[root/item['Path'] for item in entries]+[p,root/'.poa-deployment.json',root/'.git/HEAD']
        key=tuple((str(x),x.stat().st_mtime_ns,x.stat().st_size) if x.is_file() else (str(x),None,None) for x in paths)
        return _deployment(str(root),key)
    except Exception:return dict(commit=None,status='UNVERIFIED')

def stamp(payload,settings):
    alias=getattr(settings,'POA_NOTIFICATION_SERVER_ALIAS',None) if settings is not None else None
    if not isinstance(alias,str) or not re.fullmatch('[A-Za-z0-9_-]{1,24}',alias) or re.search('(?i)secret|token|bearer|apikey|password',alias):alias=None
    # Origin metadata is opt-in; layout marks new notification payloads only.
    origin=deployment() if alias is not None or (ROOT/'.poa-deployment.json').is_file() else {'commit':None}
    value={'layout':2,'server':alias,'commit':origin['commit']}
    payload.verification_source=(payload.verification_source or '').split(PREFIX)[0]+PREFIX+json.dumps(value,sort_keys=True,separators=(',',':'))
    return payload

def display(payload):
    try:
        text=payload.verification_source or ''
        if PREFIX not in text:return None
        value=json.loads(text.rsplit(PREFIX,1)[1]);alias=value.get('server');sha=value.get('commit')
        if alias is None and sha is None:return None
        if alias is not None and not re.fullmatch('[A-Za-z0-9_-]{1,24}',alias):return None
        if sha is not None and not re.fullmatch('[0-9a-f]{40}',sha):return None
        return '서버: '+(alias or '별칭 미설정')+' · 배포: '+(sha[:12] if sha else '미확인')
    except Exception:return None

REASONS={'TV_ACQUISITION_UNAVAILABLE':'매수 신호/전략 평균 진입가격 없음','ACQUISITION_UNAVAILABLE':'연결된 매수 체결 기록 없음 · tv_signal/strategy_id 확인','STRATEGY_UNAVAILABLE':'공통 strategy_id 필요','ACQUISITION_QUANTITY_GAP':'매도 수량에 대응하는 매수 기록 부족','UNCONFIRMED_HISTORY':'체결 미확정 관측 존재','TV_HISTORY_LIMIT':'신호 이력 처리 한도 초과','NOTIFICATION_HISTORY_LIMIT':'관측 이력 처리 한도 초과','TV_OBSERVATION_CONFLICT':'신호 참조 충돌 · REVIEW','OBSERVATION_CONFLICT':'관측 충돌 · REVIEW','EXECUTION_SCOPE_CONFLICT':'체결 범위 불일치'}
def reason(value):return REASONS.get(value,'자료 부족 또는 범위 확인 필요')

def reason_code(value):return value if value in REASONS else 'UNAVAILABLE'


def current_layout(payload):
    try:return json.loads((payload.verification_source or '').rsplit(PREFIX,1)[1]).get('layout')==2
    except Exception:return False
