import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts' / 'ni'))
from image_quality_gate import decide, pair_accepted, duplicate_details, validate_candidate_report

class ImageQualityGateTests(unittest.TestCase):
    def test_patent_and_platform_pages_delete_even_if_flash_misses(self):
        for flag in ('PATENT', 'TAOJINBI', 'PRICE_EXPLANATION', 'PLATFORM_DELIVERY', 'PLATFORM_PROMOTION'):
            lite = {'flags':[flag], 'violations':[], 'legible':True, 'layoutOk':True, 'valuable':True}
            flash = {'flags':[], 'violations':[], 'legible':True, 'layoutOk':True, 'valuable':True}
            self.assertEqual(decide('detail', lite, flash), 'DELETE')
            self.assertEqual(decide('main1', lite, flash), 'BLOCK', 'first image must not silently disappear')

    def test_local_violation_is_edit_not_whole_image_delete(self):
        bad = {'flags':['SMALL_VIOLATION'], 'violations':[{'term':'旗舰店'}], 'legible':True,'layoutOk':True,'valuable':True}
        self.assertEqual(decide('main1', bad, bad), 'EDIT')
        self.assertEqual(decide('detail', bad, bad), 'EDIT')
        self.assertEqual(decide('detail', {'flags':['DENSE_VIOLATIONS'],'legible':True,'layoutOk':True,'valuable':False}), 'DELETE')

    def test_bad_type_and_unreadable_fail_closed(self):
        self.assertEqual(decide('detail', {'flags':[], 'legible':False,'layoutOk':True,'valuable':True}), 'BLOCK')
        self.assertEqual(decide('detail', {'flags':[], 'violations':[], 'legible':True,'layoutOk':True,'valuable':True}, None), 'KEEP')
        self.assertEqual(decide('detail', {'flags':[], 'legible':True}, None), 'BLOCK')

    def test_pair_refuses_first_image_text_loss_garbled_font_and_holes(self):
        good = {'productSame':True,'violationsCleared':True,'noNewArtifacts':True,'qualityOk':True,
                'layoutPreserved':True,'safeTextPreserved':True,'textLegible':True,'noHoles':True,
                'sourceFirstMatched':True}
        self.assertTrue(pair_accepted('main1', good))
        for key in ('layoutPreserved','safeTextPreserved','textLegible','noHoles','sourceFirstMatched'):
            broken = dict(good, **{key:False})
            self.assertFalse(pair_accepted('main1',broken),key)
        self.assertFalse(pair_accepted('detail', dict(good,textLegible=False)))
        self.assertFalse(pair_accepted('detail', {'productSame':True,'violationsCleared':True,'noNewArtifacts':True,'qualityOk':True}))

    def test_near_duplicate_details_block(self):
        from PIL import Image
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as tmp:
            one, two = Path(tmp)/'one.png', Path(tmp)/'two.png'
            Image.new('RGB',(64,64),'white').save(one)
            Image.new('RGB',(128,128),'white').save(two)
            self.assertEqual(len(duplicate_details([str(one),str(two)])),1)

    def test_audit_only_escalates_suspicious_and_keeps_patent_delete(self):
        import tempfile, types
        from unittest import mock
        from PIL import Image
        from image_quality_gate import audit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('main.png','patent.png'):
                Image.new('RGB',(32,32),'white').save(root/name)
            clean={'flags':[], 'violations':[], 'legible':True, 'layoutOk':True, 'valuable':True, 'uncertain':False}
            patent={**clean, 'flags':['PATENT']}
            calls=[]
            def single(_key,model,path):
                calls.append((model,Path(path).name))
                return {'ok':True,'parsed':patent if (Path(path).name=='patent.png' and model=='gemini-2.5-flash-lite') else clean}
            verdict={k:True for k in ('productSame','violationsCleared','noNewArtifacts','qualityOk','layoutPreserved','safeTextPreserved','textLegible','noHoles','sourceFirstMatched')}
            pair=types.SimpleNamespace(load_key=lambda:'placeholder-not-a-key',call=lambda *a:verdict,
                PRIMARY_MODEL='gemini-2.5-flash-lite',REVIEW_MODEL='gemini-2.5-flash')
            scanner=types.SimpleNamespace(call=single,PRIMARY_MODEL='gemini-2.5-flash-lite',
                REVIEW_MODEL='gemini-2.5-flash',needs_review=lambda r:bool(r['parsed']['flags']))
            manifest={'items':{'123':{'sourceFirst':'main.png','images':[
                {'role':'main1','source':'main.png','final':'main.png'},
                {'role':'detail','source':'patent.png','final':'patent.png'}]}}}
            with mock.patch.dict(sys.modules,{'oxygen_verify':pair,'vision_audit':scanner}):
                report=audit(manifest,root)
            self.assertEqual(calls,[('gemini-2.5-flash-lite','main.png'),('gemini-2.5-flash-lite','patent.png'),('gemini-2.5-flash','patent.png')])
            self.assertEqual(report['items']['123']['images'][1]['decision'],'DELETE')
            self.assertTrue(report['items']['123']['images'][0]['pairAccepted'])
    def test_source_patent_page_deleted_even_when_final_redraw_cleans_word(self):
        import tempfile, types
        from unittest import mock
        from PIL import Image
        from image_quality_gate import audit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            for name in ('patent.png','redrawn.png'):
                Image.new('RGB',(32,32),'white' if name=='patent.png' else 'black').save(root/name)
            clean={'flags':[], 'violations':[], 'legible':True, 'layoutOk':True, 'valuable':True,'uncertain':False}
            patent={**clean,'flags':['PATENT']}
            models=[]
            def single(_key,model,path):
                models.append((model,Path(path).name))
                return {'ok':True,'parsed':patent if Path(path).name=='patent.png' else clean}
            scanner=types.SimpleNamespace(call=single,PRIMARY_MODEL='gemini-2.5-flash-lite',
                REVIEW_MODEL='gemini-2.5-flash',needs_review=lambda r:bool(r['parsed']['flags']))
            pair=types.SimpleNamespace(load_key=lambda:'placeholder-not-a-key',call=lambda *a:None,
                PRIMARY_MODEL='gemini-2.5-flash-lite',REVIEW_MODEL='gemini-2.5-flash')
            manifest={'items':{'x':{'sourceFirst':'patent.png',
                'images':[{'role':'detail','source':'patent.png','final':'redrawn.png'}]}}}
            with mock.patch.dict(sys.modules,{'vision_audit':scanner,'oxygen_verify':pair}):
                report=audit(manifest,root)
            self.assertEqual(report['items']['x']['images'][0]['decision'],'DELETE')
            self.assertIn(('gemini-2.5-flash-lite','patent.png'),models)
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
    def test_recheck_does_not_rebuild_deleted_detail_selection(self):
        import tempfile, json
        from unittest import mock
        import listing_batch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);folder=root/'candidates'/'ITEM';folder.mkdir(parents=True)
            candidate={'sourceItemId':'ITEM','detailImages':['kept-page.png']}
            (folder/'candidate.json').write_text(json.dumps(candidate),encoding='utf-8')
            with mock.patch.object(listing_batch,'ROOT',root), mock.patch.object(listing_batch,'static_validate',return_value=[]):
                results=listing_batch.recheck_existing('ITEM')
            self.assertEqual(results[0]['status'],'LOCAL_VALIDATED')
            self.assertEqual(json.loads((folder/'candidate.json').read_text(encoding='utf-8'))['detailImages'],['kept-page.png'])
            self.assertTrue(json.loads((folder/'local-validation.json').read_text(encoding='utf-8'))['ok'])
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
            report={'promptVersion':3,'items':{'ITEM':{'sourceFirst':file_evidence(folder/'original/first.png'),'images':rows}}}
            (folder/'image-quality-acceptance.json').write_text(json.dumps(report),encoding='utf-8')
            enforce_taobao_quality(folder,source,listing)
            Image.new('RGB',(32,32),'red').save(folder/'main/first.png')
            with self.assertRaisesRegex(Exception,'image quality gate'):
                enforce_taobao_quality(folder,source,listing)
    def test_new_listing_blocks_before_network_when_report_missing(self):
        import json, tempfile
        from unittest import mock
        import listing_batch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); dest = root/'candidates'/'123'; dest.mkdir(parents=True)
            (dest/'candidate.json').write_text(json.dumps({'sourceItemId':'123'}),encoding='utf-8')
            (dest/'local-validation.json').write_text(json.dumps({'ok':True}),encoding='utf-8')
            (dest/'state.json').write_text(json.dumps({'stage':'JD_VALIDATED'}),encoding='utf-8')
            with mock.patch.object(listing_batch,'ROOT',root), mock.patch.object(listing_batch,'CONFIG',{'imageQualityGate':'strict'}), mock.patch.object(listing_batch,'identity',side_effect=AssertionError('network identity called')):
                with self.assertRaisesRegex(RuntimeError,'image quality gate failed'):
                    listing_batch.validate_one('123','PUBLISH_TO_JD')
                with self.assertRaisesRegex(RuntimeError,'image quality gate failed'):
                    listing_batch.publish_one('123','PUBLISH_TO_JD')
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
            report={'promptVersion':3,'items':{'x':{'sourceFirst':file_evidence(first),'images':records}}}
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
            report={'promptVersion':3,'items':{'123':{'sourceFirst':record(original), 'images':[
                {'role':'main1','source':record(original),'final':record(final),'decision':'KEEP','pairAccepted':True},
                {'role':'detail','source':record(detail),'final':record(detail),'decision':'KEEP','pairAccepted':True}]}}}
            self.assertEqual(validate_candidate_report(candidate,report,record(original)),[])
            final.write_bytes(b'changed')
            self.assertTrue(any('stale' in e for e in validate_candidate_report(candidate,report,record(original))))
            self.assertTrue(validate_candidate_report(candidate,report,record(detail)))

    def test_dense_origin_must_block_first_even_if_generated_image_looks_clean(self):
        import tempfile, types
        from unittest import mock
        from PIL import Image
        from image_quality_gate import audit
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            Image.new('RGB',(32,32),'red').save(root/'source.png')
            Image.new('RGB',(32,32),'blue').save(root/'edited.png')
            clean={'flags':[],'violations':[],'legible':True,'layoutOk':True,'valuable':True,'uncertain':False}
            dense=dict(clean,flags=['DENSE_VIOLATIONS'])
            scanner=types.SimpleNamespace(call=lambda key,model,path: {'ok':True,'parsed':dense if Path(path).name=='source.png' else clean},
                PRIMARY_MODEL='gemini-2.5-flash-lite',REVIEW_MODEL='gemini-2.5-flash',needs_review=lambda r:bool(r['parsed']['flags']))
            accepted={k:True for k in ('productSame','violationsCleared','noNewArtifacts','qualityOk','layoutPreserved','safeTextPreserved','textLegible','noHoles','sourceFirstMatched')}
            pair=types.SimpleNamespace(load_key=lambda:'test-only',call=lambda *a:accepted,
                PRIMARY_MODEL='gemini-2.5-flash-lite',REVIEW_MODEL='gemini-2.5-flash')
            manifest={'items':{'one':{'sourceFirst':'source.png', 'images':[
                {'role':'main1','source':'source.png','final':'edited.png'}]}}}
            with mock.patch.dict(sys.modules,{'vision_audit':scanner,'oxygen_verify':pair}):
                report=audit(manifest,root)
            self.assertEqual(report['promptVersion'],3)
            self.assertEqual(report['items']['one']['images'][0]['decision'],'BLOCK')

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
            report={'promptVersion':3,'items':{'x':{'sourceFirst':file_evidence(first),'images':rows}}}
            self.assertIn('detail-duplicates-main:'+str(same),validate_candidate_report(candidate,report,file_evidence(first)))
            old=dict(report,promptVersion=2)
            self.assertIn('image-quality-report-version-mismatch',validate_candidate_report(candidate,old,file_evidence(first)))

    def test_prompt_classifies_dense_violations_on_first_image_too(self):
        from image_quality_gate import SINGLE_PROMPT
        self.assertIn('\u9996\u4e3b\u56fe',SINGLE_PROMPT)
        self.assertIn('DENSE_VIOLATIONS',SINGLE_PROMPT)

    def test_taobao_draft_without_batch_ledger_can_import_vision_clients(self):
        import os,subprocess,tempfile
        ni=Path(__file__).resolve().parents[1]/'scripts'/'ni'
        with tempfile.TemporaryDirectory() as tmp:
            env=os.environ.copy();env.pop('NO_INVENTORY_ROOT',None)
            probe=("import sys; sys.path.insert(0,sys.argv[2]); "
                   "draft=sys.argv[1];sys.argv=['image_quality_gate.py','--root',draft]; "
                   "import oxygen_verify,vision_audit; print('IMPORT_OK')")
            result=subprocess.run([sys.executable,'-B','-c',probe,tmp,str(ni)],env=env,
                                  capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15)
            self.assertEqual(result.returncode,0,(result.stderr or '')[-300:])
            self.assertIn('IMPORT_OK',result.stdout)

if __name__ == '__main__': unittest.main()
