#!/usr/bin/env python3
"""Pull WHOOP data and write it into dashboard.html, so reloading the page shows the latest numbers.

  python3 whoop.py              pull now (last 14 days, plus a full re-pull once a day)
  python3 whoop.py --full       force a full re-pull
  python3 whoop.py --install    auto-pull every 15 min in the background (macOS launchd); --install 10 for every 10 min
  python3 whoop.py --uninstall  stop the background pulls
  python3 whoop.py --status     show the background job and the last pull
  python3 whoop.py --auth       sign in to WHOOP again (only if the refresh token has died)

Nothing listens on a port: launchd starts this script on a timer, it writes the files and exits.
It writes whoop_data.json, the data block inside dashboard.html, and whoop_live.js (which an open
dashboard tab re-reads every minute, so it updates without a reload).
"""
import fcntl, json, os, plistlib, re, secrets, shutil, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

REDIRECT      = "https://localhost:8080/callback"
SCOPES        = "offline read:recovery read:cycles read:sleep read:workout read:profile read:body_measurement"

AUTH_URL  = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
BASE      = "https://api.prod.whoop.com/developer"

HERE      = os.path.dirname(os.path.abspath(__file__))
ENV       = os.path.join(HERE, ".env")  # client id/secret and the current token pair; never commit it
DATA      = os.path.join(HERE, "whoop_data.json")
DASH      = os.path.join(HERE, "dashboard.html")
LIVE      = os.path.join(HERE, "whoop_live.js")
LOCK      = os.path.join(HERE, ".whoop.lock")
LOG       = os.path.join(HERE, "sync.log")
LABEL     = "com.whoopdash.sync"
PLIST     = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")

WINDOW_DAYS = 14          # incremental pulls re-read this many days, so re-scored records get picked up
FULL_EVERY_H = 24         # a full pull also drops records you deleted in the WHOOP app
UA = {"User-Agent": "whoop-dashboard/2.0"}
COLLECTIONS = {"recovery": "/v2/recovery", "cycles": "/v2/cycle",
               "sleep": "/v2/activity/sleep", "workouts": "/v2/activity/workout"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def write_atomic(path, text, mode=None):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)  # readers (the browser, another run) never see a half-written file


def log(msg):
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)


def trim_log(max_lines=400):
    # Truncate in place: launchd holds this file open in append mode, so it must stay the same file.
    try:
        with open(LOG, "r+") as f:
            lines = f.readlines()
            if len(lines) > max_lines:
                f.seek(0); f.writelines(lines[-max_lines:]); f.truncate()
    except FileNotFoundError:
        pass


# ---------- secrets (.env) ----------
def read_env():
    env = {}
    if os.path.exists(ENV):
        for line in open(ENV):
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#"):
                env[k.strip()] = v.strip().strip("\"'")
    return env


def write_env(updates):
    """Set these keys in .env, keeping every other line as it was."""
    lines, seen = [], set()
    if os.path.exists(ENV):
        for line in open(ENV):
            k = line.partition("=")[0].strip()
            if k in updates:
                line = f"{k}={updates[k]}"
                seen.add(k)
            lines.append(line.rstrip("\n") + "\n")
    lines += [f"{k}={v}\n" for k, v in updates.items() if k not in seen]
    write_atomic(ENV, "".join(lines), 0o600)


def setting(name):
    v = os.environ.get(name) or read_env().get(name)
    if not v:
        raise SystemExit(f"{name} is missing. Add it to {ENV} (see .env.example).")
    return v


# ---------- auth ----------
def post_token(data):
    req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(data).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded", **UA})
    return json.load(urllib.request.urlopen(req, timeout=30))


def save_tokens(t):
    t["expires_at"] = time.time() + t["expires_in"]
    # write BEFORE using: each refresh kills the old pair, so a lost write locks you out
    write_env({"WHOOP_ACCESS_TOKEN": t["access_token"], "WHOOP_REFRESH_TOKEN": t["refresh_token"],
               "WHOOP_TOKEN_EXPIRES_AT": f"{t['expires_at']:.0f}"})
    return t


