"""Mock-input-only Upbit observation journal. No engine, signer or transport imports.

All identities and observations are host CLAIMS. A local consistency result grants
no HISTORY completeness, independent FeeAudit, financial or delivery authority.
"""
from dataclasses import dataclass, asdict
from decimal import Decimal, localcontext
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager, closing
import hashlib
import json
import re
import sqlite3
import uuid

APP_ID = 0x504F414A
KINDS = {'REST_ORDER', 'REST_BALANCE', 'MYORDER', 'MYASSET', 'DEPOSIT', 'WITHDRAWAL', 'POCKET_TRANSFER', 'REST_PAGE'}
CODES = {'INITIAL_SUBSCRIPTION_DELAY', 'DISCONNECTED', 'RECONNECTED', 'MISSING_PAGE', 'REST_TIMEOUT', 'HTTP_401', 'HTTP_429', 'HTTP_5XX', 'CLOCK_UNCERTAIN', 'PERMISSION_DENIED', 'TRANSFER_PERMISSION_BLOCKED', 'BACKUP_MISMATCH', 'STORAGE_FAILURE'}
SECRET = re.compile(r'(?i)(bearer|api.?key|secret|passphrase|webhook|canary|authorization|access.?token|jwt|password|private.?key|signature|https?://|%[0-9a-f]{2})')

class JournalError(ValueError):
    """Safe category only; never includes input, paths or original exceptions."""

class NumberToken(str):
    pass

def canonical(v):
    def clean(x):
        if isinstance(x, Decimal):
            if not x.is_finite(): raise JournalError('NUMBER_INVALID')
            text = format(x, 'f')
            return '0' if not x else (text.rstrip('0').rstrip('.') if '.' in text else text)
        if isinstance(x, dict): return {k: clean(y) for k, y in x.items()}
        if isinstance(x, (tuple, list)): return [clean(y) for y in x]
        return x
    return json.dumps(clean(v), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(v):
    return hashlib.sha256(canonical(v).encode()).hexdigest()

def uid(value):
    try:
        if type(value) is not str or str(uuid.UUID(value)) != value: raise ValueError()
        return value
    except Exception: raise JournalError('UUID_INVALID') from None

def instant(value):
    if not isinstance(value, str): raise JournalError('TIME_INVALID')
    try:
        d = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if d.utcoffset() is None: raise ValueError()
        # Preserve microseconds without a float round-trip.
        import calendar
        return calendar.timegm(d.utctimetuple()) * 1000000 + d.microsecond
    except Exception: raise JournalError('TIME_INVALID') from None

def integer(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)): raise JournalError('INTEGER_INVALID')
    try:
        d = Decimal(value)
        if not d.is_finite() or d != d.to_integral_value() or not 0 <= d <= 10**18: raise ValueError()
        return int(d)
    except Exception: raise JournalError('INTEGER_INVALID') from None

@dataclass(frozen=True)
class Scope:
    account_ref: str
    pocket_uuid: str
    source_ref: str
    def __post_init__(self):
        if not all(isinstance(v, str) and re.fullmatch('[0-9a-f]{64}', v) for v in (self.account_ref, self.source_ref)): raise JournalError('SCOPE_INVALID')
        uid(self.pocket_uuid)
    @classmethod
    def from_refs(cls, account, pocket, source):
        # Raw host references are not stored or logged and confer no authenticity.
        if not all(type(v) is str and 0 < len(v) <= 512 for v in (account, source)): raise JournalError('SCOPE_INVALID')
        return cls(digest(['account', account]), uid(pocket), digest(['source', source]))

@dataclass(frozen=True)
class AppendResult:
    stored: bool
    status: str
    code: str
    seq: int = None


