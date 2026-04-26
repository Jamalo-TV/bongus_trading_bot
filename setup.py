from setuptools import setup, Extension
from Cython.Build import cythonize
import numpy

extensions = [
    Extension(
        "core_c.fast_math",
        ["core_c/fast_math.pyx"],
        include_dirs=[numpy.get_include()]
    )
]

setup(
    name="fast_math",
    ext_modules=cythonize(extensions),
)
