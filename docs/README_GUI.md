# Interact with GUI

This subfolder provides the GUI mode of `pdf2zh`.

## Usage

1. Run `pdf2zh -i`

2. Drop the PDF file into the window and click `Translate`.

### GUI Controls

The config panel (right side) offers the following controls:

- **Translation service / languages** — pick any supported service (`google`, `openai`, `deepl`, …) and source/target languages.
- **Service mode** — `auto` keeps historical behaviour, `babeldoc` routes through the BabelDOC layout engine.
- **ONNX backend** (`auto`/`cpu`/`cuda`/`dml`) — selects the execution provider for layout inference. The live **backend-status** panel below shows the installed ONNX Runtime version plus registered vs. actually-effective providers, so a silent CPU fallback is visible.
- **OCR mode** (`auto`/`on`/`off`) — scanned-PDF/OCR handling for the BabelDOC layout engine.
- **Parse engine** (`auto`/`legacy`/`babeldoc`/`magicpdf`) — switches the PDF parsing layer. `magicpdf` uses MinerU/magic-pdf (requires `pip install pdf2zh[magicpdf]` and the PDF-Extract-Kit models in `~/.cache/magic-pdf/models`); it automatically falls back to the legacy kernel when the engine is unavailable.
- **MagicPDF OCR** checkbox — forces OCR during magic-pdf parsing (`pipe_ocr_merge`), recommended for scanned PDFs.
- **Advanced options** — threads, page range, font exceptions, custom prompt, compatibility mode, etc.

All values persist to `localStorage` and are sent with every translation request.

### Environment Variables

You can set the source and target languages using environment variables:

- `PDF2ZH_LANG_FROM`: Sets the source language. Defaults to "English".
- `PDF2ZH_LANG_TO`: Sets the target language. Defaults to "Simplified Chinese".

### Supported Languages

The following languages are supported:

- English
- Simplified Chinese
- Traditional Chinese
- French
- German
- Japanese
- Korean
- Russian
- Spanish
- Italian

## Preview

<img src="./images/before.png" width="500"/>
<img src="./images/after.png" width="500"/>

## Maintainance

GUI maintained by [Rongxin](https://github.com/reycn)
