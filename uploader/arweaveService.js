import dotenv from 'dotenv';
import { TurboFactory, ArweaveSigner } from '@ardrive/turbo-sdk';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.join(__dirname, '..');

dotenv.config({ path: path.join(REPO_ROOT, '.env') });

function resolveKeyfilePath() {
  const raw = process.env.PATH_TO_KEYFILE;
  if (!raw) {
    throw new Error('Missing PATH_TO_KEYFILE in repo-root .env');
  }
  return path.isAbsolute(raw) ? raw : path.join(REPO_ROOT, raw);
}

const WALLET_PATH = resolveKeyfilePath();

if (!fs.existsSync(WALLET_PATH)) {
  throw new Error(`Keyfile not found: ${WALLET_PATH}`);
}

const jwk = JSON.parse(fs.readFileSync(WALLET_PATH, 'utf-8'));
const signer = new ArweaveSigner(jwk);
const turbo = TurboFactory.authenticated({ signer });

/**
 * Uploads file and returns transaction ID and Web URL
 * @param {string} filePath - Path to file to be uploaded
 * @param {{ tags?: { name: string; value: string }[] }} [options]
 */
export async function upload(filePath, options = {}) {
  try {
    fs.accessSync(filePath, fs.constants.R_OK);
    if (!fs.statSync(filePath).isFile()) {
      throw new Error('Path is not a file!');
    }
  } catch {
    throw new Error('Invalid file Path!');
  }
  try {
    const fileSize = fs.statSync(filePath).size;

    const uploadParams = {
      fileStreamFactory: () => fs.createReadStream(filePath),
      fileSizeFactory: () => fileSize,
    };
    const tags = options.tags;
    if (Array.isArray(tags) && tags.length > 0) {
      uploadParams.dataItemOpts = { tags };
    }

    const uploadResult = await turbo.uploadFile(uploadParams);

    const txId = uploadResult.id;
    const webUrl = `https://arweave.net/${txId}`;

    return {
      success: true,
      txId,
      webUrl,
    };
  } catch (error) {
    console.error('Upload failed: ', error.message);
    return { success: false, error: error.message };
  }
}

/**
 * Retrieves file data from Arweave by transaction ID
 * @param {string} txId - Arweave transaction ID
 */
export async function retrieve(txId) {
  try {
    const gatewayUrl = `https://arweave.net/${txId}`;

    const response = await fetch(gatewayUrl);

    if (!response.ok) {
      throw new Error(`Failed to fetch: ${response.statusText}`);
    }

    const data = await response.arrayBuffer();

    return {
      success: true,
      data,
    };
  } catch (error) {
    console.error('Retrieval failed:', error.message);
    return { success: false, error: error.message };
  }
}
