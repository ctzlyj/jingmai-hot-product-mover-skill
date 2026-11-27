"""The same offline TOP SKU build and price evidence through either normal dispatcher."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def run(prefix):
    case = unittest.TestCase()
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        attrs = [{'attrId': 3, 'values': [{'attrValue': '5'}]}]
        products, rows = {}, []
        for index, (pid, price) in enumerate(((11, '15.25'), (22, '27.40')), start=1):
            sku = pid * 10
            rows.append({'n': index, 'pid': str(pid), 'skuId': str(sku),
                         'price': price, 'spec': f'样品 {index}'})
            products[str(pid)] = {
                'productInfo': {'productId': pid, 'brandInfo': {'brandId': 1},
                                'categoryDetail': {'lastCategoryId': 123}, 'goodsAttrInfos': attrs,
                                'productDetailDesc': {'desc': f'<p>规格 {index}</p>'},
                                'length': 2, 'width': 3, 'height': 4, 'weight': 1,
                                'logisticsInfo': {'transportId': 5, 'promiseId': 6}},
                'material': {'mainImages': [{'uuid': '0000000000', 'imageInfoList': [
                    {'imgUrl': f'jfs/{pid}/image-{n}.jpg', 'orderSort': n} for n in range(1, 6)]}]},
                'skuList': [{'skuId': sku, 'priceInfo': {'jdPrice': price},
                             'goodsAttrInfos': attrs, 'saleAttrs': [{'attrId': 4, 'attrName': '颜色'}]}],
            }
        config = {'venderId': 10, 'shopName': '合成测试店', 'jdoProfile': 'pop-fixture',
                  'categoryId': 123, 'title': '选品草稿', 'topN': len(rows), 'topSkus': rows}
        config_file = root / 'config.json'
        config_file.write_text(json.dumps(config), encoding='utf-8')
        (root / 'source_applies.json').write_text(json.dumps(products), encoding='utf-8')
        result = subprocess.run([*prefix, 'build', '--config', str(config_file)], capture_output=True, timeout=90)
        case.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        receipt = json.loads(result.stdout)
        manifest = json.loads(Path(receipt['manifestPath']).read_bytes())
        case.assertEqual((receipt['status'], receipt['skuCount']), ('BUILT', 2))
        case.assertEqual(manifest['saveActionType'], 0)
        case.assertIsNone(manifest['applyId'])
        case.assertTrue(manifest['skuDetailManualPending'])
        csv = (root / '新链接对照表.csv').read_text(encoding='utf-8-sig')
        for row in rows:
            case.assertIn(f"{row['price']},{row['price']}", csv)
        print(json.dumps({'verifiedPrices':len(rows), 'platformWrites':0}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--entry-json')
    args = parser.parse_args()
    script = Path(__file__).resolve().parents[1] / 'scripts/top_sku_aggregation.py'
    run(json.loads(args.entry_json) if args.entry_json else [sys.executable, str(script)])
