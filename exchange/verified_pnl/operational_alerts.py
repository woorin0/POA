"""Bounded operational incident journal. Never accepts order/financial wire payloads.

No network on report(); one separate reporter calls the unchanged DiscordSender.
Ambiguous POSTs are UNKNOWN, including a crash after durable POST intent.
"""
import hashlib,json,re,sqlite3,threading,time,uuid
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from loguru import logger
from .alert_presentation import ERROR,PENDING

_SQL=('INTAKE_PERSISTENCE_FAILED','INTAKE_DISK_FULL','INTAKE_LOCKED','INTAKE_READONLY','INTAKE_CORRUPT','INTAKE_UNAVAILABLE','INTAKE_INVALID_REQUEST')
CODES=frozenset(('ORDER_PROCESSING_FAILED ORDER_RESULT_UNCONFIRMED ORDER_AUTH_ERROR ORDER_PERMISSION_ERROR ORDER_PARAMETERS_ERROR ORDER_TIMEOUT ORDER_NETWORK_ERROR ORDER_REJECTION_EVENT UPBIT_OBSERVATION_REVIEW UPBIT_OBSERVATION_DB_FAILED UPBIT_OBSERVATION_ADMISSION_FAILED UPBIT_READ_AUTH_REJECTED UPBIT_READ_PERMISSION_REJECTED UPBIT_READ_IP_REJECTED UPBIT_READ_RATE_LIMITED UPBIT_READ_TIMEOUT UPBIT_READ_SERVER_ERROR UPBIT_READ_CONNECTION_FAILED UPBIT_READ_SCHEMA_INVALID UPBIT_READ_ORDER_UUID UPBIT_READ_SCOPE_MISMATCH UPBIT_READ_RESPONSE_SCOPE UPBIT_READ_QUANTITY_CONFLICT UPBIT_READ_FILL_INCOMPLETE UPBIT_READ_CLOCK_RANGE UPBIT_READ_BINDING_FAILED UPBIT_READ_UNEXPECTED_FAILURE UPBIT_READ_UNAVAILABLE BUY_ACK_ADMISSION_FAILED BUY_ADMISSION_REJECTED SIGNAL_SELL_ADMISSION_REJECTED NOTIFICATION_INTAKE_REVIEW INTAKE_SAVE_FAILED OUTBOX_SAVE_FAILED VERIFICATION_FAILED RESTORE_PREFLIGHT_BLOCKED DISCORD_DELIVERY_FAILED DISCORD_DELIVERY_UNKNOWN WORKER_START_FAILED WORKER_NOT_STARTED WORKER_EXITED WORKER_CUTOVER_REQUIRED WORKER_RESTORE_BLOCKED').split())|frozenset(stage+'_'+code for stage in ('WORKER_TICK','WORKER_STORE','WORKER_RECOVERY') for code in _SQL)
PHASES={'ORDER':'주문 실행/결과 확인','OBSERVATION':'거래소 체결조회/관측 승인','INTAKE':'알림 입력 저장','OUTBOX':'알림 승인/전송','WORKER':'알림 Worker','RESTORE':'복원 일관성 검사'}
_SECRET=re.compile('(?i)secret|token|bearer|webhook|password|apikey|https?://|jwt|canary')


def safe(value,pattern,limit,fallback):
    return value if type(value) is str and len(value)<=limit and re.fullmatch(pattern,value) and not _SECRET.search(value) else fallback


def context(settings,info,order_id):
    def get(name):return info.get(name) if isinstance(info,dict) else getattr(info,name,None)
    ex=safe(get('exchange'),r'(UPBIT|BINANCE|BYBIT|OKX)',8,'UNKNOWN')
    symbol=safe(get('unified_symbol'),r'[A-Z0-9]{1,20}/(?:KRW|USDT|USD)(?::[A-Z0-9]{1,20})?',48,'미확인')
    strategy=safe(get('strategy_id') or get('order_name'),r'[A-Za-z0-9가-힣_. -]+',48,'참조 미표시')
    side='BUY' if get('is_buy') is True or get('side') in ('buy','매수') else 'SELL' if get('is_sell') is True or get('side') in ('sell','매도') else 'UNKNOWN'
    account=getattr(settings,ex+'_KEY',None)
    account=account if type(account) is str else 'unconfigured'
    ref=order_id if type(order_id) is str and 0<len(order_id)<=128 else get('signal_id') or '|'.join((symbol,strategy,side))
    if type(ref) is not str:ref='unconfirmed'
    fingerprint=hashlib.sha256((ex+'|'+account+'|'+ref).encode()).hexdigest()
    return dict(exchange=ex,symbol=symbol,strategy=strategy,side=side,order_ref=fingerprint)


