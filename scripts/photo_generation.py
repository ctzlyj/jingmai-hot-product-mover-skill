"""Company-only photo generation with a durable per-image receipt and three independent model pools."""
from __future__ import annotations
import base64,hashlib,io,json,os,re,tempfile,threading,time
from concurrent.futures import ThreadPoolExecutor,as_completed
from pathlib import Path
from PIL import Image
import requests

MODELS=('GPT-Image-2.5-Flare-joybuilder','GPT-Image-2.5-Sunburst-joybuilder','GPT-image-2-joybuilder')
BASE_URL='http://llm-gw.jd.local/v1'
class GenerationUnknown(RuntimeError):pass
class GenerationRejected(RuntimeError):
 def __init__(self,status,retry_after=0):
  self.status,self.retry_after=status,retry_after;super().__init__(f'model rejected request: HTTP {status}')

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def atomic_json(path,value):
 path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
 raw=json.dumps(value,ensure_ascii=False,indent=2)
 for n in range(10):
  temp=path.with_name(path.name+'.'+os.urandom(4).hex()+'.tmp')
  try:
   with temp.open('w',encoding='utf-8') as f:f.write(raw);f.flush();os.fsync(f.fileno())
   os.replace(temp,path);return
  except OSError:
   temp.unlink(missing_ok=True)
   if n==9:raise
   time.sleep(.15*(n+1))
def load(path,default=None):return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).is_file() else default

def image_bytes(raw):
 with Image.open(io.BytesIO(raw)) as im:
  im.load()
  if min(im.size)<1:raise ValueError('empty generated image')
  if im.format=='JPEG':return raw
  out=io.BytesIO();im.convert('RGB').save(out,'JPEG',quality=95);return out.getvalue()

class CompanyPhotoClient:
 def __init__(self,key,post=None,per_model=4,starts_per_second=1):
  self.capacity=per_model;self.key=key;self.post=post or requests.post;self.slots={m:threading.BoundedSemaphore(per_model) for m in MODELS}
  self.locks={m:threading.Lock() for m in MODELS};self.next={m:0. for m in MODELS};self.period=1/starts_per_second
 def edit(self,*,model,prompt,photos,size):
  if model not in MODELS:raise ValueError('Only the three company GPT models are allowed')
  refs=[]
  for p in photos:
   raw=Path(p).read_bytes()
   with Image.open(io.BytesIO(raw)) as im:mime=Image.MIME.get(im.format)
   if mime not in ('image/png','image/jpeg','image/webp'):raise ValueError('invalid photo encoding')
   refs.append('data:'+mime+';base64,'+base64.b64encode(raw).decode('ascii'))
  body={'model':model,'prompt':prompt,'image':refs,'size':size,'n':1,'output_format':'jpeg'}
  with self.slots[model]:
   with self.locks[model]:
    delay=self.next[model]-time.monotonic()
    if delay>0:time.sleep(delay)
    self.next[model]=time.monotonic()+self.period
   try:
    r=self.post(BASE_URL+'/images/edits',json=body,headers={'Authorization':'Bearer '+self.key,'Content-Type':'application/json'},timeout=600)
   except requests.RequestException as e:raise GenerationUnknown(type(e).__name__) from None
   if r.status_code in (400,402,404,413,422,429):
    try:retry=float(r.headers.get('Retry-After','0'))
    except (TypeError,ValueError):retry=0
    if retry>0:
     with self.locks[model]:self.next[model]=max(self.next[model],time.monotonic()+retry)
    raise GenerationRejected(r.status_code,retry)
   if r.status_code in (401,403):raise GenerationUnknown('AUTHORIZATION_REQUIRED')
   if r.status_code!=200:raise GenerationUnknown(f'HTTP_{r.status_code}')
   try:
    data=r.json().get('data') or []
    if len(data)!=1 or not data[0].get('b64_json'):raise ValueError('missing unique output')
    return image_bytes(base64.b64decode(data[0]['b64_json'],validate=True))
   except Exception as e:raise GenerationUnknown('INVALID_OUTPUT_'+type(e).__name__) from None

def key_from_environment():
 for name in ('JD_LLM_API_KEY','GPT_IMAGE2_API_KEY'):
  if os.environ.get(name):return os.environ[name]
 try:
  import winreg
  with winreg.OpenKey(winreg.HKEY_CURRENT_USER,'Environment') as h:
   for name in ('JD_LLM_API_KEY','GPT_IMAGE2_API_KEY'):
    try:
     value=winreg.QueryValueEx(h,name)[0]
     if value:return str(value)
    except OSError:pass
 except ImportError:pass
 raise ValueError('Configure your own company model Key; do not paste it into chat')

class ModelScheduler:
 def __init__(self,capacity=4):
  self.capacity=capacity;self.active={m:0 for m in MODELS};self.condition=threading.Condition()
 def acquire(self,preferred):
  with self.condition:
   while True:
    available=[m for m in preferred if self.active[m]<self.capacity]
    if available:
     chosen=min(available,key=lambda m:self.active[m]);self.active[chosen]+=1;return chosen
    self.condition.wait()
 def release(self,model):
  with self.condition:self.active[model]-=1;self.condition.notify_all()

