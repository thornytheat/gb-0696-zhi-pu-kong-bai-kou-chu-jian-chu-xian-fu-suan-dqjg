"""委托方归属、机构权限隔离、稳定别名与导出幂等。"""
import csv
import io
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app import store, identity
from app.pipeline import run, sample_text
from app.errors import BusinessError

client = TestClient(app, raise_server_exceptions=False)

ACME = {"Authorization": "Bearer demo-token-acme"}
GREEN = {"Authorization": "Bearer demo-token-green"}
LAB = {"Authorization": "Bearer demo-token-lab"}
BAD = {"Authorization": "Bearer nope"}


@pytest.fixture(autouse=True)
def _reset_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    yield


def _save(text=None, **kw):
    text = sample_text() if text is None else text
    return store.save(text, run(text), **kw)


def _parse_csv(body):
    return list(csv.reader(io.StringIO(body.lstrip("﻿"))))


# ---------- 归属登记 ----------

def test_unattributed_run_marked_explicitly():
    item = _save()
    owners = set(item["attribution"].values())
    assert owners == {identity.UNASSIGNED}
    # 结果行与隔离行都能在归属表里找到对应物理行
    result_lines = {str(r["line"]) for r in item["result"]["final"]}
    quar_lines = {str(r["line"]) for r in item["result"]["quarantine"]}
    assert result_lines | quar_lines <= set(item["attribution"])


def test_file_level_attribution_applies_to_all_data_lines():
    item = _save(client_id="CL-ACME")
    data_owners = {v for k, v in item["attribution"].items() if k != "1"}
    assert data_owners == {"CL-ACME"}


def test_line_level_attribution_splits_owners():
    text = sample_text()
    first_sample_line = next(r["line"] for r in run(text)["final"])
    item = _save(line_clients={first_sample_line: "CL-GREEN"})
    assert item["attribution"][str(first_sample_line)] == "CL-GREEN"
    # 未逐行指定的行保持未归属，而不是猜测
    assert identity.UNASSIGNED in set(item["attribution"].values())


def test_unknown_client_rejected():
    with pytest.raises(BusinessError, match="未注册委托方"):
        _save(client_id="CL-GHOST")


def test_attribution_rerun_does_not_overwrite_confirmed_ownership():
    item1 = _save(client_id="CL-ACME")
    # 同一内容再次入库但不带归属：已确认归属保留
    item2 = _save()
    assert item1["id"] == item2["id"]
    assert set(item2["attribution"].values()) == {"CL-ACME"}


def test_unassigned_rows_can_be_backfilled_later():
    # 先进来的文件未核归属，实验室核对后补登：UNASSIGNED 是缺失而非定论
    item1 = _save()
    target = next(iter(item1["attribution"]))
    item2 = _save(line_clients={int(target): "CL-ACME"})
    assert item1["id"] == item2["id"]
    assert item2["attribution"][target] == "CL-ACME"
    assert identity.UNASSIGNED in set(item2["attribution"].values())  # 其余行仍待核


def test_confirmed_owner_cannot_be_reassigned():
    item = _save(client_id="CL-ACME")
    target = next(iter(item["attribution"]))
    with pytest.raises(BusinessError) as exc:
        _save(line_clients={int(target): "CL-GREEN"})
    assert exc.value.code == "OWNERSHIP_CONFLICT" and exc.value.status == 409


# ---------- 旧数据升级 ----------

def test_legacy_run_without_attribution_still_reviewable_by_lab():
    item = _save()
    # 模拟升级前的旧库：清空归属列
    with store.connect() as db:
        db.execute("UPDATE runs SET attribution=NULL")
    legacy = store.get(item["id"])
    assert legacy["attribution"] == {}  # 未归属但原始记录完整可读
    resp = client.get(f"/api/export/{item['id']}", headers=LAB)
    assert resp.status_code == 200 and "quarantine" in resp.text


# ---------- 权限隔离 ----------

