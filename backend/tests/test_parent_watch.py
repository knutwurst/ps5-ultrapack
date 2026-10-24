import os, subprocess, sys, tempfile, textwrap, time, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
BACKEND = HERE.parent


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # a zombie still answers kill(0); ps tells it apart
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")


@unittest.skipIf(os.name == "nt", "process groups are POSIX")
class ParentWatch(unittest.TestCase):
    def _run(self, own_group: bool):
        tmp = Path(tempfile.mkdtemp())
        pids = tmp / "pids"
        # the "backend": watches its parent, starts a long child, then waits
        backend = textwrap.dedent(f"""
            import subprocess, sys, time
            sys.path.insert(0, {str(BACKEND)!r})
            import cli
            cli._exit_with_parent(poll=0.1)
            child = subprocess.Popen(["sleep", "30"])
            open({str(pids)!r}, "w").write(f"{{__import__('os').getpid()}} {{child.pid}}")
            time.sleep(30)
        """)
        # the app: starts the backend in its own session or group, then leaves
        app = textwrap.dedent(f"""
            import subprocess, sys, time, pathlib
            subprocess.Popen([sys.executable, "-c", {backend!r}], start_new_session={own_group!r})
            for _ in range(100):
                if pathlib.Path({str(pids)!r}).exists():
                    break
                time.sleep(0.05)
        """)
        subprocess.run([sys.executable, "-c", app], timeout=30)
        backend_pid, child_pid = map(int, pids.read_text().split())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (_alive(backend_pid) or _alive(child_pid)):
            time.sleep(0.1)
        return _alive(backend_pid), _alive(child_pid)

    def test_session_of_its_own(self):
        self.assertEqual(self._run(True), (False, False))

    def test_in_the_apps_group(self):
        self.assertEqual(self._run(False), (False, False))


def _alive_windows(pid: int) -> bool:
    import psutil
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


@unittest.skipUnless(os.name == "nt", "Windows has no process groups: the backend watches its app")
class ParentWatchWindows(unittest.TestCase):
    def test_backend_ends_with_its_app(self):
        tmp = Path(tempfile.mkdtemp())
        pids = tmp / "pids"
        backend = textwrap.dedent(f"""
            import subprocess, sys, time
            sys.path.insert(0, {str(BACKEND)!r})
            import cli
            cli._exit_with_parent(poll=0.1)
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            open({str(pids)!r}, "w").write(f"{{__import__('os').getpid()}} {{child.pid}}")
            time.sleep(30)
        """)
        app = textwrap.dedent(f"""
            import subprocess, sys, time, pathlib
            subprocess.Popen([sys.executable, "-c", {backend!r}], creationflags=subprocess.CREATE_NO_WINDOW)
            for _ in range(200):
                if pathlib.Path({str(pids)!r}).exists():
                    break
                time.sleep(0.05)
        """)
        subprocess.run([sys.executable, "-c", app], timeout=60)
        backend_pid, child_pid = map(int, pids.read_text().split())
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (_alive_windows(backend_pid) or _alive_windows(child_pid)):
            time.sleep(0.1)
        self.assertEqual((_alive_windows(backend_pid), _alive_windows(child_pid)), (False, False))


@unittest.skipUnless(os.name == "nt", "Windows: Cancel walks the process tree")
class KillTreeWindows(unittest.TestCase):
    def test_cancel_stops_the_backend_and_its_children(self):
        import importlib.util
        os.environ.setdefault("PS5_FFPFSC_APP_DIR", tempfile.mkdtemp(prefix="ultrapack-test-profile-"))
        spec = importlib.util.spec_from_file_location("ultra_app", str(BACKEND.parent / "PS5_UltraPack.py"))
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        pids = Path(tempfile.mkdtemp()) / "pids"
        backend = textwrap.dedent(f"""
            import subprocess, sys, time
            kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            open({str(pids)!r}, "w").write(str(kid.pid))
            time.sleep(60)
        """)
        proc = subprocess.Popen([sys.executable, "-c", backend], creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(200):
            if pids.exists() and pids.read_text():
                break
            time.sleep(0.05)
        kid = int(pids.read_text())
        app._kill_process_tree(proc)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (_alive_windows(proc.pid) or _alive_windows(kid)):
            time.sleep(0.1)
        self.assertEqual((_alive_windows(proc.pid), _alive_windows(kid)), (False, False))


if __name__ == "__main__":
    unittest.main()
