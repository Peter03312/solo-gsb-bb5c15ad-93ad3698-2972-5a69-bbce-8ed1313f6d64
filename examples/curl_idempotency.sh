#!/usr/bin/env bash
# 幂等重试（Idempotency-Key）调用示例。
#
# 场景：安全员在网络超时后重试区域变更。为写操作携带幂等键后，
# 相同请求的重试返回首次成功的原状态与原响应，不会重复落库。
#
# 演示：
#   1. 带幂等键登记区域（201）
#   2. 网络超时后原样重试（同键同请求）→ 仍 201，逐字节返回原响应，
#      响应头带 Idempotency-Replayed: true，区域不重复登记、revision 不再推进
#   3. 期间其它写入推进 revision 后重试 → 仍返回首次的原响应
#   4. 带键修订区域，重试 → version/revision 不再次推进
#   5. 带键删除区域，重试 → 仍 200（不会变成 404）
#   6. 同键不同请求 → 409 idempotency_conflict，不变更
#   7. 非法键（空 / 超过 128 字符）→ 422 invalid_idempotency_key
#   8. 失败的业务请求不占键：422 后改正请求可用同一键提交
#   9. 批量变更带键：重试时 expected_revision 已过期也返回原响应
#
# 用法：BASE_URL=http://localhost:8000 bash examples/curl_idempotency.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
J() { python3 -c 'import sys,json; print(json.load(sys.stdin)'"$1"')'; }

echo "== 1. 带幂等键登记区域（Idempotency-Key: demo-create-1） =="
FIRST=$(curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-1' \
  -d '{
        "name": "A区-焊接作业面",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }')
echo "$FIRST" | python3 -m json.tool
RID=$(echo "$FIRST" | J "['region']['id']")

echo "== 2. 网络超时后原样重试（同键同请求）→ 201，逐字节返回原响应 =="
curl -sS -D - -o /tmp/idem_replay.json -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-1' \
  -d '{
        "name": "A区-焊接作业面",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }' | grep -iE 'HTTP/|idempotency-replayed'
python3 -m json.tool /tmp/idem_replay.json
[ "$(cat /tmp/idem_replay.json)" = "$(echo "$FIRST" | python3 -c 'import sys,json; print(json.dumps(json.load(sys.stdin),ensure_ascii=False,separators=(",",":")))')" ] \
  && echo "响应体与首次一致" || echo "提示：响应体语义相同（此处比较受格式化影响）"
echo "当前 revision：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")（重试未推进）"

echo "== 3. 其它写入推进 revision 后重试 → 仍返回首次的原响应 =="
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -d '{"name":"无关区域","polygon":[[200,200],[210,200],[210,210]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}' > /dev/null
echo "revision 已被其它登记推进到：$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")"
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-1' \
  -d '{
        "name": "A区-焊接作业面",
        "polygon": [[0,0],[100,0],[100,100],[0,100]],
        "valid_from": "2026-09-01T00:00:00Z",
        "valid_to":   "2026-10-01T00:00:00Z"
      }' | python3 -m json.tool
echo "（响应中的 revision 仍是首次提交时的值）"

echo "== 4. 带键修订区域（demo-update-1），重试不再次推进 version =="
curl -fsS -X PATCH "$BASE_URL/api/regions/$RID" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-update-1' \
  -d '{"name": "A区-缩小"}' | python3 -m json.tool
curl -fsS -X PATCH "$BASE_URL/api/regions/$RID" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-update-1' \
  -d '{"name": "A区-缩小"}' | python3 -m json.tool
echo "version 仍为：$(curl -fsS "$BASE_URL/api/regions/$RID" | J "['version']")（重试未 +1）"

echo "== 5. 带键删除区域（demo-delete-1），重试仍 200 而不变 404 =="
curl -fsS -X DELETE "$BASE_URL/api/regions/$RID" \
  -H 'Idempotency-Key: demo-delete-1' | python3 -m json.tool
curl -fsS -X DELETE "$BASE_URL/api/regions/$RID" \
  -H 'Idempotency-Key: demo-delete-1' | python3 -m json.tool

echo "== 6. 同键不同请求 → 409 idempotency_conflict =="
curl -sS -o /tmp/idem_conflict.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-create-1' \
  -d '{"name":"换了内容","polygon":[[0,0],[1,0],[1,1]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}'
python3 -m json.tool /tmp/idem_conflict.json

echo "== 7. 非法键 → 422 invalid_idempotency_key =="
curl -sS -o /tmp/idem_badkey.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H "Idempotency-Key: $(python3 -c 'print("k"*129)')" \
  -d '{"name":"键太长","polygon":[[0,0],[1,0],[1,1]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}'
python3 -m json.tool /tmp/idem_badkey.json

echo "== 8. 失败的业务请求不占键：先 422，改正后同键可提交 =="
curl -sS -o /tmp/idem_fail.json -w 'HTTP %{http_code}\n' \
  -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-retry-after-failure' \
  -d '{"name":"时段非法","polygon":[[0,0],[1,0],[1,1]],
       "valid_from":"2026-10-01T00:00:00Z","valid_to":"2026-09-01T00:00:00Z"}'
python3 -m json.tool /tmp/idem_fail.json
curl -fsS -X POST "$BASE_URL/api/regions" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-retry-after-failure' \
  -d '{"name":"改正后","polygon":[[0,0],[1,0],[1,1]],
       "valid_from":"2026-09-01T00:00:00Z","valid_to":"2026-10-01T00:00:00Z"}' | python3 -m json.tool

echo "== 9. 批量变更带键：重试时 expected_revision 已过期也返回原响应 =="
REV=$(curl -fsS "$BASE_URL/api/revision" | J "['revision']")
BATCH=$(curl -fsS -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-batch-1' \
  -d "{
        \"expected_revision\": $REV,
        \"operations\": [
          {\"action\": \"create\", \"name\": \"批量区-幂等\",
           \"polygon\": [[300,300],[310,300],[310,310],[300,310]],
           \"valid_from\": \"2026-09-01T00:00:00Z\", \"valid_to\": \"2026-10-01T00:00:00Z\"}
        ],
        \"trajectories\": [[
          {\"x\": -50, \"y\": 500, \"t\": \"2026-09-15T00:00:00Z\"},
          {\"x\": 500, \"y\": 500, \"t\": \"2026-09-15T00:10:00Z\"}
        ]]
      }")
echo "$BATCH" | python3 -m json.tool
echo "重试同一批量请求（expected_revision=$REV 已过期）："
curl -sS -D - -o /tmp/idem_batch_replay.json -X POST "$BASE_URL/api/regions/batch" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-batch-1' \
  -d "{
        \"expected_revision\": $REV,
        \"operations\": [
          {\"action\": \"create\", \"name\": \"批量区-幂等\",
           \"polygon\": [[300,300],[310,300],[310,310],[300,310]],
           \"valid_from\": \"2026-09-01T00:00:00Z\", \"valid_to\": \"2026-10-01T00:00:00Z\"}
        ],
        \"trajectories\": [[
          {\"x\": -50, \"y\": 500, \"t\": \"2026-09-15T00:00:00Z\"},
          {\"x\": 500, \"y\": 500, \"t\": \"2026-09-15T00:10:00Z\"}
        ]]
      }" | grep -iE 'HTTP/|idempotency-replayed'
python3 -m json.tool /tmp/idem_batch_replay.json
echo "（未返回 409 revision_conflict，而是重放首次成功结果；区域 ID 未重新分配）"

echo "完成。提示：示例区域会保留，可用 DELETE /api/regions/{id} 清理。"
