import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
from claude_profile_lib import prompt_session_profile  # noqa: E402

LAUNCHER = ROOT / "bin" / "claude-remote-launch"
HERDR_LAUNCHER = ROOT / "bin" / "herdr-launch"


class RemoteLaunchHereTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.project = self.root / "Projects" / "example"
        self.profile = self.home / ".claude-work"
        self.second_profile = self.home / ".claude-shubhi"
        self.capture = self.root / "capture.json"
        for directory in (self.home, self.bin, self.project, self.profile,
                          self.second_profile):
            directory.mkdir(parents=True, exist_ok=True)
        fake = self.bin / "claude"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['CAPTURE'], 'w') as f:\n"
            " json.dump({'argv': sys.argv[1:], 'cwd': os.getcwd(), "
            "'config': os.environ.get('CLAUDE_CONFIG_DIR')}, f)\n"
        )
        fake.chmod(0o755)

    def tearDown(self):
        self.temp.cleanup()

    def run_launcher(self):
        env = os.environ.copy()
        env.update({
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "CAPTURE": str(self.capture),
            "CLAUDE_TERMINAL": "this-must-be-ignored-by-here",
        })
        subprocess.run(
            [sys.executable, str(LAUNCHER), "--here", "--profile", "work",
             "--continue-if-available", str(self.project)],
            env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        return json.loads(self.capture.read_text())

    @staticmethod
    def slug(path):
        return re.sub(r"[^A-Za-z0-9]+", "-", str(path.resolve()))

    def add_transcript(self, cwd):
        project_dir = self.profile / "projects" / self.slug(self.project)
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "session.jsonl").write_text(json.dumps({"cwd": str(cwd)}) + "\n")

    def add_session(self, profile, session_id, cwd):
        project_dir = profile / "projects" / self.slug(cwd)
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / (session_id + ".jsonl")).write_text(
            json.dumps({"cwd": str(cwd)}) + "\n")

    def run_herdr_shim(self, session_id, cwd=None):
        shim = self.bin / "claude-shim" / "claude"
        shim.parent.mkdir(exist_ok=True)
        if not shim.exists():
            shim.symlink_to(LAUNCHER)
        env = os.environ.copy()
        env.update({
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "CAPTURE": str(self.capture),
            "HERDR_ENV": "1",
            "CLAUDE_CONFIG_DIR": str(self.profile),
            "CLAUDE_REMOTE_LAUNCH_REAL_CLAUDE": str(self.bin / "claude"),
        })
        subprocess.run(
            [str(shim), "--resume", session_id],
            env=env, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=str(cwd or self.project),
        )
        return json.loads(self.capture.read_text())

    def test_here_starts_new_session_when_no_conversation_exists(self):
        result = self.run_launcher()
        self.assertNotIn("--continue", result["argv"])
        self.assertEqual(os.path.realpath(result["cwd"]), os.path.realpath(self.project))
        self.assertEqual(result["config"], str(self.profile))
        self.assertIn("--remote-control", result["argv"])
        expected_name = "example (%s@" % os.environ.get("USER", "")
        self.assertTrue(any(arg.startswith(expected_name) for arg in result["argv"]))
        trust = json.loads((self.profile / ".claude.json").read_text())
        self.assertTrue(trust["projects"][str(self.project)]["hasTrustDialogAccepted"])

    def test_here_continues_matching_project_conversation(self):
        self.add_transcript(self.project)
        result = self.run_launcher()
        self.assertIn("--continue", result["argv"])

    def test_slug_collision_does_not_continue_another_project(self):
        colliding = pathlib.Path(str(self.project).replace("example", "exam.ple"))
        self.add_transcript(colliding)
        result = self.run_launcher()
        self.assertNotIn("--continue", result["argv"])

    def test_herdr_shim_resumes_through_selected_profile(self):
        result = self.run_herdr_shim("session-123")
        self.assertEqual(result["config"], str(self.profile))
        self.assertIn("--resume", result["argv"])
        self.assertIn("session-123", result["argv"])
        self.assertIn("--remote-control", result["argv"])
        self.assertTrue(any(arg.startswith("example (") for arg in result["argv"]))

    def test_herdr_shim_infers_profile_from_resume_transcript(self):
        self.add_session(self.second_profile, "session-shubhi", self.project)
        result = self.run_herdr_shim("session-shubhi")
        self.assertEqual(result["config"], str(self.second_profile))
        self.assertIn("session-shubhi", result["argv"])

    def test_herdr_shim_uses_cwd_to_disambiguate_copied_session(self):
        other_project = self.root / "Projects" / "other"
        other_project.mkdir(parents=True)
        self.add_session(self.profile, "session-copy", self.project)
        self.add_session(self.second_profile, "session-copy", other_project)
        result = self.run_herdr_shim("session-copy", cwd=other_project)
        self.assertEqual(result["config"], str(self.second_profile))

    def test_herdr_shim_prefers_only_registered_copy(self):
        self.add_session(self.profile, "session-copy", self.project)
        self.add_session(self.second_profile, "session-copy", self.project)
        (self.second_profile / ".claude.json").write_text(json.dumps({
            "projects": {str(self.project): {"hasTrustDialogAccepted": True}}
        }))
        result = self.run_herdr_shim("session-copy")
        self.assertEqual(result["config"], str(self.second_profile))

    def test_ambiguous_profile_prompt_warns_and_does_not_remember(self):
        class FakeTTY:
            def __init__(self, answer):
                self.answer = answer

            def isatty(self):
                return True

            def readline(self):
                answer, self.answer = self.answer, ""
                return answer

        class Capture:
            def __init__(self):
                self.parts = []

            def write(self, value):
                self.parts.append(value)

            def flush(self):
                pass

        output = Capture()
        selected = prompt_session_profile(
            "session-copy", [str(self.profile), str(self.second_profile)],
            input_stream=FakeTTY("2\n"), output_stream=output)
        self.assertEqual(selected, str(self.second_profile))
        text = "".join(output.parts)
        self.assertIn("Remove the duplicate", text)
        self.assertIn("every Herdr start", text)

    def test_herdr_launcher_sets_private_shim_environment(self):
        herdr_capture = self.root / "herdr-capture.json"
        fake_herdr = self.bin / "herdr"
        fake_herdr.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "with open(os.environ['HERDR_CAPTURE'], 'w') as f:\n"
            " json.dump({'argv': sys.argv[1:], 'path': os.environ['PATH'], "
            "'config': os.environ.get('CLAUDE_CONFIG_DIR'), "
            "'real': os.environ.get('CLAUDE_REMOTE_LAUNCH_REAL_CLAUDE')}, f)\n"
        )
        fake_herdr.chmod(0o755)
        shim_dir = self.root / "herdr-bin"
        shim_dir.mkdir()
        (shim_dir / "claude").symlink_to(LAUNCHER)
        env = os.environ.copy()
        env.update({
            "HOME": str(self.home),
            "PATH": str(self.bin) + os.pathsep + env.get("PATH", ""),
            "HERDR_CAPTURE": str(herdr_capture),
            "CLAUDE_REMOTE_LAUNCH_HERDR_SHIM_DIR": str(shim_dir),
        })
        proc = subprocess.run(
            [sys.executable, str(HERDR_LAUNCHER), "--session", "projects",
             "--claude-fallback-profile", "work"],
            env=env, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(herdr_capture.read_text())
        self.assertEqual(result["argv"], ["--session", "projects"])
        self.assertEqual(result["config"], str(self.profile))
        self.assertEqual(os.path.realpath(result["real"]),
                         os.path.realpath(self.bin / "claude"))
        self.assertIn(str(shim_dir), result["path"].split(os.pathsep))

    def test_remote_launcher_has_no_herdr_session_option(self):
        proc = subprocess.run(
            [sys.executable, str(LAUNCHER), "--help"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertNotIn("--herdr-session", proc.stdout)

    def test_herdr_help_uses_explicit_fallback_option_name(self):
        proc = subprocess.run(
            [sys.executable, str(HERDR_LAUNCHER), "--help"],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        self.assertIn("--claude-fallback-profile", proc.stdout)
        self.assertNotIn("--claude-profile ", proc.stdout)


if __name__ == "__main__":
    unittest.main()
