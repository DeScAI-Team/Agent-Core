import fs from "node:fs"
import path from "node:path"
import { fileURLToPath } from "node:url"
import dotenv from "dotenv"

const moduleDir = path.dirname(fileURLToPath(import.meta.url))
const repoRoot = path.resolve(moduleDir, "..", "..", "..", "..")
const envPath = path.join(repoRoot, ".env")

if (fs.existsSync(envPath)) {
  dotenv.config({ path: envPath, override: false })
}
