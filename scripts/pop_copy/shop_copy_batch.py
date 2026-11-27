"""Frozen scope and resumable coordination of the normal single-product copy operations."""
import argparse
from collections import Counter, defaultdict
import re
import time
import uuid
from pathlib import Path

from .safety import atomic_json, file_sha, read_json, safe_join, redact
from .state import FileLock, merchant_home


ACTIONS = ('batch-plan','batch-run','batch-resume','batch-stop','batch-status','batch-skip','batch-census','batch-materials')
WRITE_CONFIRM = {'pilot':'PUBLISH_COPY_PILOT','bulk':'PUBLISH_FROZEN_SCOPE'}


def register(subs):
    for name in ACTIONS:
        p = subs.add_parser(name)
        if name != 'batch-plan':
            p.add_argument('--batch',required=True)
        if name in ('batch-plan','batch-run','batch-resume','batch-materials'):
            for side in ('source','target'):
                p.add_argument('--'+side+'-config',type=Path,required=True)
                p.add_argument('--'+side+'-profile',required=True)
        if name == 'batch-plan':
            p.add_argument('--inventory',type=Path,required=True)
            p.add_argument('--product-id',action='append',default=[])
            p.add_argument('--pilot-id')
            p.add_argument('--existing-map',type=Path,help='已知来源与目标商品编号的一对一对照 JSON；仅用于核对已有商品')
        if name in ('batch-run','batch-resume'):
            p.add_argument('--delivery-evidence',type=Path)
            p.add_argument('--correction-evidence',type=Path,help='只用于原明确拒绝的同一商品；宿主的现场修正证据')
            p.add_argument('--authorization-ref',help='商家已表达的范围授权或恢复指令的本地引用')
        if name == 'batch-run':
            p.add_argument('--phase',choices=('draft','pilot','bulk'),required=True)
            p.add_argument('--confirm',choices=tuple(WRITE_CONFIRM.values()))
        if name == 'batch-resume':
            p.add_argument('--stop-id',help='商家已要求继续时，指明要解除的原停止回执')
        if name == 'batch-stop':
            p.add_argument('--reason',default='商家要求停止')
        if name=='batch-skip':
            p.add_argument('--source-product-id',action='append',required=True,help='商家放弃的源商品，可重复')
            p.add_argument('--reason',default='商家要求跳过')
        if name=='batch-census':
            p.add_argument('--target-config',type=Path,required=True)
            p.add_argument('--target-profile',required=True)
        if name=='batch-materials':
            p.add_argument('--health',action='store_true')
            p.add_argument('--confirm',choices=['FILL_MISSING_COPY_MATERIALS'])
            p.add_argument('--source-product-id',action='append',default=[])
        if name=='batch-status':
            p.add_argument('--output',type=Path,help='保存当前批次原始报告，供后续主图任务读取')


def _base():
    return safe_join(merchant_home(),'work/shop-copy-batches')


def directory(batch):
    if not isinstance(batch,str) or not re.fullmatch('[0-9a-f]{64}',batch):
        raise ValueError('批次编号不完整')
    return safe_join(_base(),batch[:24])


def _scope(root, batch, api):
    scope = read_json(safe_join(root,'scope.json'))
    if not isinstance(scope,dict) or api.digest(scope)!=batch:
        raise ValueError('原批次不存在或冻结范围已经改变')
    return scope


def discover(state, shop_id, vender_id, task_id=None):
    from .owned_product_copy import _digest
    for path in safe_join(Path(state),'work/shop-copy-batches').glob('*'):
        if not re.fullmatch('[0-9a-f]{24}',path.name):
            continue
        original=safe_join(path,'scope.json')
        scope=read_json(original)
        if not isinstance(scope,dict):
            continue
        batch=_digest(scope)
        target=scope.get('targetShop',{})
        if (batch[:24]!=path.name or task_id and task_id!=batch or
                str(target.get('shopId',''))!=shop_id or str(target.get('venderId',''))!=vender_id):
            continue
        phase=read_json(path/'phase.json',{}).get('phase')
        stop=read_json(path/'stop.json',{}).get('active')
        hold=read_json(path/'hold.json',{}).get('active')
        yield {'kind':'shop-copy-batch','taskId':batch,'taskDirectory':str(path),'shopIdentity':target,
               'sourceShop':scope['sourceShop'],'sourceProductIds':scope['sourceProductIds'],'originalScope':str(original),
               'phase':phase,'state':'stopped' if stop else 'held' if hold else 'recoverable' if phase else 'planned',
               'updatedAt':max(p.stat().st_mtime for p in (original,path/'phase.json',path/'hold.json',path/'stop.json') if p.is_file()),
               'nextAction':'先运行 shop-copy batch-status 读取逐项回执；继续原批次沿已记录阶段和授权。'}


def _stores(args, api):
    target = api.verify(args.target_config,args.target_profile)
    source = api.verify(args.source_config,args.source_profile)
    if source['shopId']==target['shopId'] or source['profile']==target['profile']:
        raise ValueError('批量复制需要两家不同的已核验店铺')
    return source,target


