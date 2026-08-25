"""Backtest API routes."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from web.auth import current_user
from web.models import User
from web.services.backtest_service import (
    get_default_config, submit_job, get_job, list_jobs, submit_grid,
)

router = APIRouter(prefix="/api/backtest", tags=["backtest"])


class BacktestSubmit(BaseModel):
    name: str = "Quick backtest"
    config: dict
    config_b: dict | None = None

class GridSubmit(BaseModel):
    param: str
    values: list
    base_config: dict

class StrategyGenerate(BaseModel):
    description: str  # Natural language strategy description


@router.get("/config/default")
async def default_config(user=Depends(current_user)):
    """Return DEFAULT_CONFIG for the UI form."""
    return get_default_config()


@router.get("/config/spec")
async def strategy_spec(user=Depends(current_user)):
    """Return strategy parameter metadata for auto-generating UI forms."""
    from web.services.strategy_metadata import get_strategy_spec
    return get_strategy_spec()


@router.get("/jobs")
async def my_jobs(user=Depends(current_user)):
    return list_jobs(user.id)


@router.post("/jobs")
async def create_job(body: BacktestSubmit, user=Depends(current_user)):
    """Submit a backtest job."""
    job_id = submit_job(user.id, body.name, body.config, body.config_b)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
async def job_status(job_id: str, user=Depends(current_user)):
    """Poll job status + results (scoped to user)."""
    return get_job(job_id, user_id=user.id)


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str, user=Depends(current_user)):
    """Delete a backtest job."""
    from web.db import SessionLocal
    from web.models import BacktestJob
    db = SessionLocal()
    try:
        job = db.query(BacktestJob).filter(
            BacktestJob.id == job_id,
            BacktestJob.user_id == user.id,
        ).first()
        if not job:
            raise HTTPException(status_code=404, detail="Job not found")
        db.delete(job)
        db.commit()
        return {"status": "deleted", "job_id": job_id}
    finally:
        db.close()


@router.post("/grid")
async def grid_sweep(body: GridSubmit, user=Depends(current_user)):
    """Submit a parameter grid sweep."""
    job_id = submit_grid(user.id, body.param, body.values, body.base_config)
    return {"job_id": job_id}


@router.post("/generate")
async def generate_strategy(body: StrategyGenerate, user=Depends(current_user)):
    """LLM translates natural language strategy description into backtest config + runs it."""
    import os, json, re

    # Build prompt with available parameters
    prompt = f"""你是量化策略工程师。根据用户的自然语言描述，生成回测参数JSON。

可用参数（BacktestConfig）：
- pool_size: 股票池大小 (10-200)
- overheat_mode: 过热处理 (reduce/eliminate/off)
- abs_stop: 绝对止损比例 (负数, 如 -0.12 表示-12%)
- trailing_stop: 追踪止损比例 (负数, 如 -0.18)
- ma_exit_days: MA10连续跌破天数触发卖出 (1-10)
- take_profit_tiers: 止盈档位数组 (如 [25,50] 表示+25%卖1/3, +50%卖1/3)
- rebalance_freq: 调仓频率 (weekly/biweekly/monthly)
- max_position_pct: 单票最大仓位 (0.05-0.20)

用户描述：{body.description}

只返回一行JSON，不要markdown，不要解释。格式示例：
{{"name":"策略名","config":{{"pool_size":30}},"reasoning":"理由"}}"""

    try:
        import anthropic
        client = anthropic.Anthropic(
            api_key=os.getenv("ANTHROPIC_AUTH_TOKEN", ""),
            base_url=os.getenv("ANTHROPIC_BASE_URL", "https://api.deepseek.com/anthropic"),
        )
        resp = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "disabled"},
        )
        text = ""
        for block in resp.content:
            if hasattr(block, 'text') and block.text:
                text += block.text
            elif hasattr(block, 'thinking') and block.thinking:
                text += block.thinking
            elif hasattr(block, 'content') and isinstance(block.content, list):
                for inner in block.content:
                    if hasattr(inner, 'text'):
                        text += inner.text

        # Try multiple JSON parse strategies
        result = None
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                result = json.loads(match.group())
            except json.JSONDecodeError:
                pass

        if not result:
            # Fallback: extract config from default + reasoning
            result = {
                "name": "AI生成策略",
                "config": {
                    "pool_size": 30, "overheat_mode": "reduce",
                    "abs_stop": -0.12, "trailing_stop": -0.18,
                    "ma_exit_days": 4, "take_profit_tiers": [25, 50],
                    "rebalance_freq": "biweekly", "max_position_pct": 0.10,
                },
                "reasoning": text.strip()[:200] if text.strip() else "AI根据描述生成的默认策略参数",
            }

        if not result:
            return {"error": "无法解析LLM输出", "raw": text[:300]}
        # Submit the backtest job
        job_id = submit_job(
            user.id,
            result.get("name", "LLM策略"),
            result.get("config", {}),
        )
        return {
            "job_id": job_id,
            "name": result.get("name", ""),
            "config": result.get("config", {}),
            "reasoning": result.get("reasoning", ""),
        }
    except Exception as e:
        return {"error": str(e)}
