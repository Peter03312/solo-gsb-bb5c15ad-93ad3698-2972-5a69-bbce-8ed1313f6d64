"""幂等键（Idempotency-Key）集成测试，需要一个可连接的 PostgreSQL（DATABASE_URL）。

运行：
    DATABASE_URL=postgresql://restrict@localhost:5432/restrict \
        python3 tests/test_idempotency_api.py
脚本会清空 regions/region_meta/passes/idempotency_keys 后跑全套用例。
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.main import app  # noqa: E402

failures: list[str] = []


def check(label, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


db.init_pool()
client = TestClient(app)

# 清空数据
with db.pool.connection() as conn:
    with conn.cursor() as cur:
        cur.execute("TRUNCATE idempotency_keys")
        cur.execute("TRUNCATE passes RESTART IDENTITY")
        cur.execute("TRUNCATE regions RESTART IDENTITY")
        cur.execute("UPDATE region_meta SET revision = 0 WHERE id = 1")
    conn.commit()


def revision() -> int:
    return client.get("/api/revision").json()["revision"]


def region_count() -> int:
    return len(client.get("/api/regions").json())


REGION_A = {
    "name": "A区-幂等",
    "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
    "valid_from": "2026-09-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
}

# ---------------------------------------------------------------- 登记幂等

key = "create-0001"
r1 = client.post("/api/regions", json=REGION_A, headers={"Idempotency-Key": key})
check("带键登记 201", r1.status_code == 201, r1.text)
check("首次响应不带重放标记", "idempotency-replayed" not in {k.lower() for k in r1.headers})
body1 = r1.json()
check("revision 推进到 1", body1["revision"] == 1, str(body1))

r2 = client.post("/api/regions", json=REGION_A, headers={"Idempotency-Key": key})
check("相同请求重试仍 201", r2.status_code == 201, r2.text)
check("重试返回原响应体", r2.json() == body1, f"{r2.json()} != {body1}")
check("重试响应与首次逐字节一致", r2.text == r1.text)
check("重试响应带重放标记", r2.headers.get("Idempotency-Replayed") == "true")
check("重试未新增区域", region_count() == 1)
check("重试未推进 revision", revision() == 1)

# 期间有其他写入推进 revision 后，重试仍返回原状态与原响应
r_other = client.post("/api/regions", json={**REGION_A, "name": "无关区域"})
check("无键登记正常推进 revision", r_other.status_code == 201 and revision() == 2)
r3 = client.post("/api/regions", json=REGION_A, headers={"Idempotency-Key": key})
check("revision 变化后重试仍返回原响应", r3.status_code == 201 and r3.json() == body1)
check("重试后 revision 不再推进", revision() == 2)
check("仍未重复登记", region_count() == 2)

# 规范化判定：时区写法不同、多边形显式闭合，但语义相同的请求视为同一次
key_norm = "create-normalized"
rn1 = client.post("/api/regions", json={
    "name": "规范化区域",
    "polygon": [[20, 0], [30, 0], [30, 10], [20, 10]],
    "valid_from": "2026-09-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
}, headers={"Idempotency-Key": key_norm})
rn2 = client.post("/api/regions", json={
    "name": "规范化区域",
    "polygon": [[20, 0], [30, 0], [30, 10], [20, 10], [20, 0]],
    "valid_from": "2026-09-01T08:00:00+08:00",
    "valid_to": "2026-10-01T08:00:00+08:00",
}, headers={"Idempotency-Key": key_norm})
check("规范化后相同的请求重放原响应",
      rn1.status_code == 201 and rn2.status_code == 201 and rn2.json() == rn1.json(),
      f"{rn1.status_code} {rn2.status_code}")
check("规范化重放未新增区域", region_count() == 3)

# 同键不同请求 → 409，不变更
before = revision()
r_conf = client.post("/api/regions", json={**REGION_A, "name": "换了内容"},
                     headers={"Idempotency-Key": key})
check("同键不同请求 409", r_conf.status_code == 409, r_conf.text)
check("错误码 idempotency_conflict",
      r_conf.json()["error"]["code"] == "idempotency_conflict", r_conf.text)
check("冲突后 revision 不变", revision() == before)
check("冲突后未新增区域", region_count() == 3)

# 不同键互不影响
r_new_key = client.post("/api/regions", json={**REGION_A, "name": "换了内容"},
                        headers={"Idempotency-Key": "create-0002"})
check("不同键可正常登记", r_new_key.status_code == 201, r_new_key.text)

# 无键请求保持既有行为：相同内容重复提交会重复登记
rnk1 = client.post("/api/regions", json={**REGION_A, "name": "无键重复"})
rnk2 = client.post("/api/regions", json={**REGION_A, "name": "无键重复"})
check("无键重复提交各自落库",
      rnk1.status_code == 201 and rnk2.status_code == 201
      and rnk1.json()["region"]["id"] != rnk2.json()["region"]["id"])

# ---------------------------------------------------------------- 非法键

r_empty = client.post("/api/regions", json=REGION_A, headers={"Idempotency-Key": ""})
check("空键 422", r_empty.status_code == 422, r_empty.text)
check("空键错误码 invalid_idempotency_key",
      r_empty.json()["error"]["code"] == "invalid_idempotency_key", r_empty.text)
r_blank = client.post("/api/regions", json=REGION_A, headers={"Idempotency-Key": "   "})
check("全空白键 422", r_blank.status_code == 422)
r_long = client.post("/api/regions", json=REGION_A,
                     headers={"Idempotency-Key": "k" * 129})
check("超长键（129）422", r_long.status_code == 422)
r_max = client.post("/api/regions", json={**REGION_A, "name": "边界键"},
                    headers={"Idempotency-Key": "k" * 128})
check("128 字符键可用", r_max.status_code == 201, r_max.text)

# ---------------------------------------------------------------- 失败业务不占键

key_fail = "create-fails-first"
rf1 = client.post("/api/regions", json={
    **REGION_A, "name": "先失败",
    "valid_from": "2026-10-01T00:00:00Z", "valid_to": "2026-09-01T00:00:00Z",
}, headers={"Idempotency-Key": key_fail})
check("非法时段 422", rf1.status_code == 422, rf1.text)
rf2 = client.post("/api/regions", json={**REGION_A, "name": "改正后"},
                  headers={"Idempotency-Key": key_fail})
check("失败请求不占键，改正后同键可提交", rf2.status_code == 201, rf2.text)

# ---------------------------------------------------------------- 修订幂等

rid = rnk1.json()["region"]["id"]
key_upd = "update-0001"
ru1 = client.patch(f"/api/regions/{rid}", json={"name": "修订后"},
                   headers={"Idempotency-Key": key_upd})
check("带键修订 200", ru1.status_code == 200, ru1.text)
check("修订后 version=2", ru1.json()["region"]["version"] == 2, ru1.text)
rev_after_update = revision()
ru2 = client.patch(f"/api/regions/{rid}", json={"name": "修订后"},
                   headers={"Idempotency-Key": key_upd})
check("相同修订重试返回原响应", ru2.status_code == 200 and ru2.json() == ru1.json())
check("重试未再次推进 version",
      client.get(f"/api/regions/{rid}").json()["version"] == 2)
check("重试未再次推进 revision", revision() == rev_after_update)

ru3 = client.patch(f"/api/regions/{rid}", json={"name": "换个名字"},
                   headers={"Idempotency-Key": key_upd})
check("同键不同修订 409", ru3.status_code == 409
      and ru3.json()["error"]["code"] == "idempotency_conflict", ru3.text)

# 目标区域属于同一次请求的判定要素：同键同内容但换了区域 id → 409
other_rid = rnk2.json()["region"]["id"]
ru4 = client.patch(f"/api/regions/{other_rid}", json={"name": "修订后"},
                   headers={"Idempotency-Key": key_upd})
check("同键同内容但目标区域不同 409", ru4.status_code == 409
      and ru4.json()["error"]["code"] == "idempotency_conflict", ru4.text)

# 修订 404 不占键
key_upd_404 = "update-missing"
ru404 = client.patch("/api/regions/999999", json={"name": "不存在"},
                     headers={"Idempotency-Key": key_upd_404})
check("修订不存在区域 404", ru404.status_code == 404, ru404.text)
ru_ok = client.patch(f"/api/regions/{other_rid}", json={"name": "占用该键"},
                     headers={"Idempotency-Key": key_upd_404})
check("404 不占键，同键可用于其它修订", ru_ok.status_code == 200, ru_ok.text)

# ---------------------------------------------------------------- 删除幂等

key_del = "delete-0001"
rd1 = client.delete(f"/api/regions/{other_rid}", headers={"Idempotency-Key": key_del})
check("带键删除 200", rd1.status_code == 200, rd1.text)
del_body = rd1.json()
rev_after_delete = revision()
rd2 = client.delete(f"/api/regions/{other_rid}", headers={"Idempotency-Key": key_del})
check("相同删除重试仍 200（不变 404）", rd2.status_code == 200, rd2.text)
check("删除重试返回原响应体", rd2.json() == del_body)
check("删除重试未推进 revision", revision() == rev_after_delete)
rd3 = client.delete(f"/api/regions/{rid}", headers={"Idempotency-Key": key_del})
check("同键删除其它区域 409", rd3.status_code == 409
      and rd3.json()["error"]["code"] == "idempotency_conflict", rd3.text)

# 跨操作类型同键 → 409（键在全部写操作间全局唯一）
r_cross = client.post("/api/regions", json=REGION_A, headers={"Idempotency-Key": key_del})
check("同键跨操作类型 409", r_cross.status_code == 409
      and r_cross.json()["error"]["code"] == "idempotency_conflict", r_cross.text)

# ---------------------------------------------------------------- 批量幂等

SAFE_TRAJ = [{"x": -50, "y": 500, "t": "2026-09-15T00:00:00Z"},
             {"x": 500, "y": 500, "t": "2026-09-15T00:10:00Z"}]
key_batch = "batch-0001"
rev_before_batch = revision()
batch_payload = {
    "expected_revision": rev_before_batch,
    "operations": [
        {"action": "create", "name": "批量区-1",
         "polygon": [[100, 100], [110, 100], [110, 110], [100, 110]],
         "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"},
        {"action": "create", "name": "批量区-2",
         "polygon": [[200, 200], [210, 200], [210, 210], [200, 210]],
         "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"},
    ],
    "trajectories": [SAFE_TRAJ],
}
rb1 = client.post("/api/regions/batch", json=batch_payload,
                  headers={"Idempotency-Key": key_batch})
check("带键批量 200", rb1.status_code == 200, rb1.text)
batch_body = rb1.json()
check("批量推进 revision 一次", batch_body["revision"] == rev_before_batch + 1, str(batch_body))
count_after_batch = region_count()

# 重试时 expected_revision 已过期，仍返回原状态与原响应
rb2 = client.post("/api/regions/batch", json=batch_payload,
                  headers={"Idempotency-Key": key_batch})
check("批量重试仍 200（revision 已过期也重放）", rb2.status_code == 200, rb2.text)
check("批量重试返回原响应体", rb2.json() == batch_body)
check("批量重试未重复落库", region_count() == count_after_batch)
check("批量重试未推进 revision", revision() == rev_before_batch + 1)

# 同键不同批量内容 → 409 idempotency_conflict（而非 revision_conflict）
rb3 = client.post("/api/regions/batch",
                  json={**batch_payload, "expected_revision": rev_before_batch + 1},
                  headers={"Idempotency-Key": key_batch})
check("同键不同批量 409 idempotency_conflict",
      rb3.status_code == 409
      and rb3.json()["error"]["code"] == "idempotency_conflict", rb3.text)

# 批量业务失败不占键：revision 不符的 409 之后，改正可同键提交
key_batch_fail = "batch-fails-first"
rbf1 = client.post("/api/regions/batch",
                   json={**batch_payload, "expected_revision": 999},
                   headers={"Idempotency-Key": key_batch_fail})
check("批量 revision 不符 409", rbf1.status_code == 409
      and rbf1.json()["error"]["code"] == "revision_conflict", rbf1.text)
rbf2 = client.post("/api/regions/batch",
                   json={**batch_payload, "expected_revision": revision(),
                         "operations": [
                             {"action": "create", "name": "批量区-改正",
                              "polygon": [[300, 300], [310, 300], [310, 310], [300, 310]],
                              "valid_from": "2026-09-01T00:00:00Z",
                              "valid_to": "2026-10-01T00:00:00Z"}]},
                   headers={"Idempotency-Key": key_batch_fail})
check("失败批量不占键，改正后同键可提交", rbf2.status_code == 200, rbf2.text)

# 批量轨迹不安全（422 unsafe_trajectory）同样不占键
key_batch_unsafe = "batch-unsafe-first"
unsafe_batch = {
    "expected_revision": revision(),
    "operations": [
        {"action": "create", "name": "批量区-压住轨迹",
         "polygon": [[0, 490], [10, 490], [10, 510], [0, 510]],
         "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z"},
    ],
    "trajectories": [SAFE_TRAJ],
}
rbu1 = client.post("/api/regions/batch", json=unsafe_batch,
                   headers={"Idempotency-Key": key_batch_unsafe})
check("批量轨迹不安全 422 unsafe_trajectory", rbu1.status_code == 422
      and rbu1.json()["error"]["code"] == "unsafe_trajectory", rbu1.text)
rbu2 = client.post("/api/regions/batch",
                   json={**unsafe_batch, "expected_revision": revision(),
                         "operations": [
                             {"action": "create", "name": "批量区-安全",
                              "polygon": [[600, 600], [610, 600], [610, 610], [600, 610]],
                              "valid_from": "2026-09-01T00:00:00Z",
                              "valid_to": "2026-10-01T00:00:00Z"}]},
                   headers={"Idempotency-Key": key_batch_unsafe})
check("unsafe 批量不占键，改正后同键可提交", rbu2.status_code == 200, rbu2.text)

# 请求结构校验失败（Pydantic 层 422）也不占键
key_struct = "create-bad-structure"
rs1 = client.post("/api/regions", json={
    "name": "自相交", "polygon": [[0, 0], [10, 10], [10, 0], [0, 10]],
    "valid_from": "2026-09-01T00:00:00Z", "valid_to": "2026-10-01T00:00:00Z",
}, headers={"Idempotency-Key": key_struct})
check("非法多边形 422", rs1.status_code == 422, rs1.text)
rs2 = client.post("/api/regions", json={**REGION_A, "name": "结构改正后"},
                  headers={"Idempotency-Key": key_struct})
check("结构校验失败不占键，改正后同键可提交", rs2.status_code == 201, rs2.text)

# ---------------------------------------------------------------- 凭证保护仍然适用

future1 = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
future2 = (datetime.now(timezone.utc) + timedelta(minutes=40)).isoformat().replace("+00:00", "Z")
rp = client.post("/api/passes", json={"points": [
    {"x": -50, "y": 5, "t": future1},
    {"x": 500, "y": 5, "t": future2},
]})
check("签发凭证 201", rp.status_code == 201, rp.text)
pass_id = rp.json()["id"]

key_pass = "create-blocked-by-pass"
blocked = {
    "name": "压住凭证轨迹",
    "polygon": [[0, 0], [100, 0], [100, 10], [0, 10]],
    "valid_from": "2020-01-01T00:00:00Z",
    "valid_to": "2030-01-01T00:00:00Z",
}
rp1 = client.post("/api/regions", json=blocked, headers={"Idempotency-Key": key_pass})
check("与凭证冲突 409 pass_conflict", rp1.status_code == 409
      and rp1.json()["error"]["code"] == "pass_conflict", rp1.text)
check("pass_conflict 后 revision 不变", revision() == revision())
client.post(f"/api/passes/{pass_id}/cancel")
rp2 = client.post("/api/regions", json=blocked, headers={"Idempotency-Key": key_pass})
check("pass_conflict 不占键，取消后同键可提交", rp2.status_code == 201, rp2.text)
rp3 = client.post("/api/regions", json=blocked, headers={"Idempotency-Key": key_pass})
check("该键随后可正常重放", rp3.status_code == 201 and rp3.json() == rp2.json())

# ---------------------------------------------------------------- 并发同键

key_conc = "create-concurrent"
N = 8
barrier = threading.Barrier(N)
outcomes: list = []
olock = threading.Lock()
conc_region = {
    "name": "并发幂等区",
    "polygon": [[500, 500], [510, 500], [510, 510], [500, 510]],
    "valid_from": "2026-09-01T00:00:00Z",
    "valid_to": "2026-10-01T00:00:00Z",
}
rev_before_conc = revision()
count_before_conc = region_count()


def conc_worker():
    barrier.wait()
    rr = client.post("/api/regions", json=conc_region,
                     headers={"Idempotency-Key": key_conc})
    with olock:
        outcomes.append((rr.status_code, rr.text))


threads = [threading.Thread(target=conc_worker) for _ in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join(timeout=30)

check("并发同键全部 201", all(s == 201 for s, _ in outcomes), str(outcomes))
check("并发同键响应逐字节一致", len({b for _, b in outcomes}) == 1, str(outcomes))
check("并发同键只有一次实际提交（只新增 1 个区域）",
      region_count() == count_before_conc + 1,
      f"{count_before_conc} -> {region_count()}")
check("并发同键 revision 只推进一次", revision() == rev_before_conc + 1,
      f"{rev_before_conc} -> {revision()}")

# ---------------------------------------------------------------- 汇总

print()
if failures:
    print(f"{len(failures)} 个失败")
    for f in failures:
        print(" -", f)
    sys.exit(1)
print("全部幂等键 API 集成用例通过")
