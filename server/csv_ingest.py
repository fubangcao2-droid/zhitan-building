# -*- coding: utf-8 -*-
"""
解析用户上传的电表 CSV，算出一栋建筑的指标。

指标定义严格对齐 ml/analyze_database.py 的第一遍循环，这样"上传自己的数据"
和"从库里选建筑"两条路径得到的建议才可以直接比较：

    night_ratio    = 小时 ∈ [0,5] 的电量之和 / 总电量
    weekend_ratio  = 周六日均值 / 周一至周五均值（分母 ≤ 0 时取 1.0）
    daily_avg_kwh  = 总电量 / 不同日期数
    weekend_days   = 数据里出现过的不同周末日期数（周末浪费公式要用）
    anomaly_count  = MAD 稳健 z-score 超过 3.5 的读数条数（同 analyze_database.py 第 55-65 行）

时间戳按本地时间直接使用，不做时区换算——与原数据库里 ts 的存法一致。
"""
import io

import pandas as pd

# 允许的列名写法，尽量照顾用户手上各种导出口径
TIME_ALIASES = ("timestamp", "ts", "time", "datetime", "date_time", "date", "时间", "时间戳")
ENERGY_ALIASES = ("kwh", "value", "energy", "consumption", "electricity", "reading", "电量", "用电量")

# 与 analyze_database.py 第 29 行一致：读数太少的建筑直接跳过分析
MIN_ROWS = 100

Z_THRESHOLD = 3.5
MAD_SCALE = 1.4826


def _pick_column(columns: list[str], aliases: tuple[str, ...]) -> str | None:
    """从实际列名里挑出匹配别名的那个（先精确匹配，再忽略大小写和空格）。"""
    lowered = {c.strip().lower(): c for c in columns}
    for alias in aliases:
        if alias in lowered:
            return lowered[alias]
    return None


def compute_metrics(raw: bytes, building_id: str, building_type: str) -> dict:
    """把 CSV 字节流算成指标字典。

    参数：
        raw: CSV 文件内容。
        building_id: 用户自报的楼名，仅用于回显。
        building_type: 建筑类型，决定用哪套同类基准线。

    返回：
        可直接喂给 advisor.build_advice 的指标字典。

    异常：
        ValueError —— 列名对不上、时间无法解析、或有效数据太少。
    """
    # utf-8-sig 兼容 Excel 导出的 BOM
    try:
        df = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig")
    except UnicodeDecodeError:
        try:
            df = pd.read_csv(io.BytesIO(raw), encoding="gbk")
        except Exception as exc:  # noqa: BLE001 - 原样上报给用户看
            raise ValueError(f"CSV 编码无法识别（试过 utf-8 和 gbk）：{exc}") from exc

    if df.empty:
        raise ValueError("CSV 里没有数据行。")

    time_col = _pick_column(list(df.columns), TIME_ALIASES)
    energy_col = _pick_column(list(df.columns), ENERGY_ALIASES)

    if time_col is None or energy_col is None:
        raise ValueError(
            "没能识别出时间列和电量列。"
            f"当前列名：{list(df.columns)}；"
            f"时间列可用 {list(TIME_ALIASES)}，电量列可用 {list(ENERGY_ALIASES)}。"
        )

    df = df[[time_col, energy_col]].rename(columns={time_col: "ts", energy_col: "kwh"})
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    df["kwh"] = pd.to_numeric(df["kwh"], errors="coerce")
    df = df.dropna(subset=["ts", "kwh"])

    if len(df) < MIN_ROWS:
        raise ValueError(
            f"有效数据只有 {len(df)} 条，少于分析所需的最少 {MIN_ROWS} 条。"
            "请提供更长时段的逐时电量数据。"
        )

    # 同一时间戳可能重复，按主键语义去重后排序
    df = df.drop_duplicates(subset=["ts"]).sort_values("ts").reset_index(drop=True)

    df["hour"] = df["ts"].dt.hour
    df["date"] = df["ts"].dt.date
    df["weekday"] = df["ts"].dt.weekday  # 周一=0 … 周日=6
    weekend_mask = df["weekday"].isin([5, 6])

    total_kwh = float(df["kwh"].sum())
    if total_kwh <= 0:
        raise ValueError("总电量为 0 或负数，无法分析。")

    days_count = int(df["date"].nunique())

    night_kwh = float(df.loc[df["hour"].between(0, 5), "kwh"].sum())
    night_ratio = night_kwh / total_kwh

    weekend_mean = float(df.loc[weekend_mask, "kwh"].mean()) if weekend_mask.any() else 0.0
    weekday_mean = float(df.loc[~weekend_mask, "kwh"].mean()) if (~weekend_mask).any() else 0.0
    # 全库分析里分母 ≤ 0 时取 1.0，这里保持一致
    weekend_ratio = weekend_mean / weekday_mean if weekday_mean > 0 else 1.0

    weekend_days = int(df.loc[weekend_mask, "date"].nunique())

    hour_avg = df.groupby("hour")["kwh"].mean()
    peak_hour = int(hour_avg.idxmax())
    offpeak_hour = int(hour_avg.idxmin())

    return {
        "building_id": building_id,
        "building_type": building_type,
        # 这里一律保留全精度，不要提前四舍五入：
        # 节能量公式是 total_kwh * (weekend_ratio - p75) * (weekend_days/days_count)，
        # 若先把 weekend_ratio 舍到 3 位再代入，实测会让结果偏差约 2%。
        # 展示用的舍入统一在 advisor.build_advice 的输出层做。
        "total_kwh": total_kwh,
        "days_count": days_count,
        "daily_avg_kwh": total_kwh / max(days_count, 1),
        "night_base_load": float(df.loc[df["hour"].between(0, 5), "kwh"].median()),
        "night_ratio": night_ratio,
        "weekend_ratio": weekend_ratio,
        "weekend_days": weekend_days,
        "peak_hour": peak_hour,
        "offpeak_hour": offpeak_hour,
        "anomaly_count": count_anomalies(df, hour_avg),
        # 路径 B 没有预先算好的节能量，交给 advisor 现算
        "saving_potential_kwh": None,
        "saving_pct": None,
    }


def count_anomalies(df: pd.DataFrame, hour_avg: pd.Series) -> int:
    """用本楼自身的 24 小时平均曲线做期望，数出疑似异常读数条数。

    完全复刻 ml/analyze_database.py 第 55-65 行：残差 → 中位数 + MAD 稳健 z-score
    → |z| > 3.5 计一条。用中位数和 MAD 而不是均值和标准差，是因为电表数据不服从
    正态分布，一两个极端值就能把标准差带偏。
    """
    expected = df["hour"].map(hour_avg)
    residual = df["kwh"] - expected
    med = residual.median()
    mad = (residual - med).abs().median()
    if mad <= 0:
        return 0
    z = (residual - med) / (MAD_SCALE * mad)
    return int((z.abs() > Z_THRESHOLD).sum())
