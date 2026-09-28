# -*- coding: utf-8 -*-
"""
节能建议规则引擎（纯计算，不碰数据库、不碰网络）。

两条数据入口共用这一份逻辑：
    路径 A —— 从库里选建筑：building_summary 已经存好了 saving_potential_kwh，
              直接读，不重算。
    路径 B —— 用户上传 CSV：需要按同一套公式现算。

为什么路径 A 不重算：
    analyze_database.py 第 124 行的周末浪费公式用到 weekend_days
    （weekend_waste = total_kwh * excess_ratio * (weekend_days / days_count)），
    但这个中间量没有写进 building_summary，重算就得回扫该栋的全部读数。
    直接读现成结果既准确又快。

节能量的口径与全库分析完全一致，见 ml/analyze_database.py 第 114-129 行。

证据边界（docs/design-spec.md 第 28-31 行）：
    只输出"排查方向"，措辞一律用疑问句，不给控制指令；
    异常只称"疑似异常时段"，不称设备故障。
"""

# 电价按 0.8 元/度折算，与技术报告 PDF 第五章"效益分析"一致
ELECTRICITY_YUAN_PER_KWH = 0.8

# 影响等级只看绝对节电量：严重度回答"偏离基准多少"，影响回答"能省多少钱"，
# 两者会不一致——一栋年耗 220 万度的楼，夜间只超标 1.9 个百分点也能省 80 万度。
# 物业排优先级看的是后者，所以必须分开表达，不能混成一个标签。
IMPACT_HIGH_KWH = 100_000
IMPACT_MEDIUM_KWH = 10_000

# 通用排查方向：任何建筑类型都适用，措辞为疑问句而非控制指令
NIGHT_CHECKS_COMMON = [
    "空调系统夜间是否未回温、也未停机？",
    "走廊、楼梯间等公共区域照明是否长明？",
    "夜间是否仍有非必要设备在运行（机房、实验设备、后厨设备等）？",
]

WEEKEND_CHECKS_COMMON = [
    "周末是否未切换到停机或低负荷模式？",
    "周末是否仍有设备处于待机带电状态？",
    "周末值班区域的负荷是否与全楼混在同一回路，导致无法单独停机？",
]

# 分类型补充的排查方向。只收录有据可依的类型（教育、办公取自技术报告 PDF
# 第五章例 1/例 2），其余类型回落到通用清单，不臆造具体设备。
TYPE_CHECKS = {
    "教育": {
        "night": [
            "教室、办公室空调夜间是否未回温？",
            "实验室或机房设备是否 24 小时空转？",
            "食堂后厨设备是否在打烊后未关？",
        ],
        "weekend": [
            "假期、周末是否未切换到停机模式？",
        ],
    },
    "办公": {
        "night": [
            "办公区空调夜间是否未回温？",
        ],
        "weekend": [
            "中央空调周末是否仍在运行？",
            "茶水间、打印机、饮水机周末是否仍带电？",
            "安防、监控负荷是否被高估，能否单独走回路？",
        ],
    },
    "零售": {
        "night": [
            "营业结束后照明与展示设备是否仍在运行？",
            "冷链设备以外的负荷夜间是否未做分区关闭？",
        ],
    },
    "酒店/住宿": {
        "night": [
            "空置客房是否未做节能控制？",
            "公共区域夜间是否未做分区或调光？",
        ],
    },
    "医疗": {
        "night": [
            "非诊疗区域夜间照明是否未分区控制？",
            "非关键设备夜间是否仍在运行？",
        ],
    },
}

DISCLAIMER_TOTAL_METER = (
    "本结论基于整栋总电表数据，没有空调/照明/插座分项表，"
    "因此只能定位到时间段和排查方向，无法精确指出哪台设备多耗了多少电。"
)

DISCLAIMER_INVESTIGATION = (
    "上述条目为需现场核实的排查方向，不是可直接执行的控制指令；"
    "具体措施应由运维人员结合现场情况判断。"
)


def _severity(observed: float, p75: float) -> str:
    """按超出 P75 的相对幅度定级。

    用相对幅度而不是绝对差值，是因为夜间占比（0-1）和周末比（无上界，实测可达 169）
    量纲完全不同，绝对差值没法共用同一套阈值。
    """
    if p75 <= 0:
        return "high"
    relative = (observed - p75) / p75
    if relative >= 0.25:
        return "high"
    if relative >= 0.10:
        return "medium"
    return "low"


