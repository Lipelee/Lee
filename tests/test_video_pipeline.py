"""Teste de ponta a ponta com vídeo real (pulado se não houver ffmpeg)."""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg não instalado")
class VideoPipelineTest(unittest.TestCase):
    def test_demo_edits_and_animates(self):
        with tempfile.TemporaryDirectory() as out:
            proc = subprocess.run([sys.executable, str(ROOT / "examples" / "demo.py"), "--out", out],
                                  capture_output=True, text=True, timeout=300)
            self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
            self.assertIn("Projeto done.", proc.stdout)
            final = next(Path(out).glob("*-final.mp4"))
            dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                        "-of", "csv=p=0", str(final)], capture_output=True, text=True).stdout)
            self.assertAlmostEqual(dur, 12.0, delta=0.2)  # briefing pede 12s


if __name__ == "__main__":
    unittest.main()
