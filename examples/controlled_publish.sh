#!/usr/bin/env bash
# 受控发布工作流端到端示例：
#   设为受控 -> 直接发布被拒 -> 提交申请 -> 无凭据/越权/自审拒绝
#   -> 他人复核 -> 批准生成快照与令牌 -> 公开读取 -> 驳回终态
# 依赖：curl、jq
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
SERVICE_KEY="${SERVICE_KEY:-dev-service-key-change-me}"
SVC=(-H "X-Service-Key: ${SERVICE_KEY}" -H "Content-Type: application/json")
# 演示操作者目录（见 README“配置”）；生产用 OPERATOR_CREDENTIALS 覆盖
ALICE=(-H "X-Operator-Id: alice" -H "X-Operator-Key: alice-key" -H "Content-Type: application/json")
BOB=(-H "X-Operator-Id: bob"   -H "X-Operator-Key: bob-key"   -H "Content-Type: application/json")
CAROL=(-H "X-Operator-Id: carol" -H "X-Operator-Key: carol-key" -H "Content-Type: application/json")

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "1. 创建记录并设为受控发布"
RID=$(curl -sS -X POST "${BASE_URL}/records" "${SVC[@]}" -d '{
  "text": "患者张三，身份证号 110101199003077777，联系电话 13800001111。",
  "intervals": [{"start": 2, "end": 4}, {"start": 10, "end": 28}, {"start": 34, "end": 45}]
}' | jq -r .record_id)
curl -sS -X POST "${BASE_URL}/records/${RID}/controlled" "${SVC[@]}" -d '{"controlled": true}' | jq .

step "2. 受控记录的既有直接发布请求 -> 403 明确拒绝"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST "${BASE_URL}/records/${RID}/publish" \
  "${SVC[@]}" -d '{"version": 1}'

step "3. 无操作者凭据提交申请 -> 401；未知操作者 -> 401"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "${BASE_URL}/records/${RID}/publish-requests" -H "Content-Type: application/json" -d '{"version": 1}'
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "${BASE_URL}/records/${RID}/publish-requests" \
  -H "X-Operator-Id: eve" -H "X-Operator-Key: guess" -H "Content-Type: application/json" \
  -d '{"version": 1}'

step "4. bob（operator）提交指定历史版本 v1 的发布申请"
PR=$(curl -sS -X POST "${BASE_URL}/records/${RID}/publish-requests" "${BOB[@]}" \
  -d '{"version": 1}')
echo "${PR}" | jq .
PRID=$(echo "${PR}" | jq -r .request_id)

step "5. 申请人不得复核；bob 也不具备 reviewer 角色 -> 403"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "${BASE_URL}/publish-requests/${PRID}/approve" "${BOB[@]}" -d '{}'

step "6. carol（reviewer，且非申请人）批准 -> 同事务生成快照与新读取令牌"
AP=$(curl -sS -X POST "${BASE_URL}/publish-requests/${PRID}/approve" "${CAROL[@]}" -d '{}')
echo "${AP}" | jq .
SID=$(echo "${AP}" | jq -r .snapshot_id)
TOK=$(echo "${AP}" | jq -r .token)

step "7. 凭新令牌公开读取脱敏正文（错误响应不泄露原文）"
curl -sS "${BASE_URL}/public/snapshots/${SID}?token=${TOK}" | jq .

step "8. 已终结申请再次批准/驳回 -> 409（并发复核也只产生一个终态）"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "${BASE_URL}/publish-requests/${PRID}/approve" "${CAROL[@]}" -d '{}'
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X POST \
  "${BASE_URL}/publish-requests/${PRID}/reject" "${ALICE[@]}" -d '{}'

step "9. 新记录演示驳回：驳回后不能批准，且不产生快照/令牌"
RID2=$(curl -sS -X POST "${BASE_URL}/records" "${SVC[@]}" -d '{
  "text": "另一条受控记录。", "intervals": [{"start": 3, "end": 5}]
}' | jq -r .record_id)
curl -sS -o /dev/null -X POST "${BASE_URL}/records/${RID2}/controlled" "${SVC[@]}" \
  -d '{"controlled": true}'
PRID2=$(curl -sS -X POST "${BASE_URL}/records/${RID2}/publish-requests" "${BOB[@]}" \
  -d '{"version": 1}' | jq -r .request_id)
curl -sS -X POST "${BASE_URL}/publish-requests/${PRID2}/reject" "${CAROL[@]}" \
  -d '{"note": "材料不全"}' | jq .
curl -sS -o /dev/null -w '驳回后批准 HTTP %{http_code}\n' -X POST \
  "${BASE_URL}/publish-requests/${PRID2}/approve" "${ALICE[@]}" -d '{}'
curl -sS "${BASE_URL}/records/${RID2}" "${SVC[@]}" | jq '.snapshots | length'

step "10. 非受控记录与既有接口保持兼容（直接发布仍可用）"
RID3=$(curl -sS -X POST "${BASE_URL}/records" "${SVC[@]}" \
  -d '{"text": "普通记录。", "intervals": []}' | jq -r .record_id)
curl -sS -X POST "${BASE_URL}/records/${RID3}/publish" "${SVC[@]}" -d '{"version": 1}' | jq .

printf '\n\033[1m受控发布演示完成。\033[0m\n'
