"""Expose the shared product copy operations and installed host extensions."""
import argparse
import json
from importlib.util import find_spec
from pathlib import Path
from . import shop_copy_command as ops


def main(argv=None):
    from . import shop_copy_batch as batches
    from . import shop_copy_materials as materials
    from . import shop_copy_short_titles as titles
    from . import shop_copy_history as history
    parser = argparse.ArgumentParser(description='自家两店商品复制与按原批次恢复')
    subs = parser.add_subparsers(dest='action',required=True)
    materials.register(subs)
    titles.register(subs)
    history.register(subs)
    review = subs.add_parser('diagnose-application',help='读取指定目标商品的原申请、审核原因和商品库状态')
    review.add_argument('--target-config',type=Path,required=True)
    review.add_argument('--target-profile',required=True)
    review.add_argument('--product-id',type=int,required=True)
    review.add_argument('--apply-id',type=int)
    batches.register(subs)
    names = ['inventory','freeze','stage-media','prepare','submit-one','compare']
    if find_spec(__package__ + '.shop_copy_repair') is not None:
        names.append('repair-sku-codes')
    for name in names:
        p = subs.add_parser(name)
        p.add_argument('--target-config', type=Path, required=name!='prepare')
        p.add_argument('--target-profile', required=name!='prepare')
        if name in ('inventory','freeze','submit-one'):
            p.add_argument('--source-config', type=Path, required=True)
            p.add_argument('--source-profile', required=True)
        if name not in ('inventory','freeze'):
            p.add_argument('--task', required=True)
        if name == 'freeze':
            p.add_argument('--product-id', type=int, required=True)
            p.add_argument('--existing-target-id', type=int, help='原任务已有目标商品时绑定该商品，仅对账')
        if name == 'inventory':
            p.add_argument('--status', type=int, action='append', required=True, help='要盘点的官方商品状态码，可重复')
        if name == 'stage-media':
            p.add_argument('--confirm', choices=['UPLOAD_COPY_MEDIA'], required=True)
        if name in ('prepare','compare'):
            p.add_argument('--delivery-evidence', type=Path)
        if name == 'prepare':
            p.add_argument('--intent', choices=['local-draft','publish-one'], default='local-draft')
        if name == 'submit-one':
            p.add_argument('--approval', required=True)
            p.add_argument('--confirm', choices=['PUBLISH_ONE_COPY'], required=True)
        if name == 'compare':
            p.add_argument('--product-id', type=int)
            p.add_argument('--apply-id', type=int, help='用原申请单回读目标商品的编辑值')
        if name == 'repair-sku-codes':
            p.add_argument('--product-id',type=int)
            mode=p.add_mutually_exclusive_group()
            mode.add_argument('--check-only',action='store_true')
            mode.add_argument('--reconcile',action='store_true')
            mode.add_argument('--confirm',choices=['REPAIR_TARGET_SKU_CODES'])
        if name=='submit-one':
            p.add_argument('--correction-evidence',type=Path)
    args = parser.parse_args(argv)
    try:
        if args.action=='copy-materials':
            result=materials.handle(args,ops)
        elif args.action=='copy-short-titles':
            result=titles.handle(args,ops)
        elif args.action=='history-census':
            result=history.run(args,ops)
        elif args.action=='diagnose-application':
            target=ops.verify(args.target_config,args.target_profile)
            review=ops.application_observation(args.product_id,args.apply_id)
            try:
                live=ops.read_owned_detail(args.product_id)
                sku_count=len(ops.copy.sku_rows(live,args.product_id))
                listing='read-live-product'
            except (ops.api.JdoError,ops.matrix.MatrixError,KeyError,ValueError):
                sku_count=None
                listing='live-product-unverified'
            ops.api.recheck_verified_jdo_store()
            result={'ok':True,'status':'application-observed','targetShop':target,'targetProductId':str(args.product_id),
                    'listingEvidence':listing,'liveSkuCount':sku_count,'platformWrites':0,**review}
        elif args.action in batches.ACTIONS:
            result=batches.dispatch(args,ops)
        elif args.action == 'inventory':
            result = ops.inventory(args)
        elif args.action == 'freeze':
            result = ops.freeze(args)
        else:
            root = ops.task_directory(args.task)
            with ops.FileLock(ops.safe_join(root,'copy.lock')):
                folder, frozen = ops.load_copy(root)
                local = args.action == 'prepare' and args.intent == 'local-draft'
                target = ops.local_target(frozen,args) if local else ops.require_target(frozen,args)
                if args.action == 'stage-media':
                    prior = ops.publication_state(root,args.task)
                    result = prior if prior['status'] != 'not-submitted' else ops.stage_media(folder, frozen, target)
                elif args.action == 'prepare':
                    prior = {} if local else ops.publication_state(root,args.task)
                    result = ops.prepare(folder, frozen, target, args) if local or prior['status'] in ('not-submitted','publication-rejected') else prior
                elif args.action == 'submit-one':
                    result = ops.submit_one(root, folder, frozen, target, args)
                    result.update(sourceProductId=frozen['sourceProductId'],targetShop=target,
                                  sourceSkuUrl='https://item.jd.com/'+str(frozen['sourceDetail']['skuList'][0]['skuId'])+'.html')
                    if result.get('targetProductId'):
                        try:
                            result['comparison'] = ops.compare(root,folder,frozen,target,argparse.Namespace(
                                task=args.task, product_id=result['targetProductId'], apply_id=None, delivery_evidence=None))
                        except (ops.api.JdoError,ValueError,KeyError,OSError):
                            result['comparison'] = {'status':'awaiting-readback','nextAction':'从原复制任务只读对账。'}
                elif args.action=='repair-sku-codes':
                    from .shop_copy_repair import repair_codes
                    result=repair_codes(root,folder,frozen,target,args,api=ops.api,read_product=ops.read_owned_detail)
                else:
                    result = ops.compare(root, folder, frozen, target, args)
        result.setdefault('ok',result['status'] in ('inventory-ready','local-plan','media-ready','local-draft-ready',
                                             'pilot-prepared','submitted','existing-target-for-comparison',
                                             'compared-fields-match'))
        if result['status'] in ('submission-needs-reconciliation','upload-needs-reconciliation','publication-rejected'):
            result['retryAllowed'] = False
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['ok'] else 2
    except ops.api.JdoError:
        print(json.dumps({'ok':False,'status':'needs-platform-receipt','message':'官方店铺请求未完成，请保留本任务并核对原回执。'},ensure_ascii=False))
        return 2
    except (KeyError, TypeError, ValueError, OSError) as error:
        from .safety import redact
        print(json.dumps({'ok':False,'status':'blocked','message':redact(str(error))},ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
