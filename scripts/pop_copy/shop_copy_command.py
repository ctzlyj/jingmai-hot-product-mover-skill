"""Copy one merchant-owned product across two explicitly verified POP shops."""
import argparse
from contextlib import redirect_stdout
from hashlib import sha256
import io
import json
from pathlib import Path
import re
import sys
import time

import jdo_listing_mvp as api
import jd_product_pipeline as pipeline
import matrix_listing as matrix
from . import owned_product_copy as copy
from .safety import atomic_json, file_sha, read_json, safe_join
from .shop_copy_delivery import capture_freight
from .state import FileLock, merchant_home


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode()).hexdigest()


def verify(config, profile):
    settings = read_json(config) or {}
    bound = (settings.get('stores') or {}).get(profile) or settings.get('store') or {}
    if not str(bound.get('shopId') or '').isdigit():
        raise ValueError('请从原店铺核验回执取得 shopId，再与 venderId 一起绑定到配置')
    store = api.verify_configured_store_identity(config, profile)
    if any(not str(store.get(k) or '').isascii() or not str(store[k]).isdigit() or int(store[k]) <= 0
           for k in ('shopId', 'venderId')):
        raise ValueError('官方核店缺少店铺编号或商家编号')
    return {k: str(store[k]) for k in ('profile', 'shopName', 'shopId', 'venderId')}


def inventory(args):
    target = verify(args.target_config,args.target_profile)
    source = verify(args.source_config,args.source_profile)
    if source['shopId']==target['shopId'] or source['profile']==target['profile']:
        raise ValueError('请选择两家不同的已授权店铺')
    products, seen = [], set()
    reasons = []
    for status in sorted(set(args.status)):
        expected = None
        count, page = 0, 1
        while True:
            response = api.get('jdo-product-status','/rest/sp-product/v0/products',{
                'scopeSet':['productId','productName','productStatus'],'productStatusList':str(status),
                'page':page,'pageSize':20,'sortBy':'created','sortType':'asc'})
            rows, meta = response.get('data'), response.get('paginationData') or {}
            if not isinstance(rows,list) or not str(meta.get('totalItems','')).isdigit():
                reasons.append(f'状态 {status} 第 {page} 页没有完整的列表与总数')
                break
            total = int(meta['totalItems'])
            if (expected is not None and expected != total) or int(meta.get('pageNum',page)) != page:
                reasons.append(f'状态 {status} 的列表在翻页期间发生变化')
                break
            expected = total
            ids = [str(r.get('productId') or '') for r in rows]
            if (any(not re.fullmatch('[1-9][0-9]*', pid) for pid in ids) or len(ids) != len(set(ids))
                    or seen.intersection(ids) or any(str(r.get('productStatus'))!=str(status) for r in rows)):
                reasons.append(f'状态 {status} 第 {page} 页存在身份重复、缺失或状态不一致')
                break
            products.extend({'productId':pid, 'title':r.get('productName'), 'status':status} for pid,r in zip(ids,rows))
            seen.update(ids)
            count += len(rows)
            if count == total:
                break
            if not rows or count > total:
                reasons.append(f'状态 {status} 的行数与官方总数不一致')
                break
            page += 1
    api.recheck_verified_jdo_store()
    record = {'sourceShop':source,'targetShop':target,'selectedStatuses':sorted(set(args.status)),'products':products,
              'issues':reasons,'status':'partial-inventory' if reasons else 'inventory-ready','publishedProducts':0}
    out = safe_join(merchant_home(), 'work/shop-copy-inventories/'+digest(record)[:32]+'.json')
    atomic_json(out,record)
    return {k:v for k,v in record.items() if k!='products'} | {
        'recordedProducts':len(products),'examples':products[:3],'inventoryFile':str(out)}


def read_owned_detail(pid):
    detail = matrix.read_product(pid)
    product = detail.get('productInfo') or {}
    if str(product.get('productId') or '') != str(pid):
        raise ValueError('官方商品详情没有返回所选商品，请核对原店铺和商品编号')
    active = api.require_verified_jdo_store()
    checked = set()
    for record in (product, detail):
        for key in ('venderId','shopId'):
            if key in record:
                if str(record[key]) != str(active[key]):
                    raise ValueError('商品详情中的店铺身份与本次核验的店铺不符')
                checked.add(key)
    if len(checked) != 2:
        # Some detail responses omit owner IDs. Prove membership in the authenticated
        # seller catalogue rather than treating a product URL as ownership evidence.
        result = api.get('jdo-product-status','/rest/sp-product/v0/products',{
            'productIdList':[int(pid)], 'scopeSet':'productId,productName,productStatus',
            'page':1,'pageSize':20})
        rows = result.get('data')
        if not isinstance(rows,list) or len(rows)!=1 or str(rows[0].get('productId'))!=str(pid):
            raise ValueError('当前店铺商品清单没有唯一匹配这一个商品')
    return detail


def read_product(pid):
    detail = read_owned_detail(pid)
    ids = [int(row['skuId']) for row in detail.get('skuList') or []]
    gaps = []
    try:
        stocks = api.query_sku_stocks(ids)
    except api.JdoError:
        stocks = None
        gaps.append('stock-service')
    try:
        videos = pipeline.existing_video_materials(ids)
    except api.JdoError:
        videos = None
        gaps.append('video-material-service')
    return detail, stocks, videos, gaps


def category(detail):
    return int(detail['productInfo']['categoryDetail']['lastCategoryId'])