def parse(kind, text):
    if SECRET.search(text): raise JournalError('SECRET_INPUT_REJECTED')
    def pairs(items):
        d = {}
        for k, v in items:
            if k in d: raise JournalError('DUPLICATE_JSON_KEY')
            d[k] = v
        return d
    try:
        raw = json.loads(text, parse_float=NumberToken, parse_int=NumberToken,
                         parse_constant=lambda _: (_ for _ in ()).throw(JournalError('NUMBER_INVALID')), object_pairs_hook=pairs)
    except JournalError: raise
    except Exception: raise JournalError('JSON_INVALID') from None
    if SECRET.search(canonical(raw)):raise JournalError('SECRET_INPUT_REJECTED')
    numbers = []
    def num(v, path, nonnegative=True):
        if not isinstance(v, str): raise JournalError('NUMBER_INVALID')
        try:
            n = Decimal(v)
            if not n.is_finite() or len(n.as_tuple().digits) > 128 or abs(n.as_tuple().exponent) > 128 or (nonnegative and n < 0): raise ValueError()
        except Exception: raise JournalError('NUMBER_INVALID') from None
        numbers.append(dict(path=path, lexeme=str(v), json_type='number' if isinstance(v, NumberToken) else 'string', decimal=json.loads(canonical(n))))
        return n
    allowed={
        'REST_ORDER':set('market uuid side ord_type price state created_at volume remaining_volume executed_volume reserved_fee remaining_fee paid_fee locked trades_count time_in_force identifier smp_type prevented_volume prevented_locked trades'.split()),
        'MYORDER':set('type code uuid ask_bid order_type state trade_uuid price avg_price volume remaining_volume executed_volume trades_count reserved_fee remaining_fee paid_fee locked executed_funds time_in_force trade_fee is_maker identifier smp_type prevented_volume prevented_locked trade_timestamp order_timestamp timestamp stream_type'.split()),
        'MYASSET':set('type asset_uuid assets asset_timestamp timestamp stream_type'.split()),
    }
    if kind in allowed and (type(raw) is not dict or not set(raw)<=allowed[kind]):raise JournalError('UNSUPPORTED_FIELD')
    code = 'OBSERVED'
    order = trade = None
    if kind == 'REST_ORDER':
        order = uid(raw['uuid'])
        if raw['market'] != 'KRW-BTC' or raw['side'] not in ('bid', 'ask'): raise JournalError('ORDER_SCOPE_INVALID')
        rows = raw['trades']
        if type(rows) is not list or len(rows) > 40 or integer(raw['trades_count']) != len(rows): raise JournalError('TRADE_COVERAGE_INVALID')
        facts = dict(order_id=order, market=raw['market'], side=raw['side'], state=raw['state'], ord_type=raw['ord_type'], created_us=instant(raw['created_at']),
                     limit_price=num(raw['price'],'/price') if raw['ord_type']=='limit' else None, quantity=num(raw['volume'], '/volume'), filled=num(raw['executed_volume'], '/executed_volume'), remaining=num(raw['remaining_volume'], '/remaining_volume'), fee=num(raw['paid_fee'], '/paid_fee'), trades=[])
        seen = set()
        for i, t in enumerate(rows):
            if type(t) is not dict or not set(t)<=set('market uuid price volume funds trend created_at side order_uuid'.split()):raise JournalError('UNSUPPORTED_FIELD')
            tid = uid(t['uuid'])
            if tid in seen or tid == order or t.get('order_uuid', order) != order or t['market'] != 'KRW-BTC' or t['side'] != raw['side']: raise JournalError('TRADE_ID_CONFLICT')
            seen.add(tid)
            price, quantity, funds = (num(t[k], '/trades/%s/%s' % (i, k)) for k in ('price', 'volume', 'funds'))
            if price <= 0 or quantity <= 0 or price * quantity != funds: raise JournalError('TRADE_ARITHMETIC_CONFLICT')
            facts['trades'].append(dict(trade_id=tid, price=price, quantity=quantity, funds=funds, at_us=instant(t['created_at'])))
        if facts['quantity'] != facts['filled'] + facts['remaining'] or sum((t['quantity'] for t in facts['trades']), Decimal(0)) != facts['filled']: raise JournalError('QUANTITY_CONFLICT')
        if raw['ord_type'] != 'limit' or raw['state'] != 'done' or len(rows) != 1 or facts['remaining'] != 0: code = 'UNSUPPORTED_ORDER_FORM'
        key = ['order', order, raw['state'], facts['filled']]
    elif kind == 'MYORDER':
        order = uid(raw['uuid'])
        if raw['code'] != 'KRW-BTC' or raw['ask_bid'] not in ('BID', 'ASK'): raise JournalError('ORDER_SCOPE_INVALID')
        state = raw['state']
        facts = dict(order_id=order, state=state, side=raw['ask_bid'], at_us=integer(raw.get('trade_timestamp', raw['timestamp'])) * 1000)
        if state == 'trade':
            trade = uid(raw['trade_uuid'])
            facts.update(trade_id=trade, price=num(raw['price'], '/price'), quantity=num(raw['volume'], '/volume'), fee=num(raw['trade_fee'], '/trade_fee'))
            key = ['trade', trade]
        else:
            facts['filled'] = num(raw['executed_volume'], '/executed_volume'); key = ['order-state', order, state, facts['filled']]
    elif kind in ('MYASSET', 'REST_BALANCE'):
        assets = raw['assets'] if kind == 'MYASSET' else raw
        if type(assets) is not list or len(assets) > 40: raise JournalError('ASSET_SCHEMA_INVALID')
        facts = dict(balances={}, at_us=integer(raw['asset_timestamp']) * 1000 if kind == 'MYASSET' else None)
        if kind == 'MYASSET': facts['asset_id'] = uid(raw['asset_uuid'])
        seen = set()
        for i, item in enumerate(assets):
            if type(item) is not dict or not set(item)<=set(('currency balance locked avg_buy_price avg_buy_price_modified unit_currency' if kind=='REST_BALANCE' else 'currency balance locked').split()):raise JournalError('UNSUPPORTED_FIELD')
            c = item['currency']
            if c in seen: raise JournalError('CURRENCY_DUPLICATE')
            seen.add(c)
            if c not in ('KRW', 'BTC'): continue
            if kind == 'REST_BALANCE' and item.get('unit_currency') != 'KRW': raise JournalError('CURRENCY_CONFLICT')
            free, locked = num(item['balance'], '/assets/%s/balance' % i), num(item['locked'], '/assets/%s/locked' % i)
            facts['balances'][c] = dict(free=free, locked=locked, total=free + locked)
        if set(facts['balances']) != {'KRW', 'BTC'}: code = 'PARTIAL_ASSET_FRAME'
        key = ['asset', facts['asset_id'], facts['at_us']] if kind == 'MYASSET' else ['balance', digest(facts)]
    elif kind in ('DEPOSIT', 'WITHDRAWAL', 'POCKET_TRANSFER'):
        if type(raw) is not dict or not set(raw)<=set('uuid type currency net_type txid state created_at done_at amount fee transaction_type from to identifier'.split()):raise JournalError('UNSUPPORTED_FIELD')
        facts = dict(movement_id=uid(raw['uuid']), currency=raw['currency'], state=raw['state'], amount=num(raw['amount'], '/amount'), at_us=instant(raw['created_at']))
        key = ['movement', kind, facts['movement_id'], facts['state']]; code = 'EXTERNAL_MOVEMENT_REVIEW'
    elif kind == 'REST_PAGE':
        if type(raw) is not list or len(raw) > 40: raise JournalError('PAGE_SCHEMA_INVALID')
        if any(type(x) is not dict or not set(x)<=allowed['REST_ORDER'] for x in raw):raise JournalError('UNSUPPORTED_FIELD')
        facts = dict(count=len(raw)); key = ['page', digest(raw)]
    else: raise JournalError('KIND_INVALID')
    return facts, numbers, key, code, order, trade


