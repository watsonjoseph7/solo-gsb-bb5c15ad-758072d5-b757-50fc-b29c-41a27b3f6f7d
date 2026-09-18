"""双时态核心逻辑。

两个时间维度：
- 业务时间（valid time）  [valid_from, valid_to)：费率在现实中生效的区间，左闭右开；
- 系统时间（system time） [recorded_at, superseded_at)：该记录被系统认知为真的区间，左闭右开。

修订 = 追加新版本并把被覆盖的旧版本标记 superseded_at，历史行永不修改、永不删除。
"""

import math
import sqlite3
from datetime import datetime
from typing import Optional

from .db import connect
from .timeutil import iso, now_iso

# 无限期区间的上界占位（仅用于 SQL 比较）
INF = "9999-12-31T23:59:59.999999+00:00"

# 费用/比例的小数精度
MONEY_DIGITS = 6


class InvalidIntervalError(ValueError):
    """区间终点不晚于起点（valid_to <= valid_from，或查询窗口 from >= to）。"""


class NonMonotonicRecordTimeError(ValueError):
    """recorded_at 早于该合同已有记录，破坏系统时间单调性。"""


class InvalidAmountError(ValueError):
    """账单行金额非法（负数或 NaN/Inf 等非有限值）。"""


def record_rate(
    contract_no: str,
    rate: float,
    valid_from: datetime,
    valid_to: Optional[datetime],
    recorded_at: Optional[datetime],
    reason: Optional[str],
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[sqlite3.Row, list[int]]:
    """录入费率（追加式）。

    若新区间与当前认知中（superseded_at IS NULL）的既有版本重叠，
    则将重叠旧版本在 recorded_at 时刻标记为被取代，再插入新版本。

    返回 (新版本行, 被取代的版本 id 列表)。
    """
    vf = iso(valid_from)
    vt = iso(valid_to) if valid_to is not None else None
    ra = iso(recorded_at) if recorded_at is not None else now_iso()

    if vt is not None and vt <= vf:
        raise InvalidIntervalError("valid_to 必须晚于 valid_from")

    own_conn = conn is None
    if own_conn:
        conn = connect()
    try:
        state = conn.execute(
            "SELECT MAX(recorded_at) AS max_ra, COALESCE(MAX(version), 0) AS max_v "
            "FROM rate_versions WHERE contract_no = ?",
            (contract_no,),
        ).fetchone()
        if state["max_ra"] is not None and ra < state["max_ra"]:
            raise NonMonotonicRecordTimeError(
                f"recorded_at={ra} 早于合同 {contract_no} 已有记录时间 {state['max_ra']}，"
                "系统时间必须单调递增"
            )

        overlaps = conn.execute(
            "SELECT id FROM rate_versions "
            "WHERE contract_no = ? AND superseded_at IS NULL "
            "  AND valid_from < COALESCE(?, ?) "
            "  AND COALESCE(valid_to, ?) > ?",
            (contract_no, vt, INF, INF, vf),
        ).fetchall()

        superseded_ids = [row["id"] for row in overlaps]
        for version_id in superseded_ids:
            conn.execute(
                "UPDATE rate_versions SET superseded_at = ? WHERE id = ?",
                (ra, version_id),
            )

        cursor = conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contract_no, rate, vf, vt, ra, state["max_v"] + 1, reason),
        )
        new_row = conn.execute(
            "SELECT * FROM rate_versions WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()

        if own_conn:
            conn.commit()
        return new_row, superseded_ids
    finally:
        if own_conn:
            conn.close()


def find_visible_rates(
    contract_no: str, valid_at: datetime, known_at: Optional[datetime]
) -> tuple[list[sqlite3.Row], str]:
    """查询在 (valid_at, known_at) 双时态坐标下可见且有效的所有版本。

    正常情况下结果应为 0 或 1 条；多于 1 条说明数据存在重叠冲突。
    返回 (命中行列表, 实际使用的 known_at)。
    """
    va = iso(valid_at)
    ka = iso(known_at) if known_at is not None else now_iso()
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? "
            "  AND valid_from <= ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "  AND recorded_at <= ? "
            "  AND (superseded_at IS NULL OR superseded_at > ?)",
            (contract_no, va, va, ka, ka),
        ).fetchall()
    return list(rows), ka


