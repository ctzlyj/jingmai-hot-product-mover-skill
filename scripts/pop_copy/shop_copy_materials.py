"""Read and fill SKU content slots in two merchant-owned shops using live product identities."""
from collections import Counter, defaultdict
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import uuid4

from . import pop_copy_pairing as copy
from .shop_copy_history import material_origins
from .safety import atomic_json, file_sha, read_json, redact, safe_join
from .state import FileLock, merchant_home


READ = '/rest/sp-product/v0/sku-materials'
TYPES = (((31,32,33,36),1), ((1002,),2))
SLOTS = ((31,1),(32,1),(32,2),(33,1),(36,1),(1002,1),(1002,2),(1002,3))


def register(subs):
    p=subs.add_parser('copy-materials',help='用两店实时 SKU 盘点素材；已交办时补绑明确缺失的槽位')
    for side in ('source','target'):
        p.add_argument('--'+side+'-profile',required=True)
        p.add_argument('--'+side+'-config',type=Path,required=True)
    p.add_argument('--source-product-id', type=int, required=True)
    p.add_argument('--target-product-id', type=int, action='append', required=True,
                   help='原交办范围内的目标商品，可重复。按每个链接分别核对并出具回执')
    p.add_argument('--confirm',choices=['FILL_MISSING_COPY_MATERIALS'])
    p.add_argument('--health',action='store_true',help='另外读取目标店当前信息分诊断和平台提出的改进项')
    p.add_argument('--history-dir',type=Path,help='对照原搬家目录中的发布回执或商品回读，分别核实同源的每个目标商品')
    p.add_argument('--source-image-name',action='append',default=[],help='按商家已有索引名称查询源店图片空间候选')


def _jfs(url):
    if not isinstance(url,str):
        raise ValueError('素材没有图片路径')
    if url.startswith('https://'):
        parts=urlsplit(url)
        if not (parts.hostname or '').endswith('.360buyimg.com') or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError('素材图片地址无法核对')
        if '/jfs/' in parts.path:
            url=parts.path[parts.path.index('/jfs/')+1:]
    if not re.fullmatch(r'jfs/[A-Za-z0-9_./-]+',url) or '..' in url or '//' in url:
        raise ValueError('素材图片缺少京东原图路径')
    return url


def _slot(item,sid,allowed):
    if not isinstance(item,dict):
        raise ValueError('素材记录格式错误')
    kind,order=int(item['materialType']),int(item['order'])
    if kind not in allowed or order<1 or str(item.get('skuId',sid))!=sid:
        raise ValueError('素材类型、位置或所属 SKU 不符')
    if kind==1002:
        values=(item.get('textMaterial') or {}).get('contents')
        if not isinstance(values,list) or not values or not all(isinstance(v,str) and v.strip() for v in values):
            raise ValueError('卖点文案内容不完整')
        content={'textMaterial':{'contents':values}}
    else:
        image=item['imageMaterial']
        values=image['skuImages']
        if not isinstance(values,list) or not values or int(image.get('imgType',1))!=1:
            raise ValueError('图片素材结构不完整')
        images=sorted(({'index':int(v.get('index',1 if len(values)==1 else 0)),
                         'imgUrl':_jfs(v['imgUrl'])} for v in values),key=lambda v:v['index'])
        if [v['index'] for v in images]!=list(range(1,len(images)+1)):
            raise ValueError('图片组内顺序不完整')
        content={'imageMaterial':{'imgType':1,'skuImages':images}}
        if image.get('region'):
            if not isinstance(image['region'],dict):
                raise ValueError('素材裁切区域格式错误')
            content['imageMaterial']['region']=image['region']
    audit={'3':'pending','4':'approved','5':'rejected'}.get(str(item.get('status')),'unknown')
    return (kind,order), {'content':content,'audit':audit,'raw':item,
                          'reason':redact(str(item.get('auditDesc') or item.get('reason') or ''))}


