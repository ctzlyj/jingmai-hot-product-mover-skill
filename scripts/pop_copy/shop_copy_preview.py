"""A merchant-readable local draft of the same facts returned by shop-copy."""
from html import escape
import re


def _text(value):
    if value is None:
        return '待核'
    if value == '':
        return '空'
    value = escape(str(value), quote=False)
    return re.sub(r'([\\|`*_\[\]~])', r'\\\1', value).replace('\r', '').replace('\n', '<br>')


def preview(draft):
    product = draft['product']
    lines = [f"# {_text(product['title'])}", '',
             f"本地草稿 · {_text(draft['sourceShop']['shopName'])} → {_text(draft['targetShop']['shopName'])}",
             '', '按源商品当前记录整理。平台写入 0 次。', '',
             '| 源 SKU | 销售规格 | 原商家编码 | 京东价 | 库存 | 本款主图张数 |',
             '|---|---|---|---:|---:|---:|']
    for row in draft['skuRows']:
        specs = row['specifications']
        names = ' / '.join(f"{_text(a['name'])} {_text(a['value'])}" for a in specs or [])
        count = len(row['mainImages']) if isinstance(row['mainImages'],list) else None
        lines.append('| ' + ' | '.join([_text(row['sourceSkuId']), names or ('单规格' if specs == [] else '待核'),
                                        _text(row['merchantSku']), _text(row['price']), _text(row['stock']), _text(count)]) + ' |')
    if draft['repeatedSourceMerchantCodes']:
        lines += ['', '多款沿用同一原编码，已按各自规格保留。']
    if draft['evidenceActions']:
        lines += ['', '## 发布前可继续核对', '']
        for item in draft['evidenceActions']:
            parts = item['field'].split('.')
            sku = f"SKU {_text(parts[1])}　" if len(parts)==3 and parts[0]=='sku' else ''
            lines.append(f"- {sku}{_text(item['reason'])}。{_text(item['nextAction'])}")
    return '\n'.join(lines) + '\n'