def _plan(args, api):
    source,target = _stores(args,api)
    record = read_json(args.inventory)
    if not isinstance(record,dict) or (record.get('sourceShop'),record.get('targetShop'))!=(source,target):
        raise ValueError('盘点回执的源店或目标店与当前身份不符')
    location = safe_join(merchant_home(),'work/shop-copy-inventories/'+api.digest(record)[:32]+'.json')
    if location.resolve()!=args.inventory.resolve():
        raise ValueError('请使用本安装的原始 inventoryFile')
    if record.get('status')!='inventory-ready' and not args.product_id:
        raise ValueError('盘点尚未覆盖完整范围；重新只读盘点，或按实际交办列出本次商品')
    all_ids = [str(r['productId']) for r in record['products']]
    ids = [str(n) for n in args.product_id] or all_ids
    if (not ids or len(set(ids))!=len(ids) or any(not re.fullmatch('[1-9][0-9]*',pid) for pid in ids)
            or not set(ids)<=set(all_ids)):
        raise ValueError('批次范围须为原盘点中的非重复商品编号')
    if args.pilot_id and args.pilot_id not in ids:
        raise ValueError('所选样品不在冻结商品范围内')
    bindings = read_json(args.existing_map) if args.existing_map else {}
    if not isinstance(bindings,dict) or not set(bindings)<=set(ids):
        raise ValueError('既有目标对照含计划外来源')
    bindings = {s:str(t) for s,t in bindings.items()}
    if len(set(bindings.values()))!=len(bindings) or any(s==t or not re.fullmatch('[1-9][0-9]*',t) for s,t in bindings.items()):
        raise ValueError('源目标对照需要唯一的目标商品编号')
    if bindings:
        api.verify(args.target_config,args.target_profile)
        for tid in bindings.values():
            api.read_owned_detail(int(tid))
        api.api.recheck_verified_jdo_store()
    scope = {'sourceShop':source,'targetShop':target,'sourceProductIds':ids,'pilotId':args.pilot_id or ids[0],
             'existingTargets':bindings,'inventorySha256':api.digest(record),'sourceStatuses':record['selectedStatuses']}
    batch = api.digest(scope)
    root = directory(batch)
    with FileLock(safe_join(root,'scope.lock')):
        old = read_json(root/'scope.json')
        if old is not None and old!=scope:
            raise ValueError('批次目录已有另一份冻结范围')
        if old is None:
            atomic_json(root/'scope.json',scope)
    return {'status':'batch-planned','batchId':batch,'scopeFile':str(root/'scope.json'),'scopeSize':len(ids),
            'pilotId':scope['pilotId'],'sourceShop':source,'targetShop':target,'platformWrites':0,'ok':True}


class BatchStopped(Exception):
    pass


class MemberSkipped(Exception):
    pass


def _check_stop(root, source_id=None):
    record = read_json(root/'stop.json',{})
    if record.get('active'):
        raise BatchStopped(record['stopId'])
    if source_id and source_id in read_json(root/'excluded-sources.json',{}):
        raise MemberSkipped(source_id)


def _set_stop(root, args):
    with FileLock(safe_join(root,'control.lock')):
        note = {'active':True,'stopId':uuid.uuid4().hex,'reason':redact(args.reason),
                'batchId':args.batch,'requestedAt':int(time.time())}
        atomic_json(root/'stop.json',note)
    return {'status':'stop-requested','ok':True,**note,
            'nextAction':'在途操作先等原回执；后续写入在下一次提交边界停止。'}


def _authorize(root, args, scope):
    with FileLock(safe_join(root,'control.lock')):
        stop = read_json(root/'stop.json',{})
        if stop.get('active'):
            if (args.action!='batch-resume' or args.stop_id!=stop['stopId'] or
                    not (args.authorization_ref or '').strip()):
                raise BatchStopped(stop['stopId'])
            atomic_json(root/'stop.json',{**stop,'active':False,'resumedAt':int(time.time()),
                        'resumeReference':args.authorization_ref.strip()})
        previous=read_json(root/'phase.json',{})
        if args.action=='batch-resume':
            phase=previous.get('phase')
            if phase not in ('draft','pilot','bulk') or previous.get('batchId')!=args.batch:
                raise ValueError('该批次尚无可恢复的阶段')
        else:
            phase=args.phase
        if phase in WRITE_CONFIRM:
            file=root/(phase+'-grant.json')
            grant=read_json(file,{})
            if args.action=='batch-run':
                if args.confirm!=WRITE_CONFIRM[phase] or not (args.authorization_ref or '').strip():
                    raise ValueError('需要本次商家已批准的商品范围和实际交办引用，再执行对应阶段')
                grant={'batchId':args.batch,'phase':phase,'sourceProductIds':scope['sourceProductIds'] if phase=='bulk' else [scope['pilotId']],
                       'authorizationRef':args.authorization_ref.strip(),'recordedAt':int(time.time())}
                atomic_json(file,grant)
            if (grant.get('batchId')!=args.batch or grant.get('phase')!=phase or not grant.get('authorizationRef') or
                    grant.get('sourceProductIds')!=(scope['sourceProductIds'] if phase=='bulk' else [scope['pilotId']])):
                raise ValueError('未找到与原批次、阶段和商品范围一致的授权')
        if args.action=='batch-run':
            atomic_json(root/'phase.json',{'batchId':args.batch,'phase':phase})
        return phase


def _record_path(root, pid):
    return safe_join(root,'items/'+pid+'.json')


