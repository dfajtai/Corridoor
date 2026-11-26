"""Exercise the GUI code paths with a stand-in tkinter (no real widgets)."""
import sys, types
from pathlib import Path
from unittest.mock import MagicMock
class Var:
    def __init__(self, value=None): self.v = value
    def get(self): return self.v
    def set(self, v): self.v = v
class Frame:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n):
        if n.startswith(("grid","columnconfigure","rowconfigure","after")): return MagicMock()
        raise AttributeError(n)
tk = types.ModuleType("tkinter"); tk.StringVar = tk.BooleanVar = Var; tk.Variable = Var; tk.Tk = MagicMock
ttk = MagicMock(); ttk.Frame = Frame
tk.ttk = ttk; tk.filedialog = tk.messagebox = tk.scrolledtext = MagicMock()
sys.modules.update({"tkinter": tk})
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import corridoor_core as c, corridoor_gui as g
w = g.ConfigWindow(MagicMock(), c.Settings(profile_file="a", host="h"))
assert w.current_settings() == c.Settings(profile_file="a", host="h")
c.save_settings = lambda s: None
assert w.save() is not None
w.variables["use_ssh"].set(False); assert w.save() is None
w.refresh()
c.Session.run = lambda self, test_only, client="ssh": client == "hoptodesk"
r = g.RunWindow(MagicMock(), c.Settings(), "hoptodesk"); r.worker.join(); assert r.success
w = g.ConfigWindow(MagicMock(), c.Settings(profile_file="a", host="h"))
saved = []
c.save_settings = saved.append
c.Session.pipe_test = lambda self: {"pipe_vpn_config": False, "pipe_vpn_auth": True, "pipe_ssh": True}
w.pipe_test(); w.worker.join(); w.refresh()
assert saved[-1].pipe_vpn_auth and saved[-1].pipe_ssh and not saved[-1].pipe_vpn_config
c.Session.pipe_test = lambda self: None
n = len(saved); w.pipe_test(); w.worker.join(); w.refresh(); assert len(saved) == n + 1
print("GUI code paths OK")
