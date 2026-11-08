"""Bind one local MP4 from a verified listing-package. Durable checkpoints prevent duplicate upload/save/bind."""
from __future__ import annotations
import argparse,json,math,os,shutil,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
import win_no_window  # noqa: F401
import requests
import jdo_listing_mvp as cli
import video_handoff as handoff
import jd_product_pipeline as pipeline
from photo_contract import ReconcileRequired,SCHEMA_VERSION,check_store,package_path
from photo_generation import atomic_json,load,sha
from photo_listing import verify_store

def probe_video(path):
 import cv2
 path=Path(path)
 if path.suffix.lower()!='.mp4' or not path.is_file():raise ValueError('A local MP4 is required')
 if path.stat().st_size>200*1024*1024:raise ValueError('MP4 exceeds 200 MB')
 cap=cv2.VideoCapture(str(path))
 try:
  fps=cap.get(cv2.CAP_PROP_FPS);frames=cap.get(cv2.CAP_PROP_FRAME_COUNT);width=int(cap.get(cv2.CAP_PROP_FRAME_WIDTH));height=int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
  if fps<=0 or frames<=0 or width<=0 or height<=0:raise ValueError('Cannot verify video metadata')
  seconds=math.ceil(frames/fps)
  if not 5<=seconds<=90:raise ValueError('Main video duration must be 5-90 seconds')
  return {'durationSeconds':seconds,'width':width,'height':height,'bytes':path.stat().st_size}
 finally:cap.release()

def read_product(product_id):return cli.get('jdo-product-status','/rest/sp-product/v0/products/{productId}',{'productId':product_id,'scene':'pop'}).get('data') or {}
def normalize_relations(rows):return [{**r,'relationStatus':r['relationStatus'] if 'relationStatus' in r else r.get('status',0)} for r in rows]