def reference(shop, detail, *, for_creation=True, with_sale_templates=True):
    cid = category(detail)
    response = api.get('jdo-product-publish', '/rest/sp-product/v0/product-schemas',
                       {'scene':'pop','lastCategoryId':str(cid)}).get('data')
    if not isinstance(response, list) or len(response) != 1:
        raise ValueError('未取得当前类目唯一的官方创建模板')
    schema = json.loads(response[0]) if isinstance(response[0], str) else response[0]
    if not isinstance(schema, dict) or not schema.get('componentList'):
        raise ValueError('当前店铺的类目模板缺少组件')
    brands = (api.get('jdo-product-publish', '/rest/sp-seller/v0/vender-brands',
                      {'queryType':2, 'categoryId':cid}).get('data') if for_creation else [])
    result = {'shopIdentity':shop, 'category':{'id':cid}, 'schema':schema, 'brands':brands}
    if with_sale_templates:
        import sale_attr_templates as sat
        structured = [n for n in copy._template_nodes(schema['componentList'])
                      if n.get('componentCode','').startswith('sale-prop-') and
                      str((n.get('extra') or {}).get('isStructure','')).lower() in ('true','1')]
        if structured:
            def official(category_id):
                return api.get('jdo-product-publish','/rest/sp-product/v0/categories/{categoryId}/sale-attr-templates',
                               {'categoryId':category_id}).get('data')
            def samples(category_id):
                found = []
                for row in pipeline.list_products(category_id=category_id):
                    status = row.get('productStatusNew') or row.get('productStatus')
                    if str(status) not in ('1','103'):
                        continue
                    sample = read_owned_detail(int(row['productId']))
                    if category(sample)==category_id:
                        found.append({**sample,'liveProductStatus':int(status)})
                    if len(found)==6:
                        break
                return found
            try:
                resolved = sat.resolve(structured,cid,fetch_official=official,fetch_samples=samples)
            except (api.JdoError,pipeline.PipelineError,ValueError,KeyError,TypeError):
                resolved = {'templates':{},'missing':[n['componentCode'] for n in structured]}
            for template in resolved['templates'].values():
                template['source'].update(shopIdentity=shop,categoryId=cid)
            result.update(structuredSaleTemplates=resolved['templates'],structuredSaleTemplateGaps=resolved['missing'])
    return result


def read_components(shop, detail):
    try:
        return reference(shop, detail, for_creation=False, with_sale_templates=False)['schema']['componentList']
    except (api.JdoError, ValueError):
        return []  # A read-only seller can still provide the product and its selected freight ID.


def task_directory(task):
    if not re.fullmatch('[0-9a-f]{64}', task):
        raise ValueError('请使用原任务回执中的 taskId')
    root = safe_join(merchant_home(), 'work/shop-copies/' + task[:24])
    existing = read_json(root/'current.json',{})
    if existing and existing.get('taskId') != task:
        raise ValueError('任务索引指向不同的源商品或店铺，未覆盖')
    return root


def load_copy(root):
    pointer = read_json(root/'current.json') or {}
    revision = pointer.get('revision', '')
    if not re.fullmatch('[0-9a-f]{64}', revision):
        raise ValueError('没有找到原商品复制任务')
    folder = safe_join(root, 'revisions/' + revision[:24])
    frozen = read_json(folder/'source.json')
    if not frozen or frozen.get('sha256') != revision:
        raise ValueError('原商品快照与任务回执不一致')
    copy.plan_copy(frozen)  # Check the frozen data hash before following any pointers.
    if pair_key(frozen) != pointer.get('taskId') or pair_key(frozen)[:24] != root.name:
        raise ValueError('快照绑定的源店、目标店或商品与原任务不一致')
    return folder, frozen


def pair_key(frozen):
    stores = (frozen['sourceShop'], frozen['targetShop'])
    return digest([*[s[key] for s in stores for key in ('shopId','venderId')], frozen['sourceProductId']])


def require_target(frozen, args):
    if not args.target_config or not args.target_profile:
        raise ValueError('此步骤需要目标店配置和 Profile 以实时核店')
    target = verify(args.target_config, args.target_profile)
    if target != frozen['targetShop']:
        raise ValueError('实际目标店铺与这笔原任务不一致')
    return target


def local_target(frozen, args):
    target = frozen['targetShop']
    if args.target_profile and args.target_profile != target['profile']:
        raise ValueError('本地草稿归属于另一目标店 Profile')
    if args.target_config:
        config = read_json(args.target_config) or {}
        store = (config.get('stores') or {}).get(target['profile']) or config.get('store') or {}
        if any(str(store.get(k)) != target[k] for k in ('shopId','venderId')):
            raise ValueError('本地配置的店铺编号与该草稿归属不符')
    return target


