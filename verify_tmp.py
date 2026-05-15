import asyncio, json, sqlite3, sys, time
from datetime import datetime, timedelta
import websockets
sys.path.insert(0, "/root")
from ws_common import WS_URL, WS_HEADERS, get_device_id, get_login_token, handshake_payload, make_msg

conn = sqlite3.connect("/root/matches.db")
alert = set()
with open("/root/channels/alert_channels.txt") as f:
    for line in f:
        s = line.strip()
        if s and not s.startswith("#"): alert.add(s.lower())
rows = conn.execute("""SELECT channel_link, chat_id, channel_title, COUNT(*)
    FROM messages WHERE saved_at >= datetime('now','localtime','-24 hours')
    GROUP BY channel_link ORDER BY 4 DESC""").fetchall()
TARGETS=[]
for link, cid, title, posts in rows:
    al = link.rsplit("/",1)[-1].lower()
    if al not in alert:
        TARGETS.append((al, cid, title))
        if len(TARGETS)>=10: break

class C:
    def __init__(self):
        self._t=get_login_token(); self._d=get_device_id()
        self._s=0; self._p={}; self._w=None; self._r=None
    def _n(self): self._s+=1; return self._s
    async def _rl(self):
        try:
            async for raw in self._w:
                try: m=json.loads(raw)
                except: continue
                seq=m.get("seq")
                if m.get("cmd",0) in (1,3) and seq in self._p:
                    f=self._p.pop(seq)
                    if not f.done(): f.set_result(m)
        except: pass
    async def send_op(self, op, payload, timeout=15):
        s=self._n(); f=asyncio.get_running_loop().create_future()
        self._p[s]=f
        await self._w.send(make_msg(s,op,payload))
        try: return await asyncio.wait_for(f,timeout=timeout)
        except asyncio.TimeoutError:
            self._p.pop(s,None); return None
    async def connect(self):
        self._w=await websockets.connect(WS_URL,additional_headers=WS_HEADERS,
            ping_interval=30,open_timeout=15,close_timeout=10)
        self._r=asyncio.create_task(self._rl())
        h=await self.send_op(6,handshake_payload(self._d))
        if not h or h.get("cmd")==3: raise RuntimeError("hs")
        l=await self.send_op(19,{"token":self._t},timeout=15)
        if not l or l.get("cmd")==3: raise RuntimeError("login")
    async def close(self):
        if self._w:
            try: await asyncio.wait_for(self._w.close(),timeout=5)
            except: pass
        if self._r: self._r.cancel()

async def fetch(c,cid,since):
    out=[]; seen=set()
    cur=int(time.time()*1000)+60_000
    for _ in range(50):
        r=await c.send_op(49,{"chatId":cid,"from":cur,"forward":0,"backward":100,"getMessages":True})
        await asyncio.sleep(0.05)
        if not r or r.get("cmd")==3: break
        ms=(r.get("payload") or {}).get("messages") or []
        if not ms: break
        old=None
        for m in ms:
            t=m.get("time",0) or 0
            if old is None or t<old: old=t
            if t<since: continue
            mid=str(m.get("id",""))
            if not mid or mid in seen: continue
            seen.add(mid)
            out.append({"msg_id":mid,"time":t})
        if old is None or old<=since: break
        if old>=cur: break
        cur=old
    return out

async def main():
    client=C()
    await client.connect()
    try:
        for label, hours in [("1h",1),("3h",3),("6h",6),("12h",12),("24h",24)]:
            since_ms=int((datetime.now()-timedelta(hours=hours)).timestamp()*1000)
            gm=gd=0
            for al,cid,title in TARGETS:
                p=await fetch(client,cid,since_ms)
                if not p: continue
                mids=[x["msg_id"] for x in p]
                ph=",".join("?"*len(mids))
                r=conn.execute(f"SELECT msg_id FROM messages WHERE chat_id=? AND msg_id IN ({ph})",[cid]+mids).fetchall()
                ds={x[0] for x in r}
                gm+=len(p); gd+=len(ds)
            pct=100*gd/gm if gm else 0
            print(f"== {label}: MAX={gm:>4}, BD={gd:>4}, miss={gm-gd:>3}, coverage={pct:5.1f}% ==")
    finally:
        await client.close()
asyncio.run(main())