def bind_video(manifest_path,mp4=None,*,confirm,attempts=12,interval=15,resume_video_id=None):
 if confirm!='BIND_MAIN_VIDEO':raise ValueError('User confirmation --confirm BIND_MAIN_VIDEO is required')
 manifest_path=Path(manifest_path).resolve();root=manifest_path.parent;m=load(manifest_path)
 if not m or m.get('schemaVersion')!=SCHEMA_VERSION or m.get('status')!='VERIFIED':raise ValueError('Use a verified listing-package, not guessed SKU IDs')
 check_store(m['store']);pid=int(m['productId']);ids=sorted(set(map(int,m['skuIds'])))
 if not ids:raise ValueError('Missing SKU IDs')
 # Gate: the video is the one the manifest names, verified by 11190 verify-handoff, and (if required) listened to by the user.
 # Nothing below (probe, store check, platform reads/writes) runs unless this passes.
 verified=handoff.check_handoff(manifest_path,mp4=mp4);handoff.require_voice_review(verified)
 source=Path(verified['path']);source_sha=verified['sha256']
 def still_same(stage):
  # the bytes uploaded/bound must be the bytes that were verified and listened to
  if not source.is_file() or sha(source)!=source_sha:raise handoff.HandoffError('Video file changed ('+stage+'); stop, never upload or bind a different file than the verified one')
 still_same('before probing')
 info=probe_video(source);still_same('after probing');verify_store(m['store'])
 owned=sorted(int(s['skuId']) for s in read_product(pid).get('skuList') or [])
 if owned!=ids:raise ValueError('Manifest SKU IDs do not match the owned product')
 directory=root/'.video-binding';directory.mkdir(exist_ok=True);state_path=directory/'state.json';state=load(state_path,{}) or {}
 if state and state.get('sourceSha256')!=source_sha:raise ValueError('Video source changed; reconcile previous binding before replacing')
 known_id=int(state.get('videoId') or 0)
 materials=pipeline.existing_video_materials(ids)
 for sid in ids:
  others={int((r.get('videoMaterial') or {}).get('videoId') or 0) for r in materials.get(sid) or []}-{0,known_id}
  if others:raise ValueError(f'SKU {sid} already has another video; never overwrite')
 zone=pipeline.video_zone_id()
 if state.get('stage') in ('URL_UNKNOWN','TRANSFER_SUBMITTING','TRANSFER_UNKNOWN','SAVE_SUBMITTING','SAVE_UNKNOWN') and not known_id and not resume_video_id:
  raise ReconcileRequired('Video transfer/save result unknown; reconcile the original jdVideoId, never upload again')
 lock=root/'.video-binding.lock'
 try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd)
 except FileExistsError:raise ReconcileRequired('Another video-binding process owns this package')
 began=time.monotonic()
 def save(stage,**values):
  state.update(values);state.update(stage=stage,sourceSha256=source_sha,productId=pid,skuIds=ids);atomic_json(state_path,state)
 try:
  if resume_video_id:
   video=pipeline.query_video(zone,int(resume_video_id))
   if not video or int(video.get('jdVideoId') or 0)!=int(state.get('jdVideoId') or 0):raise ValueError('Resume video does not match the original transfer receipt')
   known_id=int(resume_video_id);save('VIDEO_SAVED',videoId=known_id)
  if not known_id:
   if not state.get('jdVideoId'):
    still_same('before upload address');save('URL_UNKNOWN')
    response=cli.get('jdo-product-material','/rest/sp-product/v0/media-zones/{zoneId}/upload-urls',{'zoneId':zone,**pipeline.video_upload_url_params(source,source.stat().st_size)})
    upload=response.get('data') or {};url=upload.get('httpsUploadUrl') or upload.get('uploadUrl');jd_id=int(upload.get('jdVideoId') or 0)
    if not url or not jd_id:raise ReconcileRequired('Upload address response incomplete')
    save('TRANSFER_SUBMITTING',jdVideoId=jd_id)
    try:
     cli.recheck_verified_jdo_store()
     with source.open('rb') as f:transfer=requests.post(url,files={'video_file':(source.name,f,'video/mp4')},timeout=300)
     if not 200<=transfer.status_code<300:raise ValueError('Transfer not confirmed')
    except Exception:
     save('TRANSFER_UNKNOWN');raise ReconcileRequired('Video transfer result unknown; do not retry transfer') from None
    try:still_same('after transfer')
    except handoff.HandoffError:save('TRANSFER_UNKNOWN',changedDuringTransfer=True);raise
    save('TRANSFERRED')
   if state['stage']!='TRANSFERRED':raise ReconcileRequired('Video upload not confirmed')
   still_same('before save');body=directory/'save-video-body.json';atomic_json(body,{'zoneVideoDTO':{'videoName':str(m['title'])[:18],'videoType':pipeline.VIDEO_TYPE,'videoDesc':'商品主图展示','videoSize':source.stat().st_size,'jdVideoId':state['jdVideoId']}})
   save('SAVE_SUBMITTING')
   try:response=cli.run_jdo_large(url='/rest/sp-product/v0/media-zones/{zoneId}/videos',method='POST',body_path=body,params={'zoneId':zone},skill_id='jdo-product-material',yes=True)
   except Exception:
    save('SAVE_UNKNOWN');raise ReconcileRequired('Video save result unknown; find original jdVideoId before retry') from None
   atomic_json(directory/'save-video-response.json',response);known_id=int((response.get('data') or {}).get('videoId') or 0)
   if not known_id:save('SAVE_UNKNOWN');raise ReconcileRequired('Missing saved video ID')
   save('VIDEO_SAVED',videoId=known_id)
  video=None
  for n in range(attempts):
   video=pipeline.query_video(zone,known_id)
   if video and pipeline.video_is_ready(int(video.get('status') or 0)):break
   if video and int(video.get('status') or 0) in (5,6):raise ValueError('Video transcode/review rejected')
   if n+1<attempts:time.sleep(interval)
  else:save('TRANSCODE_PENDING');raise ReconcileRequired('Video transcode pending; saved video is not uploaded again')
  category=m.get('category') or {'id':m.get('facts',{}).get('categoryId')};tag_category=int(category.get('secondCategoryId') or category['id'])
  tags=cli.get('jdo-product-material','/rest/sp-product/v0/sku-material-tags',{'materialType':pipeline.VIDEO_MATERIAL_TYPE,'secondCategoryId':tag_category}).get('data') or []
  tag=next((t for t in tags if t.get('tagName')=='产品展示'),None)
  if not tag:raise ValueError('Current category has no product-display video tag')
  still_same('before binding');bindings=state.setdefault('bindings',{});materials=pipeline.existing_video_materials(ids)
  for sid in ids:
   existing={int((r.get('videoMaterial') or {}).get('videoId') or 0) for r in materials.get(sid) or []}-{0}
   if existing=={known_id}:continue
   if existing:raise ValueError('A SKU acquired another video; stop without overwriting')
   if (bindings.get(str(sid)) or {}).get('status') in ('SUBMITTING','UNKNOWN','SUCCEEDED'):raise ReconcileRequired('Binding not visible yet; reconcile relations, never repeat the POST')
   body=directory/f'bind-{sid}.json';atomic_json(body,pipeline.build_video_binding_body(product_id=pid,sku_id=sid,video=video,tag=tag))
   bindings[str(sid)]={'status':'SUBMITTING'};save('BINDING')
   try:response=cli.run_jdo_large(url='/rest/sp-product/v0/sku-materials/batch',method='POST',body_path=body,skill_id='jdo-product-material',yes=True)
   except Exception:
    bindings[str(sid)]['status']='UNKNOWN';save('BINDING_UNKNOWN');raise ReconcileRequired('SKU binding result unknown; query relations before any action') from None
   atomic_json(directory/f'bind-{sid}-response.json',response);bindings[str(sid)]['status']='SUCCEEDED';save('BINDING')
  audit={}
  for n in range(attempts):
   rows=normalize_relations(pipeline.query_video_relations(zone,known_id));audit=pipeline.video_relation_audit(rows,ids,known_id)
   atomic_json(directory/'audit.json',audit)
   if audit['allApproved'] or audit['rejectedSkuIds']:break
   if n+1<attempts:time.sleep(interval)
  stage='VIDEO_VERIFIED' if audit.get('allApproved') else 'VIDEO_REJECTED' if audit.get('rejectedSkuIds') else 'VIDEO_REVIEW_PENDING';save(stage,audit=audit)
  local=str(source.relative_to(root));m=load(manifest_path);meta=m.setdefault('video',{});meta.update({'localPath':local,**info,'binding':{'videoId':known_id,'jdVideoId':state['jdVideoId'],'stage':stage,'audit':audit,'sourceSha256':source_sha}});m.setdefault('timings',{})['videoBindingSeconds']=round(time.monotonic()-began,2);atomic_json(manifest_path,m)
  return {'stage':stage,'videoId':known_id,'audit':audit,'seconds':round(time.monotonic()-began,2)}
 finally:lock.unlink(missing_ok=True)

