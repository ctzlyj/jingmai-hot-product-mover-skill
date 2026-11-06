"""detail-repair: complete the detail page of an already published product.

Reads an existing listing-package, regenerates detail modules (company three GPT models only),
and after the user previews the result, patches ONLY the WareEditor component (PC + mobile).
Price, stock, SKU, main images and video are never part of the request body.
An unknown outcome is reconciled by reading the product back; the PATCH is never replayed.
"""
from __future__ import annotations
import argparse, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import win_no_window  # noqa: F401
import photo_listing as pl
import detail_pages as dp
from photo_contract import ReconcileRequired, media_entries, package_path, review_digest
from photo_generation import atomic_json, generate_tasks, load, sha

CONFIRM = 'DETAIL_PATCH'
IN_FLIGHT = ('SUBMITTING', 'UNKNOWN')


def _state_path(root):
    return Path(root) / '.detail-repair' / 'state.json'


def _published(root):
    m = pl.read_manifest(root)
    if not m.get('productId'):
        raise ValueError('detail-repair 只用于已上架商品：manifest 里没有 productId')
    return m


def plan(root):
    """Detail-only generation plan from the existing package (facts + every SKU)."""
    root = Path(root)
    m = _published(root)
    result = pl.make_plan(root, preview=False, only='detail')
    return {'tasks': result['tasks'], 'detailModules': result['detailModules'], 'productId': m['productId']}


def generate(root):
    root = Path(root)
    tasks = load(root / 'plan.json')
    if not tasks or {t['role'] for t in tasks} != {'detail'}:
        raise ValueError('先运行 detail-repair plan，生成仅含详情模块的方案')
    result = generate_tasks(tasks, root / '.generation')
    pl.materialize_generated(root, tasks, result)
    return {k: v for k, v in result.items() if k != 'ledger'}


def _repair_digest(m, root):
    items = [{'localPath': d['localPath'], 'sha256': sha(package_path(root, d['localPath']))} for d in m.get('detailImages') or []]
    value = {'store': m.get('store'), 'productId': m.get('productId'), 'detail': items, 'policy': (m.get('facts') or {}).get('detailPolicy')}
    import hashlib
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def review(root):
    root = Path(root)
    m = _published(root)
    gate = dp.check_detail_set(m, root)
    digest = _repair_digest(m, root)
    atomic_json(root / '.detail-repair' / 'review.json', {'digest': digest, 'gateOk': gate['ok'], 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})
    details = m.get('detailImages') or []
    lines = ['# 详情页补救确认清单', '', f"店铺：{m['store']['shopName']}（{m['store']['profile']} / {m['store']['venderId']}）", f"商品ID：{m['productId']}",
             '只修改：详情页（PC + 手机端）。不修改价格、库存、SKU、主图、视频。', '',
             f"详情页：{len(details)} 张｜发布前门槛：{'通过' if gate['ok'] else '未通过，禁止写入'}"]
    lines += [f"{i}. {d.get('id')}｜{dp.KIND_TITLES.get(d.get('module'), d.get('module') or '')}｜SKU：{','.join(d.get('skuKeys') or []) or '通用'}" for i, d in enumerate(details, 1)]
    lines += [f'- 问题：{p}' for p in gate['problems']]
    if details:
        overview = dp.overview_image([package_path(root, d['localPath']) for d in details], root / '详情图' / '详情页总览.jpg')
        lines += ['', f'![详情页总览]({overview.relative_to(root).as_posix()})']
    lines += ['', f'确认摘要：`{digest}`', '用户确认当前预览后才能执行 apply --confirm DETAIL_PATCH。']
    (root / '详情补救确认清单.md').write_text('\n'.join(lines), encoding='utf-8')
    return {'digest': digest, 'detailCount': len(details), 'gateOk': gate['ok'], 'problems': gate['problems'], 'checklist': str(root / '详情补救确认清单.md')}


def _remote(product_id):
    return pl.cli.get('jdo-product-update', '/rest/sp-product/v0/product-apply/{applyId}', {'applyId': str(product_id), 'scene': 'pop'}).get('data') or {}


