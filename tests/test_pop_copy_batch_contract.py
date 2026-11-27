"""Run the full read-only interrupted-batch scenario at the business CLI."""
from pathlib import Path
import subprocess
import sys
import unittest


class BatchEntryTests(unittest.TestCase):
    def test_frozen_targets_stop_unknown_and_receipted_refusal(self):
        subprocess.run([sys.executable, "-B", str(Path(__file__).with_name("pop_copy_batch_contract.py"))],
                       check=True, timeout=180)