def snapshot(api, product_id, sku_ids, shop):
    """The official empty shape is an identified SKU row with the materials field omitted."""
    rows={sid:{} for sid in sku_ids}
    gaps={sid:{} for sid in sku_ids}
    shapes={sid:[] for sid in sku_ids}
    jobs=[(sku_ids[n:n+50],types,scope) for n in range(0,len(sku_ids),50) for types,scope in TYPES]
    def get(job):
        ids,types,scope=job
        try:
            reply=api.get('jdo-api-830',READ,{'skuIds':list(map(int,ids)),'materialTypes':list(types),'scopeSet':[scope]})
            if reply.get('success') is not True or reply.get('errorList') or not isinstance(reply.get('data'),list):
                raise ValueError('素材查询缺少成功业务回执')
            found={}
            for row in reply['data']:
                if not isinstance(row,dict):
                    raise ValueError('素材查询的 SKU 行格式错误')
                sid=str(row['skuId'])
                if sid not in ids or sid in found or str(row.get('productId',product_id))!=str(product_id):
                    raise ValueError('素材回读含重复或不属于本商品的 SKU')
                found[sid]=row
        except (api.JdoError,OSError,ValueError,KeyError,TypeError) as exc:
            return ids,types,{},redact(str(exc))[:220]
        return ids,types,found,None
    with ThreadPoolExecutor(max_workers=min(8,max(1,len(jobs)))) as pool:
        for ids,types,found,error in pool.map(get,jobs):
            for sid in ids:
                mats=found.get(sid,{}).get('materials')
                shape='sku-missing'
                if error:shape='request-unverified'
                elif sid in found:
                    shape='materials-array' if isinstance(mats,list) else 'invalid-materials-field'
                    if 'materials' not in found[sid]:
                        shape='identified-sku-only' if set(found[sid])<={'skuId','productId'} else 'unrecognized-sku-row'
                shapes[sid].append({'materialTypes':list(types),'shape':shape,
                                    'returnedCount':len(mats) if isinstance(mats,list) else (0 if shape=='identified-sku-only' else None)})
                try:
                    if shape=='identified-sku-only':
                        mats=[]
                    if not isinstance(mats,list):
                        raise ValueError(error or ('素材列表格式错误' if sid in found else '未返回该 SKU'))
                    slots={}
                    for item in mats:
                        key,value=_slot(item,sid,types)
                        if key in slots:
                            raise ValueError('同一类型和位置有多条记录')
                        slots[key]=value
                    rows[sid].update(slots)
                except (ValueError,TypeError,KeyError) as exc:
                    gaps[sid].update(dict.fromkeys(types,redact(str(exc))[:220]))
    _same_session(api,shop)
    return rows,gaps,shapes


def compare_slots(src, dst, pairing):
    source,src_gaps,src_shapes=src
    target,dst_gaps,dst_shapes=dst
    checks=[]; changes=defaultdict(dict); counts=Counter()
    for sid,slots in source.items():
        tid=pairing['pairs'].get(sid)
        for kind,order in sorted(set(SLOTS)|set(slots)|set(target.get(tid,{}))):
            left=slots.get((kind,order))
            right=target[tid].get((kind,order)) if tid else None
            if not tid: state='unpaired'
            elif kind in src_gaps[sid]: state='source-unreported'
            elif kind in dst_gaps[tid]: state='target-unreported'
            elif not left: state='target-only-preserved' if right else 'source-empty'
            elif left['audit']!='approved': state='source-'+left['audit']
            elif not right:
                state='target-missing'
                changes[tid][(kind,order)]=left['content']
            elif left['content']!=right['content']: state='different'
            elif right['audit']!='approved': state='target-'+right['audit']
            else: state='matched-approved'
            counts[state]+=1
            checks.append({'sourceSkuId':sid,'targetSkuId':tid,'type':kind,'order':order,'state':state,
                           'source':left,'target':right})
    source_count=sum(value['audit']=='approved' for slots in source.values() for value in slots.values())
    unpaired_target=sorted(set(target)-set(pairing['pairs'].values()))
    unresolved=bool(set(counts)-{'source-empty','target-only-preserved','matched-approved'} or unpaired_target)
    status='incomplete' if unresolved else ('verified' if source_count else 'no-source-materials')
    census=Counter()
    for tid,slots in target.items():
        for kind,order in set(SLOTS)|set(slots):
            info=slots.get((kind,order))
            state='unreported' if kind in dst_gaps[tid] else (info['audit'] if info else 'missing')
            census[state]+=1
    return {'checks':checks,'counts':dict(counts),'migrationStatus':status,
            'sourceApprovedSlots':source_count,'matchedApprovedSlots':counts['matched-approved'],
            'targetSlotCensus':dict(census),'sourceSkuCount':len(source),'targetSkuCount':len(target),
            'independentlyPaired':len(pairing['pairs']),'unpairedSource':pairing['reasons'],
            'unpairedTarget':unpaired_target,
            'sourceReadShapes':src_shapes,'targetReadShapes':dst_shapes,
            'sourceReadGaps':{k:v for k,v in src_gaps.items() if v},'targetReadGaps':{k:v for k,v in dst_gaps.items() if v},
            'sourceSlotsReturned':sum(map(len,source.values()))},changes