def _impact(saving_kwh: float) -> str:
    """按绝对节电量定影响等级，用于给排查项排优先级。"""
    if saving_kwh >= IMPACT_HIGH_KWH:
        return "high"
    if saving_kwh >= IMPACT_MEDIUM_KWH:
        return "medium"
    return "low"


def _checks_for(building_type: str, kind: str) -> list[str]:
    """拼接某类型的排查方向：通用条目 + 该类型的补充条目。"""
    specific = TYPE_CHECKS.get(building_type, {}).get(kind, [])
    common = NIGHT_CHECKS_COMMON if kind == "night" else WEEKEND_CHECKS_COMMON
    return list(specific) + list(common)


def split_saving(
    total_kwh: float,
    night_ratio: float,
    weekend_ratio: float,
    days_count: int,
    weekend_days: int | None,
    night_p75: float,
    weekend_p75: float,
    known_total_saving: float | None = None,
) -> tuple[float, float]:
    """把节能量拆成"夜间浪费"和"周末浪费"两部分。

    参数：
        known_total_saving: 路径 A 传入库里已算好的 saving_potential_kwh。
            此时夜间部分仍可直接算（只依赖 total_kwh 和占比），
            周末部分由 总节能 - 夜间部分 倒推，避免回扫读数求 weekend_days。
            路径 B 传 None，两部分都现算。

    返回：
        (night_waste, weekend_waste)，单位 kWh。
    """
    night_waste = 0.0
    if night_ratio > night_p75:
        night_waste = total_kwh * (night_ratio - night_p75)

    if known_total_saving is not None:
        # 路径 A：总节能量是既成事实，周末部分倒推，保证两者相加等于库里存的值
        weekend_waste = max(0.0, known_total_saving - night_waste)
        return night_waste, weekend_waste

    # 路径 B：周末浪费按 analyze_database.py 第 122-124 行原样计算
    weekend_waste = 0.0
    if weekend_ratio > weekend_p75 and weekend_days is not None:
        weekend_waste = (
            total_kwh
            * (weekend_ratio - weekend_p75)
            * (weekend_days / max(days_count, 1))
        )
    return night_waste, weekend_waste


