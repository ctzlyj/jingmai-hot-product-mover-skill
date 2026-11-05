"""Real-photo listing orchestration. No product write occurs without a current human review digest."""
from __future__ import annotations
import argparse,hashlib,html,json,os,re,shutil,subprocess,sys,time,uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
sys.path.insert(0,str(Path(__file__).resolve().parent))
import win_no_window  # noqa: F401
import jdo_listing_mvp as cli
import jd_product_pipeline as pipeline
from jd_product_mover import HttpClient,MoverError,parse_mobile_page,parse_detail_urls
from photo_contract import (SCHEMA_VERSION,ReconcileRequired,check_store,initial_manifest,join_detail_panels,media_entries,missing_information,package_path,review_digest,walk_components)
from photo_generation import atomic_json,generate_tasks,load,sha
from skill_paths import installed_skill_dir

IDENTITY_RULES='只使用用户实拍图中的真实商品，外形、比例、颜色、结构、配件与原图一致。禁止补画看不清或被遮挡的部件；不得复制参考链接的任何商品图，禁止别家品牌、店名、水印、二维码。仅使用下面已确认事实；不添加未经核实的材质、尺寸、承重、认证、功效或价格承诺。'
DEFAULT_STYLE='专业干净的京东电商风格，柔和光线，统一配色，主体清楚，中文排版准确。'

def ensure_shop(shop):
 linker=installed_skill_dir('jingmai-cli-linker')
 if not linker:raise ValueError('先安装并使用“京麦 CLI 店铺连接”授权目标店铺')
 p=subprocess.run([sys.executable,'-X','utf8',str(linker/'scripts/linker.py'),'ensure-shop','--shop',shop],capture_output=True,text=True,encoding='utf-8',timeout=900)
 try:data=json.loads(p.stdout)
 except ValueError:raise ValueError('京麦 CLI 店铺连接未返回可核验结果') from None
 if data.get('status')!='ready':raise ValueError('京麦 CLI 店铺连接未就绪：'+str(data.get('status'))+'；'+str(data.get('hint') or '请完成本人授权或选择准确店铺'))
 return check_store({k:data[k] for k in ('profile','venderId','shopName')})

def verify_store(store):
 check_store(store);actual=ensure_shop(str(store['venderId']))
 if any(str(actual[k])!=str(store[k]) for k in ('profile','venderId','shopName')):raise ValueError('京麦 CLI 店铺连接返回身份不匹配')
 cli.configure_jdo_profile(store['profile'])
 data=cli.get('jdo-seller-info','/rest/sp-seller/v0/venderinfo',{'scopeSet':'baseInfo'}).get('data') or {}
 if int(data.get('venderId') or 0)!=int(store['venderId']) or (data.get('shopBaseInfo') or {}).get('shopName')!=store['shopName']:raise ValueError('店铺三要素回查不匹配，禁止写入')
 cli.mark_jdo_store_verified(store['profile'],int(store['venderId']),store['shopName']);return actual

def reject_credentials(value):
 if isinstance(value,dict):
  for key,item in value.items():
   if str(key).lower().replace('_','') in {'apikey','accesstoken','token','cookie','authorization','password','appsecret','deviceid'}:
    raise ValueError('Credential/device fields must not enter a listing package')
   reject_credentials(item)
 elif isinstance(value,list):
  for item in value:reject_credentials(item)
 elif isinstance(value,str) and re.search(r'Bearer\s+\S{20,}|sk-[A-Za-z0-9]{20,}',value):
  raise ValueError('Credential-looking content must not enter a listing package')

def clean_reference_url(url):
 if not url:return None
 parsed=urlparse(url)
 if parsed.scheme!='https' or parsed.hostname!='item.jd.com' or parsed.username or parsed.password or not re.fullmatch(r'/\d+\.html',parsed.path):raise ValueError('A canonical public JD item URL is required')
 return 'https://item.jd.com'+parsed.path

def create_package(root,store,photos,title,reference_url=None,reference_answered=False):
 if not reference_answered:raise ValueError('先问一次有没有参考京东链接，并记录用户回答')
 root=Path(root)
 if (root/'manifest.json').exists():raise ValueError('Package exists; resume it without overwriting')
 root.mkdir(parents=True,exist_ok=True);check_store(store);copied=[]
 for i,source in enumerate(photos,1):
  source=Path(source)
  if not source.is_file():raise ValueError('Missing user photo')
  destination=root/'实拍图'/f'photo-{i:02d}{source.suffix.lower()}';destination.parent.mkdir(exist_ok=True);shutil.copy2(source,destination);copied.append(str(destination.relative_to(root)))
 if not copied:raise ValueError('请提供真实商品照片')
 m=initial_manifest(store,copied,title,clean_reference_url(reference_url));m['packageId']=uuid.uuid4().hex;m['facts']['title']=title
 atomic_json(root/'manifest.json',m);atomic_json(root/'state.json',{'status':'INFORMATION_PENDING'});return m

