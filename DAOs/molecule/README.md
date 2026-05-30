# Molecule DAO Pipeline

Research DAO review pipeline for Molecule IP-NFT datarooms.

## Flow

1. **Crawl** — `crawlers/molecule/crawler` fetches profiles + dataroom files
2. **Multimedia router** — `pipeline/multimedia_processor.py` routes all file types into `{output}/bundle/`
3. **Phase 1** — article pipeline on `bundle/pdf/articles/*.pdf`
4. **Phase 2** — DAO synthesis (aggregate, review, score, evidence)

## Multimedia bundle layout

```text
{output_dir}/bundle/
├── manifest.json
├── pdf/
│   ├── articles/
│   ├── proposals/
│   └── other/
├── images/
├── videos/
└── text/
```

PDF routing uses ephemeral 2-page vision OCR + text LLM classification. Only raw PDF copies are saved.

## Environment

| Variable | Purpose |
|----------|---------|
| `VLLM_BASE_URL` | OpenAI-compatible API (vision + text) |
| `VLLM_API_KEY` | API key |
| `READ_PAPER_MODEL` | Vision/OCR model (default: `nanonets/Nanonets-OCR2-3B`) |
| `VALIDATOR_MODEL` | Text LLM for PDF classification + synthesis |
| `WHISPER_CPP_BIN` | whisper.cpp binary (default: `whisper-cli`) |
| `WHISPER_MODEL_PATH` | Path to ggml-small model (default: `models/ggml-small.bin`) |

## External tools

- **ffmpeg** / **ffprobe** — video frame extraction and audio demux (must be on PATH)
- **whisper.cpp** — CPU transcription for video audio tracks

## Usage

```bash
# Multimedia bundle only
python DAOs/molecule/pipeline/multimedia_processor.py \
  --ipnft-dir crawlers/output/molecule/ipnfts/CLAW \
  --output-dir reviews/DAOs/CLAW

# Full DAO pipeline (multimedia → articles → synthesis)
python DAOs/molecule/pipeline/run_dao_pipeline.py \
  --ipnft-dir crawlers/output/molecule/ipnfts/CLAW \
  --output-dir reviews/DAOs/CLAW \
  --model /model

# Stop after bundle, resume for article OCR
python run_dao_pipeline.py ... --stop-after multimedia
python run_dao_pipeline.py ... --from-step ocr --model /vision-model
```

`--from-step filter` is an alias for `multimedia`.

Flags: `--skip-vision` (PDF-only routing), `--skip-llm` (route all PDFs to `pdf/other`), `--keep-temp` (retain video frame PNGs).
