from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator


class Interval(BaseModel):
    """左闭右开 [start, end)，按 Unicode 码点计。"""

    start: int = Field(ge=0)
    end: int = Field(ge=1)


class RecordCreateRequest(BaseModel):
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class RecordReviseRequest(BaseModel):
    base_version: int = Field(ge=1)
    text: str
    intervals: list[Interval] = Field(default_factory=list)


class PublishRequest(BaseModel):
    version: int = Field(ge=1)
    # 两种到期方式二选一，均不填则令牌永不到期
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _only_one_expiry(self):
        if self.expires_in_seconds is not None and self.expires_at is not None:
            raise ValueError("use only one of expires_in_seconds and expires_at")
        return self


class RecordResponse(BaseModel):
    record_id: UUID
    version: int


class RecordControlledRequest(BaseModel):
    controlled: bool


class RecordControlledResponse(BaseModel):
    record_id: UUID
    controlled: bool


class SnapshotInfo(BaseModel):
    snapshot_id: UUID
    version: int
    status: str
    published_at: datetime


class RecordStatusResponse(BaseModel):
    record_id: UUID
    current_version: int
    controlled: bool
    snapshots: list[SnapshotInfo]


class PublishRequestCreateRequest(BaseModel):
    """受控发布申请：指定历史版本，沿用发布的两种到期方式（可都不填）。"""

    version: int = Field(ge=1)
    expires_in_seconds: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    note: str | None = None

    @model_validator(mode="after")
    def _only_one_expiry(self):
        if self.expires_in_seconds is not None and self.expires_at is not None:
            raise ValueError("use only one of expires_in_seconds and expires_at")
        return self


class PublishRequestResponse(BaseModel):
    """申请元数据；任何状态下都不包含原文或脱敏正文。"""

    request_id: UUID
    record_id: UUID
    version: int
    status: str
    expires_at: datetime | None
    submitted_by: str
    submitted_at: datetime
    reviewed_by: str | None
    reviewed_at: datetime | None
    review_note: str | None
    snapshot_id: UUID | None


class PublishRequestDecisionRequest(BaseModel):
    note: str | None = None


class PublishApprovedResponse(BaseModel):
    """批准结果：与直接发布一致的不可变快照信息与全新读取令牌。"""

    request_id: UUID
    record_id: UUID
    version: int
    status: str
    snapshot_id: UUID
    token: str
    expires_at: datetime | None


class PublishResponse(BaseModel):
    snapshot_id: UUID
    record_id: UUID
    version: int
    status: str
    token: str
    expires_at: datetime | None


class RevokeResponse(BaseModel):
    token: str
    revoked: bool
    already_revoked: bool


class PublicSnapshotResponse(BaseModel):
    record_id: UUID
    version: int
    status: str
    redacted_text: str
