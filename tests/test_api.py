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


# ---------- 费用试算 ----------

def test_charge_preview_single_version_full_cover(client):
    post_rate(client, contract_no="CP-1", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-07-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = client.post("/contracts/CP-1/charge-preview", json={
        "known_at": "2026-02-01T00:00:00Z",
        "lines": [{"from": "2026-01-01T00:00:00Z", "to": "2026-01-03T00:00:00Z",
                   "base_amount": 10000}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["contract_no"] == "CP-1"
    assert data["known_at"] == "2026-02-01T00:00:00.000000+00:00"
    line, = data["lines"]
    assert line["from"] == "2026-01-01T00:00:00.000000+00:00"
    assert line["to"] == "2026-01-03T00:00:00.000000+00:00"
    assert line["base_amount"] == 10000.0
    assert line["status"] == "ok"
    assert line["amount"] == 500.0
    seg, = line["segments"]
    assert seg["status"] == "covered"
    assert seg["rate_id"] == 1
    assert seg["rate"] == 0.05
    assert seg["ratio"] == 1.0
    assert seg["amount"] == 500.0
    assert seg["candidates"] == []
    assert data["total_amount"] == 500.0


def test_charge_preview_split_across_versions(client):
    """账单行跨越两个相接版本的业务边界 → 按时长比例拆分计费。"""
    post_rate(client, contract_no="CP-2", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-01-03T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="CP-2", rate=0.09,
              valid_from="2026-01-03T00:00:00Z", valid_to="2026-02-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = client.post("/contracts/CP-2/charge-preview", json={
        "known_at": "2026-01-10T00:00:00Z",
        "lines": [{"from": "2026-01-01T00:00:00Z", "to": "2026-01-05T00:00:00Z",
                   "base_amount": 10000}],
    })
    assert r.status_code == 200, r.text
    line, = r.json()["lines"]
    segs = line["segments"]
    # 4 天均分两段：前 2 天 @0.05，后 2 天 @0.09
    assert [(s["from"], s["to"], s["status"]) for s in segs] == [
        ("2026-01-01T00:00:00.000000+00:00", "2026-01-03T00:00:00.000000+00:00", "covered"),
        ("2026-01-03T00:00:00.000000+00:00", "2026-01-05T00:00:00.000000+00:00", "covered"),
    ]
    assert [s["rate"] for s in segs] == [0.05, 0.09]
    assert [s["rate_id"] for s in segs] == [1, 2]
    assert [s["ratio"] for s in segs] == [0.5, 0.5]
    assert [s["amount"] for s in segs] == [250.0, 450.0]
    assert line["status"] == "ok"
    assert line["amount"] == 700.0
    assert r.json()["total_amount"] == 700.0


def test_charge_preview_multiple_lines_total(client):
    post_rate(client, contract_no="CP-3", rate=0.1,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = client.post("/contracts/CP-3/charge-preview", json={
        "known_at": "2026-02-01T00:00:00Z",
        "lines": [
            {"from": "2026-03-01T00:00:00Z", "to": "2026-03-03T00:00:00Z", "base_amount": 1000},
            {"from": "2026-06-01T00:00:00Z", "to": "2026-06-11T00:00:00Z", "base_amount": 500},
        ],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert [ln["amount"] for ln in data["lines"]] == [100.0, 50.0]
    assert all(ln["status"] == "ok" for ln in data["lines"])
    assert data["total_amount"] == 150.0


def test_charge_preview_gap_refuses_total(client):
    """部分区间无费率 → 标记 gap，该行与总额均拒绝给出金额。"""
    post_rate(client, contract_no="CP-4", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2026-02-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    r = client.post("/contracts/CP-4/charge-preview", json={
        "known_at": "2026-01-10T00:00:00Z",
        "lines": [{"from": "2026-01-15T00:00:00Z", "to": "2026-02-15T00:00:00Z",
                   "base_amount": 10000}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    line, = data["lines"]
    segs = line["segments"]
    assert [(s["from"], s["to"], s["status"]) for s in segs] == [
        ("2026-01-15T00:00:00.000000+00:00", "2026-02-01T00:00:00.000000+00:00", "covered"),
        ("2026-02-01T00:00:00.000000+00:00", "2026-02-15T00:00:00.000000+00:00", "gap"),
    ]
    # covered 段照常计费（17/31 时长占比），gap 段不计费
    assert segs[0]["rate_id"] == 1
    assert segs[0]["ratio"] == round(17 / 31, 6)
    assert segs[0]["amount"] == 274.193548
    assert segs[1]["rate_id"] is None
    assert segs[1]["amount"] is None
    assert line["status"] == "gap"
    assert line["amount"] is None
    assert data["total_amount"] is None


def test_charge_preview_reflects_known_at(client):
    """同一账单行，修订前后的认知时点得到不同的试算结果。"""
    post_rate(client, contract_no="CP-5", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    post_rate(client, contract_no="CP-5", rate=0.09,
              valid_from="2026-06-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-03-01T00:00:00Z")
    lines = [{"from": "2026-01-01T00:00:00Z", "to": "2026-07-01T00:00:00Z",
              "base_amount": 10000}]

    # 修订前：旧版本覆盖整行
    old = client.post("/contracts/CP-5/charge-preview",
                      json={"known_at": "2026-02-01T00:00:00Z", "lines": lines}).json()
    assert old["lines"][0]["status"] == "ok"
    assert old["lines"][0]["amount"] == 500.0
    assert old["total_amount"] == 500.0

    # 修订后：上半年变为 gap，下半年按新版本计费，总额被拒绝
    new = client.post("/contracts/CP-5/charge-preview",
                      json={"known_at": "2026-03-02T00:00:00Z", "lines": lines}).json()
    line, = new["lines"]
    assert [s["status"] for s in line["segments"]] == ["gap", "covered"]
    covered = line["segments"][1]
    assert covered["rate_id"] == 2
    assert covered["rate"] == 0.09
    assert covered["amount"] == round(10000 * 0.09 * 30 / 181, 6)
    assert line["status"] == "gap"
    assert line["amount"] is None
    assert new["total_amount"] is None


def test_charge_preview_conflict(client, tmp_path, monkeypatch):
    """直接注入互相重叠的脏数据 → 重叠片段标记 conflict 并附全部候选，拒绝总额。"""
    post_rate(client, contract_no="CP-6", rate=0.05,
              valid_from="2026-01-01T00:00:00Z", valid_to="2027-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    db = os.environ["DATABASE_PATH"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO rate_versions "
            "(contract_no, rate, valid_from, valid_to, recorded_at, version) "
            "VALUES ('CP-6', 0.99, '2026-06-01T00:00:00.000000+00:00', "
            "        '2026-09-01T00:00:00.000000+00:00', "
            "        '2026-01-01T00:00:00.000000+00:00', 2)"
        )
    r = client.post("/contracts/CP-6/charge-preview", json={
        "known_at": "2026-02-01T00:00:00Z",
        "lines": [{"from": "2026-01-01T00:00:00Z", "to": "2027-01-01T00:00:00Z",
                   "base_amount": 10000}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    line, = data["lines"]
    segs = line["segments"]
    assert [s["status"] for s in segs] == ["covered", "conflict", "covered"]
    conflict = segs[1]
    assert conflict["from"] == "2026-06-01T00:00:00.000000+00:00"
    assert conflict["to"] == "2026-09-01T00:00:00.000000+00:00"
    assert conflict["amount"] is None
    assert conflict["rate_id"] is None
    assert [c["version"] for c in conflict["candidates"]] == [1, 2]
    assert line["status"] == "conflict"
    assert line["amount"] is None
    assert data["total_amount"] is None


def test_charge_preview_unknown_contract_is_all_gap(client):
    r = client.post("/contracts/GHOST/charge-preview", json={
        "known_at": "2026-06-01T00:00:00Z",
        "lines": [{"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z",
                   "base_amount": 1000}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    line, = data["lines"]
    assert [(s["from"], s["to"], s["status"]) for s in line["segments"]] == [
        ("2026-01-01T00:00:00.000000+00:00", "2026-02-01T00:00:00.000000+00:00", "gap"),
    ]
    assert line["status"] == "gap"
    assert line["amount"] is None
    assert data["total_amount"] is None


def test_charge_preview_default_known_at_is_now(client):
    # recorded_at 缺省 = 当前时间；不指定 known_at 的试算应立即可见
    post_rate(client, contract_no="CP-7", rate=0.03,
              valid_from="2020-01-01T00:00:00Z")
    r = client.post("/contracts/CP-7/charge-preview", json={
        "lines": [{"from": "2026-09-01T00:00:00Z", "to": "2026-09-02T00:00:00Z",
                   "base_amount": 1000}],
    })
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["lines"][0]["status"] == "ok"
    assert data["total_amount"] == 30.0


def test_charge_preview_validation(client):
    post_rate(client, contract_no="CP-8", rate=0.05,
              valid_from="2026-01-01T00:00:00Z",
              recorded_at="2026-01-01T00:00:00Z")
    # 区间非法：from >= to → INVALID_INTERVAL
    for frm, to in [("2026-02-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                    ("2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z")]:
        r = client.post("/contracts/CP-8/charge-preview", json={
            "lines": [{"from": frm, "to": to, "base_amount": 100}]})
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "INVALID_INTERVAL"
    # 金额非法：负数 → INVALID_AMOUNT
    r = client.post("/contracts/CP-8/charge-preview", json={
        "lines": [{"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z",
                   "base_amount": -5}]})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "INVALID_AMOUNT"
    # 空账单行 / 缺少 lines / known_at 非法 → INVALID_REQUEST
    r = client.post("/contracts/CP-8/charge-preview", json={"lines": []})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    r = client.post("/contracts/CP-8/charge-preview", json={})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"
    r = client.post("/contracts/CP-8/charge-preview", json={
        "known_at": "not-a-date",
        "lines": [{"from": "2026-01-01T00:00:00Z", "to": "2026-02-01T00:00:00Z",
                   "base_amount": 100}]})
    assert r.status_code == 422
    assert r.json()["code"] == "INVALID_REQUEST"


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}
