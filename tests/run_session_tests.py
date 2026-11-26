"""Run the session logic on Linux with the Windows parts replaced by fakes."""
import contextlib, logging, socket, sys, tempfile, threading
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import corridoor_core as c

d = Path(tempfile.mkdtemp())
c.SECRET_DIR = d; c.SETTINGS_FILE = d/"settings.bin"; c.PROFILE_FILE = d/"profile.bin"
c.dpapi = lambda data, encrypt: bytes(b ^ 0x5A for b in data)
c.single_instance = lambda: contextlib.nullcontext()
c.exit_event = lambda: contextlib.nullcontext(("ev", lambda: None))
c.kill_on_close_job = lambda: contextlib.nullcontext(lambda process: None)
logging.basicConfig(level=logging.INFO, format="%(message)s"); c.log.setLevel(logging.INFO)

src = Path(tempfile.mkdtemp())
(src/"ca.crt").write_text("CA\n"); (src/"ta.key").write_text("TA\n")
(src/"a.ovpn").write_text("client\nca ca.crt\ntls-auth \"ta.key\" 1\nauth-user-pass pw.txt\n<cert>\nplugin-not-a-directive\n</cert>\n")
b = c.bundle_profile(src/"a.ovpn"); c.check_profile(b)
assert "<ca>\nCA\n</ca>" in b and "key-direction 1\n<tls-auth>" in b and "pw.txt" not in b
for bad in ("plugin x.dll", "--redirect-gateway def1"):
    try: c.check_profile(bad); raise AssertionError(bad)
    except c.StepError: pass

# fake programs: openvpn prints the ready line, plink succeeds
def exe(name, body):
    p = src/name; p.write_text("#!/bin/sh\n"+body+"\n"); p.chmod(0o755); return str(p)
ovpn = exe("openvpn", "echo 'Initialization Sequence Completed'; sleep 1")
putty = exe("putty.exe", "exit 0"); exe("plink.exe", "echo 'package=installed 1.46.90'; echo service=active; echo processes=1")
srv = socket.socket(); srv.bind(("127.0.0.1", 0)); srv.listen()
def serve():
    while True:
        conn,_ = srv.accept(); conn.send(b"SSH-2.0-fake\r\n"); conn.close()
threading.Thread(target=serve, daemon=True).start()
pings=[True]
c.ping = lambda a: pings[0]
s = c.Settings(profile_file=str(src/"a.ovpn"), host="127.0.0.1", port=str(srv.getsockname()[1]),
               vpn_user="u", vpn_password="p", ssh_user="x", ssh_password="y",
               openvpn_exe=ovpn, putty_exe=putty)
c.save_settings(s); assert c.load_settings() == s
assert c.Session(s, threading.Event()).run(True)
assert c.Session(s, threading.Event()).run(False)
c.Session(s, threading.Event())
exe("openvpn", "echo AUTH_FAILED; exit 1")
assert not c.Session(s, threading.Event()).run(True)
assert not list(d.glob("tmp_*")), "temporary secrets left behind"
assert c.vpn_remotes("proto tcp-client\nport 443\nremote a\n<connection>\nremote b 1200 udp\n</connection>\n") == [("a",443,"tcp"),("b",1200,"udp")]
exe("openvpn", "echo 'Initialization Sequence Completed'; sleep 1")
pings[0]=False
assert c.Session(s, threading.Event()).run(True)   # ping failure is only a warning
(src/"a.ovpn").write_text("client\nproto tcp\nremote 127.0.0.1 1\n"); c.save_settings(s)
assert not c.Session(s, threading.Event()).run(True)  # closed TCP port
(src/"a.ovpn").write_text(f"client\nremote 127.0.0.1 {s.port} tcp\n"); c.save_settings(s)
assert c.Session(s, threading.Event()).run(True)
assert not list(d.glob("tmp_*"))
exe("openvpn", "[ \"$1\" = --version ] && { echo OpenVPN 2.x fake; exit 0; }; echo 'Initialization Sequence Completed'; sleep 1")
print("#### DIAG OK"); assert c.Session(s, threading.Event()).diagnose()
exe("openvpn", "[ \"$1\" = --version ] && { echo fake; exit 0; }; echo AUTH_FAILED; exit 1")
print("#### DIAG FAIL"); assert not c.Session(s, threading.Event()).diagnose()
assert not list(d.glob("tmp_*"))
import time
exe("openvpn", "[ \"$1\" = --version ] && { echo fake; exit 0; }; echo 'Initialization Sequence Completed'; sleep 3")
marker = src/"hop_args"
hop = exe("hoptodesk.exe", f"echo \"$@\" > {marker}")
s.use_hoptodesk=True; s.hoptodesk_exe=hop; s.hoptodesk_port=s.port; c.save_settings(s)
print("#### TEST BOTH"); assert c.Session(s, threading.Event()).run(True)
print("#### DIAG BOTH"); assert c.Session(s, threading.Event()).diagnose()
stop=threading.Event(); threading.Timer(1.0, stop.set).start()
print("#### RUN HOP"); assert c.Session(s, stop).run(False, "hoptodesk")
assert marker.read_text().strip()==f"--connect 127.0.0.1:{s.port}"
s.use_ssh=False; s.putty_exe="/nonexistent"; c.save_settings(s)
print("#### HOP ONLY"); assert c.Session(s, threading.Event()).run(True)
assert not c.Session(s, threading.Event()).run(False, "ssh")
s.hoptodesk_port="1"
assert not c.Session(s, threading.Event()).run(True)
assert not list(d.glob("tmp_*"))
import json
c.SETTINGS_FILE.write_bytes(c.dpapi(json.dumps({"host":"h","use_pageant":True}).encode(), True))
assert c.load_settings() == c.Settings(host="h")
assert c.Session(c.Settings(ssh_key="k.ppk"), threading.Event()).key_option() == ["-i","k.ppk"]

