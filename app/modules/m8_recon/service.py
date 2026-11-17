"""M8 编排：对账运行、补数记账、关账、续跑、查询视图。

事务边界：
- 每条观测一个写事务（BEGIN IMMEDIATE），记完即提交 = 记账进度落库，
  中断后续跑从已记进度接回，幂等键兜底不重复记账；
- 关账一个写事务：截点 = 当前最大记账序号，截点内的流水进关账快照，
  截点后的流水转入下一未关账日，关账与补数并发时按同一截点核对；
- 账日合计只重算未关账日，已关账日的订单、支付与差异金额按关账时保留。
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from ... import config, errors, manifest, reasons
from ..m1_ingest import service as m1_service
from ..m2_contract import service as m2_service
from . import journal

ORDER_TABLE = "odl_sale_order"
PAYMENT_TABLE = "odl_payment"

DAY_STATUS_LABELS = {"open": "未关账", "closed": "已关账"}
RUN_STATUS_LABELS = {"running": "运行中", "partial": "部分记账", "done": "完成",
                     "aborted": "中断"}
SOURCE_LABELS = {"batch": "批次同步", "api": "补数", "close": "关账", "resume": "续跑"}
MATCH_LABELS = {"balanced": "平", "over": "多收", "under": "欠收", "canceled": "已取消"}


def _now() -> str:
    return dt.datetime.now(config.UTC).isoformat(timespec="seconds")


def _new_run(source: str, total: int = 0, note: str = "") -> dict:
    return {
        "run_id": manifest.new_run_id("RC"),
        "source": source,
        "status": "running",
        "total": total,
        "processed": 0,
        "booked": 0,
        "skipped": 0,
        "note": note,
        "created_at": _now(),
        "finished_at": None,
    }


# ---------- 观测来源 ----------

def observations_from_batch(settings, store, batch_id: str | None = None) -> list:
    """从已完成批次的样本里取订单与支付观测（只读重算，与 M7 同一套归一）。"""
    if batch_id:
        batch = store.get_batch(batch_id)
        if batch is None or batch["status"] != "done":
            raise errors.NotFound(f"批次不存在或未完成：{batch_id}", code="B_EMPTY")
    else:
        batch = store.latest_batch()
    if batch is None:
        raise errors.NotFound("还没有任何批次，请先跑一次接入", code="B_EMPTY")
    intake = m1_service.intake(settings)
    coerced = m2_service.build(intake, settings)
    observations = []
    order_table = coerced.table(ORDER_TABLE)
    if order_table is not None:
        for row in order_table.rows_norm:
            if not row.get("order_id") or row.get("order_amount") is None \
                    or not row.get("order_time"):
                continue
            observations.append(journal.Observation(
                kind="order", ref=row["order_id"], order_id=row["order_id"],
                amount=Decimal(row["order_amount"]), biz_day=row["order_time"][:10],
                status=(row.get("status") or "paid").strip().lower() or "paid",
                source=f"batch:{batch['batch_id']}"))
    payment_table = coerced.table(PAYMENT_TABLE)
    if payment_table is not None:
        for row in payment_table.rows_norm:
            if not row.get("id") or row.get("pay_amount") is None \
                    or not row.get("pay_time"):
                continue
            observations.append(journal.Observation(
                kind="payment", ref=row["id"], order_id=row.get("order_id") or "",
                amount=Decimal(row["pay_amount"]), biz_day=row["pay_time"][:10],
                source=f"batch:{batch['batch_id']}"))
    rank = {"order": 0, "payment": 1}
    observations.sort(key=lambda obs: (obs.biz_day, rank.get(obs.kind, 9), obs.ref))
    return observations


def parse_events(payload) -> list:
    """补数事件 → 观测；格式不合法直接 400，不进账。"""
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list) \
            or not payload["events"]:
        raise errors.BadRequest("events 必须是非空数组", code="R_EVENT_BAD")
    observations = []
    for index, item in enumerate(payload["events"], 1):
        if not isinstance(item, dict):
            raise errors.BadRequest(f"第 {index} 条事件不是对象", code="R_EVENT_BAD")
        kind = str(item.get("kind") or "").strip()
        if kind not in journal.EVENT_KINDS:
            raise errors.BadRequest(f"第 {index} 条事件类型 {kind or '空'} 未登记",
                                    code="R_EVENT_BAD")
        if kind == "revoke":
            flow_id = str(item.get("flow_id") or "").strip()
            if not flow_id:
                raise errors.BadRequest(f"第 {index} 条撤销缺少 flow_id",
                                        code="R_EVENT_BAD")
            observations.append(journal.Observation(kind="revoke", ref=flow_id,
                                                    source="api"))
            continue
        amount_raw = item.get("amount")
        biz_day = str(item.get("biz_day") or "").strip()
        try:
            amount = journal.quantize(Decimal(str(amount_raw)))
        except Exception:
            raise errors.BadRequest(f"第 {index} 条金额无法解析：{amount_raw}",
                                    code="R_EVENT_BAD") from None
        try:
            dt.date.fromisoformat(biz_day)
        except ValueError:
            raise errors.BadRequest(f"第 {index} 条账日不是日期：{biz_day or '空'}",
                                    code="R_EVENT_BAD") from None
        if kind == "order":
            order_id = str(item.get("order_id") or "").strip()
            if not order_id:
                raise errors.BadRequest(f"第 {index} 条订单缺少 order_id",
                                        code="R_EVENT_BAD")
            status = str(item.get("status") or "paid").strip().lower()
            if status not in ("paid", "cancel"):
                raise errors.BadRequest(f"第 {index} 条订单状态 {status} 未登记",
                                        code="R_EVENT_BAD")
            observations.append(journal.Observation(
                kind="order", ref=order_id, order_id=order_id, amount=amount,
                biz_day=biz_day, status=status, source="api"))
        else:
            flow_id = str(item.get("flow_id") or "").strip()
            if not flow_id:
                raise errors.BadRequest(f"第 {index} 条支付缺少 flow_id",
                                        code="R_EVENT_BAD")
            observations.append(journal.Observation(
                kind="payment", ref=flow_id,
                order_id=str(item.get("order_id") or "").strip(), amount=amount,
                biz_day=biz_day, source="api"))
    return observations


# ---------- 记账（写事务内） ----------

def _day_map(conn) -> dict:
    rows = conn.execute("SELECT day, status FROM recon_days").fetchall()
    return {row["day"]: row["status"] for row in rows}


def _ensure_day(conn, day: str, now: str) -> None:
    conn.execute(
        "INSERT INTO recon_days (day, status, updated_at) VALUES (?, 'open', ?) "
        "ON CONFLICT(day) DO NOTHING", (day, now))


def _next_seq(conn) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM recon_entries"
                       ).fetchone()
    return int(row["n"])


def _entries_for_refs(conn, refs: list) -> list:
    if not refs:
        return []
    marks = ",".join("?" for _ in refs)
    rows = conn.execute(
        f"SELECT * FROM recon_entries WHERE ref IN ({marks}) ORDER BY seq",
        tuple(refs)).fetchall()
    return [dict(row) for row in rows]


def _entries_for_day(conn, day: str) -> list:
    rows = conn.execute(
        "SELECT * FROM recon_entries WHERE day=? OR biz_day=? ORDER BY seq",
        (day, day)).fetchall()
    return [dict(row) for row in rows]


def _recompute_open_day(conn, day: str, now: str) -> None:
    """只重算未关账日；已关账日的合计按关账时保留，绝不动。"""
    row = conn.execute("SELECT status FROM recon_days WHERE day=?", (day,)).fetchone()
    if row is None or row["status"] != "open":
        return
    totals = journal.day_totals(_entries_for_day(conn, day), day)
    conn.execute(
        """UPDATE recon_days SET order_total=?, payment_total=?, diff=?,
               adjust_total=?, order_count=?, payment_count=?, updated_at=?
           WHERE day=? AND status='open'""",
        (str(totals["order_total"]), str(totals["payment_total"]), str(totals["diff"]),
         str(totals["adjust_total"]), totals["order_count"], totals["payment_count"],
         now, day))


def _book_observation(conn, obs: journal.Observation, run_id: str, now: str) -> list:
    """一条观测记账：计划分录 → 定入账日 → 插分录（幂等键兜底）→ 重算账日。"""
    existing = _entries_for_refs(conn, [obs.ref])
    state = journal.ref_state(existing, obs.ref)
    try:
        planned = journal.plan(obs, state)
    except KeyError:
        raise errors.BadRequest(f"撤销的支付流水不存在：{obs.ref}",
                                code="R_REVOKE_UNKNOWN") from None
    if not planned:
        return []
    days = _day_map(conn)
    seq = _next_seq(conn)
    booked = []
    touched_days = set()
    for entry in planned:
        day, link_day = journal.resolve_booking_day(entry["biz_day"], days)
        _ensure_day(conn, day, now)
        days.setdefault(day, "open")
        cursor = conn.execute(
            """INSERT OR IGNORE INTO recon_entries
                   (seq, day, biz_day, kind, ref, order_id, amount, fingerprint,
                    link_day, source, run_id, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (seq, day, entry["biz_day"], entry["kind"], entry["ref"],
             entry["order_id"], str(entry["amount"]), entry["fingerprint"],
             link_day, obs.source, run_id, now))
        if cursor.rowcount == 0:
            continue  # 幂等键撞上了：同一观测重放，不新增分录
        booked.append({**entry, "seq": seq, "day": day, "link_day": link_day,
                       "source": obs.source, "run_id": run_id})
        seq += 1
        touched_days.add(day)
    for day in sorted(touched_days):
        _recompute_open_day(conn, day, now)
    return booked