def _read_health_pages(api, wanted=None):
    # In a batch read all pages once. A single product only needs its own rows.
    rows={};seen=set();page=1;total=None
    while True:
        reply=api.get('jdo-product-price','/rest/sp-product/v0/health-infos',{'page':page,'pageSize':100})
        meta=reply.get('paginationData') or {};items=reply.get('data')
        extent=meta.get('totalItems')
        page_no=meta.get('currentPage',meta.get('pageNum',meta.get('page')))
        if (reply.get('success') is not True or reply.get('errorList') or not isinstance(items,list)
                or not str(extent).isdigit() or (page_no is not None and str(page_no)!=str(page))):
            raise ValueError('信息分服务未完整返回当前页')
        if total is not None and int(extent)!=total:
            raise ValueError('信息分页范围已变化')
        total=int(extent)
        for item in items:
            sid=str(item['skuId'])
            if not sid.isdigit() or sid in seen:raise ValueError('信息分 SKU 重复或编号缺失')
            seen.add(sid)
            if wanted is None or sid in wanted:
                rows[sid]={'score':item.get('healthScore'),'improvements':item.get('improvementList')}
        if len(seen)>total:raise ValueError('信息分列表超出平台总数')
        if len(seen)==total or (wanted is not None and rows.keys()==wanted.keys()):break
        if not items:raise ValueError('信息分列表未读全')
        page+=1
    return rows


def _health(api, target_skus, shop, cache=None):
    if cache is None:
        selected=_read_health_pages(api,target_skus)
    else:
        key=tuple(shop[k] for k in ('profile','shopId','venderId','shopName'))
        if key not in cache:
            try:
                rows=_read_health_pages(api)
                _same_session(api,shop)
                cache[key]={'rows':rows}
            except (api.JdoError,OSError,KeyError,TypeError,ValueError) as exc:
                cache[key]={'status':'unverified','reason':redact(str(exc))[:220]}
        if 'rows' not in cache[key]:
            return cache[key]
        selected={sid:cache[key]['rows'][sid] for sid in target_skus if sid in cache[key]['rows']}
    unreported=sorted(set(target_skus)-set(selected))
    unresolved=[sid for sid,row in selected.items() if row['score'] is None or
                not isinstance(row['improvements'],list)]
    problems=Counter()
    for row in selected.values():
        for issue in row['improvements'] if isinstance(row['improvements'],list) else []:
            detail=issue.get('improvementDetail') if isinstance(issue,dict) else None
            if isinstance(detail,dict) and detail.get('troubleCode'):
                problems[str(detail['troubleCode'])]+=1
    return {'status':'partial' if unreported or unresolved else 'observed','skus':selected,
            'skuCount':len(target_skus),'returnedSkuCount':len(selected),'unreportedSkus':unreported,
            'incompleteHealthRows':unresolved,'improvementCounts':dict(problems)}


def source_asset_candidates(api, terms):
    """Search a bounded page for each source identifier; only bound SKU slots prove order and purpose."""
    zones=api.get('jdo-product-material','/rest/sp-product/v0/media-zones')
    if zones.get('success') is not True or zones.get('errorList') or not isinstance(zones.get('data'),list):
        raise ValueError('源店图片空间列表回执不完整')
    active=[str(row['zoneId']) for row in zones['data'] if str(row.get('zoneType'))=='1' and row.get('zoneId')]
    if len(active)!=1:
        raise ValueError('源店图片空间未唯一确认')
    results=[]
    for term in terms:
        reply=api.get('jdo-product-material','/rest/sp-product/v0/media-zones/{zoneId}/images',
                      {'zoneId':active[0],'imgName':term,'page':1,'pageSize':20})
        if reply.get('success') is not True or reply.get('errorList') or not isinstance(reply.get('data'),list):
            raise ValueError('源店按名称查询图片空间失败')
        candidates=[{k:row.get(k) for k in ('imgId','imgName','imgUrl','categoryId','imgWidth','imgHeight')}
                    for row in reply['data'] if row.get('imgId') and row.get('imgUrl')]
        results.append({'name':term,'page':1,'candidateCount':len(candidates),'candidates':candidates,
                        'totalReported':(reply.get('paginationData') or {}).get('totalItems')})
    return {'status':'searched','zoneId':active[0],'queries':results,'bindingProven':False}


