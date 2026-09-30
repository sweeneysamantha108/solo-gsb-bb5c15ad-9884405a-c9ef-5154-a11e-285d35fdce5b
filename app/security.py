import hmac
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status

from .config import settings


def require_service_key(x_service_key: str | None = Header(default=None)) -> None:
    """维护接口鉴权：缺少或服务密钥不符时整次拒绝。"""
    if x_service_key is None or not hmac.compare_digest(x_service_key, settings.service_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid service key",
        )


@dataclass(frozen=True)
class Operator:
    """通过个人凭据认证后的操作者身份。"""

    operator_id: str
    roles: frozenset[str]


def authenticate_operator(
    x_operator_id: str | None = Header(default=None),
    x_operator_key: str | None = Header(default=None),
) -> Operator:
    """操作者鉴权：用各自的 X-Operator-Id / X-Operator-Key 标识身份。

    未知操作者、缺少凭据或凭据不符一律 401；错误信息保持模糊，不回显任何身份信息。
    """
    if not x_operator_id or not x_operator_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid operator credentials",
        )
    credential = settings.operator_credentials.get(x_operator_id)
    if credential is None or not hmac.compare_digest(x_operator_key, credential.key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid operator credentials",
        )
    return Operator(operator_id=x_operator_id, roles=frozenset(credential.roles))


def require_operator_role(role: str):
    """生成依赖：认证操作者并要求其具备指定角色，否则 403。"""

    def _dependency(operator: Operator = Depends(authenticate_operator)) -> Operator:
        if role not in operator.roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="operator lacks required role",
            )
        return operator

    return _dependency
