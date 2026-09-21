"""Hash snapshots must not follow an input swapped after path validation."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from exitzero.files import sha256_file


class GuardedHashTests(unittest.TestCase):
    def test_leaf_symlink_and_fifo_are_never_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"
            target.write_text("synthetic outside content")
            alias = root / "alias"
            alias.symlink_to(target)
            with self.assertRaises((OSError, ValueError)):
                sha256_file(alias, root=root)
            if hasattr(os, "mkfifo"):
                fifo = root / "fifo"
                os.mkfifo(fifo)
                with self.assertRaises(ValueError):
                    sha256_file(fifo, root=root)

    @unittest.skipUnless(os.open in os.supports_dir_fd, "directory-relative open is unavailable")
    def test_parent_swap_keeps_open_descriptor_inside_original_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, outside = base / "repo", base / "outside"
            (root / "subdir").mkdir(parents=True)
            outside.mkdir()
            (root / "subdir/victim.py").write_bytes(b"trusted bytes")
            (outside / "victim.py").write_bytes(b"outside bytes")
            original = os.open
            def swap(path, flags, *args, **kwargs):
                if path == "victim.py" and "dir_fd" in kwargs:
                    (root / "subdir").rename(root / "held")
                    (root / "subdir").symlink_to(outside, target_is_directory=True)
                return original(path, flags, *args, **kwargs)
            with patch("exitzero.files.os.open", side_effect=swap) as opened:
                with patch("exitzero.files.os.supports_dir_fd", {opened}):
                    digest = sha256_file(root / "subdir/victim.py", root=root)
            self.assertEqual(digest, hashlib.sha256(b"trusted bytes").hexdigest())
            self.assertNotEqual(digest, hashlib.sha256(b"outside bytes").hexdigest())


if __name__ == "__main__":
    unittest.main()
