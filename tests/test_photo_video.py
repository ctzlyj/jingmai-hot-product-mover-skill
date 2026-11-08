import sys,tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import photo_video as pv
sys.path.insert(0,str(ROOT/'tests'))
import video_handoff as vh
from test_video_handoff import make_package,completed,ACCEPT
from photo_generation import atomic_json

class PhotoVideoTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  # a package whose video was handed off by 11190 (SHA256, runId, qc) and already listened to; the gate itself is tested in test_photo_video_handoff
  self.file=make_package(self.root);self.mp4=self.root/'视频'/'主图视频_AI带货_run1.mp4'
  for p in (patch.object(vh.subprocess,'run',return_value=completed(0)),patch.object(vh,'find_video_skill',return_value=Path('S'))):p.start();self.addCleanup(p.stop)
  vh.record_voice_review(self.file,ACCEPT)
  self.m=json.loads(self.file.read_text(encoding='utf-8'))
 def test_no_confirmation_has_no_remote_side_effect(self):
  with patch.object(pv,'verify_store') as verify:
   with self.assertRaises(ValueError):pv.bind_video(self.file,None,confirm='')
   verify.assert_not_called()
 def test_other_video_blocks_before_upload(self):
  with patch.object(pv,'verify_store'),patch.object(pv,'probe_video',return_value={'durationSeconds':10}),patch.object(pv,'read_product',return_value={'skuList':[{'skuId':11},{'skuId':12}]}),patch.object(pv.pipeline,'existing_video_materials',return_value={11:[{'videoMaterial':{'videoId':999}}]}),patch.object(pv.pipeline,'video_zone_id') as zone:
   with self.assertRaises(ValueError):pv.bind_video(self.file,None,confirm='BIND_MAIN_VIDEO')
   zone.assert_not_called()
 def test_manifest_ids_must_match_owned_product(self):
  with patch.object(pv,'verify_store'),patch.object(pv,'probe_video',return_value={'durationSeconds':10}),patch.object(pv,'read_product',return_value={'skuList':[{'skuId':99}]}):
   with self.assertRaises(ValueError):pv.bind_video(self.file,None,confirm='BIND_MAIN_VIDEO')
 def test_relation_status_compatibility_is_explicit(self):
  rows=pv.normalize_relations([{'status':4,'skuId':11},{'relationStatus':3,'status':4,'skuId':12}]);self.assertEqual([4,3],[r['relationStatus'] for r in rows])
 def test_unknown_video_submit_is_not_repeated(self):
  atomic_json(self.root/'.video-binding/state.json',{'stage':'SAVE_UNKNOWN','sourceSha256':pv.sha(self.mp4),'jdVideoId':5})
  with patch.object(pv,'verify_store'),patch.object(pv,'probe_video',return_value={'durationSeconds':10}),patch.object(pv,'read_product',return_value={'skuList':[{'skuId':11},{'skuId':12}]}),patch.object(pv.pipeline,'existing_video_materials',return_value={}),patch.object(pv.pipeline,'video_zone_id',return_value='v'),patch.object(pv.cli,'run_jdo_large') as post:
   with self.assertRaises(pv.ReconcileRequired):pv.bind_video(self.file,None,confirm='BIND_MAIN_VIDEO')
   post.assert_not_called()

# appended regression cases
class PhotoVideoHappyPathTests(unittest.TestCase):
 setUp=PhotoVideoTests.setUp
 def test_upload_once_bind_each_sku_and_resume_without_writes(self):
  video={'videoId':77,'jdVideoId':5,'status':4,'indexImage':'cover','videoName':'test','duration':10}
  writes=[];gets=[];count={'material':0}
  def get(skill,url,params=None):
   gets.append(url)
   if url.endswith('/upload-urls'):return {'data':{'httpsUploadUrl':'https://example.test/upload','jdVideoId':5}}
   if url.endswith('/sku-material-tags'):return {'data':[{'tagId':7,'tagName':'产品展示','sort':1}]}
   raise AssertionError(url)
  def post(**kw):
   writes.append(kw['url'])
   return {'data':{'videoId':77}} if kw['url'].endswith('/videos') else {'data':{}}
  def materials(ids):
   count['material']+=1
   if count['material']<=2:return {}
   return {i:[{'videoMaterial':{'videoId':77}}] for i in ids}
  with patch.object(pv,'verify_store'),patch.object(pv.cli,'recheck_verified_jdo_store'),patch.object(pv,'probe_video',return_value={'durationSeconds':10}),patch.object(pv,'read_product',return_value={'skuList':[{'skuId':11},{'skuId':12}]}),patch.object(pv.pipeline,'existing_video_materials',side_effect=materials),patch.object(pv.pipeline,'video_zone_id',return_value='v'),patch.object(pv.pipeline,'query_video',return_value=video),patch.object(pv.pipeline,'query_video_relations',return_value=[{'skuId':i,'videoId':77,'status':4} for i in (11,12)]),patch.object(pv.cli,'get',side_effect=get),patch.object(pv.cli,'run_jdo_large',side_effect=post),patch.object(pv.requests,'post',return_value=type('Response',(),{'status_code':200})()) as transfer:
   first=pv.bind_video(self.file,None,confirm='BIND_MAIN_VIDEO',interval=0)
   second=pv.bind_video(self.file,None,confirm='BIND_MAIN_VIDEO',interval=0)
   self.assertEqual('VIDEO_VERIFIED',first['stage']);self.assertEqual(1,transfer.call_count);self.assertEqual(3,len(writes));self.assertEqual(1,sum(u.endswith('/upload-urls') for u in gets))

if __name__=='__main__':unittest.main()
