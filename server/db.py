# -*- coding: utf-8 -*-
"""
只读访问建筑能耗数据库。

数据库有 6.3GB、3270 万条读数，但建议功能需要的全部指标都已经预先算在
building_summary（1662 行）里了，所以这里只查 buildings / building_summary
两张小表，不碰 readings——这也是接口能在毫秒级返回的原因。

连接一律用 SQLite 的只读 URI 打开，从根上杜绝后端误写数据库。
"""
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# 优先用精简库（部署版），本地开发用全量库
_slim = ROOT / "data" / "building_db_slim.sqlite"
_full = ROOT / "data" / "building_db.sqlite"
DB_PATH = _slim if _slim.exists() else _full

# building_summary 里给建议模块用的字段
SUMMARY_COLUMNS = (
    "building_id, building_type, total_kwh, days_count, daily_avg_kwh, "
    "night_base_load, night_ratio, weekend_ratio, peak_hour, offpeak_hour, "
    "anomaly_count, saving_potential_kwh, saving_pct"
)


def connect() -> sqlite3.Connection:
    """以只读方式打开数据库。

    FastAPI 的同步接口跑在线程池里，SQLite 连接默认不能跨线程复用，
    因此每次调用都新开一个连接（SQLite 打开连接很轻，不会去读那 6.3GB）。
    """
    if not DB_PATH.exists():
        raise FileNotFoundError(f"未找到数据库：{DB_PATH}")
    uri = f"file:{DB_PATH.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def list_types() -> list[dict]:
    """列出全部建筑类型及各自建筑数量（按数量倒序）。

    以 building_summary 为准而不是 buildings：只有前者才有算好的指标，
    后者里那 8 栋数据不足的建筑做不了分析，列出来只会让用户白点一次。
    """
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT building_type AS type, COUNT(*) AS count
            FROM building_summary
            WHERE building_type IS NOT NULL AND building_type <> ''
            GROUP BY building_type
            ORDER BY count DESC, building_type
            """
        ).fetchall()
        return [{"type": r["type"], "count": r["count"]} for r in rows]
    finally:
        conn.close()


def list_buildings(building_type: str, limit: int = 100) -> list[dict]:
    """列出某类型下的建筑，附带可直接展示的关键指标。

    参数：
        building_type: 建筑类型，需与库中取值完全一致。
        limit: 最多返回多少栋。
    """
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT {SUMMARY_COLUMNS}
            FROM building_summary
            WHERE building_type = ?
            ORDER BY saving_potential_kwh DESC
            LIMIT ?
            """,
            (building_type, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_building(building_id: str) -> dict | None:
    """按 building_id 取单栋建筑的指标，供建议接口使用。

    返回 None 表示库里没有这栋建筑或它没有分析结果（1670 栋里有 8 栋数据不足）。
    """
    conn = connect()
    try:
        row = conn.execute(
            f"SELECT {SUMMARY_COLUMNS} FROM building_summary WHERE building_id = ?",
            (building_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def suggest_building_ids(keyword: str, limit: int = 10) -> list[str]:
    """按关键字模糊查建筑 ID，方便小程序里搜索。"""
    conn = connect()
    try:
        rows = conn.execute(
            """
            SELECT building_id FROM building_summary
            WHERE building_id LIKE ?
            ORDER BY saving_potential_kwh DESC
            LIMIT ?
            """,
            (f"%{keyword}%", limit),
        ).fetchall()
        return [r["building_id"] for r in rows]
    finally:
        conn.close()