def _request_rows(changes,pid):
    grouped=defaultdict(list)
    for sid,slots in sorted(changes.items()):
        types=defaultdict(list)
        for (kind,order),content in sorted(slots.items()):
            item={'skuId':int(sid),'materialType':kind,'order':order,**content}
            if 'imageMaterial' in item:item['imageMaterial']={**item['imageMaterial'],'replaceType':1}
            types[kind].append(item)
        for kind,items in types.items():
            grouped[kind].append({'productId':int(pid),'skuId':int(sid),'materials':items})
    for entries in grouped.values():
        for start in range(0,len(entries),50):
            yield entries[start:start+50]


def _keys(entries):
    return {f"{row['skuId']}/{m['materialType']}/{m['order']}" for row in entries for m in row['materials']}


WRITE = READ + '/batch'
PERMISSION_REFUSALS = {'99904030007','99904030008'}


def _refusal_codes(codes, body):
    if not isinstance(codes,list) or not codes or any(not isinstance(code,str) for code in codes):
        return False
    unique=set(codes)
    if unique<=PERMISSION_REFUSALS:
        return True
    # An absent SKU prevents binding any slot of that SKU. A heterogeneous batch can
    # also contain successfully applied siblings, even if the returned data is empty.
    return unique=={'10109002101'} and len({row['skuId'] for row in body['skuMaterials']})==1


def _write_outcome(reply,body):
    if not isinstance(reply,dict):
        return 'outcome-unknown',[]
    errors=reply.get('errorList')
    if (reply.get('success') is False and isinstance(errors,list) and all(isinstance(row,dict) for row in errors)
            and reply.get('data') in (None,{}) and set(reply)<={'success','errorList','data','requestId','traceId'}
            and all(set(row)<={'message','code'} for row in errors)):
        codes=[str(row.get('code','')) for row in errors]
        if _refusal_codes(codes,body):
            return 'business-rejected',codes
    data=reply.get('data')
    if (reply.get('success') is True and not errors and data is not False
            and not (isinstance(data,dict) and (data.get('success') is False or data.get('errorList')))):
        return 'response-received',[]
    return 'outcome-unknown',[]


def _documented_refusal(root,fp,rec,body):
    if not body or rec.get('status')!='business-rejected':
        return False
    proof=read_json(root/'requests'/fp/'rejection.json',{})
    return bool(proof.get('call')==['POST',WRITE]
                and proof.get('requestFingerprint')==fp and proof.get('noPartialResults') is True
                and proof.get('codes')==rec.get('platformCodes') and _refusal_codes(proof['codes'],body))


def verified_request_body(entry,root,fp,rec):
    if not re.fullmatch(r'[a-f0-9]{64}',str(fp)):
        return None
    identity={k:rec[k] for k in ('sourceShop','targetShop','sourceProductId','targetProductId')}
    body=read_json(root/'requests'/fp/'body.json')
    if body and entry.digest([identity,body])==fp and _keys(body['skuMaterials'])==set(rec['slots']):
        return body
    return None


def receipt_readbacks(entry, root, receipts, dst):
    rows,gaps,_=dst
    results={}
    for fp,rec in receipts.items():
        body=verified_request_body(entry,root,fp,rec)
        states={}
        if body:
            for row in body['skuMaterials']:
                sid=str(row['skuId'])
                for item in row['materials']:
                    kind,order=int(item['materialType']),int(item['order'])
                    key=f'{sid}/{kind}/{order}'
                    if sid not in rows or kind in gaps[sid]:
                        states[key]='unreported'
                        continue
                    slot,expected=_slot(item,sid,(kind,))
                    actual=rows[sid].get(slot)
                    if not actual:
                        states[key]='missing'
                    elif expected['content']!=actual['content']:
                        states[key]='different'
                    else:
                        states[key]='matched-'+actual['audit']
        counts=Counter(states.values()) if states else Counter({'original-body-unverified':len(rec['slots'])})
        status=rec['status']
        if status=='business-rejected' and not _documented_refusal(root,fp,rec,body):
            status='outcome-unknown'
        results[fp]={'requestStatus':status,'readback':dict(counts),'slots':states,
                     'reason':rec.get('error'),'platformCodes':rec.get('platformCodes',[])}
    return results


def _pair(entry, frozen, target, detail):
    mapping=copy.pair_frozen_target_skus(frozen,detail)
    if mapping['reasons']:
        try:
            ref=entry.reference(target,detail,for_creation=False)
        except (entry.api.JdoError,OSError,ValueError):
            ref=None
        if ref:
            mapping=copy.pair_frozen_target_skus(frozen,detail,ref)
    return mapping