class Journal:
    def __init__(self, path, *, test_root, timeout=0.1):
        self.root = Path(test_root).resolve(); self.path = Path(path).resolve(); self.timeout = timeout
        if self.root not in self.path.parents or not self.path.name.endswith('.journal.sqlite'): raise JournalError('ISOLATED_PATH_REQUIRED')

        try:self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:raise JournalError('JOURNAL_INIT_FAILED') from None
        try:
            exists = self.path.exists() and self.path.stat().st_size > 0
            with self._connect() as c:
                if exists:
                    if c.execute('PRAGMA application_id').fetchone()[0] != APP_ID: raise JournalError('FOREIGN_DATABASE_REJECTED')
                    if c.execute('SELECT version FROM journal_meta WHERE id=1').fetchone() != (1,): raise JournalError('SCHEMA_REJECTED')
                c.execute('PRAGMA journal_mode=WAL'); c.execute('PRAGMA synchronous=FULL'); c.execute('BEGIN IMMEDIATE')
                c.execute('PRAGMA application_id=%d' % APP_ID)
                c.execute('CREATE TABLE IF NOT EXISTS journal_meta(id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, journal_id TEXT NOT NULL)')
                c.execute('CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY,scope TEXT NOT NULL,started_us INTEGER NOT NULL)')
                c.execute('CREATE TABLE IF NOT EXISTS epochs(id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id),channel TEXT NOT NULL,started_us INTEGER NOT NULL)')
                c.execute('CREATE TABLE IF NOT EXISTS entries(seq INTEGER PRIMARY KEY,id TEXT UNIQUE NOT NULL,epoch_id TEXT REFERENCES epochs(id),kind TEXT NOT NULL,status TEXT NOT NULL,code TEXT NOT NULL,scope TEXT,order_id TEXT,trade_id TEXT,fact_key TEXT,facts_digest TEXT,raw_digest TEXT,raw TEXT,facts TEXT NOT NULL,numbers TEXT NOT NULL,start_us INTEGER NOT NULL,end_us INTEGER NOT NULL,uncertainty_us INTEGER,previous_hash TEXT NOT NULL,entry_hash TEXT NOT NULL)')
                c.execute('CREATE INDEX IF NOT EXISTS journal_fact_lookup ON entries(fact_key,seq)')
                c.execute('INSERT OR IGNORE INTO journal_meta VALUES(1,1,?)', (str(uuid.uuid4()),))
                for table in ('journal_meta', 'sessions', 'epochs', 'entries'):
                    for action in ('UPDATE', 'DELETE'):
                        c.execute("CREATE TRIGGER IF NOT EXISTS %s_%s_sealed BEFORE %s ON %s BEGIN SELECT RAISE(ABORT,'JOURNAL_IMMUTABLE'); END" % (table, action, action, table))
                self._commit(c)
        except JournalError: raise
        except (sqlite3.Error, OSError): raise JournalError('JOURNAL_INIT_FAILED') from None
    @contextmanager
    def _connect(self, readonly=False):
        c = sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=self.timeout) if readonly else sqlite3.connect(self.path, timeout=self.timeout)
        c.execute('PRAGMA foreign_keys=ON')
        if not readonly: c.execute('PRAGMA synchronous=FULL')
        try:
            yield c
        finally:
            if c.in_transaction: c.rollback()
            c.close()
    def _commit(self, c): c.commit()
    def session(self, scope, started_us):
        if not isinstance(scope, Scope) or type(started_us) is not int or started_us < 0: raise JournalError('SESSION_INVALID')
        sid = str(uuid.uuid4())
        try:
            with self._connect() as c:
                c.execute('INSERT INTO sessions VALUES(?,?,?)', (sid, canonical(asdict(scope)), started_us)); self._commit(c)
            return sid
        except (sqlite3.Error, OSError): raise JournalError('SESSION_STORE_FAILED') from None
    def epoch(self, session, channel, started_us):
        if channel not in ('REST', 'MYORDER', 'MYASSET') or type(started_us) is not int or started_us < 0: raise JournalError('EPOCH_INVALID')
        eid = str(uuid.uuid4())
        try:
            with self._connect() as c:
                c.execute('BEGIN IMMEDIATE')
                old = c.execute('SELECT 1 FROM epochs WHERE session_id=? AND channel=? LIMIT 1', (session, channel)).fetchone()
                c.execute('INSERT INTO epochs VALUES(?,?,?,?)', (eid, session, channel, started_us)); self._commit(c)
            if channel != 'REST':
                result = self.gap(eid, 'RECONNECTED' if old else 'INITIAL_SUBSCRIPTION_DELAY', started_us)
                if not result.stored: raise JournalError('EPOCH_GAP_NOT_STORED')
            return eid
        except JournalError: raise
        except (sqlite3.Error, OSError): raise JournalError('EPOCH_STORE_FAILED') from None
    @staticmethod
    def _store_code(exc):
        n = getattr(exc, 'sqlite_errorcode', None)
        return {5:'STORE_LOCKED', 6:'STORE_LOCKED', 8:'STORE_READONLY', 11:'STORE_CORRUPT', 13:'STORE_FULL'}.get(n & 255 if isinstance(n, int) else None, 'STORE_FAILED')
    def _append(self, epoch, kind, status, code, facts, numbers, raw, start, end, uncertainty=None, key=None, order=None, trade=None, raw_hash_override=None):
        try:
            with self._connect() as c:
                c.execute('BEGIN IMMEDIATE')
                scope = None
                if epoch is not None:
                    e = c.execute('SELECT s.scope,e.channel FROM epochs e JOIN sessions s ON s.id=e.session_id WHERE e.id=?', (epoch,)).fetchone()
                    if e is None: raise JournalError('EPOCH_UNKNOWN')
                    scope, channel = e
                    if kind in KINDS and channel != ('MYORDER' if kind == 'MYORDER' else 'MYASSET' if kind == 'MYASSET' else 'REST'): raise JournalError('CHANNEL_SCOPE_INVALID')
                if epoch is not None and kind in KINDS and channel in ('MYORDER','MYASSET') and not c.execute("SELECT 1 FROM entries WHERE epoch_id=? AND kind='GAP' LIMIT 1",(epoch,)).fetchone():raise JournalError('EPOCH_NOT_READY')
                fd = digest(facts); fk = None
                if key is not None:
                    sc = json.loads(scope); fk = digest([sc['account_ref'], sc['pocket_uuid'], kind, key])
                    old = c.execute('SELECT facts_digest FROM entries WHERE fact_key=? ORDER BY seq LIMIT 1', (fk,)).fetchone()
                    if old:
                        if old[0] != fd: status, code = 'CONFLICT', 'FACT_CONFLICT'
                        elif status == 'RECORDED': status, code = 'DUPLICATE', 'SAME_FACT'
                last = c.execute('SELECT seq,entry_hash FROM entries ORDER BY seq DESC LIMIT 1').fetchone()
                seq, prev = (last[0]+1, last[1]) if last else (1, '0'*64)
                row = dict(seq=seq, id=str(uuid.uuid4()), epoch_id=epoch, kind=kind, status=status, code=code, scope=scope, order_id=order, trade_id=trade, fact_key=fk, facts_digest=fd, raw_digest=raw_hash_override if raw_hash_override is not None else (hashlib.sha256(raw.encode()).hexdigest() if raw is not None else None), raw=raw, facts=canonical(facts), numbers=canonical(numbers), start_us=start, end_us=end, uncertainty_us=uncertainty, previous_hash=prev)
                eh = digest(row)
                c.execute('INSERT INTO entries VALUES('+','.join('?' for _ in range(20))+')', tuple(row.values())+(eh,)); self._commit(c)
            return AppendResult(True, status, code, seq)
        except JournalError as exc: return AppendResult(False, 'REJECTED', str(exc))
        except (sqlite3.Error, OSError) as exc: return AppendResult(False, 'STORAGE_FAILED', self._store_code(exc))
    def observe(self, epoch, kind, raw_utf8, *, start_us, end_us, clock_uncertainty_us=None, query_window=None):
        if type(kind) is not str or kind not in KINDS or type(start_us) is not int or type(end_us) is not int or not 0 <= start_us <= end_us or (clock_uncertainty_us is not None and (type(clock_uncertainty_us) is not int or clock_uncertainty_us < 0)): return AppendResult(False, 'REJECTED', 'OBSERVATION_METADATA_INVALID')
        text = None
        try:
            if type(raw_utf8) is not bytes or len(raw_utf8) > 262144: raise JournalError('RESPONSE_SIZE_INVALID')
            text = raw_utf8.decode('utf-8')
            with localcontext() as ctx:
                ctx.prec = 512
                facts, numbers, key, code, order, trade = parse(kind, text)
            if query_window is not None:
                if kind!='REST_PAGE' or type(query_window) is not dict or set(query_window)!={'start_ms','end_ms','limit'} or any(type(v) is not int for v in query_window.values()) or not 0<=query_window['start_ms']<=query_window['end_ms'] or query_window['end_ms']-query_window['start_ms']>7*86400000 or not 1<=query_window['limit']<=40:raise JournalError('QUERY_WINDOW_INVALID')
                facts['query_window']=dict(query_window);key.append(query_window)
            return self._append(epoch, kind, 'RECORDED' if code == 'OBSERVED' else 'REVIEW', code, facts, numbers, text, start_us, end_us, clock_uncertainty_us, key, order, trade)
        except (JournalError, ValueError, TypeError, KeyError, ArithmeticError, UnicodeError) as exc:
            code = str(exc) if isinstance(exc, JournalError) else 'SCHEMA_INVALID'
            # Rejected raw is not retained: unknown JSON may include credentials.
            return self._append(epoch, kind, 'REVIEW', code, {}, [], None, start_us, end_us, clock_uncertainty_us, raw_hash_override=hashlib.sha256(raw_utf8).hexdigest() if type(raw_utf8) is bytes else None)
    def gap(self, epoch, code, at_us):
        if code not in CODES or type(at_us) is not int or at_us < 0: return AppendResult(False, 'REJECTED', 'GAP_INVALID')
        return self._append(epoch, 'GAP', 'REVIEW', code, {}, [], None, at_us, at_us)
    def page(self, epoch, raw_utf8, *, start_ms, end_ms, limit, observed_us):
        if type(start_ms) is not int or type(end_ms) is not int or not 0 <= start_ms <= end_ms or end_ms-start_ms > 7*86400000: return self.gap(epoch, 'MISSING_PAGE', observed_us)
        if type(limit) is not int or not 1 <= limit <= 40: return self.gap(epoch, 'MISSING_PAGE', observed_us)
        result = self.observe(epoch, 'REST_PAGE', raw_utf8, start_us=observed_us, end_us=observed_us, query_window=dict(start_ms=start_ms,end_ms=end_ms,limit=limit))
        if result.stored and result.status in ('RECORDED', 'DUPLICATE'):
            row = self.tail(result.seq-1, 1)[0]
            if json.loads(row['facts'])['count'] >= limit: return self.gap(epoch, 'MISSING_PAGE', observed_us)
        return result
    def tail(self, after=0, limit=40):
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 40: raise JournalError('BOUNDED_READ_REQUIRED')
        try:
            with self._connect(True) as c:
                c.row_factory = sqlite3.Row
                return [dict(x) for x in c.execute('SELECT * FROM entries WHERE seq>? ORDER BY seq LIMIT ?', (after, limit)).fetchall()]
        except (sqlite3.Error, OSError): raise JournalError('READ_FAILED') from None
    def verify_chain(self):
        """Bounded corruption check, NOT authentication of exchange evidence."""
        try:
            with self._connect(True) as c:
                c.row_factory=sqlite3.Row;c.execute('BEGIN');cursor=0;previous='0'*64
                while True:
                    rows=c.execute('SELECT * FROM entries WHERE seq>? ORDER BY seq LIMIT 40',(cursor,)).fetchall()
                    if not rows:return True
                    for value in rows:
                        row=dict(value);stored=row.pop('entry_hash')
                        if row['seq']!=cursor+1 or row['previous_hash']!=previous or digest(row)!=stored:return False
                        if row['raw'] is not None and hashlib.sha256(row['raw'].encode()).hexdigest()!=row['raw_digest']:return False
                        cursor=row['seq'];previous=stored
        except (sqlite3.Error,OSError,ValueError,TypeError):return False
    def backup_token(self):
        with self._connect(True) as c:
            c.execute('BEGIN')
            jid = c.execute('SELECT journal_id FROM journal_meta WHERE id=1').fetchone()[0]
            last = c.execute('SELECT seq,entry_hash FROM entries ORDER BY seq DESC LIMIT 1').fetchone()
            return dict(journal_id=jid, highwater=last[0] if last else 0, last_hash=last[1] if last else '0'*64)
    def snapshot(self, destination):
        dst = Path(destination).resolve()
        if self.root not in dst.parents or not dst.name.endswith('.journal.sqlite') or dst.exists(): raise JournalError('SNAPSHOT_TARGET_INVALID')
        try:
            with self._connect(True) as src, closing(sqlite3.connect(dst)) as target:
                target.execute('PRAGMA synchronous=FULL');src.backup(target,pages=40)
            copy = Journal(dst, test_root=self.root, timeout=self.timeout)
            return copy.backup_token()
        except JournalError: raise
        except (sqlite3.Error, OSError): raise JournalError('SNAPSHOT_FAILED') from None
    def validate_restore(self, token, observed_us):
        good = False
        try: good = token == self.backup_token() and self.verify_chain()
        except (sqlite3.Error, OSError): pass
        if not good: self.gap(None, 'BACKUP_MISMATCH', observed_us)
        return good
    def assess_cycle(self, buy_seq, sell_seq, checkpoints):
        """Read-only bounded local comparison. Always UNVERIFIED outside journal."""
        result = dict(status='REVIEW', reasons=[], independent_source_status='NOT_ESTABLISHED', history_complete=False, financial_verified=False, delivered=False, verified_issuance_count=0, atomicity='UNPROVEN')
        def fail(code):
            result['reasons'].append(code);return result
        if type(checkpoints) not in (list, tuple) or len(checkpoints) != 3 or any(type(x) is not int for x in [buy_seq, sell_seq, *checkpoints]) or len(set(checkpoints)) != 3: return fail('SELECTOR_INVALID')
        try:
            with self._connect(True) as c, localcontext() as ctx:
                ctx.prec=512; c.row_factory=sqlite3.Row; c.execute('BEGIN')
                selected=[c.execute('SELECT * FROM entries WHERE seq=?',(n,)).fetchone() for n in (buy_seq,sell_seq,*checkpoints)]
                if any(x is None for x in selected): return fail('REFERENCE_MISSING')
                b,s,*points=[dict(x) for x in selected]
                if any(x['status'] not in ('RECORDED','DUPLICATE') for x in selected): return fail('REVIEW_REFERENCE')
                scopes=[json.loads(x['scope']) for x in selected]
                account=lambda x:(x['account_ref'],x['pocket_uuid'])
                if len({account(x) for x in scopes}) != 1: return fail('SCOPE_CONFLICT')
                if b['kind']!='REST_ORDER' or s['kind']!='REST_ORDER' or any(x['kind']!='MYASSET' for x in points): return fail('CHECKPOINT_TIME_UNPROVEN')
                if any(x['uncertainty_us'] != 0 for x in selected): return fail('CLOCK_UNCERTAIN')
                buy,sell = json.loads(b['facts']),json.loads(s['facts']); pts=[json.loads(x['facts']) for x in points]
                if buy['side']!='bid' or sell['side']!='ask' or any(x['state']!='done' or x['ord_type']!='limit' or len(x['trades'])!=1 for x in (buy,sell)): return fail('UNSUPPORTED_CYCLE')
                bt,st=buy['trades'][0],sell['trades'][0];times=[x['at_us'] for x in pts]
                if not times[0] < buy['created_us'] <= bt['at_us'] <= times[1] < sell['created_us'] <= st['at_us'] <= times[2]: return fail('CHECKPOINT_ORDER_CONFLICT')
                totals=lambda p,ccy:Decimal(p['balances'][ccy]['total'])
                q=Decimal(bt['quantity'])
                if q<=0 or q!=Decimal(st['quantity']) or [totals(p,'BTC') for p in pts]!=[Decimal(0),q,Decimal(0)]: return fail('BTC_POSITION_CONFLICT')
                if totals(pts[0],'KRW')-totals(pts[1],'KRW')!=Decimal(bt['funds'])+Decimal(buy['fee']) or totals(pts[2],'KRW')-totals(pts[1],'KRW')!=Decimal(st['funds'])-Decimal(sell['fee']): return fail('CASH_LEG_CONFLICT')
                high=c.execute('SELECT coalesce(max(seq),0) FROM entries').fetchone()[0];result['observed_highwater']=high
                cursor=0;seen=set();max_batch=0
                while cursor<high:
                    rows=c.execute('SELECT * FROM entries WHERE seq>? AND seq<=? ORDER BY seq LIMIT 40',(cursor,high)).fetchall();max_batch=max(max_batch,len(rows))
                    if not rows: break
                    for row in rows:
                        cursor=row['seq'];row=dict(row)
                        if row['scope'] is None:
                            if row['code']=='BACKUP_MISMATCH':return fail('RESTORE_REVIEW')
                            continue
                        if account(json.loads(row['scope']))!=account(scopes[0]):continue
                        f=json.loads(row['facts']);at=f.get('at_us') or f.get('created_us')
                        active=(min(checkpoints)<=row['seq']<=max(checkpoints)) or (row['kind']=='GAP' and row['seq']>=min(checkpoints)) or (at is not None and times[0]<=at<=times[2])
                        if not active:continue
                        if row['status'] in ('REVIEW','CONFLICT'):return fail('GAP_OR_CONFLICT')
                        if row['kind'] in ('REST_ORDER','MYORDER') and f['order_id'] not in (buy['order_id'],sell['order_id']):return fail('OTHER_ORDER_INTERVENED')
                        if row['kind']=='MYORDER' and f['state']=='trade':
                            wanted=bt if f['order_id']==buy['order_id'] else st;order=buy if f['order_id']==buy['order_id'] else sell
                            if f['trade_id']!=wanted['trade_id'] or f['side']!=('BID' if order['side']=='bid' else 'ASK') or any(Decimal(f[k])!=Decimal(wanted[k]) for k in ('price','quantity')) or Decimal(f['fee'])!=Decimal(order['fee']) or f['at_us']!=wanted['at_us']:return fail('TRADE_CHANNEL_CONFLICT')
                            seen.add(f['order_id'])
                result['max_read_batch']=max_batch
                if seen != {buy['order_id'],sell['order_id']}:return fail('MISSING_TRADE_CHANNEL')
                result.update(status='LOCAL_CONSISTENCY_ONLY',reasons=['EXCHANGE_COVERAGE_AND_INDEPENDENCE_UNPROVEN'])
                return result
        except (sqlite3.Error,OSError,KeyError,ValueError,TypeError,ArithmeticError):return fail('ASSESSMENT_UNPROVEN')
