"""Local settings: encrypted at rest, atomic updates, environment fallback.
Single process deployment; never return credentials to the client.
"""
import json, os, re, secrets, tempfile, threading, hashlib
from pathlib import Path
from datetime import date
from urllib.parse import parse_qsl, urlencode
from cryptography.fernet import Fernet, InvalidToken
from .db import DEMO

KEYS=('FRED_API_KEY','ECOS_API_KEY','KOSIS_API_KEY','KIS_APP_KEY','KIS_APP_SECRET','DART_API_KEY','OPENAI_API_KEY','GEMINI_API_KEY','ANTHROPIC_API_KEY','TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID')
QUERIES=('KOSIS_LEADING_QUERY','KOSIS_COINCIDENT_QUERY')
# Candidate presets from public query examples. The provider verifies the returned series identity.
KOSIS_PRESETS={k:f'orgId=101&tblId=DT_1C8015&itmId=T1&objL1={code}' for k,code in zip(QUERIES,('A03','B03'))}
DEFAULTS={**dict.fromkeys(KEYS+QUERIES,''),'HISTORY_START':'1995-01-01','AUTO_COLLECT':'0','COLLECT_HOUR_KST':'8','AI_PROVIDER':'off','OPENAI_MODEL':'','GEMINI_MODEL':'','ANTHROPIC_MODEL':'','AI_DAILY_LIMIT':'3','AI_MAX_OUTPUT':'1200','BLS_AUTO_REFRESH':'0','ALERT_ENABLED':'0','ALERT_DAILY':'1','ALERT_HOUR_KST':'9','ALERT_RISK':'1','ALERT_FLOW':'1','ALERT_CALENDAR':'1','ALERT_DIVIDEND':'1','ALERT_PRICE_RULES':'','BACKUP_ENABLED':'0','BACKUP_HOUR_KST':'4','BACKUP_KEEP':'3'}
GUARD=threading.RLock()

def directory():
    root=Path(os.getenv('SETTINGS_DIR','./data/settings')) / ('demo' if DEMO else 'live')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    return root

def private_write(path, content):
    fd,name=tempfile.mkstemp(dir=path.parent,prefix='.tmp-')
    try:
        with os.fdopen(fd,'wb') as f:
            f.write(content); f.flush(); os.fsync(f.fileno())
        os.replace(name,path)
    finally:
        if os.path.exists(name): os.unlink(name)

def secret_file(name, factory):
    with GUARD:
        path=directory()/name
        if not path.exists(): private_write(path,factory())
        return path.read_bytes().strip()

def password():
    value=os.getenv('ADMIN_PASSWORD','')
    if value:
        if len(value)<12: raise RuntimeError('ADMIN_PASSWORD must be at least 12 characters')
        return value
    return secret_file('admin-password.txt',lambda: secrets.token_urlsafe(24).encode()).decode()

def cipher(): return Fernet(secret_file('settings.key',Fernet.generate_key))

def load_settings():
    with GUARD:
        out={k:os.getenv(k,v).strip() for k,v in DEFAULTS.items()}
        path=directory()/'settings.enc'
        if path.exists():
            try: data=json.loads(cipher().decrypt(path.read_bytes()))
            except (InvalidToken,ValueError,OSError): raise RuntimeError('저장 설정 복호화 실패: settings.key 및 settings.enc 확인') from None
            out.update({k:str(v) for k,v in data.items() if k in DEFAULTS})
        # Query strings may contain API keys from generated URLs; always sanitize.
        for k in QUERIES: out[k]=effective_kosis_query(k,out[k],strict=False)
        return out

def clean_query(value,strict=True):
    if not value.strip(): return ''
    q=dict(parse_qsl(value.split('?',1)[-1],keep_blank_values=False))
    allowed={'orgId','tblId','itmId',*(f'objL{i}' for i in range(1,9))}
    out={k:v.strip().strip('+').strip() for k,v in q.items() if k in allowed}
    if strict and any(re.search(r'[+\s,]',v) for v in out.values()):
        raise ValueError('KOSIS 단일 항목만 선택하세요.')
    if strict and not all(out.get(k) for k in ('orgId','tblId','itmId','objL1')):
        raise ValueError('KOSIS URL: orgId, tblId, itmId, objL1 필수')
    return urlencode(sorted(out.items()))

