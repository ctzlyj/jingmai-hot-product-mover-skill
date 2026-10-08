import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
# 2026-10-08: pruned tests for decide/pair_accepted/audit/collapsed_source_bands/
# REMAKE_PAIR_*/SINGLE_PROMPT/vision_audit - that visual-detection API was
# removed on 2026-09-30. The surviving tests cover the technical-only helpers
# (duplicate_details, validate_candidate_report, create_manifest_from_listing)
# and the publish_local_draft quality checkpoints.
from image_quality_gate import duplicate_details, validate_candidate_report, PROMPT_VERSION


class ImageQualityGateTests(unittest.TestCase):
    def test_near_duplicate_details_block(self):
        from PIL import Image
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            one, two = Path(tmp)/'one.png', Path(tmp)/'two.png'
            Image.new('RGB',(64,64),'white').save(one)
            Image.new('RGB',(128,128),'white').save(two)
            self.assertEqual(len(duplicate_details([str(one),str(two)])),1)

    def test_taobao_materialize_preserves_untouched_originals(self):
        import io, tempfile
        from PIL import Image
        from jd_product_mover import materialize, read_json
        buffer=io.BytesIO(); Image.new('RGB',(64,64),'blue').save(buffer,format='PNG')
        class StubClient:
            def get_bytes(self,url): return buffer.getvalue()
        source={'sourceSku':'42','title':'source title','sourcePlatform':'taobao',
                'imageQualityGate':'strict','mainImageUrls':['https://source.example/main'],
                'detailImageUrls':['https://source.example/detail'],
                'variants':[{'sourceImageUrl':'https://source.example/sku'}]}
        with tempfile.TemporaryDirectory() as tmp:
            folder=materialize(source,Path(tmp),StubClient())
            stored=read_json(folder/'manifest.json')
            first=stored['mainImages'][0]
            original=folder/stored['sourceImageSnapshot'][first]
            self.assertEqual((folder/first).read_bytes(),original.read_bytes())
            (folder/first).write_bytes(b'changed local image')
            self.assertNotEqual((folder/first).read_bytes(),original.read_bytes())

    def test_taobao_new_link_requires_source_snapshot_and_quality_report(self):
        import tempfile, json, hashlib
        from PIL import Image
        from image_quality_gate import create_manifest_from_listing, file_evidence
        from publish_local_draft import enforce_taobao_quality
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            for directory in ('original','main','detail'):
                (folder/directory).mkdir()
            for path,color in (('original/first.png','white'),('main/first.png','white'),('original/page.png','black'),('detail/page.png','black')):
                Image.new('RGB',(32,32),color).save(folder/path)
            source={'sourcePlatform':'taobao','imageQualityGate':'strict','sourceSku':'ITEM',
                    'mainImages':['main/first.png'],
                    'sourceImageSnapshot':{'main/first.png':'original/first.png','detail/page.png':'original/page.png'}}
            listing={'images':[{'role':'spu_main','order':1,'localPath':'main/first.png'},
                               {'role':'detail','order':1,'localPath':'detail/page.png'}]}
            manifest=create_manifest_from_listing(folder,source,listing)
            self.assertEqual(manifest['items']['ITEM']['sourceFirst'],'original/first.png')
            with self.assertRaisesRegex(Exception,'image quality gate'):
                enforce_taobao_quality(folder,source,listing)
            rows=[]
            for image in manifest['items']['ITEM']['images']:
                rows.append({'role':image['role'],'source':file_evidence(folder/image['source']),
                             'final':file_evidence(folder/image['final']),'decision':'KEEP','pairAccepted':True})
            report={'promptVersion':PROMPT_VERSION,'items':{'ITEM':{'sourceFirst':file_evidence(folder/'original/first.png'),'images':rows}}}
            (folder/'image-quality-acceptance.json').write_text(json.dumps(report),encoding='utf-8')
            enforce_taobao_quality(folder,source,listing)
            from publish_local_draft import taobao_quality_stamp, require_reviewed_upload
            stamp=taobao_quality_stamp(folder,source,listing)
            uploaded=folder/'.pipeline'/'listing.uploaded.json'
            uploaded.parent.mkdir()
            uploaded.write_text(json.dumps(listing),encoding='utf-8')
            sidecar=uploaded.parent/'upload-quality-snapshot.json'
            sidecar.write_text(json.dumps({'reviewSha256':stamp,
                                          'uploadedSha256':hashlib.sha256(uploaded.read_bytes()).hexdigest()}),encoding='utf-8')
            require_reviewed_upload(folder,source,listing,stamp,uploaded)
            report['items']['ITEM']['reviewMarker']='new acceptance pass'
            (folder/'image-quality-acceptance.json').write_text(json.dumps(report),encoding='utf-8')
            with self.assertRaisesRegex(Exception,'snapshot changed'):
                require_reviewed_upload(folder,source,listing,stamp,uploaded)
            report['items']['ITEM'].pop('reviewMarker')
            (folder/'image-quality-acceptance.json').write_text(json.dumps(report),encoding='utf-8')
            uploaded.write_text('{}',encoding='utf-8')
            with self.assertRaisesRegex(Exception,'does not match'):
                require_reviewed_upload(folder,source,listing,stamp,uploaded)
            Image.new('RGB',(32,32),'red').save(folder/'main/first.png')
            with self.assertRaisesRegex(Exception,'image quality gate'):
                enforce_taobao_quality(folder,source,listing)

    def test_taobao_old_upload_checkpoint_stops_before_validation_or_product_creation(self):
        import json, tempfile
        from types import SimpleNamespace
        from unittest import mock
        from PIL import Image
        from image_quality_gate import create_manifest_from_listing, file_evidence, PROMPT_VERSION
        import publish_local_draft as draft
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            for name in ('original','main'):
                (folder/name).mkdir()
            for name in ('original/first.png','main/first.png'):
                Image.new('RGB',(32,32),'white').save(folder/name)
            source={'sourcePlatform':'taobao','imageQualityGate':'strict','sourceSku':'ITEM',
                    'mainImages':['main/first.png'],
                    'sourceImageSnapshot':{'main/first.png':'original/first.png'}}
            listing={'product':{'itemNumber':'ABC','vendorSkuPrefix':'TST','vendorStyleStart':1},
                     'category':{'id':123},
                     'images':[{'role':'spu_main','order':1,'localPath':'main/first.png','imageKey':'first'}]}
            manifest=create_manifest_from_listing(folder,source,listing)
            rows=[{'role':row['role'],'source':file_evidence(folder/row['source']),
                   'final':file_evidence(folder/row['final']),
                   'decision':'KEEP','pairAccepted':True}
                  for row in manifest['items']['ITEM']['images']]
            report={'promptVersion':PROMPT_VERSION,'items':{'ITEM':{
                'sourceFirst':file_evidence(folder/'original/first.png'),'images':rows}}}
            for name,data in (('manifest.json',source),('listing.json',listing),('state.json',{}),
                              ('image-quality-acceptance.json',report)):
                (folder/name).write_text(json.dumps(data),encoding='utf-8')
            runtime=folder/'.pipeline';runtime.mkdir()
            (runtime/'listing.uploaded.json').write_text(json.dumps(listing),encoding='utf-8')
            for name in ('store.json','rules.json','reference.json'):
                (folder/name).write_text('{}',encoding='utf-8')
            args=SimpleNamespace(folder=folder,confirm='PUBLISH_TO_JD',profile='test',
                                 store_config=folder/'store.json',rules=folder/'rules.json',
                                 reference=folder/'reference.json')
            with (mock.patch.object(draft.pipeline,'effective_rules',return_value={}),
                  mock.patch.object(draft.pipeline,'verify_store_identity',return_value={}),
                  mock.patch.object(draft.jingmai,'validate_manifest',return_value=([],[])),
                  mock.patch.object(draft,'existing_product_id',return_value=(0,None)),
                  mock.patch.object(draft.pipeline,'next_style_start',return_value=(1,[])),
                  mock.patch.object(draft.jingmai,'run_jdo_large',side_effect=AssertionError('network called')) as network):
                with self.assertRaisesRegex(Exception,'uploaded media does not match'):
                    draft.publish_local_draft(args)
            network.assert_not_called()
            self.assertFalse((folder/'.publish.lock').exists())

    def test_detail_sequence_change_invalidates_approval(self):
        import tempfile
        from PIL import Image
        from image_quality_gate import file_evidence
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);paths=[root/name for name in ('first.png','a.png','b.png')]
            for i,path in enumerate(paths): Image.new('RGB',(48,48),('white','black','red')[i]).save(path)
            first,a,b=paths
            records=[{'role':role,'source':file_evidence(path),'final':file_evidence(path),
                      'decision':'KEEP','pairAccepted':True} for role,path in
                     [('main1',first),('detail',a),('detail',b)]]
            report={'promptVersion':PROMPT_VERSION,'items':{'x':{'sourceFirst':file_evidence(first),'images':records}}}
            candidate={'sourceItemId':'x','mainImages':[str(first)],'detailImages':[str(b),str(a)],'skus':[]}
            self.assertIn('image-quality-order-mismatch',validate_candidate_report(candidate,report,file_evidence(first)))

    def test_report_requires_current_files_and_source_first(self):
        import tempfile, hashlib
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            original=root/'source.png'; final=root/'final.png'; detail=root/'detail.png'
            from PIL import Image, ImageDraw
            for i,path in enumerate((original,final,detail)):
                image=Image.new('RGB',(64,64),('white','gray','black')[i]); ImageDraw.Draw(image).rectangle((i*4,8,25+i*4,40),fill='red'); image.save(path)
            record=lambda path: {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
            candidate={'sourceItemId':'123','mainImages':[str(final)],'detailImages':[str(detail)],'skus':[]}
            report={'promptVersion':PROMPT_VERSION,'items':{'123':{'sourceFirst':record(original), 'images':[
                {'role':'main1','source':record(original),'final':record(final),'decision':'KEEP','pairAccepted':True},
                {'role':'detail','source':record(detail),'final':record(detail),'decision':'KEEP','pairAccepted':True}]}}}
            self.assertEqual(validate_candidate_report(candidate,report,record(original)),[])
            final.write_bytes(b'changed')
            self.assertTrue(any('stale' in e for e in validate_candidate_report(candidate,report,record(original))))
            self.assertTrue(validate_candidate_report(candidate,report,record(detail)))

    def test_report_version_and_identical_detail_main_block(self):
        import shutil,tempfile
        from PIL import Image, ImageDraw
        from image_quality_gate import file_evidence
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            first=root/'first.png';same=root/'detail.png'
            image=Image.new('RGB',(64,64),'white')
            ImageDraw.Draw(image).rectangle((10,15,40,30),fill='red')
            image.save(first);shutil.copyfile(first,same)
            candidate={'sourceItemId':'x','mainImages':[str(first)],'detailImages':[str(same)],'skus':[]}
            rows=[{'role':role,'source':file_evidence(p),'final':file_evidence(p),'decision':'KEEP','pairAccepted':True}
                  for role,p in [('main1',first),('detail',same)]]
            report={'promptVersion':PROMPT_VERSION,'items':{'x':{'sourceFirst':file_evidence(first),'images':rows}}}
            self.assertIn('detail-duplicates-main:'+str(same),validate_candidate_report(candidate,report,file_evidence(first)))
            old=dict(report,promptVersion=PROMPT_VERSION-1)
            self.assertIn('image-quality-report-version-mismatch',validate_candidate_report(candidate,old,file_evidence(first)))

    def test_remake_fidelity_report_modes_are_not_interchangeable(self):
        import hashlib, tempfile
        from PIL import Image
        from image_quality_gate import REMAKE_PROMPT_VERSION, validate_candidate_report
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = root / 'source.png'
            final = root / 'remade.png'
            Image.new('RGB', (64, 64), 'white').save(original)
            Image.new('RGB', (64, 64), 'white').save(final)
            def record(path):
                return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            candidate = {'sourceItemId': '123', 'mainImages': [str(final)], 'detailImages': [], 'skus': [],
                         'gateMode': 'remake'}
            row = {'role': 'main1', 'source': record(original), 'final': record(final),
                   'decision': 'KEEP', 'pairAccepted': True}
            remake_report = {'promptVersion': REMAKE_PROMPT_VERSION, 'gateMode': 'remake',
                             'items': {'123': {'sourceFirst': record(original), 'images': [row]}}}
            self.assertEqual(validate_candidate_report(candidate, remake_report, record(original)), [])
            fidelity_report = dict(remake_report, promptVersion=PROMPT_VERSION, gateMode='fidelity')
            errors = validate_candidate_report(candidate, fidelity_report, record(original))
            self.assertIn('image-quality-report-version-mismatch', errors)
            self.assertIn('image-quality-mode-mismatch', errors)


if __name__ == '__main__': unittest.main()