def _readback(m, urls):
    remote = _remote(m['productId'])
    desc = (remote.get('productInfo') or {}).get('productDetailDesc') or {}
    return remote, dp.readback_detail(desc, urls)


def _finish(root, m, urls, readback, began):
    gate = dp.check_detail_set(m, root)
    problems = list(readback['problems']) + ['local detail gate: ' + x for x in gate['problems']]
    checks = dict(m.get('deliveryChecks') or {})
    checks.update(detailQualityPassed=not problems, checkedAt=time.strftime('%Y-%m-%dT%H:%M:%S'))
    m['deliveryChecks'] = checks
    checks['statement'] = dp.completion_statement(dp.delivery_checks(m))
    for d, url in zip(m.get('detailImages') or [], urls):
        d['onlineUrl'] = url
    m['detailPlan'] = {'moduleCount': len(m.get('detailImages') or []), 'width': dp.OUTPUT_WIDTH, 'exemption': gate.get('exemption'),
                       'readback': {k: v for k, v in readback.items() if k != 'problems'},
                       'modules': [{'id': d.get('id'), 'kind': d.get('module'), 'localPath': d.get('localPath'), 'skuKeys': d.get('skuKeys') or [], 'onlineUrl': d.get('onlineUrl') or ''} for d in m.get('detailImages') or []]}
    m.setdefault('timings', {})['detailRepairSeconds'] = round(time.monotonic() - began, 2)
    atomic_json(Path(root) / 'manifest.json', m)
    return {'detailQualityPassed': not problems, 'problems': problems, 'detailCount': len(urls), 'productId': m['productId'], 'statement': checks['statement'], 'readback': {k: v for k, v in readback.items() if k != 'problems'}}