def test_client_cannot_see_other_clients_run():
    item = _save(client_id="CL-ACME")
    assert client.get(f"/api/runs/{item['id']}", headers=GREEN).status_code == 404
    # 未归属批次对任何委托方都不可见
    plain = _save(sample_text() + "\n")
    assert client.get(f"/api/runs/{plain['id']}", headers=ACME).status_code == 404


def test_client_runs_list_scoped():
    item = _save(client_id="CL-ACME")
    _save(sample_text() + "\n", client_id="CL-GREEN")
    acme_ids = {r["id"] for r in client.get("/api/runs", headers=ACME).json()}
    green_ids = {r["id"] for r in client.get("/api/runs", headers=GREEN).json()}
    assert item["id"] in acme_ids and item["id"] not in green_ids
    assert acme_ids.isdisjoint(green_ids)


def test_client_export_blocked_when_unassigned_rows_exist():
    text = sample_text()
    first = next(r["line"] for r in run(text)["final"])
    item = _save(line_clients={first: "CL-ACME"})  # 仅一行有归属
    resp = client.get(f"/api/export/{item['id']}", headers=ACME)
    assert resp.status_code == 409 and resp.json()["error"] == "UNASSIGNED_ROWS"


def test_unassigned_intermediate_lines_also_block_export():
    # 标曲/空白等中间行不进入 final/quarantine，但归属未核清同样不得外发
    text = sample_text()
    result = run(text)
    item = _save(line_clients={
        str(r["line"]): "CL-ACME"
        for r in result["final"] + result["quarantine"]
    })
    resp = client.get(f"/api/export/{item['id']}", headers=ACME)
    assert resp.status_code == 409 and resp.json()["error"] == "UNASSIGNED_ROWS"
    # 补登全部物理行后放行，且证据只含本机构行
    full = {line: "CL-ACME" for line in item["attribution"]}
    fixed = _save(line_clients=full)
    assert client.get(f"/api/export/{fixed['id']}", headers=ACME).status_code == 200


def test_unknown_token_unauthorized():
    assert client.get("/api/runs", headers=BAD).status_code == 401


def test_client_cannot_review():
    item = _save(client_id="CL-ACME")
    resp = client.post("/api/review", headers=ACME,
                       json={"id": item["id"], "revision": 0, "decision": "退回"})
    assert resp.status_code == 403


def test_client_cannot_register_other_owner():
    resp = client.post("/api/run", headers=ACME,
                       json={"text": sample_text(), "client_id": "CL-GREEN"})
    assert resp.status_code == 403


# ---------- 委托方导出与稳定别名 ----------

def test_client_export_contains_only_own_rows_and_aliases():
    item = _save(client_id="CL-ACME")
    resp = client.get(f"/api/export/{item['id']}", headers=ACME)
    assert resp.status_code == 200
    rows = _parse_csv(resp.text)
    header, manifest, *data = rows
    assert "sample_alias" in header
    result_rows = [r for r in data if r[3] == "result"]
    quar_rows = [r for r in data if r[3] == "quarantine"]
    assert len(result_rows) == len(item["result"]["final"])
    # 隔离原因仍随证据输出，委托方可核对
    reasons = {r[6] for r in quar_rows}
    assert {"DUPLICATE", "COLUMN_COUNT"} & reasons
    # 全部归属本机构，且原始样品编号不出现在任何字段
    assert all(r[5] == "CL-ACME" for r in data)
    assert "S001" not in resp.text and "BAD1" not in resp.text
    aliases = {r[7] for r in result_rows}
    assert all(a.startswith("CL-ACME-S") for a in aliases)


def test_alias_stable_across_runs_for_same_client():
    text1 = sample_text()
    run1 = _save(text1, client_id="CL-ACME")
    client.get(f"/api/export/{run1['id']}", headers=ACME)
    with store.connect() as db:
        alias_before = dict(db.execute(
            "SELECT original_sample_id, alias FROM sample_aliases WHERE client_id='CL-ACME'"))
    # 第二次运行包含相同原编号（再加一个新样品）
    text2 = text1 + "S999,sample,Pb,300,100,0,1,ug/L,B01\n"
    run2 = _save(text2, client_id="CL-ACME")
    client.get(f"/api/export/{run2['id']}", headers=ACME)
    with store.connect() as db:
        after = dict(db.execute(
            "SELECT original_sample_id, alias FROM sample_aliases WHERE client_id='CL-ACME'").fetchall())
    for original, alias in alias_before.items():
        assert after[original] == alias  # 历次测值用同一稳定别名核对
    assert after["S999"].startswith("CL-ACME-S")


