"""Internal POP adapter using the mover's verified employee CLI profile."""
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
from .shop_copy_command import category, digest, pair_key, read_owned_detail, reference, task_directory, verify

from .shop_copy_batch import publication_attempts, publication_directory


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


from .shop_copy_command import publication_state


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
    from . import shop_copy_command as operations
    if not argv or argv[0] not in ("copy-materials", "history-census", "copy-short-titles"):
        return operations.main(argv)
    parser = argparse.ArgumentParser(description="跨店搬家后逐商品、逐规格核对")
    subs = parser.add_subparsers(dest="action", required=True)
    shop_copy_materials.register(subs)
    shop_copy_history.register(subs)
    from .shop_copy_short_titles import register
    register(subs)
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
