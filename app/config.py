"""服务配置：全部通过环境变量注入（可搭配 .env 文件）。"""

import json

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# 演示用操作者目录：operator 可提交受控发布申请，reviewer 可复核（批准/驳回）。
# 生产环境必须通过 OPERATOR_CREDENTIALS 环境变量覆盖。
# 结构为 {操作者ID: {"key": 个人凭据, "roles": ["operator"/"reviewer", ...]}}。
DEFAULT_OPERATOR_CREDENTIALS = {
    "alice": {"key": "alice-key", "roles": ["operator", "reviewer"]},
    "bob": {"key": "bob-key", "roles": ["operator"]},
    "carol": {"key": "carol-key", "roles": ["reviewer"]},
}


class OperatorCredential(BaseModel):
    key: str
    roles: list[str]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # PostgreSQL 连接串，Compose 中指向 db 服务
    database_url: str = "postgresql+psycopg2://redact:redact@localhost:5432/redact"
    # 维护人员服务密钥，必填，未配置时服务拒绝启动
    service_key: str
    # 启动时等待数据库就绪的重试参数
    db_connect_retries: int = 30
    db_connect_retry_delay_seconds: float = 1.0
    # 受控发布工作流的操作者凭据目录（JSON 字符串，见 DEFAULT_OPERATOR_CREDENTIALS）
    operator_credentials: dict[str, OperatorCredential] = DEFAULT_OPERATOR_CREDENTIALS

    @property
    def reviewer_ids(self) -> frozenset[str]:
        return frozenset(
            operator_id
            for operator_id, cred in self.operator_credentials.items()
            if "reviewer" in cred.roles
        )


def load_operator_credentials(raw: str | dict | None) -> dict[str, OperatorCredential]:
    """解析 OPERATOR_CREDENTIALS（JSON 字符串或等价 dict），供测试与启动校验使用。"""
    if raw is None:
        return {k: OperatorCredential(**v) for k, v in DEFAULT_OPERATOR_CREDENTIALS.items()}
    if isinstance(raw, str):
        raw = json.loads(raw)
    return {operator_id: OperatorCredential(**cred) for operator_id, cred in raw.items()}


settings = Settings()