def _save_run(conn, run: dict) -> None:
    conn.execute(
        """INSERT INTO recon_runs (run_id, source, status, total, processed,
                booked, skipped, note, created_at, finished_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,
             total=excluded.total, processed=excluded.processed,
             booked=excluded.booked, skipped=excluded.skipped,
             note=excluded.note, finished_at=excluded.finished_at""",
        (run["run_id"], run["source"], run["status"], run["total"],
         run["processed"], run["booked"], run["skipped"], run.get("note", ""),
         run["created_at"], run.get("finished_at")))


def _process_observations(store, run: dict, observations: list,
                          limit: int | None) -> dict:
    """逐条观测记账，每条一个写事务 = 记账进度随记随落，中断可续。"""
    start = int(run["processed"])
    for obs in observations[start:]:
        if limit is not None and run["processed"] - start >= limit:
            break
        now = _now()
        with store.write_txn() as conn:
            booked = _book_observation(conn, obs, run["run_id"], now)
            run["processed"] += 1
            if booked:
                run["booked"] += len(booked)
            else:
                run["skipped"] += 1
            _save_run(conn, run)
    return run


def _finish_run(store, run: dict, observations_total: int) -> dict:
    run["total"] = observations_total
    done = run["processed"] >= observations_total
    run["status"] = "done" if done else "partial"
    if done:
        run["finished_at"] = _now()
    with store.write_txn() as conn:
        _save_run(conn, run)
    return run


