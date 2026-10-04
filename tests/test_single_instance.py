import tempfile
from pathlib import Path
import unittest
from windows import SingleInstance


class InstanceTests(unittest.TestCase):
    def test_same_data_directory_is_exclusive_and_released_on_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            with SingleInstance(directory) as first:
                self.assertTrue(first.acquired)
                with SingleInstance(directory) as second:self.assertFalse(second.acquired)
                with SingleInstance(Path(directory)/'other') as other:self.assertTrue(other.acquired)
            with SingleInstance(directory) as reopened:self.assertTrue(reopened.acquired)


if __name__=='__main__':unittest.main()