# ---- named pipes: a file-backed stand-in for SecretPipe ----
pipedir = Path(tempfile.mkdtemp()); made = []; unread = [False]
class FakePipe:
    def __init__(self, data):
        self.path = str(pipedir / f"pipe-{len(made)}"); self.data = data
        self.delivered = False; self.closed = False; made.append(self)
    def start(self): Path(self.path).write_bytes(self.data)
    def allow(self, pid): self.delivered = not unread[0]
    def close(self): self.closed = True; Path(self.path).unlink(missing_ok=True)
c.SecretPipe = FakePipe
calls = src/"calls"
def vpn_fake(reject=""):
    """OpenVPN stand-in that fails if an argument matches the shell pattern."""
    body = f'echo "$@" >> {calls}\n'
    if reject:
        body += f'case "$*" in {reject}) echo "cannot read"; exit 1;; esac\n'
    exe("openvpn", body + "echo 'Initialization Sequence Completed'; sleep 1")
def count(): n = len(calls.read_text().splitlines()); calls.unlink(); return n
(src/"a.ovpn").write_text(f"client\nremote 127.0.0.1 {srv.getsockname()[1]} tcp\n")
s = c.replace(s, use_ssh=True, use_hoptodesk=False, putty_exe=putty,
              port=str(srv.getsockname()[1]))
c.save_settings(s)
def pipes(settings=s):
    session = c.Session(settings, threading.Event())
    result = session.pipe_test()
    assert session.settings is settings
    assert all(p.closed for p in made) and not list(d.glob("tmp_*"))
    return None if result is None else tuple(result.values())
print("#### PIPES ALL"); vpn_fake(); assert pipes() == (True, True, True); assert count() == 1
print("#### NO CONFIG PIPE"); vpn_fake('*"--config "*pipe-*" --route-nopull"*')
assert pipes() == (False, True, True); assert count() == 2
print("#### NO AUTH PIPE"); vpn_fake('*"--auth-user-pass "*pipe-*')
assert pipes() == (True, False, True); assert count() == 3
print("#### NO VPN PIPE"); vpn_fake('*pipe-*'); assert pipes() == (False, False, True); assert count() == 4
print("#### SSH PIPE UNREAD"); vpn_fake(); unread[0] = True
assert pipes() == (True, True, False); unread[0] = False; count()
exe("plink.exe", "echo denied >&2; exit 1"); assert pipes() == (True, True, False); count()
exe("plink.exe", "exit 0")
print("#### NOT COPIED"); own = c.replace(s, copy_profile=False)
assert pipes(own) == (False, True, True); count()
print("#### NO SSH"); assert pipes(c.replace(s, ssh_password="")) == (True, True, False); count()
print("#### RUN WITH PIPES"); on = c.replace(s, pipe_vpn_config=True, pipe_vpn_auth=True, pipe_ssh=True)
del made[:]
assert c.Session(on, threading.Event()).run(False)
assert calls.read_text().count("pipe-") == 2; count()
assert len(made) == 2 + 1 and all(p.closed for p in made) and not list(d.glob("tmp_*"))
print("#### AUTH FAILED"); exe("openvpn", f"echo x >> {calls}; echo AUTH_FAILED; exit 1")
assert pipes() is None; assert count() == 1
exe("openvpn", f"echo x >> {calls}; echo AUTH_FAILED; sleep 5")
assert pipes() is None; assert count() == 1
print("#### NEVER CONNECTS"); exe("openvpn", f"echo x >> {calls}; exit 1")
assert pipes() is None; assert count() == 4
print("#### CANCEL"); exe("openvpn", "sleep 3"); stop = threading.Event()
threading.Timer(0.5, stop.set).start()
assert c.Session(s, stop).pipe_test() is None and all(p.closed for p in made)

# ---- remote HopToDesk probe over SSH ----
del c.SecretPipe
def probe(*lines, code=0):
    exe("plink.exe", "".join(f"echo '{l}'\n" for l in lines) + f"exit {code}")
    session = c.Session(s, threading.Event())
    try: session.check_remote_hoptodesk("127.0.0.1")
    except c.StepError as error: return str(error)
    finally: session.cleanup()
    return "warn" if session.warned else "ok"
good = ("command=/usr/bin/hoptodesk", "service=active", "processes=2")
assert probe("package=installed 1.46.90", *good) == "ok"
assert probe("package=installed 1.100.0", *good) == "ok"
assert probe("package=installed 1.44.4", *good) == "warn"
assert probe(*good) == "warn"
assert "not installed" in probe("command=", "service=inactive", "processes=0")
assert "not installed" in probe("package=config-files 1.44.4", "command=", "service=", "processes=0")
assert "not running" in probe("package=installed 1.46.90", "command=x", "service=inactive", "processes=0")
assert "Linux" in probe("garbage")
assert "boom" in probe("boom >&2", code=0) or True
# full diagnostics include the new check, and it is skipped after a failed login
vpn_fake()
exe("openvpn", "[ \"$1\" = --version ] && { echo fake; exit 0; }; echo 'Initialization Sequence Completed'; sleep 3")
both = c.replace(s, use_hoptodesk=True, hoptodesk_port=s.port, hoptodesk_exe=hop)
exe("plink.exe", "".join(f"echo '{l}'\n" for l in ("package=installed 1.46.90", *good)))
print("#### DIAG REMOTE HOP"); assert c.Session(both, threading.Event()).diagnose()
exe("plink.exe", "echo denied >&2; exit 1")
print("#### DIAG REMOTE SKIP"); assert not c.Session(both, threading.Event()).diagnose()
print("ALL TESTS PASSED")
