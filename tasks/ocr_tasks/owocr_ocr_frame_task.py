def run_owocr_ocr_frame(prev_result: list) -> list:
    b64_image, seq_num = prev_result
    # Non-LLM OCR: wrap plain text as a Narrator replica
    return [[{"speaker": "Narrator", "text": "placeholder"}], seq_num]
