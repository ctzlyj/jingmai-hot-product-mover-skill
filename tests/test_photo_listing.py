import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'));sys.path.insert(0,str(ROOT/'tests'))
import photo_listing as pl
from test_photo_contract import facts,reference
from photo_contract import initial_manifest
from photo_generation import atomic_json

class PhotoListingTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)/'listing-package';self.root.mkdir()
  self.store={'profile':'pop-test','venderId':1,'shopName':'测试'}
  for name in ('photo.png','main.jpg','sku.jpg','detail.jpg'):Image.new('RGB',(800,800),'white').save(self.root/name)
  self.m=initial_manifest(self.store,['photo.png'],'测试收纳架',None);self.m['facts']=facts();self.m['mainImages']=[{'localPath':'main.jpg','role':'white'}];self.m['detailImages']=[{'localPath':'detail.jpg'}];self.m['variants']=[{'key':'one','skuImage':{'localPath':'sku.jpg'}}]
  atomic_json(self.root/'manifest.json',self.m)
 def test_reference_choice_is_required_once(self):
  with self.assertRaises(ValueError):pl.create_package(self.root.parent/'new',self.store,[self.root/'photo.png'],'title',reference_answered=False)
 def test_plan_references_only_real_photos_and_sku_plan_exists(self):
  plan=pl.build_generation_plan(self.m,self.root,reference())
  self.assertTrue(any(t['role']=='sku' for t in plan));self.assertTrue(any(t['role']=='detail' for t in plan))
  self.assertTrue(all([str(self.root/'photo.png')]==t['photos'] for t in plan))
 def test_preview_plan_does_not_imply_ready_to_publish(self):
  m=dict(self.m);m['facts']={};plan=pl.build_generation_plan(m,self.root,reference(),preview=True)
  self.assertEqual(3,len(plan));self.assertTrue(all(t['previewOnly'] for t in plan))
 def test_schema_uses_live_sale_attribute_not_decorative_painting_default(self):
  listing=pl.build_listing(self.m,self.root,reference());self.assertEqual('sale-prop-2',listing['product']['saleAttributeComponents']['style']['componentCode'])
  self.assertEqual('10.50',listing['skus'][0]['jdPrice']);self.assertEqual(12,listing['skus'][0]['stock'])
 def test_manifest_export_contains_sku_image_and_downstream_fields(self):
  listing=pl.build_listing(self.m,self.root,reference());self.assertEqual(1,len(listing['skus']))
  self.assertEqual('sku_main',listing['images'][1]['role'])
 def test_prepare_without_user_review_confirmation_has_no_uploads(self):
  with patch.object(pl.cli,'upload') as upload:
   with self.assertRaises(ValueError):pl.prepare_package(self.root,reference(),confirm='',digest='')
   upload.assert_not_called()
 def test_publish_without_confirmation_has_no_remote_access(self):
  with patch.object(pl.cli,'get') as get:
   with self.assertRaises(ValueError):pl.publish_package(self.root,confirm='',digest='')
   get.assert_not_called()
 def test_price_or_file_change_invalidates_review(self):
  digest=pl.review_package(self.root)['digest'];m=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'));m['facts']['skus'][0]['jdPrice']='13';atomic_json(self.root/'manifest.json',m)
  with self.assertRaises(ValueError):pl.check_review(self.root,digest)
 def test_unknown_product_submit_is_only_reconciled(self):
  atomic_json(self.root/'state.json',{'status':'UNKNOWN','itemNumber':'PN-test'})
  with patch.object(pl.cli,'run_jdo_large') as write:
   with self.assertRaises(Exception):pl.publish_package(self.root,confirm='PUBLISH_TO_JD',digest='missing')
   write.assert_not_called()

