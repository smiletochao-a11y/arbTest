# -*- coding: utf-8 -*-
"""ETFarb 场内份额采集：从东京 VPS(ArbSiphon) SFTP 拉取 shares_*.json 入库 fund_share。

东哥 2026-09-19 决策：
  · 所有基金份额统一走交易所(VPS)源，替代集思录 jsl_snapshot。
  · 单位口径（2026-09-19 实测核实）：SH_SZ_LOFshares 三条路径（深交所 dqgm /
    上交所 INTERNAL_VOL / ETF 东财 f84或派生）落库**全部为「万份」**（见脚本日志
    "万份" + 011_2 §六 + 093 §5.2 三方一致）。故本模块**不做任何折算**，只做
    sh/sz 前缀剥离 → 直接以「万份」写入 fund_share.pre_share（与现有 f84 口径一致）。
    ⚠️ 旧版曾把 LOF 再 ÷10000（误以为 VPS 是「份」），已废除——双重除会让 LOF 份额小 1 万倍。
  · 新增 = 本行 pre_share − 上一有记录日期 pre_share（复用 ShareFetcher.save 自动求差）。
  · 绝不兜底：拉取/解析失败暴露异常；单只缺失不影响其余。

运行：
  python core/vps_share_sync.py            # 拉最新、入库
  python core/vps_share_sync.py --dry      # 只拉取打印，不入库
  python core/vps_share_sync.py --dry --codes 161226,513590
"""
import datetime
import io
import json
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import db as _db   # noqa: F401  ensure_schema()
from share_fetcher import ShareFetcher

try:
    import paramiko
except ImportError:
    paramiko = None

from account_private import (  # noqa: E402
    VPS_HOST, VPS_PORT, VPS_USER, VPS_KEY_PATH, VPS_KEY_PASSWORD, VPS_SHARES_DIR)

SOURCE_TAG = "vps_exchange"
_REMOTE_RE = re.compile(r'^shares_\d{4}-\d{2}-\d{2}\.json$')


def classify_fund(code):
    """与 VPS SH_SZ_LOFshares.classify_fund 同口径：
    ETF: 159/51/56/58 开头；其余 5→上交所LOF；其余 1→深交所LOF。"""
    c = re.sub(r'^(sh|sz)', '', code, flags=re.I)
    if re.match(r'^(159|51|56|58)', c):
        return 'etf'
    return 'lof'


def _clean_code(raw):
    return raw.lower().replace('sh', '').replace('sz', '')


def to_wan(code, val):
    """VPS 三条路径（深交所 dqgm / 上交所 INTERNAL_VOL / ETF 东财 f84 或派生）落库
    **全部为「万份」**（脚本日志「万份」+ 011_2 §六 + 093 §5.2 三方一致）。本模块
    **不做任何折算**，只做 sh/sz 前缀剥离 → 直接以「万份」写入 fund_share.pre_share。

    🔴 严禁再对 LOF 做 ÷10000（双重除会让 LOF 份额小 1 万倍）：2026-09-19 实测踩坑——
    VPS 原始 `sz161725=343497.17` 已是万份，旧 `to_wan` 误当「份」再 ÷10000 落库成
    `34.3497`，导致深交所 LOF/ETF 份额列与换手率分母全错。第一性原理：源头已是万份，
    同步层不该再除。"""
    if val is None:
        return None
    try:
        return round(float(val), 4)
    except (TypeError, ValueError):
        return None


def _connect():
    if paramiko is None:
        raise RuntimeError("paramiko 未安装：pip install paramiko")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    # 双认证：优先私钥，失败降级密码（与 arbTest daily_updater 同口径）
    try:
        if VPS_KEY_PATH and os.path.exists(VPS_KEY_PATH):
            pkey = paramiko.Ed25519Key.from_private_key_file(
                VPS_KEY_PATH, password=VPS_KEY_PASSWORD or None)
            ssh.connect(VPS_HOST, port=VPS_PORT, username=VPS_USER,
                        pkey=pkey, timeout=15, banner_timeout=15, auth_timeout=15)
            return ssh
    except Exception as ke:
        print("[VPS] 私钥认证失败 (%s)，降级密码" % ke)
    if VPS_PASSWORD:
        ssh.connect(VPS_HOST, port=VPS_PORT, username=VPS_USER,
                    password=VPS_PASSWORD, timeout=15, banner_timeout=15,
                    auth_timeout=15, look_for_keys=False, allow_agent=False)
        return ssh
    raise RuntimeError("VPS 凭据未配置：请在 core/account_private.py 填 VPS_KEY_PATH 或 VPS_PASSWORD")


def pull_latest():
    """拉取 VPS 上最新日期 shares_*.json，返回 (date_str, {code: 万份})。"""
    ssh = _connect()
    try:
        sftp = ssh.open_sftp()
        files = [f for f in sftp.listdir(VPS_SHARES_DIR) if _REMOTE_RE.match(f)]
        if not files:
            raise RuntimeError("VPS %s 下无 shares_*.json" % VPS_SHARES_DIR)
        files.sort()
        latest = files[-1]
        date_str = latest[len('shares_'):-len('.json')]
        # ⚠️ VPS 是 Linux 路径，Windows 的 os.path.join 会拼出反斜杠导致 ENOENT，必须强制正斜杠
        remote_path = VPS_SHARES_DIR.rstrip('/') + '/' + latest
        with sftp.open(remote_path, 'r') as rf:
            content = json.loads(rf.read().decode('utf-8'))
        sftp.close()
    finally:
        ssh.close()
    out = {}
    for raw, val in content.items():
        code = _clean_code(raw)
        wan = to_wan(code, val)
        if wan is not None:
            out[code] = wan
    return date_str, out


def sync(date=None, dry=False, verbose=True):
    """主流程：拉取 → 归一化 → 入库 fund_share。返回统计。"""
    date_str, data = pull_latest()
    if date:
        date_str = str(date)
    sf = ShareFetcher()
    ok, failed = [], []
    for code, wan in sorted(data.items()):
        if dry:
            ok.append((code, wan))
            continue
        try:
            sf.save(code, date_str, wan, source=SOURCE_TAG)
            ok.append((code, wan))
        except Exception as e:
            failed.append((code, str(e)))
        if verbose and len(ok) % 50 == 0:
            print("  已入库 %d 只…" % len(ok))
    if verbose:
        print("✅ VPS份额同步完成 date=%s 入库=%d 跳过=%d"
              % (date_str, len(ok), len(failed)))
    return {"date": date_str, "ok": ok, "failed": failed}


def main():
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    import argparse
    ap = argparse.ArgumentParser(description="ETFarb 场内份额 VPS 同步")
    ap.add_argument("--dry", action="store_true", help="只拉取打印，不入库")
    ap.add_argument("--date", help="强制写入此日期 YYYY-MM-DD（默认用 VPS 文件名日期）")
    ap.add_argument("--codes", help="只看这些 code（逗号分隔，配合 --dry）")
    a = ap.parse_args()
    r = sync(date=a.date, dry=a.dry, verbose=True)
    if a.dry:
        want = set((a.codes or "").split(',')) if a.codes else None
        for code, wan in r["ok"]:
            if want and code not in want:
                continue
            print("  %s  %s 万份" % (code, wan))


if __name__ == "__main__":
    main()