def read_manifest(root):
 root=Path(root);m=load(root/'manifest.json')
 if not m or m.get('schemaVersion')!=SCHEMA_VERSION:raise ValueError('A valid listing-package/manifest.json is required')
 check_store(m['store']);return m

def collect_reference(root,client=None):
 root=Path(root);m=read_manifest(root);url=clean_reference_url(m.get('reference',{}).get('url'))
 if not url:raise ValueError('The user selected no reference; do not ask again or invent one')
 sku=url.rsplit('/',1)[-1].split('.')[0];client=client or HttpClient(retries=1);began=time.monotonic()
 parsed=parse_mobile_page(client.get_text('https://item.m.jd.com/product/'+sku+'.html'))
 main=['https://img10.360buyimg.com/imgzone/'+p.lstrip('/') for p in parsed.get('item',{}).get('image') or []]
 info=parsed.get('product') or {};pid=info.get('popPid') or info.get('realSpuId')
 if not pid or not main:raise ValueError('Reference lacks verifiable image/detail identity; do not bypass login or guess')
 detail=parse_detail_urls(client.get_text('https://d.3.cn/desc/'+str(pid)))
 cache=root/'.reference-cache';entries=[]
 for role,urls in (('main',main),('detail',detail)):
  for i,source in enumerate(urls,1):
   destination=cache/role/f'{i:02d}.jpg';destination.parent.mkdir(parents=True,exist_ok=True)
   if not destination.is_file():destination.write_bytes(client.get_bytes(source,headers={'Referer':url,'User-Agent':'Mozilla/5.0'}))
   entries.append({'role':role,'path':str(destination.relative_to(root)),'sourceUrl':source,'sha256':sha(destination),'purpose':'style-only'})
 result={'purpose':'style-only','sourceUrl':url,'mainCount':len(main),'detailCount':len(detail),'entries':entries,'seconds':round(time.monotonic()-began,2)}
 atomic_json(cache/'assets.json',result);m['reference']['cacheManifest']=str((cache/'assets.json').relative_to(root));m['timings']['referenceCollectionSeconds']=result['seconds'];atomic_json(root/'manifest.json',m)
 return result

def build_generation_plan(m,root,reference,preview=False):
 root=Path(root);facts=m.get('facts') or {}
 if not preview:
  missing=missing_information(facts,reference)
  if missing:raise ValueError('集中补齐信息：'+', '.join(missing))
 sources=[str(package_path(root,p)) for p in m['sourcePhotos']]
 style=(m.get('reference') or {}).get('styleSummary') or DEFAULT_STYLE
 verified={'title':facts.get('title') or m['title'],'sellingPoints':facts.get('sellingPoints') or []}
 if not preview:verified.update({k:facts[k] for k in ('brandId','categoryId')});verified['specifications']=[{'spec':s['spec']} for s in facts['skus']]
 common=IDENTITY_RULES+'已确认事实：'+json.dumps(verified,ensure_ascii=False)+'风格学习摘要（仅文字）：'+style
 descriptions=[('main-01','main','纯白背景产品独立首图，主体完整，移除手和非售卖的使用配景，不加文字或标签。'),
 ('main-02','main','简洁生活场景图；真实商品主体占主要画面，只写已确认商品名称，不编参数。'),
 ('main-03','main','突出已确认收纳用途，统一高级电商版式，中文逐字准确。')]
 if not preview:descriptions += [('main-04','main','以实拍角度展示可见结构细节，不扩展看不清的背面或配件。'),('main-05','main','已确认卖点信息图；仅用事实里的短句，不添加承重、功效、材质数据。')]
 plan=[]
 for key,role,description in descriptions:plan.append({'id':key,'role':role,'prompt':common+description,'photos':sources,'size':'1024x1024','previewOnly':preview})
 if not preview:
  for i,sku in enumerate(facts['skus'],1):
   selected=sku.get('sourcePhotos') or m['sourcePhotos']
   if not set(selected).issubset(m['sourcePhotos']):raise ValueError('SKU source photos must belong to the user photo set')
   plan.append({'id':f'sku-{i:02d}','role':'sku','skuKey':sku['key'],'prompt':common+'生成白底SKU图，商品对应用户确认规格：'+sku['spec']+'。只画对应实拍商品，规格文字必须准确，不能用文字遮挡商品。','photos':[str(package_path(root,p)) for p in selected],'size':'1024x1024','previewOnly':False})
  for i,section in enumerate(('标题与整体展示','真实使用场景','可见结构与已确认卖点'),1):
   plan.append({'id':f'detail-{i:02d}','role':'detail','prompt':common+'生成一张竖版详情模块：'+section+'。上下边缘统一奶油色留白，设计完整，文字准确，不引用未确认参数。','photos':sources,'size':'1024x1536','previewOnly':False})
 return plan

