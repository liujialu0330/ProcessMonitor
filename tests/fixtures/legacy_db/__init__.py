"""
真实旧库样本（golden 库）

样本由 generate.py 手动生成并压缩入库；测试只读 manifest.json 与 *.db.gz。
"""
import os

FIXTURE_DIR = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(FIXTURE_DIR, 'manifest.json')
