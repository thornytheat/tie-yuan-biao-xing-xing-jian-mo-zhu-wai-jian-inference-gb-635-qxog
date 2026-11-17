"""M8 入口：按日支付对账、补数记账、关账、续跑、订单核对、对账页。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, Request

from ... import errors
from ...web import Ctx, get_ctx, render
from . import service

router = APIRouter(tags=["M8 支付对账"])


@router.get("/api/recon")
def api_recon(ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.overview(ctx.store)


@router.get("/api/recon/days/{day}")
def api_recon_day(day: str, ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.day_detail(ctx.store, day)


@router.get("/api/recon/orders")
def api_recon_orders(ctx: Ctx = Depends(get_ctx)) -> dict:
    rows = service.orders_view(ctx.store)
    return {"rows": rows, "total": len(rows)}


@router.post("/api/recon/run")
def api_recon_run(payload: dict = Body(default_factory=dict),
                  ctx: Ctx = Depends(get_ctx)) -> dict:
    if not isinstance(payload, dict):
        raise errors.BadRequest("请求体必须是对象", code="E_BAD_REQUEST")
    batch_id = payload.get("batch_id") or None
    limit = payload.get("limit")
    if limit is not None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise errors.BadRequest("limit 必须是正整数", code="E_BAD_REQUEST")
    return service.run_sync(ctx.settings, ctx.store, batch_id=batch_id, limit=limit)


@router.post("/api/recon/resume")
def api_recon_resume(ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.resume(ctx.settings, ctx.store)


@router.post("/api/recon/events")
def api_recon_events(payload: dict = Body(default_factory=dict),
                     ctx: Ctx = Depends(get_ctx)) -> dict:
    return service.post_events(ctx.store, payload)


@router.post("/api/recon/close")
def api_recon_close(payload: dict = Body(default_factory=dict),
                    ctx: Ctx = Depends(get_ctx)) -> dict:
    day = str(payload.get("day") or "").strip() if isinstance(payload, dict) else ""
    if not day:
        raise errors.BadRequest("缺少账日 day", code="E_BAD_REQUEST")
    return service.close_day(ctx.store, day)


@router.get("/recon")
def page_recon(request: Request, ctx: Ctx = Depends(get_ctx)):
    batch = ctx.current_batch()
    data = service.overview(ctx.store)
    orders = service.orders_view(ctx.store)
    adjustments = [service.entry_payload(row)
                   for row in ctx.store.recon_adjustments()]
    return render(request, "recon.html", page="recon", page_title="支付对账",
                  payload=data, orders=orders, adjustments=adjustments, batch=batch)
