"""Portfolio API routes."""
import json
from fastapi import APIRouter, Depends, HTTPException, Form
from web.auth import current_user
from web.models import User
from web.services.portfolio_service import get_ma10_triggers, get_exit_status, save_user_positions
from web.services.portfolio_advice import get_portfolio_advice

router = APIRouter(prefix="/api/portfolio", tags=["portfolio"])


@router.get("/ma10-triggers")
async def ma10_triggers(user=Depends(current_user)):
    """MA10-4d trigger log and stats."""
    return get_ma10_triggers()


@router.get("/exits")
async def exit_status(user=Depends(current_user)):
    """Per-user exit status."""
    return get_exit_status(user_id=user.id, is_admin=(user.role == "admin"))


@router.post("/holdings")
async def save_holdings(holdings_json: str = Form(...), user=Depends(current_user)):
    """Save user's holdings (JSON array of {code, name, cost, shares}). Admin cannot use this."""
    if user.role == "admin":
        raise HTTPException(status_code=400, detail="Admin holdings managed via CSV file")
    try:
        positions = json.loads(holdings_json)
        if not isinstance(positions, list):
            raise ValueError("Must be an array")
        for p in positions:
            if "code" not in p or "cost" not in p or "shares" not in p:
                raise ValueError(f"Missing required fields in: {p}")
            p["code"] = str(p["code"]).zfill(6)
            p["shares"] = int(p["shares"])
            p["cost"] = float(p["cost"])
        save_user_positions(user.id, positions)
        return {"status": "saved", "count": len(positions)}
    except (json.JSONDecodeError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/signal-alerts")
async def signal_alerts(user=Depends(current_user)):
    """外盘领先信号提醒（伦铜/金/NBI/SOX/恒生 → 持仓）。"""
    from web.services.signal_alerts import get_holding_signal_alerts
    return get_holding_signal_alerts()


@router.get("/advice")
async def portfolio_advice(user=Depends(current_user)):
    """Today's portfolio operation suggestions."""
    return get_portfolio_advice(user_id=user.id, is_admin=(user.role == "admin"))


@router.get("/t-signals")
async def t_signals(user=Depends(current_user)):
    """做T触发信号 (持仓票涨跌≥2%)."""
    from web.services.tservice import get_t_signals
    return get_t_signals(is_admin=(user.role == "admin"), user_id=user.id)


@router.get("/t-records")
async def t_records(user=Depends(current_user)):
    """做T记录 + 统计."""
    from web.services.tservice import load_t_records, get_t_stats, load_t_settlements
    # 结算明细排在手工记录前面(自动结算为主口径)
    return {"records": load_t_settlements() + load_t_records(), "stats": get_t_stats()}


@router.get("/t-signal-history")
async def t_signal_history(user=Depends(current_user)):
    """做T信号历史 (漏看回查)."""
    from web.services.tservice import get_signal_history
    return {"history": get_signal_history()}


@router.post("/t-records")
async def save_t_record_endpoint(
    code: str = Form(...), name: str = Form(""),
    direction: str = Form(...), sell_price: float = Form(...),
    buy_price: float = Form(...), shares: int = Form(...),
    settle_date: str = Form(""),
    user=Depends(current_user),
):
    """保存做T记录."""
    from web.services.tservice import save_t_record
    return save_t_record(code, name, direction, sell_price, buy_price, shares, settle_date)


@router.post("/ocr-holdings")
async def ocr_holdings(image_data: str = Form(...), user=Depends(current_user)):
    """OCR holdings from a screenshot image (base64). Returns parsed positions."""
    import base64, os
    try:
        # Decode base64 image
        img_bytes = base64.b64decode(image_data.split(",")[-1] if "," in image_data else image_data)

        # Use LLM to extract holdings
        import anthropic
        client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_AUTH_TOKEN", ""),
            base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"),
        )
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": base64.b64encode(img_bytes).decode(),
                }},
                {"type": "text", "text": "提取图中所有持仓，返回JSON数组：[{\"code\":\"股票代码\",\"name\":\"名称\",\"cost\":成本价,\"shares\":股数}]。只要JSON，不要其他文字。"},
            ]}],
        )
        text = ""
        for block in resp.content:
            if hasattr(block, 'text') and block.text:
                text += block.text

        # Parse JSON from response
        import re, json
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            positions = json.loads(match.group())
            return {"status": "ok", "positions": positions, "count": len(positions)}
        return {"status": "error", "detail": "无法解析持仓数据，请重试"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}
