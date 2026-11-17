"""M8 支付对账：按日对账、关账保留、调整转日、冲回、更正、幂等、截点、续跑。"""
from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from app.modules.m8_recon import journal
from app.modules.m8_recon import service as recon

# 样例数据算出的确定值：8 张订单（1 张取消）+ 6 笔支付
OBSERVATIONS = 14          # 8 订单 + 6 支付
ENTRIES = 15               # 取消的订单多记一笔冲减
DAY_TOTALS = {             # 账日 → (应收, 实收)
    "2026-11-01": ("1469.50", "1469.50"),
    "2026-11-02": ("130.50", "130.50"),
    "2026-11-03": ("199.90", "199.90"),   # 56.00 取消单已冲减
    "2026-11-04": ("318.00", "318.00"),
}


def _run_recon(settings, store, **kwargs):
    return recon.run_sync(settings, store, **kwargs)


def _day(store, day):
    row = store.recon_day(day)
    assert row is not None, f"账日不存在：{day}"
    return row


def test_run_books_daily_recon_from_sample(settings, store, run):
    """按日支付对账：应收=订单有效额，实收=支付有效额，差异=实收-应收。"""
    result = _run_recon(settings, store)
    assert result["status"] == "done"
    assert result["processed"] == result["total"] == OBSERVATIONS
    assert result["booked"] == ENTRIES
    for day, (order_total, payment_total) in DAY_TOTALS.items():
        row = _day(store, day)
        assert row["status"] == "open"
        assert row["order_total"] == order_total
        assert row["payment_total"] == payment_total
        assert row["diff"] == "0.00"
    overview = recon.overview(store)
    assert overview["totals"]["order_total"] == "2117.90"
    assert overview["totals"]["payment_total"] == "2117.90"
    assert overview["totals"]["day_count"] == 4


def test_invariant_rerun_books_no_new_entries(settings, store, run):
    """不变量：重复重跑不新增调整，分录数与账日合计一个都不变。"""
    _run_recon(settings, store)
    before = store.count_recon()
    days_before = store.recon_days()
    second = _run_recon(settings, store)
    assert second["booked"] == 0
    assert second["skipped"] == OBSERVATIONS
    assert store.count_recon()["recon_entries"] == before["recon_entries"]
    assert store.count_recon()["recon_days"] == before["recon_days"]
    assert [(d["day"], d["order_total"], d["payment_total"], d["diff"])
            for d in store.recon_days()] == [
        (d["day"], d["order_total"], d["payment_total"], d["diff"])
        for d in days_before]


def test_same_flow_changed_amount_is_correction(settings, store, run):
    """相同流水改金额识别为更正：补记差额，原流水保留，再投同一笔跳过。"""
    _run_recon(settings, store)
    result = recon.post_events(store, {"events": [
        {"kind": "payment", "flow_id": "9007199254740993",
         "order_id": "SO20261101001", "amount": "1234.55",
         "biz_day": "2026-11-01"}]})
    assert result["booked"] == 1
    entry = result["entries"][0]
    assert entry["kind"] == "correction"
    assert entry["amount"] == "0.05"
    assert entry["day"] == "2026-11-01"
    originals = [e for e in store.recon_entries(biz_day="2026-11-01")
                 if e["ref"] == "9007199254740993"]
    assert [e["kind"] for e in originals] == ["payment", "correction"]
    assert _day(store, "2026-11-01")["payment_total"] == "1469.55"
    again = recon.post_events(store, {"events": [
        {"kind": "payment", "flow_id": "9007199254740993",
         "order_id": "SO20261101001", "amount": "1234.55",
         "biz_day": "2026-11-01"}]})
    assert again["booked"] == 0 and again["skipped"] == 1


def test_cancel_and_reprice_reflect_latest_on_open_day(settings, store, run):
    """订单取消与改价后，未关账日反映最新应收；原分录不覆盖。"""
    _run_recon(settings, store)
    recon.post_events(store, {"events": [
        {"kind": "order", "order_id": "SO20261102003", "amount": "90.00",
         "biz_day": "2026-11-02", "status": "paid"},
        {"kind": "order", "order_id": "SO20261102004", "amount": "42.00",
         "biz_day": "2026-11-02", "status": "cancel"}]})
    row = _day(store, "2026-11-02")
    assert row["order_total"] == "90.00"      # 88.50→90.00，42.00 取消
    assert row["payment_total"] == "130.50"
    assert row["diff"] == "40.50"             # 实收 130.50 - 应收 90.00
    kinds = [e["kind"] for e in store.recon_entries(biz_day="2026-11-02")
             if e["ref"] in ("SO20261102003", "SO20261102004")]
    assert kinds == ["order", "order", "order_delta", "cancel"]