def confirmed_refusal(record, marker, body):
    codes = record.get('platformCodes')
    return (record.get('status') == 'official-rejection' and record.get('noIssuedIds') is True
            and isinstance(codes, list) and bool(codes)
            and all(c in ('1010300506','10103001002','99904030007','99904030008') for c in codes)
            and not marker.get('productId') and not marker.get('applyId')
            and marker.get('payloadSha256') and body.is_file()
            and record.get('payloadSha256') == marker['payloadSha256'] == file_sha(body))


def publication_attempts(root):
    """A correction joins the history only after saving its own native dispatch intent."""
    base=safe_join(root,'publication')
    paths=[safe_join(base,'correction')]
    paths.extend(safe_join(base,p.name) for p in base.glob('r-*') if re.fullmatch('r-[a-f0-9]{16}',p.name))
    children={}
    for path in paths:
        marker=read_json(safe_join(path,'publish-body.submit-state.json'))
        if marker is None:continue
        request=read_json(path/'correction.json',{})
        parent_sha=request.get('previousBodySha256')
        if not re.fullmatch('[a-f0-9]{64}',str(parent_sha)) or parent_sha in children:
            raise ValueError('修正申请的父请求摘要缺失或已有两个不同后续回执')
        children[parent_sha]=(path,marker,request)
    attempts=[base]
    if not children:return attempts
    scope=read_json(root/'current.json',{})
    seen={file_sha(base/'publish-body.json')}
    while children:
        parent=attempts[-1]
        original=read_json(parent/'rejection.json',{})
        parent_marker=read_json(parent/'publish-body.submit-state.json',{})
        parent_sha=original.get('payloadSha256')
        if (not confirmed_refusal(original, parent_marker, parent/'publish-body.json') or parent_sha not in seen or parent_sha not in children
                or parent_sha!=parent_marker.get('payloadSha256') or parent_marker.get('productId') or parent_marker.get('applyId')):
            raise ValueError('缺少与下一次尝试对应的原平台明确拒绝，保留所有原申请')
        retry,marker,request=children.pop(parent_sha)
        sha=marker.get('payloadSha256')
        if (not sha or sha in seen or request.get('payloadSha256')!=sha
                or any(request.get(k)!=scope.get(k) for k in ('taskId','sourceShop','targetShop','sourceProductId','revision'))
                or file_sha(retry/'publish-body.json')!=sha):
            raise ValueError('修正请求与店铺、商品或历史原请求摘要不一致')
        seen.add(sha)
        attempts.append(retry)
    return attempts


def publication_directory(root):
    return publication_attempts(root)[-1]


def _report(root, batch, scope):
    from .owned_product_copy import _digest
    rows=[read_json(_record_path(root,pid),{'sourceProductId':pid,'status':'not-started'}) for pid in scope['sourceProductIds']]
    excluded=read_json(root/'excluded-sources.json',{})
    for row in rows:
        row['skippedByMerchant']=row['sourceProductId'] in excluded
    # A pilot can predate this batch. Use the original frozen plan's count where
    # available, even when this batch has not yet touched that member.
    for row in rows:
        if type(row.get('sourceSkuCount')) is int:continue
        pid=row['sourceProductId']
        task=_digest([*[s[k] for s in (scope['sourceShop'],scope['targetShop']) for k in ('shopId','venderId')],pid])
        single=safe_join(merchant_home(),'work/shop-copies/'+task[:24])
        ptr=read_json(single/'current.json',{})
        revision=ptr.get('revision','')
        if ptr.get('taskId')==task and ptr.get('sourceShop')==scope['sourceShop'] and ptr.get('targetShop')==scope['targetShop'] and re.fullmatch('[a-f0-9]{64}',revision):
            plan=read_json(safe_join(single,'revisions/'+revision[:24]+'/plan.json'),{})
            if plan.get('sourceFingerprint')==revision and plan.get('sourceProductId')==pid and type(plan.get('skuCount')) is int:
                row['sourceSkuCount']=plan['skuCount']
    counts={}
    for row in rows:
        counts[row['status']]=counts.get(row['status'],0)+1
    assignments=Counter(str(row['targetProductId']) for row in rows if row.get('targetProductId'))
    conflicts={str(row['targetProductId']) for row in rows if row.get('targetProductId') and row['status']=='mapping-conflict'}
    unique={tid for tid,count in assignments.items() if count==1}-conflicts
    reports=[row['comparison'] for row in rows if row.get('comparison') and str(row.get('targetProductId')) in unique]
    sale_counts=Counter(r.get('saleState') for r in reports)
    audit_counts=Counter(r.get('auditState') for r in reports)
    evidence=[r['merchantCodeCoverage'] for r in reports if r.get('merchantCodeCoverage')]
    known=[r['sourceSkuCount'] for r in rows if type(r.get('sourceSkuCount')) is int]
    code_counts={k:sum(e.get(k,0) for e in evidence) for k in ('independentlyPaired','verified','different')}
    code_counts.update(sourceSkuCount=sum(known),productsWithEvidence=len(evidence),sourceProductsWithUnknownSkuCount=len(rows)-len(known),
                       unverified=sum(known)-code_counts['verified']-code_counts['different'])
    return {'status':'batch-status','batchId':batch,'ok':True,'sourceShop':scope['sourceShop'],'targetShop':scope['targetShop'],
            'pilotId':scope['pilotId'],'scopeSize':len(rows),'countsByStatus':counts,'rows':rows,
            'excludedSourceProductIds':list(excluded),
            'uniqueMappedTargets':len(unique),
            'productsReadBack':sum(r.get('readbackView')=='product' for r in reports),
            'onSaleCount':sale_counts['on-sale'],'notOnSaleCount':sale_counts['not-on-sale'],
            'unverifiedSaleCount':len(unique)-sale_counts['on-sale']-sale_counts['not-on-sale'],
            'pendingAuditCount':audit_counts['pending'],'rejectedAuditCount':audit_counts['rejected'],
            'unverifiedAuditCount':len(unique)-audit_counts['pending']-audit_counts['rejected'],
            'comparedProductsMatch':sum(r.get('status')=='compared-fields-match' for r in reports),
            'codeCoverage':code_counts,
            'phase':read_json(root/'phase.json',{}).get('phase'),'stop':read_json(root/'stop.json',{}),
            'hold':read_json(root/'hold.json',{}),'categoryHolds':read_json(root/'category-holds.json',{}),
            'scopeFile':str(root/'scope.json')}


