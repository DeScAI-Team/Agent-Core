# Add_data

# import chromadb
# from chromadb.utils.embedding_functions import SentenceTransformerEmbeddingFunction
import os
import glob
import argparse
import json
import re
from pathlib import Path
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.chunking import HybridChunker
from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer
from docling_core.types.doc import PictureItem, TableItem
from transformers import AutoTokenizer
import sys
from openai import OpenAI

_ARTICLES = Path(__file__).resolve().parents[2]
if str(_ARTICLES) not in sys.path:
    sys.path.insert(0, str(_ARTICLES))

from llm_env import make_client, tagger_model  # noqa: E402

# === CONFIG ===
EMBED_MODEL_ID = "BAAI/bge-m3"
MAX_TOKENS = 300
DEFAULT_OUTPUT_JSONL = "text_knowledge_base.jsonl"
IMAGE_RESOLUTION_SCALE = 2.0
HEADING_CLASSIFIER_RETRIES = 3
ALLOWED_SEMANTIC_CATEGORIES = {
    "abstract",
    "introduction",
    "method",
    "result",
    "conclusion",
    "reference",
    "other",
}

_heading_client = make_client(tagger=True)


def _heading_model() -> str:
    return tagger_model()

# === SETUP ===
tokenizer = HuggingFaceTokenizer(
    tokenizer=AutoTokenizer.from_pretrained(EMBED_MODEL_ID),
    max_tokens=MAX_TOKENS,
)

chunker = HybridChunker(
    tokenizer=tokenizer,
    merge_peers=True,
)

pipeline_options = PdfPipelineOptions()
pipeline_options.do_table_structure = False

pdf_format_options = PdfFormatOption(pipeline_options=pipeline_options)

converter = DocumentConverter(
    format_options={"pdf": pdf_format_options}
)

# ============================Image Export Converter==============================
def get_image_export_converter():
    """Get a converter configured for image export."""
    img_pipeline_options = PdfPipelineOptions()
    img_pipeline_options.images_scale = IMAGE_RESOLUTION_SCALE
    img_pipeline_options.generate_page_images = True
    img_pipeline_options.generate_picture_images = True
    img_pipeline_options.do_table_structure = True
    
    img_pdf_format_options = PdfFormatOption(pipeline_options=img_pipeline_options)
    
    return DocumentConverter(
        format_options={"pdf": img_pdf_format_options}
    )

# ============================ Semantic Heading Classifier ==============================
def _extract_json_object(raw_text):
    """Extract and parse a JSON object from model text output."""
    text = raw_text.strip()
    if not text:
        raise ValueError("empty response")
    # Try direct parse first
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Fallback: first JSON object in the response
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError("no JSON object found")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("parsed JSON is not an object")
    return parsed


def get_semantic_heading_map(headings_list):
    """
    Uses local vLLM endpoint to map technical headings to functional categories.
    """
    if not headings_list:
        return {}

    system_prompt = (
        "You are a scientific document structure analyst. "
        "Categorize each heading into one of: introduction, abstract, method, result, conclusion, reference, other. "
        "If the name matches or similiar to the above sections, just give simantic category as the above mentioned ones."
        "Return ONLY a valid JSON object mapping each input heading to one category. "
        "Do not rename or omit any heading."
    )

    user_content = "HEADINGS:\n" + "\n".join(headings_list)

    lower_to_original = {h.lower(): h for h in headings_list}

    for attempt in range(1, HEADING_CLASSIFIER_RETRIES + 1):
        try:
            response = _heading_client.chat.completions.create(
                model=_heading_model(),
                max_tokens=1024,
                temperature=0,
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
            )

            raw_content = response.choices[0].message.content or ""
            raw_content = re.sub(r"<think>.*?</think>", "", raw_content, flags=re.DOTALL).strip()
            raw_content = re.sub(r"<think>.*", "", raw_content, flags=re.DOTALL).strip()
            parsed_map = _extract_json_object(raw_content)

            cleaned_map = {}
            for heading in headings_list:
                value = parsed_map.get(heading)
                if value is None:
                    value = parsed_map.get(heading.lower())
                if value is None:
                    maybe_key = lower_to_original.get(heading.lower(), heading).lower()
                    value = parsed_map.get(maybe_key)
                label = str(value).strip().lower() if value is not None else "other"
                cleaned_map[heading] = label if label in ALLOWED_SEMANTIC_CATEGORIES else "other"

            return cleaned_map

        except Exception as e:
            print(
                f"  [WARN] heading classification attempt "
                f"{attempt}/{HEADING_CLASSIFIER_RETRIES} failed: {e}"
            )

    print("  [ERROR] heading classification failed after retries; using 'other'.")
    return {h: "other" for h in headings_list}


