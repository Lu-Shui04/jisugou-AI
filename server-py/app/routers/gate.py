"""开屏人机验证路由：下发 challenge + 校验滑块结果并签发 Token

实现（一次性 challenge、签名 Token、防脚本的时间/轨迹下限）在 app/security/gate.py。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.security import gate

router = APIRouter()


class GateVerifyRequest(BaseModel):
    challenge_id: str
    duration_ms: int = Field(default=0, description="拖动耗时（毫秒）")
    points: int = Field(default=0, description="拖动轨迹点数量")


@router.get("/challenge")
async def gate_challenge():
    """下发一次性 challenge（5 分钟有效、只能用一次）"""
    return await gate.new_challenge()


@router.post("/verify")
async def gate_verify(req: GateVerifyRequest):
    """校验滑块结果，通过则签发 Token"""
    ok, reason = await gate.verify_challenge(req.challenge_id, req.duration_ms, req.points)
    if not ok:
        print(f"[gate] 滑块验证未通过: {reason} (duration={req.duration_ms}ms points={req.points})")
        raise HTTPException(status_code=400, detail=reason)
    result = gate.issue_token()
    print(f"[gate] 滑块验证通过，签发 Token（{result['expires_in']} 秒有效）")
    return result
