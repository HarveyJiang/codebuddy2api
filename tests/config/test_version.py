"""版本标识解析：环境变量优先，缺省不炸。

镜像里没有 .git，版本只能靠部署侧注入，所以这里要覆盖的全是
「注入了 / 没注入 / 注了空串」三种情况。
"""
import os
import unittest
from unittest import mock

from admin import version


class VersionTests(unittest.TestCase):
    def test_defaults_to_unknown_when_nothing_is_set(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(version.image_tag())
            self.assertIsNone(version.semantic())
            self.assertEqual(version.describe(), "unknown")

    def test_image_tag_is_used_when_only_it_is_set(self):
        with mock.patch.dict(os.environ, {"WORKBUDDY_IMAGE_TAG": "main-abc1234"}, clear=True):
            self.assertEqual(version.image_tag(), "main-abc1234")
            self.assertEqual(version.describe(), "main-abc1234")

    def test_semantic_version_wins_over_image_tag(self):
        """想显示 1.2.0 就别显示 sha：手写版本是更「给人看」的口径。"""
        with mock.patch.dict(os.environ,
                             {"WORKBUDDY_IMAGE_TAG": "main-abc1234", "WORKBUDDY_VERSION": "1.2.0"},
                             clear=True):
            self.assertEqual(version.describe(), "1.2.0")

    def test_blank_value_counts_as_unset(self):
        """空串是「没填」，不能当成版本号显示成空白。"""
        with mock.patch.dict(os.environ, {"WORKBUDDY_IMAGE_TAG": "   "}, clear=True):
            self.assertIsNone(version.image_tag())
            self.assertEqual(version.describe(), "unknown")

    def test_legacy_env_name_still_works(self):
        with mock.patch.dict(os.environ, {"CODEBUDDY2OPENAI_IMAGE_TAG": "main-legacy"}, clear=True):
            self.assertEqual(version.image_tag(), "main-legacy")

    def test_snapshot_exposes_both_sources(self):
        with mock.patch.dict(os.environ,
                             {"WORKBUDDY_IMAGE_TAG": "main-abc1234", "WORKBUDDY_VERSION": "1.2.0"},
                             clear=True):
            self.assertEqual(version.snapshot(),
                             {"label": "1.2.0", "semantic": "1.2.0", "image_tag": "main-abc1234"})


if __name__ == "__main__":
    unittest.main()

import pytest  # noqa: E402  (标记需要)

# ---------------------------------------------------------------------------
# 分层标记（见 pytest.ini / tests/README.md）
#
# 本文件整体属于「core」。标记写在文件里而不是逐条标注：同一文件的用例
# 保护的是同一块行为，分域跑（`pytest tests/pool`）时就该一起跑。
#   core    : 必须守住的不变量，默认只跑这些
#   e2e     : 起整套 create_app 走真实请求链路
#   network : 经由 mock 传输层模拟上游交互
# 若某条用例其实不属于本文件默认域（例如纯形状断言、只测常量的），
# 请在该用例上单独加 @pytest.mark.<域>，或在下面改用更准确的一档。
# ---------------------------------------------------------------------------
pytestmark = [pytest.mark.core]