def materialize_generated(root,plan,result):
 root=Path(root);m=read_manifest(root);records=result['ledger'];main=[];details=[];variants=[];panels=[]
 for task in plan:
  rec=records.get(task['id']) or {}
  if rec.get('status')!='SUCCEEDED':continue
  folder={'main':'主图','sku':'SKU图','detail':'详情图'}[task['role']];dest=root/folder/f'{task["id"]}.jpg';dest.parent.mkdir(exist_ok=True)
  shutil.copy2(root/'.generation'/rec['localPath'],dest)
  item={'id':task['id'],'localPath':str(dest.relative_to(root)),'sha256':sha(dest),'model':rec['model'],'onlineUrl':'','previewOnly':task.get('previewOnly',False)}
  if task['role']=='main':item['role']='white' if task['id']=='main-01' else 'main';main.append(item)
  elif task['role']=='detail':panels.append(dest)
  else:variants.append({'key':task['skuKey'],'skuId':None,'skuImage':item})
 if panels:
  long=join_detail_panels(panels,root/'详情图/详情页长图.jpg');details=[{'id':'detail-long','localPath':str(long.relative_to(root)),'sha256':sha(long),'onlineUrl':'','sourcePanels':[str(p.relative_to(root)) for p in panels]}]
 m['mainImages']=main;m['detailImages']=details;m['variants']=variants;m['timings']['generationSeconds']=result['seconds'];m['generationModels']=result['modelsUsed']
 m.pop('review',None);m.pop('imageReview',None);m['status']='MATERIAL_PREVIEW' if any(t.get('previewOnly') for t in plan) else ('MATERIAL_REVIEW_PENDING' if result['successCount']==len(plan) else 'MATERIAL_INCOMPLETE')
 atomic_json(root/'manifest.json',m);return m

