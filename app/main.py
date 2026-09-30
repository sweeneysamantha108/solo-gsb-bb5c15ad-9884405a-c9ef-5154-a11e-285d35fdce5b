import hashlib
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .config import settings
from .db import Base, engine, get_db
from .models import PublishRequest, ReadToken, Record, RecordVersion, Snapshot
from .redaction import IntervalOutOfBoundsError, redact_text, validate_intervals
from .schemas import (
    Interval,
    PublicSnapshotResponse,
    PublishApprovedResponse,
    PublishRequestCreateRequest,
    PublishRequestDecisionRequest,
    PublishRequestResponse,
    PublishRequest as PublishRequestSchema,
    PublishResponse,
    RecordControlledRequest,
    RecordControlledResponse,
    RecordCreateRequest,
    RecordResponse,
    RecordReviseRequest,
    RecordStatusResponse,
    RevokeResponse,
    SnapshotInfo,
)
from .security import Operator, authenticate_operator, require_operator_role, require_service_key

SNAPSHOT_STATUS = "published"

REQUEST_PENDING = "pending"
REQUEST_APPROVED = "approved"
REQUEST_REJECTED = "rejected"


def _init_db_with_retry() -> None:
    """启动时建表（幂等），并等待数据库就绪。"""
    last_exc: Exception | None = None
    for _ in range(settings.db_connect_retries):
        try:
            Base.metadata.create_all(engine)
            return
        except OperationalError as exc:
            last_exc = exc
            time.sleep(settings.db_connect_retry_delay_seconds)
    raise RuntimeError("database is not reachable") from last_exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    _init_db_with_retry()
    yield


app = FastAPI(
    title="Redacted Records API",
    version="1.1.0",
    summary="机构公开脱敏记录服务（支持受控发布复核工作流）",
    lifespan=lifespan,
)


def _validated_intervals(text: str, intervals: list[Interval]) -> list[tuple[int, int]]:
    """越界区间直接 422，且发生在任何写库之前，保证整次拒绝。"""
    try:
        return validate_intervals(len(text), [(iv.start, iv.end) for iv in intervals])
    except IntervalOutOfBoundsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    """规范化数据库读回的时刻：naive 一律按 UTC 解释。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _expires_at_from(expires_in_seconds: int | None, expires_at: datetime | None) -> datetime | None:
    """两种到期方式二选一，统一换算为 UTC 绝对时刻；过去时刻在写库前 422。"""
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="expires_at must be in the future",
            )
        return expires_at
    if expires_in_seconds is not None:
        return datetime.now(timezone.utc) + timedelta(seconds=expires_in_seconds)
    return None


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.post(
    "/records",
    status_code=status.HTTP_201_CREATED,
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def create_record(payload: RecordCreateRequest, db: Session = Depends(get_db)):
    """创建记录（版本 1）：提交原文与敏感区间。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    record = Record(current_version=1)
    db.add(record)
    db.flush()
    db.add(
        RecordVersion(
            record_id=record.id,
            version=1,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record.id, version=1)


@app.put(
    "/records/{record_id}",
    response_model=RecordResponse,
    dependencies=[Depends(require_service_key)],
)
def revise_record(record_id: UUID, payload: RecordReviseRequest, db: Session = Depends(get_db)):
    """修订记录：必须携带当前版本号作为基准，过期则 409 并返回当前版本。"""
    intervals = _validated_intervals(payload.text, payload.intervals)
    # 条件更新是原子的：并发修订时只有一个能基于同一基准版本成功，
    # 其余因 current_version 已变化而失败，不会产生分叉版本。
    new_version = db.execute(
        update(Record)
        .where(Record.id == record_id, Record.current_version == payload.base_version)
        .values(current_version=payload.base_version + 1)
        .returning(Record.current_version)
    ).scalar_one_or_none()
    if new_version is None:
        db.rollback()
        record = db.get(Record, record_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "base_version is stale",
                "current_version": record.current_version,
            },
        )
    db.add(
        RecordVersion(
            record_id=record_id,
            version=new_version,
            original_text=payload.text,
            intervals=[{"start": s, "end": e} for s, e in intervals],
        )
    )
    db.commit()
    return RecordResponse(record_id=record_id, version=new_version)


