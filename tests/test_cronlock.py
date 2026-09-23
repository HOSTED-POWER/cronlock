"""Black-box checks against a disposable, real Redis server.

Set CRONLOCK_TEST_PORT to the published Redis test port. Tests never use the
default production Redis endpoint and isolate all keys with a random prefix.
"""

import os
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "cronlock"
TEST_HOST = os.environ.get("CRONLOCK_TEST_HOST", "127.0.0.1")
TEST_PORT = int(os.environ.get("CRONLOCK_TEST_PORT", "0"))


class ReplyHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.recv(65536)
        self.request.sendall(self.server.reply)


class ReplyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def redis_command(*parts):
    """Minimal RESP client used only to inspect the isolated test keys."""
    with socket.create_connection((TEST_HOST, TEST_PORT), timeout=3) as conn:
        payload = f"*{len(parts)}\r\n".encode()
        for part in parts:
            raw = str(part).encode()
            payload += f"${len(raw)}\r\n".encode() + raw + b"\r\n"
        conn.sendall(payload)
        data = conn.makefile("rb")
        kind = data.read(1)
        line = data.readline().rstrip(b"\r\n")
        if kind == b"+":
            return line.decode()
        if kind == b":":
            return int(line)
        if kind == b"$":
            if line == b"-1":
                return None
            return data.read(int(line) + 2)[:-2].decode()
        raise AssertionError(f"unexpected Redis reply: {kind!r} {line!r}")


