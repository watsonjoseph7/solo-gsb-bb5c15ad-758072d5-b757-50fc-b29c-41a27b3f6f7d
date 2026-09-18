"""端到端 API 测试：每个用例使用独立临时 SQLite 库。"""

import os
import sqlite3

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    from app.main import app

    with TestClient(app) as c:
        yield c


def post_rate(client, **kw):
    return client.post("/rates", json=kw)


# ---------- 录入与基本报价 ----------

def test_create_and_quote(client):
    r = post_rate(
        client,
        contract_no="C-1",
        rate=0.05,
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-07-01T00:00:00Z",
        recorded_at="2026-01-01T00:00:00Z",
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"]["version"] == 1
    assert body["created"]["status"] == "current"
    assert body["superseded_rate_ids"] == []

    q = client.get(
        "/quote",
        params={
            "contract_no": "C-1",
            "valid_at": "2026-03-01T00:00:00Z",
            "known_at": "2026-02-01T00:00:00Z",
        },
    )
    assert q.status_code == 200, q.text
    data = q.json()
    assert data["rate"] == 0.05
    assert data["basis"]["version"] == 1
    assert data["basis"]["recorded_at"] == "2026-01-01T00:00:00.000000+00:00"


def test_quote_boundary_left_closed_right_open(client):
    post_rate(
        client,
        contract_no="C-2",
        rate=0.1,
        valid_from="2026-01-01T00:00:00Z",
        valid_to="2026-02-01T00:00:00Z",
        recorded_at="2025-12-01T00:00:00Z",
    )
    # valid_from 边界：含
    assert client.get("/quote", params={
        "contract_no": "C-2", "valid_at": "2026-01-01T00:00:00Z",
        "known_at": "2026-01-01T00:00:00Z"}).status_code == 200
    # valid_to 边界：不含
    assert client.get("/quote", params={
        "contract_no": "C-2", "valid_at": "2026-02-01T00:00:00Z",
        "known_at": "2026-01-01T00:00:00Z"}).status_code == 404


def test_open_ended_interval(client):
    post_rate(
        client,
        contract_no="C-3",
        rate=0.07,
        valid_from="2026-01-01T00:00:00Z",
        recorded_at="2026-01-01T00:00:00Z",
    )
    q = client.get("/quote", params={
        "contract_no": "C-3", "valid_at": "2099-06-01T00:00:00Z",
        "known_at": "2026-01-02T00:00:00Z"})
    assert q.status_code == 200
    assert q.json()["rate"] == 0.07


# ---------- 修订：追加版本，不覆盖历史 ----------

def test_revision_appends_version_and_keeps_history(client):
    post_rate(client, contract_no="C-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z", reason="初始费率")
    r2 = post_rate(client, contract_no="C-4", rate=0.08,
                   valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
                   recorded_at="2026-06-01T00:00:00Z", reason="费率更正")
    assert r2.status_code == 201
    body = r2.json()
    assert body["created"]["version"] == 2
    assert len(body["superseded_rate_ids"]) == 1

    # 修订前的认知时点：仍看到旧费率 0.05
    old = client.get("/quote", params={
        "contract_no": "C-4", "valid_at": "2026-03-01T00:00:00Z",
        "known_at": "2026-03-01T00:00:00Z"})
    assert old.status_code == 200
    assert old.json()["rate"] == 0.05
    assert old.json()["basis"]["version"] == 1

    # 修订后的认知时点：看到新费率 0.08
    new = client.get("/quote", params={
        "contract_no": "C-4", "valid_at": "2026-03-01T00:00:00Z",
        "known_at": "2026-06-02T00:00:00Z"})
    assert new.status_code == 200
    assert new.json()["rate"] == 0.08
    assert new.json()["basis"]["version"] == 2

    # 时间线保留两个版本，旧版本状态为 superseded
    tl = client.get("/contracts/C-4/timeline")
    assert tl.status_code == 200
    versions = tl.json()["versions"]
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["status"] == "superseded"
    assert versions[0]["superseded_at"] == "2026-06-01T00:00:00.000000+00:00"
    assert versions[1]["status"] == "current"


def test_partial_overlap_revision(client):
    """部分重叠的修订只取代重叠的当前版本，不相干版本不受影响。"""
    post_rate(client, contract_no="C-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="C-5", rate=0.06,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    # 新区间覆盖整个 2026 年 → 取代上面两个当前版本
    r = post_rate(client, contract_no="C-5", rate=0.09,
                  valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
                  recorded_at="2026-03-01T00:00:00Z")
    assert len(r.json()["superseded_rate_ids"]) == 2
    assert r.json()["created"]["version"] == 3

    q = client.get("/quote", params={
        "contract_no": "C-5", "valid_at": "2026-08-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z"})
    assert q.json()["rate"] == 0.09


def test_recorded_at_must_be_monotonic(client):
    post_rate(client, contract_no="C-6", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    r = post_rate(client, contract_no="C-6", rate=0.06,
                  valid_from="2026-01-01T00:00:00Z",
                  recorded_at="2026-05-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "NON_MONOTONIC_RECORDED_AT"


def test_invalid_interval_rejected(client):
    r = post_rate(client, contract_no="C-7", rate=0.05,
                  valid_from="2026-06-01T00:00:00Z", valid_to="2026-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_malformed_request_422(client):
    # 缺少必填字段 / 非法时间格式 → 统一的 INVALID_REQUEST
    r = post_rate(client, rate=0.05, valid_from="2026-01-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    r = post_rate(client, contract_no="C-7", rate=0.05, valid_from="not-a-date")
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


# ---------- 报价错误分支 ----------

def test_quote_no_match_404(client):
    q = client.get("/quote", params={
        "contract_no": "NOPE", "valid_at": "2026-01-01T00:00:00Z",
        "known_at": "2026-01-01T00:00:00Z"})
    assert q.status_code == 404
    assert q.json()["detail"]["code"] == "NO_MATCH"


def test_quote_not_yet_recorded_404(client):
    """known_at 早于 recorded_at 时，费率尚不可见。"""
    post_rate(client, contract_no="C-8", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    q = client.get("/quote", params={
        "contract_no": "C-8", "valid_at": "2026-03-01T00:00:00Z",
        "known_at": "2026-01-15T00:00:00Z"})
    assert q.status_code == 404


def test_quote_overlap_409(client, tmp_path, monkeypatch):
    """直接注入互相重叠的脏数据（绕过录入保护），报价应报 409 冲突。"""
    post_rate(client, contract_no="C-9", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('C-9', 0.99, '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-09-01T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    q = client.get("/quote", params={
        "contract_no": "C-9", "valid_at": "2026-07-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"})
    assert q.status_code == 409
    detail = q.json()["detail"]
    assert detail["code"] == "OVERLAP"
    assert len(detail["candidates"]) == 2


def test_quote_default_known_at_is_now(client):
    # recorded_at 缺省 = 当前时间；不指定 known_at 的报价应立即可见
    post_rate(client, contract_no="C-10", rate=0.03,
              valid_from="2020-01-01T00:00:00Z")
    q = client.get("/quote", params={
        "contract_no": "C-10", "valid_at": "2026-09-18T00:00:00Z"})
    assert q.status_code == 200
    assert q.json()["rate"] == 0.03


# ---------- 时间线 ----------

def test_timeline_unknown_contract_404(client):
    r = client.get("/contracts/GHOST/timeline")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "CONTRACT_NOT_FOUND"


# ---------- 覆盖率 ----------

def test_coverage_covered_and_gap(client):
    post_rate(client, contract_no="CV-1", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-07-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = client.get("/contracts/CV-1/coverage", params={
        "from": "2026-03-01T00:00:00Z", "to": "2026-09-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["contract_no"] == "CV-1"
    assert data["from"] == "2026-03-01T00:00:00.000000+00:00"
    assert data["to"] == "2026-09-01T00:00:00.000000+00:00"
    assert data["known_at"] == "2026-02-01T00:00:00.000000+00:00"
    segs = data["segments"]
    assert [(s["from"], s["to"], s["status"]) for s in segs] == [
        ("2026-03-01T00:00:00.000000+00:00", "2026-07-01T00:00:00.000000+00:00", "covered"),
        ("2026-07-01T00:00:00.000000+00:00", "2026-09-01T00:00:00.000000+00:00", "gap"),
    ]
    # covered 段附唯一版本依据；gap 段无依据、无候选
    assert segs[0]["basis"]["version"] == 1
    assert segs[0]["basis"]["rate"] == 0.05
    assert segs[0]["candidates"] == []
    assert segs[1]["basis"] is None
    assert segs[1]["candidates"] == []


def test_coverage_from_must_precede_to(client):
    for frm, to in [("2026-09-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                    ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")]:
        r = client.get("/contracts/CV-X/coverage", params={"from": frm, "to": to})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_coverage_missing_params_422(client):
    r = client.get("/contracts/CV-X/coverage")
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


def test_coverage_no_visible_versions_is_single_gap(client):
    # 合同不存在 → 覆盖整个查询窗口的 gap
    r = client.get("/contracts/GHOST/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-06-01T00:00:00Z"})
    assert r.status_code == 200
    segs = r.json()["segments"]
    assert [(s["from"], s["to"], s["status"]) for s in segs] == [
        ("2026-01-01T00:00:00.000000+00:00", "2027-01-01T00:00:00.000000+00:00", "gap"),
    ]

    # 合同存在但 known_at 早于 recorded_at → 版本尚不可见，仍整窗 gap
    post_rate(client, contract_no="CV-2", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    r = client.get("/contracts/CV-2/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-01-15T00:00:00Z"})
    assert [s["status"] for s in r.json()["segments"]] == ["gap"]


def test_coverage_reflects_known_at(client):
    """同一业务窗口，修订前后的认知时点看到不同的覆盖结果。"""
    post_rate(client, contract_no="CV-3", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="CV-3", rate=0.08,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-03-01T00:00:00Z")

    # 修订前的认知时点：旧版本覆盖整个窗口
    old = client.get("/contracts/CV-3/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"}).json()
    assert [(s["status"], s["basis"]["version"] if s["basis"] else None)
            for s in old["segments"]] == [("covered", 1)]

    # 修订后的认知时点：旧版本已被取代，上半年变为 gap，下半年为新版本
    new = client.get("/contracts/CV-3/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-03-02T00:00:00Z"}).json()
    assert [(s["from"], s["status"]) for s in new["segments"]] == [
        ("2026-01-01T00:00:00.000000+00:00", "gap"),
        ("2026-06-01T00:00:00.000000+00:00", "covered"),
    ]
    assert new["segments"][1]["basis"]["version"] == 2
    assert new["segments"][1]["basis"]["rate"] == 0.08


def test_coverage_multiple_versions_segmentation(client):
    """相接的两个当前版本在边界处切分；相邻 covered 段候选不同，不合并。"""
    post_rate(client, contract_no="CV-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-06-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="CV-4", rate=0.06,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = client.get("/contracts/CV-4/coverage", params={
        "from": "2025-06-01T00:00:00Z", "to": "2027-06-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"}).json()
    segs = data["segments"]
    assert [(s["status"], s["basis"]["rate"] if s["basis"] else None) for s in segs] == [
        ("gap", None), ("covered", 0.05), ("covered", 0.06), ("gap", None),
    ]
    # 片段连续铺满整个窗口
    assert segs[0]["from"] == "2025-06-01T00:00:00.000000+00:00"
    assert segs[-1]["to"] == "2027-06-01T00:00:00.000000+00:00"
    for prev, nxt in zip(segs, segs[1:]):
        assert prev["to"] == nxt["from"]
    assert segs[1]["to"] == "2026-06-01T00:00:00.000000+00:00"


def test_coverage_conflict(client):
    """直接注入互相重叠的脏数据 → 重叠区段标记 conflict 并附全部候选。"""
    post_rate(client, contract_no="CV-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('CV-5', 0.99, '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-09-01T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    data = client.get("/contracts/CV-5/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
        "known_at": "2026-02-01T00:00:00Z"}).json()
    segs = data["segments"]
    assert [s["status"] for s in segs] == ["covered", "conflict", "covered"]
    conflict = segs[1]
    assert conflict["from"] == "2026-06-01T00:00:00.000000+00:00"
    assert conflict["to"] == "2026-09-01T00:00:00.000000+00:00"
    assert conflict["basis"] is None
    assert [c["version"] for c in conflict["candidates"]] == [1, 2]


def test_coverage_open_ended_and_default_known_at(client):
    """无限期区间裁剪到窗口终点；known_at 缺省为当前时间即可见。"""
    post_rate(client, contract_no="CV-6", rate=0.07,
              valid_from="2026-01-01T00:00:00Z")
    data = client.get("/contracts/CV-6/coverage", params={
        "from": "2026-01-01T00:00:00Z", "to": "2099-01-01T00:00:00Z"}).json()
    segs = data["segments"]
    assert [(s["from"], s["to"], s["status"]) for s in segs] == [
        ("2026-01-01T00:00:00.000000+00:00", "2099-01-01T00:00:00.000000+00:00", "covered"),
    ]


# ---------- 计费预览 ----------

def post_preview(client, contract, **kw):
    return client.post(f"/contracts/{contract}/charge-preview", json=kw)


def test_charge_preview_single_version_full_cover(client):
    post_rate(client, contract_no="P-1", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = post_preview(client, "P-1", known_at="2026-06-01T00:00:00Z", lines=[
        {"from": "2026-02-01T00:00:00Z", "to": "2026-03-01T00:00:00Z", "base_amount": 1000.0},
    ])
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["contract_no"] == "P-1"
    assert data["known_at"] == "2026-06-01T00:00:00.000000+00:00"
    assert data["status"] == "ok"
    (line,) = data["lines"]
    assert line["status"] == "ok"
    assert line["from"] == "2026-02-01T00:00:00.000000+00:00"
    assert line["to"] == "2026-03-01T00:00:00.000000+00:00"
    assert line["base_amount"] == 1000.0
    (seg,) = line["segments"]
    assert seg["status"] == "covered"
    assert seg["portion"] == 1.0
    assert seg["rate_id"] == 1
    assert seg["rate"] == 0.05
    assert seg["charge"] == 50.0
    assert seg["candidates"] == []
    assert line["line_total"] == 50.0
    assert data["total_amount"] == 50.0


def test_charge_preview_splits_at_version_boundaries(client):
    """账单行被两个相接版本的边界切开，各段按时长比例折算。"""
    post_rate(client, contract_no="P-2", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-01-03T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="P-2", rate=0.08,
              valid_from="2026-01-03T00:00:00Z", valid_to="2026-01-05T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = post_preview(client, "P-2", known_at="2026-02-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-05T00:00:00Z", "base_amount": 1000.0},
    ]).json()
    (line,) = data["lines"]
    segs = line["segments"]
    assert [(s["status"], s["rate"], s["portion"]) for s in segs] == [
        ("covered", 0.05, 0.5),
        ("covered", 0.08, 0.5),
    ]
    assert [s["rate_id"] for s in segs] == [1, 2]
    assert [s["charge"] for s in segs] == [25.0, 40.0]
    assert line["line_total"] == 65.0
    assert data["status"] == "ok"
    assert data["total_amount"] == 65.0


def test_charge_preview_prorates_by_duration(client):
    """非对半的时长占比：1/3 与 2/3，费用按 6 位小数取舍。"""
    post_rate(client, contract_no="P-3", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-01-02T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="P-3", rate=0.08,
              valid_from="2026-01-02T00:00:00Z", valid_to="2026-01-04T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = post_preview(client, "P-3", known_at="2026-02-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-04T00:00:00Z", "base_amount": 300.0},
    ]).json()
    (line,) = data["lines"]
    segs = line["segments"]
    assert [s["portion"] for s in segs] == [0.333333, 0.666667]
    assert [s["charge"] for s in segs] == [5.0, 16.0]
    assert line["line_total"] == 21.0
    assert data["total_amount"] == 21.0


def test_charge_preview_respects_known_at(client):
    """同一账单行，修订前后的认知时点命中不同费率版本。"""
    post_rate(client, contract_no="P-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="P-4", rate=0.08,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-06-01T00:00:00Z")
    line = {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z", "base_amount": 100.0}
    old = post_preview(client, "P-4", known_at="2026-03-01T00:00:00Z", lines=[line]).json()
    assert old["lines"][0]["segments"][0]["rate"] == 0.05
    assert old["lines"][0]["segments"][0]["rate_id"] == 1
    new = post_preview(client, "P-4", known_at="2026-06-02T00:00:00Z", lines=[line]).json()
    assert new["lines"][0]["segments"][0]["rate"] == 0.08
    assert new["lines"][0]["segments"][0]["rate_id"] == 2


def test_charge_preview_gap_rejects_total(client):
    """部分区间无费率 → 该行标记 gap、行合计为 null，整体拒绝总额；正常行不受影响。"""
    post_rate(client, contract_no="P-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-02-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = post_preview(client, "P-5", known_at="2026-03-01T00:00:00Z", lines=[
        {"from": "2026-01-15T00:00:00Z", "to": "2026-03-01T00:00:00Z", "base_amount": 900.0},
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z", "base_amount": 100.0},
    ]).json()
    assert data["status"] == "rejected"
    assert data["total_amount"] is None
    gap_line, ok_line = data["lines"]
    assert gap_line["status"] == "gap"
    assert gap_line["line_total"] is None
    assert [s["status"] for s in gap_line["segments"]] == ["covered", "gap"]
    gap_seg = gap_line["segments"][1]
    assert gap_seg["from"] == "2026-02-01T00:00:00.000000+00:00"
    assert gap_seg["rate_id"] is None
    assert gap_seg["rate"] is None
    assert gap_seg["charge"] is None
    assert gap_seg["candidates"] == []
    # 未受影响的行仍正常计价
    assert ok_line["status"] == "ok"
    assert ok_line["line_total"] == 5.0


def test_charge_preview_conflict_rejects_total(client):
    """注入重叠脏数据 → 重叠区段标记 conflict 并附候选，行与总额均被拒绝。"""
    post_rate(client, contract_no="P-6", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('P-6', 0.99, '2026-01-02T00:00:00.000000+00:00', "
            "        '2026-01-04T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    data = post_preview(client, "P-6", known_at="2026-02-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-05T00:00:00Z", "base_amount": 400.0},
    ]).json()
    (line,) = data["lines"]
    assert line["status"] == "conflict"
    assert line["line_total"] is None
    assert data["status"] == "rejected"
    assert data["total_amount"] is None
    assert [s["status"] for s in line["segments"]] == ["covered", "conflict", "covered"]
    conflict = line["segments"][1]
    assert conflict["from"] == "2026-01-02T00:00:00.000000+00:00"
    assert conflict["to"] == "2026-01-04T00:00:00.000000+00:00"
    assert conflict["portion"] == 0.5
    assert conflict["charge"] is None
    assert [(c["rate_id"], c["rate"], c["version"]) for c in conflict["candidates"]] == [
        (1, 0.05, 1), (2, 0.99, 2),
    ]


def test_charge_preview_mixed_gap_and_conflict(client):
    """同一行同时出现 gap 与 conflict 段 → 行状态 mixed。"""
    post_rate(client, contract_no="P-7", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-01-03T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('P-7', 0.99, '2026-01-02T00:00:00.000000+00:00', "
            "        '2026-01-03T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    data = post_preview(client, "P-7", known_at="2026-02-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-05T00:00:00Z", "base_amount": 400.0},
    ]).json()
    (line,) = data["lines"]
    assert [s["status"] for s in line["segments"]] == ["covered", "conflict", "gap"]
    assert line["status"] == "mixed"
    assert line["line_total"] is None
    assert data["status"] == "rejected"
    assert data["total_amount"] is None


def test_charge_preview_unknown_contract_is_all_gap(client):
    """合同不存在（或该认知时点无可见版本）→ 整行 gap，拒绝总额，不报 404。"""
    r = post_preview(client, "GHOST", known_at="2026-06-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z", "base_amount": 100.0},
    ])
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "rejected"
    assert data["total_amount"] is None
    (line,) = data["lines"]
    assert line["status"] == "gap"
    assert [s["status"] for s in line["segments"]] == ["gap"]


def test_charge_preview_multiple_lines_sum(client):
    post_rate(client, contract_no="P-8", rate=0.5,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    data = post_preview(client, "P-8", known_at="2026-06-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z", "base_amount": 100.0},
        {"from": "2026-02-01T00:00:00Z", "to": "2026-02-03T00:00:00Z", "base_amount": 200.0},
    ]).json()
    assert data["status"] == "ok"
    assert [line["line_total"] for line in data["lines"]] == [50.0, 100.0]
    assert data["total_amount"] == 150.0


def test_charge_preview_default_known_at_is_now(client):
    # recorded_at 缺省 = 当前时间；不指定 known_at 的预览应立即可见
    post_rate(client, contract_no="P-9", rate=0.1, valid_from="2020-01-01T00:00:00Z")
    data = post_preview(client, "P-9", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-01-02T00:00:00Z", "base_amount": 10.0},
    ]).json()
    assert data["status"] == "ok"
    assert data["total_amount"] == 1.0


def test_charge_preview_invalid_interval(client):
    for frm, to in [("2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                    ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")]:
        r = post_preview(client, "P-10", lines=[{"from": frm, "to": to, "base_amount": 100.0}])
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_INTERVAL"


def test_charge_preview_invalid_amount(client):
    r = post_preview(client, "P-11", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z", "base_amount": -1.0},
    ])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_AMOUNT"


def test_charge_preview_future_known_at_rejected(client):
    r = post_preview(client, "P-12", known_at="2999-01-01T00:00:00Z", lines=[
        {"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z", "base_amount": 100.0},
    ])
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_KNOWN_AT"


def test_charge_preview_malformed_request_422(client):
    # 空账单行列表 / 缺少必填字段 → 统一的 INVALID_REQUEST
    r = post_preview(client, "P-13", lines=[])
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    r = post_preview(client, "P-13", lines=[{"from": "2026-01-01T00:00:00Z",
                                             "to": "2026-02-01T00:00:00Z"}])
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
