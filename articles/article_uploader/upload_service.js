import dotenv from 'dotenv';
import {TurboFactory, ArweaveSigner} from '@ardrive/turbo-sdk';
import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

// Load environment from Arweave-Cli/.env
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const envPath = path.resolve(__dirname, '../../Arweave-Cli/.env');
dotenv.config({ path: envPath });

// Access wallet
const WALLET_PATH = process.env.WALLET_PATH;

if (!WALLET_PATH) {
  throw new Error("Missing WALLET_PATH in .env - please configure ../../Arweave-Cli/.env");
}

// Load Arweave Wallet
const jwk = JSON.parse(fs.readFileSync(WALLET_PATH, 'utf-8'));
const signer = new ArweaveSigner(jwk);
const turbo = TurboFactory.authenticated({signer});

/**
 * Detect Content-Type based on file extension
 * @param {string} filePath - Path to file
 * @returns {string} - MIME type
 */
function detectContentType(filePath) {
  const ext = path.extname(filePath).toLowerCase();
  const mimeTypes = {
    '.json': 'application/json',
    '.md': 'text/markdown',
    '.txt': 'text/plain',
    '.html': 'text/html',
    '.pdf': 'application/pdf',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
  };
  return mimeTypes[ext] || 'application/octet-stream';
}

/**
 * Uploads file with optional tags and returns transaction ID and Web URL
 * @param {string} filePath - Path to file to be uploaded
 * @param {Object} dataItemOpts - Optional data item options including tags
 * @param {Array<{name: string, value: string}>} dataItemOpts.tags - Array of tag objects
 * @returns {Promise<{success: boolean, txId?: string, webUrl?: string, error?: string}>}
 */
export async function upload(filePath, dataItemOpts = {}) {
    try {
        // Validate file exists and is readable
        fs.accessSync(filePath, fs.constants.R_OK);
        if(!fs.statSync(filePath).isFile()){
            throw new Error('Path is not a file!');
        }
    } catch(err) {
        throw new Error('Invalid file path!');
    }
    
    try {
        const fileSize = fs.statSync(filePath).size;
        
        // Auto-detect Content-Type if not provided in tags
        let tags = dataItemOpts.tags || [];
        const hasContentType = tags.some(tag => tag.name === 'Content-Type');
        if (!hasContentType) {
            const contentType = detectContentType(filePath);
            tags = [{name: 'Content-Type', value: contentType}, ...tags];
        }
        
        // Build final dataItemOpts with tags
        const finalOpts = {
            ...dataItemOpts,
            tags
        };

        const uploadResult = await turbo.uploadFile({
            fileStreamFactory: () => fs.createReadStream(filePath),
            fileSizeFactory: () => fileSize,
            dataItemOpts: finalOpts
        });
    
        const txId = uploadResult.id;
        const webUrl = `https://arweave.net/${txId}`;

        return {
            success: true,
            txId,
            webUrl
        };
    } catch(error) {
        console.error('Upload failed: ', error.message);
        return {success: false, error: error.message};
    }
}

/**
 * Retrieves file from Arweave by transaction ID
 * @param {string} txId - Arweave transaction ID
 * @returns {Promise<{success: boolean, data?: ArrayBuffer, error?: string}>}
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
            data: data,
        };
    } catch(error) {
        console.error('Retrieval failed:', error.message);
        return {success: false, error: error.message};
    }
}
