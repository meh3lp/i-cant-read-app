#!/usr/bin/env python3
"""
Requires a running Redis server and a Celery worker:
    redis-server
    celery -A tasks worker --loglevel=info --concurrency=4
"""

import argparse
import logging

import config
from management.i_cant_read import ICantRead

log = logging.getLogger("cantread")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    # p.add_argument("--owocr-url", default=config.OWOCR_WS_URL,
    #                 help="owocr websocket URL  (default: %(default)s)")
    # p.add_argument("--kokoro-url", default=config.KOKORO_URL,
    #                 help="Kokoro TTS Gradio URL  (default: %(default)s)")
    # p.add_argument("--rvc-url", default=config.RVC_URL,
    #                 help="RVC Gradio URL  (default: %(default)s)")
    # p.add_argument("--voice", default=config.KOKORO_VOICE,
    #                 help="Kokoro voice name  (default: %(default)s)")
    # p.add_argument("--speed", type=float, default=config.SPEED,
    #                 help="TTS speed  (default: %(default)s)")
    # p.add_argument("--no-cleanup", action="store_true",
    #                 help="Disable Ollama OCR text cleanup")
    # p.add_argument("--disable-rvc", action="store_true",
    #                 help="Disable RVC voice conversion")
    p.add_argument("--debug-stream", action="store_true",
                    help="Launch MPV window with live CV gate debug overlay")
    p.add_argument("-v", "--verbose", action="store_true",
                    help="DEBUG-level logging")
    return p.parse_args()


def apply_overrides(args: argparse.Namespace) -> None:
    """Push CLI overrides back into the config module."""
    # config.OWOCR_WS_URL = args.owocr_url
    # config.KOKORO_URL = args.kokoro_url
    # config.RVC_URL = args.rvc_url
    # config.KOKORO_VOICE = args.voice
    # config.SPEED = args.speed
    # if args.no_cleanup:
    #     config.OLLAMA_CLEANUP = False
    # if args.disable_rvc:
    #     config.RVC_ENABLED = False
    if args.debug_stream:
        config.VISION_DEBUG_STREAM = True


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s  %(name)-18s  %(levelname)-7s  %(message)s",
        datefmt="%H:%M:%S",
    )

    apply_overrides(args)

    cantread = ICantRead()
    cantread.run()


if __name__ == "__main__":
    main()
