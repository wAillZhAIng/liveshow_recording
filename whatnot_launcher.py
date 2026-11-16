import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

LAUNCHER = Path(__file__).resolve()
PROJECT = LAUNCHER.parent
ENVIRONMENT = PROJECT / ".venv"
WINDOWS = os.name == "nt"
VENV_PYTHON = ENVIRONMENT / ("Scripts/python.exe" if WINDOWS else "bin/python")
PROJECT_ID = hashlib.sha256(os.fsencode(PROJECT)).hexdigest()[:12]
if not WINDOWS:
    instance = f"wn-{os.getuid()}-{PROJECT_ID}"
    STATE = Path(tempfile.gettempdir()) / instance
    # Some desktop sessions have a TMPDIR longer than a Unix socket allows.
    if len(os.fsencode(STATE / "stop.sock")) >= 104:
        STATE = Path("/tmp") / instance
    CONTROL = STATE / "stop.sock"


def prepare_environment():
    if not (PROJECT / "src/whatnot_monitor/app.py").is_file():
        raise RuntimeError(f"找不到 Whatnot 项目目录：{PROJECT}")
    if not VENV_PYTHON.is_file():
        if ENVIRONMENT.exists() or ENVIRONMENT.is_symlink():
            raise RuntimeError(f"{ENVIRONMENT} 不完整，请修复或重命名后重试。")
        if sys.version_info[:2] not in ((3, 11), (3, 12)):
            raise RuntimeError("需要 Python 3.11 或 3.12。")
        subprocess.run([sys.executable, "-m", "venv", ENVIRONMENT], check=True)

    check = subprocess.run(
        [VENV_PYTHON, "-c",
         "import sys; assert sys.version_info[:2] in ((3, 11), (3, 12)); import tkinter"],
        cwd=PROJECT,
    )
    if check.returncode:
        raise RuntimeError("项目的 .venv 需要支持 Tkinter 的 Python 3.11 或 3.12。")

    ready = ENVIRONMENT / ".whatnot-ready"
    requirements = PROJECT / "requirements.txt"
    fingerprint = hashlib.sha256(requirements.read_bytes()).hexdigest()
    if not ready.is_file() or ready.read_text(encoding="utf-8").strip() != fingerprint:
        print("正在安装或更新 Python 依赖和 Chromium……", flush=True)
        subprocess.run(
            [VENV_PYTHON, "-m", "pip", "install", "-r", requirements],
            cwd=PROJECT,
            check=True,
        )
        subprocess.run(
            [VENV_PYTHON, "-m", "playwright", "install", "chromium"],
            cwd=PROJECT,
            check=True,
        )
        ready.write_text(fingerprint + "\n", encoding="utf-8")


def start():
    if WINDOWS:
        prepare_environment()
        subprocess.run([VENV_PYTHON, LAUNCHER, "window"], cwd=PROJECT, check=True)
        return

    import fcntl

    STATE.mkdir(mode=0o700, exist_ok=True)
    with (STATE / "instance.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Whatnot 已在运行或正在启动。")
            return

        CONTROL.unlink(missing_ok=True)
        prepare_environment()
        print("正在打开 Whatnot 直播监控……", flush=True)
        subprocess.run(
            [VENV_PYTHON, LAUNCHER, "window"],
            cwd=PROJECT,
            check=True,
            pass_fds=(lock.fileno(),),
        )


def run_window():
    sys.path.insert(0, str(PROJECT / "src"))
    from whatnot_monitor.app import MonitorApp, configure_logging, main, tk

    if WINDOWS:
        main()
        return

    configure_logging()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        listener.bind(str(CONTROL))
        try:
            listener.listen(4)
            listener.setblocking(False)

            root = tk.Tk()
            app = MonitorApp(root)

            def check_for_stop():
                try:
                    client, _ = listener.accept()
                except BlockingIOError:
                    pass
                else:
                    with client:
                        client.setblocking(False)
                        try:
                            client.sendall(b"1")
                        except OSError:
                            pass
                    root.after_idle(app.close)
                root.after(250, check_for_stop)

            root.after(250, check_for_stop)
            root.mainloop()
        finally:
            CONTROL.unlink(missing_ok=True)


def stop():
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        try:
            client.connect(str(CONTROL))
        except (FileNotFoundError, ConnectionRefusedError):
            print("没有找到通过此启动器打开且已就绪的 Whatnot 窗口。")
            return
        if client.recv(1) != b"1":
            raise RuntimeError("Whatnot 窗口关闭前未确认停止请求。")
    print("已请求安全退出。如果 Whatnot 弹出确认，请点击「是」，等待录制保存。")


if __name__ == "__main__":
    try:
        commands = {"start": start, "check": prepare_environment, "window": run_window}
        if not WINDOWS:
            commands["stop"] = stop
        if len(sys.argv) != 2 or sys.argv[1] not in commands:
            sys.exit("Usage: whatnot_launcher.py start|check" + ("" if WINDOWS else "|stop"))
        commands[sys.argv[1]]()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        sys.exit(f"Whatnot 操作失败：{error}")