# ---------- 对账运行与续跑 ----------

def run_sync(settings, store, *, batch_id: str | None = None,
             limit: int | None = None, resume_run: dict | None = None) -> dict:
    """批次同步对账：订单与支付流水入账，重跑幂等不新增分录。"""
    observations = observations_from_batch(settings, store, batch_id)
    if not observations:
        raise errors.NotFound("批次里没有可记账的订单或支付流水", code="R_RECON_EMPTY")
    if resume_run is not None:
        run = dict(resume_run)
        run["status"] = "running"
        run["total"] = len(observations)
        run["note"] = ""
    else:
        run = _new_run("batch", total=len(observations))
        store.recon_create_run(run)
    try:
        _process_observations(store, run, observations, limit)
    except Exception as exc:  # noqa: BLE001 - 中断要留痕，已记进度不回滚
        run["status"] = "aborted"
        run["note"] = f"{type(exc).__name__}: {exc}"
        with store.write_txn() as conn:
            _save_run(conn, run)
        raise
    _finish_run(store, run, len(observations))
    return run_payload(store, run["run_id"])


def resume(settings, store) -> dict:
    """续跑：接回最近一次未完成的对账运行，从已记账进度继续。"""
    run = store.recon_latest_incomplete_run()
    if run is None:
        raise errors.Conflict("没有可续跑的对账运行", code="R_NO_RUN")
    if run["source"] != "batch":
        raise errors.Conflict(f"运行 {run['run_id']} 不是批次同步，不能续跑",
                              code="R_NO_RUN")
    return run_sync(settings, store, resume_run=run)