def _same_session(api, expected):
    current=api.recheck_verified_jdo_store()
    if not all(str(current.get(k))==str(expected[k]) for k in ('profile','shopId','venderId','shopName')):
        raise ValueError('素材操作期间店铺身份发生变化，需要重新核对两店')


def _numbers(entry, info):
    number=info.get('itemNum')
    if not isinstance(number,str) or not number.strip():
        return []
    try:
        rows=entry.pipeline.list_products(item_num=number,require_success=True)
    except (entry.api.JdoError,entry.pipeline.PipelineError,OSError,ValueError):
        return []
    # Reject a partial row instead of silently counting it as a different item.
    if any(row.get('itemNum') is None for row in rows):
        return []
    return [str(row['productId']) for row in rows if row['itemNum']==number]


def product_pair(entry, frozen, detail, source_numbers, origins=()):
    src=frozen['sourceDetail']['productInfo']
    dst=detail['productInfo']
    product_id=str(dst['productId'])
    source_id=frozen['sourceProductId']
    def facts(row):
        values={'title':row.get('productName'),'itemNumber':row.get('itemNum'),
                'category':(row.get('categoryDetail') or {}).get('lastCategoryId'),
                'brand':(row.get('brandInfo') or {}).get('brandId',row.get('brandId')),
                'packSpecification':row.get('packSpecification')}
        invalid=[]
        for key in ('length','width','height','weight'):
            aliases=('width','wide') if key=='width' else (key,)
            numbers={copy._number(row[k]) for k in aliases if row.get(k) not in (None,'')}
            if len(numbers)>1 or None in numbers:
                invalid.append(key)
            values[key]=numbers.pop() if len(numbers)==1 else None
        for attr in row.get('goodsAttrInfos') or []:
            if isinstance(attr,dict) and attr.get('attrId'):
                for key in ('values','attrValues','attrValue'):
                    if attr.get(key):
                        values['attribute.'+str(attr['attrId'])]=entry.digest(attr[key])
                        break
        return values,invalid
    left,bad_left=facts(src)
    right,bad_right=facts(dst)
    conflicts=[k for k in left if left[k] not in (None,'') and right.get(k) not in (None,'') and
               str(left[k])!=str(right[k])]
    conflicts=sorted(set(conflicts+bad_left+bad_right))
    proof={'verified':False,'sourceProductId':source_id,'targetProductId':product_id,'conflicts':conflicts}
    if origins:
        proof['directHistoryReceipts']=[{'basis':o['basis'],'file':o['receiptFile']} for o in origins]
    if any(k!='itemNumber' for k in conflicts):
        return {**proof,'reason':'live-product-facts-conflict'}
    for origin in origins:
        if (not left['title'] or left['title']!=right['title'] or left['title']!=origin['title']
                or not left['category'] or str(left['category'])!=str(right['category'])
                or str(left['category'])!=origin['category']
                or ('targetSkuCount' in origin and origin['targetSkuCount']!=len(detail['skuList']))):
            continue
        # The original publisher could explicitly replace the merchant item number with the source SPU.
        if conflicts and not (str(right['itemNumber'])==origin['itemNumber']==source_id):
            continue
        physical=[k for k in ('length','width','height','weight') if left[k] not in (None,'0') and left[k]==right.get(k)]
        descriptive=[k for k in left if (k.startswith('attribute.') or k=='packSpecification')
                     and left[k] and left[k]==right.get(k)]
        if str(left['brand']).isdigit() and int(left['brand'])>0 and str(left['brand'])==str(right['brand']):
            descriptive.append('brand')
        if len(physical+descriptive)>=2:
            return {**proof,'verified':True,'basis':origin['basis'],'history':origin,'conflicts':[],
                    'recordedItemNumberChange':bool(conflicts),'matchingProductFacts':['title','category',*physical,*descriptive]}
    if conflicts:
        return {**proof,'reason':'live-product-facts-conflict'}
    task=entry.pair_key(frozen)
    folder=entry.task_directory(task)
    if (folder/'current.json').exists():
        _,original=entry.load_copy(folder)
        if any(frozen[k]!=original[k] for k in ('sourceShop','targetShop','sourceProductId')):
            return {**proof,'reason':'original-copy-shop-or-source-conflict'}
        prior=entry.publication_state(folder,task)
        bound=str(prior.get('targetProductId') or '')
        if bound and bound!=product_id:
            return {**proof,'reason':'original-copy-target-conflict','originalTargetProductId':bound}
        if bound==product_id and prior['status']=='submitted':
            out=Path(prior['receiptDirectory'])
            marker=read_json(out/'publish-body.submit-state.json',{})
            result=read_json(out/'result.json',{})
            native=result.get('data') if isinstance(result.get('data'),dict) else {}
            bound_apply=read_json(out/'target-binding.json',{})
            native_id=(str(native.get('productId') or '')==product_id or
                       (bound_apply and str(native.get('applyId') or '')==str(bound_apply.get('applyId'))))
            if (result.get('success') is True and not result.get('errorList') and native_id and
                    (out/'publish-body.json').is_file() and marker.get('payloadSha256')==file_sha(out/'publish-body.json')):
                return {**proof,'verified':True,'basis':'original-copy-publication','taskId':task}
    if not all(isinstance(left[k],str) and left[k].strip() and left[k]==right[k] for k in ('title','itemNumber')) or not (
            left['category'] and right['category'] and str(left['category'])==str(right['category'])):
        return {**proof,'reason':'missing-live-product-identity'}
    target_numbers=_numbers(entry,dst)
    if source_numbers!=[source_id] or target_numbers!=[product_id]:
        return {**proof,'reason':'item-number-not-unique-in-both-catalogues',
                'sourceMatches':source_numbers,'targetMatches':target_numbers}
    return {**proof,'verified':True,'basis':'unique-item-number-and-product-facts'}


