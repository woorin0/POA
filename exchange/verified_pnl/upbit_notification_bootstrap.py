"""Explicit opt-in notification host wiring. No .env/secret discovery or API IO.

A trusted deployment must supply a LOCAL external-registry evidence resolver and
an independent order-query credential provider. Configuration strings alone are
not account/pocket identity evidence. No financial issuer/provider is registered.
"""
import hashlib,os,re,time,uuid
from dataclasses import dataclass,field
from .upbit_notification_get import UpbitNotificationGET,UpbitGETError
from .bootstrap import SpotScope,AuthenticatedAccount
from .upbit_spot_read import UpbitSpotReads,UpbitNotificationHost,configure_upbit_notification_host
from .estimated_pnl import text

@dataclass(frozen=True,repr=False)
class UpbitReadConfig:
    account_uuid:str
    pocket_uuid:str
    source_account_ref:str
    strategy:str
    read_key_fingerprint:str
    producer:str='POA_MAIN_LOG_V1'
    def validate(self):
        for value in (self.account_uuid,self.pocket_uuid):
            if type(value) is not str or str(uuid.UUID(value))!=value:raise ValueError('UUID_REQUIRED')
        if not re.fullmatch('[0-9a-f]{64}',self.source_account_ref) or not re.fullmatch('[0-9a-f]{64}',self.read_key_fingerprint):raise ValueError('REFERENCE_REQUIRED')
        text(self.strategy)
        if self.strategy in ('default','주문') or self.producer!='POA_MAIN_LOG_V1':raise ValueError('PRODUCER_REQUIRED')

@dataclass(frozen=True,repr=False)
class ExternalAccountBinding:
    """Result from an approved external-registry verifier, NOT self-verified JSON.

    The registered resolver is the trust root for external evidence authenticity.
    This module checks bindings/expiry; it cannot prove off-platform records itself.
    """
    account_uuid:str
    pocket_uuid:str
    source_account_ref:str
    strategy:str
    read_key_fingerprint:str
    producer:str
    permissions:frozenset
    evidence_source:str
    evidence_reference:str
    evidence_digest:str
    expires_at:float

@dataclass(frozen=True,repr=False)
class UpbitReadSources:
    config:UpbitReadConfig
    credential_provider:object=field(repr=False)
    binding_resolver:object=field(repr=False)
    transport:object=field(default=None,repr=False)

@dataclass(frozen=True)
class HostPreparation:
    enabled:bool
    status:str
    host:object=field(default=None,repr=False)

_SOURCES=None


def configure_upbit_read_sources(sources):
    """Trusted local application bootstrap only; never consumes a webhook/env dict."""
    global _SOURCES
    _SOURCES=None
    if sources is None:return True
    try:
        if type(sources) is not UpbitReadSources or type(sources.config) is not UpbitReadConfig:return False
        sources.config.validate()
        if not callable(sources.credential_provider) or not callable(sources.binding_resolver) or (sources.transport is not None and not callable(sources.transport)):return False
        _SOURCES=sources;return True
    except Exception:return False


def _binding(sources):
    cfg=sources.config
    try:
        proof=sources.binding_resolver(cfg) # LOCAL approved verifier; no Upbit IO or secret loading.
        if type(proof) is not ExternalAccountBinding:raise ValueError()
        for name in ('account_uuid','pocket_uuid','source_account_ref','strategy','read_key_fingerprint','producer'):
            if getattr(proof,name)!=getattr(cfg,name):raise ValueError()
        if proof.permissions!=frozenset({'order_query'}) or type(proof.expires_at) not in (int,float) or not time.time()<proof.expires_at<time.time()+366*86400:raise ValueError()
        for value in (proof.evidence_source,proof.evidence_reference):
            if type(value) is not str or not 1<=len(value)<=256 or not value.isprintable():raise ValueError()
        if not re.fullmatch('[0-9a-f]{64}',proof.evidence_digest):raise ValueError()
        return proof
    except Exception:raise UpbitGETError('ACCOUNT_BINDING_UNAVAILABLE') from None


def prepare_upbit_notification_host(*,environment=None):
    env=os.environ if environment is None else environment
    flag=env.get('POA_UPBIT_ESTIMATED_READ_ENABLED','false')
    if type(flag) is str and flag.strip().lower()=='false':return HostPreparation(False,'DISABLED')
    if type(flag) is not str or flag.strip().lower()!='true':
        configure_upbit_notification_host(None);return HostPreparation(False,'INVALID_OPT_IN')
    sources=_SOURCES
    try:
        if sources is None:raise UpbitGETError('READ_SOURCES_NOT_CONFIGURED')
        cfg=sources.config;cfg.validate();_binding(sources)
        def guard(fingerprint):
            proof=_binding(sources)
            if fingerprint!=proof.read_key_fingerprint:raise UpbitGETError('CREDENTIAL_BINDING_MISMATCH')
        def identity(client):
            proof=_binding(sources)
            return AuthenticatedAccount('UPBIT',proof.account_uuid,'pocket:'+proof.pocket_uuid,frozenset({'read'}))
        get=UpbitNotificationGET(sources.credential_provider,guard,transport=sources.transport)
        scope=SpotScope('UPBIT',cfg.account_uuid,'pocket:'+cfg.pocket_uuid,cfg.source_account_ref,'BTC/KRW','KRW-BTC','KRW','notification-read-stream',cfg.strategy)
        reader=UpbitSpotReads(scope,get,identity,pocket_uuid=cfg.pocket_uuid,as_of_ms=int(time.time()*1000))
        # Native order timestamp upper bound must follow the clock, not startup time.
        # Only refresh before a read; no network or key lookup at construction.
        original_get=reader._get
        def observed_get(path,params):
            reader.as_of_ms=int(time.time()*1000)
            try:return original_get(path,params)
            finally:reader.as_of_ms=int(time.time()*1000)
        reader._get=observed_get
        host=UpbitNotificationHost(reader,source_account_ref=cfg.source_account_ref,strategy=cfg.strategy)
        return HostPreparation(True,'HOST_CONFIGURED_EXTERNAL_EVIDENCE_REQUIRED',host)
    except Exception:
        configure_upbit_notification_host(None)
        return HostPreparation(True,'READ_HOST_CONFIGURATION_REJECTED')
