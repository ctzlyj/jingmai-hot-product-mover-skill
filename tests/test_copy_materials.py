"""Exercise the merchant shop-copy command with two synthetic live product catalogues."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE={'profile':'lantern-source','shopName':'甲灯具','shopId':'910','venderId':'110'}
TARGET={'profile':'lantern-target','shopName':'乙灯具','shopId':'920','venderId':'120'}


def sample(target=False):
    pid=802 if target else 701
    first=300 if target else 200
    prefix='target' if target else 'source'
    def group(gid,urls):
        return {'uuid':gid,'imageInfoList':[{'orderSort':pos,'imgUrl':url} for pos,url in enumerate(urls,1)]}
    groups=[group('0000000000',['shared','features'])]
    rows=[]
    specs=[]
    for n,color in enumerate(('白','蓝'),1):
        sid=first+n
        gid=prefix+'group'+str(n)
        groups.append(group(gid,[f'hero{n}',f'angle{n}',f'size{n}']))
        specs.append({'uuid':str(sid),'imageUrl':f'spec{n}'})
        rows.append({'skuId':sid,'productId':pid,'outerId':('000134','LED/A-9')[n-1],'logo':f'hero{n}',
                     'saleAttrs':[{'attrId':25,'attrName':'饰面','attrValueAlias':color,'attrValues':gid,'dim':1,'attrValuesSeqNo':n}],
                     'priceInfo':{'jdPrice':('18.50','22.75')[n-1]},'stockNum':(0,17)[n-1],'skuEnableStatus':1,
                     'upcCode':'', 'goodsAttrInfos':[]})
    return {'productInfo':{'productId':pid,'productName':'合成阅读灯','brandInfo':{'brandId':12},'itemNum':'L-008',
                           'categoryDetail':{'lastCategoryId':800},'goodsAttrInfos':[],'length':210,'wide':120,
                           'height':80,'weight':'.52','packSpecification':'',
                           'productDetailDesc':{'desc':'<p>使用说明<img src="https://img10.360buyimg.com/imgzone/jfs/detail1"><img src="https://img10.360buyimg.com/imgzone/jfs/detail2"></p>',
                                                'mobileDesc':'<p><img data-src="https://img10.360buyimg.com/imgzone/jfs/mobile1"></p>'}},
            'skuList':list(reversed(rows)) if target else rows,
            'material':{'mainImages':groups,'rectangleImages':[],'ggtImages':specs}}


def reference():
    codes=('WareTitle WareItemnum WareBrand WareSkuImg WareEditor WareLength WareWide WareHeight WareWeight '
           'SKUS SKU SkuVendorSku SkuJdprice SkuStockNum SkuEnableStatus SkuMainImage VenderCategoryValue sale-prop-25')
    return {'shopIdentity':TARGET,'category':{'id':800},'brands':[{'brandId':12}],
            'schema':{'componentList':[{'componentCode':c} for c in codes.split()]}}





def scene(order, path, status=4):
    return {'materialType':32,'order':order,'status':status,
            'imageMaterial':{'skuImages':[{'index':1,'imgUrl':'jfs/'+path}]}}


class CopyMaterialTests(unittest.TestCase):
    def setUp(self):
        self.root=Path(self.enterContext(tempfile.TemporaryDirectory()))
        project=Path(__file__).resolve().parents[1]
        if (project / 'scripts/merchant.py').is_file():
            import heideng_merchant
            project = Path(heideng_merchant.__file__).resolve().parents[1]
            self.state_env = 'HEIDENG_MERCHANT_HOME'
            self.enterContext(patch.object(sys, 'path', [str(project/'mature/listing/scripts'), *sys.path]))
            from heideng_merchant import shop_copy_command as entry, shop_copy_materials, safety, pop_copy_titles
        else:
            self.state_env = 'HEIDENG_POP_HOME'
            self.enterContext(patch.object(sys, 'path', [str(project/'scripts'), *sys.path]))
            from pop_copy import entry, shop_copy_materials, safety, pop_copy_titles
        self.enterContext(patch.dict(os.environ, {self.state_env:str(self.root)}))
        self.materials = shop_copy_materials
        self.titles = pop_copy_titles
        self.safety = safety
        self.entry=entry
        self.details={'701':sample(),'802':sample(True)}
        self.source={'201':[scene(1,'red-a.jpg'),scene(2,'red-b.jpg')],'202':[]}
        self.target={'301':[],'302':[]}
        self.posts=[]
        self.unknown_rows=set()
        self.explicit_empty=set()
        self.mode='accept'
        self.fresh_hook=None
        self.get_count={}
        self.duplicates=set()
        def verify(config,profile):
            self.active=SOURCE if profile==SOURCE['profile'] else TARGET
            return self.active
        self.enterContext(patch.object(entry,'verify',side_effect=verify))
        self.enterContext(patch.object(entry,'read_owned_detail',side_effect=self.read_detail))
        def products(item_num,require_success):
            self.assertTrue(require_success)
            pid='701' if self.active==SOURCE else '802'
            product=self.details[pid]['productInfo']
            rows=[dict(product)] if product['itemNum']==item_num else []
            if pid in self.duplicates:rows.append({**product,'productId':9001})
            return rows
        self.enterContext(patch.object(entry.pipeline,'list_products',side_effect=products))
        self.enterContext(patch.object(entry.api,'recheck_verified_jdo_store',side_effect=lambda:self.active))
        self.enterContext(patch.object(entry.api,'get',side_effect=self.get))
        self.enterContext(patch.object(entry.api,'run_jdo_large',side_effect=self.post))

    def read_detail(self,pid):
        self.assertEqual(self.active,SOURCE if pid==701 else TARGET)
        return deepcopy(self.details[str(pid)])

    def get(self,skill,url,params=None):
        if url.endswith('media-zones'):
            self.assertEqual(self.active,SOURCE)
            return {'success':True,'data':[{'zoneId':'src-zone','zoneType':1}]}
        if url.endswith('/images'):
            self.assertEqual(self.active,SOURCE)
            return {'success':True,'data':[{'imgId':'image-1','imgName':'candidate.jpg','imgUrl':'jfs/candidate.jpg'}]}
        if url.endswith('health-infos'):
            return {'success':True,'data':[{'skuId':301,'healthScore':82,'improvementList':[
                {'improvementDetail':{'troubleCode':'hasShortTitle'}}]}],
                'paginationData':{'page':1,'totalItems':1}}
        self.assertTrue(url.endswith('/sku-materials'))
        source=self.active==SOURCE
        materials=self.source if source else self.target
        ids=[str(v) for v in params['skuIds']]
        types=tuple(params['materialTypes'])
        which=('src' if source else 'dst',tuple(ids),types)
        self.get_count[which]=self.get_count.get(which,0)+1
        if not source and self.fresh_hook:
            self.fresh_hook(ids,types)
        rows=[]
        for sid in ids:
            if (source,sid) in self.unknown_rows:
                continue
            found=[deepcopy(m) for m in materials[sid] if m['materialType'] in types]
            rows.append({'skuId':int(sid),**({'materials':found} if found or (source,sid) in self.explicit_empty else {})})
        return {'success':True,'data':rows}

    def post(self,**kwargs):
        self.assertEqual(self.active,TARGET)
        self.assertEqual(kwargs['method'],'POST')
        body=json.loads(kwargs['body_path'].read_bytes())
        self.posts.append(body)
        if self.mode in ('reject','system','partial'):
            code='1000000001' if self.mode=='system' else '10109002101'
            reply={'success':False,'errorList':[{'code':code,'message':'upstream timeout' if self.mode=='system' else 'SKU数据不存在'}]}
            if self.mode=='partial':reply['successCount']=1
            return self.entry.api.parse_response(json.dumps(reply),method=kwargs['method'],url=kwargs['url'])
        if self.mode=='raw-system':
            return {'success':False,'errorList':[{'code':'1000000001','message':'upstream timeout'}]}
        if self.mode=='lost':
            raise TimeoutError('响应丢失')
        for row in body['skuMaterials']:
            self.assertEqual(row['productId'],802)
            self.target[str(row['skuId'])].extend(dict(m,status=4) for m in row['materials'])
        if self.mode=='lost-after-write':
            raise TimeoutError('回执丢失，远端已写')
        return {'success':True,'data':{}}

    def call(self,write=False,health=False,history=None,pid=802):
        argv=['copy-materials','--source-profile',SOURCE['profile'],'--source-config','source.json',
              '--source-product-id','701','--target-profile',TARGET['profile'],'--target-config','target.json',
              '--target-product-id',str(pid)]
        if history:
            argv+=['--history-dir',str(history)]
        if write:
            argv+=['--confirm','FILL_MISSING_COPY_MATERIALS']
        if health:
            argv+=['--health']
        out=StringIO()
        with redirect_stdout(out):
            code=self.entry.main(argv)
        receipt=json.loads(out.getvalue())
        self.assertEqual(code,0 if receipt['ok'] else 2)
        return receipt,json.loads(Path(receipt['reportFile']).read_bytes())

    def test_empty_identified_sku_fills_only_missing_slot_and_keeps_existing_order(self):
        self.target['301']=[scene(1,'red-a.jpg')]
        self.target['302']=[scene(1,'existing.jpg')]
        data,report=self.call()
        self.assertEqual((data['sourceApprovedSlots'],data['matchedApprovedSlots']),(2,1))
        self.assertEqual(data['counts']['target-only-preserved'],1)
        self.assertFalse(self.posts)
        data,report=self.call(write=True)
        self.assertEqual(data['migrationStatus'],'verified')
        self.assertEqual(data['matchedApprovedSlots'],2)
        self.assertEqual(data['targetSlotCensus'],{'approved':3,'missing':13})
        self.assertEqual(len(self.posts),1)
        self.assertEqual([m['order'] for m in self.posts[0]['skuMaterials'][0]['materials']],[2])
        self.assertEqual(report['preservedSlotDifferences'],[])
        self.call(write=True)
        self.assertEqual(len(self.posts),1)

    def test_source_has_only_carousel_and_health_is_an_independent_sku_denominator(self):
        self.source['201']=[]
        self.explicit_empty.add((True,'202'))
        data,report=self.call(write=True,health=True)
        self.assertEqual(data['migrationStatus'],'no-source-materials')
        self.assertEqual(data['sourceSlotsReturned'],0)
        self.assertEqual(data['targetSlotCensus']['missing'],16)
        self.assertEqual(data['targetHealthStatus'],'partial')
        self.assertEqual(report['targetHealth']['unreportedSkus'],['302'])
        self.assertEqual(data['healthImprovementCounts'],{'hasShortTitle':1})
        self.assertEqual(report['sourceReadShapes']['201'][0]['shape'],'identified-sku-only')
        self.assertEqual(report['sourceReadShapes']['202'][0]['shape'],'materials-array')
        self.assertEqual(report['sourceImageSpace']['queries'][0]['candidateCount'],1)
        self.assertFalse(report['sourceImageSpace']['bindingProven'])
        self.assertFalse(self.posts)

    def test_text_and_region_are_compared_in_full_and_audit_is_reported_independently(self):
        text={'materialType':1002,'order':1,'textMaterial':{'contents':['原句1','原句2']},'status':4}
        self.source['201']=[text,scene(1,'cup.jpg')]
        self.source['201'][1]['imageMaterial']['region']={'x':10}
        self.target['301']=[dict(deepcopy(text),status=3),scene(1,'cup.jpg')]
        data,report=self.call(write=True)
        self.assertEqual(data['counts']['target-pending'],1)
        self.assertEqual(data['counts']['different'],1)
        observed=next(c for c in report['checks'] if c['type']==1002 and c['source'])
        self.assertEqual(observed['source']['content']['textMaterial']['contents'],['原句1','原句2'])
        self.assertEqual(observed['target']['raw']['status'],3)
        self.assertFalse(self.posts)

    def test_omitted_sku_row_stays_unknown_and_does_not_prompt_new_image(self):
        self.unknown_rows.add((False,'301'))
        data,report=self.call(write=True)
        self.assertEqual(data['status'],'material-copy-incomplete')
        self.assertEqual(data['counts']['target-unreported'],8)
        self.assertTrue(report['targetReadGaps']['301'])
        self.assertFalse(self.posts)

    def test_business_rejection_and_untrusted_exception_keep_distinct_original_receipts(self):
        self.mode='reject'
        data,report=self.call(write=True)
        original=list(report['originalRequests'].values())[0]
        self.assertEqual(original['platformCodes'],['10109002101'])
        self.assertEqual(original['requestStatus'],'business-rejected')
        self.assertEqual(original['readback'],{'missing':2})
        self.assertFalse(data['ok'])
        self.call(write=True)
        self.assertEqual(len(self.posts),1)
        self.mode='accept'
        self.source['201'][0]=scene(1,'corrected.jpg')
        self.call(write=True)
        self.assertEqual(len(self.posts),2)
        self.assertEqual([item['order'] for item in self.posts[1]['skuMaterials'][0]['materials']],[1])

    def test_unknown_no_replay_and_later_readback_still_compares_original_content(self):
        self.mode='lost'
        data,report=self.call(write=True)
        self.assertEqual(next(iter(report['originalRequests'].values()))['requestStatus'],'outcome-unknown')
        self.assertFalse(data['ok'])
        self.source['201'][0]=scene(1,'new-source.jpg')
        self.target['301']=[scene(1,'red-a.jpg'),scene(2,'red-b.jpg')]
        data,report=self.call(write=True)
        self.assertEqual(data['counts']['different'],1)
        self.assertEqual(next(iter(report['originalRequests'].values()))['readback'],{'matched-approved':2})
        self.assertEqual(len(self.posts),1)

    def test_response_lost_after_landing_and_concurrent_target_edit_are_observed(self):
        def fill_one(ids,types):
            if ids==['301'] and 32 in types:
                self.target['301']=[scene(1,'colleague.jpg')]
                self.fresh_hook=None
        self.fresh_hook=fill_one
        self.mode='lost-after-write'
        data,report=self.call(write=True)
        self.assertEqual(data['migrationStatus'],'incomplete')
        self.assertEqual(report['changedBeforeWrite'],['301/32/1'])
        self.assertEqual([m['order'] for m in self.posts[0]['skuMaterials'][0]['materials']],[2])
        self.assertEqual(next(iter(report['originalRequests'].values()))['readback'],{'matched-approved':1})
        self.assertEqual(self.target['301'][0]['imageMaterial']['skuImages'][0]['imgUrl'],'jfs/colleague.jpg')

    def test_batch_health_reads_merchant_pages_once_then_filters_each_product(self):
        _health = self.materials._health
        self.active=TARGET
        cache={}
        api=self.entry.api
        with patch.object(api,'get',side_effect=self.get) as call:
            first=_health(api,{'301':{}},TARGET,cache)
            second=_health(api,{'302':{}},TARGET,cache)
        self.assertEqual(call.call_count,1)
        self.assertEqual(first['status'],'observed')
        self.assertEqual(second['unreportedSkus'],['302'])
        self.assertEqual(second['returnedSkuCount'],0)

    def test_removed_live_sku_cannot_receive_a_frozen_source_mapping(self):
        target_reads=0
        def change_sku(pid):
            nonlocal target_reads
            if pid==802:
                target_reads+=1
                if target_reads==2:
                    self.details['802']['skuList']=[row for row in self.details['802']['skuList'] if row['skuId']!=301]
            return self.read_detail(pid)
        with (patch.object(self.entry,'read_owned_detail',side_effect=change_sku),
              patch.object(self.entry,'reference',return_value=None)):
            data,report=self.call(write=True)
        self.assertFalse(data['ok'])
        self.assertEqual(report['changedBeforeWrite'],['301/32/1','301/32/2'])
        self.assertEqual(report['targetSkuCount'],1)
        self.assertFalse(self.posts)

    def test_stopped_batch_cannot_create_new_material_intent(self):
        from argparse import Namespace
        handle = self.materials.handle
        class Stopped(Exception):
            pass
        def stop():
            raise Stopped()
        options=Namespace(source_profile=SOURCE['profile'],target_profile=TARGET['profile'],
                          source_config=None,target_config=None,source_product_id=701,target_product_id=802,
                          confirm='FILL_MISSING_COPY_MATERIALS',health=False,_before_write=stop)
        with self.assertRaises(Stopped):
            handle(options,self.entry)
        self.assertFalse(self.posts)
        self.assertEqual(list(self.root.rglob('requests.json')),[])

    def test_same_colors_on_unrelated_products_never_authorize_material_writes(self):
        product=self.details['802']['productInfo']
        product.update(productName='另一款灯',itemNum='DIFFERENT',categoryDetail={'lastCategoryId':909})
        data,report=self.call(write=True)
        self.assertFalse(data['ok'])
        self.assertEqual(report['migrationStatus'],'unverified-product-pair')
        self.assertEqual(set(report['productPair']['conflicts']),{'title','itemNumber','category'})
        self.assertEqual(data['pairedSkuCount'],0)
        self.assertEqual(self.posts,[])
        self.assertEqual(list(self.root.rglob('requests.json')),[])

    def test_same_title_and_item_number_do_not_disambiguate_duplicate_target(self):
        self.duplicates.add('802')
        data,report=self.call(write=True)
        self.assertFalse(data['ok'])
        self.assertEqual(report['productPair']['targetMatches'],['802','9001'])
        self.assertFalse(self.posts)

    def legacy_history(self):
        atomic_json, file_sha = self.safety.atomic_json, self.safety.file_sha
        history=self.root/'old-run'
        member=history/'batch5/701'
        folder=member/'201_lamp'
        archive=folder/'.pipeline/publish'
        variants=[{'sourceSku':'201'},{'sourceSku':'202'}]
        title=self.details['701']['productInfo']['productName']
        atomic_json(folder/'manifest.json',{'sourceSpu':'701','sourceSku':'201','sourceVenderId':SOURCE['venderId'],
                                           'variants':variants})
        atomic_json(folder/'listing.json',{'source':{'spu':'701','sku':'201'},'product':{'title':title,'itemNumber':'701'},
                                          'category':{'id':800},'skus':variants})
        schema=[{'componentCode':'WareTitle','value':title},{'componentCode':'WareItemnum','value':'701'}]
        atomic_json(archive/'publish-body.json',{'schema':json.dumps(schema),'productApplyDTO':{'categoryDTO':{'lastCategoryId':800}}})
        atomic_json(archive/'publish-response.json',{'success':True,'data':{'applyId':'900','productId':803}})
        report={'ok':False,'productId':802,'productName':title,'expectedSkuCount':2,'actualSkuCount':2,
                'productStatus':103,'verifiedAt':'2026-01-10T08:00:00+08:00','errors':['图片不足']}
        atomic_json(archive/'verification.json',report)
        state={'sourceSpu':701,'sourceSku':201,'store':TARGET,'productId':803,'applyId':900,'verification':report,
               'folder':str(folder),'payloadSha256':file_sha(archive/'publish-body.json')}
        state_path=member/'state/201.json'
        atomic_json(state_path,state)
        self.details['802']['productInfo']['itemNum']='701'
        self.details['803']=deepcopy(self.details['802'])
        self.details['803']['productInfo']['productId']=803
        for row in self.details['803']['skuList']:
            row.update(productId=803,skuId=row['skuId']+100)
            self.target[str(row['skuId'])]=[]
        return history,archive,state_path,state

    def test_legacy_original_receipt_and_separate_failed_readback_prove_their_own_live_product(self):
        history,archive,state_path,state=self.legacy_history()
        published,_=self.call(history=history,pid=803)
        self.assertEqual(published['productPair']['basis'],'legacy-accepted-publication')
        self.assertTrue(published['productPair']['recordedItemNumberChange'])
        self.assertEqual(published['pairedSkuCount'],2)
        data,report=self.call(history=history,write=True)
        self.assertEqual(data['migrationStatus'],'verified')
        self.assertEqual(report['productPair']['basis'],'legacy-product-readback')
        self.assertFalse(report['productPair']['history']['readbackPassed'])
        self.assertEqual({r['skuId'] for body in self.posts for r in body['skuMaterials']},{301})
        self.assertTrue(all(not self.target[tid] for tid in ('401','402')))

    def test_legacy_state_candidate_or_unrelated_product_file_cannot_authorize(self):
        atomic_json = self.safety.atomic_json
        history,archive,state_path,state=self.legacy_history()
        other=history/'batch5/999/other/.pipeline/publish/verification.json'
        atomic_json(other,state['verification'])
        for change in ({'verification':str(other)}, {'folder':str(other.parents[2])}, {'store':SOURCE},
                       {'sourceSpu':999}, {'verification':{**state['verification'],'actualSkuCount':1}}):
            with self.subTest(change=list(change)):
                atomic_json(state_path,{**state,**change})
                data,_=self.call(history=history,write=True)
                self.assertFalse(data['productPair']['verified'])
                self.assertEqual(data['productPair']['readOnlySpecificationMatches'],2)
        atomic_json(state_path,state)
        (archive/'verification.json').unlink()
        self.assertFalse(self.call(history=history,write=True)[0]['productPair']['verified'])
        atomic_json(archive/'publish-response.json',{'success':False,'data':{'productId':803,'applyId':900}})
        self.assertFalse(self.call(history=history,pid=803,write=True)[0]['productPair']['verified'])
        self.assertFalse(self.posts)

    def test_legacy_path_readback_hash_and_current_target_are_checked_at_their_own_boundaries(self):
        atomic_json = self.safety.atomic_json
        history,archive,state_path,state=self.legacy_history()
        atomic_json(state_path,{**state,'verification':str(archive/'verification.json'),'payloadSha256':'f'*64})
        published,_=self.call(history=history,pid=803,write=True)
        self.assertFalse(published['productPair']['verified'])
        self.assertEqual(self.call(history=history)[0]['productPair']['basis'],'legacy-product-readback')
        target_reads=0
        def changed(pid):
            nonlocal target_reads
            if pid==802:
                target_reads+=1
                if target_reads==2:
                    self.details['802']['productInfo']['height']=990
            return self.read_detail(pid)
        with patch.object(self.entry,'read_owned_detail',side_effect=changed):
            data,report=self.call(history=history,write=True)
        self.assertFalse(data['productPair']['verified'])
        self.assertEqual(data['productPair']['conflicts'],['height','itemNumber'])
        self.assertEqual(data['productPair']['directHistoryReceipts'][0]['basis'],'legacy-product-readback')
        self.assertEqual(report['changedBeforeWrite'],['301/32/1','301/32/2'])
        self.assertFalse(self.posts)

    def test_legacy_receipt_rechecks_live_facts_and_only_independent_sku_matches_can_write(self):
        history,archive,state_path,state=self.legacy_history()
        product=self.details['802']['productInfo']
        for changes in ({'brandInfo':{'brandId':99}},{'categoryDetail':{'lastCategoryId':11}},
                        {'width':999},{'weight':12},{'itemNum':'UNRECORDED'}):
            with self.subTest(changes=changes),patch.dict(product,changes):
                data,_=self.call(history=history,write=True)
                self.assertFalse(data['productPair']['verified'])
        self.assertFalse(self.posts)
        self.details['802']['skuList'][0]['saleAttrs']=deepcopy(self.details['802']['skuList'][1]['saleAttrs'])
        with patch.object(self.entry,'reference',return_value=None):
            data,_=self.call(history=history,write=True)
        self.assertTrue(data['productPair']['verified'])
        self.assertEqual(data['pairedSkuCount'],0)
        self.assertFalse(self.posts)

    def test_legacy_proven_origin_with_no_approved_source_slots_never_claims_migration(self):
        history,archive,state_path,state=self.legacy_history()
        self.source['201']=[]
        data,report=self.call(history=history,write=True)
        self.assertEqual(data['migrationStatus'],'no-source-materials')
        self.assertEqual(data['sourceApprovedSlots'],0)
        self.assertTrue(report['productPair']['verified'])
        self.assertFalse(self.posts)

    def test_timeout_code_and_partial_results_keep_slots_reserved_after_source_changes(self):
        for mode in ('system','raw-system','partial'):
            with (self.subTest(mode=mode), tempfile.TemporaryDirectory() as isolated,
                  patch.dict(os.environ,{self.state_env:isolated})):
                self.posts.clear()
                self.mode=mode
                data,report=self.call(write=True)
                self.assertFalse(data['ok'])
                original=next(iter(report['originalRequests'].values()))
                self.assertEqual(original['requestStatus'],'outcome-unknown')
                self.source['201'][0]=scene(1,'changed-'+mode+'.jpg')
                self.mode='accept'
                data,report=self.call(write=True)
                self.assertEqual(data['platformRequestsIssued'],0)
                self.assertEqual(len(self.posts),1)

    def test_legacy_rejected_label_without_route_and_no_partial_proof_stays_reserved(self):
        self.mode='reject'
        self.call(write=True)
        marker=next(self.root.rglob('rejection.json'))
        marker.unlink()
        self.source['201'][0]=scene(1,'changed.jpg')
        self.mode='accept'
        data,report=self.call(write=True)
        self.assertEqual(next(iter(report['originalRequests'].values()))['requestStatus'],'outcome-unknown')
        self.assertEqual(data['platformRequestsIssued'],0)
        self.assertEqual(len(self.posts),1)


    def test_short_titles_pair_by_spec_and_preserve_existing_target(self):
        from types import SimpleNamespace
        src = self.details['701']
        dst = self.details['802']
        for sku in src['skuList']:
            sku['features'] = {'shortTitle':'来源规格'+str(sku['skuId'])}
        for sku in dst['skuList']:
            sku['features'] = {'shortTitle':'原目标标题' if sku['skuId'] == 301 else ''}
        frozen = self.materials.copy.freeze_copy(SOURCE, TARGET, src)
        self.active = TARGET
        proof, rows, _ = self.titles._observe(self.entry, frozen, ['701'], dst,
                                               SimpleNamespace(valid_short_title=lambda text: bool(text)), ())
        self.assertTrue(proof['verified'])
        self.assertEqual({r['targetSkuId']:(r['sourceSkuId'],r['status']) for r in rows},
                         {'301':('201','target-preserved'), '302':('202','ready')})
        self.assertFalse(self.posts)

    def test_multiple_targets_keep_both_historical_links(self):
        history, _, _, _ = self.legacy_history()
        argv = ['copy-materials', '--source-profile', SOURCE['profile'], '--source-config', 'source.json',
                '--source-product-id', '701', '--target-profile', TARGET['profile'], '--target-config', 'target.json',
                '--target-product-id', '803', '--target-product-id', '802', '--history-dir', str(history)]
        out = StringIO()
        with redirect_stdout(out):
            self.assertEqual(self.entry.main(argv), 0)
        rows = json.loads(out.getvalue())['rows']
        self.assertEqual([(row['targetProductId'], row['productPair']['basis']) for row in rows],
                         [(803, 'legacy-accepted-publication'), (802, 'legacy-product-readback')])
        self.assertTrue(all(row['pairedSkuCount'] == 2 for row in rows))
        self.assertNotEqual(rows[0]['reportFile'], rows[1]['reportFile'])
        self.assertEqual(self.posts, [])


class PopCopyCliTests(unittest.TestCase):
    def test_empty_source_never_delivers_a_copy(self):
        import runpy
        run_contract = runpy.run_path(str(Path(__file__).with_name('pop_copy_contract.py')))['run']
        root = Path(__file__).resolve().parents[1]
        run_contract(root, merchant=(root/'scripts/merchant.py').is_file())


if __name__=='__main__':
    unittest.main()