def freeze(args):
    if args.product_id <= 0:
        raise ValueError('请选择一个确定的源商品')
    target = verify(args.target_config, args.target_profile)
    existing_id = args.existing_target_id
    if existing_id:
        if existing_id <= 0 or existing_id == args.product_id:
            raise ValueError('既有目标商品编号不能是源商品编号')
        read_owned_detail(existing_id)
        api.recheck_verified_jdo_store()
    source = verify(args.source_config, args.source_profile)
    detail, stocks, videos, gaps = read_product(args.product_id)
    source_components = read_components(source, detail)
    frozen = copy.freeze_copy(source, target, detail, stock_rows=stocks, video_rows=videos, source_components=source_components)
    task = pair_key(frozen)
    root = task_directory(task)
    with FileLock(root/'copy.lock'):
        task_directory(task)
        prior = publication_state(root,task)
        if prior['status'] != 'not-submitted':
            if existing_id and str(prior.get('targetProductId')) != str(existing_id):
                raise ValueError('本任务已有其他目标或未决提交，请先按原回执对账')
            return prior
        freight = capture_freight(api, detail, source_components, source)
        api.recheck_verified_jdo_store()
        plan = copy.plan_copy(frozen)
        folder = safe_join(root, 'revisions/' + frozen['sha256'][:24])
        existing = read_json(folder/'source.json')
        if existing is not None and existing != frozen:
            raise ValueError('原商品快照目录已经绑定了不同内容，未覆盖')
        atomic_json(folder/'source.json', frozen)
        atomic_json(folder/'source-delivery.json', freight)
        atomic_json(folder/'plan.json', plan)
        atomic_json(root/'current.json', {'taskId':task, 'revision':frozen['sha256'], 'sourceShop':source,
                                         'targetShop':target,'sourceProductId':frozen['sourceProductId'],
                                         'existingTargetId':existing_id})
        content = plan['content']
        first_sku = next(iter(content['skus']), None)
        return {'status':'local-plan','taskId':task, 'revision':frozen['sha256'], 'sourceShop':source,
                'targetShop':target, 'sourceProductId':str(args.product_id), 'skuCount':plan['skuCount'],
                'sourceExampleSkuUrl':'https://item.jd.com/'+first_sku+'.html' if first_sku else None,
                'title':content['title'],'packaging':content['packaging'],
                'gallerySlots':{gid:len(urls) for gid,urls in (content['mainGroups'] or {}).items()},
                'readGaps':gaps, 'planFile':str(folder/'plan.json'), 'sourceFreightEvidence':str(folder/'source-delivery.json'),
                'existingTargetProductId':existing_id,'platformWrites':0}


def empty_media(frozen):
    return {k:frozen[k] for k in ('sourceShop','targetShop','sourceProductId')} | {'images':[]}


def stage_media(folder, frozen, target, *, before_write=None):
    from PIL import Image
    from .sample_comparison import download_image
    from .sku_image_edit import media_space
    urls = copy.plan_copy(frozen)['imageUrls']
    manifest = empty_media(frozen)
    task_root = folder.parent.parent
    zone = None
    for url in urls:
        url_key = digest(url)[:24]
        local = safe_join(folder, 'media/'+url_key+'.img')
        origin_file = safe_join(folder, 'media/'+url_key+'.json')
        origin = read_json(origin_file)
        if origin is None:
            raw = download_image('https:' + url if url.startswith('//') else url)
            original_hash = sha256(raw).hexdigest()
            if local.exists() and file_sha(local) != original_hash:
                raise ValueError('中断前的原图与当前获取的字节不同，请保留原样核对')
            local.parent.mkdir(parents=True, exist_ok=True)
            pending = local.with_suffix('.tmp')
            pending.write_bytes(raw)
            pending.replace(local)
            atomic_json(origin_file, {'sourceUrl':url,'sha256':original_hash})
            origin = read_json(origin_file)
        content_hash = file_sha(local)
        if origin != {'sourceUrl':url,'sha256':content_hash}:
            raise ValueError('缓存原图与首次读取的来源或文件哈希不一致')
        path = safe_join(task_root, 'uploads/'+content_hash[:32]+'.json')
        upload = read_json(path)
        if upload is not None:
            if (upload.get('status') != 'accepted' or not upload.get('imgUrl') or
                    upload.get('sha256') != content_hash or upload.get('shopIdentity') != target):
                return {'status':'upload-needs-reconciliation','receipt':str(path),'replayed':False}
        else:
            with Image.open(local) as image:
                suffix = {'JPEG':'.jpg','PNG':'.png'}.get(image.format)
                image.verify()
            if not suffix:
                return {'status':'needs-media-format-review','sourceImage':str(local)}
            staged = safe_join(task_root, 'uploads/'+content_hash[:32]+suffix)
            staged.parent.mkdir(exist_ok=True, parents=True)
            staged.write_bytes(local.read_bytes())
            if file_sha(staged) != content_hash:
                raise ValueError('原图在上传准备期间已改变')
            if zone is None:
                zone = media_space(api)
            intent = {'status':'uploading','sha256':content_hash,'shopIdentity':target}
            if before_write:
                before_write()
            atomic_json(path, intent)
            response = api.upload(*zone, staged)
            if not response.get('imgUrl'):
                return {'status':'upload-needs-reconciliation','receipt':str(path),'replayed':False}
            upload = {**intent, 'status':'accepted','imgUrl':response['imgUrl']}
            atomic_json(path, upload)
        manifest['images'].append({'sourceUrl':url,'sourceFile':str(local),'sourceSha256':content_hash,
                                   'uploadReceipt':upload})
    atomic_json(folder/'media.json',manifest)
    return {'status':'media-ready','sourceUrls':len(urls),'uniqueFiles':len({i['sourceSha256'] for i in manifest['images']}),
            'receipt':str(folder/'media.json'), 'publishedProducts':0}


def merge_delivery(identity, capture, human):
    if not isinstance(human,dict) or (human and any(str((human.get('shop') or {}).get(k,'')) != identity[k]
                                                   for k in ('profile','shopId','venderId'))):
        raise ValueError('配送规则的原店铺身份不一致')
    result = {**human, **capture, 'shop':identity}
    for key in ('freightTemplates','promiseTemplates'):
        templates = {str(t['id']):t for t in human.get(key, [])}
        for t in capture.get(key, []):
            templates[str(t['id'])] = t
        result[key] = list(templates.values())
    return result