def _read_source(args, entry):
    api=entry.api
    source=entry.verify(args.source_config,args.source_profile)
    src_id=args.source_product_id
    source_detail=entry.read_owned_detail(src_id)
    source_numbers=_numbers(entry,source_detail['productInfo'])
    src=snapshot(api,src_id,list(copy.sku_rows(source_detail,src_id)),source)
    terms=list(dict.fromkeys(getattr(args,'source_image_name',[]) or []))
    if len(terms)>20 or any(not term.strip() or len(term)>180 for term in terms):
        raise ValueError('每次最多查询 20 个有效源店图片名称')
    candidates={'status':'not-queried'}
    if terms or (not any(src[0].values()) and not any(src[1].values())):
        terms=terms or list(dict.fromkeys(str(s) for s in (src_id,source_detail['productInfo'].get('itemNum')) if s))
        try:candidates=source_asset_candidates(api,terms)
        except (api.JdoError,OSError,KeyError,TypeError,ValueError) as exc:
            candidates={'status':'unverified','reason':redact(str(exc))[:220]}
        _same_session(api,source)
    return source,source_detail,source_numbers,src,candidates


def handle(args, entry):
    raw = args.target_product_id
    targets = list(dict.fromkeys(raw if isinstance(raw, list) else [raw]))
    if not targets or min([args.source_product_id, *targets]) < 1 or args.source_product_id in targets:
        raise ValueError('请提供有效且不同于源商品的目标商品编号')
    if len(targets) == 1:
        single = Namespace(**{**vars(args), 'target_product_id': targets[0]})
        return _one_target(single, entry, _read_source(single, entry))

    # One active CLI profile per process. Keep profile changes sequential.
    # Each target has its own locked ledger. Re-running reconciles uncertain writes.
    original = None if args.confirm else _read_source(args, entry)
    report_file = safe_join(merchant_home(), 'work/shop-copy-materials/batches/'+uuid4().hex+'.json')
    health = {}
    batch = {'ok':False, 'status':'running', 'sourceProductId':args.source_product_id,
             'targetProductIds':targets, 'rows':[], 'reportFile':str(report_file)}
    atomic_json(report_file, batch)
    for tid in targets:
        single = Namespace(**{**vars(args), 'target_product_id':tid, '_batch_health':health})
        try:
            receipt = _one_target(single, entry, original or _read_source(single, entry))
        except (entry.api.JdoError, entry.pipeline.PipelineError, entry.matrix.MatrixError,
                OSError, ValueError, KeyError, TypeError) as exc:
            receipt = {'ok':False, 'migrationStatus':'unverified', 'reason':redact(str(exc))[:240]}
        batch['rows'].append({'targetProductId':tid, **receipt})
        atomic_json(report_file, batch)
    batch['ok'] = all(row['ok'] for row in batch['rows'])
    if args.confirm:
        batch['status'] = 'material-copy-checked' if batch['ok'] else 'material-copy-incomplete'
    else:
        batch['status'] = 'material-audit-ready' if batch['ok'] else 'material-audit-partial'
    atomic_json(report_file, batch)
    return batch


