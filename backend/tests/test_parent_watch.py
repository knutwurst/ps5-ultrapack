"""A backend ends, with everything it started, when the app that started it is gone (the
app crashed once and left a pack job and a scan running on the drive)."""
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
        # the "app": starts the backend (in a session of its own, as the app does for jobs,
        # or in its own group, as for a scan) and leaves at once
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


if __name__ == "__main__":
    unittest.main()
