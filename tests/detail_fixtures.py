"""Shared fixtures for detail-page tests: a valid, distinct, plan-aligned detail set."""
import random
import sys
from pathlib import Path
from PIL import Image
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import detail_pages as dp
from photo_generation import atomic_json, sha


def noise_image(path, seed, size=(790, 1185)):
    rnd = random.Random(seed)
    small = Image.new('RGB', (9, 8))
    small.putdata([(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)) for _ in range(72)])
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    small.resize(size, Image.Resampling.NEAREST).save(path, 'JPEG', quality=90)
    return Path(path)


def seven_skus():
    return [{'key': f'sku{i}', 'spec': f'组合{i}', 'jdPrice': '10', 'stock': 5, 'skuAttributes': []} for i in range(1, 8)]


def valid_detail_set(root, manifest, seed=100):
    """Write detail-plan.json + one distinct image per module; update manifest['detailImages']."""
    root = Path(root)
    plan = dp.build_detail_plan(manifest, root, identity_rules='只使用实拍商品。', style='干净的电商风格')
    atomic_json(root / 'detail-plan.json', plan)
    items = []
    for i, mod in enumerate(plan['modules']):
        path = noise_image(root / mod['outputFile'], seed + i)
        items.append({'id': mod['id'], 'module': mod['kind'], 'localPath': mod['outputFile'], 'sha256': sha(path),
                      'skuKeys': mod['skuKeys'], 'onlineUrl': ''})
    manifest['detailImages'] = items
    return plan