def _one_target(args, entry, source_read):
    api = entry.api
    src_id, dst_id = args.source_product_id, args.target_product_id
    source, source_detail, source_numbers, src, candidates = source_read
    target=entry.verify(args.target_config,args.target_profile)
    if source['shopId']==target['shopId'] or source['profile']==target['profile'] or src_id==dst_id:
        raise ValueError('请核对两店与已落地的源、目标商品')
    key=entry.digest([target['shopId'],target['venderId'],dst_id])
    root=safe_join(merchant_home(),'work/shop-copy-materials/'+key)
    with FileLock(root/'material.lock'):
        dst_detail=entry.read_owned_detail(dst_id)
        dst_ids=list(copy.sku_rows(dst_detail,dst_id))
        frozen=copy.freeze_copy(source,target,source_detail)
        history=getattr(args,'history_dir',None)
        origins=material_origins(history,frozen,str(dst_id)) if history else ()
        product_evidence=product_pair(entry,frozen,dst_detail,source_numbers,origins)
        mapping=_pair(entry,frozen,target,dst_detail) if product_evidence['verified'] else {
            'pairs':{},'reasons':{sid:'product-pair-unverified' for sid in src[0]}}
        dst=snapshot(api,dst_id,dst_ids,target)
        report,changes=compare_slots(src,dst,mapping)
        receipts=read_json(root/'requests.json',{})
        before=dst
        posted=0; blocked=[]; changed_before_write=[]
        if args.confirm:
            reserved=set()
            rejected=defaultdict(set)
            for fp,rec in receipts.items():
                body=verified_request_body(entry,root,fp,rec) if rec['status']=='business-rejected' else None
                if not body or not _documented_refusal(root,fp,rec,body):
                    reserved.update(rec['slots'])
                    continue
                for row in body['skuMaterials']:
                    for item in row['materials']:
                        key=f"{row['skuId']}/{item['materialType']}/{item['order']}"
                        rejected[key].add(entry.digest(item))
            for entries in _request_rows(changes,dst_id):
                candidate_ids=[str(row['skuId']) for row in entries]
                fresh=entry.read_owned_detail(dst_id)
                product_evidence=product_pair(entry,frozen,fresh,source_numbers,origins)
                fresh_mapping=_pair(entry,frozen,target,fresh) if product_evidence['verified'] else {'pairs':{}}
                same_targets={tid for sid,tid in mapping['pairs'].items() if fresh_mapping['pairs'].get(sid)==tid}
                current_ids=[sid for sid in candidate_ids if sid in same_targets]
                current,gaps,_=snapshot(api,dst_id,current_ids,target)
                eligible=[]
                unchanged_rejections=set()
                for row in entries:
                    sid=str(row['skuId'])
                    if sid not in current:
                        continue
                    clean=[]
                    for m in row['materials']:
                        key=f"{sid}/{m['materialType']}/{m['order']}"
                        if key in rejected and entry.digest(m) in rejected[key]:
                            unchanged_rejections.add(key)
                        elif (key not in reserved and m['materialType'] not in gaps[sid]
                              and (m['materialType'],m['order']) not in current[sid]):
                            clean.append(m)
                    if clean:eligible.append({**row,'materials':clean})
                excluded=_keys(entries)-_keys(eligible)
                blocked.extend(sorted((excluded & reserved)|unchanged_rejections))
                changed_before_write.extend(sorted(excluded-reserved-unchanged_rejections))
                if not eligible:continue
                before_write=getattr(args,'_before_write',None)
                if before_write is not None:
                    before_write()
                body={'skuMaterials':eligible}
                identity={'sourceShop':source,'targetShop':target,'sourceProductId':src_id,'targetProductId':dst_id}
                fingerprint=entry.digest([identity,body]); attempt=root/'requests'/fingerprint
                atomic_json(attempt/'body.json',body)
                record={'status':'outcome-unknown',**identity,'slots':sorted(_keys(eligible))}
                receipts[fingerprint]=record
                atomic_json(root/'requests.json',receipts)
                reserved.update(record['slots'])
                posted+=1
                try:
                    reply=api.run_jdo_large(skill_id='jdo-api-830',method='POST',url=READ+'/batch',
                                            body_path=attempt/'body.json',yes=True)
                    atomic_json(attempt/'response.json',reply)
                    record['status'],codes=_write_outcome(reply,body)
                    if codes:record['platformCodes']=codes
                    if isinstance(reply,dict) and reply.get('errorList'):
                        record['error']=redact(str(reply['errorList']))[:300]
                except (api.JdoError,OSError,RuntimeError) as exc:
                    record['error']=redact(str(exc))[:220]
                    if (isinstance(exc,api.JdoError) and getattr(exc,'platform_call',None)==('POST',WRITE)
                            and getattr(exc,'no_issued_ids',False) is True and getattr(exc,'no_partial_results',False) is True
                            and _refusal_codes(getattr(exc,'platform_codes',None),body)):
                        record['status']='business-rejected'
                        record['platformCodes']=exc.platform_codes
                if record['status']=='business-rejected':
                    atomic_json(attempt/'rejection.json',{'call':['POST',WRITE],'codes':record['platformCodes'],
                                                          'requestFingerprint':fingerprint,'noPartialResults':True})
                atomic_json(root/'requests.json',receipts)
            if changes:
                dst_detail=entry.read_owned_detail(dst_id)
                dst_ids=list(copy.sku_rows(dst_detail,dst_id))
                dst=snapshot(api,dst_id,dst_ids,target)
                product_evidence=product_pair(entry,frozen,dst_detail,source_numbers,origins)
                latest=_pair(entry,frozen,target,dst_detail) if product_evidence['verified'] else {
                    'pairs':{},'reasons':{sid:'product-pair-unverified' for sid in src[0]}}
                report,_=compare_slots(src,dst,latest)
        original_receipts=receipt_readbacks(entry,root,receipts,dst)
        preserved=[{'skuId':sid,'type':kind,'order':order} for sid,slots in before[0].items()
                   for (kind,order),value in slots.items()
                   if dst[0].get(sid,{}).get((kind,order),{}).get('content')!=value['content']]
        health={'status':'not-queried'}
        if args.health:
            try:health=_health(api,dst[0],target,getattr(args,'_batch_health',None))
            except (api.JdoError,OSError,KeyError,TypeError,ValueError) as exc:
                health={'status':'unverified','reason':redact(str(exc))[:220]}
            _same_session(api,target)
        stamp=datetime.now(timezone.utc).isoformat()
        path=root/('report-'+entry.digest([source['shopId'],src_id,stamp])[:24]+'.json')
        if preserved:
            report['migrationStatus']='incomplete'
        if not product_evidence['verified']:report['migrationStatus']='unverified-product-pair'
        if history and not product_evidence['verified']:
            sku_candidates=copy.pair_frozen_target_skus(frozen,dst_detail)
            product_evidence['readOnlySpecificationMatches']=len(sku_candidates['pairs'])
        report.update(sourceShop=source,targetShop=target,sourceProductId=src_id,targetProductId=dst_id,
                      productPair=product_evidence,
                      checkedAt=stamp,platformRequestsIssued=posted,originalRequests=original_receipts,
                      preservedSlotDifferences=preserved,reservedSlotsSkipped=blocked,
                      changedBeforeWrite=changed_before_write,targetHealth=health,sourceImageSpace=candidates)
        if report['sourceSlotsReturned']==0:
            report['sourceEvidenceNextStep']='按本商品核对源店图片空间候选和原通过记录，取得 SKU、用途及顺序后再绑定。'
        atomic_json(path,report)
        completed=report['migrationStatus']=='verified'
        audit_complete=not (report['sourceReadGaps'] or report['targetReadGaps'] or
                            report['unpairedSource'] or report['unpairedTarget'])
        return {'ok':(completed if args.confirm else audit_complete) and (not args.health or health['status']=='observed'),
                'status':('material-audit-ready' if audit_complete else 'material-audit-partial') if not args.confirm
                         else ('material-copy-checked' if completed else 'material-copy-incomplete'),
                'migrationStatus':report['migrationStatus'],'auditComplete':audit_complete,
                'productPair':product_evidence,
                'counts':report['counts'],'sourceSkuCount':report['sourceSkuCount'],
                'sourceApprovedSlots':report['sourceApprovedSlots'],'matchedApprovedSlots':report['matchedApprovedSlots'],
                'targetSlotCensus':report['targetSlotCensus'],
                'targetSkuCount':len(dst_ids),'pairedSkuCount':report['independentlyPaired'],
                'sourceSlotsReturned':report['sourceSlotsReturned'],'targetHealthStatus':health['status'],
                'healthSkuCount':health.get('returnedSkuCount'),'healthImprovementCounts':health.get('improvementCounts'),
                'sourceImageSpaceStatus':candidates['status'],
                'platformRequestsIssued':posted,'reservedSlotsSkipped':len(blocked),'preservedSlotDifferences':len(preserved),
                'reportFile':str(path),'checkedAt':stamp}