def check_freight_reads(previous, current, ids=None):
    for tid, old in previous.get('freightReads', {}).items():
        if ids is not None and tid not in ids:
            continue
        if old.get('status') == 'read':
            new = current.get('freightReads',{}).get(tid,{})
            if new.get('status') != 'read' or new.get('sha256') != old.get('sha256'):
                raise ValueError('原已核验的运费规则发生变化或暂时无法复查，请重新核对原任务')


def delivery_evidence(folder, frozen, detail, target, components, extra, *, source_capture=None):
    provided = read_json(extra) if extra else read_json(folder/'delivery-input.json',{})
    if not isinstance(provided, dict):
        raise ValueError('配送证据文件无法读取')
    result = {}
    for side, identity in (('source', frozen['sourceShop']), ('target', target)):
        human = provided.get(side, {})
        ids = [t['id'] for t in human.get('freightTemplates', [])]
        capture = ((source_capture if source_capture is not None else read_json(folder/'source-delivery.json',{})) if side == 'source' else
                   capture_freight(api, detail, components, target, template_ids=ids))
        # Keep the official capture and host-interpreted page rules together. Page input
        # cannot override shop identity or the platform's raw read receipts.
        result[side] = merge_delivery(identity, capture, human)
    return result


def prepare(folder, frozen, target, args):
    from .owned_product_copy import prepare_copy_schema
    if args.intent == 'local-draft':
        if args.delivery_evidence:
            supplied = read_json(args.delivery_evidence)
            if not isinstance(supplied,dict):
                raise ValueError('本地配送证据不是有效的原回执')
            atomic_json(folder/'delivery-input.json',supplied)
        draft = copy.local_copy_draft(frozen)
        path = folder/'local-draft.json'
        atomic_json(path,draft)
        from .shop_copy_preview import preview
        preview_file = folder/'商品草稿.md'
        pending = preview_file.with_suffix('.tmp')
        pending.write_text(preview(draft),encoding='utf-8')
        pending.replace(preview_file)
        return {'status':'local-draft-ready','draftFile':str(path),'previewFile':str(preview_file),'sourceProductId':frozen['sourceProductId'],
                'skuCount':len(draft['skuRows']),'missing':draft.get('missing',[]),
                'currentStage':draft['currentStage'],'merchantCodeHandling':draft['merchantCodeHandling'],
                'evidenceActions':draft.get('evidenceActions',[]),'targetShop':target,'platformWrites':0}
    source = frozen['sourceDetail']
    ref = reference(target, source)
    delivery = delivery_evidence(folder, frozen, {}, target, ref['schema']['componentList'], args.delivery_evidence)
    if args.delivery_evidence:
        atomic_json(folder/'delivery-input.json',read_json(args.delivery_evidence))
    atomic_json(folder/'delivery-evidence.json', delivery)
    prepared = prepare_copy_schema(frozen, target, ref, media_evidence=read_json(folder/'media.json'),
                                   delivery_evidence=delivery)
    atomic_json(folder/'prepared.json',prepared)
    if prepared['status'] != 'ready':
        return {'status':'needs-evidence','missing':prepared.get('missing',[]),'evidenceFile':str(folder/'prepared.json'),
                'platformWrites':0}
    schema = prepared['schema']
    cid = category(source)
    body = {'scene':'pop','schema':json.dumps(schema,ensure_ascii=False,separators=(',',':')),
            'productApplyDTO':{'categoryDTO':{'lastCategoryId':cid}}}
    approval = digest({'source':frozen['sha256'], 'target':target, 'body':body, 'delivery':delivery})
    atomic_json(folder/'pilot.json',{'approval':approval,'body':body,'intent':'publish-one','revision':frozen['sha256'],
                                    'deliveryDigest':digest(delivery),'target':target})
    return {'status':'pilot-prepared','approvalFingerprint':approval,'sourceProductId':frozen['sourceProductId'],
            'targetShop':target,'followUp':prepared.get('followUp',[]),'platformWrites':0}