def tokens(force=False):
    env = read_env()
    if not env.get("WHOOP_REFRESH_TOKEN"):
        raise SystemExit("No WHOOP tokens in .env yet. Run: python3 whoop.py --auth")
    if not force and float(env.get("WHOOP_TOKEN_EXPIRES_AT") or 0) > time.time() + 120:
        return {"access_token": env["WHOOP_ACCESS_TOKEN"]}
    return save_tokens(post_token({"grant_type": "refresh_token", "refresh_token": env["WHOOP_REFRESH_TOKEN"],
                                   "client_id": setting("WHOOP_CLIENT_ID"),
                                   "client_secret": setting("WHOOP_CLIENT_SECRET"), "scope": "offline"}))


def authorize():
    state = secrets.token_hex(8)
    url = AUTH_URL + "?" + urllib.parse.urlencode({"response_type": "code", "client_id": setting("WHOOP_CLIENT_ID"),
                                                    "redirect_uri": REDIRECT, "scope": SCOPES, "state": state})
    print("1. Open this link and sign in to WHOOP:\n\n   " + url + "\n")
    print("2. You'll land on a localhost page that fails to load. That's expected.")
    got = input("3. Paste the full address from the browser bar here: ").strip()
    q = urllib.parse.parse_qs(urllib.parse.urlparse(got).query)
    code = q.get("code", [got])[0]
    if q.get("state") and q["state"][0] != state:
        raise SystemExit("State didn't match. Start again with: python3 whoop.py --auth")
    with locked():
        save_tokens(post_token({"grant_type": "authorization_code", "code": code,
                                "client_id": setting("WHOOP_CLIENT_ID"),
                                "client_secret": setting("WHOOP_CLIENT_SECRET"), "redirect_uri": REDIRECT}))
    print("Signed in. Pulling data…")


# ---------- API ----------
class Api:
    def __init__(self):
        self.at = tokens()["access_token"]
        self.calls = 0

    def get(self, path, retried=False):
        for attempt in range(4):
            req = urllib.request.Request(BASE + path, headers={"Authorization": "Bearer " + self.at, **UA})
            try:
                self.calls += 1
                return json.load(urllib.request.urlopen(req, timeout=30))
            except urllib.error.HTTPError as e:
                if e.code == 401 and not retried:
                    self.at = tokens(force=True)["access_token"]
                    return self.get(path, retried=True)
                if e.code == 429 or e.code >= 500:
                    time.sleep(min(60, int(e.headers.get("Retry-After") or 0) or 5 * (attempt + 1)))
                    continue
                raise
        raise RuntimeError(f"WHOOP kept refusing {path.split('?')[0]} (rate limit or outage)")

    def page(self, path, start=None, cap=3000):
        out, tok = [], None
        params = {"limit": 25}
        if start:
            params["start"] = start
        while True:
            q = dict(params, **({"nextToken": tok} if tok else {}))
            d = self.get(path + "?" + urllib.parse.urlencode(q))
            out += d.get("records", [])
            tok = d.get("next_token")
            if not tok or len(out) >= cap:
                return out
            time.sleep(0.2)


def key(kind, r):
    return r.get("cycle_id") if kind == "recovery" else r.get("id")


def sort_key(r):
    return r.get("start") or r.get("created_at") or ""


def merge(old, new, kind):
    m = {key(kind, r): r for r in old}
    m.update({key(kind, r): r for r in new})
    return sorted(m.values(), key=sort_key, reverse=True)


# ---------- outputs ----------
def schedule_minutes():
    try:
        return int(plistlib.load(open(PLIST, "rb")).get("StartInterval", 0)) // 60 or None
    except Exception:
        return None


def publish(data):
    """Write whoop_data.json, the data block inside dashboard.html, and whoop_live.js."""
    write_atomic(DATA, json.dumps(data, indent=2))
    page = json.loads(json.dumps(data))
    page.get("profile", {}).pop("email", None)  # the dashboard never shows it; keep it out of the page
    blob = json.dumps(page, separators=(",", ":")).replace("</", "<\\/")
    if os.path.exists(DASH):
        html = open(DASH).read()
        tag = re.compile(r'(<script type="application/json" id="whoop-data">)(.*?)(</script>)', re.S)
        if tag.search(html):
            write_atomic(DASH, tag.sub(lambda m: m.group(1) + blob + m.group(3), html, count=1))
        else:
            log("warning: whoop-data block not found in dashboard.html, page not updated")
    write_atomic(LIVE, "/* written by whoop.py, read by dashboard.html */\n"
                       "window.whoopLive && window.whoopLive(" + blob + ");\n")