def get_timeline(contract_no: str) -> list[sqlite3.Row]:
    """合同的全部版本，按版本号升序（即录入顺序）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions WHERE contract_no = ? ORDER BY version ASC, id ASC",
            (contract_no,),
        ).fetchall()
    return list(rows)


def _segment_window(rows: list[sqlite3.Row], f: str, t: str) -> list[dict]:
    """把窗口 [f, t) 按 rows 中版本的业务边界切成连续片段并分类。

    rows 为同一认知时点下可见的版本行，与窗口不相交的行在此过滤；
    将其业务区间裁剪到窗口后，以所有区间边界把窗口切成连续片段；
    每段按命中的候选版本数分类：0 → gap，1 → covered，>1 → conflict；
    分类与候选完全相同的相邻段合并为一段。

    返回 {"from", "to", "status", "candidates": [Row]} 字典列表，连续铺满
    整个窗口；rows 为空时自然得到覆盖整窗的单个 gap 段。
    """
    # 可见版本的业务区间裁剪到查询窗口（NULL valid_to 用 INF 占位参与比较）
    clipped = [
        (max(row["valid_from"], f), min(row["valid_to"] or INF, t), row)
        for row in rows
        if row["valid_from"] < t and (row["valid_to"] or INF) > f
    ]

    # 以所有区间边界（含窗口端点）切分 [f, t)
    bounds = sorted({f, t, *(p for s, e, _ in clipped for p in (s, e))})

    segments = []
    last_key = None  # 上一段的 (分类, 候选 id 元组)，用于相邻段合并
    for seg_from, seg_to in zip(bounds, bounds[1:]):
        # 区间边界全部参与切分，故与片段相交的裁剪区间必完整覆盖该片段
        candidates = [row for s, e, row in clipped if s <= seg_from and e >= seg_to]
        if not candidates:
            seg_status = "gap"
        elif len(candidates) == 1:
            seg_status = "covered"
        else:
            seg_status = "conflict"
        key = (seg_status, tuple(row["id"] for row in candidates))
        if key == last_key:
            segments[-1]["to"] = seg_to
        else:
            segments.append(
                {"from": seg_from, "to": seg_to, "status": seg_status, "candidates": candidates}
            )
            last_key = key
    return segments


def compute_coverage(
    contract_no: str,
    from_dt: datetime,
    to_dt: datetime,
    known_at: Optional[datetime],
) -> tuple[list[dict], str]:
    """计算查询窗口 [from, to) 在 known_at 认知时点下的覆盖率分段。

    先筛出 known_at 可见的版本（recorded_at <= known_at 且未被取代），
    再以 _segment_window 把窗口切分为连续片段并逐段分类。

    返回 (segments, 实际使用的 known_at)。
    """
    f = iso(from_dt)
    t = iso(to_dt)
    if f >= t:
        raise InvalidIntervalError("from 必须早于 to")
    ka = iso(known_at) if known_at is not None else now_iso()

    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? "
            "  AND recorded_at <= ? "
            "  AND (superseded_at IS NULL OR superseded_at > ?) "
            "  AND valid_from < ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY version ASC, id ASC",
            (contract_no, ka, ka, t, f),
        ).fetchall()
    return _segment_window(rows, f, t), ka


def compute_charge_preview(
    contract_no: str,
    lines: list[tuple[datetime, datetime, float]],
    known_at: Optional[datetime],
) -> tuple[list[dict], Optional[float], str]:
    """费用试算：把每条账单行 [from, to) 按 known_at 可见版本的业务边界拆分，
    每片段费用 = base_amount × 命中费率 × (片段时长 / 行时长)，再汇总行费用与总额。

    lines 为 (from, to, base_amount) 三元组列表，不能为空；
    base_amount 必须是不小于 0 的有限数值，否则抛 InvalidAmountError；
    任一行 from >= to 抛 InvalidIntervalError。

    片段为 gap（无费率）或 conflict（重叠冲突）时不计费；账单行含此类片段时
    该行 amount 为 None；任一行未完全覆盖时总额为 None（拒绝总额）。
    金额与时长比例均保留 MONEY_DIGITS 位小数（金额按未取整的比例计算后再取整）。

    返回 (lines_result, total_or_None, 实际使用的 known_at)。
    """
    ka = iso(known_at) if known_at is not None else now_iso()

    norm = []  # (f, t, base_amount)，时间已规范化为等宽 ISO 字符串
    for idx, (from_dt, to_dt, base_amount) in enumerate(lines):
        f, t = iso(from_dt), iso(to_dt)
        if f >= t:
            raise InvalidIntervalError(f"第 {idx + 1} 行账单区间非法：from 必须早于 to")
        if not math.isfinite(base_amount) or base_amount < 0:
            raise InvalidAmountError(
                f"第 {idx + 1} 行 base_amount 必须是不小于 0 的有限数值"
            )
        norm.append((f, t, float(base_amount)))

    lo = min(f for f, _, _ in norm)
    hi = max(t for _, t, _ in norm)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM rate_versions "
            "WHERE contract_no = ? "
            "  AND recorded_at <= ? "
            "  AND (superseded_at IS NULL OR superseded_at > ?) "
            "  AND valid_from < ? "
            "  AND (valid_to IS NULL OR valid_to > ?) "
            "ORDER BY version ASC, id ASC",
            (contract_no, ka, ka, hi, lo),
        ).fetchall()

    out_lines = []
    grand_total = 0.0
    all_covered = True
    for f, t, base_amount in norm:
        span = (datetime.fromisoformat(t) - datetime.fromisoformat(f)).total_seconds()
        segments = []
        line_total = 0.0
        line_status = "ok"
        for seg in _segment_window(rows, f, t):
            seg_span = (
                datetime.fromisoformat(seg["to"]) - datetime.fromisoformat(seg["from"])
            ).total_seconds()
            ratio = seg_span / span
            if seg["status"] == "covered":
                hit = seg["candidates"][0]
                amount = round(base_amount * hit["rate"] * ratio, MONEY_DIGITS)
                line_total += amount
                segments.append({
                    "from": seg["from"], "to": seg["to"], "status": "covered",
                    "rate_id": hit["id"], "rate": hit["rate"],
                    "ratio": round(ratio, MONEY_DIGITS), "amount": amount,
                    "candidates": [],
                })
            else:
                if seg["status"] == "conflict":
                    line_status = "conflict"
                elif line_status == "ok":
                    line_status = "gap"
                segments.append({
                    "from": seg["from"], "to": seg["to"], "status": seg["status"],
                    "rate_id": None, "rate": None,
                    "ratio": round(ratio, MONEY_DIGITS), "amount": None,
                    "candidates": seg["candidates"] if seg["status"] == "conflict" else [],
                })
        if line_status == "ok":
            line_amount = round(line_total, MONEY_DIGITS)
            grand_total += line_amount
        else:
            line_amount = None
            all_covered = False
        out_lines.append({
            "from": f, "to": t, "base_amount": base_amount,
            "status": line_status, "amount": line_amount, "segments": segments,
        })
    total = round(grand_total, MONEY_DIGITS) if all_covered else None
    return out_lines, total, ka
