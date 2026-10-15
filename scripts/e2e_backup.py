"""端到端手工验证：真起一个服务，走一遍备份 → 改数据 → 恢复。

不进测试套件（会起真进程），只在开发时手动跑一次确认整条链路通。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(tempfile.mkdtemp())
AUTH = ROOT / "auth"
AUTH.mkdir()
(AUTH / "s1.info").write_text(json.dumps({
    "account": {"uid": "one", "nickname": "端到端账号", "enterpriseId": "test"},
    "auth": {"accessToken": "tok", "refreshToken": "ref",
             "expiresAt": int(time.time() * 1000) + 3600000}}), encoding="utf-8")

PORT = 8799
env = {**os.environ, "WORKBUDDY_DATA_DIR": str(ROOT / "management"),
       "WORKBUDDY_AUTH_DIR": str(AUTH), "WORKBUDDY_ADMIN_KEY": "admin-key-long-enough-for-test",
       "WORKBUDDY_API_KEY": "client-key", "WORKBUDDY_PORT": str(PORT),
       "WORKBUDDY_ENV_FILE": "/dev/null", "TZ": "Asia/Shanghai"}

proc = subprocess.Popen([sys.executable, "-m", "admin.server"], env=env,
                        cwd=Path(__file__).resolve().parent.parent,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)


# 管理会话 cookie 带 Secure 标志，纯 HTTP 下不会被回发（见 docker-factory 技能
# 的 pitfalls）。这里手工从 Set-Cookie 里取出会话值，每次请求显式带上。
SESSION = {"cookie": ""}


def call(path, method="GET", body=None, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method)
    req.add_header("Content-Type", "application/json")
    if SESSION["cookie"]:
        req.add_header("Cookie", SESSION["cookie"])
    if token:
        req.add_header("X-CSRF-Token", token)
    data = json.dumps(body).encode() if body is not None else None
    try:
        with urllib.request.urlopen(req, data, timeout=15) as resp:
            capture_cookie(resp)
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def capture_cookie(response):
    raw = response.headers.get("Set-Cookie", "")
    if raw.startswith("workbuddy_admin="):
        SESSION["cookie"] = raw.split(";", 1)[0]


try:
    # 等它起来
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2) as r:
                if json.loads(r.read())["status"] == "ok":
                    break
        except Exception:
            time.sleep(0.5)
    else:
        print("服务未起来"); sys.exit(1)
    print("✅ 服务已启动")

    status, data = call("/admin/api/login", "POST", {"key": "admin-key-long-enough-for-test"})
    csrf = data["csrf"]
    print("✅ 已登录")

    # 加一个账号，好在恢复前后区分数据
    status, data = call("/admin/api/accounts", "POST", {"credential": json.loads(
        (AUTH / "s1.info").read_text()), "name": "标记账号"}, csrf)
    print("✅ 已添加账号:", status)

    status, before = call("/admin/api/backups")
    print(f"✅ 备份列表（初始）: {before['count']} 份 · {before['schedule']}")

    status, created = call("/admin/api/backups", "POST", {}, csrf)
    name = created["name"]
    print(f"✅ 手动备份成功: {name} ({created['items'][0]['size_text']}, {created['items'][0]['created_text']})")

    # 改数据：删掉账号
    status, overview = call("/admin/api/overview")
    aid = overview["accounts"][0]["id"]
    call(f"/admin/api/accounts/{aid}", "DELETE", None, csrf)
    status, overview = call("/admin/api/overview")
    print(f"✅ 恢复前账号数: {len(overview['accounts'])}")

    # 恢复
    status, restored = call(f"/admin/api/backups/{name}/restore", "POST", {}, csrf)
    print(f"✅ 恢复请求: {status} {restored.get('message')} · 恢复前已自保为 {restored.get('safety')}")

    # 等服务重启回来。注意 os._exit 会丢掉内存里的会话，旧 cookie 失效，
    # 必须清掉重新登录。
    #
    # 线上靠容器 restart: unless-stopped 拉起，这个脚本没有守护进程，所以自己
    # 做同样的事——**先杀掉旧进程再启新的**。不杀的话旧进程从 stdout 管道写日志
    # 会被写满阻塞，poll() 一直不为 None 之外的状态，等待逻辑就卡住了。
    SESSION["cookie"] = ""
    proc.kill()
    proc.communicate()
    print("✅ 旧进程已停止，重新拉起（线上由容器 restart 策略接管）")
    proc = subprocess.Popen([sys.executable, "-m", "admin.server"], env=env,
                            cwd=Path(__file__).resolve().parent.parent,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    for _ in range(120):
        try:
            status, data = call("/admin/api/login", "POST", {"key": "admin-key-long-enough-for-test"})
        except Exception:
            status = None  # 新进程还没接管端口
        if status == 200:
            break
        time.sleep(0.5)
    else:
        print("❌ 恢复后服务没起来"); sys.exit(1)

    print("✅ 服务已重启并登录成功")
    csrf = data["csrf"]
    status, after = call("/admin/api/overview")
    print(f"✅ 恢复后账号数: {len(after['accounts'])} → {[a['name'] for a in after['accounts']]}")
    status, listed = call("/admin/api/backups")
    print(f"✅ 备份列表: {listed['count']} 份")
    for item in listed["items"]:
        print(f"   - {item['name']}  {item['size_text']}  {item['age_text']}{'  [最近一次]' if item['is_last'] else ''}")
    print("✅ 端到端通过" if after["accounts"] else "❌ 数据没恢复回来")
finally:
    proc.terminate()
    try:
        out = proc.communicate(timeout=10)[0]
    except subprocess.TimeoutExpired:
        proc.kill(); out = ""
    print("\n--- 服务日志尾部 ---")
    print("\n".join(out.splitlines()[-12:]))