def publication_state(root, task):
    from .shop_copy_batch import confirmed_refusal, publication_directory
    body_dir = publication_directory(root)
    result = read_json(body_dir/'result.json', {}) or {}
    data = result.get('data') if isinstance(result.get('data'),dict) else {}
    marker = body_dir/'publish-body.submit-state.json'
    saved = read_json(marker,{})
    for key in ('productId','applyId'):
        if data.get(key) and saved.get(key) and str(data[key]) != str(saved[key]):
            raise ValueError('原申请回执与提交记录中的商品或申请编号不一致')
    target_id = data.get('productId') or saved.get('productId')
    apply_id = data.get('applyId') or saved.get('applyId')
    status = ('submitted' if target_id or apply_id else
              'submission-needs-reconciliation' if marker.exists() else 'not-submitted')
    refusal=read_json(body_dir/'rejection.json',{})
    if refusal:
        if not marker.exists() or not refusal.get('payloadSha256') or refusal['payloadSha256']!=saved.get('payloadSha256') or target_id or apply_id:
            raise ValueError('平台拒绝回执与原申请记录不一致')
        if confirmed_refusal(refusal, saved, body_dir/'publish-body.json'):
            status='publication-rejected'
    pointer = read_json(root/'current.json',{})
    adopted=read_json(body_dir/'existing-after-rejection.json',{})
    if adopted:
        if (status!='publication-rejected' or adopted.get('taskId')!=task or adopted.get('targetShop')!=pointer.get('targetShop')
                or adopted.get('sourceProductId')!=pointer.get('sourceProductId')
                or adopted.get('originalBodySha256')!=refusal.get('payloadSha256') or not adopted.get('productId')):
            raise ValueError('只读认领记录与原拒绝或店铺不符')
        status,target_id='existing-target-for-comparison',adopted['productId']
    binding=read_json(body_dir/'target-binding.json')
    if binding:
        if (binding.get('kind')!='original-application' or binding.get('taskId')!=task or not apply_id or
                str(binding.get('applyId'))!=str(apply_id) or not saved.get('payloadSha256') or
                binding.get('payloadSha256')!=saved['payloadSha256'] or
                any(binding.get(k)!=pointer.get(k) for k in ('sourceShop','targetShop','sourceProductId')) or
                not re.fullmatch('[1-9][0-9]*',str(binding.get('productId',''))) or
                target_id and str(target_id)!=str(binding['productId'])):
            raise ValueError('原申请补回的目标编号与提交回执不一致')
        target_id=binding['productId']
        status='submitted'
    bound = pointer.get('existingTargetId')
    if bound is not None:
        if status != 'not-submitted' or pointer.get('taskId') != task or not str(bound).isdigit():
            raise ValueError('已有目标与任务提交记录不一致，请核对原样品')
        status, target_id = 'existing-target-for-comparison', bound
    return {'status':status,
            'taskId':task,'targetProductId':target_id,'applyId':apply_id,
            'followUp':read_json(body_dir/'follow-up.json',[]),
            'receiptDirectory':str(body_dir),'consumerPageStatus':'unchecked','replayed':False,
            **({'rejection':refusal} if refusal else {})}


def validation_passed(response):
    if not isinstance(response,dict) or response.get('success') is not True or not isinstance(response.get('data'),dict):
        return False
    data = response['data']
    for level in (response,data):
        if level.get('errorList') or ('code' in level and level['code'] not in (0,'0',200,'200','SUCCESS')):
            return False
        if any(k in level and level[k] is not True for k in ('valid','success','ok','passed','allowPublish')):
            return False
        if 'status' in level and str(level['status']).lower() not in ('ok','success'):
            return False
    if not data:
        return True  # Documented clean validation can have an empty data object.
    if set(data) - {'wareKeeperDetectResultList','success','valid','passed','allowPublish','ok','status','code','errorList','message'}:
        return False
    findings = data.get('wareKeeperDetectResultList')
    return isinstance(findings,list) and all(isinstance(row,dict) and str(row.get('detectResult'))=='0' for row in findings)


def _business_components(body):
    """Compare field content independent of JSON whitespace and generated application mapping IDs."""
    pieces={}
    def visit(node):
        if isinstance(node,list):
            for item in node:visit(item)
        elif isinstance(node,dict):
            code=node.get('componentCode')
            if isinstance(code,str):
                value=node.get('value')
                props={p['key']:p.get('value') for p in node.get('properties',[]) if p.get('key')
                       and p['key'] not in ('mappingId','imgRefId','colorId')}
                if props or not isinstance(value,list):
                    pieces.setdefault(code,[]).append({'properties':props,'value':value})
                if isinstance(value,list):visit(value)
    visit(json.loads(body['schema']))
    return pieces


