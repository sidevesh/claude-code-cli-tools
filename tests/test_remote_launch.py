import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin" / "claude-remote-launch"


class RemoteLaunchHereTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.project = self.root / "Projects" / "example"
        self.profile = self.home / ".claude-work"
        self.capture = self.root / "capture.json"
        for directory in (self.home, self.bin, self.project, self.profile):
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


if __name__ == "__main__":
    unittest.main()