def build_advice(metrics: dict, baseline: dict, data_source: str) -> dict:
    """生成一栋建筑的完整节能建议。

    参数：
        metrics: 建筑指标，至少含 building_id / building_type / total_kwh /
            days_count / daily_avg_kwh / night_ratio / weekend_ratio / anomaly_count。
            路径 A 额外带 saving_potential_kwh / saving_pct；
            路径 B 额外带 weekend_days。
        baseline: 该建筑类型的基准线（来自 server/baselines.py）。
        data_source: "library" 或 "upload"，用于在前端标注数据来自哪里。

    返回：
        可直接 JSON 序列化的建议结构。

    异常：
        ValueError —— 基准线缺失或类型对不上。
    """
    if not baseline:
        raise ValueError(f"没有该建筑类型的基准线：{metrics.get('building_type')}")

    building_type = metrics.get("building_type") or ""
    total_kwh = float(metrics["total_kwh"])
    days_count = int(metrics.get("days_count") or 0)
    night_ratio = float(metrics["night_ratio"])
    weekend_ratio = float(metrics["weekend_ratio"])

    night_p75 = baseline["night_ratio"]["p75"]
    weekend_p75 = baseline["weekend_ratio"]["p75"]

    known_total = metrics.get("saving_potential_kwh")
    night_waste, weekend_waste = split_saving(
        total_kwh=total_kwh,
        night_ratio=night_ratio,
        weekend_ratio=weekend_ratio,
        days_count=days_count,
        weekend_days=metrics.get("weekend_days"),
        night_p75=night_p75,
        weekend_p75=weekend_p75,
        known_total_saving=float(known_total) if known_total is not None else None,
    )

    findings: list[dict] = []

    # 夜间空载
    if night_ratio > night_p75:
        findings.append(
            {
                "id": "night",
                "severity": _severity(night_ratio, night_p75),
                "title": "夜间用电高于同类水平",
                "observed": round(night_ratio, 4),
                "baseline_mean": baseline["night_ratio"]["mean"],
                "baseline_p75": night_p75,
                "excess_pp": round((night_ratio - night_p75) * 100, 2),
                "excess_relative_pct": round((night_ratio - night_p75) / night_p75 * 100, 1)
                if night_p75 > 0
                else None,
                "saving_kwh": round(night_waste, 2),
                "saving_yuan": round(night_waste * ELECTRICITY_YUAN_PER_KWH, 2),
                "impact": _impact(night_waste),
                "checks": _checks_for(building_type, "night"),
            }
        )

    # 周末未停机
    if weekend_ratio > weekend_p75:
        findings.append(
            {
                "id": "weekend",
                "severity": _severity(weekend_ratio, weekend_p75),
                "title": "周末用电相对工作日偏高",
                "observed": round(weekend_ratio, 4),
                "baseline_mean": baseline["weekend_ratio"]["mean"],
                "baseline_p75": weekend_p75,
                "excess_pp": None,  # 比值型指标，百分点没有意义
                "excess_relative_pct": round(
                    (weekend_ratio - weekend_p75) / weekend_p75 * 100, 1
                )
                if weekend_p75 > 0
                else None,
                "saving_kwh": round(weekend_waste, 2),
                "saving_yuan": round(weekend_waste * ELECTRICITY_YUAN_PER_KWH, 2),
                "impact": _impact(weekend_waste),
                "checks": _checks_for(building_type, "weekend"),
            }
        )

    # 异常时段：只作提示，不做量化。
    # anomaly_count 是逐条读数的计数，CKW 的 15 分钟表动辄上万条、BDG2 小时表只有几百条，
    # 跨来源不可比，所以这里只陈述事实，不给"异常率高"之类的结论。
    anomaly_count = metrics.get("anomaly_count")
    anomaly_note = None
    if anomaly_count:
        anomaly_note = (
            f"全期检出 {anomaly_count} 条疑似异常读数，"
            "建议结合运行记录复核（异常仅代表统计上偏离本楼自身规律，不代表设备故障）。"
        )

    # 库里那 1 栋 saving_pct 高达 48.38 的脏数据（weekend_ratio 无上界导致公式放大），
    # 必须在展示前截断，否则界面上会出现 4838% 这种数字
    flags: list[str] = []
    raw_saving_pct = metrics.get("saving_pct")
    saving_pct = None
    if raw_saving_pct is not None:
        raw_saving_pct = float(raw_saving_pct)
        if raw_saving_pct > 1:
            flags.append("saving_pct_out_of_range")
        saving_pct = max(0.0, min(1.0, raw_saving_pct))

    # 按节电量倒序，让用户先看到钱最多的那条
    findings.sort(key=lambda f: f["saving_kwh"], reverse=True)

    total_saving = night_waste + weekend_waste

    return {
        "building_id": metrics.get("building_id"),
        "building_type": building_type,
        "data_source": data_source,
        "metrics": {
            "total_kwh": round(total_kwh, 2),
            "days_count": days_count,
            "daily_avg_kwh": round(float(metrics.get("daily_avg_kwh") or 0), 2),
            "night_ratio": round(night_ratio, 4),
            "weekend_ratio": round(weekend_ratio, 4),
            "peak_hour": metrics.get("peak_hour"),
            "offpeak_hour": metrics.get("offpeak_hour"),
            "anomaly_count": anomaly_count,
        },
        "baseline": {
            "count": baseline["count"],
            "night_ratio": baseline["night_ratio"],
            "weekend_ratio": baseline["weekend_ratio"],
            "daily_avg_kwh": baseline["daily_avg_kwh"],
        },
        "findings": findings,
        "total_saving_kwh": round(total_saving, 2),
        "total_saving_yuan": round(total_saving * ELECTRICITY_YUAN_PER_KWH, 2),
        "saving_pct": saving_pct,
        "anomaly_note": anomaly_note,
        "flags": flags,
        "disclaimers": [DISCLAIMER_TOTAL_METER, DISCLAIMER_INVESTIGATION],
    }
