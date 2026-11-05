"""Portable listing-package contracts. Only user-confirmed facts become publishing parameters."""
from __future__ import annotations
import hashlib,json,re
from copy import deepcopy
from decimal import Decimal,InvalidOperation
from pathlib import Path
from PIL import Image
from photo_generation import atomic_json,load,sha

SCHEMA_VERSION='listing-package/v1'
class ReconcileRequired(RuntimeError):pass

def package_path(root,relative):
 root=Path(root).resolve(); text=str(relative)
 if '://' in text or Path(text).is_absolute() or re.match(r'^[A-Za-z]:',text):raise ValueError('A package path must be relative')
 target=(root/text).resolve()
 if root!=target and root not in target.parents:raise ValueError('Package path escapes listing-package')
 return target

def check_store(store):
 if not re.fullmatch(r'[a-z0-9][a-z0-9-]{1,61}[a-z0-9]',str(store.get('profile') or '')) or store['profile']=='default':raise ValueError('An explicit non-default Profile is required')
 if int(store.get('venderId') or 0)<=0 or not str(store.get('shopName') or '').strip():raise ValueError('Store identity incomplete')
 return store

def walk_components(node):
 if isinstance(node,dict):
  if 'componentCode' in node:yield node
  for v in node.values():yield from walk_components(v)
 elif isinstance(node,list):
  for v in node:yield from walk_components(v)

def required(component):
 return any(r.get('validateType')=='required' and str(r.get('validateRule')).lower()=='true' for r in component.get('validateRule') or [])

def option_valid(component,values):
 available={str(v['value']) for v in component.get('optionsValueList') or []}
 return bool(values) and (not available or set(map(str,values)).issubset(available))

def missing_information(facts,reference):
 missing=[]
 for field in ('confirmed','title','categoryId','brandId','skus','packageLengthMm','packageWidthMm','packageHeightMm','weightKg','freightTemplateId','sevenDayReturnCode'):
  value=facts.get(field)
  if value is None or value=='' or (field in ('confirmed','skus') and not value):missing.append(field)
 if facts.get('confirmed') is not True and 'confirmed' not in missing:missing.append('confirmed')
 if int(facts.get('categoryId') or 0)!=int((reference.get('category') or {}).get('id') or 0):missing.append('category-template-mismatch')
 if facts.get('brandId') is not None:
  brands={str(b.get('brandId')) for b in reference.get('brands') or []}
  if str(facts['brandId']) not in brands:missing.append('brand-not-authorized')
 attrs={a['componentCode']:a for a in facts.get('categoryAttributes') or []}
 sku_components=[]
 for c in walk_components(reference.get('schema') or {}):
  code=c['componentCode']
  if code.startswith('prop-product-') and required(c):
   if not option_valid(c,(attrs.get(code) or {}).get('valueIds')):missing.append(code)
  if code.startswith('prop-sku-') and required(c):sku_components.append(c)
 seen=set()
 for i,sku in enumerate(facts.get('skus') or []):
  for field in ('key','spec','jdPrice','stock','skuAttributes'):
   if sku.get(field) is None or sku.get(field)=='':missing.append(f'skus[{i}].{field}')
  if sku.get('key') in seen:missing.append('duplicate-sku-key')
  seen.add(sku.get('key'))
  try:
   if Decimal(str(sku.get('jdPrice')))<=0:raise ValueError()
   stock=Decimal(str(sku.get('stock')))
   if stock<0 or stock!=stock.to_integral():raise ValueError()
  except (ValueError,InvalidOperation):missing.append(f'skus[{i}].price-stock')
  values={a['componentCode']:a['valueId'] for a in sku.get('skuAttributes') or []}
  for c in sku_components:
   if not option_valid(c,[values.get(c['componentCode'])] if c['componentCode'] in values else []):missing.append(f'skus[{i}].{c["componentCode"]}')
 return list(dict.fromkeys(missing))

def media_entries(manifest):
 for key in ('mainImages','detailImages'):
  for item in manifest.get(key) or []:yield item
 for sku in manifest.get('variants') or []:
  if sku.get('skuImage'):yield sku['skuImage'] if isinstance(sku['skuImage'],dict) else {'localPath':sku['skuImage']}

def review_digest(manifest,root):
 images=[{'localPath':m['localPath'],'sha256':sha(package_path(root,m['localPath']))} for m in media_entries(manifest)]
 value={'store':manifest.get('store'),'facts':manifest.get('facts'),'images':images}
 return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()

def can_submit(record):
 if record.get('status') in ('SUBMITTING','UNKNOWN','SUCCEEDED'):raise ReconcileRequired('Operation already dispatched; reconcile remotely, never replay')

def join_detail_panels(panels,destination,width=790):
 if not panels:raise ValueError('No detail panels')
 images=[]
 for p in panels:
  with Image.open(p) as source:
   im=source.convert('RGB');im=im.resize((width,round(im.height*width/im.width)),Image.Resampling.LANCZOS);images.append(im)
 height=sum(im.height for im in images)
 if height>20000:raise ValueError('Split the detail page into bounded sections')
 result=Image.new('RGB',(width,height),'white');y=0
 for im in images:result.paste(im,(0,y));y+=im.height
 destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True);result.save(destination,'JPEG',quality=92)
 return destination

def initial_manifest(store,photos,title,reference_url=None):
 check_store(store)
 return {'schemaVersion':SCHEMA_VERSION,'mode':'photo-new-listing','status':'INFORMATION_PENDING','store':deepcopy(store),
         'profile':store['profile'],'venderId':int(store['venderId']),'shopName':store['shopName'],'productId':None,'skuIds':[],
         'title':title,'sellingPoints':[],'facts':{},'sourcePhotos':photos,'reference':{'url':reference_url,'purpose':'style-only','questionAnswered':True},
         'mainImages':[],'detailImages':[],'variants':[],'timings':{},'video':{}}