# ============================Export figures and tables==============================
def export_figures(pdf_path, output_base_dir="exported_figures"):
    """Export figures and tables from a PDF."""
    doc_name = os.path.basename(pdf_path).replace(".pdf", "")
    output_dir = Path(output_base_dir) / doc_name
    
    images_dir = output_dir / "images"
    tables_dir = output_dir / "tables"
    
    images_dir.mkdir(parents=True, exist_ok=True)
    tables_dir.mkdir(parents=True, exist_ok=True)
    
    print(f"Getting figures from {pdf_path}...")
    
    img_converter = get_image_export_converter()
    conv_res = img_converter.convert(pdf_path)
    
    table_counter = 0
    picture_counter = 0
    for element, _level in conv_res.document.iterate_items():
        if isinstance(element, TableItem):
            table_counter += 1
            element_image_filename = tables_dir / f"{doc_name}-table-{table_counter}.png"
            with element_image_filename.open("wb") as fp:
                element.get_image(conv_res.document).save(fp, "PNG")
        
        if isinstance(element, PictureItem):
            picture_counter += 1
            element_image_filename = images_dir / f"{doc_name}-picture-{picture_counter}.png"
            with element_image_filename.open("wb") as fp:
                element.get_image(conv_res.document).save(fp, "PNG")
    
    print(f"Exported {picture_counter} images and {table_counter} tables to {output_dir}")
    return output_dir

# ============================Load existing chunks to get last chunk_id==============================
def get_last_chunk_id(jsonl_path):
    if not os.path.exists(jsonl_path):
        return 0
    with open(jsonl_path, "r") as f:
        lines = f.readlines()
        if not lines:
            return 0
        last = json.loads(lines[-1])
        return last["chunk_id"] + 1
    
def get_last_chunk_id_fallback(jsonl_path):
    if os.path.exists(jsonl_path):
        return get_last_chunk_id(jsonl_path)
    return 0

# ============================Find all PDFs recursively(this is used only when we have multiple subfolders with pdfs)==============================
def find_all_pdfs(root_folder):
    """Recursively find all PDF files in subdirectories."""
    pdf_files = []
    root_path = Path(root_folder)
    
    # Use rglob to recursively find all PDFs
    for pdf_path in root_path.rglob("*.pdf"):
        pdf_files.append(pdf_path)
    
    return sorted(pdf_files)

