import * as fs from "node:fs/promises"
import * as path from "node:path"
import { getProjectDataRoomFiles } from "./molecule-lib-bridge.js"
import {
  ensureProjectLayout,
  PROFILE_FILENAME,
  resolveProfileJsonPath,
} from "./ipnft-layout.js"
const MANIFEST_FILENAME = "manifest.json"
const DATAROOM_FILENAME = "dataroom.json"

interface DataRoomFile {
  did: string
  path: string
  version: number
  contentType: string
  accessLevel: string
  description: string | null
  tags: string[]
  categories: string[]
  downloadUrl: string
}

interface ManifestEntry {
  description: string
  path: string
  tags: string[]
  fileName: string
  error?: string
}

export interface DataBundleOptions {
  profilesDir: string
  delayMs: number
  max?: number
  dryRun: boolean
  /** Top-level profile folder names to skip (incremental crawl from crawl-log). */
  skipFolderNames?: ReadonlySet<string>
}

/** MIME base type → file extension (without dot). */
const CONTENT_TYPE_EXTENSION: Record<string, string> = {
  "application/pdf": "pdf",
  "application/x-pdf": "pdf",
  "video/mp4": "mp4",
  "video/quicktime": "mov",
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/gif": "gif",
  "image/webp": "webp",
}

function extensionFromContentType(contentType: string): string | undefined {
  const base = contentType.split(";")[0]?.trim().toLowerCase()
  if (!base) return undefined
  if (CONTENT_TYPE_EXTENSION[base]) return CONTENT_TYPE_EXTENSION[base]
  const slash = base.indexOf("/")
  if (slash === -1) return undefined
  const subtype = base.slice(slash + 1).replace(/^x-/, "")
  return /^[a-z0-9.+-]+$/.test(subtype) ? subtype : undefined
}

