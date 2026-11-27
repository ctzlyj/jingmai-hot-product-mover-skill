"""Internal POP adapter using the mover's verified employee CLI profile."""
from hashlib import sha256
import argparse
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

import jdo_listing_mvp as api
import jd_product_pipeline as pipeline
import matrix_listing as matrix
from . import pop_copy_pairing as copy
from . import shop_copy_history, shop_copy_materials
from .safety import atomic_json, file_sha, read_json, redact, safe_join
from .state import merchant_home
from . import sale_attr_templates

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
        if (not original.get('noIssuedIds') or parent_sha not in seen or parent_sha not in children
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
        sat = sale_attr_templates
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
    copy._validate(frozen)  # Check the frozen data hash before following any pointers.
    if pair_key(frozen) != pointer.get('taskId') or pair_key(frozen)[:24] != root.name:
        raise ValueError('快照绑定的源店、目标店或商品与原任务不一致')
    return folder, frozen


def pair_key(frozen):
    stores = (frozen['sourceShop'], frozen['targetShop'])
    return digest([*[s[key] for s in stores for key in ('shopId','venderId')], frozen['sourceProductId']])


def publication_state(root, task):
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


def short_titles(args, adapter):
    from .pop_copy_titles import _observe
    root = args.materials_root or Path(__file__).resolve().parents[3] / 'jd-material-pop'
    scripts = root.resolve() / 'scripts'
    if not (scripts / 'jd_material_agent.py').is_file():
        raise ValueError('请从实际安装的 jd-material-pop 选择 --materials-root，以使用对应平台模板')
    sys.path.insert(0, str(scripts))
    import jd_material_agent as agent

    source = verify(args.source_config, args.source_profile)
    detail = read_owned_detail(args.source_product_id)
    numbers = shop_copy_materials._numbers(adapter, detail['productInfo'])
    shop_copy_materials._same_session(api, source)
    target = verify(args.target_config, args.target_profile)
    frozen = copy.freeze_copy(source, target, detail)
    live = read_owned_detail(args.target_product_id)
    history = shop_copy_history.material_origins(args.history_dir, frozen, str(args.target_product_id)) if args.history_dir else ()
    proof, rows, target_skus = _observe(adapter, frozen, numbers, live, agent, history)
    unpaired = sorted(set(target_skus) - {row['targetSkuId'] for row in rows})
    eligible = [(row['targetSkuId'], row['sourceShortTitle']) for row in rows if row['status'] == 'ready']
    out = safe_join(merchant_home(), 'work/shop-copy-short-titles/' + uuid4().hex)
    out.mkdir(parents=True)
    workbooks = []
    limit = agent.POP_SHORT_TITLE_MAX_ROWS
    for offset in range(0, len(eligible), limit):
        path = out / f'titles-{offset // limit + 1}.xlsx'
        path.write_bytes(agent._short_title_workbook_bytes(eligible[offset:offset + limit]))
        workbooks.append({'path':str(path), 'sha256':file_sha(path)})
    complete = proof['verified'] and not unpaired and all(
        row['status'] in ('matched', 'ready', 'target-preserved', 'source-empty') for row in rows)
    result = {'ok':complete, 'status':'titles-compared' if complete else 'titles-partial',
              'sourceShop':source, 'targetShop':target, 'productPair':proof,
              'unpairedTarget':unpaired, 'rows':rows, 'workbooks':workbooks,
              'preparedRows':len(eligible), 'platformWrites':0, 'reportFile':str(out / 'report.json')}
    atomic_json(out / 'report.json', result)
    return {key:value for key,value in result.items() if key != 'rows'}


def main(argv):
    parser = argparse.ArgumentParser(description="跨店搬家后逐商品、逐规格核对")
    subs = parser.add_subparsers(dest="action", required=True)
    shop_copy_materials.register(subs)
    shop_copy_history.register(subs)
    titles = subs.add_parser('copy-short-titles', help='逐 SKU 比对并用信息分模块生成目标店官方导入表')
    for side in ('source', 'target'):
        titles.add_argument(f'--{side}-profile', required=True)
        titles.add_argument(f'--{side}-config', required=True, type=Path)
        titles.add_argument(f'--{side}-product-id', required=True, type=int)
    titles.add_argument('--history-dir', type=Path)
    titles.add_argument('--materials-root', type=Path)
    args = parser.parse_args(argv)
    adapter = sys.modules[__name__]
    try:
        if args.action == "history-census":
            result = shop_copy_history.run(args, adapter)
        elif args.action == "copy-short-titles":
            result = short_titles(args, adapter)
        else:
            result = shop_copy_materials.handle(args, adapter)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
        result = {"ok": False, "status": "unverified", "reason": redact(str(exc))}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 2
