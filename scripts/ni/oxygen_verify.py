# -*- coding: utf-8 -*-
"""Verify generated outputs against their source compliant images via LTS4AI vision.
Works for any output dir (pro/imagen/sufy/oxygen). Resume-safe by output path."""
import argparse, base64, io, json, os, re, subprocess, sys, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import cv2, numpy as np
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import batch as ni_batch  # noqa: E402

ROOT = ni_batch.root_from_argv()
API = "https://api.lts4ai.com/v1/chat/completions"
HELPER = str(Path.home() / ".joycode" / "skills" / "sufy-image2" / "scripts" / "read_dpapi_credential.ps1")
DPAPI = Path(os.environ["LOCALAPPDATA"]) / "JoyCode" / "credentials" / "sufy-image2.dpapi"
SYS = "你是京东商品图验收员。仔细对比两张图，只输出JSON。"
PRIMARY_MODEL = "gemini-2.5-flash-lite"  # tier-1: full-pass first screen
REVIEW_MODEL = "gemini-2.5-flash"        # tier-2: review only failed/unclear verdicts
VERDICT_KEYS = ("productSame", "violationsCleared", "noNewArtifacts", "qualityOk")

def verdict_ok(v):
    return all(v.get(k) for k in VERDICT_KEYS)


VIO_TXT = {
 "brand": "品牌名/Logo/商标", "flag": "旗舰字样", "official": "官方/正品字样",
 "phone": "电话/二维码等联系方式", "patent": "专利字样", "factory": "厂家直销/源头工厂字样",
 "thirdparty": "第三方平台标识与活动信息", "watermark": "水印", "person_name": "画面中的人名",
 "price": "画面中的价格信息", "water_circle": "圆形水印角标", "taojinbi": "淘金币标识",
 "authority_claim": "权威机构背书与功效数据声明（如CMA质检、除醛率百分比、中科院研发等字样）",
}

def build_user_prompt(vios, dmg, person_mode="remove"):
    is_person = "person" in (vios or [])
    vios_txt = "、".join(VIO_TXT[v] for v in (vios or []) if v in VIO_TXT)
    if not vios_txt: vios_txt = "画面损伤（空白块/文字残缺/错位/接缝/马赛克）"
    if is_person:
        if person_mode == "remove":
            return ("第1张是原图（含人物），第2张是修复后的新图。本次任务的目标就是移除人物，人物已消失是预期结果。请验收：\n"
                    "1) productSame：除人物以外的商品主体（外形、颜色、数量、包装文字）、场景与文案是否与原图一致；\n"
                    "2) violationsCleared：原图人物是否已被完全移除（不得残留人物任何部位），人物区域是否用背景自然补全、无留白/空洞/马赛克；"
                    "画面中的其他违规项（"+vios_txt+"）是否已清除；\n"
                    "3) noNewArtifacts：新图是否没有空白块、空洞、文字残缺、错位、乱码、接缝、马赛克等瑕疵；\n"
                    "4) qualityOk：新图整体清晰度和排版是否合格（不差于原图）。\n"
                    '输出：{"productSame":true/false,"violationsCleared":true/false,"noNewArtifacts":true/false,'
                    '"qualityOk":true/false,"issues":["若有问题逐条列出，无则空数组"]}')
        return ("第1张是原图（含违规人物），第2张是修复后的新图。人物替换是本次任务的预期目标，"
                "不是缺陷。请验收：\n"
                "1) productSame：除人物以外的商品主体（外形、颜色、数量、包装文字）、场景与文案是否与原图一致；"
                "人物本身允许且必须与原图不同，不得据此判为不一致；\n"
                "2) violationsCleared：原图人物是否已被完全替换为新的AI人物（不得保留原图人物面部或身份特征，"
                "也不得简单擦除留白/空洞）；画面中的人名等其他违规项（"+vios_txt+"）是否已清除；\n"
                "3) noNewArtifacts：新图是否没有空白块、空洞、文字残缺、错位、乱码、接缝、马赛克等瑕疵；"
                "人物区域必须自然补全，出现空洞即不合格；\n"
                "4) qualityOk：新图整体清晰度和排版是否合格（不差于原图）。\n"
                '输出：{"productSame":true/false,"violationsCleared":true/false,"noNewArtifacts":true/false,'
                '"qualityOk":true/false,"issues":["若有问题逐条列出，无则空数组"]}')
    dmg_note = ""
    if dmg:
        dmg_note = "注意：原图存在文字残缺/画面损伤，新图把残缺文字修复为清晰正确的文字是预期行为，productSame 不要求保留原图的残缺文字。"
    return ("第1张是原图（存在问题），第2张是修复后的新图。请验收：\n"
            "1) productSame：商品主体（外形、颜色、数量、包装文字）是否与原图一致，不允许变形或改变内容；信息图（表格/报告/说明文字页）的文字内容必须与原图一致；\n"
            "2) violationsCleared：第1张中的以下违规内容是否已清除（"+vios_txt+"）；\n"
            "3) noNewArtifacts：新图是否没有空白块、空洞、文字残缺、错位、乱码、接缝、马赛克等瑕疵；\n"
            "4) qualityOk：新图整体清晰度和排版是否合格（不差于原图）。"+dmg_note+"\n"
            '输出：{"productSame":true/false,"violationsCleared":true/false,"noNewArtifacts":true/false,'
            '"qualityOk":true/false,"issues":["若有问题逐条列出，无则空数组"]}')
