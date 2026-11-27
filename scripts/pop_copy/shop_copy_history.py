"""Reconcile historical migration receipts with the current target catalogue. Read-only."""
import json
import re
from collections import defaultdict
from pathlib import Path
import time

from .safety import SafetyError, atomic_json, file_sha, is_linked, read_json, safe_join
from .state import merchant_home


def register(subs):
    p=subs.add_parser('history-census',help='对照旧搬家工作目录、实际在售商品与指定目标链接')
    p.add_argument('--history-dir',required=True,type=Path)
    p.add_argument('--target-config',required=True,type=Path)
    p.add_argument('--target-profile',required=True)
    p.add_argument('--target-product-id',action='append',default=[])


def _state_files(directory):
    # Batch runs may share one state directory or have one per source product.
    for pattern in ('state/*.json', 'batch*/state/*.json', 'batch*/*/state/*.json', '[0-9]*/state/*.json'):
        for path in sorted(directory.glob(pattern)):
            parts = path.relative_to(directory).parts
            if parts[0].startswith('batch') and not re.fullmatch(r'batch[0-9]*', parts[0]):
                continue
            member = path.parent.parent.name
            yield path, member if re.fullmatch('[1-9][0-9]*', member) else None


def _verification(record, member_root):
    value = record.get('verification')
    if not isinstance(value, str):
        return (value if isinstance(value, dict) else {}), None
    path = Path(value)
    if path.name != 'verification.json':
        raise ValueError('unexpected verification receipt name')
    if path.is_absolute():
        path = path.relative_to(member_root.absolute())
    elif path.absolute().is_relative_to(member_root.absolute()):
        path = path.absolute().relative_to(member_root.absolute())
    path = safe_join(member_root, path.as_posix())
    report = read_json(path)
    if not isinstance(report, dict):
        raise ValueError('missing verification receipt')
    return report, path


def history_records(directory,target):
    if not directory.is_dir() or is_linked(directory):
        raise ValueError('请选择原搬家工作目录')
    candidates=defaultdict(lambda:defaultdict(list))
    skipped=[]
    for path,member in _state_files(directory):
        relative=path.relative_to(directory).as_posix()
        try:
            d=read_json(safe_join(directory,relative))
            store=d.get('store') or {}
            if any(str(store.get(k,''))!=str(target[k]) for k in ('profile','shopId','venderId')):
                skipped.append({'file':relative,'reason':'other-or-unverified-shop'})
                continue
            sid=str(d.get('sourceSpu') or '')
            source_sku=str(d.get('sourceSku') or '')
            if (not re.fullmatch('[1-9][0-9]*',sid) or member and member not in (sid,source_sku)
                    or not member and path.stem not in (sid,source_sku)):
                skipped.append({'file':relative,'reason':'unconfirmed-source-product'})
                continue
            published = str(d.get('productId') or '')
            if re.fullmatch('[1-9][0-9]*', published):
                role = 'existingProduct' if str((d.get('existingProduct') or {}).get('productId')) == published else 'publication'
                candidates[published][sid].append({'field':role,'file':relative,'applyId':d.get('applyId')})
            try:
                verification, receipt = _verification(d, path.parent.parent)
            except (SafetyError, ValueError, OSError):
                skipped.append({'file':relative,'reason':'unreadable-verification'})
                continue
            verified = str(verification.get('productId') or '')
            if re.fullmatch('[1-9][0-9]*', verified):
                candidates[verified][sid].append({'field':'verification',
                    'file':receipt.relative_to(directory.absolute()).as_posix() if receipt else relative,
                    'applyId':verification.get('applyId')})
        except (SafetyError,ValueError,TypeError,AttributeError,OSError):
            skipped.append({'file':relative,'reason':'unreadable-receipt'})
    return candidates,skipped


def run(args,entry):
    target=entry.verify(args.target_config,args.target_profile)
    candidates,skipped=history_records(args.history_dir,target)
    products=entry.pipeline.list_products(require_success=True)
    entry.api.recheck_verified_jdo_store()
    current={str(p['productId']):p for p in products}
    requested=list(map(str,args.target_product_id))
    if any(not re.fullmatch('[1-9][0-9]*',pid) for pid in requested):
        raise ValueError('目标商品编号无效')
    ids=list(dict.fromkeys(requested)) if requested else sorted(set(current)|set(candidates))
    by_source=defaultdict(set)
    for tid,origins in candidates.items():
        for sid in origins:by_source[sid].add(tid)
    rows=[]
    by_name=defaultdict(list)
    for tid,product in current.items():
        if product.get('productName') and tid in candidates:
            by_name[product['productName']].append(tid)
    for tid in ids:
        row=current.get(tid)
        links=candidates.get(tid,{})
        sibling_hints=[]
        if row and not links:
            for other in by_name.get(row.get('productName'),[]):
                sibling=current[other]
                if row.get('categoryId') and sibling.get('categoryId') and str(row['categoryId'])!=str(sibling['categoryId']):
                    continue
                sibling_hints.append({'targetProductId':other,'sourceProductIds':sorted(candidates[other]),
                                      'evidence':'same-catalogue-title','requiresIndependentProductAndSkuCheck':True})
        rows.append({'targetProductId':tid,'productName':row.get('productName') if row else None,
                     'liveCatalogueStatus':row.get('productStatus') if row else None,
                     'foundInProductCatalogue':row is not None,'sourceCandidates':dict(links),
                     'unverifiedSiblingHints':sibling_hints,
                     'otherTargetsForSameSource':{sid:sorted(by_source[sid]-{tid}) for sid in links if len(by_source[sid])>1}})
    out={'status':'history-compared','targetShop':target,'requestedRowCount':len(requested),'uniqueTargetCount':len(ids),
         'checkedAt':int(time.time()),'rows':rows,'unusableReceipts':skipped,'platformWrites':0,
         'scopeNote':'创建回执与回读可能指向不同目标。此表供追溯。素材写入另行核实两店真实商品及规格。'}
    dest=safe_join(merchant_home(),'work/shop-copy-catalogues/'+entry.digest(out)+'.json')
    atomic_json(dest,out)
    return {'ok':True,**out,'reportFile':str(dest)}


