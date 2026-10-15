# 机密已集中到 D:/Study/ARB/account_info/etfarb_core_account_private.py
# 本文件为无密钥转发垫片，可安全入库；真实凭据不在此处。
import importlib.util
import os
import sys

_ACCOUNT_INFO_DIR = os.environ.get("ARB_ACCOUNT_INFO_DIR", r"D:\Study\ARB\account_info")
_CENTRAL = os.path.join(_ACCOUNT_INFO_DIR, "etfarb_core_account_private.py")
_spec = importlib.util.spec_from_file_location("_etfarb_account_private_central", _CENTRAL)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_this = sys.modules[__name__]
for _k, _v in vars(_mod).items():
    if not _k.startswith("__"):
        setattr(_this, _k, _v)