function deriveFileName(filePath: string, contentType: string): string {
  let name = filePath.replace(/^\/+/, "").replace(/\//g, "_")
  const hasExtension = /\.[a-zA-Z0-9]{1,10}$/.test(name)
  if (!hasExtension) {
    const ext = extensionFromContentType(contentType)
    if (ext) name += `.${ext}`
  }
  return name
}

async function sleep(ms: number): Promise<void> {
  await new Promise((r) => setTimeout(r, ms))
}

/** Prefer output/; accept legacy binaries left in metadata/. */
async function resolveDownloadPath(
  contentDir: string,
  metadataDir: string,
  fileName: string,
): Promise<string> {
  const primary = path.join(contentDir, fileName)
  try {
    await fs.access(primary)
    return primary
  } catch {
    const legacy = path.join(metadataDir, fileName)
    try {
      await fs.access(legacy)
      return legacy
    } catch {
      return primary
    }
  }
}

async function downloadFile(url: string, dest: string): Promise<void> {
  const res = await fetch(url)
  if (!res.ok) {
    throw new Error(`HTTP ${res.status} ${res.statusText}`)
  }
  const buf = Buffer.from(await res.arrayBuffer())
  await fs.writeFile(dest, buf)
}

export async function runDataBundle(opts: DataBundleOptions): Promise<void> {
  const resolvedDir = path.resolve(opts.profilesDir)

  let entries: string[]
  try {
    entries = await fs.readdir(resolvedDir)
  } catch {
    throw new Error(`Cannot read profiles directory: ${resolvedDir}`)
  }

  const profileDirs: { dir: string; folderName: string; tokenId: string }[] = []

  for (const entry of entries) {
    const projectDir = path.join(resolvedDir, entry)
    const profilePath = await resolveProfileJsonPath(projectDir)
    if (!profilePath) continue
    try {
      const raw = await fs.readFile(profilePath, "utf-8")
      const profile = JSON.parse(raw)
      const ipnftId = profile?.ipnft?.id
      if (!ipnftId) continue
      profileDirs.push({
        dir: projectDir,
        folderName: entry,
        tokenId: String(ipnftId),
      })
    } catch {
      // unreadable profile — skip
    }
  }

  const slice = opts.max !== undefined ? profileDirs.slice(0, opts.max) : profileDirs

  process.stderr.write(
    `Found ${profileDirs.length} profile(s) with valid ipnft.id; processing ${slice.length}.\n`,
  )

  if (profileDirs.length === 0) {
    process.stderr.write(
      `Hint: data-bundle only uses metadata/profile.json where ipnft.id is set. ` +
        `Profiles with null ipnft or fetch errors have no id, so the second step skips them.\n`,
    )
  }

  let totalDownloaded = 0
  let totalSkipped = 0
  let totalErrors = 0

  for (let i = 0; i < slice.length; i++) {
    const { dir, folderName, tokenId } = slice[i]
    process.stderr.write(
      `[${i + 1}/${slice.length}] ${folderName} (token ${tokenId}) ... `,
    )

    if (opts.skipFolderNames?.has(folderName)) {
      process.stderr.write(`skipped (crawl_log)\n`)
      if (opts.delayMs) await sleep(opts.delayMs)
      continue
    }

    let files: DataRoomFile[]
    try {
      files = await getProjectDataRoomFiles(tokenId)
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e)
      process.stderr.write(`dataroom error: ${msg}\n`)
      if (opts.delayMs) await sleep(opts.delayMs)
      continue
    }

    const meta = await ensureProjectLayout(dir)
    const dataroomPath = path.join(meta.metadata, DATAROOM_FILENAME)
    const dataroomPayload = {
      tokenId,
      fileCount: files.length,
      publicCount: files.filter((f) => f.accessLevel === "PUBLIC").length,
      files,
    }
    await fs.writeFile(dataroomPath, JSON.stringify(dataroomPayload, null, 2), "utf-8")

    const publicFiles = files.filter((f) => f.accessLevel === "PUBLIC")

    const seenDescriptions = new Set<string>()
    const deduped: DataRoomFile[] = []
    for (const f of publicFiles) {
      const key = (f.description ?? "").trim()
      if (key.length > 0 && seenDescriptions.has(key)) continue
      if (key.length > 0) seenDescriptions.add(key)
      deduped.push(f)
    }

    const manifest: ManifestEntry[] = []

    if (opts.dryRun) {
      for (const f of deduped) {
        const fileName = deriveFileName(f.path, f.contentType)
        manifest.push({
          description: f.description ?? "",
          path: f.path,
          tags: f.tags,
          fileName,
        })
        process.stderr.write(`  [dry-run] ${fileName}\n`)
      }
      process.stderr.write(
        `${deduped.length} file(s) would be downloaded\n`,
      )
    } else {
      for (const f of deduped) {
        const fileName = deriveFileName(f.path, f.contentType)
        const destPath = await resolveDownloadPath(
          meta.output,
          meta.metadata,
          fileName,
        )

        let alreadyExists = false
        try {
          await fs.access(destPath)
          alreadyExists = true
        } catch {
          // doesn't exist yet
        }

        if (alreadyExists) {
          manifest.push({
            description: f.description ?? "",
            path: f.path,
            tags: f.tags,
            fileName,
          })
          totalSkipped++
          continue
        }

        try {
          const writePath = path.join(meta.output, fileName)
          await downloadFile(f.downloadUrl, writePath)
          manifest.push({
            description: f.description ?? "",
            path: f.path,
            tags: f.tags,
            fileName,
          })
          totalDownloaded++
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e)
          manifest.push({
            description: f.description ?? "",
            path: f.path,
            tags: f.tags,
            fileName,
            error: msg,
          })
          totalErrors++
          process.stderr.write(`  download error (${fileName}): ${msg}\n`)
        }
      }

      const manifestPath = path.join(meta.metadata, MANIFEST_FILENAME)
      await fs.writeFile(manifestPath, JSON.stringify(manifest, null, 2), "utf-8")
      process.stderr.write(
        `${deduped.length} public, ${manifest.length} in manifest\n`,
      )
    }

    if (opts.delayMs) await sleep(opts.delayMs)
  }

  process.stderr.write(
    opts.dryRun
      ? `Dry run complete.\n`
      : `Done. ${totalDownloaded} downloaded, ${totalSkipped} already existed, ${totalErrors} errors.\n`,
  )
}