# ---------- 补数 ----------

def post_events(store, payload) -> dict:
    """补数事件：迟到支付、改价、取消、更正、撤销，一次请求一个写事务。"""
    observations = parse_events(payload)
    run = _new_run("api", total=len(observations))
    store.recon_create_run(run)
    booked_entries = []
    notes = []
    with store.write_txn() as conn:
        for obs in observations:
            booked = _book_observation(conn, obs, run["run_id"], _now())
            run["processed"] += 1
            if booked:
                run["booked"] += len(booked)
                booked_entries.extend(booked)
                for entry in booked:
                    if entry["link_day"]:
                        notes.append(reasons.label("R_ADJUST_NEXT_OPEN"))
                    if entry["code"]:
                        notes.append(reasons.label(entry["code"]))
            else:
                run["skipped"] += 1
                notes.append(reasons.label("R_IDEMPOTENT_SKIP"))
        run["status"] = "done"
        run["finished_at"] = _now()
        _save_run(conn, run)
    return {
        "run_id": run["run_id"],
        "status": "done",
        "total": run["total"],
        "booked": run["booked"],
        "skipped": run["skipped"],
        "entries": [entry_payload(entry) for entry in booked_entries],
        "notes": sorted(set(notes)),
    }


# ---------- 关账 ----------

def close_day(store, day: str) -> dict:
    """关账：同一截点核对，未平账可带差异关账；关账后账面冻结。"""
    try:
        dt.date.fromisoformat(day)
    except ValueError:
        raise errors.BadRequest(f"账日不是日期：{day}", code="R_EVENT_BAD") from None
    now = _now()
    run = _new_run("close", note=f"关账 {day}")
    with store.write_txn() as conn:
        row = conn.execute("SELECT * FROM recon_days WHERE day=?", (day,)).fetchone()
        if row is None:
            raise errors.NotFound(f"账日不存在：{day}", code="R_DAY_MISSING")
        if row["status"] == "closed":
            raise errors.Conflict(f"账日已关账：{day}", code="R_DAY_CLOSED")
        cutoff = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS n FROM recon_entries").fetchone()["n"]
        totals = journal.day_totals(_entries_for_day(conn, day), day)
        conn.execute(
            """UPDATE recon_days SET status='closed', order_total=?, payment_total=?,
                   diff=?, adjust_total=?, order_count=?, payment_count=?,
                   cutoff_seq=?, closed_at=?, close_run_id=?, updated_at=?
               WHERE day=?""",
            (str(totals["order_total"]), str(totals["payment_total"]),
             str(totals["diff"]), str(totals["adjust_total"]),
             totals["order_count"], totals["payment_count"], int(cutoff), now,
             run["run_id"], now, day))
        run["status"] = "done"
        run["finished_at"] = now
        run["note"] = f"关账 {day}，截点 {int(cutoff)}"
        _save_run(conn, run)
    code = "R_CLOSE_WITH_DIFF" if totals["diff"] != journal.ZERO else "R_CLOSE_BALANCED"
    payload = day_payload(store.recon_day(day))
    payload["code"] = code
    payload["code_label"] = reasons.label(code)
    payload["close_run_id"] = run["run_id"]
    return payload


# ---------- 查询视图 ----------

