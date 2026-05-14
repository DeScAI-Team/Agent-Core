#!/usr/bin/env node
import {upload} from './upload_service.js';
import fs from 'fs';

// Command Line Arguments
const args = process.argv.slice(2);

// Parse arguments
let filePath = null;
let tags = [];

for (let i = 0; i < args.length; i++) {
  if (args[i] === '--file' || args[i] === '-f') {
    filePath = args[i + 1];
    i++;
  } else if (args[i] === '--tags' || args[i] === '-t') {
    try {
      tags = JSON.parse(args[i + 1]);
      if (!Array.isArray(tags)) {
        console.error('Error: --tags must be a JSON array of tag objects');
        process.exit(1);
      }
    } catch (err) {
      console.error('Error: Invalid JSON for --tags argument');
      console.error(err.message);
      process.exit(1);
    }
    i++;
  } else if (!filePath && !args[i].startsWith('--')) {
    // First non-flag argument is the file path
    filePath = args[i];
  }
}

// Validate required arguments
if (!filePath) {
  console.error('Error: Please provide a file path to upload.');
  console.error('Usage: node upload_cli.js <file> [--tags <json-array>]');
  console.error('Example: node upload_cli.js myfile.json --tags \'[{"name":"doctype","value":"review"}]\'');
  process.exit(1);
}

async function runUpload() {
  try {
    console.error('--- Starting Upload ---');
    console.error(`File: ${filePath}`);
    if (tags.length > 0) {
      console.error(`Tags: ${JSON.stringify(tags, null, 2)}`);
    }
    
    const dataItemOpts = tags.length > 0 ? { tags } : {};
    const uploadInfo = await upload(filePath, dataItemOpts);
    
    // Write result to stdout as JSON (for parsing by Python)
    console.log(JSON.stringify(uploadInfo, null, 2));
    
    if (uploadInfo.success) {
      console.error(`✓ Upload successful!`);
      console.error(`  TX ID: ${uploadInfo.txId}`);
      console.error(`  URL: ${uploadInfo.webUrl}`);
      process.exit(0);
    } else {
      console.error(`✗ Upload failed: ${uploadInfo.error}`);
      process.exit(1);
    }
  } catch (error) {
    console.error('✗ Unexpected error:', error.message);
    console.log(JSON.stringify({ success: false, error: error.message }, null, 2));
    process.exit(1);
  }
}

runUpload();
