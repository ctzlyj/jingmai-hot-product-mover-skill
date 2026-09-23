# -*- coding: utf-8 -*-
"""Routed internal-model pool (no-inventory mode).
Model routing per vendor intro:
  - person-depiction tasks -> Oxygen-Imagen (person removal + background fill)
  - everything else        -> Oxygen-Product-Pro (product preservation + text rendering)
A task is skipped when a valid output already exists in EITHER model dir:
existing outputs are reused and judged by verification instead of regenerating.
"""
import argparse, base64, io, json, os, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import urllib.request, urllib.error

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402

ROOT = ni_batch.root_from_argv()
HELPER = str(Path.home() / ".joycode" / "skills" / "sufy-image2" / "scripts" / "read_dpapi_credential.ps1")
DPAPI = Path(os.environ["LOCALAPPDATA"]) / "JoyCode" / "credentials" / "oxygen-product-pro.dpapi"
API = "http://llm-gw.jd.local/v1/images/edits"
PRO_DIR = ROOT / "image-edits-pro"
IMG_DIR = ROOT / "image-edits-imagen"

VIO = {
 "brand": "品牌名、品牌Logo、商标、包装上的品牌字样",
 "flag": "旗舰字样",
 "official": "官方、正品字样与认证章",
 "phone": "电话、手机号、微信、二维码等联系方式",
 "patent": "专利字样与专利证书图标",
 "factory": "厂家直销、源头工厂字样",
 "thirdparty": "淘宝、天猫、拼多多、88VIP等第三方平台标识、链接与活动信息",
 "watermark": "其他店铺或平台的水印",
 "person_name": "画面文字里出现的人名",
 "price": "画面中的价格数字、价格标签与促销价格信息",
 "water_circle": "圆形水印角标",
 "taojinbi": "淘金币标识",
}

def load_key():
    p = subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",HELPER,str(DPAPI)],
        capture_output=True, text=True, encoding="utf-8", timeout=30)
    key=(p.stdout or "").strip()
    if p.returncode==0 and key: return key
    raise SystemExit("NO_KEY")

def pick_size(w,h):
    r=w/h
    if r>1.15: return "1344x1024"
    if r<0.87: return "1024x1344"
    return "1024x1024"

def build_strict_prompt(vios, damage):
    kinds=[VIO[v] for v in vios if v in VIO and v != "person"]
    removal="清除图中"+ "、".join(kinds) if kinds else "整体精修画质"
    if damage:
        removal+="；修复画面损伤（空白块、文字残缺、错位、色带接缝、马赛克），把残缺文字修复为清晰正确的文字"
    return (
     "这是一次最小化编辑任务，不是重新创作。输出必须与输入几乎完全相同：相同的构图、相同的商品、相同的背景、相同的文字。"
     "逐字保留图中所有说明文字、数据、表格、报告内容，不得改写、删减、截断或新增任何文字。"
     "唯一允许的改动：" + removal + "。"
     "移除处仅用紧邻的背景色/图案自然填补，不添加任何新元素、新文字、新装饰、新商品。"
     "商品外形、颜色、数量、包装文字、拍摄角度、光线、色调整体保持原样。"
    )

def build_person_prompt(vios, damage):
    """Verified person policy: removal + natural background fill (never replacement)."""
    kinds=[VIO[v] for v in vios if v in VIO and v not in ("person","person_name")]
    p=(
     "这是一次人物移除编辑任务，不是重新创作。将原图中的所有人物（真人、手部、模特、卡通人物、插画人物、3D人物、剪影）"
     "整体移除，人物所在区域仅用紧邻的背景色/图案自然填补完整，不得留白、空洞或马赛克，不添加任何新人物或新元素。"
    )
    if kinds:
        p+="同时清除图中的"+"、".join(kinds)+"。"
    if "person_name" in vios:
        p+="画面文字中出现的人名一并清除。"
    if damage:
        p+="修复画面损伤，把残缺文字修复为清晰正确的文字。"
    p+=(
     "除上述改动外，画面其余部分必须与原图完全一致：相同的构图、相同的商品、相同的背景、相同的文案。"
     "逐字保留其余说明文字、数据、表格内容，不得改写、删减或新增。"
     "商品外形、颜色、数量、包装文字、拍摄角度、光线、色调整体保持原样。"
    )
    return p