def test_close_with_diff_keeps_frozen_snapshot(settings, store, run):
    """未平账可带差异关账；关账后订单、支付与差异金额按关账时保留。"""
    _run_recon(settings, store)
    recon.post_events(store, {"events": [
        {"kind": "payment", "flow_id": "9007199254740993",
         "order_id": "SO20261101001", "amount": "1234.55",
         "biz_day": "2026-11-01"}]})
    closed = recon.close_day(store, "2026-11-01")
    assert closed["code"] == "R_CLOSE_WITH_DIFF"
    assert closed["diff"] == "0.05"
    assert closed["payment_total"] == "1469.55"
    # 关账后再有他日流水，已关账日一动不动
    recon.post_events(store, {"events": [
        {"kind": "payment", "flow_id": "NEW1", "order_id": "SO20261102003",
         "amount": "5.00", "biz_day": "2026-11-02"}]})
    row = _day(store, "2026-11-01")
    assert row["status"] == "closed"
    assert row["payment_total"] == "1469.55"
    assert row["diff"] == "0.05"
    assert row["cutoff_seq"] > 0


def test_late_and_correction_move_to_next_open_day(settings, store, run):
    """迟到与更正转入下一未关账日作关联原日的调整；差额不跨错账日。"""
    _run_recon(settings, store)
    recon.close_day(store, "2026-11-01")
    result = recon.post_events(store, {"events": [
        {"kind": "payment", "flow_id": "LATE1", "order_id": "SO20261101001",
         "amount": "10.00", "biz_day": "2026-11-01"},
        {"kind": "payment", "flow_id": "9007199254740994",
         "order_id": "SO20261101002", "amount": "240.00",
         "biz_day": "2026-11-01"}]})
    landed = {(e["kind"], e["day"], e["link_day"], e["amount"]) for e in result["entries"]}
    assert landed == {("payment", "2026-11-02", "2026-11-01", "10.00"),
                      ("correction", "2026-11-02", "2026-11-01", "5.00")}
    # 原日冻结不动；入账日自身差异不含调整
    assert _day(store, "2026-11-01")["payment_total"] == "1469.50"
    day2 = _day(store, "2026-11-02")
    assert day2["diff"] == "0.00"
    assert day2["adjust_total"] == "15.00"


def test_adjustment_hops_over_runs_of_closed_days(settings, store, run):
    """连续关账时，调整落在下一个仍未关账的账日。"""
    _run_recon(settings, store)
    recon.close_day(store, "2026-11-01")
    recon.close_day(store, "2026-11-02")
    result = recon.post_events(store, {"events": [
        {"kind": "payment", "flow_id": "LATE2", "order_id": "SO20261101001",
         "amount": "3.00", "biz_day": "2026-11-01"}]})
    entry = result["entries"][0]
    assert entry["day"] == "2026-11-03"
    assert entry["link_day"] == "2026-11-01"
    assert _day(store, "2026-11-03")["adjust_total"] == "3.00"
    assert _day(store, "2026-11-02")["adjust_total"] == "0.00"


def test_revoke_books_reversal_and_keeps_original(settings, store, run):
    """撤销支付要冲回：原流水保留，冲回另记一笔；重复撤销不重复冲回。"""
    _run_recon(settings, store)
    result = recon.post_events(store, {"events": [
        {"kind": "revoke", "flow_id": "9007199254740994"}]})
    entry = result["entries"][0]
    assert entry["kind"] == "reversal"
    assert entry["amount"] == "-235.00"
    assert entry["day"] == "2026-11-01"
    flows = [e for e in store.recon_entries(biz_day="2026-11-01")
             if e["ref"] == "9007199254740994"]
    assert [e["kind"] for e in flows] == ["payment", "reversal"]
    assert _day(store, "2026-11-01")["payment_total"] == "1234.50"
    again = recon.post_events(store, {"events": [
        {"kind": "revoke", "flow_id": "9007199254740994"}]})
    assert again["booked"] == 0 and again["skipped"] == 1


