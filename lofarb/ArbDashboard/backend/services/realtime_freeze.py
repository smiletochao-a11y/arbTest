"""
[2026-07-31] 收盘后冻结实时估值快照 + 最后有效估值缓存
[AI-2026-10-09 东哥拍板] 冻结机制已全面取消（本模块大幅精简）：
- 不再拍 15:00 快照、不再写 rt_cache.json，rt_frozen/rt_frozen_note 恒为 None（字段保留，前端兼容）。
- 程序估值与时间无关，行情展示走数据驱动：有真实盘口→显示，无推送/无数据→"等待数据"（禁兜底）。
- 仅保留「盘后现价收敛」apply_post_close_price_convergence：
  ≥15:00 时把 realtime_price 收敛到官方收盘价 price，根治白银 AG0 夜盘源盘后残留当日
  tick(如1.985)被当现价的问题（原 2026-08-21 冻结逻辑中唯一保留的段落，一处根治）。
"""
# [AI-2026-10-09] 精简：删除 JSON 原子写/路径常量/进程锁（rt_freeze.json、rt_cache.json 写入已随冻结机制一并取消）
import logging
from datetime import datetime

logger = logging.getLogger(__name__)




def apply_post_close_price_convergence(result: list) -> None:
    """[AI-2026-10-09] 在 get_unified_dashboard_data() 末尾调用：盘后（≥15:00）「现价」收敛到官方收盘价。

    冻结机制（15:00 快照 rt_freeze.json / 最后有效缓存 rt_cache.json / rt_frozen 标记）已全面取消：
    估值不再被冻结值覆盖——盘后有真实行情就显示，无数据就是 None（前端"等待数据"），绝不兜底。
    仅保留本段「盘后现价收敛」（原 2026-08-21 FIX 的核心，一处根治）：
      所有 A股LOF（含白银161226 / 黄金原油QDII欧美等）盘后 realtime_price 应=官方收盘(price)，
      杜绝白银 AG0 夜盘源盘后仍返当日残留 tick(如1.985)被当现价。
    前端 涨跌幅/实时溢价 列均用 curPrice(=realtime_price) 计算，故一处改动同步修好，不碰其它列。
    """
    if datetime.now().hour < 15:
        return
    for item in result:
        _p = item.get('price')
        if _p is not None and _p != 0:
            item['realtime_price'] = _p
