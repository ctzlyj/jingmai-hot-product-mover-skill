"""Read back exact short titles for independently matched SKU pairs."""
import unicodedata

from . import pop_copy_pairing as copy
from . import shop_copy_materials as materials
from .safety import redact


def _title(row):
    features = row.get('features')
    if isinstance(features, dict):
        value = features.get('shortTitle')
    elif isinstance(features, list):
        if any(not isinstance(item, dict) or not isinstance(item.get('key'), str) for item in features):
            return None
        titles = [item.get('value') for item in features if item['key'] == 'shortTitle']
        value = titles[0] if len(titles) == 1 else None
    else:
        return None
    if (isinstance(value, str) and len(value) <= 4096 and redact(value) == value
            and not any(unicodedata.category(c).startswith('C') for c in value)):
        return value
    return None


def _observe(entry, frozen, source_numbers, detail, agent, origins):
    target = frozen['targetShop']
    proof = materials.product_pair(entry, frozen, detail, source_numbers, origins)
    source = copy.sku_rows(frozen['sourceDetail'], frozen['sourceProductId'])
    target_rows = copy.sku_rows(detail, proof['targetProductId'])
    mapping = materials._pair(entry, frozen, target, detail) if proof['verified'] else {'pairs':{}, 'matchedBy':{}}
    materials._same_session(entry.api, target)
    rows = []
    for sid, sku in source.items():
        tid = mapping['pairs'].get(sid)
        left = _title(sku)
        right = _title(target_rows[tid]) if tid else None
        if not tid:
            status = 'unpaired'
        elif right and left == right:
            status = 'matched'
        elif right:
            status = 'target-preserved'
        elif right is None:
            status = 'target-unreported'
        elif left is None:
            status = 'source-unreported'
        elif left == '':
            status = 'source-empty'
        elif not agent.valid_short_title(left):
            status = 'source-outside-template'
        else:
            status = 'ready'
        rows.append({'sourceSkuId':sid, 'targetSkuId':tid, 'sourceShortTitle':left, 'targetShortTitle':right,
                     'status':status, 'pairedBy':mapping['matchedBy'].get(sid)})
    return proof, rows, target_rows