def _member_artifact(member, value):
    path=Path(value)
    if path.is_absolute():
        path=path.relative_to(member)
    return safe_join(member,path.as_posix())


def _accepted_publication(root, state, product_id, title, category, item_number):
    if str(state.get('productId'))!=product_id:
        return False
    result=read_json(safe_join(root,'publish-response.json'),{})
    native=result.get('data') or {}
    if (result.get('success') is not True or result.get('errorList') or str(native.get('productId'))!=product_id
            or not native.get('applyId') or native.get('errorList') or native.get('success') is False
            or any(str(level['code']) not in ('0','200','SUCCESS') for level in (result,native) if 'code' in level)
            or str(native['applyId'])!=str(state.get('applyId'))):
        return False
    body_file=safe_join(root,'publish-body.json')
    if not body_file.is_file() or file_sha(body_file)!=state.get('payloadSha256'):
        return False
    body=read_json(body_file)
    schema=body['schema']
    components=json.loads(schema) if isinstance(schema,str) else schema
    values={row['componentCode']:row.get('value') for row in components}
    return (len(values)==len(components) and values.get('WareTitle')==title
            and str(values.get('WareItemnum'))==item_number
            and str(body['productApplyDTO']['categoryDTO']['lastCategoryId'])==category)


def material_origins(directory, frozen, product_id):
    """Load direct original receipts scoped by both SPUs and the source's authenticated SKU set."""
    if not directory.is_dir() or is_linked(directory):
        raise ValueError('请选择原搬家工作目录')
    directory=directory.absolute()
    source_id=frozen['sourceProductId']
    source_skus={str(row['skuId']) for row in frozen['sourceDetail']['skuList']}
    scope_ids=source_skus|{source_id}
    origins=[]
    for path,member_id in _state_files(directory):
        if path.stem not in scope_ids or member_id and member_id not in scope_ids:
            continue
        relative=path.relative_to(directory).as_posix()
        try:
            rec=read_json(safe_join(directory,relative))
            if (str(rec.get('sourceSpu'))!=source_id or str(rec.get('sourceSku')) not in source_skus
                    or any(str(rec['store'].get(k))!=frozen['targetShop'][k] for k in ('shopId','venderId','profile'))):
                continue
            member=path.parent.parent
            folder=_member_artifact(member,rec['folder'])
            listing=read_json(safe_join(folder,'listing.json'))
            source=listing['source']
            manifest=read_json(safe_join(folder,'manifest.json'))
            if (str(source.get('spu'))!=source_id or str(manifest.get('sourceSpu'))!=source_id
                    or str(source.get('sku'))!=str(rec['sourceSku']) or str(manifest.get('sourceSku'))!=str(rec['sourceSku'])
                    or manifest.get('sourceVenderId') and str(manifest['sourceVenderId'])!=frozen['sourceShop']['venderId']):
                continue
            for rows in (listing['skus'],manifest['variants']):
                if len(rows)!=len(source_skus) or {str(row['sourceSku']) for row in rows}!=source_skus:
                    raise ValueError('历史原商品 SKU 清单与当前不符')
            archive=safe_join(folder,'.pipeline/publish')
            title=listing['product']['title']
            item_number=str(listing['product']['itemNumber'])
            category=str(listing['category']['id'])
            facts={'stateFile':relative,'title':title,'category':category,'itemNumber':item_number}
            if _accepted_publication(archive,rec,product_id,title,category,item_number):
                origins.append({**facts,'basis':'legacy-accepted-publication',
                                'receiptFile':(archive/'publish-response.json').relative_to(directory).as_posix()})
                continue
            file=safe_join(archive,'verification.json')
            if isinstance(rec.get('verification'),str) and _member_artifact(member,rec['verification'])!=file:
                continue
            report,_=_verification(rec,member)
            if (str(report.get('productId'))==product_id and type(report.get('expectedSkuCount')) is int
                    and report==read_json(file) and report.get('productName')==title and report.get('verifiedAt')
                    and type(report.get('ok')) is bool and report.get('expectedSkuCount')==len(source_skus)
                    and type(report.get('actualSkuCount')) is int and report['actualSkuCount']>0
                    and type(report.get('productStatus')) is int and report['productStatus']>0):
                origins.append({**facts,'basis':'legacy-product-readback','readbackPassed':report['ok'],
                                'targetSkuCount':report['actualSkuCount'],'receiptFile':file.relative_to(directory).as_posix()})
        except (SafetyError,ValueError,KeyError,TypeError,AttributeError,OSError):
            continue
    return origins