def _retry_after_refusal(root,folder,frozen,target,task,prior,body,evidence_file):
    from .shop_copy_batch import publication_attempts
    history=publication_attempts(root)
    original=history[-1]
    refused=prior.get('rejection',{})
    old_body=read_json(original/'publish-body.json',{})
    old_sha=file_sha(original/'publish-body.json')
    original_scope=read_json(original/'request-scope.json',{})
    if (not refused.get('noIssuedIds') or old_sha!=refused.get('payloadSha256') or
            original_scope!={'taskId':task,'sourceShop':frozen['sourceShop'],'targetShop':target,
                             'sourceProductId':frozen['sourceProductId'],'revision':frozen['sha256'],'bodySha256':old_sha}):
        return None,{**prior,'nextAction':'原明确拒绝、无受理编号或请求归属证据不完整。'}
    proof=read_json(evidence_file) if evidence_file else {}
    if not isinstance(proof,dict) or proof.get('taskId')!=task:
        return None,prior
    if (proof.get('originalBodySha256')!=old_sha or proof.get('targetShop')!=target or not isinstance(proof.get('receipt'),str) or
            str(proof.get('platformCode')) not in refused.get('platformCodes',[]) or not isinstance(proof.get('fields'),list)):
        return None,{**prior,'nextAction':'需关联原失败代码、原请求 SHA、相同目标店及已修正字段的官方证据。'}
    receipt_path=safe_join(Path(evidence_file).resolve().parent,proof['receipt'])
    if not receipt_path.is_file() or not re.fullmatch('[a-f0-9]{64}',str(proof.get('receiptSha256',''))) or file_sha(receipt_path)!=proof['receiptSha256']:
        return None,{**prior,'nextAction':'现场修正证据缺失或摘要不符。'}
    before,after=_business_components(old_body),_business_components(body)
    changed=sorted(code for code in set(before)|set(after) if before.get(code)!=after.get(code))
    if any(_business_components(read_json(path/'publish-body.json'))==after for path in history):
        return None,{**prior,'nextAction':'这份业务请求已经发送过，保留全部原回执，不再提交。'}
    if not proof['fields'] or not all(isinstance(code,str) and code in changed for code in proof['fields']):
        return None,{**prior,'nextAction':'没有可核对的业务字段修正，原发布请求不会重放。'}
    if str(proof['platformCode']) in ('99904030007','99904030008'):
        grant=proof.get('permissionChange') or {}
        if (grant.get('action')!='POST.sp-product.v0.product-applies' or grant.get('shop')!=target or
                grant.get('from')!='denied' or grant.get('to')!='granted' or not grant.get('receipt') or
                not isinstance(grant.get('checkedAt'),int) or grant['checkedAt']<=refused.get('observedAt',0)):
            return None,{**prior,'nextAction':'原拒绝涉及权限，须核对该店该正式发品能力的新授权回执。'}
        permission_receipt=safe_join(Path(evidence_file).resolve().parent,grant['receipt'])
        if not permission_receipt.is_file() or file_sha(permission_receipt)!=grant.get('receiptSha256'):
            return None,{**prior,'nextAction':'目标店新权限回执缺失或凭证摘要不符，未提交。'}
    # Same lookup as the mature photo-listing confirm-not-created flow. Require a complete
    # catalogue response, and preserve ambiguous/possible matches for non-writing review.
    top={n['componentCode']:n.get('value') for n in json.loads(old_body['schema'])}
    item_num=str(top.get('WareItemnum') or '')
    title=str(top.get('WareTitle') or '')
    if not title:
        return None,{**prior,'nextAction':'原申请没有可独立查证的商品标题。'}
    rows=pipeline.list_products(require_success=True)
    matches={str(p['productId']) for p in rows if p.get('productName')==title or
             (item_num and str(p.get('itemNum') or '')==item_num)}
    if matches:
        if len(matches)==1:
            pid=next(iter(matches));detail=read_owned_detail(int(pid))
            info=detail['productInfo']
            rows_for_sku=copy.pair_frozen_target_skus(frozen,detail)
            n=len(frozen['sourceDetail']['skuList'])
            if (info.get('productName')==title and (not item_num or str(info.get('itemNum'))==item_num) and
                    len(rows_for_sku['pairs'])==len(detail.get('skuList') or [])==n and n):
                atomic_json(original/'existing-after-rejection.json',{'taskId':task,'sourceProductId':frozen['sourceProductId'],
                            'targetShop':target,'productId':pid,'originalBodySha256':old_sha,'checkedAt':int(time.time())})
                return None,publication_state(root,task)
        return None,{**prior,'candidateProductIds':sorted(matches),'nextAction':'已有候选目标，先核实映射。'}
    # Keep paths short even across several independent corrected refusals.
    name='correction' if len(history)==1 else 'r-'+digest([task,old_sha,after])[:16]
    retry=safe_join(history[0],name)
    correction={'taskId':task,'sourceShop':frozen['sourceShop'],'targetShop':target,'sourceProductId':frozen['sourceProductId'],
                'revision':frozen['sha256'],'previousBodySha256':old_sha,'platformCodes':refused['platformCodes'],
                'changedFields':changed,'correctionEvidenceSha256':digest(proof),'resolvedFields':proof['fields'],
                'absence':{'checkedAt':int(time.time()),'checkedCatalogProducts':len(rows),'itemNumber':item_num,
                           'title':title,'matchingProducts':[]}}
    return retry,correction


