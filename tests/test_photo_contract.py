import json,sys,tempfile,unittest
from pathlib import Path
from PIL import Image
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'scripts'))
import photo_contract as pc

def reference():
 return {'category':{'id':1},'brands':[{'brandId':-1}], 'schema':{'componentList':[{'componentCode':'CategoryGroupList','childrenComponents':[{'componentCode':'prop-product-1','componentTitle':'材质','validateRule':[{'validateType':'required','validateRule':True}],'optionsValueList':[{'label':'金属','value':'11'}]}]}, {'componentCode':'SKUS','childrenComponents':[{'componentCode':'VenderCategoryValue','childrenComponents':[{'componentCode':'sale-prop-2','componentTitle':'规格','extra':{'dim':1}}]}]}]},'componentCatalog':[], 'media':{'defaultCategoryId':1,'zoneId':'z','categories':[{'categoryId':1}]}}
def facts():
 return {'confirmed':True,'title':'测试收纳架','categoryId':1,'brandId':-1,'sellingPoints':['吸盘壁挂'], 'packageLengthMm':100,'packageWidthMm':80,'packageHeightMm':30,'weightKg':'0.100','freightTemplateId':1,'sevenDayReturnCode':'1','categoryAttributes':[{'componentCode':'prop-product-1','valueIds':['11']}],'skus':[{'key':'one','spec':'白色','jdPrice':'10.50','stock':12,'skuAttributes':[]}]}

class PhotoContractTests(unittest.TestCase):
 def test_missing_fields_cannot_be_invented(self):
  missing=pc.missing_information({'title':'t'},reference());self.assertIn('brandId',missing);self.assertIn('skus',missing);self.assertIn('packageLengthMm',missing);self.assertIn('prop-product-1',missing)
 def test_unconfirmed_facts_or_unknown_material_block_build(self):
  f=facts();f['confirmed']=False;self.assertIn('confirmed',pc.missing_information(f,reference()))
  f=facts();f['categoryAttributes'][0]['valueIds']=['bad'];self.assertTrue(pc.missing_information(f,reference()))
 def test_positive_facts_and_real_brand_template_match(self):self.assertEqual([],pc.missing_information(facts(),reference()))
 def test_key_profile_vender_shop_are_mandatory(self):
  with self.assertRaises(ValueError):pc.check_store({'profile':'default','venderId':1,'shopName':'x'})
 def test_package_paths_are_relative_and_cannot_escape(self):
  with tempfile.TemporaryDirectory() as temp:
   with self.assertRaises(ValueError):pc.package_path(Path(temp),'../private')
   with self.assertRaises(ValueError):pc.package_path(Path(temp),'https://example.com/file')
 def test_review_digest_changes_with_price_or_image(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp);(root/'one').write_bytes(b'1')
   manifest={'store':{'profile':'pop-test','venderId':1,'shopName':'test'},'facts':facts(),'mainImages':[{'localPath':'one'}]}
   a=pc.review_digest(manifest,root);manifest['facts']['skus'][0]['jdPrice']='11';self.assertNotEqual(a,pc.review_digest(manifest,root))
   a=pc.review_digest(manifest,root);(root/'one').write_bytes(b'2');self.assertNotEqual(a,pc.review_digest(manifest,root))
 def test_submission_guard_never_replays_unknown(self):
  for stage in ('SUBMITTING','UNKNOWN','SUCCEEDED'):
   with self.assertRaises(pc.ReconcileRequired):pc.can_submit({'status':stage})
 def test_detail_panels_preserve_order_and_long_image(self):
  with tempfile.TemporaryDirectory() as temp:
   root=Path(temp)
   for i,color in enumerate(('red','blue')):Image.new('RGB',(100,100),color).save(root/f'{i}.jpg')
   p=pc.join_detail_panels([root/'0.jpg',root/'1.jpg'],root/'long.jpg',width=100)
   with Image.open(p) as im:self.assertEqual((100,200),im.size);self.assertGreater(im.getpixel((0,0))[0],im.getpixel((0,0))[2])

if __name__=='__main__':unittest.main()