def fingerprint(task):
 if 'referenceImages' in task:raise ValueError('Competitor images cannot be product generation references')
 if not re.fullmatch(r'[A-Za-z0-9_-]{1,50}',task.get('id','')):raise ValueError('Invalid task id')
 if not task.get('prompt') or not task.get('photos'):raise ValueError('Missing prompt or real photos')
 if task.get('size') not in ('1024x1024','1024x1536','1536x1024'):raise ValueError('Unsupported photo size')
 if any(not Path(p).is_file() for p in task['photos']):raise ValueError('Missing real photo')
 content={'prompt':task['prompt'],'photos':[sha(p) for p in task['photos']],'size':task['size'],'role':task.get('role')}
 return hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False).encode()).hexdigest()

def generate_tasks(tasks,out,*,client=None,redo=None):
 out=Path(out);out.mkdir(parents=True,exist_ok=True);redo=set(redo or [])
 ids=[t['id'] for t in tasks]
 if len(ids)!=len(set(ids)) or not redo.issubset(ids):raise ValueError('Invalid duplicate/redo task ids')
 lock_path=out/'.generation.lock'
 try:fd=os.open(lock_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd)
 except FileExistsError:raise GenerationUnknown('A generation worker already owns this output; inspect before recovery')
 try:
  ledger_path=out/'generation.json';ledger=load(ledger_path,{}) or {};receipt=load(out/'tasks.json',{}) or {}
  todo=[];lock=threading.Lock();began=time.monotonic()
  for index,task in enumerate(tasks):
   name=task['id'];fp=fingerprint(task);previous=ledger.get(name) or {};file=out/'generated'/f'{name}.jpg'
   if receipt.get(name) and receipt[name]!=fp and name not in redo:raise ValueError('Task changed: request an explicit single-image redo')
   if name in redo:
    if previous.get('status') in ('STARTED','UNKNOWN'):raise GenerationUnknown('Unknown old request must be reconciled before redo')
    if file.exists():
     archive=out/'history';archive.mkdir(exist_ok=True);os.replace(file,archive/f'{name}-{time.time_ns()}.jpg')
    previous={};ledger.pop(name,None)
   if previous.get('status')=='SUCCEEDED':
    if not file.is_file() or sha(file)!=previous['sha256']:raise ValueError('Cached generated file changed')
    continue
   if previous.get('status') in ('STARTED','UNKNOWN'):
    if file.is_file() and receipt.get(name)==fp:
     image_bytes(file.read_bytes());ledger[name]={**previous,'status':'SUCCEEDED','sha256':sha(file),'recovered':True};continue
    raise GenerationUnknown(f'{name}: previous generation outcome unknown; no automatic replay')
   receipt[name]=fp;todo.append((index,task))
  atomic_json(out/'tasks.json',receipt);atomic_json(ledger_path,ledger)
  if todo and client is None:client=CompanyPhotoClient(key_from_environment())
  scheduler=ModelScheduler(getattr(client,"capacity",4))
  def work(index,task):
   name=task['id'];started=time.monotonic();attempts=[]
   preferred=[MODELS[(index+offset)%3] for offset in range(3)]
   for offset in range(3):
    model=scheduler.acquire(preferred)
    preferred.remove(model)
    with lock:
     ledger[name]={'status':'STARTED','model':model,'fingerprint':receipt[name],'attempts':attempts};atomic_json(ledger_path,ledger)
    try:raw=client.edit(model=model,prompt=task['prompt'],photos=task['photos'],size=task['size'])
    except GenerationRejected as e:
     attempts.append({'model':model,'status':'REJECTED','httpStatus':e.status});continue
    except Exception as e:
     return name,{'status':'UNKNOWN','model':model,'fingerprint':receipt[name],'attempts':attempts,'error':type(e).__name__,'seconds':round(time.monotonic()-started,2)}
    finally:scheduler.release(model)
    raw=image_bytes(raw);file=out/'generated'/f'{name}.jpg';file.parent.mkdir(exist_ok=True)
    temp=file.with_suffix('.tmp');temp.write_bytes(raw);os.replace(temp,file)
    return name,{'status':'SUCCEEDED','model':model,'fingerprint':receipt[name],'attempts':attempts,'sha256':sha(file),'localPath':str(file.relative_to(out)),'seconds':round(time.monotonic()-started,2)}
   return name,{'status':'REJECTED','attempts':attempts,'fingerprint':receipt[name],'seconds':round(time.monotonic()-started,2)}
  with ThreadPoolExecutor(max_workers=12) as pool:
   futures=[pool.submit(work,i,t) for i,t in todo]
   for future in as_completed(futures):
    name,result=future.result()
    with lock:ledger[name]=result;atomic_json(ledger_path,ledger)
  selected=[ledger[n] for n in ids]
  report={'successCount':sum(r['status']=='SUCCEEDED' for r in selected),'unknownCount':sum(r['status']=='UNKNOWN' for r in selected),'failedCount':sum(r['status']=='REJECTED' for r in selected),'seconds':round(time.monotonic()-began,2),'modelsUsed':sorted({r.get('model') for r in selected if r.get('model')}),'ledger':ledger}
  submitted=[]
  for _,task in todo:
   rec=ledger[task['id']]
   if rec.get('model'):submitted.append(rec['model'])
   submitted.extend(a['model'] for a in rec.get('attempts') or [])
  report.update(submittedCount=len(todo),modelsSubmitted=sorted(set(submitted)))
  atomic_json(out/'generation-result.json',report);return report
 finally:lock_path.unlink(missing_ok=True)
