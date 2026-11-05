import hashlib,io,json,sys,tempfile,threading,time,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import photo_generation as pg

def jpeg():
 s=io.BytesIO();Image.new('RGB',(32,32),'white').save(s,'JPEG');return s.getvalue()

class PhotoGenerationTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  p=self.root/'real.png';Image.new('RGB',(32,32),'red').save(p);self.source=p
 def tasks(self,n=3):
  return [{'id':f'main-{i+1}','role':'main','prompt':'保留真实结构，不添参数','photos':[str(self.source)],'size':'1024x1024'} for i in range(n)]
 def test_three_models_exact_whitelist(self):
  self.assertEqual(('GPT-Image-2.5-Flare-joybuilder','GPT-Image-2.5-Sunburst-joybuilder','GPT-image-2-joybuilder'),pg.MODELS)
  with self.assertRaises(ValueError):pg.CompanyPhotoClient('placeholder').edit(model='Oxygen-Product-Pro',prompt='x',photos=[self.source],size='1024x1024')
 def test_three_models_are_in_flight_at_once(self):
  barrier=threading.Barrier(3);seen=[]
  class Client:
   def edit(_,**kw):seen.append(kw['model']);barrier.wait(timeout=3);return jpeg()
  out=pg.generate_tasks(self.tasks(),self.root/'out',client=Client())
  self.assertEqual(set(pg.MODELS),set(seen));self.assertEqual(3,out['successCount'])
 def test_explicit_rejection_changes_only_within_pool(self):
  seen=[]
  class Client:
   def edit(_,**kw):
    seen.append(kw['model'])
    if len(seen)==1:raise pg.GenerationRejected(402)
    return jpeg()
  r=pg.generate_tasks(self.tasks(1),self.root/'out',client=Client());self.assertEqual(1,r['successCount']);self.assertEqual(list(pg.MODELS[:2]),seen)
 def test_unknown_outcome_is_not_replayed_or_fallback(self):
  calls=[]
  class Client:
   def edit(_,**kw):calls.append(kw);raise pg.GenerationUnknown('timeout')
  t=self.tasks(1);r=pg.generate_tasks(t,self.root/'out',client=Client());self.assertEqual(1,r['unknownCount'])
  with self.assertRaises(pg.GenerationUnknown):pg.generate_tasks(t,self.root/'out',client=Client())
  self.assertEqual(1,len(calls))
 def test_cache_only_reuses_same_task_and_output_hash(self):
  calls=[]
  class Client:
   def edit(_,**kw):calls.append(kw);return jpeg()
  t=self.tasks(1);r=pg.generate_tasks(t,self.root/'out',client=Client());r=pg.generate_tasks(t,self.root/'out',client=Client());self.assertEqual(1,len(calls))
  t[0]['prompt']='changed'
  with self.assertRaises(ValueError):pg.generate_tasks(t,self.root/'out',client=Client())
 def test_single_redo_preserves_other_images(self):
  calls=[]
  class Client:
   def edit(_,**kw):calls.append(kw);return jpeg()
  tasks=self.tasks();pg.generate_tasks(tasks,self.root/'out',client=Client());pg.generate_tasks(tasks,self.root/'out',client=Client(),redo=['main-2']);self.assertEqual(4,len(calls))
 def test_reference_style_photo_is_not_an_allowed_product_input(self):
  tasks=self.tasks(1);tasks[0]['referenceImages']=['competitor.jpg']
  with self.assertRaises(ValueError):pg.generate_tasks(tasks,self.root/'out',client=object())

class DynamicPhotoSchedulingTests(unittest.TestCase):
 setUp=PhotoGenerationTests.setUp
 tasks=PhotoGenerationTests.tasks
 def test_slow_model_does_not_starve_faster_model_capacity(self):
  event=threading.Event();calls=[];lock=threading.Lock()
  timer=threading.Timer(.5,event.set);timer.start();self.addCleanup(timer.cancel)
  class Client:
   def edit(_,**kw):
    with lock:calls.append(kw['model'])
    if kw['model']==pg.MODELS[2]:event.wait(2)
    return jpeg()
  pg.generate_tasks(self.tasks(30),self.root/'out',client=Client())
  self.assertLess(calls.count(pg.MODELS[2]),10)

if __name__=='__main__':unittest.main()
