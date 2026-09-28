# -*- coding: utf-8 -*-
"""
节能建议后端 API（FastAPI）。

启动：
    python server/api.py
    浏览器打开 http://127.0.0.1:8000/docs 可以看到全部接口

真机预览（手机和电脑在同一局域网）时需要改成对外可访问：
    设置环境变量 ADVICE_HOST=0.0.0.0 后重启，再用 http://<本机IP>:8000 访问。

注意：建筑类型里带斜杠的取值（酒店/住宿、科研/实验室、市政/设施、住宅/小商业）
不能做路径参数，凡是传类型的地方一律用查询参数。
"""
import os

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

import advisor
import baselines
import csv_ingest
import db
import usage_profile

HOST = os.environ.get("ADVICE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ADVICE_PORT", "8000"))

app = FastAPI(
    title="智碳建筑 · 节能建议 API",
    description=(
        "基于 1670 栋建筑、3270 万条实测读数的同类对标节能建议。\n\n"
        "所有结论基于整栋总电表数据，无分项表，只给出排查方向，不构成控制指令。"
    ),
    version="1.0.0",
)

# 小程序不走浏览器同源策略，这里放开是为了方便用浏览器直接调试接口
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/meta", summary="基准线来源与方法说明")
def get_meta():
    """返回对标基准线是怎么算出来的，供小程序标注口径。"""
    try:
        return baselines.metadata()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@app.get("/api/types", summary="全部建筑类型及数量")
def get_types():
    """列出 19 种建筑类型和各自的建筑数量（按数量倒序）。"""
    return {"types": db.list_types()}


@app.get("/api/baseline", summary="某建筑类型的对标基准线")
def get_baseline(type: str = Query(..., description="建筑类型，如 教育、酒店/住宿")):
    """返回该类型的均值 / P75 / 中位数，用于"和同类比"的对标。"""
    try:
        baseline = baselines.get_baseline(type)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if baseline is None:
        raise HTTPException(status_code=404, detail=f"没有这种建筑类型：{type}")

    # count 放在顶层更好取，types 里已经带了一份
    return {"type": type, **baseline}


@app.get("/api/buildings", summary="某类型下的建筑列表")
def get_buildings(
    type: str = Query(..., description="建筑类型"),
    limit: int = Query(100, ge=1, le=500, description="最多返回多少栋"),
):
    """按节能量倒序列出该类型下的建筑。"""
    buildings = db.list_buildings(type, limit=limit)
    if not buildings:
        raise HTTPException(status_code=404, detail=f"该类型下没有建筑：{type}")
    return {"type": type, "count": len(buildings), "buildings": buildings}


@app.get("/api/advice/{building_id}", summary="路径A：库内建筑的建议")
def get_advice(building_id: str):
    """取某栋建筑的节能建议。

    节能量直接读 building_summary 里已算好的 saving_potential_kwh，不重算——
    因为周末浪费公式用到的 weekend_days 没有落盘，重算需要回扫该栋全部读数。
    """
    metrics = db.get_building(building_id)
    if metrics is None:
        raise HTTPException(
            status_code=404,
            detail=f"库里没有这栋建筑，或它因数据不足没有分析结果：{building_id}",
        )

    try:
        baseline = baselines.get_baseline(metrics["building_type"])
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    try:
        return advisor.build_advice(metrics, baseline, data_source="library")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/api/advice/upload", summary="路径B：上传 CSV 得到建议")
async def upload_advice(
    file: UploadFile = File(..., description="含时间列和电量列的 CSV"),
    type: str = Form(..., description="建筑类型，决定用哪套同类基准线"),
    building_id: str = Form("我的建筑", description="楼名，仅用于回显"),
):
    """解析用户上传的电表数据并给出建议。

    走的是和库内建筑完全相同的指标定义与节能公式，两条路径结果可直接比较。
    """
    try:
        baseline = baselines.get_baseline(type)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if baseline is None:
        raise HTTPException(status_code=404, detail=f"没有这种建筑类型：{type}")

    raw = await file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="上传的文件是空的。")

    try:
        metrics = csv_ingest.compute_metrics(raw, building_id=building_id, building_type=type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    try:
        return advisor.build_advice(metrics, baseline, data_source="upload")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/api/health", summary="健康检查")
def health():
    """给启动脚本和小程序探活用。"""
    return {"status": "ok", "database": db.DB_PATH.exists()}


@app.get("/api/usage-profile", summary="设备档案分析的楼栋参考曲线")
def get_usage_profile(building_id: str = Query(...)):
    """Return a database-derived proxy, not fabricated device-level measurements."""
    try:
        return usage_profile.build_profile(building_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


if __name__ == "__main__":
    import uvicorn
    from pathlib import Path
    from fastapi.staticfiles import StaticFiles

    dist_dir = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if dist_dir.exists():
        app.mount("/", StaticFiles(directory=str(dist_dir), html=True), name="static")

    print(f"API started: http://{HOST}:{PORT}/docs")
    uvicorn.run(app, host=HOST, port=PORT)
