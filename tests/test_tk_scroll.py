import tkinter as tk

from whatnot_monitor import app as app_module
from whatnot_monitor.config import Settings


def test_overview_scrollbars_keep_position_after_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "APP_ROOT", tmp_path)
    settings = Settings(recordings_dir=str(tmp_path / "recordings"))
    monkeypatch.setattr(app_module, "load_settings", lambda: settings)
    root = tk.Tk()
    root.withdraw()
    app = app_module.MonitorApp(root)
    try:
        root.geometry("1120x720-32000-32000")
        root.deiconify()
        for index in range(60):
            app.store.create_session(
                session_id=f"show-{index}", room_id=f"room-{index}", name=f"Room {index}",
                url=f"https://www.whatnot.com/show/{index}", started_at=None,
                start_time_source="first_seen_estimate", first_seen_at="2026-11-11T10:00:00+00:00",
                keyword_phrases=("cards",), audience_due_at=None,
            )
        app._refresh()
        root.update()
        assert app.session_tree.winfo_manager() == "grid"
        app.session_tree.xview_moveto(0.45)
        app.session_tree.yview_moveto(0.45)
        root.update()
        before = (app.session_tree.xview()[0], app.session_tree.yview()[0])
        assert before[0] > 0 and before[1] > 0
        app._refresh()
        root.update()
        after = (app.session_tree.xview()[0], app.session_tree.yview()[0])
        assert abs(before[0] - after[0]) < 0.02
        assert abs(before[1] - after[1]) < 0.02
    finally:
        app.runtime._postprocess.shutdown(wait=True)
        app.store.close()
        root.destroy()