class PhotoPreparePublishTests(unittest.TestCase):
 setUp=PhotoListingTests.setUp
 def test_prepare_requires_explicit_image_acceptance_even_with_digest(self):
  d=pl.review_package(self.root)['digest']
  with patch.object(pl,'verify_store') as store:
   with self.assertRaises(ValueError):pl.prepare_package(self.root,reference(),confirm='PREPARE_PHOTO_LISTING',digest=d)
   store.assert_not_called()
 def test_confirmed_prepare_and_publish_are_idempotent(self):
  d=pl.review_package(self.root)['digest'];pl.accept_images(self.root,d,confirm='PHOTOS_REVIEWED')
  calls=[]
  def write(**kw):
   calls.append(kw['url'])
   return {'data':{'productId':10,'applyId':'10'}} if kw['url'].endswith('/product-applies') else {'data':{}}
  with patch.object(pl,'verify_store'),patch.object(pl.cli,'upload',return_value={'imgUrl':'jfs/test.jpg'}),patch.object(pl.cli,'run_jdo_large',side_effect=write),patch.object(pl.pipeline,'find_existing_product',return_value=None),patch.object(pl,'verify_package',return_value={'status':'VERIFIED'}):
   prepared=pl.prepare_package(self.root,reference(),confirm='PREPARE_PHOTO_LISTING',digest=d)
   self.assertFalse(prepared['published']);pl.publish_package(self.root,confirm='PUBLISH_TO_JD',digest=d);pl.publish_package(self.root,confirm='PUBLISH_TO_JD',digest=d)
  self.assertEqual(1,sum(u.endswith('/product-applies') for u in calls));self.assertEqual(10,json.loads((self.root/'manifest.json').read_text(encoding='utf-8'))['productId'])
 def test_known_quality_problem_cannot_be_accepted(self):
  m=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'));m['qualityProblems']=['product structure mismatch'];atomic_json(self.root/'manifest.json',m);d=pl.review_package(self.root)['digest']
  with self.assertRaises(ValueError):pl.accept_images(self.root,d,confirm='PHOTOS_REVIEWED')

class PhotoInputSafetyTests(unittest.TestCase):
 def test_tracking_query_is_not_stored_in_package(self):
  self.assertEqual('https://item.jd.com/123.html',pl.clean_reference_url('https://item.jd.com/123.html?tracking=opaque'))
 def test_private_key_fields_are_rejected_without_echoing_value(self):
  with self.assertRaisesRegex(ValueError,'Credential'):
   pl.reject_credentials({'apiKey':'synthetic-private-value'})

class PhotoReadbackTests(unittest.TestCase):
 setUp=PhotoListingTests.setUp
 def prepare_readback(self):
  m=self.m;m['productId']=100;listing=pl.build_listing(m,self.root,reference());sku=listing['skus'][0]
  for item in listing['images']:item['upload']={'imgUrl':{'spu_main':'jfs/spu.jpg','sku_main':'jfs/sku.jpg','detail':'jfs/detail.jpg'}[item['role']]}
  m['variants'][0]['vendorSku']=sku['vendorSku'];atomic_json(self.root/'manifest.json',m);atomic_json(self.root/'listing.json',listing)
  remote={'skuId':101,'outerId':sku['vendorSku'],'priceInfo':{'jdPrice':10.50},'stockNum':12,'skuEnableStatus':1,'skuStats':{'onOffShelfStatus':1},'saleAttrs':[{'attrValueAlias':'白色','attrValues':'color-one'}]}
  detail={'productInfo':{'productName':'测试收纳架','productStatus':{'productStatusNew':1},'categoryDetail':{'lastCategoryId':1}},'skuList':[remote]}
  apply={'applyInfo':{'draftComputeStatus':2},'skuList':[remote], 'productInfo':{'productDetailDesc':{'desc':"<img src='https://img10.360buyimg.com/imgzone/jfs/detail.jpg'>"}},'material':{'mainImages':[{'uuid':'0000000000','imageInfoList':[{'imgUrl':'jfs/spu.jpg','orderSort':1}]},{'uuid':'color-one','imageInfoList':[{'imgUrl':'jfs/sku.jpg','orderSort':1}]}]}}
  return detail,apply
 def test_verify_exports_online_product_and_sku_ids(self):
  detail,apply=self.prepare_readback()
  def get(skill,url,params):return {'data':apply if '/product-apply/' in url else detail}
  with patch.object(pl,'verify_store'),patch.object(pl.cli,'get',side_effect=get),patch.object(pl.cli,'query_sku_stocks',return_value=[{'skuId':101,'stockNum':12}]):r=pl.verify_package(self.root,attempts=1,interval=0)
  self.assertEqual('VERIFIED',r['status']);m=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'));self.assertEqual([101],m['skuIds']);self.assertEqual(101,m['variants'][0]['skuId'])
 def test_unshelved_product_cannot_be_marked_verified(self):
  detail,apply=self.prepare_readback();detail['productInfo']['productStatus']['productStatusNew']=2;detail['skuList'][0]['skuStats']['onOffShelfStatus']=0
  def get(skill,url,params):return {'data':apply if '/product-apply/' in url else detail}
  with patch.object(pl,'verify_store'),patch.object(pl.cli,'get',side_effect=get),patch.object(pl.cli,'query_sku_stocks',return_value=[{'skuId':101,'stockNum':12}]):r=pl.verify_package(self.root,attempts=1,interval=0)
  self.assertEqual('VERIFICATION_FAILED',r['status'])
 def test_missing_dedicated_inventory_cannot_fall_back_to_product_cache(self):
  detail,apply=self.prepare_readback()
  def get(skill,url,params):return {'data':apply if '/product-apply/' in url else detail}
  with patch.object(pl,'verify_store'),patch.object(pl.cli,'get',side_effect=get),patch.object(pl.cli,'query_sku_stocks',return_value=[]):r=pl.verify_package(self.root,attempts=1,interval=0)
  self.assertEqual('VERIFICATION_FAILED',r['status']);self.assertTrue(any(p.startswith('stock') for p in r['problems']))
