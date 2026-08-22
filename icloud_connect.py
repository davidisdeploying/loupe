#!/usr/bin/env python3
"""
icloud_connect.py — Connect Phase 2a: the iCloud auth handshake (sign-in + 2FA).

NO DOWNLOAD. This module ONLY establishes an authenticated icloudpd session and
persists its cookie; Phase 2b wires the actual pull.

Why a stdin-driven icloudpd subprocess (not an imported library)
----------------------------------------------------------------
The installed icloudpd (1.32.3) is a Nuitka-frozen binary with NO importable auth
API. Its only importable sibling on PyPI, pyicloud_ipd 0.10.2, predates modern
two-factor auth (hsaVersion==2) — its source carries a literal
`# FIXME: Implement 2FA for hsaVersion == 2`. So the ONLY code on this box that can
do a real modern-2FA handshake AND mint a cookie icloudpd itself will accept in 2b
is icloudpd's own `--auth-only` path. We drive it as a child process and feed the
password and the 6-digit code over STDIN — never as argv, so they never appear in
`/proc/<pid>/cmdline` or `ps`. This is the faithful realization of the spec's intent
("credentials never on argv; never logged; never on disk; only icloudpd's cookie
persists"); the literal "imported library" path isn't available for modern 2FA here.

Credential discipline (non-negotiable):
  * The password is received by start_auth(), written straight to the child's stdin,
    then dropped. It is NEVER stored in the pending-session, NEVER logged, NEVER
    written to disk by us. icloudpd reads it via getpass and does not echo it.
  * The 6-digit code is written to the live child's stdin and likewise never stored
    or logged.
  * Child stdout/stderr is read only to drive the state machine; we never persist it
    and never return raw child lines in HTTP bodies (fixed messages only).
  * Only icloudpd's session cookie persists, in a Loupe-controlled dir, files 0600.
"""

import json
import os
import queue
import re
import secrets
import subprocess
import sys
import threading
import time

# --- configuration (server.py calls configure() once) ----------------------
_CFG = {"COOKIE_DIR": None}
PENDING_TTL = 300.0          # a pending 2FA handshake lives at most 5 minutes
_START_DEADLINE = 60.0       # seconds to reach the 2FA prompt / a verdict in /start
_2FA_DEADLINE = 60.0         # seconds to reach a verdict after the code is submitted

# Markers parsed from icloudpd output (verified against icloudpd 1.32.3). We match
# on OUTCOMES, leniently, never on exact prompt wording.
_RE_BAD_PASSWORD = re.compile(r"invalid email/password", re.I)
_RE_2FA = re.compile(r"two[-\s]?factor|two[-\s]?step|\b2fa\b|verification code|authentication code", re.I)
_RE_BAD_CODE = re.compile(r"incorrect|wrong.*code|invalid.*code|failed to (?:verify|validate)|please try again", re.I)
# HTTP 421 (Misdirected Request) — iCloud's signal that web access / ADP is misconfigured.
_RE_421 = re.compile(r"\b421\b|misdirected", re.I)


def configure(cookie_dir):
    """Called once from server.py with the Loupe-controlled cookie directory."""
    _CFG["COOKIE_DIR"] = cookie_dir
    try:
        os.makedirs(cookie_dir, mode=0o700, exist_ok=True)
        os.chmod(cookie_dir, 0o700)
    except OSError as e:
        print(f"icloud_connect: could not prepare cookie dir: {e}", flush=True)


# --- pending-handshake registry (in-memory only) ---------------------------
_lock = threading.Lock()
_pending = {}                # token -> _Session


class _Session:
    __slots__ = ("token", "apple_id", "proc", "q", "reader", "created", "done")

    def __init__(self, token, apple_id, proc, q, reader):
        self.token = token
        self.apple_id = apple_id
        self.proc = proc
        self.q = q
        self.reader = reader
        self.created = time.time()
        self.done = False


def _sanitized(apple_id):
    """icloudpd's cookie filename stem: alphanumerics of the Apple ID."""
    return re.sub(r"[^0-9a-zA-Z]", "", apple_id or "")


def _harden_cookie_files():
    """All files in the cookie dir → 0600; dir → 0700. The cookiejar is already 0600
    from icloudpd, but the .session companion is not — lock it down too."""
    d = _CFG["COOKIE_DIR"]
    if not d or not os.path.isdir(d):
        return
    try:
        os.chmod(d, 0o700)
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    os.chmod(p, 0o600)
                except OSError:
                    pass
    except OSError:
        pass


def _session_authenticated(apple_id):
    """True iff the persisted .session shows a real trust/session token (present only
    after a successful sign-in; an init-only session has just client_id/scnt/session_id)."""
    d = _CFG["COOKIE_DIR"]
    if not d:
        return False
    stem = _sanitized(apple_id)
    path = os.path.join(d, stem + ".session")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return any(k in data for k in ("session_token", "trust_token", "account_country", "apple_id"))


def _spawn(apple_id):
    """Launch icloudpd --auth-only with password+code coming over STDIN (never argv)."""
    cookie_dir = _CFG["COOKIE_DIR"]
    cmd = [sys.executable, "-m", "icloudpd", "--auth-only",
           "--username", apple_id,                  # identifier, not a secret
           "--password-provider", "console",        # password read from OUR stdin
           "--mfa-provider", "console",             # 2FA code read from OUR stdin
           "--cookie-directory", cookie_dir,
           "--no-progress-bar"]
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
    q = queue.Queue()

    def _read():
        try:
            for line in iter(proc.stdout.readline, ""):
                q.put(line)
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            q.put(None)            # sentinel: stream closed

    reader = threading.Thread(target=_read, name="icloud-auth-read", daemon=True)
    reader.start()
    return proc, q, reader


