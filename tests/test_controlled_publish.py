"""受控发布工作流的端到端测试（SQLite 内存库 + FastAPI TestClient）。

运行：SERVICE_KEY=svk python3 -m unittest tests.test_controlled_publish -v
"""

import os
import threading
import unittest
import uuid

os.environ.setdefault("SERVICE_KEY", "test-service-key")
# import 前切到 SQLite，避免 app.db 构建引擎时需要 psycopg2/PostgreSQL
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.db as db_module  # noqa: E402
from app.db import Base  # noqa: E402
from app.main import app  # noqa: E402
from app import models  # noqa: E402,F401  (确保模型已注册)


SERVICE_KEY = "test-service-key"
SVC = {"X-Service-Key": SERVICE_KEY}
ALICE = {"X-Operator-Id": "alice", "X-Operator-Key": "alice-key"}  # operator + reviewer
BOB = {"X-Operator-Id": "bob", "X-Operator-Key": "bob-key"}  # operator only
CAROL = {"X-Operator-Id": "carol", "X-Operator-Key": "carol-key"}  # reviewer only


def make_client():
    # 共享的内存 SQLite：StaticPool 复用单一连接，多线程可见同一份数据
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        session = TestingSession()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[db_module.get_db] = override_get_db
    # 测试客户端不触发生命周期（避免启动时去连真实 PostgreSQL）
    client = TestClient(app)
    return client, TestingSession


def create_record(client, text="患者张三，身份证号 110101199003077777。", intervals=None):
    if intervals is None:
        intervals = [{"start": 2, "end": 4}, {"start": 10, "end": 28}]
    r = client.post("/records", headers=SVC, json={"text": text, "intervals": intervals})
    assert r.status_code == 201, r.text
    return r.json()["record_id"], text, intervals


def set_controlled(client, record_id, flag=True):
    r = client.post(
        f"/records/{record_id}/controlled", headers=SVC, json={"controlled": flag}
    )
    assert r.status_code == 200, r.text
    assert r.json()["controlled"] is flag


class ControlledWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.client, self.Session = make_client()

    # ---- 鉴权 ----
    def test_operator_endpoints_require_credentials(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        r = self.client.post(
            f"/records/{rid}/publish-requests", json={"version": 1}
        )
        self.assertEqual(r.status_code, 401)

    def test_unknown_operator_rejected(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        r = self.client.post(
            f"/records/{rid}/publish-requests",
            headers={"X-Operator-Id": "eve", "X-Operator-Key": "whatever"},
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)
        self.assertNotIn("患者", r.text)

    def test_wrong_key_rejected(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        r = self.client.post(
            f"/records/{rid}/publish-requests",
            headers={"X-Operator-Id": "bob", "X-Operator-Key": "wrong"},
            json={"version": 1},
        )
        self.assertEqual(r.status_code, 401)

    def test_reviewer_cannot_submit(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        r = self.client.post(
            f"/records/{rid}/publish-requests", headers=CAROL, json={"version": 1}
        )
        self.assertEqual(r.status_code, 403)

    def test_operator_cannot_approve(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        pr = self.client.post(
            f"/records/{rid}/publish-requests", headers=BOB, json={"version": 1}
        ).json()
        r = self.client.post(
            f"/publish-requests/{pr['request_id']}/approve", headers=BOB, json={}
        )
        self.assertEqual(r.status_code, 403)

    # ---- 申请与终态规则 ----
    def test_submit_unknown_record_or_version(self):
        r = self.client.post(
            f"/records/{uuid.uuid4()}/publish-requests", headers=BOB, json={"version": 1}
        )
        self.assertEqual(r.status_code, 404)
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        r = self.client.post(
            f"/records/{rid}/publish-requests", headers=BOB, json={"version": 99}
        )
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("患者", r.text)

    def test_submit_on_non_controlled_rejected(self):
        rid, _, _ = create_record(self.client)
        r = self.client.post(
            f"/records/{rid}/publish-requests", headers=BOB, json={"version": 1}
        )
        self.assertEqual(r.status_code, 409)

    def test_direct_publish_on_controlled_rejected(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        r = self.client.post(
            f"/records/{rid}/publish", headers=SVC, json={"version": 1}
        )
        self.assertEqual(r.status_code, 403)
        self.assertNotIn("患者", r.text)

    def test_direct_publish_still_works_when_not_controlled(self):
        rid, _, _ = create_record(self.client)
        r = self.client.post(
            f"/records/{rid}/publish", headers=SVC, json={"version": 1}
        )
        self.assertEqual(r.status_code, 201, r.text)
        self.assertIn("token", r.json())

    def test_submitter_cannot_review_own_request(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        pr = self.client.post(
            f"/records/{rid}/publish-requests", headers=ALICE, json={"version": 1}
        ).json()
        r = self.client.post(
            f"/publish-requests/{pr['request_id']}/approve", headers=ALICE, json={}
        )
        self.assertEqual(r.status_code, 403)
        r = self.client.post(
            f"/publish-requests/{pr['request_id']}/reject", headers=ALICE, json={}
        )
        self.assertEqual(r.status_code, 403)

    def test_unknown_request_rejected(self):
        r = self.client.post(
            f"/publish-requests/{uuid.uuid4()}/approve", headers=CAROL, json={}
        )
        self.assertEqual(r.status_code, 404)
        r = self.client.post(
            f"/publish-requests/{uuid.uuid4()}/reject", headers=CAROL, json={}
        )
        self.assertEqual(r.status_code, 404)

    # ---- 批准主路径 ----
    def test_full_approve_flow_snapshot_and_token(self):
        rid, text, intervals = create_record(self.client)
        set_controlled(self.client, rid)
        pr = self.client.post(
            f"/records/{rid}/publish-requests",
            headers=BOB,
            json={"version": 1, "expires_in_seconds": 3600},
        )
        self.assertEqual(pr.status_code, 201, pr.text)
        body = pr.json()
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["submitted_by"], "bob")
        self.assertIsNone(body["snapshot_id"])
        self.assertNotIn("token", body)
        self.assertNotIn("患者", pr.text)

        approved = self.client.post(
            f"/publish-requests/{body['request_id']}/approve", headers=CAROL, json={}
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        abody = approved.json()
        self.assertEqual(abody["status"], "approved")
        self.assertTrue(abody["token"])
        self.assertIsNotNone(abody["expires_at"])

        # 令牌可读取脱敏正文（沿用既有规则）
        read = self.client.get(
            f"/public/snapshots/{abody['snapshot_id']}?token={abody['token']}"
        )
        self.assertEqual(read.status_code, 200)
        redacted = read.json()["redacted_text"]
        self.assertIn("[已遮蔽]", redacted)
        self.assertNotIn("张三", redacted)
        self.assertNotIn("110101199003077777", redacted)

        # 申请状态已更新
        got = self.client.get(
            f"/publish-requests/{body['request_id']}", headers=BOB
        ).json()
        self.assertEqual(got["status"], "approved")
        self.assertEqual(got["reviewed_by"], "carol")
        self.assertEqual(got["snapshot_id"], abody["snapshot_id"])
        self.assertNotIn("token", got)

    def test_approved_request_is_terminal(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        pr = self.client.post(
            f"/records/{rid}/publish-requests", headers=BOB, json={"version": 1}
        ).json()
        self.client.post(
            f"/publish-requests/{pr['request_id']}/approve", headers=CAROL, json={}
        )
        # 再批准 / 驳回都必须 409
        r = self.client.post(
            f"/publish-requests/{pr['request_id']}/approve", headers=CAROL, json={}
        )
        self.assertEqual(r.status_code, 409)
        r = self.client.post(
            f"/publish-requests/{pr['request_id']}/reject", headers=ALICE, json={}
        )
        self.assertEqual(r.status_code, 409)

    def test_rejected_request_is_terminal_and_cannot_approve(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        pr = self.client.post(
            f"/records/{rid}/publish-requests", headers=BOB, json={"version": 1}
        ).json()
        r = self.client.post(
            f"/publish-requests/{pr['request_id']}/reject",
            headers=CAROL,
            json={"note": "信息不全"},
        )
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["status"], "rejected")
        self.assertEqual(r.json()["review_note"], "信息不全")
        # 驳回后不能批准
        r = self.client.post(
            f"/publish-requests/{pr['request_id']}/approve", headers=ALICE, json={}
        )
        self.assertEqual(r.status_code, 409)
        # 驳回不产生任何令牌/快照：公开侧无新快照
        rec = self.client.get(f"/records/{rid}", headers=SVC).json()
        self.assertEqual(rec["snapshots"], [])
        self.assertTrue(rec["controlled"])

    # ---- 申请冻结版本内容 ----
    def test_request_bound_to_submitted_version_content(self):
        rid, v1_text, _ = create_record(
            self.client,
            text="患者张三，身份证号 110101199003077777。",
            intervals=[{"start": 2, "end": 4}],
        )
        set_controlled(self.client, rid)
        pr = self.client.post(
            f"/records/{rid}/publish-requests", headers=BOB, json={"version": 1}
        ).json()
        # 提交后继续修订到版本 2
        rev = self.client.put(
            f"/records/{rid}",
            headers=SVC,
            json={
                "base_version": 1,
                "text": "完全不同的新原文，李四手机号 13900000000。",
                "intervals": [{"start": 9, "end": 20}],
            },
        )
        self.assertEqual(rev.status_code, 200, rev.text)
        # 批准的仍是版本 1 的内容
        approved = self.client.post(
            f"/publish-requests/{pr['request_id']}/approve", headers=CAROL, json={}
        ).json()
        read = self.client.get(
            f"/public/snapshots/{approved['snapshot_id']}?token={approved['token']}"
        ).json()
        self.assertEqual(read["version"], 1)
        self.assertIn("患者", read["redacted_text"])
        self.assertNotIn("李四", read["redacted_text"])

    def test_history_versions_remain_readable_for_request(self):
        # 即使旧版本已有快照，受控工作流仍指向历史版本内容（唯一快照复用）
        rid, _, _ = create_record(self.client)
        # 非受控时先直接发布
        pub = self.client.post(
            f"/records/{rid}/publish", headers=SVC, json={"version": 1}
        ).json()
        # 再受控：修订不影响；申请历史版本 1 批准时复用同一份不可变快照
        set_controlled(self.client, rid)
        self.client.put(
            f"/records/{rid}",
            headers=SVC,
            json={"base_version": 1, "text": "v2 文本", "intervals": []},
        )
        pr = self.client.post(
            f"/records/{rid}/publish-requests", headers=BOB, json={"version": 1}
        ).json()
        approved = self.client.post(
            f"/publish-requests/{pr['request_id']}/approve", headers=CAROL, json={}
        ).json()
        self.assertEqual(approved["snapshot_id"], pub["snapshot_id"])
        # 新令牌独立可用
        read = self.client.get(
            f"/public/snapshots/{approved['snapshot_id']}?token={approved['token']}"
        )
        self.assertEqual(read.status_code, 200)

    # ---- 并发复核：只产生一个终态 ----
    def test_concurrent_reviews_produce_single_terminal_state(self):
        import tempfile
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path

        from fastapi import HTTPException as FastAPIHTTPException

        from app import main as main_module
        from app.schemas import PublishRequestDecisionRequest
        from app.security import Operator

        tmpdir = tempfile.mkdtemp()
        db_path = Path(tmpdir) / "concurrency.db"
        # 每个引擎独立连接，指向同一份文件库（WAL + busy_timeout 支持真并发写）
        setup_engine = create_engine(f"sqlite:///{db_path}")
        with setup_engine.begin() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA busy_timeout=10000")
        Base.metadata.create_all(setup_engine)

        def new_session():
            eng = create_engine(
                f"sqlite:///{db_path}",
                connect_args={"check_same_thread": False, "timeout": 30},
            )
            return sessionmaker(bind=eng, autoflush=False, expire_on_commit=False)()

        setup_db = new_session()
        record = models.Record(current_version=1, controlled=True)
        setup_db.add(record)
        setup_db.flush()
        setup_db.add(
            models.RecordVersion(
                record_id=record.id,
                version=1,
                original_text="患者张三。",
                intervals=[{"start": 2, "end": 4}],
            )
        )
        req = models.PublishRequest(
            record_id=record.id,
            version=1,
            content_hash=main_module._content_hash("患者张三。"),
            intervals=[{"start": 2, "end": 4}],
            status="pending",
            submitted_by="bob",
        )
        setup_db.add(req)
        setup_db.commit()
        request_id = req.id
        setup_db.close()

        reviewer = Operator(operator_id="carol", roles=frozenset({"reviewer"}))
        decision_payload = PublishRequestDecisionRequest()

        def call(decision):
            session = new_session()
            try:
                if decision == "approve":
                    main_module.approve_publish_request(
                        request_id, decision_payload, session, reviewer
                    )
                else:
                    main_module.reject_publish_request(
                        request_id, decision_payload, session, reviewer
                    )
                return decision, 200
            except FastAPIHTTPException as exc:
                return decision, exc.status_code
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=3) as pool:
            outcomes = list(pool.map(call, ["approve", "reject", "approve"]))

        codes = sorted(code for _, code in outcomes)
        self.assertEqual(codes.count(200), 1, outcomes)
        self.assertEqual(codes.count(409), 2, outcomes)

        check_db = new_session()
        final = check_db.get(models.PublishRequest, request_id)
        self.assertIn(final.status, ("approved", "rejected"))
        snapshot_count = check_db.query(models.Snapshot).count()
        token_count = check_db.query(models.ReadToken).count()
        if final.status == "approved":
            self.assertEqual(snapshot_count, 1)
            self.assertEqual(token_count, 1)
        else:
            self.assertEqual(snapshot_count, 0)
            self.assertEqual(token_count, 0)
        check_db.close()

    # ---- 既有接口兼容 ----
    def test_legacy_create_revise_revoke_unchanged(self):
        rid, _, _ = create_record(self.client)
        # 401 仍生效
        self.assertEqual(
            self.client.post("/records", json={"text": "x"}).status_code, 401
        )
        # 修订乐观锁
        r = self.client.put(
            f"/records/{rid}",
            headers=SVC,
            json={"base_version": 99, "text": "y", "intervals": []},
        )
        self.assertEqual(r.status_code, 409)
        # 越界 422
        r = self.client.put(
            f"/records/{rid}",
            headers=SVC,
            json={"base_version": 1, "text": "短", "intervals": [{"start": 0, "end": 9}]},
        )
        self.assertEqual(r.status_code, 422)
        # 撤销未知令牌 404
        self.assertEqual(
            self.client.post("/tokens/nope/revoke", headers=SVC).status_code, 404
        )
        # GET 记录带 controlled 字段且缺省 false
        rec = self.client.get(f"/records/{rid}", headers=SVC).json()
        self.assertFalse(rec["controlled"])

    def test_unset_controlled_restores_direct_publish(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid, True)
        self.assertEqual(
            self.client.post(
                f"/records/{rid}/publish", headers=SVC, json={"version": 1}
            ).status_code,
            403,
        )
        set_controlled(self.client, rid, False)
        self.assertEqual(
            self.client.post(
                f"/records/{rid}/publish", headers=SVC, json={"version": 1}
            ).status_code,
            201,
        )

    def test_expires_at_past_rejected_at_submit(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        r = self.client.post(
            f"/records/{rid}/publish-requests",
            headers=BOB,
            json={"version": 1, "expires_at": "2000-01-01T00:00:00Z"},
        )
        self.assertEqual(r.status_code, 422)

    def test_token_from_approval_revokable_via_legacy_endpoint(self):
        rid, _, _ = create_record(self.client)
        set_controlled(self.client, rid)
        pr = self.client.post(
            f"/records/{rid}/publish-requests", headers=BOB, json={"version": 1}
        ).json()
        approved = self.client.post(
            f"/publish-requests/{pr['request_id']}/approve", headers=CAROL, json={}
        ).json()
        r = self.client.post(f"/tokens/{approved['token']}/revoke", headers=SVC)
        self.assertEqual(r.status_code, 200)
        read = self.client.get(
            f"/public/snapshots/{approved['snapshot_id']}?token={approved['token']}"
        )
        self.assertEqual(read.status_code, 403)


if __name__ == "__main__":
    unittest.main()
