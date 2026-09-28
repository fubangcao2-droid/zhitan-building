"""Read-only building profiles for explicitly labelled inventory-level estimates."""
from datetime import datetime, timedelta
from statistics import mean, median

import db


def build_profile(building_id: str) -> dict:
    """Return recent and typical normalized hourly shapes, with source dates and coverage.

    A building-total curve is only a proxy for appliance loading. Equivalent
    full-load hours must never be described as measured appliance switch-on time.
    """
    building = db.get_building(building_id)
    if building is None:
        raise ValueError("未找到参考建筑")
    conn = db.connect()
    try:
        latest = conn.execute("SELECT MAX(ts) FROM readings WHERE building_id = ?", (building_id,)).fetchone()[0]
        if not latest:
            raise ValueError("参考建筑没有逐时读数")
        start = (datetime.fromisoformat(latest[:10]) - timedelta(days=55)).date().isoformat()
        records = conn.execute(
            "SELECT ts, kwh FROM readings WHERE building_id = ? AND ts >= ? ORDER BY ts",
            (building_id, start),
        ).fetchall()
    finally:
        conn.close()
    days = {}
    for record in records:
        value = record['kwh']
        if value is None or value < 0:
            continue
        stamp = record['ts']
        hour = int(stamp[11:13])
        days.setdefault(stamp[:10], {}).setdefault(hour, []).append(float(value))
    complete = {day: [sum(hours[h]) for h in range(24)] for day, hours in days.items() if len(hours) == 24}
    if len(complete) < 7:
        raise ValueError("参考建筑不足7个完整日，请换一栋")
    dates = sorted(complete)
    recent = dates[-7:]
    # One shared observed peak preserves the scale between recent and typical profiles.
    peak = max(max(values) for values in complete.values())
    if peak <= 0:
        raise ValueError("参考建筑无有效正电量")
    predicted = [mean(complete[day][hour] for day in recent) / peak for hour in range(24)]
    standard = [median(values[hour] for values in complete.values()) / peak for hour in range(24)]
    return {
        'building_id': building_id, 'type': building['building_type'],
        'start': dates[0], 'end': dates[-1], 'days': len(dates),
        'predicted': predicted, 'standard': standard,
        'predicted_hours': sum(predicted), 'standard_hours': sum(standard),
        'method': '最近7个完整日均值 / 参考期峰值；标准参考为最多56日逐小时中位数 / 同一峰值。',
        'boundary': '整栋曲线代理估算，各类别共用楼栋负荷形状；等效满负荷小时不等于开机时长，标准值不是强制能耗限额。',
    }
