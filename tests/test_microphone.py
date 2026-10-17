"""Microphone stream status handling; no audio hardware."""

import unittest
from unittest.mock import MagicMock, patch

import sounddevice as sd

from devices.microphone import microphone_chunks


def flags(**values):
    status = sd.CallbackFlags()
    for name, value in values.items():
        setattr(status, name, value)
    return status


class MicrophoneChunkTests(unittest.IsolatedAsyncioTestCase):
    async def test_input_overflow_keeps_audio_flowing_but_other_statuses_stop(self):
        with patch("devices.microphone.sd.RawInputStream", return_value=MagicMock()) as stream:
            chunks = microphone_chunks()
            try:
                callback = None

                def capture_first_chunk():
                    nonlocal callback
                    callback = stream.call_args.kwargs["callback"]
                    callback(b"ok", 1, None, flags())

                stream.return_value.__enter__.side_effect = lambda: capture_first_chunk()
                self.assertEqual(await anext(chunks), b"ok")
                callback(b"kept", 1, None, flags(input_overflow=True))
                self.assertEqual(await anext(chunks), b"kept")
                callback(b"lost", 1, None, flags(input_underflow=True))
                with self.assertRaisesRegex(RuntimeError, "underflow"):
                    await anext(chunks)
            finally:
                await chunks.aclose()


if __name__ == "__main__":
    unittest.main()