def build_listing(m,root,reference):
 root=Path(root);f=m.get('facts') or {};missing=missing_information(f,reference)
 if missing:raise ValueError('Missing verified facts: '+','.join(missing))
 if not m.get('mainImages') or not m.get('detailImages') or any(i.get('previewOnly') for i in m['mainImages']):raise ValueError('Full generated main/SKU/detail materials are required')
 fields=sorted([c for c in walk_components(reference.get('schema') or {}) if c['componentCode'].startswith('sale-prop-')],key=lambda c:int((c.get('extra') or {}).get('dim') or 1))
 dimensions=f.get('saleDimensions') or [fields[0].get('componentTitle','规格') if fields else '规格']
 if len(dimensions)>2 or len(fields)<len(dimensions):raise ValueError('Target live template cannot represent the provided sale dimensions; do not truncate SKUs')
 definitions={'style':{'componentCode':fields[0]['componentCode'],'attrName':dimensions[0]}}
 if len(dimensions)==2:definitions['size']={'componentCode':fields[1]['componentCode'],'attrName':dimensions[1]}
 key=m.get('packageId') or hashlib.sha256(json.dumps(m['store'],sort_keys=True).encode()).hexdigest()[:16]
 product={'productKey':'PN-'+key[:16],'title':f['title'],'brandId':f['brandId'],'itemNumber':'PN-'+key[:24],
          'sevenDayReturnCode':f['sevenDayReturnCode'],'freightTemplateId':f['freightTemplateId'],
          'includeSizeSaleAttribute':len(dimensions)==2,'mainImageMode':'multi_set','saleAttributeComponents':definitions}
 for field in ('packageLengthMm','packageWidthMm','packageHeightMm','weightKg'):product[field]=f[field]
 skus=[];styles=[];sizes=[];images=[];sku_images={v['key']:v['skuImage'] for v in m['variants']}
 for i,item in enumerate(m['mainImages'],1):images.append({'imageKey':item.get('id') or f'M{i}','role':'spu_main','localPath':item['localPath'],'order':i,'mediaCategoryId':reference['media']['defaultCategoryId']})
 for i,s in enumerate(f['skus'],1):
  values=s.get('attributes') or {};style=values.get(dimensions[0]) or s['spec'];size=values.get(dimensions[1]) if len(dimensions)==2 else s['spec']
  if not size:raise ValueError('Missing explicit SKU dimension value')
  if style not in styles:styles.append(style)
  if size not in sizes:sizes.append(size)
  mapped=lambda field,value:hashlib.sha256((key+'|'+field+'|'+value).encode()).hexdigest()[:32]
  skus.append({'skuKey':s['key'],'jdPrice':str(s['jdPrice']),'stock':int(s['stock']),'enabled':True,'vendorSku':s.get('vendorSku') or f'PN-{key[:12]}-{i:03d}',
               'styleAlias':style,'sizeAlias':size,'styleMappingId':mapped('style',style),'sizeMappingId':mapped('size',size),
               'styleOrder':styles.index(style)+1,'sizeOrder':sizes.index(size)+1,'skuAttributes':deepcopy(s['skuAttributes'])})
  if s['key'] not in sku_images:raise ValueError('Missing generated SKU image')
  item=sku_images[s['key']];local=item['localPath'] if isinstance(item,dict) else item
  images.insert(len(m['mainImages'])+i-1,{'imageKey':f'SKU-{i:02d}','skuKey':s['key'],'role':'sku_main','localPath':local,'order':1,'mediaCategoryId':reference['media']['defaultCategoryId']})
 for i,item in enumerate(m['detailImages'],1):images.append({'imageKey':item.get('id') or f'D{i}','role':'detail','localPath':item['localPath'],'order':i,'mediaCategoryId':reference['media']['defaultCategoryId']})
 listing={'category':{'id':int(f['categoryId'])},'product':product,'categoryAttributes':deepcopy(f['categoryAttributes']),'skus':skus,'images':images}
 errors,warnings=cli.validate_manifest(root/'listing.json',listing,reference)
 if errors:raise ValueError('Local preflight failed: '+'; '.join(errors))
 return listing

def review_package(root):
 root=Path(root);m=read_manifest(root);digest=review_digest(m,root);m['review']={'digest':digest,'at':time.strftime('%Y-%m-%dT%H:%M:%S'),'confirmedByUser':False};atomic_json(root/'manifest.json',m)
 facts=m.get('facts') or {};lines=['# 发布前确认清单','',f"店铺：{m['store']['shopName']}（{m['store']['profile']} / {m['store']['venderId']}）",f"类目：{facts.get('categoryId','待确认')}",f"标题：{facts.get('title') or m['title']}",f"品牌ID：{facts.get('brandId','待确认')}",f"包装/重量：{facts.get('packageLengthMm')} × {facts.get('packageWidthMm')} × {facts.get('packageHeightMm')} mm / {facts.get('weightKg')} kg",f"运费模板：{facts.get('freightTemplateId','待确认')}",f"无理由退货代码：{facts.get('sevenDayReturnCode','待确认')}",'','|SKU|规格|京东价|库存|','|---|---|---|---|']
 for s in facts.get('skus') or []:lines.append(f"|{s['key']}|{s['spec']}|{s.get('jdPrice')}|{s.get('stock')}|")
 lines += ['','## 图片预览']
 for item in media_entries(m):lines.append(f"![图片]({item['localPath']})")
 lines += ['',f'确认摘要：`{digest}`','未收到用户明确确认，不得调用 prepare 或 publish；图片/价格等有任何变化需重新预览确认。']
 (root/'确认清单.md').write_text('\n'.join(lines),encoding='utf-8')
 page='<html><meta charset="utf-8"><title>实拍上新预览</title><style>body{font:16px sans-serif;padding:24px}img{max-width:450px;margin:8px;vertical-align:top}pre{white-space:pre-wrap}</style><h1>实拍上新预览（尚未发布）</h1><pre>'+html.escape('\n'.join(lines[:15]))+'</pre>'
 for item in media_entries(m):page+='<figure><img src="'+html.escape(item['localPath'],quote=True)+'"><figcaption>'+html.escape(item['localPath'])+'</figcaption></figure>'
 (root/'preview.html').write_text(page+'</html>',encoding='utf-8');return {'digest':digest,'checklist':str(root/'确认清单.md'),'preview':str(root/'preview.html')}