def effective_kosis_query(name,value,strict=True):
    return clean_query(value if value.strip() else KOSIS_PRESETS[name],strict=strict)

def query_fingerprint(value):
    return hashlib.sha256(clean_query(value,strict=False).encode()).hexdigest()

def save_settings(updates):
    with GUARD:
        path=directory()/'settings.enc'; stored={}
        if path.exists():
            try: stored=json.loads(cipher().decrypt(path.read_bytes()))
            except (InvalidToken,ValueError,OSError): raise RuntimeError('저장 설정 복호화 실패') from None
        for k,v in updates.items():
            if k not in DEFAULTS: raise ValueError('허용되지 않은 설정 항목')
            if not isinstance(v,str): raise ValueError('설정값은 문자열이어야 합니다.')
            v=v.strip()
            if k in ('TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID') and v:
                pattern=r'[0-9]{5,20}:[A-Za-z0-9_-]{20,128}' if k=='TELEGRAM_BOT_TOKEN' else r'[1-9][0-9]{0,18}'
                if not re.fullmatch(pattern,v): raise ValueError('Telegram token or private chat ID format')
            if k in KEYS and k not in ('TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID') and v and (len(v)>512 or not re.fullmatch(r'[A-Za-z0-9_+=./-]+',v) or (k not in ('KIS_APP_KEY','KIS_APP_SECRET','KOSIS_API_KEY') and '/' in v)):
                raise ValueError('API 키 형식을 확인하세요.')
            if k in QUERIES: v=effective_kosis_query(k,v)
            if k=='HISTORY_START':
                d=date.fromisoformat(v)
                if not date(1900,1,1)<=d<=date.today(): raise ValueError('조회 시작일 범위를 확인하세요.')
            if k in ('AUTO_COLLECT','BLS_AUTO_REFRESH','ALERT_ENABLED','ALERT_DAILY','ALERT_RISK','ALERT_FLOW','ALERT_CALENDAR','ALERT_DIVIDEND','BACKUP_ENABLED') and v not in ('0','1'): raise ValueError('자동수집 설정값 오류')
            if k in ('COLLECT_HOUR_KST','ALERT_HOUR_KST','BACKUP_HOUR_KST') and (not v.isdigit() or not 0<=int(v)<=23): raise ValueError('수집 시각은 0~23시')
            if k=='BACKUP_KEEP' and v not in ('1','2','3','4','5','6','7'): raise ValueError('Backup retention 1-7')
            if k=='AI_PROVIDER' and v not in ('off','openai','gemini','anthropic'): raise ValueError('AI 제공처 설정 오류')
            if k in ('OPENAI_MODEL','GEMINI_MODEL','ANTHROPIC_MODEL') and v and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}',v): raise ValueError('모델 ID 형식 오류')
            if k=='AI_DAILY_LIMIT' and (not v.isascii() or not v.isdigit() or not 1<=int(v)<=20): raise ValueError('하루 1~20회')
            if k=='AI_MAX_OUTPUT' and (not v.isascii() or not v.isdigit() or not 256<=int(v)<=3000): raise ValueError('출력 토큰 256~3000')
            if k=='ALERT_PRICE_RULES':
                from .alert_rules import normalize_rules
                v=normalize_rules(v)
            stored[k]=v
        candidate={k:stored.get(k,os.getenv(k,v).strip()) for k,v in DEFAULTS.items()}
        if candidate['ALERT_ENABLED']=='1' and not all(candidate[k] for k in ('TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID')):
            raise ValueError('Telegram token and chat ID required')
        private_write(path,cipher().encrypt(json.dumps(stored,ensure_ascii=False).encode()))
        return load_settings()

def public_settings():
    c=load_settings()
    return {'configured':{k:bool(c[k]) for k in KEYS},'values':{k:c[k] for k in DEFAULTS if k not in KEYS},'demo':DEMO,'kosis_presets':{k:effective_kosis_query(k,'') for k in QUERIES}}
