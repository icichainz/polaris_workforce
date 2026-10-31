"""Default file storage: uploads get unguessable names.

Worker photos and payment proofs (photos of people receiving cash, signatures, thumb
marks) must not be found by guessing a URL or a file name. Every uploaded file keeps its
folder (``proofs/2026/10/``, ``workers/``) and its extension, but its name becomes a
random token: ``proofs/2026/10/q3Zk…Yw.jpg``. The phone's or the office PC's file name is
dropped.

No migration is needed: the fields keep their ``upload_to`` and use this storage because
it is the default one (``STORAGES["default"]`` in ``config/settings.py``). Files are
served only through the protected views in ``apps.core.files``.
"""

import posixpath
import re
import secrets

from django.core.files.storage import FileSystemStorage

TOKEN_BYTES = 24  # 192 bits, 32 URL-safe characters
_EXTENSION = re.compile(r"\.[a-z0-9]{1,10}")


def random_name(original: str) -> str:
    """A random token with the (lower-cased) extension of ``original``."""
    extension = posixpath.splitext(original)[1].lower()
    if not _EXTENSION.fullmatch(extension):
        extension = ""
    return secrets.token_urlsafe(TOKEN_BYTES) + extension


class RandomNameStorage(FileSystemStorage):
    """``FileSystemStorage`` that replaces every new file's name with a random token."""

    def generate_filename(self, filename):
        # Called by FileField with "<upload_to folder>/<uploaded name>"; the parent class
        # rejects ".." in the folder and normalises the path.
        filename = super().generate_filename(filename)
        dirname, name = posixpath.split(str(filename).replace("\\", "/"))
        return posixpath.join(dirname, random_name(name))

    def get_available_name(self, name, max_length=None):
        # A clash is practically impossible; if it happens, draw a new token instead of
        # Django's "_<7 characters>" suffix (which would keep the guessable part).
        name = str(name).replace("\\", "/")
        dirname, basename = posixpath.split(name)
        while self.exists(name):
            name = posixpath.join(dirname, random_name(basename))
        return super().get_available_name(name, max_length=max_length)
