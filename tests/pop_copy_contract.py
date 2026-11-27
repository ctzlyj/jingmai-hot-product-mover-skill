"""Run both normal POP entrypoints against an offline CLI double in a temporary home.

python tests/pop_copy_contract.py [--merchant-root <source checkout>]
python tests/pop_copy_contract.py --entry-json '["python", "...", "shop-copy"]'
The alternate argv prefix supports the internal dispatcher's pinned source entry.
"""
import argparse
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from zipfile import ZipFile


def fake_official():
    args = sys.argv[1:]
    def argument(name):
        return args[args.index(name) + 1]

    assert argument('--method') == 'GET', 'this fixture accepts no platform writes'
    profile = argument('--profile')
    assert profile in ('fixture-source', 'fixture-target')
    source = profile == 'fixture-source'
    shop = 11 if source else 12
    url = argument('--url')
    params = json.loads(argument('--params')) if '--params' in args else {}
    response = {'success':True}
    if url.endswith('/venderinfo'):
        response['data'] = {'venderId':shop + 100, 'shopBaseInfo':{
            'shopId':shop, 'shopName':profile, 'type':0, 'status':1}}
    elif url.endswith('/products/{productId}'):
        pid = int(params['productId'])
        assert pid in ((701,) if source else (802, 803))
        skus = [{'skuId':pid * 10 + dim, 'productId':pid, 'saleAttrs':[
            {'attrId':5, 'attrName':'容量', 'dim':1, 'attrValueAlias':label}]}
            for dim,label in ((1, '250 ml'), (2, '500 ml'))]
        response['data'] = {'productInfo':{'productId':pid, 'shopId':shop, 'venderId':shop + 100,
            'productName':'Synthetic vessel', 'categoryDetail':{'lastCategoryId':99},
            'itemNum':'other' if pid == 803 else 'fixture-one'}, 'skuList':skus if source else skus[::-1]}
    elif url.endswith('/products'):
        ids = [701] if source else ([803] if params.get('itemNum') == 'other' else [802])
        response.update(data=[{'productId':pid, 'itemNum':params['itemNum']} for pid in ids],
                        paginationData={'totalItems':len(ids), 'currentPage':1})
    elif url.endswith('/sku-materials'):
        response['data'] = [{'skuId':sid} for sid in params['skuIds']]
    elif url.endswith('/media-zones'):
        response['data'] = [{'zoneId':'fixture-zone', 'zoneType':1}]
    elif url.endswith('/images'):
        response['data'] = []
    else:
        raise AssertionError(url)
    print(json.dumps(response))


def cli_double(directory):
    """Create an executable fixture. Windows uses pip's standard console-script launcher."""
    code = Path(__file__).read_bytes()
    if os.name == 'nt':
        import platform
        from pip._vendor.distlib import scripts
        arch = '64-arm' if platform.machine().lower() == 'arm64' else ('64' if sys.maxsize > 2**32 else '32')
        launcher = Path(scripts.__file__).with_name('t'+arch+'.exe').read_bytes()
        package = BytesIO()
        with ZipFile(package, 'w') as archive:
            archive.writestr('__main__.py', code)
        path = directory / 'fixture-jdo.exe'
        path.write_bytes(launcher + ('#!"' + sys.executable + '"\n').encode() + package.getvalue())
    else:
        path = directory / 'fixture-jdo'
        script = directory / 'fixture.py'
        script.write_bytes(code)
        path.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(script))+' "$@"\n')
        path.chmod(0o755)
    return path


def run(root, merchant=False, prefix=None):
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(tmp)
        tool = state / 'tools/fixture'
        tool.mkdir(parents=True)
        cli = cli_double(tool)
        # The test double and its receipt exist only in this disposable test installation.
        (state / 'tools/official.json').write_text(json.dumps({'root':'tools/fixture',
            'executable':cli.name, 'executableSha256':sha256(cli.read_bytes()).hexdigest()}))
        env = {**os.environ, 'HEIDENG_POP_HOME':tmp, 'HEIDENG_MERCHANT_HOME':tmp,
               'PYTHONUTF8':'1', 'JDO_CLI_PATH':str(cli)}
        if merchant:
            skills = state / 'skills'
            setup = subprocess.run([sys.executable, str(root/'install.py'), '--state',tmp,
                                    '--skills-dir',str(skills),'--offline','--no-auto-update'],
                                   env=env, capture_output=True, text=True, encoding='utf-8',errors='replace')
            assert setup.returncode == 0, (setup.stdout[-1000:], setup.stderr[-500:])
            root = skills / 'heideng-ops-agent-merchant'
        command = prefix or ([sys.executable, str(root/'scripts/merchant.py'), '--state', tmp, 'run', 'shop-copy', '--']
                  if merchant else [sys.executable, str(root/'scripts/jd_product_pipeline.py'), 'shop-copy'])
        args = ['copy-materials']
        for side, shop in [('source',11), ('target',12)]:
            profile = 'fixture-' + side
            cfg = state / (profile+'.json')
            cfg.write_text(json.dumps({'store':{'profile':profile, 'shopId':shop, 'venderId':shop+100,
                                                'shopName':profile}}))
            args.extend(['--'+side+'-profile',profile,'--'+side+'-config',str(cfg)])
        result = subprocess.run(command+args+['--source-product-id','701',
            '--target-product-id','802','--target-product-id','803','--confirm','FILL_MISSING_COPY_MATERIALS'],
            env=env, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=120)
        assert result.returncode == 2, (result.returncode, result.stdout[-1000:], result.stderr[-500:])
        try:
            receipt = json.loads(result.stdout.strip().splitlines()[-1])
            rows = receipt['rows']
        except (ValueError, KeyError, IndexError) as exc:
            raise AssertionError((result.stdout[-1500:], result.stderr[-700:])) from exc
        assert receipt['status'] == 'material-copy-incomplete' and not receipt['ok'], receipt
        assert [(r['targetProductId'], r['migrationStatus']) for r in rows] == [
            (802, 'no-source-materials'), (803, 'unverified-product-pair')], rows
        assert rows[0]['pairedSkuCount'] == 2 and rows[0]['sourceApprovedSlots'] == 0
        assert all(r['platformRequestsIssued'] == 0 and not r['ok'] for r in rows)
        assert len({r['reportFile'] for r in rows}) == 2
        for row in rows:
            local = json.loads(Path(row['reportFile']).read_text(encoding='utf-8'))
            assert local['migrationStatus'] == row['migrationStatus']
        print(json.dumps({'pass':True, 'route':'merchant' if merchant else 'internal',
                          'targets':len(rows), 'writes':0, 'statuses':[r['migrationStatus'] for r in rows]}))


if __name__ == '__main__':
    if sys.argv[1:2] == ['invoke']:
        fake_official()
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--merchant-root', type=Path)
        parser.add_argument('--entry-json')
        options = parser.parse_args()
        root = Path(__file__).resolve().parents[1]
        if options.merchant_root:
            run(options.merchant_root.resolve(), merchant=True)
        else:
            run(root, merchant=(root/'scripts/merchant.py').is_file(),
                prefix=json.loads(options.entry_json) if options.entry_json else None)