@app.post(
    "/records/{record_id}/controlled",
    response_model=RecordControlledResponse,
    dependencies=[Depends(require_service_key)],
)
def set_record_controlled(
    record_id: UUID, payload: RecordControlledRequest, db: Session = Depends(get_db)
):
    """将单条记录设为/取消受控发布；受控后直接发布被拒绝，只能走复核工作流。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    record.controlled = payload.controlled
    db.commit()
    return RecordControlledResponse(record_id=record_id, controlled=record.controlled)


@app.post(
    "/records/{record_id}/publish",
    status_code=status.HTTP_201_CREATED,
    response_model=PublishResponse,
    dependencies=[Depends(require_service_key)],
)
def publish_record(record_id: UUID, payload: PublishRequestSchema, db: Session = Depends(get_db)):
    """发布指定版本：合并区间生成不可变快照，并签发随机读取令牌。

    受控记录的直接发布一律 403 拒绝，必须走 /publish-requests 复核工作流。
    """
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    if record.controlled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="direct publish is disabled for controlled records",
        )
    version = db.get(RecordVersion, (record_id, payload.version))
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    expires_at = _expires_at_from(payload.expires_in_seconds, payload.expires_at)
    redacted = redact_text(
        version.original_text, [(iv["start"], iv["end"]) for iv in version.intervals]
    )
    snapshot_id = _insert_snapshot(db, record_id, payload.version, redacted)
    token = ReadToken(
        token=secrets.token_urlsafe(32), snapshot_id=snapshot_id, expires_at=expires_at
    )
    db.add(token)
    db.commit()
    return PublishResponse(
        snapshot_id=snapshot_id,
        record_id=record_id,
        version=payload.version,
        status=SNAPSHOT_STATUS,
        token=token.token,
        expires_at=expires_at,
    )


def _insert_snapshot(db: Session, record_id: UUID, version: int, redacted: str) -> UUID:
    """快照按 (record_id, version) 唯一：并发发布时唯一约束保证只落库一份。

    PostgreSQL 使用 INSERT ... ON CONFLICT DO NOTHING；其他方言（如测试用 SQLite）
    先查后插并吞掉唯一约束冲突，语义一致。
    """
    if engine.dialect.name == "postgresql":
        db.execute(
            pg_insert(Snapshot)
            .values(record_id=record_id, version=version, redacted_text=redacted)
            .on_conflict_do_nothing(constraint="uq_snapshots_record_version")
        )
    else:
        existing_id = db.execute(
            select(Snapshot.id).where(
                Snapshot.record_id == record_id, Snapshot.version == version
            )
        ).scalar_one_or_none()
        if existing_id is not None:
            return existing_id
        db.add(
            Snapshot(record_id=record_id, version=version, redacted_text=redacted)
        )
        db.flush()
    return db.execute(
        select(Snapshot.id).where(
            Snapshot.record_id == record_id, Snapshot.version == version
        )
    ).scalar_one()


@app.post(
    "/records/{record_id}/publish-requests",
    status_code=status.HTTP_201_CREATED,
    response_model=PublishRequestResponse,
)
def submit_publish_request(
    record_id: UUID,
    payload: PublishRequestCreateRequest,
    db: Session = Depends(get_db),
    operator: Operator = Depends(require_operator_role("operator")),
):
    """操作者以个人身份提交指定历史版本的受控发布申请。

    申请冻结提交时的版本内容（record_id+version+content_hash+区间副本），
    后续修订只追加新版本，不改变待审内容。
    """
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    version = db.get(RecordVersion, (record_id, payload.version))
    if version is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="record version not found"
        )
    if not record.controlled:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="record is not under controlled publishing",
        )
    expires_at = _expires_at_from(payload.expires_in_seconds, payload.expires_at)
    publish_request = PublishRequest(
        record_id=record_id,
        version=payload.version,
        content_hash=_content_hash(version.original_text),
        # 区间随申请留一份副本，审批脱敏以“提交时版本”为准
        intervals=[dict(iv) for iv in version.intervals],
        status=REQUEST_PENDING,
        expires_at=expires_at,
        submitted_by=operator.operator_id,
        review_note=payload.note,
    )
    db.add(publish_request)
    db.commit()
    return _publish_request_response(publish_request)


@app.get(
    "/publish-requests/{request_id}",
    response_model=PublishRequestResponse,
)
def get_publish_request(
    request_id: UUID,
    db: Session = Depends(get_db),
    operator: Operator = Depends(authenticate_operator),
):
    """查看申请状态（仅元数据，不含正文/脱敏正文/令牌）。"""
    publish_request = db.get(PublishRequest, request_id)
    if publish_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    return _publish_request_response(publish_request)


def _publish_request_response(publish_request: PublishRequest) -> PublishRequestResponse:
    return PublishRequestResponse(
        request_id=publish_request.id,
        record_id=publish_request.record_id,
        version=publish_request.version,
        status=publish_request.status,
        expires_at=publish_request.expires_at,
        submitted_by=publish_request.submitted_by,
        submitted_at=publish_request.submitted_at,
        reviewed_by=publish_request.reviewed_by,
        reviewed_at=publish_request.reviewed_at,
        review_note=publish_request.review_note,
        snapshot_id=publish_request.snapshot_id,
    )


def _load_pending_request_for_review(
    db: Session, request_id: UUID, reviewer: Operator
) -> PublishRequest:
    """加载待复核申请并执行与状态无关的拒绝规则；错误信息不含任何正文。"""
    publish_request = db.get(PublishRequest, request_id)
    if publish_request is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="publish request not found"
        )
    # 申请人不得复核自己的申请
    if publish_request.submitted_by == reviewer.operator_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="submitter cannot review their own request",
        )
    if publish_request.status != REQUEST_PENDING:
        # 已批准/已驳回均为终态；驳回后不能再批准
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"message": "publish request is already decided", "status": publish_request.status},
        )
    return publish_request


@app.post(
    "/publish-requests/{request_id}/approve",
    response_model=PublishApprovedResponse,
)
def approve_publish_request(
    request_id: UUID,
    payload: PublishRequestDecisionRequest,
    db: Session = Depends(get_db),
    reviewer: Operator = Depends(require_operator_role("reviewer")),
):
    """批准申请：沿用既有脱敏与令牌规则，同一事务生成不可变快照与新读取令牌。

    用条件 UPDATE 原子认领 pending 申请：并发复核只有一个能进入终态；
    事务任一步失败整体回滚，不会留下快照关联或可读令牌。
    """
    publish_request = _load_pending_request_for_review(db, request_id, reviewer)

    # 原子认领：仅当仍为 pending 时翻转为 approved，行锁阻止并发复核
    claimed = db.execute(
        update(PublishRequest)
        .where(PublishRequest.id == request_id, PublishRequest.status == REQUEST_PENDING)
        .values(
            status=REQUEST_APPROVED,
            reviewed_by=reviewer.operator_id,
            reviewed_at=func.now(),
        )
        .returning(PublishRequest.id)
    ).scalar_one_or_none()
    if claimed is None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="publish request is already decided",
        )

    version = db.get(RecordVersion, (publish_request.record_id, publish_request.version))
    if version is None or _content_hash(version.original_text) != publish_request.content_hash:
        # 申请绑定的版本内容缺失或指纹不符：整体回滚，申请回到 pending，绝不发布
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="cannot approve: bound version content is unavailable or altered",
        )

    if (
        publish_request.expires_at is not None
        and _as_utc(publish_request.expires_at) <= datetime.now(timezone.utc)
    ):
        # 请求的令牌时刻已过；回滚认领保持 pending，避免签发即失效的令牌
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="requested token expiry is in the past",
        )

    redacted = redact_text(
        version.original_text, [(iv["start"], iv["end"]) for iv in publish_request.intervals]
    )
    snapshot_id = _insert_snapshot(db, publish_request.record_id, publish_request.version, redacted)
    token = ReadToken(
        token=secrets.token_urlsafe(32),
        snapshot_id=snapshot_id,
        expires_at=publish_request.expires_at,
    )
    db.add(token)
    publish_request.snapshot_id = snapshot_id
    try:
        db.commit()
    except Exception:
        # 提交失败时快照与令牌一并回滚，绝不留下可读令牌
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="approval failed",
        )
    return PublishApprovedResponse(
        request_id=request_id,
        record_id=publish_request.record_id,
        version=publish_request.version,
        status=REQUEST_APPROVED,
        snapshot_id=snapshot_id,
        token=token.token,
        expires_at=publish_request.expires_at,
    )


@app.post(
    "/publish-requests/{request_id}/reject",
    response_model=PublishRequestResponse,
)
def reject_publish_request(
    request_id: UUID,
    payload: PublishRequestDecisionRequest,
    db: Session = Depends(get_db),
    reviewer: Operator = Depends(require_operator_role("reviewer")),
):
    """驳回申请：终态，驳回后不能批准；并发驳回/批准只产生一个终态。"""
    publish_request = _load_pending_request_for_review(db, request_id, reviewer)
    claimed = db.execute(
        update(PublishRequest)
        .where(PublishRequest.id == request_id, PublishRequest.status == REQUEST_PENDING)
        .values(
            status=REQUEST_REJECTED,
            reviewed_by=reviewer.operator_id,
            reviewed_at=func.now(),
            review_note=payload.note,
        )
        .returning(PublishRequest.id)
    ).scalar_one_or_none()
    if claimed is None:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="publish request is already decided",
        )
    db.commit()
    db.refresh(publish_request)
    return _publish_request_response(publish_request)


@app.post(
    "/tokens/{token}/revoke",
    response_model=RevokeResponse,
    dependencies=[Depends(require_service_key)],
)
def revoke_token(token: str, db: Session = Depends(get_db)):
    """撤销令牌：单条原子 UPDATE，提交后新开始的读取一律拒绝。"""
    result = db.execute(
        update(ReadToken)
        .where(ReadToken.token == token, ReadToken.revoked_at.is_(None))
        .values(revoked_at=func.now())
    )
    db.commit()
    if result.rowcount:
        return RevokeResponse(token=token, revoked=True, already_revoked=False)
    if db.get(ReadToken, token) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="token not found")
    return RevokeResponse(token=token, revoked=True, already_revoked=True)


@app.get(
    "/records/{record_id}",
    response_model=RecordStatusResponse,
    dependencies=[Depends(require_service_key)],
)
def get_record(record_id: UUID, db: Session = Depends(get_db)):
    """维护侧查看记录当前版本与已发布快照列表。"""
    record = db.get(Record, record_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="record not found")
    snapshots = (
        db.execute(
            select(Snapshot).where(Snapshot.record_id == record_id).order_by(Snapshot.version)
        )
        .scalars()
        .all()
    )
    return RecordStatusResponse(
        record_id=record.id,
        current_version=record.current_version,
        controlled=record.controlled,
        snapshots=[
            SnapshotInfo(
                snapshot_id=s.id,
                version=s.version,
                status=SNAPSHOT_STATUS,
                published_at=s.published_at,
            )
            for s in snapshots
        ],
    )


@app.get("/public/snapshots/{snapshot_id}", response_model=PublicSnapshotResponse)
def read_snapshot(
    snapshot_id: UUID,
    token: str = Query(..., description="发布时签发的读取令牌"),
    db: Session = Depends(get_db),
):
    """公开读取：凭有效令牌返回脱敏正文；任何失败都返回统一的模糊错误。"""
    snapshot = db.get(Snapshot, snapshot_id)
    read_token = db.get(ReadToken, token)
    now = datetime.now(timezone.utc)
    if (
        snapshot is None
        or read_token is None
        or read_token.snapshot_id != snapshot.id
        or read_token.revoked_at is not None
        or (
            read_token.expires_at is not None
            and _as_utc(read_token.expires_at) <= now
        )
    ):
        # 不区分“不存在 / 已撤销 / 已过期”，避免泄露任何信息
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="snapshot unavailable"
        )
    return PublicSnapshotResponse(
        record_id=snapshot.record_id,
        version=snapshot.version,
        status=SNAPSHOT_STATUS,
        redacted_text=snapshot.redacted_text,
    )