def main(argv=None):
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--mp4',type=Path,help='只能等于 manifest.video.localPath；默认不需要')
 parser.add_argument('--confirm',required=True);parser.add_argument('--resume-video-id',type=int)
 parser.add_argument('--diagnose',action='store_true',help='只读：列出视频交接缺哪些字段，不访问平台、不写文件')
 parser.add_argument('--voice-confirmation',help='用户原话：已试听，确认绑定 / 接受口播风险，确认绑定')
 parser.add_argument('--record-only',action='store_true',help='只记录试听确认，不绑定')
 a=parser.parse_args(argv)
 try:
  if a.diagnose:print(json.dumps(handoff.diagnose(a.manifest),ensure_ascii=False,indent=2));return 0
  if a.voice_confirmation:
   print(json.dumps(handoff.record_voice_review(a.manifest,a.voice_confirmation),ensure_ascii=False,indent=2))
   if a.record_only:return 0
 except handoff.HandoffError as e:print(json.dumps({'ok':False,'type':'HandoffError','message':str(e)},ensure_ascii=False));return 3
 try:result=bind_video(a.manifest,a.mp4,confirm=a.confirm,resume_video_id=a.resume_video_id);print(json.dumps(result,ensure_ascii=False,indent=2));return 0 if result['audit'].get('allApproved') else 2
 except handoff.HandoffError as e:print(json.dumps({'ok':False,'type':'HandoffError','message':str(e)},ensure_ascii=False));return 3
 except (ValueError,ReconcileRequired,cli.JdoError) as e:print(json.dumps({'ok':False,'type':type(e).__name__,'message':str(e)},ensure_ascii=False));return 2
if __name__=='__main__':raise SystemExit(main())