class PhotoReferenceTests(unittest.TestCase):
 setUp=PhotoListingTests.setUp
 def test_reference_collection_is_analysis_only_not_product_material(self):
  import io
  data=io.BytesIO();Image.new('RGB',(32,32),'white').save(data,'JPEG')
  class Client:
   def get_text(_,url):return 'fixture'
   def get_bytes(_,url,headers=None):return data.getvalue()
  with patch.object(pl,'parse_mobile_page',return_value={'item':{'image':['jfs/one.jpg']},'product':{'popPid':10}}),patch.object(pl,'parse_detail_urls',return_value=['https://img.example/detail.jpg']):
   m=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'));m['reference']['url']='https://item.jd.com/123.html';atomic_json(self.root/'manifest.json',m)
   r=pl.collect_reference(self.root,client=Client())
  self.assertEqual('style-only',r['purpose']);self.assertEqual(1,r['mainCount']);self.assertEqual(1,r['detailCount'])
  m=json.loads((self.root/'manifest.json').read_text(encoding='utf-8'));self.assertEqual('main.jpg',m['mainImages'][0]['localPath']);self.assertEqual(['photo.png'],m['sourcePhotos'])

class PhotoPublishRaceTests(unittest.TestCase):
 setUp=PhotoListingTests.setUp
 def test_state_is_rechecked_after_acquiring_publish_lock(self):
  import os
  d=pl.review_package(self.root)['digest'];pl.accept_images(self.root,d,confirm='PHOTOS_REVIEWED')
  with patch.object(pl,'verify_store'),patch.object(pl.cli,'upload',return_value={'imgUrl':'jfs/test.jpg'}),patch.object(pl.cli,'run_jdo_large',return_value={'data':{}}):pl.prepare_package(self.root,reference(),confirm='PREPARE_PHOTO_LISTING',digest=d)
  original=os.open
  def opening(path,flags,mode=0o777):
   fd=original(path,flags,mode)
   if str(path).endswith('.publish.lock'):atomic_json(self.root/'state.json',{'status':'SUCCEEDED','productId':10})
   return fd
  with patch.object(pl,'verify_store'),patch.object(pl.pipeline,'find_existing_product',return_value=None),patch.object(pl.os,'open',side_effect=opening),patch.object(pl,'reconcile_package',return_value={'status':'VERIFIED'}) as reconcile,patch.object(pl.cli,'run_jdo_large',return_value={'data':{'productId':10}}) as write,patch.object(pl,'verify_package',return_value={'status':'VERIFIED'}):
   pl.publish_package(self.root,confirm='PUBLISH_TO_JD',digest=d);write.assert_not_called();reconcile.assert_called_once()

if __name__=='__main__':unittest.main()
