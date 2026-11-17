"""M8 记账规则：观测流水 → 分录计划，纯函数不碰库。

核心约定：
- 原流水不覆盖：取消、改价、更正、撤销一律补记差额分录，原分录保留；
- 幂等键（fingerprint）由业务键 + 变化序号推出，同一观测重放必然得到同一键，
  落库时 INSERT OR IGNORE 兜底，重复重跑不新增调整；
- 相同流水改金额 = 更正：观测有效额与已记有效额的差额即更正分录；
- 账日（biz_day）与入账日（day）分离：原日已关账时转入下一未关账日作调整。
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from ... import config

ZERO = Decimal("0.00")

# 订单侧分录：应收；支付侧分录：实收
ORDER_KINDS = ("order", "order_delta", "cancel")
PAYMENT_KINDS = ("payment", "correction", "reversal")
ENTRY_KINDS = ORDER_KINDS + PAYMENT_KINDS

KIND_LABELS = {
    "order": "订单",
    "order_delta": "改价更正",
    "cancel": "订单取消",
    "payment": "支付",
    "correction": "支付更正",
    "reversal": "撤销冲回",
}

EVENT_KINDS = ("order", "payment", "revoke")


@dataclass
class Observation:
    """一条待记账观测：来自批次样本（订单/支付）或补数事件（含撤销）。"""

    kind: str            # order | payment | revoke
    ref: str             # 订单号或支付流水号
    order_id: str = ""
    amount: Decimal = ZERO
    biz_day: str = ""    # 业务发生日（账日）
    status: str = "paid"  # 订单状态：paid | cancel
    source: str = "api"


@dataclass
class RefState:
    """某业务键的已记账状态：有效额 = 该键全部分录之和。"""

    seen: bool = False
    effective: Decimal = ZERO
    biz_day: str = ""
    order_id: str = ""
    kind_counts: dict = field(default_factory=dict)

    def next_ordinal(self, kind: str) -> int:
        return int(self.kind_counts.get(kind, 0)) + 1


def quantize(amount: Decimal) -> Decimal:
    return amount.quantize(config.DECIMAL_QUANT)


def ref_state(entries: list, ref: str) -> RefState:
    """从分录列表累计某业务键的状态；entries 为全量或按 ref 过滤后的行。"""
    state = RefState()
    for entry in entries:
        if entry["ref"] != ref:
            continue
        state.seen = True
        state.effective += Decimal(str(entry["amount"]))
        state.biz_day = state.biz_day or entry["biz_day"]
        state.order_id = state.order_id or entry.get("order_id", "")
        state.kind_counts[entry["kind"]] = state.kind_counts.get(entry["kind"], 0) + 1
    state.effective = quantize(state.effective)
    return state


def _entry(kind: str, obs: Observation, amount: Decimal, fingerprint: str,
           code: str, biz_day: str = "") -> dict:
    return {
        "kind": kind,
        "ref": obs.ref,
        "order_id": obs.order_id,
        "amount": quantize(amount),
        "biz_day": biz_day or obs.biz_day,
        "fingerprint": fingerprint,
        "code": code,
    }


def plan(obs: Observation, state: RefState) -> list:
    """一条观测 → 0..n 条分录；空列表 = 幂等跳过，不新增调整。

    state 是该业务键当前的已记账状态（重放同一观测时差额为 0，自然跳过）。
    """
    if obs.kind == "order":
        target = ZERO if obs.status == "cancel" else quantize(obs.amount)
        if not state.seen:
            first = _entry("order", obs, obs.amount,
                           f"order|{obs.ref}|{obs.biz_day}|{quantize(obs.amount)}",
                           "")
            if obs.status != "cancel" or quantize(obs.amount) == ZERO:
                return [first]
            cancel = _entry("cancel", obs, -obs.amount,
                            f"cancel|{obs.ref}|1|{quantize(obs.amount)}->{ZERO}",
                            "R_CANCEL")
            return [first, cancel]
        delta = quantize(target - state.effective)
        if delta == ZERO:
            return []
        if obs.status == "cancel":
            ordinal = state.next_ordinal("cancel")
            return [_entry("cancel", obs, delta,
                           f"cancel|{obs.ref}|{ordinal}|{state.effective}->{target}",
                           "R_CANCEL")]
        ordinal = state.next_ordinal("order_delta")
        return [_entry("order_delta", obs, delta,
                       f"order_delta|{obs.ref}|{ordinal}|{state.effective}->{target}",
                       "R_REPRICE")]

    if obs.kind == "payment":
        if not state.seen:
            return [_entry("payment", obs, obs.amount,
                           f"payment|{obs.ref}|{obs.biz_day}|{quantize(obs.amount)}",
                           "")]
        delta = quantize(obs.amount - state.effective)
        if delta == ZERO:
            return []
        ordinal = state.next_ordinal("correction")
        return [_entry("correction", obs, delta,
                       f"correction|{obs.ref}|{ordinal}|{state.effective}"
                       f"->{quantize(obs.amount)}",
                       "R_CORRECTION")]

    if obs.kind == "revoke":
        if not state.seen:
            raise KeyError(obs.ref)
        if state.effective == ZERO:
            return []
        ordinal = state.next_ordinal("reversal")
        biz_day = obs.biz_day or state.biz_day
        return [_entry("reversal", obs, -state.effective,
                       f"reversal|{obs.ref}|{ordinal}|{state.effective}->{ZERO}",
                       "R_REVERSAL", biz_day=biz_day)]

    raise ValueError(f"未登记的观测类型：{obs.kind}")


def resolve_booking_day(biz_day: str, days: dict) -> tuple[str, str]:
    """入账日解析：原日未关账记原日；已关账转入下一未关账日，link_day 记原日。

    days: {day: status}。没有更晚的未关账日时，在最晚账日后顺推一日开新账日，
    保证调整永远有处可去，且不回头改已关账日。
    """
    if days.get(biz_day, "open") == "open":
        return biz_day, ""
    later_open = sorted(day for day, status in days.items()
                        if day > biz_day and status == "open")
    if later_open:
        return later_open[0], biz_day
    base = max(list(days) + [biz_day])
    next_day = (dt.date.fromisoformat(base) + dt.timedelta(days=1)).isoformat()
    return next_day, biz_day


def day_totals(entries: list, day: str) -> dict:
    """某账日的账面合计：自身业务日的订单/支付有效额 + 落在本日的他日调整。

    差额 = 实收 - 应收，只算本日业务（biz_day=day）；调整（day=day 但 biz_day≠day）
    单列不进差额，差额不跨错账日。
    """
    order_total = ZERO
    payment_total = ZERO
    adjust_total = ZERO
    order_count = 0
    payment_count = 0
    for entry in entries:
        amount = Decimal(str(entry["amount"]))
        if entry["biz_day"] == day:
            if entry["kind"] in ORDER_KINDS:
                order_total += amount
                if entry["kind"] == "order":
                    order_count += 1
            elif entry["kind"] in PAYMENT_KINDS:
                payment_total += amount
                if entry["kind"] == "payment":
                    payment_count += 1
        if entry["day"] == day and entry["biz_day"] != day:
            adjust_total += amount
    order_total = quantize(order_total)
    payment_total = quantize(payment_total)
    return {
        "order_total": order_total,
        "payment_total": payment_total,
        "diff": quantize(payment_total - order_total),
        "adjust_total": quantize(adjust_total),
        "order_count": order_count,
        "payment_count": payment_count,
    }


def order_matches(entries: list) -> list:
    """订单与多笔支付按金额核对：订单有效额 vs 该订单支付有效额合计。"""
    orders: dict = {}
    paid: dict = {}
    canceled: set = set()
    for entry in entries:
        amount = Decimal(str(entry["amount"]))
        if entry["kind"] in ORDER_KINDS:
            slot = orders.setdefault(entry["ref"], {"biz_day": entry["biz_day"],
                                                    "amount": ZERO})
            slot["amount"] = quantize(slot["amount"] + amount)
            if entry["kind"] == "cancel":
                canceled.add(entry["ref"])
        elif entry["kind"] in PAYMENT_KINDS and entry.get("order_id"):
            paid[entry["order_id"]] = quantize(
                paid.get(entry["order_id"], ZERO) + amount)
    rows = []
    for order_id in sorted(orders, key=lambda k: (orders[k]["biz_day"], k)):
        amount = orders[order_id]["amount"]
        got = paid.get(order_id, ZERO)
        diff = quantize(got - amount)
        is_canceled = order_id in canceled and amount == ZERO
        if is_canceled:
            status = "canceled"
        elif diff == ZERO:
            status = "balanced"
        elif diff > ZERO:
            status = "over"
        else:
            status = "under"
        rows.append({
            "order_id": order_id,
            "biz_day": orders[order_id]["biz_day"],
            "order_amount": amount,
            "paid_amount": got,
            "diff": diff,
            "status": status,
        })
    return rows