def _candidate_ids(api, frozen):
    """Exact item numbers are candidates only. A matching listing alone is not a create receipt."""
    number=(frozen['sourceDetail']['productInfo'].get('itemNum') or '')
    if not str(number).strip():
        return []
    return [str(p['productId']) for p in api.pipeline.list_products(item_num=str(number))
            if str(p.get('itemNum') or '')==str(number)]


def _observe(api, single, folder, frozen, target, args, prior):
    if prior['status']=='publication-rejected':
        return prior
    pid=prior.get('targetProductId')
    if not pid and prior.get('applyId'):
        application=api.matrix.read_apply(prior['applyId'])
        pid=(application.get('productInfo') or {}).get('productId')
        if pid:
            review=api.application_observation(pid,prior['applyId'],application)
            if review['auditState']=='rejected':
                return {**prior,'targetProductId':str(pid),'comparison':{'status':'application-rejected','readbackView':'application',
                        'saleState':'unknown',**review}}
            api.read_owned_detail(int(pid))
            pub=publication_directory(single)
            marker=read_json(pub/'publish-body.submit-state.json',{})
            actual_sha=file_sha(pub/'publish-body.json')
            if not marker.get('payloadSha256'):
                # Older successful apply-only markers lost the digest. Recover it only from
                # the bound original approval, identical saved request, and matching native response.
                pilot=read_json(folder/'pilot.json',{})
                body=read_json(pub/'publish-body.json',{})
                delivery=read_json(folder/'delivery-evidence.json')
                response=read_json(pub/'result.json',{})
                if (response.get('success') is not True or (response.get('data') or {}).get('applyId')!=marker.get('applyId')
                        or str(marker.get('applyId'))!=str(prior['applyId']) or pilot.get('revision')!=frozen['sha256']
                        or pilot.get('body')!=body or pilot.get('target')!=target or pilot.get('intent')!='publish-one'
                        or pilot.get('approval')!=api.digest({'source':frozen['sha256'],'target':target,'body':body,'delivery':delivery})):
                    raise ValueError('旧申请的原始申请编号、已审草稿、目标店与保存的请求体未对上')
                marker={**marker,'payloadSha256':actual_sha}
                atomic_json(pub/'publish-body.submit-state.json',marker)
            if actual_sha!=marker['payloadSha256']:
                raise ValueError('原提交请求体与已记录的摘要不一致')
            binding={'taskId':args.task,'sourceShop':frozen['sourceShop'],'targetShop':target,'sourceProductId':frozen['sourceProductId'],
                     'productId':str(pid),'applyId':str(prior['applyId']),'payloadSha256':marker['payloadSha256'],'kind':'original-application'}
            atomic_json(pub/'target-binding.json',binding)
            prior=api.publication_state(single,args.task)
    if not pid:
        return {**prior,'status':'submission-needs-reconciliation','candidateProductIds':_candidate_ids(api,frozen),
                'nextAction':'按原申请和提交回执确认目标；货号相同只列候选。','retryAllowed':False}
    try:
        compared=api.compare(single,folder,frozen,target,argparse.Namespace(
            product_id=int(pid),task=args.task,apply_id=None,delivery_evidence=args.delivery_evidence))
    except (api.api.JdoError,api.matrix.MatrixError,ValueError,KeyError,OSError):
        compared={'status':'awaiting-readback','nextAction':'按当前目标店与已有商品编号继续只读核对。'}
    if compared.get('auditState') not in ('pending','rejected'):
        try:
            lookup=prior.get('applyId') or pid
            apply=api.matrix.read_apply(lookup)
            compared.update(api.application_observation(pid, prior.get('applyId'), apply))
        except (api.api.JdoError,api.matrix.MatrixError,ValueError,KeyError,OSError):
            pass
    return {**prior,'targetProductId':str(pid),'comparison':compared}


