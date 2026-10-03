from setuptools import Extension, setup

# One stable-ABI binary serves every supported CPython (3.12+).
setup(
    ext_modules=[
        Extension(
            "seam._target._seam_trap",
            sources=["src/seam/_target/_seam_trap.c"],
            py_limited_api=True,
            extra_compile_args=["-O2", "-g"],
        )
    ],
    options={"bdist_wheel": {"py_limited_api": "cp312"}},
)
