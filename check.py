#!/usr/bin/env python3
"""
Проверка пропущенных звонков и алерт в WhatsApp. Запускается по cron (GitHub Actions)
каждые ~5 мин. Опрашивает OnlinePBX, находит «пропущен >SLA мин без перезвона»,
шлёт сообщение в заданный WhatsApp-чат. Дедуп — через alerted.json (коммитится обратно).

Всё чувствительное — в ENV/секретах:
  ONLINEPBX_API_KEY, ONLINEPBX_DOMAIN
  WHATCRM_KEY, WHATCRM_TOKEN
  WA_ALERT_CHAT          — chatId назначения
  SLA_MIN(=5), MAX_AGE_MIN(=12), SHIFT_FROM(=6), SHIFT_TO(=24)  — опц.
"""
import os, ssl, json, re, time, urllib.request, urllib.parse
from datetime import datetime

KEY=os.environ['ONLINEPBX_API_KEY']; DOM=os.environ['ONLINEPBX_DOMAIN']; BASE=f'https://api.onlinepbx.ru/{DOM}'
WHKEY=os.environ['WHATCRM_KEY']; WHTOK=os.environ['WHATCRM_TOKEN']; WA_CHAT=os.environ['WA_ALERT_CHAT']
SLA_MIN=int(os.getenv('SLA_MIN','5')); MAX_AGE_MIN=int(os.getenv('MAX_AGE_MIN','25'))
SHIFT_FROM=int(os.getenv('SHIFT_FROM','6')); SHIFT_TO=int(os.getenv('SHIFT_TO','24'))
CTX=ssl.create_default_context(); CTX.check_hostname=False; CTX.verify_mode=ssl.CERT_NONE
STATE='alerted.json'

def tail10(s):
    d=re.sub(r'\D','',s or ''); return d[-10:] if len(d)>=10 else d
def client(c):
    a=tail10(c.get('caller_id_number','')); b=tail10(c.get('destination_number',''))
    return a if len(a)==10 else (b if len(b)==10 else '')
def uexts(c): return [e.get('number') for e in (c.get('events') or []) if e.get('type')=='user']

def pbx(frm,to):
    auth=urllib.request.urlopen(urllib.request.Request(f'{BASE}/auth.json',
        data=urllib.parse.urlencode({'auth_key':KEY}).encode()), context=CTX, timeout=25)
    ad=json.load(auth)['data']
    body=json.dumps({'start_stamp_from':frm,'start_stamp_to':to,'limit':5000}).encode()
    req=urllib.request.Request(f'{BASE}/mongo_history/search.json', data=body,
        headers={'x-pbx-authentication':f"{ad['key_id']}:{ad['key']}",'Content-Type':'application/json'}, method='POST')
    return (json.load(urllib.request.urlopen(req, context=CTX, timeout=60)) or {}).get('data') or []

def send_wa(text):
    body=json.dumps({'chatId':WA_CHAT,'body':text}).encode()
    req=urllib.request.Request(f'https://api.whatcrm.net/instances/{WHKEY}/sendMessage', data=body,
        headers={'X-Crm-Token':WHTOK,'Content-Type':'application/json'}, method='POST')
    urllib.request.urlopen(req, context=CTX, timeout=40)

def load_state():
    try:
        with open(STATE) as f: return json.load(f)
    except Exception: return {}
def save_state(d):
    cutoff=int(time.time())-2*86400
    d={k:v for k,v in d.items() if v>cutoff}      # держим только свежие 2 дня
    with open(STATE,'w') as f: json.dump(d,f)

def main():
    now=int(time.time())
    calls=pbx(now-MAX_AGE_MIN*60-3600, now)
    inb=[c for c in calls if c.get('accountcode')=='inbound' and client(c)]
    out=[c for c in calls if c.get('accountcode')=='outbound' and client(c)]
    from collections import defaultdict
    by=defaultdict(list); out_by=defaultdict(list)
    for c in inb: by[client(c)].append(c)
    for c in out: out_by[client(c)].append(c['start_stamp'])
    state=load_state(); sent=0; checked=0
    for ph,legs in by.items():
        talk=max((l.get('user_talk_time',0) or 0) for l in legs)
        rang=any(uexts(l) for l in legs); maxd=max(l.get('duration',0) for l in legs)
        if talk>0: continue
        if not rang and maxd<=2: continue                 # автодозвон/сброс в очереди
        t0=min(l['start_stamp'] for l in legs); age=(now-t0)/60
        if age<SLA_MIN or age>MAX_AGE_MIN: continue
        if any(t>=t0 for t in out_by.get(ph,[])): continue # уже перезвонили
        dt=datetime.fromtimestamp(t0)
        if not (SHIFT_FROM<=dt.hour<SHIFT_TO): continue
        key=f"{ph}-{dt:%Y%m%d-%H%M}"
        if key in state: continue
        checked+=1
        txt=f"⚠️ Пропущенный {dt:%H:%M} от +7{ph} — {round(age)} мин без перезвона. Кто свободен, перезвоните 🙏"
        try:
            send_wa(txt); state[key]=now; sent+=1
        except Exception as e:
            main.last_err=f"{type(e).__name__}: {str(e)[:120]}"
            print('wa err', e, flush=True)
    save_state(state)
    err=getattr(main,'last_err','')
    return f"{datetime.fromtimestamp(now):%H:%M} вх={len(inb)} брейчей={checked} отправлено={sent}"+(f" | SENDERR: {err}" if err else "")

if __name__=='__main__':
    from datetime import datetime as _dt
    try:
        msg=main(); hb=f"{msg}"
    except Exception as e:
        import traceback
        hb=f"ОШИБКА: {type(e).__name__}: {e}"
        traceback.print_exc()
    try:
        with open('heartbeat.txt','w') as f: f.write(hb+"\n")
    except Exception: pass
    print(hb, flush=True)