def submit_one(root, folder, frozen, target, args, *, before_write=None):
    prior = publication_state(root,args.task)
    retry=prior['status']=='publication-rejected' and getattr(args,'correction_evidence',None)
    if prior['status'] != 'not-submitted' and not retry:
        return prior
    original = folder/'pilot.json'
    plan = read_json(original) or {}
    if (read_json(folder/'prepared.json',{}).get('status') != 'ready' or plan.get('intent') != 'publish-one' or
            plan.get('revision') != frozen['sha256'] or plan.get('target') != target):
        raise ValueError('请先在当前版本准备并核对这一个目标商品')
    body = plan['body']
    delivery = read_json(folder/'delivery-evidence.json')
    if args.approval != plan.get('approval') or args.approval != digest(
            {'source':frozen['sha256'],'target':target,'body':body,'delivery':delivery}):
        raise ValueError('获准样品与原商品、目标店铺或草稿内容不符')
    # An unknown or already accepted write always wins, even if the caller refreshes
    # a snapshot or switches to another task folder for the same product/shops.
    source = verify(args.source_config,args.source_profile)
    if source != frozen['sourceShop']:
        raise ValueError('重新核对时源店铺已变化')
    detail, stocks, videos, _ = read_product(int(frozen['sourceProductId']))
    current_components = read_components(source, detail)
    current = copy.freeze_copy(source,target,detail,stock_rows=stocks,video_rows=videos,
                               source_components=current_components if 'sourceComponentCodes' in frozen else None)
    if copy.source_fidelity(current) != copy.source_fidelity(frozen):
        raise ValueError('原商品在打样后发生变化，请重新取原商品、核对差异')
    old_freight = delivery['source']
    current_freight = capture_freight(api,detail,current_components,source,
                                      template_ids=list(old_freight.get('freightReads', {})))
    check_freight_reads(old_freight, current_freight)
    target = require_target(frozen,args)
    ref = reference(target,detail)
    if copy.outside_creation_templates(frozen,ref) - copy.outside_creation_templates(current,ref):
        raise ValueError('原草稿略过的字段需要重新核对源类目，请按本商品当前模板更新草稿。')
    fresh_delivery = delivery_evidence(folder,frozen,{},target,ref['schema']['componentList'],None,
                                       source_capture=current_freight)
    check_freight_reads(delivery['target'], fresh_delivery['target'])
    from .owned_product_copy import prepare_copy_schema
    fresh = prepare_copy_schema(frozen,target,ref,media_evidence=read_json(folder/'media.json'),
                                delivery_evidence=fresh_delivery)
    if fresh['status'] != 'ready' or fresh['schema'] != json.loads(body['schema']):
        raise ValueError('目标模板、媒体或配送规则与已审草稿不一致，请重新准备样品')
    out = root/'publication'
    correction=None
    if retry:
        out,correction=_retry_after_refusal(root,folder,frozen,target,args.task,prior,body,args.correction_evidence)
        if out is None:
            return correction
    brand = next(c['value'] for c in json.loads(body['schema']) if c['componentCode']=='WareBrand')
    validation = {**body,'productApplyDTO':{'brandId':brand,'categoryDTO':body['productApplyDTO']['categoryDTO']}}
    if before_write:
        before_write()
    atomic_json(out/'validation-body.json',validation)
    with redirect_stdout(io.StringIO()):
        rc = api.command_validate_jd(argparse.Namespace(body=out/'validation-body.json',output=out/'validation-result.json'))
    response = read_json(out/'validation-result.json') or {}
    if rc or not validation_passed(response):
        return {'status':'validation-needs-review','receipt':str(out/'validation-result.json'),'publishedProducts':0}
    if before_write:
        before_write()
    atomic_json(out/'publish-body.json',body)
    payload_sha=file_sha(out/'publish-body.json')
    request_scope={'taskId':args.task,'sourceShop':frozen['sourceShop'],'targetShop':target,
                   'sourceProductId':frozen['sourceProductId'],'revision':frozen['sha256'],'bodySha256':payload_sha}
    atomic_json(out/'request-scope.json',request_scope)
    if correction:
        atomic_json(out/'correction.json',{**correction,'payloadSha256':payload_sha})
    atomic_json(out/'follow-up.json',fresh.get('followUp',[]))
    try:
        api.submit_product_once(out/'publish-body.json',out/'result.json')
    except (api.JdoError, OSError, RuntimeError) as exc:
        if not (out/'publish-body.submit-state.json').exists():
            raise
        codes=getattr(exc,'platform_codes',[])
        if (getattr(exc,'platform_call',None)==('POST','/rest/sp-product/v0/product-applies') and getattr(exc,'no_issued_ids',False) and codes and
                set(codes)<={'1010300506','10103001002','99904030007','99904030008'}):
            atomic_json(out/'rejection.json',{'status':'official-rejection','payloadSha256':file_sha(out/'publish-body.json'),
                        'platformCodes':codes,'noIssuedIds':True,'requestId':getattr(exc,'request_id',None),'observedAt':int(time.time())})
    return publication_state(root,args.task)


def application_observation(pid, apply_id=None, detail=None):
    """Read the seller's application and the platform's reason, separately from its live listing."""
    if not re.fullmatch('[1-9][0-9]*',str(pid)) or apply_id is not None and not re.fullmatch('[1-9][0-9]*',str(apply_id)):
        raise ValueError('审核诊断需要有效的目标商品编号，以及已知的原申请编号')
    lookup = apply_id or pid
    if detail is None:
        detail = matrix.read_apply(lookup)
    product = detail.get('productInfo') or {}
    info = detail.get('applyInfo') or {}
    store = api.require_verified_jdo_store()
    if any(k in row and str(row[k])!=str(store.get(k,'')) for row in (detail,product,info) for k in ('shopId','venderId')):
        raise ValueError('原申请中的店铺标识与当前目标店不符')
    if str(product.get('productId')) != str(pid) or (
            apply_id and info.get('applyId') and str(info['applyId']) != str(apply_id)):
        raise ValueError('审核回执没有对应到本次目标商品和申请')
    status = {k:info[k] for k in ('applyId','draftComputeStatus','applyStatus','keeperStatus') if k in info}
    status['lookupId'] = str(lookup)
    code = str(info.get('draftComputeStatus'))
    state = {'1':'pending','3':'rejected'}.get(code,'unknown')
    result = {'auditState':state,'applicationStatus':status}
    if state=='rejected':
        result['rejection'] = pipeline.product_apply_rejection(int(pid),apply_id=apply_id,detail=detail) or {}
        reason = result['rejection']
        if 'latestProductReview' in reason:
            result['latestProductReview']=reason.pop('latestProductReview')
        result['nextAction'] = ('按平台原文定位本商品的素材或类目，核对原申请后处理。'
                                if any(reason.get(k) for k in ('auditReason','keeperReason','pushReason'))
                                else '该申请已被驳回；当前读取未取得具体原因，请从已授权店铺的审核记录继续查。')
    return result