def check_review(root,digest):
 m=read_manifest(root)
 if not digest or (m.get('review') or {}).get('digest')!=digest or review_digest(m,root)!=digest:raise ValueError('Review changed or missing; show current preview and ask user confirmation')
 return m

def accept_images(root,digest,*,confirm):
 if confirm!='PHOTOS_REVIEWED':raise ValueError('Only after user preview acceptance: --confirm PHOTOS_REVIEWED')
 root=Path(root);m=check_review(root,digest)
 if m.get('qualityProblems'):raise ValueError('Known image identity/text issues must be fixed and reviewed first')
 m['imageReview']={'ok':True,'digest':digest,'by':'USER_CONFIRMED','at':time.strftime('%Y-%m-%dT%H:%M:%S')}
 atomic_json(root/'manifest.json',m);return m['imageReview']

@contextmanager
def package_lock(root,name):
 lock=Path(root)/name
 try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd)
 except FileExistsError:raise ReconcileRequired('Another operation owns this package; inspect before recovery')
 try:yield
 finally:lock.unlink(missing_ok=True)

def prepare_package(root,reference,*,confirm,digest):
 if confirm!='PREPARE_PHOTO_LISTING':raise ValueError('Explicit reviewed-image preparation confirmation is required')
 with package_lock(root,'.prepare.lock'):
  return _prepare_package(root,reference,confirm=confirm,digest=digest)

def _prepare_package(root,reference,*,confirm,digest):
 if confirm!='PREPARE_PHOTO_LISTING':raise ValueError('Image upload and platform validation require --confirm PREPARE_PHOTO_LISTING after preview')
 root=Path(root);m=check_review(root,digest)
 if not (m.get('imageReview') or {}).get('ok') or m['imageReview'].get('digest')!=digest or m.get('qualityProblems'):raise ValueError('User must accept current image identity/text before platform preparation')
 if m.get('productId') or load(root/'state.json',{}).get('status') in ('SUBMITTING','UNKNOWN','SUCCEEDED'):raise ReconcileRequired('Reconcile the existing submission first')
 listing=build_listing(m,root,reference);verify_store(m['store']);began=time.monotonic();runtime=root/'.publish';runtime.mkdir(exist_ok=True)
 ledger_path=runtime/'uploads.json';uploads=load(ledger_path,{}) or {};zone=reference['media']['zoneId'];cat=reference['media']['defaultCategoryId']
 for image in listing['images']:
  file=package_path(root,image['localPath']);hash_=sha(file);record=uploads.get(hash_) or {}
  if not record.get('imgUrl'):
   if record.get('status') in ('SUBMITTING','UNKNOWN'):raise ReconcileRequired('An image upload outcome is unknown; reconcile in the image zone first')
   uploads[hash_]={'status':'SUBMITTING','sha256':hash_,'localPath':image['localPath']};atomic_json(ledger_path,uploads)
   snapshot=runtime/'upload-snapshots'/f'{hash_}.jpg';snapshot.parent.mkdir(exist_ok=True);shutil.copy2(file,snapshot)
   if sha(snapshot)!=hash_ or sha(file)!=hash_:raise ValueError('Image changed during preparation; review it again')
   try:result=cli.upload(zone,cat,snapshot)
   except Exception:
    uploads[hash_]['status']='UNKNOWN';atomic_json(ledger_path,uploads);raise ReconcileRequired('Image upload outcome unknown; never replay automatically') from None
   url=result.get('imgUrl') or result.get('imageUrl')
   if not url:raise ReconcileRequired('Image upload has no returned URL')
   record={'status':'SUCCEEDED','imgUrl':url,'sha256':hash_};uploads[hash_]=record;atomic_json(ledger_path,uploads)
  image['upload']={'imgUrl':record['imgUrl'],'status':'uploaded'}
 check_review(root,digest)
 atomic_json(root/'listing.json',listing);paths=cli.write_publish_payloads(root/'listing.json',runtime)
 response=cli.run_jdo_large(url='/rest/sp-product/v0/product-schemas/validations',method='POST',body_path=Path(paths['paths']['validation']),skill_id='jdo-product-publish',yes=True)
 atomic_json(runtime/'validation-response.json',response)
 strong=[v for v in pipeline.validation_violations(response) if int(v.get('detectResult') or 0)==1]
 if strong:raise ValueError('Platform validation rejected the listing; review violations in validation-response.json')
 for item in media_entries(m):
  raw=uploads[sha(package_path(root,item['localPath']))]['imgUrl'];item['imgUrl']=raw;item['onlineUrl']=cli.detail_image_url(raw)
 m['title']=m['facts']['title'];m['category']={'id':int(m['facts']['categoryId'])};m['sellingPoints']=m['facts'].get('sellingPoints') or []
 contract={'reviewDigest':digest,'bodySha256':sha(runtime/'publish-body.json'),'listingSha256':sha(root/'listing.json'),'itemNumber':listing['product']['itemNumber']}
 atomic_json(runtime/'prepared.json',contract);m['status']='PREPARED_AWAITING_CONFIRMATION';m['timings']['prepareSeconds']=round(time.monotonic()-began,2)
 by_key={s['skuKey']:s for s in listing['skus']}
 for v in m['variants']:
  s=by_key[v['key']];v.update({'spec':s['styleAlias'],'vendorSku':s['vendorSku'],'jdPrice':s['jdPrice'],'stock':s['stock']})
 atomic_json(root/'manifest.json',m);return {'status':m['status'],'reviewDigest':digest,'prepared':contract,'published':False}