def apply(root, reference, *, confirm, digest):
    if confirm != CONFIRM:
        raise ValueError('补救写入需要用户确认预览后传 --confirm DETAIL_PATCH')
    root = Path(root)
    m = _published(root)
    saved = load(root / '.detail-repair' / 'review.json', {}) or {}
    if not digest or saved.get('digest') != digest or _repair_digest(m, root) != digest:
        raise ValueError('预览已变化或缺失：请重新 review 并让用户确认当前详情页')
    gate = dp.check_detail_set(m, root)
    if not gate['ok']:
        raise ValueError('详情页未通过发布前检查，禁止写入：' + '；'.join(gate['problems']))
    state_path = _state_path(root)
    state = load(state_path, {}) or {}
    pl.verify_store(m['store'])
    began = time.monotonic()
    if state.get('status') in IN_FLIGHT or state.get('status') == 'SUCCEEDED':
        if state.get('digest') != digest:
            raise ReconcileRequired('已有未核对的详情写入；先回查，不要用新内容覆盖')
        urls = state['urls']
        remote, readback = _readback(m, urls)
        if not readback['problems']:
            atomic_json(state_path, {**state, 'status': 'SUCCEEDED'})
            return _finish(root, m, urls, readback, began)
        if state.get('status') == 'SUCCEEDED':
            raise ReconcileRequired('详情写入已记录成功但回读不一致；先人工核对，不重复提交')
        raise ReconcileRequired('详情写入结果不明且远端尚未更新：只回查，不重复 PATCH')
    with pl.package_lock(root, '.detail-repair.lock'):
        latest = load(state_path, {}) or {}
        if latest.get('status') in IN_FLIGHT or latest.get('status') == 'SUCCEEDED':
            raise ReconcileRequired('另一进程已发起详情写入；先回查')
        uploads_path = root / '.publish' / 'uploads.json'
        uploads = load(uploads_path, {}) or {}
        zone = reference['media']['zoneId']
        cat = reference['media']['defaultCategoryId']
        urls = []
        for d in m['detailImages']:
            file = package_path(root, d['localPath'])
            digest_ = sha(file)
            record = uploads.get(digest_) or {}
            if not record.get('imgUrl'):
                if record.get('status') in IN_FLIGHT:
                    raise ReconcileRequired('图片上传结果不明；先在图片空间核对')
                uploads[digest_] = {'status': 'SUBMITTING', 'sha256': digest_, 'localPath': d['localPath']}
                atomic_json(uploads_path, uploads)
                try:
                    result = pl.cli.upload(zone, cat, file)
                except Exception:
                    uploads[digest_]['status'] = 'UNKNOWN'
                    atomic_json(uploads_path, uploads)
                    raise ReconcileRequired('图片上传结果不明；不自动重放') from None
                url = result.get('imgUrl') or result.get('imageUrl')
                if not url:
                    raise ReconcileRequired('图片上传没有返回地址')
                record = {'status': 'SUCCEEDED', 'imgUrl': url, 'sha256': digest_}
                uploads[digest_] = record
                atomic_json(uploads_path, uploads)
            d['imgUrl'] = record['imgUrl']
            urls.append(pl.cli.detail_image_url(record['imgUrl']))
        remote = _remote(m['productId'])
        info = remote.get('productInfo') or {}
        category = int((info.get('categoryDetail') or {}).get('lastCategoryId') or (m.get('category') or {}).get('id') or 0)
        if not category:
            raise ValueError('读不到商品类目，停止，不写入')
        body = dp.build_detail_patch_body(m['productId'], category, urls, info.get('productDetailDesc'))
        body_path = root / '.detail-repair' / 'patch-body.json'
        atomic_json(body_path, body)
        atomic_json(state_path, {'status': 'SUBMITTING', 'digest': digest, 'urls': urls, 'bodySha256': sha(body_path), 'at': time.strftime('%Y-%m-%dT%H:%M:%S')})
        try:
            pl.cli.run_jdo_large(url='/rest/sp-product/v0/product-applies/{applyId}', method='PATCH', body_path=body_path,
                                 params={'applyId': str(m['productId'])}, skill_id='jdo-product-update', yes=True)
        except Exception:
            atomic_json(state_path, {'status': 'UNKNOWN', 'digest': digest, 'urls': urls, 'bodySha256': sha(body_path)})
            raise ReconcileRequired('详情写入结果不明：只回查商品详情，不重复提交') from None
        atomic_json(state_path, {'status': 'SUCCEEDED', 'digest': digest, 'urls': urls, 'bodySha256': sha(body_path)})
    remote, readback = None, None
    for n in range(6):
        remote, readback = _readback(m, urls)
        if not readback['problems']:
            break
        time.sleep(5)
    return _finish(root, m, urls, readback, began)


def reconcile(root):
    root = Path(root)
    m = _published(root)
    state = load(_state_path(root), {}) or {}
    if not state.get('urls'):
        raise ValueError('没有待回查的详情写入')
    pl.verify_store(m['store'])
    _, readback = _readback(m, state['urls'])
    if readback['problems']:
        raise ReconcileRequired('远端详情与方案不一致：' + '；'.join(readback['problems']) + '。不会自动重复写入')
    atomic_json(_state_path(root), {**state, 'status': 'SUCCEEDED'})
    return _finish(root, m, state['urls'], readback, time.monotonic())


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('plan', 'generate', 'review', 'apply', 'reconcile'):
        p = sub.add_parser(name)
        p.add_argument('--package', required=True, type=Path)
        if name == 'apply':
            p.add_argument('--confirm', required=True)
            p.add_argument('--review-digest', required=True)
    args = parser.parse_args(argv)
    root = args.package.resolve()
    try:
        if args.command == 'plan':
            r = plan(root); result = {'productId': r['productId'], 'detailModules': r['detailModules'], 'tasks': len(r['tasks'])}
        elif args.command == 'generate':
            result = generate(root)
        elif args.command == 'review':
            result = review(root)
        elif args.command == 'apply':
            result = apply(root, load(root / 'reference.json', {}), confirm=args.confirm, digest=args.review_digest)
        else:
            result = reconcile(root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, ReconcileRequired, pl.cli.JdoError, OSError) as e:
        print(json.dumps({'ok': False, 'type': type(e).__name__, 'message': str(e)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
