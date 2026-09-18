"""合同费率双时态追溯 API。"""

import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import logic
from .db import init_db
from .timeutil import iso


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()  # 启动时自动建表
    yield


app = FastAPI(
    title="合同费率双时态追溯 API",
    version="1.0.0",
    description="按业务时间（valid_at）与认知时间（known_at）双维度追溯合同费率，修订只追加版本、不覆盖历史。",
    lifespan=lifespan,
)


# ---------- 请求 / 响应模型 ----------

class RateCreate(BaseModel):
    contract_no: str = Field(min_length=1, max_length=128, description="合同号")
    rate: float = Field(description="费率值")
    valid_from: datetime = Field(description="业务生效起点（含），ISO-8601")
    valid_to: Optional[datetime] = Field(default=None, description="业务生效终点（不含），缺省为无限期")
    recorded_at: Optional[datetime] = Field(default=None, description="系统记录时间，缺省为当前时间；同一合同内必须单调递增")
    reason: Optional[str] = Field(default=None, max_length=500, description="录入/修订说明")


class VersionView(BaseModel):
    rate_id: int
    contract_no: str
    rate: float
    version: int
    valid_from: str
    valid_to: Optional[str]
    recorded_at: str
    superseded_at: Optional[str]
    status: str  # current / superseded
    reason: Optional[str]


class RateCreated(BaseModel):
    created: VersionView
    superseded_rate_ids: list[int]


class QuoteResponse(BaseModel):
    contract_no: str
    rate: float
    valid_at: str
    known_at: str
    basis: VersionView = Field(description="命中费率的版本依据")


class TimelineResponse(BaseModel):
    contract_no: str
    versions: list[VersionView]


class CoverageSegment(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from", description="片段起点（含）")
    to: str = Field(description="片段终点（不含）")
    status: str = Field(description="covered / gap / conflict")
    basis: Optional[VersionView] = Field(default=None, description="covered 时命中版本的唯一依据")
    candidates: list[VersionView] = Field(default_factory=list, description="conflict 时的全部候选版本")


class CoverageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    contract_no: str
    from_: str = Field(alias="from", description="查询窗口起点（含）")
    to: str = Field(description="查询窗口终点（不含）")
    known_at: str = Field(description="实际使用的认知时点")
    segments: list[CoverageSegment]


def to_view(row: sqlite3.Row) -> VersionView:
    return VersionView(
        rate_id=row["id"],
        contract_no=row["contract_no"],
        rate=row["rate"],
        version=row["version"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        recorded_at=row["recorded_at"],
        superseded_at=row["superseded_at"],
        status="current" if row["superseded_at"] is None else "superseded",
        reason=row["reason"],
    )


# ---------- 异常处理 ----------

@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"code": "INVALID_REQUEST", "message": "请求参数不合法", "errors": jsonable_encoder(exc.errors())},
    )


# ---------- 路由 ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/rates", response_model=RateCreated, status_code=status.HTTP_201_CREATED)
def create_rate(payload: RateCreate):
    """录入费率。与既有当前版本区间重叠时自动修订：旧版本标记取代、追加新版本，历史保留。"""
    try:
        row, superseded_ids = logic.record_rate(
            contract_no=payload.contract_no,
            rate=payload.rate,
            valid_from=payload.valid_from,
            valid_to=payload.valid_to,
            recorded_at=payload.recorded_at,
            reason=payload.reason,
        )
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    except logic.NonMonotonicRecordTimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "NON_MONOTONIC_RECORDED_AT", "message": str(exc)},
        )
    return RateCreated(created=to_view(row), superseded_rate_ids=superseded_ids)


@app.get("/quote", response_model=QuoteResponse)
def quote(
    contract_no: str = Query(min_length=1, description="合同号"),
    valid_at: datetime = Query(description="业务时点，ISO-8601"),
    known_at: Optional[datetime] = Query(default=None, description="认知时点，缺省为当前时间"),
):
    """报价：返回在 valid_at 有效、且在 known_at 时点可见的唯一费率及版本依据。"""
    rows, ka = logic.find_visible_rates(contract_no, valid_at, known_at)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "NO_MATCH",
                "message": f"合同 '{contract_no}' 在业务时点 {valid_at}、认知时点 {ka} 下无可见且有效的费率",
            },
        )
    if len(rows) > 1:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "OVERLAP",
                "message": f"合同 '{contract_no}' 在该时点坐标下命中 {len(rows)} 条费率，数据存在重叠冲突",
                "candidates": [to_view(r).model_dump() for r in rows],
            },
        )
    row = rows[0]
    return QuoteResponse(
        contract_no=row["contract_no"],
        rate=row["rate"],
        valid_at=iso(valid_at),
        known_at=ka,
        basis=to_view(row),
    )


@app.get("/contracts/{contract_no}/timeline", response_model=TimelineResponse)
def timeline(contract_no: str):
    """版本时间线：列出合同全部版本及其业务/系统时间区间与状态。"""
    rows = logic.get_timeline(contract_no)
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "CONTRACT_NOT_FOUND", "message": f"合同 '{contract_no}' 无任何费率版本"},
        )
    return TimelineResponse(contract_no=contract_no, versions=[to_view(r) for r in rows])


@app.get("/contracts/{contract_no}/coverage", response_model=CoverageResponse)
def coverage(
    contract_no: str,
    from_: datetime = Query(alias="from", description="查询窗口起点（含），ISO-8601"),
    to: datetime = Query(description="查询窗口终点（不含），ISO-8601"),
    known_at: Optional[datetime] = Query(default=None, description="认知时点，缺省为当前时间"),
):
    """覆盖率分析：以 known_at 可见版本的业务区间边界把 [from, to) 切为连续片段，
    逐段标记 covered（附唯一版本依据）/ gap / conflict（附全部候选），
    分类与候选完全相同的相邻段合并。"""
    try:
        segments, ka = logic.compute_coverage(contract_no, from_, to, known_at)
    except logic.InvalidIntervalError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": "INVALID_INTERVAL", "message": str(exc)},
        )
    return CoverageResponse(
        contract_no=contract_no,
        from_=iso(from_),
        to=iso(to),
        known_at=ka,
        segments=[
            CoverageSegment(
                from_=seg["from"],
                to=seg["to"],
                status=seg["status"],
                basis=to_view(seg["candidates"][0]) if seg["status"] == "covered" else None,
                candidates=[to_view(r) for r in seg["candidates"]] if seg["status"] == "conflict" else [],
            )
            for seg in segments
        ],
    )