def wire(server,code,phase,ctx,kind,created):
    assert code in CODES and phase in PHASES and kind in ('ERROR','RECOVERY')
    assert set(ctx)=={'exchange','symbol','strategy','side','order_ref'}
    assert re.fullmatch('[0-9a-f]{64}',ctx['order_ref'])
    # Revalidate safe context on read as well as ingestion.
    assert ctx['exchange'] in ('UPBIT','BINANCE','BYBIT','OKX','UNKNOWN') and ctx['side'] in ('BUY','SELL','UNKNOWN')
    assert ctx['symbol']=='미확인' or re.fullmatch(r'[A-Z0-9]{1,20}/(?:KRW|USDT|USD)(?::[A-Z0-9]{1,20})?',ctx['symbol'])
    assert ctx['strategy']=='참조 미표시' or safe(ctx['strategy'],r'[A-Za-z0-9가-힣_. -]+',48,None)==ctx['strategy']
    assert server=='별칭 미설정' or safe(server,r'[A-Za-z0-9_-]+',24,None)==server
    when=datetime.fromtimestamp(created,timezone(timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S KST')
    lines=['서버: '+server,'거래소: '+ctx['exchange'],'종목: '+ctx['symbol'],'방향: '+ctx['side'],'전략: '+ctx['strategy'],'오류 코드: '+code,'오류 단계: '+PHASES[phase]]
    if kind=='ERROR':lines+=['주문 상태: 실제 체결 여부 미확정','알림 상태: 검토 필요','조치: 기존 주문/알림 내역 확인','주의: 중복 주문 금지']
    else:lines+=['알림 상태: 해당 장애 복구 확인','주문 체결/전달 성공을 의미하지 않음']
    lines+=['사건 참조: '+ctx['order_ref'][:12],'발생 시각: '+when]
    return json.dumps({'embeds':[{'title':'🔴 POA · 오류 발생' if kind=='ERROR' else 'POA · 장애 복구 확인','description':'\n'.join(lines),'color':ERROR if kind=='ERROR' else PENDING}],'allowed_mentions':{'parse':[]}},ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()


_INIT_LOCK=threading.RLock()

class Journal:
    def __init__(self,settings):
        self.path=str(settings.VERIFIED_NOTIFICATIONS_DB_PATH)+'.ops.sqlite3'
        self.server=safe(getattr(settings,'POA_NOTIFICATION_SERVER_ALIAS',None),r'[A-Za-z0-9_-]+',24,'별칭 미설정')
        with _INIT_LOCK,self.connect() as c:
            c.execute('CREATE TABLE IF NOT EXISTS ops_meta(id INTEGER PRIMARY KEY,node TEXT NOT NULL,last_post REAL NOT NULL)')
            c.execute('INSERT OR IGNORE INTO ops_meta VALUES(1,?,0)',(uuid.uuid4().hex,))
            if self.server=='별칭 미설정':self.server='POA-'+c.execute('SELECT node FROM ops_meta WHERE id=1').fetchone()[0][:8]
            c.execute('CREATE TABLE IF NOT EXISTS ops_incidents(id TEXT PRIMARY KEY,code TEXT,phase TEXT,order_ref TEXT,episode INTEGER,active INTEGER)')
            c.execute('CREATE TABLE IF NOT EXISTS ops_events(id TEXT PRIMARY KEY,incident TEXT,episode INTEGER,kind TEXT,context TEXT,server TEXT,code TEXT,phase TEXT,created REAL,wire BLOB,digest TEXT,status TEXT,due REAL,attempts INTEGER,token TEXT,expires REAL,message_id TEXT)')
            c.execute('CREATE INDEX IF NOT EXISTS ops_ready ON ops_events(status,due)')
            c.execute("CREATE TRIGGER IF NOT EXISTS ops_wire_immutable BEFORE UPDATE OF wire,digest,context,server,code,phase,created,kind,incident,episode ON ops_events BEGIN SELECT RAISE(ABORT,'immutable_ops_event'); END")
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=.2)
        try:
            c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE');yield c;c.commit()
        except BaseException:c.rollback();raise
        finally:c.close()
    def event(self,code,phase,ctx,*,recovered=False,only_if_no_active=False):
        if code not in CODES or phase not in PHASES:return False
        now=time.time()
        with self.connect() as c:
            node=c.execute('SELECT node FROM ops_meta WHERE id=1').fetchone()[0]
            key=hashlib.sha256((node+'|'+self.server+'|'+code+'|'+ctx['order_ref']+'|'+phase).encode()).hexdigest()
            old=c.execute('SELECT episode,active FROM ops_incidents WHERE id=?',(key,)).fetchone()
            if recovered:
                if not old or not old[1]:return False
                episode=old[0];kind='RECOVERY';c.execute('UPDATE ops_incidents SET active=0 WHERE id=?',(key,))
                # Suppress a transient tick error only if no POST intent ever existed.
                if code.startswith('WORKER_TICK_') and c.execute("UPDATE ops_events SET status='RECOVERED_UNSENT' WHERE incident=? AND episode=? AND kind='ERROR' AND status='NEW' AND attempts=0",(key,episode)).rowcount:
                    return True
            else:
                if old and old[1]:return False
                if only_if_no_active and c.execute('SELECT 1 FROM ops_incidents WHERE phase=? AND order_ref=? AND active=1 LIMIT 1',(phase,ctx['order_ref'])).fetchone():return False
                episode=old[0]+1 if old else 1;kind='ERROR'
                c.execute('INSERT INTO ops_incidents VALUES(?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET episode=excluded.episode,active=1',(key,code,phase,ctx['order_ref'],episode))
            raw=wire(self.server,code,phase,ctx,kind,now);eid=hashlib.sha256((key+'|'+str(episode)+'|'+kind).encode()).hexdigest()
            c.execute('INSERT INTO ops_events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,NULL,NULL,NULL)',(eid,key,episode,kind,json.dumps(ctx,sort_keys=True),self.server,code,phase,now,raw,hashlib.sha256(raw).hexdigest(),'NEW',now+(5 if code.startswith('WORKER_TICK_') else 0)))
        return True
    def claim(self):
        now=time.time()
        with self.connect() as c:
            c.execute("UPDATE ops_events SET status='UNKNOWN' WHERE status='POSTING' AND expires<?",(now,))
            last=c.execute('SELECT last_post FROM ops_meta WHERE id=1').fetchone()[0]
            if now-last<10:return None
            row=c.execute("SELECT id,wire,digest,context,server,code,phase,kind,created,attempts FROM ops_events WHERE status='NEW' AND due<=? ORDER BY due,rowid LIMIT 1",(now,)).fetchone()
            if not row:return None
            eid,raw,wd,ctx,server,code,phase,kind,created,attempts=row
            try:valid=hashlib.sha256(raw).hexdigest()==wd and raw==wire(server,code,phase,json.loads(ctx),kind,created)
            except (AssertionError,ValueError,TypeError,OverflowError):valid=False
            if not valid:
                c.execute("UPDATE ops_events SET status='HELD' WHERE id=?",(eid,));return None
            token=uuid.uuid4().hex
            c.execute("UPDATE ops_events SET status='POSTING',token=?,expires=?,attempts=attempts+1 WHERE id=? AND status='NEW'",(token,now+30,eid));c.execute('UPDATE ops_meta SET last_post=? WHERE id=1',(now,))
            return dict(id=eid,wire=raw,token=token,attempts=attempts+1,digest=wd)
    def ack(self,claim,success,message_id,error,delay):
        with self.connect() as c:
            row=c.execute('SELECT status,token,digest FROM ops_events WHERE id=?',(claim['id'],)).fetchone()
            if row!=('POSTING',claim['token'],claim['digest']):return False
            if success:state='SENT'
            elif delay is not None and claim['attempts']<3:state='NEW'
            elif error=='delivery_unknown':state='UNKNOWN'
            else:state='HELD'
            delay=min(300,max(10,delay)) if type(delay) in (int,float) else 10
            c.execute('UPDATE ops_events SET status=?,due=?,message_id=? WHERE id=?',(state,time.time()+delay,message_id if success else None,claim['id']))
            return True


def report(settings,code,*,phase='WORKER',info=None,order_id=None,only_if_no_active=False):
    try:return Journal(settings).event(code,phase,context(settings,info,order_id),only_if_no_active=only_if_no_active)
    except Exception:logger.warning('operational_alerts: JOURNAL_UNAVAILABLE');return False


def resolve(settings,*,phase,info=None,order_id=None,codes=None):
    try:
        from pathlib import Path
        if not Path(str(settings.VERIFIED_NOTIFICATIONS_DB_PATH)+'.ops.sqlite3').is_file():return
        journal=Journal(settings);ctx=context(settings,info,order_id)
        with journal.connect() as c:rows=c.execute('SELECT code FROM ops_incidents WHERE active=1 AND phase=? AND order_ref=? LIMIT 40',(phase,ctx['order_ref'])).fetchall()
        for (code,) in rows:
            if codes is None or code in codes:journal.event(code,phase,ctx,recovered=True)
    except Exception:logger.warning('operational_alerts: RECOVERY_RECORD_FAILED')


def flush(settings,*,journal=None):
    from .outbox import DiscordSender
    try:
        journal=journal or Journal(settings);claim=journal.claim()
        if claim is None:return False
        sender=DiscordSender(getattr(settings,'DISCORD_WEBHOOK_URL',None),retry_on_5xx=False)
        try:ok,error,delay=sender.send_frozen(claim['wire'])
        except Exception:ok,error,delay=False,'delivery_unknown',None
        journal.ack(claim,ok,sender.message_id,error,delay)
        return True
    except Exception:logger.warning('operational_alerts: DELIVERY_HELD');return False



def delivery(settings,store,claim,code=None,*,recovered=False):
    """Read only the immutable claimed revision's safe routing metadata."""
    info=None;oid=claim['delivery_id']
    try:
        with store._connect_read() as c:row=c.execute('SELECT payload FROM vpnl_revisions WHERE root_id=? AND revision=?',(claim['root_id'],claim['revision'])).fetchone()
        if row:
            p=json.loads(row[0]);info=dict(exchange=p.get('exchange'),unified_symbol=p.get('symbol'),side=p.get('side'),strategy_id=p.get('strategy_id'),order_name=p.get('order_name'));oid=p.get('order_id') or oid
    except Exception:pass
    if recovered:return resolve(settings,phase='OUTBOX',info=info,order_id=oid,codes=('DISCORD_DELIVERY_FAILED','DISCORD_DELIVERY_UNKNOWN'))
    return report(settings,code,phase='OUTBOX',info=info,order_id=oid)


def order_failure(settings,error,info):
    code='ORDER_PROCESSING_FAILED'
    try:
        import ccxt
        from exchange.error import AmountError
        seen=set()
        # Exchange adapters wrap ccxt errors in OrderError. Inspect exception
        # types only; never parse/emit the wrapped message or response body.
        for _ in range(4):
            if not isinstance(error,BaseException) or id(error) in seen:break
            seen.add(id(error))
            if isinstance(error,ccxt.RequestTimeout):code='ORDER_TIMEOUT'
            elif isinstance(error,ccxt.PermissionDenied):code='ORDER_PERMISSION_ERROR'
            elif isinstance(error,ccxt.AuthenticationError):code='ORDER_AUTH_ERROR'
            elif isinstance(error,(ccxt.InvalidOrder,AmountError)):code='ORDER_PARAMETERS_ERROR'
            elif isinstance(error,ccxt.NetworkError):code='ORDER_NETWORK_ERROR'
            if code!='ORDER_PROCESSING_FAILED':break
            error=error.__cause__ or error.__context__
    except Exception:pass
    return report(settings,code,phase='ORDER',info=info,only_if_no_active=(code=='ORDER_PROCESSING_FAILED'))


_RUNNING={}
_LOCK=threading.Lock()

def start(settings):
    key=str(settings.VERIFIED_NOTIFICATIONS_DB_PATH)
    with _LOCK:
        old=_RUNNING.get(key)
        if old is not None and not old.is_set() and old.worker_thread.is_alive():return old
        stop=threading.Event()
        if not getattr(settings,'DISCORD_WEBHOOK_URL',None):return stop
        def run():
            journal=None
            while not stop.is_set():
                try:
                    if journal is None:journal=Journal(settings)
                    flush(settings,journal=journal)
                except Exception:
                    logger.warning('operational_alerts: REPORTER_STORE_UNAVAILABLE')
                    stop.wait(30);continue
                stop.wait(1)
        thread=threading.Thread(target=run,name='poa-operational-alerts',daemon=True)
        stop.worker_thread=thread;_RUNNING[key]=stop
        try:thread.start()
        except Exception:
            _RUNNING.pop(key,None);stop.set()
            logger.warning('operational_alerts: REPORTER_START_FAILED')
        return stop