def test_revoke_on_closed_day_reverses_as_adjustment(settings, store, run):
    """已关账日的撤销同样冲回，但落在下一未关账日作调整。"""
    _run_recon(settings, store)
    recon.close_day(store, "2026-11-01")
    result = recon.post_events(store, {"events": [
        {"kind": "revoke", "flow_id": "9007199254740994"}]})
    entry = result["entries"][0]
    assert entry["kind"] == "reversal"
    assert entry["day"] == "2026-11-02"
    assert entry["link_day"] == "2026-11-01"
    assert _day(store, "2026-11-01")["payment_total"] == "1469.50"
    assert _day(store, "2026-11-02")["adjust_total"] == "-235.00"


def test_interrupted_run_resumes_from_recorded_progress(settings, store, run):
    """中断续跑：部分记账后，续跑接回已记账进度，不重复记账。"""
    partial = _run_recon(settings, store, limit=5)
    assert partial["status"] == "partial"
    assert partial["processed"] == 5
    entries_at_partial = store.count_recon()["recon_entries"]
    assert 0 < entries_at_partial < ENTRIES
    resumed = recon.resume(settings, store)
    assert resumed["status"] == "done"
    assert resumed["run_id"] == partial["run_id"]
    assert resumed["processed"] == resumed["total"] == OBSERVATIONS
    assert store.count_recon()["recon_entries"] == ENTRIES
    # 全部记完后再续跑报 409
    with pytest.raises(Exception) as excinfo:
        recon.resume(settings, store)
    assert getattr(excinfo.value, "code", "") == "R_NO_RUN"


def test_aborted_run_keeps_progress_and_resumes(settings, store, run, monkeypatch):
    """运行中途异常：已记账进度保留，运行留 aborted，修复后续跑记完。"""
    calls = {"n": 0}
    real = recon._book_observation

    def boom(conn, obs, run_id, now):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("模拟记账中断")
        return real(conn, obs, run_id, now)

    monkeypatch.setattr(recon, "_book_observation", boom)
    with pytest.raises(RuntimeError):
        _run_recon(settings, store)
    incomplete = store.recon_latest_incomplete_run()
    assert incomplete["status"] == "aborted"
    assert incomplete["processed"] == 2
    assert "模拟记账中断" in incomplete["note"]
    monkeypatch.undo()
    resumed = recon.resume(settings, store)
    assert resumed["status"] == "done"
    assert resumed["run_id"] == incomplete["run_id"]
    assert store.count_recon()["recon_entries"] == ENTRIES