def call_model(key, model, src, prompt, size, timeout=300):
    b64=base64.b64encode(Path(src).read_bytes()).decode("ascii")
    body={"model":model,"prompt":prompt,"size":size,
          "image":["data:image/jpeg;base64,"+b64]}
    req=urllib.request.Request(API, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type":"application/json","Authorization":"Bearer "+key})
    d=None
    for attempt in range(1,7):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d=json.loads(r.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            if e.code in (429,502,503,504) and attempt<6:
                time.sleep(min(4*attempt,30))
                continue
            raise
    if not d: raise RuntimeError("no-response")
    url=(d.get("data") or [{}])[0].get("url")
    if not url: raise RuntimeError("no-url:"+json.dumps(d,ensure_ascii=False)[:200])
    for attempt in range(1,4):
        try:
            with urllib.request.urlopen(url, timeout=180) as r2:
                data=r2.read()
            if len(data)>=8000: return data
        except Exception:
            time.sleep(2*attempt)
    raise RuntimeError("tiny-output")

def valid_out(p):
    return p.is_file() and p.stat().st_size>8000

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan", default="reports/regen-plan.json")
    ap.add_argument("--route", required=True, choices=("pro","imagen","imagen-np"))
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=2)
    a=ap.parse_args()
    key=load_key()
    model="Oxygen-Product-Pro" if a.route=="pro" else "Oxygen-Imagen"
    outdir=PRO_DIR if a.route=="pro" else IMG_DIR
    other=IMG_DIR if a.route=="pro" else PRO_DIR
    plan=json.loads((ROOT/a.plan).read_text(encoding="utf-8"))
    dec=json.loads((ROOT/"reports"/"people-broad-decisions.json").read_text(encoding="utf-8"))
    notperson={(d["item"],d["name"]) for d in dec.get("reverted") or []}
    ordered=[]
    for p,v in plan.items():
        path=Path(p)
        item,name=path.parent.name,path.name
        vios=[x[10:] for act,x in v["acts"] if act=="REGEN" and x.startswith("violation:")]
        dmg=any(act=="REGEN" and not x.startswith("violation:") for act,x in v["acts"])
        if not dmg and set(vios)<={"person","person_name"} and (item,name) in notperson:
            continue
        is_person="person" in vios
        if a.route=="imagen" and not is_person: continue
        if a.route=="imagen-np" and is_person: continue
        ordered.append((path,vios,dmg))
    sh_n,_,sh_m=a.shard.partition("/")
    sh_n,sh_m=int(sh_n),int(sh_m)
    tasks=[]
    for idx,(path,vios,dmg) in enumerate(ordered):
        if idx%sh_m!=sh_n: continue
        stem=path.name.rsplit(".",1)[0]
        out=outdir/path.parent.name/(stem+".png")
        if valid_out(out): continue
        if valid_out(other/path.parent.name/(stem+".png")): continue  # reuse, verify later
        tasks.append((path,out,vios,dmg,is_person))
    if a.limit: tasks=tasks[:a.limit]
    print("[%s route=%s shard %d/%d] tasks: %d" % (model,a.route,sh_n,sh_m,len(tasks)), flush=True)
    logf=io.open(ROOT/"reports"/"oxygen-pool.jsonl","a",encoding="utf-8")
    lock=threading.Lock(); n=[0]; fails=[]
    def work(t):
        src,out,vios,dmg,is_person=t
        rec={"source":str(src),"item":src.parent.name,"name":src.name,"vios":vios,"damage":dmg,
             "model":model,"gen":"v5-route","route":a.route}
        try:
            from PIL import Image
            with Image.open(src) as im: w,h=im.size
            prompt=build_person_prompt(vios,dmg) if is_person else build_strict_prompt(vios,dmg)
            data=call_model(key,model,src,prompt,pick_size(w,h))
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_bytes(data)
            rec.update(ok=True,out=str(out),bytes=len(data))
        except Exception as e:
            rec.update(ok=False,error=str(e)[:200])
        with lock:
            logf.write(json.dumps(rec,ensure_ascii=False)+"\n"); logf.flush()
            n[0]+=1
            if not rec.get("ok"): fails.append(rec)
            if n[0]%20==0: print("  [route=%s] done %d / %d fails %d" % (a.route,n[0],len(tasks),len(fails)), flush=True)
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs=[ex.submit(work,t) for t in tasks]
        for fu in as_completed(futs): fu.result()
    logf.close()
    print("route=%s finished: %d failed: %d" % (a.route,n[0],len(fails)), flush=True)

if __name__=="__main__":
    main()