def _single(api, batch_root, scope, pid, args, phase, previous, category_holds):
    target=scope['targetShop']
    sample={'sourceShop':scope['sourceShop'],'targetShop':target,'sourceProductId':pid}
    key=api.pair_key(sample)
    single=api.task_directory(key)
    bound=scope['existingTargets'].get(pid)
    pointer=read_json(single/'current.json',{})
    existing=api.publication_state(single,key)
    if bound and existing.get('targetProductId') and str(existing['targetProductId'])!=bound:
        return {'taskId':key,'status':'mapping-conflict'}
    if not pointer or (bound and existing['status']=='not-submitted'):
        api.freeze(argparse.Namespace(**{**vars(args),'product_id':int(pid),'existing_target_id':int(bound) if bound else None}))
    with FileLock(safe_join(single,'copy.lock')):
        folder,frozen=api.load_copy(single)
        if frozen['sourceShop']!=scope['sourceShop'] or frozen['targetShop']!=target or frozen['sourceProductId']!=pid:
            raise ValueError('单品原任务与批次绑定的店铺或来源不一致')
        if previous.get('revision') and previous['revision']!=frozen['sha256']:
            return {'taskId':key,'status':'source-revision-changed','revision':previous['revision']}
        category=str(api.category(frozen['sourceDetail']))
        base={'taskId':key,'revision':frozen['sha256'],'categoryId':category,'sourceSkuCount':len(frozen['sourceDetail']['skuList'])}
        # Keep the revision even if the process exits before the next item checkpoint.
        atomic_json(_record_path(batch_root,pid),{**previous,**base,'sourceProductId':pid,'status':'checking-original'})
        common=argparse.Namespace(**{**vars(args),'task':key})
        api.require_target(frozen,common)
        prior=api.publication_state(single,key)
        if phase=='draft':
            if prior['status']=='not-submitted':
                draft=api.prepare(folder,frozen,target,argparse.Namespace(**{**vars(common),'intent':'local-draft'}))
                return {**base,**draft}
            return {**previous,**base,**prior}
        retry=prior['status']=='publication-rejected' and args.correction_evidence
        if prior['status']=='publication-rejected' and not retry:
            return {**base,**prior}
        if prior['status']!='not-submitted' and not retry:
            return {**base,**_observe(api,single,folder,frozen,target,common,prior)}
        held=category_holds.get(category,{})
        if held.get('active') and held['sourceProductId']!=pid:
            return {**base,'status':'category-held','heldBy':held['sourceProductId']}
        if bound:
            return {**base,'status':'mapping-conflict'}
        candidates=[] if retry else _candidate_ids(api,frozen)
        if candidates:
            return {**base,'status':'existing-target-candidates','candidateProductIds':candidates,
                    'nextAction':'核对已存在的目标，从原单品任务绑定唯一目标后再继续；未创建替代商品。'}
        publish_args=argparse.Namespace(**{**vars(common),'intent':'publish-one'})
        prepared=api.prepare(folder,frozen,target,publish_args)
        if prepared['status']=='needs-evidence':
            missing=prepared.get('missing') or []
            if retry or not missing or any(not str(item).startswith('目标店铺素材地址未唯一核验：') for item in missing):
                return {**base,**prepared}
            media=api.stage_media(folder,frozen,target,before_write=lambda:_check_stop(batch_root,pid))
            if media['status']!='media-ready':
                return {**base,**media}
            prepared=api.prepare(folder,frozen,target,publish_args)
        if prepared['status']!='pilot-prepared':
            return {**base,**prepared}
        payload=api.digest(read_json(folder/'pilot.json')['body'])
        if previous.get('blockedPayload')==payload:
            return {**previous,**base,'status':'validation-needs-review'}
        common.approval=prepared['approvalFingerprint']
        prior=api.submit_one(single,folder,frozen,target,common,before_write=lambda:_check_stop(batch_root,pid))
        if prior['status'] not in ('submitted','existing-target-for-comparison','submission-needs-reconciliation'):
            row={**base,**prior}
            if prior['status']=='validation-needs-review':
                row['blockedPayload']=payload
            return row
        return {**base,**_observe(api,single,folder,frozen,target,common,prior)}


def _block_reason(row):
    if row['status'] in ('mapping-conflict','submission-needs-reconciliation','upload-needs-reconciliation'):
        return row['status']
    comparison=row.get('comparison')
    if comparison and comparison.get('auditState')=='rejected':
        return 'application-rejected'
    if comparison and comparison.get('status')=='different':
        return 'readback-'+str(comparison.get('status','unknown'))
    return None


def _category_refusal(row):
    if row['status']=='publication-rejected':
        codes=row.get('rejection',{}).get('platformCodes',[])
        # The same generic validation code can cover unrelated product defects.
        signature=codes if any(c in ('99904030007','99904030008') for c in codes) else []
        return {'stage':row['status'],'codes':codes,'systemicSignature':signature}
    if row['status']=='validation-needs-review':
        return {'stage':row['status'],'systemicSignature':[]}
    return None


def _claims(target,api):
    """Reuse verifiable original bindings, including a single product or an overlapping batch."""
    owners=defaultdict(set)
    base=safe_join(merchant_home(),'work/shop-copies')
    for path in base.glob('*'):
        if not re.fullmatch('[0-9a-f]{24}',path.name):
            continue
        record=read_json(safe_join(path,'current.json'),{})
        shop=record.get('targetShop',{})
        if (shop.get('shopId'),shop.get('venderId'))!=(target['shopId'],target['venderId']):
            continue
        task=record.get('taskId','')
        if not re.fullmatch('[0-9a-f]{64}',task) or task[:24]!=path.name or api.pair_key(record)!=task:
            raise ValueError('目标店的历史映射与原来源不一致，请先只读核对')
        tid=api.publication_state(path,task).get('targetProductId')
        if tid:
            owners[str(tid)].add(task)
    return owners


