import ast
import logging
import queue
import types
import unittest
from pathlib import Path

source = (Path(__file__).resolve().parents[1] / 'src' / 'whatnot_monitor' / 'app.py').read_text(encoding='utf-8')
module = ast.parse(source)
cls = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == 'MonitorApp')
method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == 'close')
fragment = ast.Module(body=[method], type_ignores=[])


class Dialog:
    def __init__(self, root): self.root = root
    def title(self, *_): pass
    def transient(self, *_): pass
    def protocol(self, *_): pass
    def grab_set(self): pass
    def grab_release(self): pass
    def destroy(self): pass


class Label:
    def __init__(self, *args, **kwargs): pass
    def pack(self): pass


class Root:
    def __init__(self): self.pending = []; self.destroyed = False
    def after(self, _delay, function): self.pending.append(function)
    def destroy(self): self.destroyed = True
    def run_next(self): self.pending.pop(0)()


class DeferredThread:
    latest = None
    def __init__(self, *, target, **kwargs):
        self.target = target
        DeferredThread.latest = self
    def start(self): pass


class ShutdownTests(unittest.TestCase):
    def setUp(self):
        self.errors = []
        self.notices = []
        env = {'queue': queue, 'logging': logging, 'tk': types.SimpleNamespace(Toplevel=Dialog),
               'ttk': types.SimpleNamespace(Label=Label),
               'threading': types.SimpleNamespace(Thread=DeferredThread),
               'messagebox': types.SimpleNamespace(askyesno=lambda *_: True, showerror=lambda *v: self.errors.append(v), showwarning=lambda *v: self.notices.append(v))}
        exec(compile(ast.fix_missing_locations(fragment), 'app.py', 'exec'), env)  # noqa: S102
        self.close = env['close']
        self.order = []
        self.root = Root()
        self.app = types.SimpleNamespace(root=self.root,
            runtime=types.SimpleNamespace(running=True, shutdown=lambda: self.order.append('shutdown')),
            store=types.SimpleNamespace(close=lambda: self.order.append('store close')))

    def test_ui_waits_for_cleanup_and_does_not_close_database_early(self):
        self.close(self.app)
        self.root.run_next()
        self.assertEqual(self.order, [])
        self.assertFalse(self.root.destroyed)
        DeferredThread.latest.target()
        self.root.run_next()
        self.assertEqual(self.order, ['shutdown', 'store close'])
        self.assertTrue(self.root.destroyed)

    def test_shutdown_failure_keeps_database_and_window_open(self):
        def fail(): raise RuntimeError('test failure')
        self.app.runtime.shutdown = fail
        self.close(self.app)
        DeferredThread.latest.target()
        self.root.run_next()
        self.assertEqual(self.order, [])
        self.assertFalse(self.root.destroyed)
        self.assertTrue(self.errors)

    def test_pending_alert_loss_is_shown_before_window_closes(self):
        self.app.runtime._file_watchdog = types.SimpleNamespace(shutdown_loss=True)
        self.close(self.app)
        DeferredThread.latest.target()
        self.root.run_next()
        self.assertTrue(self.notices)
        self.assertIn('可能未写入数据库', self.notices[0][1])
        self.assertTrue(self.root.destroyed)

    def test_repeated_close_does_not_spawn_second_worker(self):
        self.close(self.app)
        first = DeferredThread.latest
        self.close(self.app)
        self.assertIs(DeferredThread.latest, first)


if __name__ == '__main__': unittest.main()