def test_close_and_supplement_share_one_cutoff(settings, store, run):
    """关账与补数并发：同一截点核对，截点前入快照，截点后转调整。"""
    _run_recon(settings, store)
    errors = []

    def poster():
        try:
            for i in range(8):
                recon.post_events(store, {"events": [
                    {"kind": "payment", "flow_id": f"RACE{i}",
                     "order_id": "SO20261101001", "amount": "1.00",
                     "biz_day": "2026-11-01"}]})
        except Exception as exc:  # noqa: BLE001
            errors.append(("poster", exc))

    def closer():
        try:
            recon.close_day(store, "2026-11-01")
        except Exception as exc:  # noqa: BLE001
            errors.append(("closer", exc))

    threads = [threading.Thread(target=poster), threading.Thread(target=closer)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    day = _day(store, "2026-11-01")
    assert day["status"] == "closed"
    entries = store.recon_entries(biz_day="2026-11-01")
    cutoff = day["cutoff_seq"]
    frozen = sum(Decimal(e["amount"]) for e in entries
                 if e["seq"] <= cutoff and e["kind"] in journal.PAYMENT_KINDS)
    assert Decimal(day["payment_total"]) == frozen
    for entry in entries:
        if entry["seq"] > cutoff:
            assert entry["day"] != "2026-11-01"
            assert entry["link_day"] == "2026-11-01"
    day2 = _day(store, "2026-11-02")
    assert day2["diff"] == "0.00"  # 调整不进入账日自身差异


def test_order_matched_with_multiple_payments_by_amount(settings, store, run):
    """订单与多笔支付按金额核对：合计相等判平，多收少收都露头。"""
    _run_recon(settings, store)
    recon.post_events(store, {"events": [
        {"kind": "order", "order_id": "SO20261104009", "amount": "100.00",
         "biz_day": "2026-11-04", "status": "paid"},
        {"kind": "payment", "flow_id": "P1", "order_id": "SO20261104009",
         "amount": "60.00", "biz_day": "2026-11-04"},
        {"kind": "payment", "flow_id": "P2", "order_id": "SO20261104009",
         "amount": "40.00", "biz_day": "2026-11-04"}]})
    orders = {row["order_id"]: row for row in recon.orders_view(store)}
    assert orders["SO20261104009"]["paid_amount"] == "100.00"
    assert orders["SO20261104009"]["status"] == "balanced"
    assert orders["SO20261103006"]["status"] == "canceled"
    assert orders["SO20261103006"]["order_amount"] == "0.00"
    assert _day(store, "2026-11-04")["diff"] == "0.00"


def test_events_post_is_atomic(settings, store, run):
    """一次补数请求要么全记要么全滚：坏事件不让好事件先进账。"""
    _run_recon(settings, store)
    before = store.count_recon()["recon_entries"]
    with pytest.raises(Exception) as excinfo:
        recon.post_events(store, {"events": [
            {"kind": "payment", "flow_id": "OK1", "order_id": "SO20261101001",
             "amount": "1.00", "biz_day": "2026-11-01"},
            {"kind": "revoke", "flow_id": "NO-SUCH-FLOW"}]})
    assert getattr(excinfo.value, "code", "") == "R_REVOKE_UNKNOWN"
    assert store.count_recon()["recon_entries"] == before


def test_recon_api_matrix(client_with_run):
    """接口矩阵：无批次 404、重复关账 409、坏事件 400、撤销未知流水 400。"""
    client = client_with_run
    assert client.post("/api/recon/run", json={}).status_code == 200
    assert client.post("/api/recon/run", json={"limit": 0}).status_code == 400
    close = client.post("/api/recon/close", json={"day": "2026-11-01"})
    assert close.status_code == 200
    assert close.json()["code"] == "R_CLOSE_BALANCED"
    again = client.post("/api/recon/close", json={"day": "2026-11-01"})
    assert again.status_code == 409
    assert again.json()["error"] == "R_DAY_CLOSED"
    missing = client.post("/api/recon/close", json={"day": "2026-11-30"})
    assert missing.status_code == 404
    assert missing.json()["error"] == "R_DAY_MISSING"
    bad_day = client.post("/api/recon/close", json={"day": "不是日期"})
    assert bad_day.status_code == 400
    bad_event = client.post("/api/recon/events", json={"events": [{"kind": "x"}]})
    assert bad_event.status_code == 400
    assert bad_event.json()["error"] == "R_EVENT_BAD"
    revoke = client.post("/api/recon/events",
                         json={"events": [{"kind": "revoke", "flow_id": "NONE"}]})
    assert revoke.status_code == 400
    assert revoke.json()["error"] == "R_REVOKE_UNKNOWN"
    day = client.get("/api/recon/days/2026-11-01").json()
    assert day["day"]["status"] == "closed"
    assert client.get("/api/recon/days/2026-11-30").status_code == 404
    orders = client.get("/api/recon/orders").json()
    assert orders["total"] == 8


def test_recon_run_requires_batch(client):
    """没有批次时对账运行给 404；总览给空态而不是报错。"""
    assert client.post("/api/recon/run", json={}).status_code == 404
    overview = client.get("/api/recon").json()
    assert overview["has_data"] is False
    assert overview["days"] == []


def test_spec_sheet_provides_daily_recon(client_with_run):
    """规格单提供按日支付对账：接口带 recon 区块，页面有对应章节。"""
    client_with_run.post("/api/recon/run", json={})
    client_with_run.post("/api/recon/close", json={"day": "2026-11-01"})
    spec = client_with_run.get("/api/spec").json()
    recon_block = spec["recon"]
    assert recon_block["has_data"] is True
    assert len(recon_block["days"]) == 4
    first = recon_block["days"][0]
    assert first["day"] == "2026-11-01"
    assert first["status"] == "closed"
    assert first["order_total"] == "1469.50"
    assert recon_block["totals"]["closed_count"] == 1
    page = client_with_run.get("/spec")
    assert "丁 按日支付对账" in page.text
    assert "己 注" in page.text


def test_recon_page_renders_sections(client_with_run):
    """对账页渲染：操作条、按日对账、调整明细、订单核对、运行记录。"""
    client_with_run.post("/api/recon/run", json={})
    page = client_with_run.get("/recon")
    assert page.status_code == 200
    for text in ("对账运行", "按日对账", "调整明细", "订单核对", "运行记录",
                 "data-action=\"recon-run\"", "data-action=\"recon-close\""):
        assert text in page.text