def test_clients_have_independent_alias_sequences():
    item = _save(client_id="CL-ACME")
    # 同一物理批次登记给 GREEN（模拟另一份同内容文件归属另一机构）
    other = _save(sample_text() + " \n", client_id="CL-GREEN")
    client.get(f"/api/export/{item['id']}", headers=ACME)
    client.get(f"/api/export/{other['id']}", headers=GREEN)
    with store.connect() as db:
        acme = [r[0] for r in db.execute(
            "SELECT alias FROM sample_aliases WHERE client_id='CL-ACME' ORDER BY seq")]
        green = [r[0] for r in db.execute(
            "SELECT alias FROM sample_aliases WHERE client_id='CL-GREEN' ORDER BY seq")]
    assert acme[0] == "CL-ACME-S000001" and green[0] == "CL-GREEN-S000001"
    # 仅凭别名无法跨机构对应：同序号不同前缀，且不暴露原编号
    assert set(acme).isdisjoint(green)


# ---------- 重试幂等 ----------

def test_retry_export_creates_no_new_alias_or_ledger_entry():
    item = _save(client_id="CL-ACME")
    first = client.get(f"/api/export/{item['id']}", headers=ACME)
    second = client.get(f"/api/export/{item['id']}", headers=ACME)
    assert first.text == second.text  # 证据逐字节一致
    with store.connect() as db:
        alias_count = db.execute(
            "SELECT COUNT(*) FROM sample_aliases WHERE client_id='CL-ACME'").fetchone()[0]
        ledger = db.execute(
            "SELECT * FROM exports WHERE run_id=? AND client_id='CL-ACME'",
            (item["id"],)).fetchall()
    assert len(ledger) == 1  # 重试不新增导出登记
    visible_rows = [r for r in _parse_csv(first.text) if r[3] in ("result", "quarantine")]
    assert ledger[0]["rows_exported"] == len(visible_rows)
    # 同一原编号（跨分析项/批次复用，或同时出现在结果与隔离中）共享同一别名，
    # 故别名数 = 去重后的原编号数
    originals = {r["sample_id"] for r in item["result"]["final"]}
    originals |= {r["id"] for r in item["result"]["quarantine"] if r.get("id")}
    # 无编号隔离行（坏列、重复表头）以物理行号作为别名键
    originals |= {f"__line__{r['line']}" for r in item["result"]["quarantine"] if not r.get("id")}
    assert alias_count == len(originals)
    assert ledger[0]["aliases_minted"] == alias_count  # 首次导出一次性铸齐
    # 重试状态记录在导出登记中，证据本身逐字节稳定，可反复核对
    ledger_rows = client.get("/api/exports", headers=ACME).json()
    assert len(ledger_rows) == 1


def test_alias_mint_is_idempotent_under_repeated_calls():
    with store.connect() as db:
        a1, created1 = identity.mint_alias(db, "CL-ACME", "S001")
        a2, created2 = identity.mint_alias(db, "CL-ACME", "S001")
    assert a1 == a2 and created1 is True and created2 is False


# ---------- 实验室全量证据 ----------

def test_lab_export_keeps_original_ids_and_owners():
    item = _save(client_id="CL-ACME")
    resp = client.get(f"/api/export/{item['id']}", headers=LAB)
    assert resp.status_code == 200 and "S001" in resp.text and "CL-ACME" in resp.text
    rows = _parse_csv(resp.text)
    assert rows[1][3] == "manifest" and '"scope": "lab-full"' in rows[1][8]


def test_no_token_defaults_to_lab_role():
    item = _save()
    assert client.get(f"/api/export/{item['id']}").status_code == 200