def _drain_until(sess, deadline_s):
    """Consume child output until a terminal/2FA verdict or timeout. Returns one of:
    'twofa', 'bad_password', 'bad_code', 'needs_web_access', 'ok', 'error', 'timeout'.
    Never logs or returns child text."""
    end = time.time() + deadline_s
    saw_2fa = False
    while time.time() < end:
        # Process already exited? decide from what we know + the persisted session.
        if sess.proc.poll() is not None:
            # flush any remaining buffered lines quickly
            while True:
                try:
                    line = sess.q.get_nowait()
                except queue.Empty:
                    break
                if line is None:
                    break
                if _RE_BAD_PASSWORD.search(line):
                    return "bad_password"
                if _RE_421.search(line):
                    return "needs_web_access"
                if _RE_BAD_CODE.search(line):
                    return "bad_code"
            return "ok" if _session_authenticated(sess.apple_id) else "error"
        try:
            line = sess.q.get(timeout=0.4)
        except queue.Empty:
            continue
        if line is None:                  # stream closed; loop will catch poll() next
            continue
        if _RE_BAD_PASSWORD.search(line):
            return "bad_password"
        if _RE_421.search(line):
            return "needs_web_access"
        if _RE_BAD_CODE.search(line):
            return "bad_code"
        if _RE_2FA.search(line):
            saw_2fa = True
            return "twofa"
    # deadline hit
    if saw_2fa:
        return "twofa"
    return "timeout"


def _kill(sess):
    """Terminate the child and forget the pending session. Idempotent."""
    try:
        if sess.proc and sess.proc.poll() is None:
            try:
                sess.proc.stdin.close()
            except Exception:
                pass
            sess.proc.terminate()
            try:
                sess.proc.wait(timeout=3)
            except Exception:
                sess.proc.kill()
    except Exception:
        pass
    with _lock:
        _pending.pop(sess.token, None)
    sess.done = True


def start_auth(apple_id, password):
    """Begin the handshake. Writes the password to the child's stdin, then drops it.
    Returns a dict for the HTTP layer (no secrets). Possible states:
      requires_2fa -> {"state":"requires_2fa","token":...}
      authenticated -> {"state":"authenticated"}            (already-trusted session)
      bad_password / needs_web_access / error / timeout."""
    if not _CFG["COOKIE_DIR"]:
        return {"state": "error", "message": "Connect is not configured."}
    apple_id = (apple_id or "").strip()
    if not apple_id or not password:
        return {"state": "error", "message": "Apple ID and password are required."}

    proc, q, reader = _spawn(apple_id)
    token = secrets.token_urlsafe(32)
    sess = _Session(token, apple_id, proc, q, reader)

    # Hand the password to the child over stdin, then forget it immediately.
    try:
        proc.stdin.write(password + "\n")
        proc.stdin.flush()
    except Exception:
        _kill(sess)
        return {"state": "error", "message": "Could not start the sign-in."}
    finally:
        password = None            # drop our only reference; never stored/logged
        del password

    verdict = _drain_until(sess, _START_DEADLINE)

    if verdict == "twofa":
        with _lock:
            _pending[token] = sess         # keep the child alive, waiting on the code
        return {"state": "requires_2fa", "token": token}
    if verdict == "ok":
        _harden_cookie_files()
        _kill(sess)
        return {"state": "authenticated"}
    # any non-2FA, non-ok verdict ends the attempt now
    _kill(sess)
    return {"state": verdict}              # bad_password | needs_web_access | error | timeout


def submit_2fa(token, code):
    """Submit the 6-digit code to the waiting child. On success the cookie is hardened
    and the pending session wiped. Returns a dict (no secrets)."""
    code = (code or "").strip()
    with _lock:
        sess = _pending.get(token)
    if not sess:
        return {"state": "error", "message": "This sign-in expired — start again."}
    if not re.fullmatch(r"\d{6}", code):
        return {"state": "bad_code", "message": "Enter the 6-digit code."}
    if sess.proc.poll() is not None:       # child died/expired while we waited
        _kill(sess)
        return {"state": "error", "message": "This sign-in expired — start again."}

    try:
        sess.proc.stdin.write(code + "\n")
        sess.proc.stdin.flush()
    except Exception:
        _kill(sess)
        return {"state": "error", "message": "Could not submit the code — start again."}
    finally:
        code = None
        del code

    verdict = _drain_until(sess, _2FA_DEADLINE)
    if verdict == "ok":
        _harden_cookie_files()
        _kill(sess)
        return {"state": "authenticated"}
    if verdict in ("bad_code", "timeout"):
        # leave a timeout recoverable only by restarting; a bad code ends this attempt
        _kill(sess)
        return {"state": "bad_code" if verdict == "bad_code" else "timeout"}
    _kill(sess)
    return {"state": verdict if verdict in ("needs_web_access",) else "error"}


# --- TTL reaper: kill abandoned handshakes so passwords/processes never linger ---
def _reaper():
    while True:
        time.sleep(30)
        now = time.time()
        stale = []
        with _lock:
            for tok, s in list(_pending.items()):
                if now - s.created > PENDING_TTL or s.proc.poll() is not None:
                    stale.append(s)
        for s in stale:
            _kill(s)


_reaper_thread = threading.Thread(target=_reaper, name="icloud-auth-reaper", daemon=True)
_reaper_thread.start()
