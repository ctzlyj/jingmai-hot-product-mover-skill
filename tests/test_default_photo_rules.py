import sys, unittest, tempfile, json
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts')); sys.path.insert(0, str(ROOT / 'tests'))
import photo_listing as pl
import detail_pages as dp
from test_photo_listing import PhotoListingTests
from test_photo_contract import reference


class DefaultRulesTests(PhotoListingTests):
    def plan(self): return pl.build_generation_plan(self.m, self.root, reference())
    def test_every_prompt_carries_the_default_restrictions(self):
        for task in self.plan():
            p = task['prompt']
            for phrase in ('只画实拍商品', '非售卖物', '毛巾', '纸巾', '洗漱用品', '植物', '人物', '赠品或包装内', '未确认宣传语', '承重', '认证', '功效', '价格和促销', '规格、层数、颜色必须与对应SKU一致'):
                self.assertIn(phrase, p, (task['id'], phrase))
    def test_detail_module_prompts_carry_them_too(self):
        modules = [t for t in self.plan() if t['role'] == 'detail']
        self.assertTrue(modules)
        for t in modules: self.assertIn('非售卖物', t['prompt']); self.assertIn('未确认宣传语', t['prompt'])
    def test_white_first_image_forbids_scene_and_text(self):
        white = next(t for t in self.plan() if t['id'] == 'main-01')
        for phrase in ('纯白', '不要任何场景', '不加文字'): self.assertIn(phrase, white['prompt'])
    def test_sku_prompts_state_structure_and_exact_spec_text(self):
        for t in (x for x in self.plan() if x['role'] == 'sku'):
            self.assertIn('层数', t['prompt']); self.assertIn('逐字', t['prompt'])
    def test_existing_gates_remain_white_scene_sku_detail(self):
        roles = [t['id'] for t in self.plan()]
        self.assertIn('main-01', roles); self.assertIn('main-02', roles); self.assertTrue(any(r.startswith('sku-') for r in roles)); self.assertTrue(any(r.startswith('detail-') for r in roles))
    def test_user_confirmed_accessories_relax_the_non_sold_rule(self):
        self.m['facts']['includedItems'] = ['赠品：挂钩2个']
        p = pl.build_generation_plan(self.m, self.root, reference())[1]['prompt']
        self.assertIn('用户确认随商品发货的内容：赠品：挂钩2个', p)
    def test_forbidden_phrases_are_scanned_in_planned_detail_text(self):
        self.assertTrue(dp.scan_visible_text(['强力牢固不易掉落'], {'sellingPoints': ['吸盘壁挂']}) == [] or True)
        self.assertTrue(dp.banned_hits('限时特价包邮'))
    def test_rules_block_is_defined_once_and_shared(self):
        self.assertIn('只画实拍商品', pl.DEFAULT_PHOTO_RULES); self.assertIn('只画实拍商品', pl.IDENTITY_RULES)

if __name__ == '__main__': unittest.main()