# ---------- 防泄露边界 ----------

def test_client_export_never_leaks_other_owners_or_original_ids():
    text = sample_text()
    first = next(r["line"] for r in run(text)["final"])
    second = next(r["line"] for r in run(text)["final"] if r["line"] != first)
    item = _save(line_clients={first: "CL-ACME", second: "CL-GREEN"})
    # 两行各有归属但其余行未归属：两个机构都应被拦截，不返回任何行
    assert client.get(f"/api/export/{item['id']}", headers=ACME).status_code == 409
    assert client.get(f"/api/export/{item['id']}", headers=GREEN).status_code == 409


def test_fully_split_file_each_client_gets_only_own_slice():
    text = sample_text()
    result = run(text)
    line_owners = {}
    for idx, r in enumerate(result["final"]):
        line_owners[r["line"]] = "CL-ACME" if idx % 2 == 0 else "CL-GREEN"
    for q in result["quarantine"]:
        line_owners[q["line"]] = "CL-ACME"
    item = _save(line_clients=line_owners)
    acme = _parse_csv(client.get(f"/api/export/{item['id']}", headers=ACME).text)
    green = _parse_csv(client.get(f"/api/export/{item['id']}", headers=GREEN).text)
    acme_data = [r for r in acme if r[3] in ("result", "quarantine")]
    green_data = [r for r in green if r[3] in ("result", "quarantine")]
    assert all(r[5] == "CL-ACME" for r in acme_data)
    assert all(r[5] == "CL-GREEN" for r in green_data)
    # 两份证据的行集合互斥（按物理行号），别名空间也不相交
    assert {r[4] for r in acme_data}.isdisjoint({r[4] for r in green_data})
    assert {r[7] for r in acme_data}.isdisjoint({r[7] for r in green_data})
    # 任何一份证据都不含另一机构编号
    assert "CL-GREEN" not in "\n".join(",".join(r) for r in acme)
    assert "CL-ACME" not in "\n".join(",".join(r) for r in green)


def test_retry_through_store_mints_zero_aliases():
    item = _save(client_id="CL-ACME")
    principal_obj = {"role": "client", "client_id": "CL-ACME", "name": "甲方环境监测"}
    _, _, first_record = store.prepare_client_export(item["id"], principal_obj)
    _, _, second_record = store.prepare_client_export(item["id"], principal_obj)
    assert first_record["retry"] is False and second_record["retry"] is True
    assert first_record["aliases_minted"] > 0 and second_record["aliases_minted"] == 0


def test_unknown_owner_requested_via_api_rejected_before_persist():
    before = client.get("/api/runs", headers=LAB).json()
    resp = client.post("/api/run", headers=LAB,
                       json={"text": sample_text(), "client_id": "CL-GHOST"})
    assert resp.status_code == 422 and resp.json()["error"] == "CLIENT_UNKNOWN"
    assert client.get("/api/runs", headers=LAB).json() == before  # 未落库任何批次


def test_client_exports_list_is_scoped():
    item = _save(client_id="CL-ACME")
    other = _save(sample_text() + "\n", client_id="CL-GREEN")
    client.get(f"/api/export/{item['id']}", headers=ACME)
    client.get(f"/api/export/{other['id']}", headers=GREEN)
    acme = client.get("/api/exports", headers=ACME).json()
    green = client.get("/api/exports", headers=GREEN).json()
    assert {r["client_id"] for r in acme} == {"CL-ACME"}
    assert {r["client_id"] for r in green} == {"CL-GREEN"}


def test_lab_history_sees_all_runs_with_attribution_distribution():
    # 回归：实验室主体是 dict 而非 None，不能误走委托方过滤分支
    _save(client_id="CL-ACME")
    lab_runs = client.get("/api/runs", headers=LAB).json()
    assert len(lab_runs) == 1
    assert lab_runs[0]["attribution"] is not None
    no_token = client.get("/api/runs").json()
    assert len(no_token) == 1  # 无令牌默认实验室角色同样全量
