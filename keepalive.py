#!/usr/bin/env python3
"""Memo Cloud (app.memoh.net) nezha agent keepalive.

Runs on GitHub Actions (cron-job.org dispatches the workflow; GA native
schedule does not fire reliably on this account).

Flow:
  1. login (username/password) -> session cookie + active team id
  2. read bot container status (/api/memoh/bots/<id>/container)
  3. if container not running -> POST .../container/start
  4. open the container terminal WS, check `pgrep -f nezha/agent -s`
  5. if the agent is missing -> `setsid nohup bash /data/nezha/boot.sh`

Exit codes: 0 = healthy or repaired, 1 = something we could not fix.
Only prints a telegram-friendly summary; no noisy logging.
"""

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
import http.cookiejar

BASE_ROOT = "https://app.memoh.net"
ORIGIN = BASE_ROOT
API = BASE_ROOT + "/api/v1"

USER = os.environ.get("MEMOH_USER", "")
PW = os.environ.get("MEMOH_PASS", "")
BOT = os.environ.get("MEMOH_BOT_ID", "").strip()
TEAM = os.environ.get("MEMOH_TEAM_ID", "").strip()

# The container terminal WS echoes every typed command back, so grepping stdout
# for a marker matches the command text itself (false AGENT_UP). Every probe
# therefore writes its verdict to /tmp/nz.state and we read that file back.
CHECK = ("if pgrep -f '[n]ezha/agent -s' >/dev/null 2>&1; "
         "then echo UP > /tmp/nz.state; else echo DOWN > /tmp/nz.state; fi; "
         "echo DONE >> /tmp/nz.state")
READ = "cat /tmp/nz.state"
BOOT = ("setsid nohup bash /data/nezha/boot.sh "
        ">/data/nezha/boot.out 2>&1 < /dev/null &")


def agent_state():
    """Return 'UP', 'DOWN' or None (could not read the verdict file)."""
    asyncio.run(ws_exec(CHECK))
    out = asyncio.run(ws_exec(READ)).replace("\r", "")
    if "DONE" not in out:
        return None
    body = out.split("DONE", 1)[0]
    if "DOWN" in body:
        return "DOWN"
    if "UP" in body:
        return "UP"
    return None

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
TEAM_BOX = {"v": TEAM}


def _headers():
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": ORIGIN,
        "Referer": ORIGIN + "/",
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/141 Safari/537.36",
        "X-Team-ID": TEAM_BOX["v"],
        "X-Team-Id": TEAM_BOX["v"],
    }


def call(method, path, body=None, root=False):
    """path is absolute (starting with /). root=True -> app root, else /api/v1."""
    url = (BASE_ROOT if root else API) + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in _headers().items():
        req.add_header(k, v)
    try:
        with opener.open(req, timeout=90) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        return 0, "%s: %s" % (type(e).__name__, e)


def login():
    st, txt = call("POST", "/auth/login", {"username": USER, "password": PW})
    if st != 200:
        raise RuntimeError("login failed %s %s" % (st, txt[:300]))
    j = json.loads(txt)
    if not TEAM_BOX["v"]:
        TEAM_BOX["v"] = j["active_team"]["team_id"]
    return j


def container_state():
    st, txt = call("GET", "/api/memoh/bots/%s/container" % BOT, root=True)
    if st != 200:
        return st, txt[:300], {}
    try:
        return st, "", json.loads(txt)
    except Exception:  # noqa: BLE001
        return st, txt[:300], {}


async def ws_exec(cmd, wait_idle=4.0):
    import websockets

    cookie = "; ".join("%s=%s" % (c.name, c.value) for c in cj)
    url = ("wss://app.memoh.net/api/memoh/bots/%s/container/terminal/ws"
           "?cols=120&rows=40" % BOT)
    headers = {"Cookie": cookie, "Origin": ORIGIN, "X-Team-ID": TEAM_BOX["v"]}
    kw = dict(open_timeout=30, max_size=2 ** 22)
    try:
        conn = websockets.connect(url, additional_headers=headers, **kw)
    except TypeError:  # websockets >= 14 renamed the kwarg
        conn = websockets.connect(url, extra_headers=headers, **kw)
    async with conn as ws:
        out = b""
        try:
            out += await asyncio.wait_for(ws.recv(), timeout=6)
        except asyncio.TimeoutError:
            pass
        await ws.send(cmd.encode() + b"\n")
        idle = 0.0
        while idle < wait_idle:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=1.5)
                out += msg if isinstance(msg, bytes) else msg.encode()
                idle = 0.0
            except asyncio.TimeoutError:
                idle += 1.5
            except Exception:  # noqa: BLE001
                break
        return out.decode("utf-8", "replace")


def main():
    if not (USER and PW and BOT):
        print("::error::missing MEMOH_USER / MEMOH_PASS / MEMOH_BOT_ID")
        return 1

    login()
    st, err, info = container_state()
    state = info.get("status")
    print("container: http=%s status=%s task_running=%s" % (st, state, info.get("task_running")))

    actions = []
    if st != 200:
        print("::warning::container state unreadable: %s" % err)
    if state and state != "running":
        s2, t2 = call("POST", "/api/memoh/bots/%s/container/start" % BOT,
                      {}, root=True)
        print("container/start -> %s %s" % (s2, t2[:200]))
        actions.append("container-start:%s" % s2)
        for _ in range(20):
            time.sleep(6)
            _, _, info = container_state()
            if info.get("status") == "running":
                break
        print("container now: %s" % info.get("status"))

    state_agent = agent_state()
    print("agent check: %s" % state_agent)
    if state_agent != "UP":
        out2 = asyncio.run(ws_exec(BOOT))
        print("boot.sh: sent (%d bytes back)" % len(out2))
        actions.append("agent-restarted")
        time.sleep(25)
        state_agent = agent_state()
        print("agent after restart: %s" % state_agent)
        if state_agent != "UP":
            return 1

    if not actions:
        print("RESULT: healthy, nothing to do")
    else:
        print("RESULT: repaired -> %s" % ", ".join(actions))

    # fingerprint on stdout lets a shell-side watchdog / log grep see the run
    print("ts=%s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
