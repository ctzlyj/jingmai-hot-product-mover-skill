import base64
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
import requests
from PIL import Image
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"scripts"))
import company_jpeg_edits as company


def jpeg():
    stream=io.BytesIO()
    Image.new("RGB",(1024,1024),"white").save(stream,format="JPEG")
    return stream.getvalue()


class Response:
    status_code=200
    def __init__(self,raw): self.raw=raw
    def json(self): return {"data":[{"b64_json":base64.b64encode(self.raw).decode()}]}


class CompanyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.raw=jpeg()
        self.source=self.root/"source.jpg"
        self.source.write_bytes(self.raw)
    def tearDown(self): self.temp.cleanup()
    def test_exact_jpeg_company_payload_and_timeout(self):
        calls=[]
        def post(url,**kwargs):
            calls.append((url,kwargs))
            response=Response(self.raw)
            kwargs['hooks']['response'](response)
            return response
        client=company.CompanyClient('dummy-not-real',post=post,sleep=lambda _:None)
        raw,meta=client.edit(model=company.MODELS[0],prompt='exact final prompt',source=self.source)
        url,kwargs=calls[0]
        self.assertEqual(url,'http://llm-gw.jd.local/v1/images/edits')
        self.assertEqual(kwargs['timeout'],600)
        self.assertEqual(kwargs['json']['model'],company.MODELS[0])
        self.assertEqual(kwargs['json']['output_format'],'jpeg')
        self.assertEqual(kwargs['json']['prompt'],'exact final prompt')
        self.assertTrue(kwargs['json']['image'][0].startswith('data:image/jpeg;base64,'))
        self.assertEqual(raw,self.raw)
        self.assertEqual(meta,{'httpStatus':200})
    def test_parallel_models_do_not_mutate_global_requests(self):
        models=[]
        original=requests.post
        def post(url,**kwargs):
            models.append(kwargs['json']['model'])
            return Response(self.raw)
        client=company.CompanyClient('dummy',post=post,sleep=lambda _:None)
        with ThreadPoolExecutor(2) as pool:
            futures=[pool.submit(client.edit,model=m,prompt='p',source=self.source) for m in company.MODELS]
            for job in futures: job.result()
        self.assertCountEqual(models,company.MODELS)
        self.assertIs(requests.post,original)
        self.assertEqual(client.slots._initial_value,4)
    def test_external_model_rejected_without_call(self):
        client=company.CompanyClient('dummy',post=lambda *a,**k:self.fail('network called'))
        with self.assertRaises(ValueError):
            client.edit(model='gpt-image-2.5',prompt='p',source=self.source)
    def test_error_body_not_exposed_or_retried(self):
        calls=[]
        class Bad:
            status_code=403
            text='unsafe-provider-secret'
            def json(self): return {'error':{'message':self.text}}
        def post(*args,**kwargs):
            calls.append(1)
            kwargs['hooks']['response'](Bad())
            return Bad()
        client=company.CompanyClient('dummy',post=post)
        with self.assertRaises(company.SafeGenerationError) as result:
            client.edit(model=company.MODELS[0],prompt='p',source=self.source)
        self.assertEqual(result.exception.status,403)
        self.assertNotIn('unsafe',str(result.exception))
        self.assertEqual(len(calls),1)
    def test_reference_preserves_exact_png_bytes_and_extension(self):
        stream=io.BytesIO()
        Image.new('RGB',(10,10),'red').save(stream,format='PNG')
        self.source.write_bytes(stream.getvalue())
        plan={'sources':[{'source':str(self.source),'sha':'a'}]}
        out=company.prepare_references(plan,self.root)[0]
        self.assertEqual(Path(out['reference']).suffix,'.png')
        self.assertEqual(Path(out['reference']).read_bytes(),stream.getvalue())
    def test_completed_cache_no_repeat(self):
        class Fake:
            def __init__(self):self.calls=0
            def edit(inner,**kwargs):
                inner.calls+=1
                return self.raw,{'httpStatus':200}
        plan={'prompt':'p','sources':[{'source':str(self.source),'sha':'a','model':company.MODELS[0]}]}
        fake=Fake()
        with patch.dict(os.environ,{'JD_LLM_API_KEY':''}):
            company.generate_plan(plan,self.root,client=fake)
            company.generate_plan(plan,self.root,client=fake)
        self.assertEqual(fake.calls,1)
        self.assertTrue((self.root/'generated'/'a.jpg').exists())
    def test_unknown_receipt_blocks_replay(self):
        company.save(self.root/'generation.json',{'a':{'status':'request-started'}})
        plan={'prompt':'p','sources':[{'source':str(self.source),'sha':'a','model':company.MODELS[0]}]}
        with self.assertRaises(RuntimeError):company.generate_plan(plan,self.root,client=object())
    def test_one_second_shared_start_rate(self):
        now=[10.0]
        sleeps=[]
        def sleep(delay):
            sleeps.append(delay)
            now[0]+=delay
        client=company.CompanyClient('dummy',post=lambda *a,**k:Response(self.raw),
            clock=lambda:now[0],sleep=sleep)
        for model in company.MODELS:
            client.edit(model=model,prompt='p',source=self.source)
        self.assertEqual(sleeps,[1.0])
    def test_truncated_response_is_not_replayed(self):
        calls=[]
        def post(*args,**kwargs):
            calls.append(1)
            raise requests.exceptions.ChunkedEncodingError('unsafe-provider-data')
        client=company.CompanyClient('dummy',post=post)
        with self.assertRaises(company.SafeGenerationError) as result:
            client.edit(model=company.MODELS[0],prompt='p',source=self.source)
        self.assertEqual(result.exception.category,'ChunkedEncodingError')
        self.assertEqual(calls,[1])
        self.assertNotIn('unsafe',str(result.exception))

if __name__=='__main__':unittest.main()