def compare(root, folder, frozen, target, args):
    submitted = publication_state(root,args.task)
    pid = str(args.product_id or submitted.get('targetProductId') or '')
    if not pid.isdecimal() or int(pid) <= 0:
        return {**submitted,'nextAction':'先按原商品货号、申请和原提交回执查询目标商品编号。'}
    if submitted.get('targetProductId') and str(submitted['targetProductId']) != pid:
        raise ValueError('目标商品与本任务提交回执不符')
    if args.apply_id:
        if submitted.get('applyId') and str(submitted['applyId']) != str(args.apply_id):
            raise ValueError('指定申请编号与原样品的回执不一致')
        detail = matrix.read_apply(args.apply_id)
        if str((detail.get('productInfo') or {}).get('productId') or '') != pid:
            raise ValueError('申请单回读尚未证明属于所选商品')
        stocks, videos, gaps = None, None, ['application-readback-only']
    else:
        detail, stocks, videos, gaps = read_product(int(pid))
    try:
        ref = reference(target,detail,for_creation=False)
    except (api.JdoError,ValueError,KeyError,TypeError):
        ref = None
    components = ref['schema']['componentList'] if ref else read_components(target,detail)
    delivery = delivery_evidence(folder, frozen, detail, target, components, args.delivery_evidence)
    report = copy.compare_copy(frozen,target,detail,stock_rows=stocks,video_rows=videos,
                               media_evidence=read_json(folder/'media.json'),delivery_evidence=delivery,target_schema=ref)
    original = read_json(folder/'delivery-evidence.json',{})
    if original and not args.delivery_evidence:
        try:
            check_freight_reads(original['target'],delivery['target'])
        except ValueError as error:
            report['delivery']['checks'].append({'field':'WareTransport.readback','status':'needs-evidence',
                                                 'nextAction':str(error)})
            report['delivery']['status']='needs-evidence'
            if report['status']=='verified':
                report['status']='needs-evidence'
    info = detail['productInfo']
    source_info = frozen['sourceDetail']['productInfo']
    report.update(sourceProductId=frozen['sourceProductId'],targetProductId=pid,
                  sourcePlatformStatus={k:source_info[k] for k in ('productStatus','productStatusNew') if k in source_info},
                  platformStatus={k:info[k] for k in ('productStatus','productStatusNew') if k in info},
                  readbackView='application' if args.apply_id else 'product',
                  consumerPageStatus='unchecked',buyerPurchaseStatus='unverified',readGaps=gaps)
    report['skuLinks'] = [{'sourceSkuId':s,'targetSkuId':t,'sourceUrl':'https://item.jd.com/'+s+'.html',
                           'targetUrl':'https://item.jd.com/'+t+'.html'} for s,t in report['skuPairs'].items()]
    if report['status']=='verified':
        report['status']='compared-fields-match'
    report['readbackScope'] = 'single-product'
    report['checkedAt'] = int(time.time())
    report['applicationStatus'] = {k:v for k,v in (detail.get('applyInfo') or {}).items()
                                   if k in ('applyId','draftComputeStatus','applyStatus')}
    status_data = info.get('productStatus')
    live_code = info.get('productStatusNew')
    if live_code is None:
        live_code = status_data.get('productStatusNew') if isinstance(status_data,dict) else status_data
    report['saleState'] = ('unknown' if args.apply_id else 'on-sale' if str(live_code) in ('1','103') else
                           'not-on-sale' if str(live_code) in ('0','2','8','104','105') else 'unknown')
    audit_code=str(report['applicationStatus'].get('draftComputeStatus'))
    report['auditState']='pending' if audit_code=='1' else 'rejected' if audit_code=='3' else 'unknown'
    original_id = submitted.get('applyId')
    if (not args.apply_id and original_id and report['auditState'] in ('pending','rejected')
            and str(report['applicationStatus'].get('applyId')) != str(original_id)):
        # The live product can expose a different, newer application. Look up the original receipt.
        report.update(auditState='unknown',applicationStatus={'lookupId':str(original_id)})
        try:
            report.update(application_observation(pid,original_id))
        except (api.JdoError,matrix.MatrixError,KeyError,ValueError):
            pass
    elif args.apply_id or report['auditState']=='rejected':
        report.update(application_observation(pid,args.apply_id or original_id,detail))
    atomic_json(folder/'comparison.json',report)
    checks = [*report['checks'], *report['delivery'].get('checks',[])]
    counts = {state:sum(c.get('status') == state for c in checks)
              for state in ('verified','different','needs-evidence')}
    mismatches = []
    for c in checks:
        if c.get('status') == 'different':
            mismatches.append({'field':c['field'],'source':str(c.get('source',c.get('sourceValue')))[:120],
                               'target':str(c.get('target',c.get('targetValue')))[:120]})
    return {k:report[k] for k in ('status','sourceProductId','targetProductId','sourcePlatformStatus','platformStatus','readbackView',
                                  'consumerPageStatus','buyerPurchaseStatus','packagingListStatus','readbackScope','applicationStatus','saleState','auditState','checkedAt','readGaps')} | {
            **{k:report[k] for k in ('latestProductReview',) if k in report},
            'rejection':report.get('rejection'),'buyerEvidence':'本次核对的是后台商品和库存。买家在特定地区、规格或预售条件下能否下单，需对应前台证据。',
            'merchantCodeCoverage':report.get('merchantCodeCoverage'), 'matchedSkuCount':len(report['skuPairs']),
            'checkCounts':counts,'skuLinkCount':len(report['skuLinks']),'skuLinks':report['skuLinks'][:3],
            'unresolvedFields':[c['field'] for c in checks if c.get('status')!='verified'][:12],
            'firstDifferences':mismatches[:4],
            'reportFile':str(folder/'comparison.json')}


def main(argv=None):
    from .shop_copy_router import main as route
    return route(argv)