@unittest.skipUnless(TEST_PORT, "set CRONLOCK_TEST_PORT to an isolated Redis instance")
class CronlockIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cronlock-test-")
        self.addCleanup(self.temp.cleanup)
        config = Path(self.temp.name) / "cronlock.conf"
        config.write_text("# Intentionally empty test config\n")
        self.env = os.environ.copy()
        self.env.update(
            CRONLOCK_CONFIG=str(config),
            CRONLOCK_HOST=TEST_HOST,
            CRONLOCK_PORT=str(TEST_PORT),
            CRONLOCK_DB="0",
            CRONLOCK_KEY=uuid.uuid4().hex,
            CRONLOCK_PREFIX="cronlock.test.",
            CRONLOCK_GRACE="0",
            CRONLOCK_LEASE="2",
            CRONLOCK_RELEASE="172800",
            CRONLOCK_RECONNECT_ATTEMPTS="0",
            CRONLOCK_VERBOSE="no",
        )

    def run_lock(self, *command, env=None, timeout=10):
        return subprocess.run(
            [str(SCRIPT), *command],
            env=env or self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def start_lock(self, *command, env=None):
        process = subprocess.Popen(
            [str(SCRIPT), *command],
            env=env or self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        def clean_up():
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            process.stdout.close()
            process.stderr.close()

        self.addCleanup(clean_up)
        return process

    def wait_for_key(self):
        key = self.env["CRONLOCK_PREFIX"] + self.env["CRONLOCK_KEY"]
        for _ in range(100):
            if redis_command("EXISTS", key):
                return key
            time.sleep(0.02)
        self.fail("cronlock never acquired its Redis key")

    def static_server(self, reply):
        server = ReplyServer(("127.0.0.1", 0), ReplyHandler)
        server.reply = reply
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def test_ordinary_command_runs(self):
        result = self.run_lock("/bin/echo", "works")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "works")

    def test_active_lease_prevents_second_run_and_is_renewed(self):
        first = self.start_lock("/bin/sleep", "4")
        key = self.wait_for_key()
        self.assertGreater(redis_command("PTTL", key), 0, "lease needs a Redis TTL")
        time.sleep(2.5)  # Longer than the short lease: renewal must keep ownership.
        second = self.run_lock("/bin/echo", "must-not-run")
        self.assertEqual(second.returncode, 200, second.stderr)
        self.assertNotIn("must-not-run", second.stdout)
        self.assertEqual(first.wait(timeout=6), 0, first.stderr.read())

    def test_crashed_owner_does_not_block_for_legacy_48_hours(self):
        first = self.start_lock("/bin/sleep", "3")
        self.wait_for_key()
        first.kill()  # SIGKILL deliberately prevents graceful release.
        first.wait(timeout=2)
        time.sleep(2.5)
        second = self.run_lock("/bin/echo", "recovered")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), "recovered")

    def test_expired_legacy_timestamp_is_migrated_atomically(self):
        key = self.env["CRONLOCK_PREFIX"] + self.env["CRONLOCK_KEY"]
        redis_command("SET", key, int(time.time()) - 10)
        result = self.run_lock("/bin/echo", "legacy-recovered")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "legacy-recovered")

    def test_future_legacy_timestamp_gets_ttl_without_early_takeover(self):
        key = self.env["CRONLOCK_PREFIX"] + self.env["CRONLOCK_KEY"]
        redis_command("SET", key, int(time.time()) + 3)
        result = self.run_lock("/bin/echo", "must-not-run")
        self.assertEqual(result.returncode, 200, result.stderr)
        self.assertGreater(redis_command("PTTL", key), 0)
        time.sleep(3.1)
        recovered = self.run_lock("/bin/echo", "legacy-expired")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)

    def test_vip_mode_skips_non_owner_before_contacting_redis(self):
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        fake_ip = fake_bin / "ip"
        fake_ip.write_text("#!/bin/sh\nexit 0\n")  # Empty output means VIP absent.
        fake_ip.chmod(0o755)
        env = self.env | {
            "CRONLOCK_LOCAL_VIP": "10.100.30.20",
            "PATH": str(fake_bin) + os.pathsep + self.env.get("PATH", ""),
        }
        result = self.run_lock("/bin/echo", "must-not-run", env=env)
        self.assertEqual(result.returncode, 200, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_vip_mode_runs_on_owner(self):
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        fake_ip = fake_bin / "ip"
        fake_ip.write_text(
            "#!/bin/sh\nprintf '5: eth3 inet 10.100.30.20/24 scope global\\n'\n"
        )
        fake_ip.chmod(0o755)
        env = self.env | {
            "CRONLOCK_LOCAL_VIP": "10.100.30.20",
            "PATH": str(fake_bin) + os.pathsep + self.env.get("PATH", ""),
        }
        result = self.run_lock("/bin/echo", "owner-ran", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "owner-ran")

    def test_vip_can_be_set_in_the_existing_config_file(self):
        config = Path(self.env["CRONLOCK_CONFIG"])
        config.write_text("CRONLOCK_LOCAL_VIP=10.100.30.20\n")
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        fake_ip = fake_bin / "ip"
        fake_ip.write_text("#!/bin/sh\nexit 0\n")
        fake_ip.chmod(0o755)
        result = self.run_lock(
            "/bin/echo",
            "must-not-run",
            env=self.env
            | {"PATH": str(fake_bin) + os.pathsep + self.env.get("PATH", "")},
        )
        self.assertEqual(result.returncode, 200, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_missing_bash_for_config_fails_without_traceback(self):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "/bin/true"],
            env=self.env | {"PATH": self.temp.name},
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(result.returncode, 201)
        self.assertIn("cannot load config file", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_old_owner_cannot_release_a_successor_token(self):
        first = self.start_lock("/bin/sleep", "8")
        key = self.wait_for_key()
        self.assertEqual(
            redis_command("SET", key, "successor-token", "PX", 10000), "OK"
        )
        self.assertEqual(first.wait(timeout=5), 201)
        self.assertEqual(redis_command("GET", key), "successor-token")

    def test_command_exit_and_timeout_are_preserved(self):
        failure = self.run_lock("/bin/sh", "-c", "exit 7")
        self.assertEqual(failure.returncode, 7, failure.stderr)
        env = self.env | {"CRONLOCK_KEY": uuid.uuid4().hex, "CRONLOCK_TIMEOUT": "1"}
        timed_out = self.run_lock("/bin/sleep", "3", env=env)
        self.assertEqual(timed_out.returncode, 202, timed_out.stderr)

    def test_missing_command_fails_cleanly_and_releases_lease(self):
        result = self.run_lock("/no/such/cronlock-test-command")
        self.assertEqual(result.returncode, 201)
        self.assertNotIn("Traceback", result.stderr)
        key = self.env["CRONLOCK_PREFIX"] + self.env["CRONLOCK_KEY"]
        self.assertEqual(redis_command("EXISTS", key), 0)

    def test_unsafe_reset_is_rejected_without_deleting_a_live_lease(self):
        key = self.env["CRONLOCK_PREFIX"] + self.env["CRONLOCK_KEY"]
        redis_command("SET", key, "another-owner", "PX", 10000)
        result = self.run_lock("/bin/true", env=self.env | {"CRONLOCK_RESET": "yes"})
        self.assertEqual(result.returncode, 201)
        self.assertEqual(redis_command("GET", key), "another-owner")

    def test_redis_reply_cannot_be_interpreted_as_shell_code(self):
        marker = Path(self.temp.name) / "should-not-exist"
        key = self.env["CRONLOCK_PREFIX"] + self.env["CRONLOCK_KEY"]
        redis_command("SET", key, f"' ; touch {marker} ; #", "PX", 10000)
        result = self.run_lock("/bin/echo", "must-not-run")
        self.assertEqual(result.returncode, 200, result.stderr)
        self.assertFalse(marker.exists())

    def test_termination_stops_command_and_releases_lease(self):
        first = self.start_lock("/bin/sleep", "8")
        key = self.wait_for_key()
        first.terminate()
        self.assertEqual(first.wait(timeout=4), 128 + signal.SIGTERM)
        self.assertEqual(redis_command("EXISTS", key), 0)

    def test_redis_unavailable_fails_closed(self):
        marker = Path(self.temp.name) / "must-not-run"
        env = self.env | {"CRONLOCK_PORT": "1"}
        result = self.run_lock("/usr/bin/touch", str(marker), env=env)
        self.assertEqual(result.returncode, 201)
        self.assertFalse(marker.exists())

    def test_redis_cluster_moved_reply_is_followed(self):
        port = self.static_server(f"-MOVED 0 127.0.0.1:{TEST_PORT}\r\n".encode())
        env = self.env | {"CRONLOCK_HOST": "127.0.0.1", "CRONLOCK_PORT": str(port)}
        result = self.run_lock("/bin/echo", "moved-ok", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "moved-ok")

    def test_sentinel_master_lookup_is_followed(self):
        redis_port = str(TEST_PORT).encode()
        reply = (
            b"*2\r\n$9\r\n127.0.0.1\r\n"
            + f"${len(redis_port)}\r\n".encode()
            + redis_port
            + b"\r\n"
        )
        port = self.static_server(reply)
        env = self.env | {
            "CRONLOCK_USE_SENTINEL": "yes",
            "CRONLOCK_SENTINEL_HOST": "127.0.0.1",
            "CRONLOCK_SENTINEL_PORT": str(port),
        }
        result = self.run_lock("/bin/echo", "sentinel-ok", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "sentinel-ok")

    def test_vip_loss_stops_an_active_command(self):
        fake_bin = Path(self.temp.name) / "bin"
        fake_bin.mkdir()
        marker = Path(self.temp.name) / "owns-vip"
        marker.touch()
        fake_ip = fake_bin / "ip"
        fake_ip.write_text(
            "#!/bin/sh\n"
            f"test -e '{marker}' && printf '5: eth3 inet 10.100.30.20/24 scope global\\n'\n"
        )
        fake_ip.chmod(0o755)
        env = self.env | {
            "CRONLOCK_LOCAL_VIP": "10.100.30.20",
            "PATH": str(fake_bin) + os.pathsep + self.env.get("PATH", ""),
        }
        first = self.start_lock("/bin/sleep", "8", env=env)
        self.wait_for_key()
        marker.unlink()
        self.assertEqual(first.wait(timeout=4), 201)


if __name__ == "__main__":
    unittest.main()
