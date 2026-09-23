# -*- coding: utf-8 -*-
"""sufy-image2 fallback pool v2: precise (item, source-name) passed matching."""
import argparse, json, re, shutil, subprocess, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402

ROOT = ni_batch.root_from_argv()
SUFY_CLI = str(Path.home() / ".joycode" / "skills" / "sufy-image2" / "scripts" / "sufy_image2.py")
PY = sys.executable
OUTDIR = ROOT / "image-edits-sufy"

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
 "person": "所有人物形象（真人、手部、卡通人物、插画人物、3D人物、剪影、模特假人）",
 "price": "画面中的价格数字、价格标签与促销价格信息",
 "water_circle": "圆形水印角标",
 "taojinbi": "淘金币标识",
}

def build_prompt(vios, damage):
    if "person" in vios:
        # Verified person policy: removal + natural background fill (never replacement).
        kinds=[VIO[v] for v in vios if v in VIO and v not in ("person","person_name")]
        p=("这是一次人物移除编辑任务，不是重新创作。将原图中的所有人物（真人、手部、模特、卡通人物、插画人物、3D人物、剪影）"
           "整体移除，人物所在区域仅用紧邻的背景色/图案自然填补完整，不得留白、空洞或马赛克，不添加任何新人物或新元素。")
        if kinds: p+="同时清除图中的"+"、".join(kinds)+"。"
        if "person_name" in vios: p+="画面文字中出现的人名一并清除。"
        if damage: p+="修复画面损伤，把残缺文字修复为清晰正确的文字。"
        p+=("除上述改动外，画面其余部分必须与原图完全一致：相同的构图、相同的商品、相同的背景、相同的文案。"
            "逐字保留其余说明文字、数据、表格内容，不得改写、删减或新增。商品外形、颜色、数量、包装文字保持原样。")
        return p
    kinds=[VIO[v] for v in vios if v in VIO]
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

def parse_json(stdout):
    text=(stdout or "").strip()
    if not text: return None
    try: return json.loads(text)
    except Exception:
        m=re.search(r"\{.*\}",text,re.S)
        if m:
            try: return json.loads(m.group(0))
            except Exception: return None
    return None

def call_sufy(src, prompt, workdir, timeout=1500):
    cmd=[PY,"-X","utf8",SUFY_CLI,"edit","--model","SF-gpt-image-2",
         "--prompt",prompt,"--image",str(src),"--ratio","Adaptive",
         "--quality","2K","--output-dir",str(workdir)]
    started=time.time()
    p=subprocess.run(cmd,capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout)
    res=parse_json(p.stdout)
    outs=(res or {}).get("outputs") or []
    if res and res.get("ok") and outs:
        return {"ok":True,"output":outs[0]["path"],"elapsed":round(time.time()-started,1)}
    return {"ok":False,"error":("%s\n%s"%(p.stderr or "",p.stdout or ""))[-1200:]}

def passed_set(verify_files):
    ok=set()
    for vf in verify_files:
        p=ROOT/"reports"/vf
        if not p.is_file(): continue
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line=line.strip()
            if not line: continue
            try:
                d=json.loads(line)
                if d.get("ok"): ok.add((d.get("item"), d.get("name")))
            except Exception: pass
    return ok

def failed_verified_set(verify_files):
    """(item, name) pairs whose internal output exists but failed verification."""
    failed=set()
    for vf in verify_files:
        p=ROOT/"reports"/vf
        if not p.is_file(): continue
        for line in p.read_text(encoding="utf-8-sig").splitlines():
            line=line.strip()
            if not line: continue
            try:
                d=json.loads(line)
                # skip API-error rows: those need verify retry, not regeneration
                if d.get("ok") is False and not d.get("error") and d.get("output") and Path(d["output"]).is_file():
                    failed.add((d.get("item"), d.get("name")))
            except Exception: pass
    return failed

def pool_failed_set():
    """(item, name) pairs whose internal generation permanently failed (logged ok:false)."""
    failed=set()
    p=ROOT/"reports"/"oxygen-pool.jsonl"
    if not p.is_file(): return failed
    for line in p.read_text(encoding="utf-8-sig").splitlines():
        line=line.strip()
        if not line: continue
        try:
            d=json.loads(line)
            if d.get("ok") is False:
                failed.add((d.get("item"), d.get("name")))
        except Exception: pass
    return failed

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--plan", default="regen-plan.json")
    ap.add_argument("--verify", default="", help="comma list of verify jsonl filenames")
    ap.add_argument("--scope", default="all", help="comma list: all,failed-verified,pool-failed")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--concurrency", type=int, default=8)
    a=ap.parse_args()
    plan=json.loads((ROOT/"reports"/a.plan).read_text(encoding="utf-8"))
    dec=json.loads((ROOT/"reports"/"people-broad-decisions.json").read_text(encoding="utf-8"))
    notperson={(d["item"],d["name"]) for d in dec.get("reverted") or []}
    vfiles=[x.strip() for x in a.verify.split(",") if x.strip()]
    passed=passed_set(vfiles) if vfiles else set()
    scopes={x.strip() for x in a.scope.split(",") if x.strip()}
    redo=None
    if "all" not in scopes:
        redo=set()
        if "failed-verified" in scopes and vfiles:
            redo |= failed_verified_set(vfiles)
        if "pool-failed" in scopes:
            redo |= pool_failed_set()
    tasks=[]
    for p,v in plan.items():
        path=Path(p)
        item,name=path.parent.name,path.name
        vios=[_[10:] for a2,_ in v["acts"] if a2=="REGEN" and _.startswith("violation:")]
        dmg=any(a2=="REGEN" and not _.startswith("violation:") for a2,_ in v["acts"])
        if not dmg and set(vios)<= {"person","person_name"} and (item,name) in notperson:
            continue
        if (item,name) in passed:
            continue
        if redo is not None and (item,name) not in redo:
            continue
        out=OUTDIR/item/(name.rsplit(".",1)[0]+".png")
        if out.is_file() and out.stat().st_size>8000: continue
        tasks.append((path,out,vios,dmg))
    if a.limit: tasks=tasks[:a.limit]
    print("sufy fallback tasks:",len(tasks),flush=True)
    logf=(ROOT/"reports"/"sufy-fallback.jsonl").open("a",encoding="utf-8")
    lock=threading.Lock(); n=[0]; fails=[]
    def work(t):
        src,out,vios,dmg=t
        rec={"source":str(src),"item":src.parent.name,"name":src.name,"provider":"sufy-image2"}
        try:
            workdir=out.parent
            workdir.mkdir(parents=True,exist_ok=True)
            r=call_sufy(src,build_prompt(vios,dmg),workdir)
            if r["ok"]:
                shutil.copyfile(r["output"],out)
                rec.update(ok=True,out=str(out),bytes=out.stat().st_size)
            else:
                rec.update(ok=False,error=r["error"])
        except Exception as e:
            rec.update(ok=False,error=str(e)[:300])
        with lock:
            logf.write(json.dumps(rec,ensure_ascii=False)+"\n"); logf.flush()
            n[0]+=1
            if not rec["ok"]: fails.append(rec)
            if n[0]%10==0: print("  done",n[0],"/",len(tasks),"fails",len(fails),flush=True)
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs=[ex.submit(work,t) for t in tasks]
        for f in as_completed(futs): f.result()
    logf.close()
    print("sufy finished:",n[0],"failed:",len(fails),flush=True)

if __name__=="__main__":
    main()