def load_key():
    p=subprocess.run(["powershell","-NoProfile","-ExecutionPolicy","Bypass","-File",HELPER,str(DPAPI)],
        capture_output=True,text=True,encoding="utf-8",timeout=30)
    k=(p.stdout or "").strip()
    if p.returncode==0 and k: return k
    raise SystemExit("NO_KEY")
def b64(p,maxside=1200):
    img=cv2.imdecode(np.fromfile(str(p),dtype=np.uint8),cv2.IMREAD_COLOR)
    if img is None: return None
    h,w=img.shape[:2]; s=min(1.0,maxside/float(max(h,w)))
    if s<1.0: img=cv2.resize(img,(max(1,int(w*s)),max(1,int(h*s))),interpolation=cv2.INTER_AREA)
    ok,buf=cv2.imencode(".jpg",img,[cv2.IMWRITE_JPEG_QUALITY,82])
    return base64.b64encode(buf.tobytes()).decode("ascii") if ok else None
def call(key,p1,p2,user_prompt,model=PRIMARY_MODEL):
    b1,b2=b64(p1),b64(p2)
    if not b1 or not b2: return {"parseFail":True,"raw":"read-fail"}
    body={"model":model,"temperature":0,"max_tokens":400,
          "messages":[{"role":"system","content":SYS},{"role":"user","content":[
            {"type":"text","text":user_prompt},
            {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+b1}},
            {"type":"image_url","image_url":{"url":"data:image/jpeg;base64,"+b2}}]}]}
    req=__import__("urllib.request",fromlist=["x"]).Request(API,data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type":"application/json","Authorization":"Bearer "+key})
    with __import__("urllib.request",fromlist=["x"]).urlopen(req,timeout=240) as r:
        d=json.loads(r.read().decode("utf-8"))
    txt=d["choices"][0]["message"]["content"]
    if isinstance(txt,list): txt="".join(c.get("text","") for c in txt if isinstance(c,dict))
    m=re.search(r"\{.*\}",txt,re.S)
    return json.loads(m.group(0)) if m else {"parseFail":True,"raw":str(txt)[:200]}
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--dir", default="image-edits-oxygen")
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--person-mode", default="remove", choices=("replace","remove"))
    ap.add_argument("--model", default=PRIMARY_MODEL)
    ap.add_argument("--review-model", default=REVIEW_MODEL)
    ap.add_argument("--no-review", action="store_true", help="skip tier-2 flash review of failed/unclear verdicts")
    a=ap.parse_args()
    key=load_key()
    plan=json.loads((ROOT/"reports"/"regen-plan.json").read_text(encoding="utf-8"))
    meta={}
    for p,v in plan.items():
        pp=Path(p)
        vios=[x[10:] for act,x in v["acts"] if act=="REGEN" and x.startswith("violation:")]
        dmg=any(act=="REGEN" and not x.startswith("violation:") for act,x in v["acts"])
        meta[(pp.parent.name,pp.name)]=(vios,dmg)
    pairs=[]
    for d in sorted((ROOT/a.dir).iterdir()):
        if not d.is_dir(): continue
        for f in sorted(d.glob("*.png")):
            if f.stem.startswith("edited"):  # sufy CLI raw naming, not canonical slots
                continue
            src=None
            for ext in (".jpg",".png",".jpeg",".webp"):
                alt=ROOT/"images"/"compliant"/d.name/(f.stem+ext)
                if alt.is_file(): src=alt; break
            if src is not None and src.is_file(): pairs.append((src,f,d.name))
    outp=ROOT/"reports"/a.out
    done=set()
    if outp.is_file():
        for line in outp.read_text(encoding="utf-8-sig").splitlines():
            line=line.strip()
            if line:
                try:
                    r=json.loads(line)
                    if r.get("ok"): done.add(r.get("output"))
                except Exception: pass
    todo=[t for t in pairs if str(t[1]) not in done]
    print("dir:",a.dir,"pairs:",len(pairs),"todo:",len(todo),flush=True)
    f=io.open(outp,"a",encoding="utf-8"); lock=threading.Lock(); n=[0]
    def work(t):
        src,out,item=t
        rec={"item":item,"name":src.name,"output":str(out)}
        vios,dmg=meta.get((item,src.name),([],False))
        rec.update(taskType="person" if "person" in vios else ("damage" if dmg and not vios else "standard"), vios=vios, damage=dmg)
        try:
            prompt=build_user_prompt(vios,dmg,a.person_mode)
            v=call(key,src,out,prompt,a.model)
            if verdict_ok(v) or a.no_review:
                rec.update(ok=verdict_ok(v),verdict=v,model=a.model,escalated=False)
            else:
                v2=call(key,src,out,prompt,a.review_model)
                rec.update(ok=verdict_ok(v2),verdict=v2,firstPass=v,model=a.review_model,escalated=True)
        except Exception as e:
            rec.update(ok=False,error=str(e)[:200])
        with lock:
            f.write(json.dumps(rec,ensure_ascii=False)+"\n"); f.flush(); n[0]+=1
            if n[0]%50==0: print("  done",n[0],"/",len(todo),flush=True)
    with ThreadPoolExecutor(max_workers=a.concurrency) as ex:
        futs=[ex.submit(work,t) for t in todo]
        for fu in as_completed(futs): fu.result()
    f.close(); print("finished",n[0],flush=True)
if __name__=="__main__":
    main()