def _permission_recovery_member(root, scope, ids, held, categories, evidence, api):
    """A store-wide hold permits only a bound corrective attempt (or its accepted receipt)."""
    excluded = read_json(root/'excluded-sources.json',{})
    requested = read_json(evidence,{}) if evidence else {}
    for category in categories.values():
        pid = category.get('sourceProductId')
        if (pid not in ids or pid in excluded or category.get('systemicSignature') != held.get('platformCodes')):
            continue
        task = api.pair_key({**scope,'sourceProductId':pid})
        if evidence and requested.get('taskId') == task and category.get('active'):
            return pid
        prior = api.publication_state(api.task_directory(task),task)
        if (prior.get('status')=='submitted' and prior.get('receiptDirectory')
                and Path(prior['receiptDirectory']).name!='publication'):
            return pid
    return None


def _member_targets(scope, rows, api):
    claims=_claims(scope['targetShop'],api)
    mapped={}
    conflicts=set()
    for row in rows:
        sid=row['sourceProductId']
        existing=scope['existingTargets'].get(sid)
        tid=str(row.get('targetProductId') or existing or '')
        mapped[sid]=tid
        if not tid:
            continue
        task=api.pair_key({**scope,'sourceProductId':sid})
        if existing and str(existing)!=tid:
            conflicts.add(str(existing))
            conflicts.add(tid)
        if row['status']=='mapping-conflict' or claims.get(tid,set())-{task}:
            conflicts.add(tid)
    return mapped,conflicts|{tid for tid,count in Counter(mapped.values()).items() if tid and count>1}


def _batch_materials(root,scope,args,api):
    from .shop_copy_materials import handle
    if _stores(args,api)!=(scope['sourceShop'],scope['targetShop']):
        raise ValueError('源店或目标店与原批次不一致')
    if api.verify(args.target_config,args.target_profile)!=scope['targetShop']:
        raise ValueError('目标店的身份已变化')
    active_catalog=api.pipeline.list_products(require_success=True)
    api.api.recheck_verified_jdo_store()
    on_sale={str(r['productId']) for r in active_catalog if str(r.get('productStatus')) in ('1','103')}
    batch=_report(root,args.batch,scope)
    mapped,conflicts=_member_targets(scope,batch['rows'],api)
    chosen=set(args.source_product_id or scope['sourceProductIds'])
    if not chosen<=set(mapped):raise ValueError('素材范围含未在原批次出现的源商品')
    outcome={'batchId':args.batch,'sourceShop':scope['sourceShop'],'targetShop':scope['targetShop'],'rows':[],
             'requestedCount':len(chosen),'onSaleOutsideBatch':sorted(on_sale-set(mapped.values())-set(scope['existingTargets'].values())),
             'status':'running','checkedAt':int(time.time())}
    path=root/'material-audits'/f'{uuid.uuid4().hex}.json'
    atomic_json(path,outcome)
    health={}
    for row in batch['rows']:
        sid=row['sourceProductId']
        if sid not in chosen:continue
        tid=mapped[sid]
        item={'sourceProductId':sid,'targetProductId':tid or None}
        try:
            _check_stop(root,sid)
            accepted=row['status'] in ('submitted','existing-target-for-comparison')
            existing=(scope['existingTargets'].get(sid)==tid and row['status'] in ('not-started','local-draft-ready'))
            if not tid or tid in conflicts or not (accepted or existing):
                item['migrationStatus']='unverified-product-pair'
            elif tid not in on_sale or (row.get('comparison') or {}).get('auditState')=='rejected':
                item['migrationStatus']='target-not-on-sale-or-application-rejected'
            else:
                options=argparse.Namespace(**{**vars(args),'source_product_id':int(sid),'target_product_id':int(tid),
                                               '_batch_health':health,'_before_write':lambda:_check_stop(root,sid)})
                item.update(handle(options,api))
        except MemberSkipped:item['migrationStatus']='skipped-by-merchant'
        except BatchStopped:
            outcome['status']='stopped'
            break
        except (api.api.JdoError,api.matrix.MatrixError,api.pipeline.PipelineError,OSError,TypeError,KeyError,ValueError) as exc:
            item.update(migrationStatus='unverified',reason=redact(str(exc))[:240])
        outcome['rows'].append(item)
        atomic_json(path,outcome)
    counts=Counter(r.get('migrationStatus','unverified') for r in outcome['rows'])
    health_counts=Counter(r.get('targetHealthStatus','not-queried') for r in outcome['rows'])
    active=[r for r in outcome['rows'] if r.get('migrationStatus')!='skipped-by-merchant']
    unresolved=sum(not r.get('ok') or (bool(args.confirm) and r.get('migrationStatus')!='verified') for r in active)
    if outcome['status']=='running':
        if args.confirm:
            outcome['status']='material-copy-incomplete' if unresolved else 'material-copy-checked'
        else:
            outcome['status']='material-audit-partial' if unresolved else 'material-audit-finished'
    outcome.update(migrationCounts=dict(counts),healthCounts=dict(health_counts),observedCount=len(outcome['rows']))
    atomic_json(path,outcome)
    return {k:v for k,v in outcome.items() if k!='rows'} | {
        'ok':outcome['status'] in ('material-audit-finished','material-copy-checked'),
        'unresolvedProducts':unresolved,'reportFile':str(path)}


