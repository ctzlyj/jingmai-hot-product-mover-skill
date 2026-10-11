from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class CompanyBatchGuidanceTests(unittest.TestCase):
    def test_jpeg_not_claimed_as_guaranteed_transport_fix(self):
        text=(ROOT/'references'/'company-jpeg-edits.md').read_text(encoding='utf-8')
        self.assertIn('JPEG 也可能响应截断',text)
        self.assertIn('不代表整批可靠性',text)
        self.assertIn('具体传输截断节点尚未确认',text)
    def test_profile_refresh_race_and_terminal_rejections_documented(self):
        text=(ROOT/'references'/'company-jpeg-edits.md').read_text(encoding='utf-8')
        self.assertIn('configure_jdo_profile',text)
        self.assertIn('同一可重入锁',text)
        self.assertIn('draftComputeStatus=3',text)
        self.assertIn('不能算换图成功',text)
        self.assertIn('不改提示词绕过',text)
    def test_authorized_external_rejection_fallback_is_not_blocked(self):
        text=(ROOT/'references'/'company-jpeg-edits.md').read_text(encoding='utf-8')
        skill=(ROOT/'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('用户已授权',text)
        self.assertIn('公司模型拒绝',text)
        self.assertIn('外部 `gpt-image-2.5`',text)
        self.assertIn('不再重复申请权限',text)
        self.assertIn('只补失败图片',text)
        self.assertIn('已授权的拒绝/额度回退按用户选择走外部',skill)
        self.assertNotIn('不改提示词绕过、不重复消耗额度、不自动切外部',text)
    def test_no_default_quality_reduction_or_partial_completion_claim(self):
        text=(ROOT/'references'/'company-jpeg-edits.md').read_text(encoding='utf-8')
        self.assertIn('不要把 low 设为通用默认',text)
        self.assertIn('全部换好、部分换好、未在线生效',text)
        self.assertIn('用户定稿提示词',text)

if __name__=='__main__':unittest.main()
