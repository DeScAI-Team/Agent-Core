# Testing Guide for Article Uploader

## Prerequisites

1. **Arweave Wallet Setup**
   - Ensure `../../Arweave-Cli/.env` exists and contains `WALLET_PATH`
   - Wallet must have sufficient AR balance for uploads

2. **Node.js Dependencies**
   ```bash
   cd articles/article_uploader
   npm install
   ```

3. **Article Output Directory**
   - Must contain: `evidence_audit.md`, `review.json`, `overview.json`
   - Example: `articles/data/Sleep Fragmentation.../output/`

## Test Cases

### 1. Test Node.js CLI (Basic)

Test with a simple text file:

```bash
cd articles/article_uploader
echo "Test content" > test.txt
node upload_cli.js test.txt
```

Expected output:
- JSON response with `success`, `txId`, and `webUrl`
- Console messages showing upload progress

### 2. Test Node.js CLI (With Tags)

```bash
cd articles/article_uploader
node upload_cli.js test.txt --tags '[{"name":"doctype","value":"test"},{"name":"platform","value":"descai"}]'
```

Expected output:
- JSON response including tags
- Tags should be visible in Arweave GraphQL queries

### 3. Test Python Uploader (Dry Run)

Test import and validation without uploading:

```bash
cd articles/article_uploader
python uploader.py --output-dir "../../test-data/mock-output"
```

Expected output:
- Should fail gracefully if files are missing
- Clear error message about missing files

### 4. Test Python Uploader (Full Sequence)

With a real article output directory:

```bash
cd articles/article_uploader
python uploader.py --output-dir "../data/Sleep Fragmentation in Neuronal Aβ42-Expressing Drosophila Replication Study (Stage 1 Registered Report)/output"
```

Expected output:
1. Upload evidence_audit.md → txid1
2. Create and upload modified review.json → txid2
3. Create and upload modified overview.json → txid3
4. Save `upload_metadata.json` with all transaction IDs

### 5. Test Resume Functionality

Simulate a failed upload by interrupting Step 2, then:

```bash
cd articles/article_uploader
python uploader.py --output-dir "../data/[article-name]/output" --resume
```

Expected output:
- Should skip evidence_audit upload (already done)
- Resume from review.json upload

### 6. Test Pipeline Integration

Run the full pipeline with upload:

```bash
cd articles/pipeline
python run_full_pipeline.py "path/to/article.pdf"
```

Expected output:
- Pipeline runs through all steps
- Upload step executes at the end
- Can skip with `--skip-upload` flag

### 7. Test Pipeline with Skip Upload

```bash
cd articles/pipeline
python run_full_pipeline.py "path/to/article.pdf" --skip-upload
```

Expected output:
- Pipeline completes without upload
- Message: "Skipped: Arweave upload (--skip-upload)"

## Validation Checks

### Verify Transaction IDs

After upload, check that txids are valid Arweave transaction IDs:
- 43 characters long
- Base64url encoded
- Example: `abc123XYZ-_456def`

### Verify Links in Modified Files

Check `upload_metadata.json`:
```json
{
  "evidence_audit": {
    "txid": "...",
    "url": "https://arweave.net/..."
  },
  "review": {
    "txid": "...",
    "url": "https://arweave.net/...",
    "descai_url": "https://descai.net/review/..."
  }
}
```

### Verify Tags on Arweave

Query Arweave GraphQL to check tags:

```graphql
query {
  transactions(
    tags: [
      { name: "doctype", values: ["evidence"] }
      { name: "research_name", values: ["Your Study Name"] }
    ]
  ) {
    edges {
      node {
        id
        tags {
          name
          value
        }
      }
    }
  }
}
```

### Verify Original Files Unchanged

After upload, verify that original files remain unchanged:
```bash
# Original files should have no txid links
cat "../data/[article]/output/review.json" | grep "arweave.net"
# Should return nothing

# Only modified versions (temporary) had links
# They should be cleaned up after upload
```

## Troubleshooting

### Error: Missing WALLET_PATH

```
Error: Missing WALLET_PATH in .env
```

**Solution:** Configure `../../Arweave-Cli/.env` with valid wallet path

### Error: Missing required files

```
Missing required files in [...]: evidence_audit.md
```

**Solution:** Ensure output directory has all three required files

### Error: Upload failed

```
Upload failed: insufficient funds
```

**Solution:** Add AR tokens to the wallet

### Error: Invalid JSON response

**Solution:** Check Node.js installation and module versions

## Performance Notes

- Typical upload times:
  - evidence_audit.md (50KB): ~5 seconds
  - review.json (5KB): ~3 seconds
  - overview.json (5KB): ~3 seconds
- Total sequence: ~15-20 seconds per article

## Security Considerations

- Never commit wallet files
- Store `.env` securely
- Review transaction costs before batch uploads
- Original files are never modified (safety first)
