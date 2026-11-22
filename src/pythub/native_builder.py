"""兼容层: PyInstaller 引擎已迁移至 pythub.engines（多引擎适配层）。"""

from pythub.engines import (
    ENGINES,
    ENGINE_NAMES,
    build_engine,
    engine_available,
)

SUPPORTED_FMTS = ENGINES["pyinstaller"]["fmts"]


def build_native(spec, logger):
    """旧接口: 调用 PyInstaller 引擎（等价 --engine pyinstaller）。"""
    from pythub.builder import BuildSpec  # noqa: F401  (类型注解用)

    spec.engine = "pyinstaller"
    return build_engine(spec, logger)
