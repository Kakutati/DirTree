# 빌드: python setup.py build_ext --inplace   (Visual Studio C++ 빌드 도구 필요)
from setuptools import Extension, setup

setup(name="dirtree", ext_modules=[Extension("_fastindex", ["_fastindex.c"])])
