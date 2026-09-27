import unittest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from image_quality_gate import decide, pair_accepted, duplicate_details, validate_candidate_report, PROMPT_VERSION

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

    def test_large_source_band_collapsing_is_blocked_even_if_both_models_pass(self):
        from PIL import Image, ImageDraw
        import tempfile, types
        from unittest import mock
        from image_quality_gate import audit, collapsed_source_bands
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);source=root/'source.png';final=root/'final.png'
            original=Image.new('RGB',(256,256),'#003317')
            pen=ImageDraw.Draw(original)
            for x in range(4,256,8):
                pen.rectangle((x,226,x+3,250),fill='white')
            original.save(source)
            Image.new('RGB',(256,256),'#003317').save(final)
            self.assertEqual(collapsed_source_bands(source,final),[7])
            self.assertEqual(collapsed_source_bands(source,source),[])
            clean={'flags':[],'violations':[],'legible':True,'layoutOk':True,'valuable':True,'uncertain':False}
            verdict={k:True for k in ('productSame','violationsCleared','noNewArtifacts','qualityOk',
                        'layoutPreserved','safeTextPreserved','textLegible','noHoles','sourceFirstMatched')}
            pairs=[]
            def compare(*args):
                pairs.append(args[-1]);return verdict
            scanner=types.SimpleNamespace(call=lambda *args:{'ok':True,'parsed':clean},
                      PRIMARY_MODEL='gemini-2.5-flash-lite',REVIEW_MODEL='gemini-2.5-flash',needs_review=lambda _:False)
            model=types.SimpleNamespace(load_key=lambda:'fake-key',call=compare,
                    PRIMARY_MODEL='gemini-2.5-flash-lite',REVIEW_MODEL='gemini-2.5-flash')
            manifest={'items':{'123':{'sourceFirst':'source.png','images':[
                {'role':'main1','source':'source.png','final':'final.png'}]}}}
            with mock.patch.dict(sys.modules,{'oxygen_verify':model,'vision_audit':scanner}):
                row=audit(manifest,root)['items']['123']['images'][0]
            self.assertEqual(pairs,['gemini-2.5-flash-lite','gemini-2.5-flash'])
            self.assertEqual(row['decision'],'BLOCK')
            self.assertFalse(row['pairAccepted'])
            self.assertEqual(row['collapsedSourceBands'],[7])
            self.assertEqual(row['issues'],['large-source-content-loss'])

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
            self.assertEqual(report['promptVersion'],PROMPT_VERSION)
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
            report={'promptVersion':PROMPT_VERSION,'items':{'x':{'sourceFirst':file_evidence(first),'images':rows}}}
            self.assertIn('detail-duplicates-main:'+str(same),validate_candidate_report(candidate,report,file_evidence(first)))
            old=dict(report,promptVersion=PROMPT_VERSION-1)
            self.assertIn('image-quality-report-version-mismatch',validate_candidate_report(candidate,old,file_evidence(first)))

    def test_prompt_classifies_dense_violations_on_first_image_too(self):
        from image_quality_gate import SINGLE_PROMPT
        self.assertIn('\u9996\u4e3b\u56fe',SINGLE_PROMPT)
        self.assertIn('DENSE_VIOLATIONS',SINGLE_PROMPT)

    def test_draft_gate_can_import_vision_clients(self):
        import os,subprocess,tempfile
        scripts=Path(__file__).resolve().parents[1]/'scripts'
        with tempfile.TemporaryDirectory() as tmp:
            env=os.environ.copy()
            probe=("import sys; sys.path.insert(0,sys.argv[2]); "
                   "draft=sys.argv[1];sys.argv=['image_quality_gate.py','--root',draft]; "
                   "import oxygen_verify,vision_audit; print('IMPORT_OK')")
            result=subprocess.run([sys.executable,'-B','-c',probe,tmp,str(scripts)],env=env,
                                  capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=15)
            self.assertEqual(result.returncode,0,(result.stderr or '')[-300:])
            self.assertIn('IMPORT_OK',result.stdout)

if __name__ == '__main__': unittest.main()