def sync(full=False):
    old = json.load(open(DATA)) if os.path.exists(DATA) else {}
    meta = dict(old.get("meta") or {})
    last_full = meta.get("last_full")
    stale_full = not last_full or (datetime.now(timezone.utc) - datetime.fromisoformat(last_full.replace("Z", "+00:00"))) > timedelta(hours=FULL_EVERY_H)
    full = full or stale_full or not all(old.get(k) for k in ("cycles", "recovery"))
    t0 = time.time()
    try:
        api = Api()
        data = {"profile": api.get("/v2/user/profile/basic"), "body": api.get("/v2/user/measurement/body")}
        start = None if full else (datetime.now(timezone.utc) - timedelta(days=WINDOW_DAYS)).isoformat(timespec="seconds").replace("+00:00", "Z")
        for k, path in COLLECTIONS.items():
            got = api.page(path, start=start)
            data[k] = sorted(got, key=sort_key, reverse=True) if full else merge(old.get(k, []), got, k)
        data["meta"] = {"fetched_at": now_iso(), "checked_at": now_iso(), "status": "ok", "error": None,
                        "mode": "full" if full else "incremental", "last_full": now_iso() if full else last_full,
                        "schedule_min": schedule_minutes(), "api_calls": api.calls}
        publish(data)
        log(f"ok   {data['meta']['mode']:<11} {api.calls:>3} calls {time.time() - t0:4.1f}s  "
            + " ".join(f"{k}={len(data[k])}" for k in COLLECTIONS))
        return 0
    except BaseException as e:  # SystemExit from tokens() too: the page should still say what went wrong
        if isinstance(e, KeyboardInterrupt):
            raise
        msg = str(e) or type(e).__name__
        if isinstance(e, urllib.error.HTTPError):
            msg = f"HTTP {e.code} from WHOOP" + (" — the sign-in has expired, run: python3 whoop.py --auth" if e.code in (400, 401) else "")
        elif isinstance(e, urllib.error.URLError):
            msg = f"Couldn't reach WHOOP ({e.reason})"
        log("FAIL " + msg)
        if old:
            old["meta"] = {**meta, "checked_at": now_iso(), "status": "error", "error": msg, "schedule_min": schedule_minutes()}
            old["meta"].setdefault("fetched_at", None)
            publish(old)
        return 1


class locked:
    """One run at a time. Two runs refreshing the token at once would kill each other's refresh token."""
    def __enter__(self):
        self.f = open(LOCK, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self
    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN); self.f.close()


# ---------- background job ----------
def install(minutes):
    py = shutil.which("python3") or sys.executable  # the stable symlink survives Homebrew upgrades
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    plistlib.dump({"Label": LABEL, "ProgramArguments": [py, os.path.join(HERE, "whoop.py")],
                   "WorkingDirectory": HERE, "StartInterval": minutes * 60, "RunAtLoad": True,
                   "ProcessType": "Background", "StandardOutPath": LOG, "StandardErrorPath": LOG},
                  open(PLIST, "wb"))
    dom = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{dom}/{LABEL}"], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", dom, PLIST], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit("launchctl refused the job: " + (r.stderr or r.stdout).strip())
    print(f"Auto-pull is on: every {minutes} min, starting now. Log: {LOG}\nTurn it off with: python3 whoop.py --uninstall")


def uninstall():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    if os.path.exists(PLIST):
        os.remove(PLIST)
    print("Auto-pull is off. The dashboard keeps the last data it got.")


def status():
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"], capture_output=True, text=True)
    print(f"Background job: {'on, every %s min' % schedule_minutes() if r.returncode == 0 else 'off'}")
    if os.path.exists(DATA):
        m = json.load(open(DATA)).get("meta") or {}
        print(f"Last check: {m.get('checked_at')}  status: {m.get('status')}  last good pull: {m.get('fetched_at')}")
        if m.get("error"):
            print("Error: " + m["error"])
    if os.path.exists(LOG):
        print("Recent log:\n" + "".join(open(LOG).readlines()[-5:]))


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--install" in args:
        i = args.index("--install")
        install(max(5, int(args[i + 1])) if len(args) > i + 1 and args[i + 1].isdigit() else 15)
    elif "--uninstall" in args:
        uninstall()
    elif "--status" in args:
        status()
    else:
        if "--auth" in args:
            authorize()
        trim_log()
        with locked():
            sys.exit(sync(full="--full" in args or "--auth" in args))