def day_payload(row: dict) -> dict:
    return {
        "day": row["day"],
        "status": row["status"],
        "status_label": DAY_STATUS_LABELS.get(row["status"], row["status"]),
        "order_total": row["order_total"],
        "payment_total": row["payment_total"],
        "diff": row["diff"],
        "adjust_total": row["adjust_total"],
        "balanced": Decimal(row["diff"]) == journal.ZERO,
        "order_count": row["order_count"],
        "payment_count": row["payment_count"],
        "cutoff_seq": row["cutoff_seq"],
        "closed_at": row.get("closed_at") or "",
        "close_run_id": row.get("close_run_id") or "",
    }


def entry_payload(row: dict) -> dict:
    return {
        "seq": row["seq"],
        "day": row["day"],
        "biz_day": row["biz_day"],
        "kind": row["kind"],
        "kind_label": journal.KIND_LABELS.get(row["kind"], row["kind"]),
        "ref": row["ref"],
        "order_id": row.get("order_id", ""),
        "amount": str(journal.quantize(Decimal(str(row["amount"])))),
        "link_day": row.get("link_day", ""),
        "adjustment": bool(row.get("link_day")),
        "source": row.get("source", ""),
        "run_id": row.get("run_id", ""),
        "created_at": row.get("created_at", ""),
    }


def run_payload(store, run_id: str) -> dict:
    row = store.recon_run(run_id)
    return _run_row(row)


def _run_row(row: dict) -> dict:
    return {
        "run_id": row["run_id"],
        "source": row["source"],
        "source_label": SOURCE_LABELS.get(row["source"], row["source"]),
        "status": row["status"],
        "status_label": RUN_STATUS_LABELS.get(row["status"], row["status"]),
        "total": row["total"],
        "processed": row["processed"],
        "booked": row["booked"],
        "skipped": row["skipped"],
        "note": row.get("note", ""),
        "created_at": row["created_at"],
        "finished_at": row.get("finished_at") or "",
    }


def overview(store) -> dict:
    days = [day_payload(row) for row in store.recon_days()]

    def _sum(key: str) -> str:
        return str(journal.quantize(
            sum((Decimal(row[key]) for row in days), journal.ZERO)))

    totals = {
        "order_total": _sum("order_total"),
        "payment_total": _sum("payment_total"),
        "diff": _sum("diff"),
        "adjust_total": _sum("adjust_total"),
        "day_count": len(days),
        "open_count": len([row for row in days if row["status"] == "open"]),
        "closed_count": len([row for row in days if row["status"] == "closed"]),
        "unbalanced_open": len([row for row in days
                                if row["status"] == "open" and not row["balanced"]]),
    }
    runs = [_run_row(row) for row in store.recon_runs(20)]
    incomplete = store.recon_latest_incomplete_run()
    return {
        "has_data": bool(days),
        "days": days,
        "totals": totals,
        "runs": runs,
        "resumable_run_id": incomplete["run_id"] if incomplete else "",
    }


def day_detail(store, day: str) -> dict:
    row = store.recon_day(day)
    if row is None:
        raise errors.NotFound(f"账日不存在：{day}", code="R_DAY_MISSING")
    entries = [entry_payload(item) for item in store.recon_entries(day=day)]
    adjustments = [item for item in entries if item["adjustment"] and item["day"] == day]
    orders = [order for order in orders_view(store) if order["biz_day"] == day]
    return {
        "day": day_payload(row),
        "entries": entries,
        "adjustments": adjustments,
        "orders": orders,
    }


def orders_view(store) -> list:
    rows = journal.order_matches(store.recon_entries())
    for row in rows:
        row["order_amount"] = str(row["order_amount"])
        row["paid_amount"] = str(row["paid_amount"])
        row["diff"] = str(row["diff"])
        row["status_label"] = MATCH_LABELS.get(row["status"], row["status"])
    return rows


def spec_section(store) -> dict:
    """规格单「按日支付对账」区块：当前账面，关账日按关账时保留。"""
    data = overview(store)
    data["note"] = ("应收=订单有效额，实收=支付有效额，差异=实收-应收；"
                    "调整列是原日已关账后转入本日的补记，不计入本日差异。")
    return data
