"""Tests for image handling: content splitting, byte resolution, MIME sniffing,
token estimates, Ollama conversion, and the content-addressed image store.

Phase 1 (passthrough) is exercised by the *_to_ollama / *_split_content / token
tests; Phase 2 (ingest + dedupe) by _ingest_images. Pure util functions import
without the proxy; _ingest_images lives in mneme_proxy so it's imported under an
isolated environment (like test_trust.py).
"""

import base64
import os
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "proxy"))

from mneme.util import (  # noqa: E402
    _extract_text,
    _split_content,
    _image_bytes_from_block,
    _sniff_mime,
    _mime_to_ext,
    _image_token_estimate,
    _to_ollama_messages,
)

# 1x1 red PNG (valid magic bytes + IHDR/IDAT/IEND)
_PNG_1x1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c626001000000ffff03000006000557bfabd40000000049454e44ae426082"
)
# a tiny GIF (different bytes → different sha256)
_GIF = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff\x00\x00\x00\x00\x00\x00"


def _data_url(data, mime="image/png"):
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _multimodal(data, text="what is this?"):
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": _data_url(data)}},
    ]


class TestImageUtil(unittest.TestCase):
    def test_split_content_string(self):
        self.assertEqual(_split_content("just text"), ("just text", []))

    def test_split_content_multimodal(self):
        text, imgs = _split_content(_multimodal(_PNG_1x1))
        self.assertEqual(text, "what is this?")
        self.assertEqual(len(imgs), 1)
        self.assertEqual(imgs[0]["type"], "image_url")

    def test_image_bytes_from_data_url(self):
        data, mime = _image_bytes_from_block(_multimodal(_PNG_1x1)[1])
        self.assertEqual(data, _PNG_1x1)
        self.assertEqual(mime, "image/png")

    def test_image_bytes_bad_block(self):
        self.assertEqual(_image_bytes_from_block({"type": "image_url", "image_url": {}}), (None, None))
        self.assertEqual(_image_bytes_from_block({"type": "image_url", "image_url": {"url": ""}}), (None, None))

    def test_sniff_mime(self):
        self.assertEqual(_sniff_mime(_PNG_1x1), "image/png")
        self.assertEqual(_sniff_mime(_GIF), "image/gif")
        self.assertEqual(_sniff_mime(b"\xff\xd8\xff\xe0"), "image/jpeg")
        self.assertIsNone(_sniff_mime(b"not an image at all"))

    def test_mime_to_ext(self):
        self.assertEqual(_mime_to_ext("image/png"), "png")
        self.assertEqual(_mime_to_ext("image/jpeg"), "jpg")
        self.assertEqual(_mime_to_ext("IMAGE/GIF"), "gif")
        self.assertEqual(_mime_to_ext("application/octet-stream"), "bin")

    def test_image_token_estimate_data_url(self):
        # tiny image → floor of 85
        self.assertEqual(_image_token_estimate(_multimodal(_PNG_1x1)[1]), 85)
        # http URL → conservative default
        self.assertEqual(_image_token_estimate({"image_url": {"url": "https://x/y.png"}}), 1000)
        # large data URL is capped at 1440
        big = _data_url(b"\x00" * (1024 * 1024))
        self.assertEqual(_image_token_estimate({"image_url": {"url": big}}), 1440)

    def test_extract_text_redacts_data_url(self):
        # _extract_text must NOT dump the raw base64 into text paths.
        txt = _extract_text(_multimodal(_PNG_1x1))
        self.assertIn("what is this?", txt)
        self.assertIn("[IMAGE: data:image/png;base64,<...>]", txt)
        self.assertNotIn(base64.b64encode(_PNG_1x1).decode()[:40], txt)

    def test_to_ollama_messages(self):
        msgs = [
            {"role": "user", "content": _multimodal(_PNG_1x1)},
            {"role": "assistant", "content": "I see a red pixel."},
        ]
        out = _to_ollama_messages(msgs)
        self.assertEqual(out[0]["role"], "user")
        self.assertEqual(out[0]["content"], "what is this?")
        self.assertEqual(len(out[0]["images"]), 1)
        self.assertEqual(base64.b64decode(out[0]["images"][0]), _PNG_1x1)
        # text-only message has no images key
        self.assertNotIn("images", out[1])
        self.assertEqual(out[1]["content"], "I see a red pixel.")


class TestIngestImages(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Isolated env BEFORE importing the proxy (so CHUNK_DIR points at a temp dir).
        cls._tmp = tempfile.mkdtemp(prefix="mneme_imgtest_")
        os.environ["MNEME_CHUNK_DIR"] = cls._tmp
        os.environ["MNEME_CONFIG"] = os.path.join(cls._tmp, "empty.json")
        with open(os.environ["MNEME_CONFIG"], "w") as f:
            f.write("{}")
        os.environ["MNEME_BACKEND"] = "ollama"
        os.environ["MNEME_OLLAMA_URL"] = "http://127.0.0.1:1"
        os.environ["MNEME_MODEL"] = "test-model"
        os.environ["EMBED_MODEL"] = "test-embed"
        os.environ["LABEL_MODEL"] = "test-label"
        os.environ["MNEME_MEMORY_ENABLED"] = "0"
        import mneme_proxy as mp
        cls.mp = mp

    def test_ingest_returns_refs(self):
        refs = self.mp._ingest_images(_multimodal(_PNG_1x1))
        self.assertEqual(len(refs), 1)
        r = refs[0]
        self.assertIn("hash", r)
        self.assertIn("path", r)
        self.assertEqual(r["mime"], "image/png")
        self.assertTrue(os.path.isfile(r["path"]))

    def test_ingest_dedupes_identical_bytes(self):
        refs1 = self.mp._ingest_images(_multimodal(_PNG_1x1))
        refs2 = self.mp._ingest_images(_multimodal(_PNG_1x1, text="reprocess the same image"))
        self.assertEqual(refs1[0]["path"], refs2[0]["path"])
        # only one file on disk for that hash
        img_dir = os.path.join(self.mp.CHUNK_DIR, "images")
        matching = [f for f in os.listdir(img_dir) if f.startswith(refs1[0]["hash"])]
        self.assertEqual(len(matching), 1)

    def test_ingest_distinct_images_get_distinct_paths(self):
        a = self.mp._ingest_images(_multimodal(_PNG_1x1))
        b = self.mp._ingest_images(_multimodal(_GIF, text="a different image"))
        self.assertNotEqual(a[0]["path"], b[0]["path"])
        self.assertNotEqual(a[0]["hash"], b[0]["hash"])

    def test_ingest_plain_text_returns_empty(self):
        self.assertEqual(self.mp._ingest_images("no image here"), [])

    def test_read_image_tool_roundtrip(self):
        refs = self.mp._ingest_images(_multimodal(_PNG_1x1))
        import mneme.tools as mntools
        data_url = mntools._exec_read_image(refs[0]["hash"])
        self.assertTrue(data_url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(data_url.split(",", 1)[1]), _PNG_1x1)

    def test_read_image_missing(self):
        import mneme.tools as mntools
        self.assertIn("not found", mntools._exec_read_image("deadbeef"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
