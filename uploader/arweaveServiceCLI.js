import { upload, retrieve } from './arweaveService.js';
import fs from 'fs';

/**
 * Usage:
 *   node arweaveServiceCLI.js <filePath> [--json] [--out <downloadPath>] [--tag name=value ...]
 * Legacy: node arweaveServiceCLI.js <filePath> <downloadPath>
 */
function parseArgs(argv) {
  const tokens = argv.slice(2);
  const tags = [];
  let outPath = null;
  let jsonOut = false;
  const positionals = [];
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    if (t === '--json') {
      jsonOut = true;
      continue;
    }
    if (t === '--out' && i + 1 < tokens.length) {
      outPath = tokens[++i];
      continue;
    }
    if (t === '--tag' && i + 1 < tokens.length) {
      const raw = tokens[++i];
      const eq = raw.indexOf('=');
      if (eq > 0) {
        tags.push({ name: raw.slice(0, eq), value: raw.slice(eq + 1) });
      }
      continue;
    }
    if (!t.startsWith('-')) {
      positionals.push(t);
    }
  }
  let filePath = positionals[0] ?? null;
  if (!outPath && positionals.length >= 2) {
    outPath = positionals[1];
  }
  return { filePath, outPath, tags, jsonOut };
}

const { filePath, outPath: outputFile, tags, jsonOut } = parseArgs(process.argv);

if (!filePath) {
  console.error(
    'Error: Please provide a file path. Optional: --json [--out <path>] [--tag name=value ...]',
  );
  process.exit(1);
}

async function runUpload() {
  console.error('--- Starting Upload ---');
  const uploadInfo = await upload(filePath, { tags });

  if (jsonOut) {
    console.log(JSON.stringify(uploadInfo));
  } else if (uploadInfo.success) {
    console.log(`File is live at: ${uploadInfo.webUrl}`);
  }

  if (!uploadInfo.success) {
    process.exit(1);
  }

  if (outputFile) {
    console.error('\n--- Starting Retrieval ---');
    const download = await retrieve(uploadInfo.txId);

    if (download.success && outputFile !== null) {
      fs.writeFileSync(outputFile, Buffer.from(download.data));
      console.error('File written successfully to:', outputFile);
    }
  }
}

runUpload();
