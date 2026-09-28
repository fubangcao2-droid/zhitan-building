# -*- coding: utf-8 -*-
"""
加载同类型建筑的 P75 对标基准线。

基准线由 ml/export_type_baselines.py 从 building_summary 聚合生成，
落在 data/processed/type_baselines.json。这里只在首次访问时读一次并缓存。

注意：文件不存在时不要静默降级——没有基准线就做不了"和同类比"的对标，
这正是本功能的核心，应当直接报错提示先跑导出脚本。
"""
import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = ROOT / "data" / "processed" / "type_baselines.json"

MISSING_HINT = (
    "未找到同类基准线文件 data/processed/type_baselines.json，"
    "请先运行：python ml/export_type_baselines.py"
)


@lru_cache(maxsize=1)
def _load() -> dict:
    """读取并缓存基准线文件（整个进程只读一次）。"""
    if not BASELINE_PATH.exists():
        raise FileNotFoundError(MISSING_HINT)
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def get_baseline(building_type: str) -> dict | None:
    """取某建筑类型的基准线；类型不存在时返回 None。

    返回结构示例：
        {
          "count": 604,
          "night_ratio": {"mean": 0.197, "p75": 0.2279, "median": 0.2041},
          "weekend_ratio": {"mean": 1.057, "p75": 0.9118, "median": 0.83},
          "daily_avg_kwh": {"mean": 4516.069, "median": 3820.0},
          "saving_pct": {"mean": 0.087}
        }
    """
    return _load()["types"].get(building_type)


def metadata() -> dict:
    """基准线的来源与方法说明，用于在小程序上标注口径。"""
    data = _load()
    return {
        "generated_from": data.get("generated_from"),
        "generated_at": data.get("generated_at"),
        "method": data.get("method"),
        "note": data.get("note"),
        "total_buildings": data.get("total_buildings"),
        # 如实带出 P75 的精度说明，供页面标注数据口径；
        # 之前这里漏了这个字段，页面上就无从解释"为什么和后端算的不完全一致"。
        "precision_caveat": data.get("precision_caveat"),
    }