def dispatch(args, api):
    if args.action=='batch-plan':
        return _plan(args,api)
    root=directory(args.batch)
    scope=_scope(root,args.batch,api)
    if args.action=='batch-materials':
        return _batch_materials(root,scope,args,api)
    if args.action=='batch-skip':
        ids=set(args.source_product_id)
        if not ids <= set(scope['sourceProductIds']):
            raise ValueError('指定源商品不在此批次，请按原任务核对来源')
        with FileLock(safe_join(root,'control.lock')):
            excluded=read_json(root/'excluded-sources.json',{})
            for pid in sorted(ids):
                excluded.setdefault(pid,{'at':int(time.time()),'reason':redact(args.reason)})
            atomic_json(root/'excluded-sources.json',excluded)
        return _report(root,args.batch,scope)
    if args.action=='batch-census':
        target=api.verify(args.target_config,args.target_profile)
        if target!=scope['targetShop']:raise ValueError('当前核验店铺与批次目标不同')
        live=api.pipeline.list_products(require_success=True)
        api.api.recheck_verified_jdo_store()
        report=_report(root,args.batch,scope)
        by_source,conflicts=_member_targets(scope,report['rows'],api)
        skipped={by_source[row['sourceProductId']] for row in report['rows'] if row['skippedByMerchant']}-{''}
        all_targets=(set(by_source.values())|set(scope['existingTargets'].values()))-{''}
        mapped=all_targets-conflicts-skipped
        on_sale={str(row['productId']) for row in live if str(row.get('productStatus')) in ('1','103')}
        records={'targetShop':target,'batchId':args.batch,'checkedAt':int(time.time()),'onSaleCount':len(on_sale),
                 'mappedOnSaleIds':sorted(mapped & on_sale),'onSaleOutsideBatch':sorted(on_sale-all_targets),
                 'mappedOutsideOnSale':sorted(mapped-on_sale),'sourceProductIds':scope['sourceProductIds'],
                 'skippedTargetIds':sorted(skipped),'conflictingTargetIds':sorted(conflicts)}
        path=root/'target-census.json'
        atomic_json(path,records)
        return {**records,'ok':True,'status':'catalogue-compared','platformWrites':0,'reportFile':str(path)}
    if args.action=='batch-stop':
        return _set_stop(root,args)
    if args.action=='batch-status':
        report=_report(root,args.batch,scope)
        if args.output:
            dest=args.output.resolve()
            if ((dest.is_relative_to(_base()) or dest.is_relative_to(merchant_home()/'work/shop-copies'))
                    and not dest.is_relative_to(root/'exports')):
                raise ValueError('报告请保存到交付目录或原批次 exports 中；原执行与对账记录保留')
            if dest.exists():
                prior=read_json(dest,{})
                if not isinstance(prior,dict) or any(prior.get(k)!=report[k] for k in ('status','batchId','scopeFile')):
                    raise ValueError('目标已有其他文件，请保存到新的报告文件名')
            atomic_json(dest,report)
        return report
    # Serialize batches in this target shop; each product still owns its original copy lock.
    shop_lock=safe_join(_base(),'locks/'+scope['targetShop']['shopId']+'.lock')
    try:
        with FileLock(shop_lock):
            if _stores(args,api)!=(scope['sourceShop'],scope['targetShop']):
                raise ValueError('批次记录和当前实际店铺不一致')
            phase=_authorize(root,args,scope)
            if args.delivery_evidence:
                data=read_json(args.delivery_evidence)
                if not isinstance(data,dict):
                    raise ValueError('配送证据文件无法读取')
                for side in ('source','target'):
                    if data.get(side):
                        identity=(data[side].get('shop') or {})
                        if any(str(identity.get(k,''))!=scope[side+'Shop'][k] for k in ('profile','shopId','venderId')):
                            raise ValueError('配送证据与批次店铺不一致')
                saved=safe_join(root,'evidence/'+api.digest(data)[:32]+'.json')
                atomic_json(saved,data)
                atomic_json(root/'delivery-pointer.json',{'sha256':api.digest(data)})
            pointer=read_json(root/'delivery-pointer.json',{})
            if pointer:
                sha=pointer.get('sha256','')
                if not re.fullmatch('[0-9a-f]{64}',sha):
                    raise ValueError('批次的原配送证据引用不完整')
                args.delivery_evidence=safe_join(root,'evidence/'+sha[:32]+'.json')
                if api.digest(read_json(args.delivery_evidence))!=sha:
                    raise ValueError('批次原配送证据已改变')
            held=read_json(root/'hold.json',{})
            category_holds=read_json(root/'category-holds.json',{})
            claims=_claims(scope['targetShop'],api)
            ids=[scope['pilotId']] if phase=='pilot' else list(scope['sourceProductIds'])
            if phase!='draft':
                ids=list(dict.fromkeys([scope['pilotId'],*ids]))
                representatives=[h['sourceProductId'] for h in category_holds.values()
                                 if h.get('active') and h['sourceProductId'] in ids]
                ids=list(dict.fromkeys([*representatives,*ids]))
                if held.get('active') and held.get('sourceProductId') in ids:
                    ids.remove(held['sourceProductId'])
                    ids.insert(0,held['sourceProductId'])
                if held.get('active') and held.get('reason')=='repeated-official-permission-refusal':
                    candidate=_permission_recovery_member(root,scope,ids,held,category_holds,args.correction_evidence,api)
                    if candidate is None:
                        return {**_report(root,args.batch,scope),'status':'batch-held','ok':False}
                    ids.remove(candidate)
                    ids.insert(0,candidate)
                elif held.get('active') and held.get('sourceProductId') not in ids:
                    return {**_report(root,args.batch,scope),'status':'batch-held','ok':False}
            for pid in ids:
                _check_stop(root)
                path=_record_path(root,pid)
                old=read_json(path,{})
                try:
                    task=api.pair_key({**scope,'sourceProductId':pid})
                    _check_stop(root,pid)
                    expected=scope['existingTargets'].get(pid) or old.get('targetProductId')
                    already=claims.get(str(expected),set())
                    if already-{task}:
                        row={**old,'status':'mapping-conflict','targetProductId':str(expected),'taskId':task}
                    else:
                        row=_single(api,root,scope,pid,args,phase,old,category_holds)
                except MemberSkipped:
                    if phase!='draft' and held.get('active'):
                        if held.get('reason')=='repeated-official-permission-refusal':
                            break
                        if held.get('sourceProductId')==pid:
                            if held.get('reason') not in ('application-rejected','readback-different'):
                                break
                            atomic_json(root/'hold.json',{**held,'active':False,'resolvedBy':'merchant-skip'})
                            held={}
                    continue
                except (api.api.JdoError,api.pipeline.PipelineError,api.matrix.MatrixError,ValueError,KeyError,OSError) as error:
                    state=read_json(path,{})
                    original=api.publication_state(api.task_directory(task),task)
                    status=original['status'] if original['status']!='not-submitted' else 'source-needs-review'
                    message='店铺只读请求未取得完整回执' if isinstance(error,api.api.JdoError) else redact(str(error))[:240]
                    row={**state,**original,'status':status,'reason':message}
                    if status in ('submitted','existing-target-for-comparison'):
                        row['comparison']={'status':'awaiting-readback'}
                row.update(sourceProductId=pid,checkedAt=int(time.time()))
                # Two source products may never receive credit for the same target listing.
                tid=row.get('targetProductId')
                if tid:
                    owners=claims[str(tid)]
                    owners.add(task)
                    if len(owners)>1:
                        row.update(status='mapping-conflict',conflictingTasks=sorted(owners))
                atomic_json(path,row)
                category=row.get('categoryId')
                recovering_permission=(held.get('reason')=='repeated-official-permission-refusal'
                                       and row.get('status')=='submitted' and row.get('receiptDirectory') and
                                       Path(row['receiptDirectory']).name!='publication' and category and
                                       category_holds.get(category,{}).get('systemicSignature')==held.get('platformCodes'))
                if (phase!='draft' and held.get('active') and held.get('reason')=='repeated-official-permission-refusal'
                        and (not recovering_permission or _block_reason(row))):
                    break
                refusal=_category_refusal(row) if phase!='draft' else None
                if refusal and category:
                    category_holds[category]={**refusal,'active':True,'sourceProductId':pid,'checkedAt':row['checkedAt']}
                    atomic_json(root/'category-holds.json',category_holds)
                    signature=refusal['systemicSignature']
                    if signature and any(k!=category and h.get('active') and h.get('systemicSignature')==signature
                                         for k,h in category_holds.items()):
                        atomic_json(root/'hold.json',{'active':True,'sourceProductId':pid,'reason':'repeated-official-permission-refusal',
                                    'platformCodes':signature,'checkedAt':row['checkedAt']})
                        break
                elif (phase!='draft' and category and category_holds.get(category,{}).get('active') and
                      category_holds[category]['sourceProductId']==pid and row['status'] in ('submitted','existing-target-for-comparison')):
                    category_holds[category]['active']=False
                    atomic_json(root/'category-holds.json',category_holds)
                reason=_block_reason(row)
                if phase!='draft' and reason:
                    atomic_json(root/'hold.json',{'active':True,'sourceProductId':pid,'reason':reason,'checkedAt':row['checkedAt']})
                    break
                if phase!='draft' and held.get('active') and (held.get('sourceProductId')==pid or recovering_permission):
                    if row['status'] not in ('submitted','existing-target-for-comparison'):
                        break
                    if (held.get('reason') in ('readback-different','application-rejected') and
                            (row.get('comparison') or {}).get('status')!='compared-fields-match'):
                        break
                    atomic_json(root/'hold.json',{**held,'active':False,'resolvedAt':row['checkedAt']})
                    held={}
            _check_stop(root)
    except BatchStopped as stopped:
        return {**_report(root,args.batch,scope),'status':'batch-stopped','ok':False,'stopId':str(stopped)}
    report=_report(root,args.batch,scope)
    selected=[r for r in report['rows'] if r['sourceProductId'] in ids and not r['skippedByMerchant']]
    remaining=any(r['status'] not in ('submitted','existing-target-for-comparison','local-draft-ready') for r in selected)
    status='batch-held' if report['hold'].get('active') else 'batch-needs-evidence' if remaining else 'batch-reviewed'
    return {**report,'status':status,'ok':status=='batch-reviewed'}