# =================================== CHUNKING ALL PDFs using docling==============================
def process_folder(folder_path=None, file_path=None, starting_chunk_id=0, export_images=False, recursive=True):
    all_chunks = []
    chunk_id = starting_chunk_id

    files_to_process = []
    if file_path:
        files_to_process.append(Path(file_path))
    elif folder_path:
        if recursive:
            files_to_process = find_all_pdfs(folder_path)
        else:
            files_to_process = [Path(p) for p in glob.glob(os.path.join(folder_path, "*.pdf"))]

    for idx, pdf_path in enumerate(files_to_process, 1):
        print(f"[{idx}/{len(files_to_process)}] Processing: {pdf_path.name}")
        
        if export_images:
            export_figures(str(pdf_path))
        
        doc_name = pdf_path.stem
        category = pdf_path.parent.name or "root"
        
        try:
            doc = converter.convert(source=str(pdf_path)).document
            
            # 1. First Pass: Collect all unique headings for this document
            doc_chunks_raw = []
            unique_headings = set()
            
            for chunk in chunker.chunk(dl_doc=doc):
                ser_txt = chunker.contextualize(chunk=chunk)
                if "\n" in ser_txt:
                    heading, _, body = ser_txt.partition("\n")
                    heading = heading.strip()
                else:
                    # Some chunks may not have a heading line at all.
                    heading = ""
                    body = ser_txt
                
                # Filter out junk/empty headings before classifying
                if len(heading) > 3:
                    unique_headings.add(heading)
                
                doc_chunks_raw.append({
                    "heading": heading,
                    "body": body,
                    "token_count": tokenizer.count_tokens(ser_txt)
                })

            # 2. Second Pass: Get Semantic Mapping from LLM
            print(f"  → Classifying {len(unique_headings)} unique headings...")
            heading_map = get_semantic_heading_map(list(unique_headings))

            # 3. Third Pass: Assemble final chunks with semantic tags
            for raw in doc_chunks_raw:
                all_chunks.append({
                    "chunk_id": chunk_id,
                    "doc_name": doc_name,
                    "category": category,
                    "section_heading": raw["heading"], # Original Header
                    "semantic_category": heading_map.get(raw["heading"], "other"), # New Category
                    "text": raw["body"],
                    "token_count": raw["token_count"]
                })
                chunk_id += 1
                
        except Exception as e:
            print(f"Error processing {pdf_path}: {e}")
            continue

    return all_chunks

# =================================================RUN ===================================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Chunk and store PDF data into a JSONL knowledge base.")
    parser.add_argument("--folder", "-F", type=str, help="Path to the folder with PDFs.")
    parser.add_argument("--file", "-f", type=str, help="Path to a single PDF file to process.")
    parser.add_argument("--output", "-o", type=str, default=DEFAULT_OUTPUT_JSONL, help="Output JSONL path.")
    parser.add_argument("--append", "-a", action="store_true", help="Append to existing JSONL and continue chunk IDs.")
    parser.add_argument("--export-images", "-e", action="store_true", help="Export images and tables from PDFs.")
    parser.add_argument("--no-recursive", "-nr", action="store_true", help="Don't process subdirectories recursively.")
    args = parser.parse_args()

    FOLDER_PATH = args.folder
    FILE_PATH = args.file
    OUTPUT_JSONL = args.output
    append_mode = args.append
    recursive = not args.no_recursive

    if bool(FOLDER_PATH) == bool(FILE_PATH):
        parser.error("Provide exactly one of --folder/-F or --file/-f.")
    if FILE_PATH and not Path(FILE_PATH).is_file():
        parser.error(f"File not found: {FILE_PATH}")
    if FOLDER_PATH and not Path(FOLDER_PATH).is_dir():
        parser.error(f"Folder not found: {FOLDER_PATH}")
    
    start_id = get_last_chunk_id_fallback(OUTPUT_JSONL) if append_mode else 0
    new_chunks = process_folder(FOLDER_PATH, FILE_PATH, start_id, export_images=args.export_images, recursive=recursive)

    write_mode = "a" if append_mode else "w"
    with open(OUTPUT_JSONL, write_mode) as f:
        for chunk in new_chunks:
            f.write(json.dumps(chunk) + "\n")

    print(f"\n✓ {len(new_chunks)} chunks written to {OUTPUT_JSONL}")
    
    if new_chunks:
        from collections import Counter
        categories = Counter(chunk["category"] for chunk in new_chunks)
        print("\nChunks per category:")
        for cat, count in sorted(categories.items()):
            print(f"  {cat}: {count}")
