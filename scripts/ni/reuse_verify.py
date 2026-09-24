# -*- coding: utf-8 -*-
"""Verify reusable model-raw outputs against raw source images (violation cleanup + fidelity)."""
import json, sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.path.insert(0, str(Path(__file__).resolve().parent))
import oxygen_verify as V
import batch as ni_batch

R = ni_batch.root_from_argv()
reuse = json.loads((R/"reports"/"damage-reuse-plan.json").read_text(encoding="utf-8"))
key = V.load_key()
pairs = []
for row in reuse:
    comp = Path(row["compliant"])
    item, name = comp.parent.name, comp.name
    stem = name.rsplit(".",1)[0]
    src_raw = R/"images"/"raw"/item/name
    if not src_raw.is_file():
        src_raw = R/"images"/"raw"/item/(stem+".jpg")
    if not src_raw.is_file():
        continue
    pairs.append((item, name, src_raw, Path(row["modelOut"])))
print("pairs:", len(pairs), flush=True)
outp = R/"reports"/"reuse-verify.jsonl"
done = set()
if outp.is_file():
    for line in outp.read_text(encoding="utf-8-sig").splitlines():
        line=line.strip()
        if line:
            d=json.loads(line)
            if d.get("ok"): done.add((d["item"],d["name"]))
todo=[t for t in pairs if (t[0],t[1]) not in done]
import threading
f=open(outp,"a",encoding="utf-8"); lock=threading.Lock(); n=[0]
def work(t):
    item,name,src,out=t
    rec={"item":item,"name":name,"output":str(out)}
    try:
        v=V.call(key,src,out)
        ok=all(v.get(k) for k in ("productSame","violationsCleared","noNewArtifacts","qualityOk"))
        rec.update(ok=ok,verdict=v)
    except Exception as e:
        rec.update(ok=False,error=str(e)[:200])
    with lock:
        f.write(json.dumps(rec,ensure_ascii=False)+"\n"); f.flush(); n[0]+=1
        if n[0]%20==0: print("  done",n[0],"/",len(todo),flush=True)
with ThreadPoolExecutor(max_workers=10) as ex:
    futs=[ex.submit(work,t) for t in todo]
    for fu in as_completed(futs): fu.result()
f.close(); print("finished",n[0],flush=True)