def publish_package(root,*,confirm,digest):
 if confirm!='PUBLISH_TO_JD':raise ValueError('Explicit user confirmation PUBLISH_TO_JD is required')
 root=Path(root);m=check_review(root,digest);runtime=root/'.publish';prepared=load(runtime/'prepared.json')
 if not prepared or prepared.get('reviewDigest')!=digest or sha(runtime/'publish-body.json')!=prepared['bodySha256'] or sha(root/'listing.json')!=prepared['listingSha256']:raise ValueError('Prepared payload no longer matches reviewed materials')
 state_path=root/'state.json';state=load(state_path,{}) or {}
 if m.get('productId') or state.get('status') in ('SUBMITTING','UNKNOWN','SUCCEEDED'):return reconcile_package(root)
 verify_store(m['store']);existing=pipeline.find_existing_product(prepared['itemNumber'])
 if existing:
  atomic_json(state_path,{'status':'SUBMITTING','itemNumber':prepared['itemNumber'],'productId':int(existing['productId'])});return reconcile_package(root)
 lock_path=root/'.publish.lock'
 try:fd=os.open(lock_path,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.close(fd)
 except FileExistsError:raise ReconcileRequired('Another publish process owns this package')
 began=time.monotonic()
 try:
  latest=load(state_path,{}) or {}
  if latest.get('status') in ('SUBMITTING','UNKNOWN','SUCCEEDED') or read_manifest(root).get('productId'):return reconcile_package(root)
  check_review(root,digest)
  atomic_json(state_path,{'status':'SUBMITTING','itemNumber':prepared['itemNumber'],'bodySha256':prepared['bodySha256']})
  try:response=cli.run_jdo_large(url='/rest/sp-product/v0/product-applies',method='POST',body_path=runtime/'publish-body.json',skill_id='jdo-product-publish',yes=True)
  except Exception:
   atomic_json(state_path,{'status':'UNKNOWN','itemNumber':prepared['itemNumber'],'bodySha256':prepared['bodySha256']});raise ReconcileRequired('Product submission outcome unknown; query by technical item number') from None
  atomic_json(runtime/'publish-response.json',response);data=response.get('data') or {}
  if not data.get('productId'):raise ReconcileRequired('No product ID returned; reconcile by item number before any retry')
  m['productId']=int(data['productId']);m['applyId']=str(data.get('applyId') or data['productId']);m['status']='PUBLISHED_UNVERIFIED';m['timings']['publishSeconds']=round(time.monotonic()-began,2)
  atomic_json(root/'manifest.json',m);atomic_json(state_path,{'status':'SUCCEEDED','itemNumber':prepared['itemNumber'],'productId':m['productId'],'bodySha256':prepared['bodySha256']});return verify_package(root)
 finally:lock_path.unlink(missing_ok=True)

def reconcile_package(root):
 root=Path(root);m=read_manifest(root);verify_store(m['store']);state=load(root/'state.json',{}) or {};prepared=load(root/'.publish/prepared.json',{}) or {}
 pid=m.get('productId') or state.get('productId')
 if not pid:
  existing=pipeline.find_existing_product(state.get('itemNumber') or prepared.get('itemNumber') or '')
  if not existing:raise ReconcileRequired('No matching remote product yet; still unknown, do not resubmit')
  pid=int(existing['productId'])
 m['productId']=int(pid);m['status']='PUBLISHED_UNVERIFIED';atomic_json(root/'manifest.json',m);return verify_package(root)

def verify_package(root,attempts=18,interval=10):
 root=Path(root);m=read_manifest(root)
 if not m.get('productId'):raise ValueError('No published product ID; use reconcile')
 verify_store(m['store']);listing=load(root/'listing.json');pid=int(m['productId']);began=time.monotonic();detail={};apply={}
 for n in range(attempts):
  try:
   apply=cli.get('jdo-product-update','/rest/sp-product/v0/product-apply/{applyId}',{'applyId':str(pid),'scene':'pop'}).get('data') or {}
   if int((apply.get('applyInfo') or {}).get('draftComputeStatus') or 0)==3:
    m['status']='PRODUCT_APPLY_REJECTED';atomic_json(root/'manifest.json',m);raise ValueError('Platform rejected product application')
   detail=cli.get('jdo-product-status','/rest/sp-product/v0/products/{productId}',{'productId':pid,'scene':'pop'}).get('data') or {}
   if detail.get('skuList') and int((apply.get('applyInfo') or {}).get('draftComputeStatus') or 0)!=1:break
  except cli.JdoError as e:
   if '未查询到商品信息' not in str(e):raise
  if n+1<attempts:time.sleep(interval)
 else:raise ReconcileRequired('Product propagation pending; only retry readback')
 atomic_json(root/'.publish/readback-apply.json',apply);atomic_json(root/'.publish/readback-product.json',detail)
 expected={s['vendorSku']:s for s in listing['skus']};online={str(s.get('outerId')):s for s in detail['skuList']};problems=[]
 if set(online)!=set(expected) or len(detail['skuList'])!=len(expected):problems.append('SKU merchant-code set mismatch')
 from image_patch_update import group_urls
 groups=group_urls(apply)
 if (detail.get('productInfo') or {}).get('productName')!=listing['product']['title']:problems.append('title mismatch')
 expected_main=[i['upload']['imgUrl'] for i in listing['images'] if i['role']=='spu_main']
 if groups.get('0000000000')!=expected_main:problems.append('SPU main image order mismatch')
 stocks={str(s.get('skuId')):s for s in cli.query_sku_stocks([int(s['skuId']) for s in detail['skuList']])}
 for key,sku in expected.items():
  remote=online.get(key)
  if not remote:continue
  value=(remote.get('priceInfo') or {}).get('jdPrice')
  if value is None or float(value)!=float(sku['jdPrice']):problems.append('price '+key)
  if int(remote.get('skuEnableStatus') or 0)!=1:problems.append('disabled SKU '+key)
  stock=stocks.get(str(remote['skuId'])) or {}
  actual_stock=stock.get('stockNum')
  if actual_stock is None or int(actual_stock)!=int(sku['stock']):problems.append('stock '+key)
  sale=remote.get('saleAttrs') or [];actual_aliases=[s.get('attrValueAlias') for s in sale]
  wanted=[sku['styleAlias']]+([sku['sizeAlias']] if listing['product']['includeSizeSaleAttribute'] else [])
  if actual_aliases!=wanted:problems.append('sale specifications '+key)
  sku_urls=[i['upload']['imgUrl'] for i in listing['images'] if i['role']=='sku_main' and i['skuKey']==sku['skuKey']]+expected_main[1:]
  if not sale or groups.get(str(sale[0].get('attrValues'))) != sku_urls:problems.append('SKU gallery '+key)
  for v in m['variants']:
   if v.get('vendorSku')==key:v['skuId']=int(remote['skuId'])
 details=[cli.detail_image_url(i['upload']['imgUrl']) for i in listing['images'] if i['role']=='detail'];desc=((apply.get('productInfo') or {}).get('productDetailDesc') or {}).get('desc','')
 if any(url not in desc for url in details):problems.append('detail image URL mismatch')
 m['skuIds']=[int(s['skuId']) for s in detail['skuList']];m['status']='VERIFIED' if not problems else 'VERIFICATION_FAILED';m['timings']['verifySeconds']=round(time.monotonic()-began,2);m['verification']={'problems':problems,'skuCount':len(online),'at':time.strftime('%Y-%m-%dT%H:%M:%S')}
 atomic_json(root/'manifest.json',m);atomic_json(root/'.publish/verification.json',m['verification']);return {'status':m['status'],'productId':pid,'skuIds':m['skuIds'],'problems':problems}

def main(argv=None):
 for stream in (sys.stdout,sys.stderr):
  if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='replace')
 parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
 init=sub.add_parser('init');init.add_argument('--package',required=True,type=Path);init.add_argument('--shop',required=True);init.add_argument('--photos',nargs='+',required=True,type=Path);init.add_argument('--title',required=True)
 choice=init.add_mutually_exclusive_group(required=True);choice.add_argument('--reference');choice.add_argument('--no-reference',action='store_true')
 for name in ('refresh','reference','set-facts','questions','make-plan','generate','review','accept-images','prepare','publish','verify','reconcile'):
  p=sub.add_parser(name);p.add_argument('--package',required=True,type=Path)
  if name=='refresh':p.add_argument('--category-id',type=int,required=True);p.add_argument('--category-name',default='');p.add_argument('--brand-category-id',type=int)
  if name=='set-facts':p.add_argument('--facts',required=True,type=Path);p.add_argument('--style',type=Path)
  if name=='make-plan':p.add_argument('--preview',action='store_true')
  if name=='generate':p.add_argument('--redo',nargs='*',default=[])
  if name in ('prepare','publish','accept-images'):p.add_argument('--confirm',required=True);p.add_argument('--review-digest',required=True)
 args=parser.parse_args(argv);root=args.package.resolve();command=args.command;command_began=time.monotonic()
 try:
  if command=='init':
   shop=args.shop
   if shop.lower().endswith('.lnk'):
    link=Path(shop)
    if not link.is_file():raise ValueError('The exact shop shortcut is missing')
    shop=link.stem.removeprefix('京麦-')
   result=create_package(root,ensure_shop(shop),args.photos,args.title,args.reference,True)
  elif command=='refresh':
   m=read_manifest(root);verify_store(m['store']);r=cli.refresh_reference(root/'reference.json',args.category_id,args.category_name,args.brand_category_id);r['store']=m['store'];atomic_json(root/'reference.json',r)
   result={'category':r['category'],'brandlessAllowed':any(str(b.get('brandId'))=='-1' for b in r['brands'])}
  elif command=='reference':result=collect_reference(root)
  elif command=='set-facts':
   m=read_manifest(root);f=load(args.facts);reject_credentials(f)
   if not isinstance(f,dict):raise ValueError('Facts must be a JSON object')
   m['facts']=f;m['title']=f.get('title') or m['title'];m['sellingPoints']=f.get('sellingPoints') or [];m.pop('review',None)
   if args.style:
    style=load(args.style);reject_credentials(style);m['reference']['styleSummary']=str(style.get('styleSummary') or DEFAULT_STYLE)
   atomic_json(root/'manifest.json',m);result={'missing':missing_information(f,load(root/'reference.json',{}))}
  elif command=='questions':
   m=read_manifest(root);reference=load(root/'reference.json',{});missing=missing_information(m.get('facts') or {},reference)
   question={'askTogetherOnce':True,'missingFields':missing,'rule':'参数必须由用户提供或明确确认；看不清的结构要求补拍，不按参考商品猜参数'}
   atomic_json(root/'missing-information.json',question);result=question
  elif command=='make-plan':
   m=read_manifest(root);tasks=build_generation_plan(m,root,load(root/'reference.json',{}),args.preview);atomic_json(root/'plan.json',tasks);result={'tasks':len(tasks),'previewOnly':args.preview}
  elif command=='generate':
   plan=load(root/'plan.json');result=generate_tasks(plan,root/'.generation',redo=args.redo);materialize_generated(root,plan,result);review_package(root);result={k:v for k,v in result.items() if k!='ledger'}
  elif command=='review':result=review_package(root)
  elif command=='accept-images':result=accept_images(root,args.review_digest,confirm=args.confirm)
  elif command=='prepare':result=prepare_package(root,load(root/'reference.json'),confirm=args.confirm,digest=args.review_digest)
  elif command=='publish':result=publish_package(root,confirm=args.confirm,digest=args.review_digest)
  elif command=='verify':result=verify_package(root)
  else:result=reconcile_package(root)
  if (root/'manifest.json').is_file():
   current=read_manifest(root);current.setdefault('timings',{})[command+'CommandSeconds']=round(time.monotonic()-command_began,2);atomic_json(root/'manifest.json',current)
  print(json.dumps(result,ensure_ascii=False,indent=2));return 0
 except (ValueError,ReconcileRequired,cli.JdoError,MoverError,OSError) as e:
  print(json.dumps({'ok':False,'type':type(e).__name__,'message':str(e)},ensure_ascii=False));return 2

if __name__=='__main__':raise SystemExit(main())
