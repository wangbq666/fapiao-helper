# -*- coding: utf-8 -*-
"""模拟瘦身后的 exe 环境(numpy/pandas/scipy/PIL/lxml 等被排除), 跑全量回归"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

BLOCKED = {'numpy', 'pandas', 'scipy', 'PIL', 'lxml', 'matplotlib',
           'contourpy', 'fontTools', 'IPython', 'jax', 'sklearn'}


class Blocker:
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in BLOCKED:
            raise ImportError('blocked: ' + name)
        return None


sys.meta_path.insert(0, Blocker())
src = open(os.path.join(HERE, 'verify_qt.py'),
           encoding='utf-8').read()
g = {'__name__': '__main__',
     '__file__': os.path.join(HERE, 'verify_qt.py')}
exec(compile(src, 'verify_qt.py', 'exec'), g)
