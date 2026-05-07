import base64
import os
import time
import logging

import config
from tasks.celery_app import app

log = logging.getLogger(__name__)


@app.task(name="tasks.run_owocr_ocr_frame")
def run_owocr_ocr_frame(prev_result: list) -> list:
    b64_image, seq_num = prev_result
    log.info(f"Running OWOCR OCR on frame {seq_num}...")

    image_dir = config.OWOCR_READ_FROM_DIRECTORY
    txt_dir = config.OWOCR_WRITE_TO_DIRECTORY
    # write to image_dir as "input_{seq_num}.png"
    image_path = os.path.join(image_dir, f"input_{seq_num}.png")
    log.info(f"Writing image to {image_path}...")
    with open(image_path, "wb") as f:
        f.write(base64.b64decode(b64_image))
    
    # wait for "output_{seq_num}.png.txt" to appear
    log.info(f"Waiting for OCR result at {txt_dir}...")
    output_path = os.path.join(txt_dir, f"input_{seq_num}.png.txt")
    while not os.path.exists(output_path):
        time.sleep(config.OWOCR_MONITOR_DIRECTORY_INTERVAL)

    log.info(f"OCR result found at {output_path}, reading...")
    with open(output_path, "r") as f:
        ocr_result = f.read().strip()

    log.info(f"OCR result for frame {seq_num}: {ocr_result}")

    return [[{"speaker": "Narrator", "text": ocr_result}], seq_num]
