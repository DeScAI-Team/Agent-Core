import * as fs from "node:fs/promises"
import * as path from "node:path"

/**
 * Per-IPNFT layout:
 *   metadata/ — JSON only (profile, links, dataroom, bundle + crawl manifests)
 *   output/   — downloaded binaries and crawled content (.md, pdf, mp4, tweets.json)
 */
export const METADATA_DIR = "metadata"
export const OUTPUT_DIR = "output"

export const PROFILE_FILENAME = "profile.json"
export const LINKS_FILENAME = "links.json"

export function metadataDir(projectDir: string): string {
  return path.join(projectDir, METADATA_DIR)
}

export function outputDir(projectDir: string): string {
  return path.join(projectDir, OUTPUT_DIR)
}

export async function ensureProjectLayout(projectDir: string): Promise<{
  metadata: string
  output: string
}> {
  const meta = metadataDir(projectDir)
  const out = outputDir(projectDir)
  await fs.mkdir(meta, { recursive: true })
  await fs.mkdir(out, { recursive: true })
  return { metadata: meta, output: out }
}

export function profileJsonPath(projectDir: string): string {
  return path.join(metadataDir(projectDir), PROFILE_FILENAME)
}

export function linksJsonPath(projectDir: string): string {
  return path.join(metadataDir(projectDir), LINKS_FILENAME)
}

/** Prefer metadata/profile.json; fall back to legacy root profile.json. */
export async function resolveProfileJsonPath(projectDir: string): Promise<string | null> {
  const metaPath = profileJsonPath(projectDir)
  try {
    await fs.access(metaPath)
    return metaPath
  } catch {
    const legacy = path.join(projectDir, PROFILE_FILENAME)
    try {
      await fs.access(legacy)
      return legacy
    } catch {
      return null
    }
  }
}

export async function resolveMetadataScanDir(projectDir: string): Promise<string> {
  const meta = metadataDir(projectDir)
  try {
    await fs.access(meta)
    return meta
  } catch {
    return projectDir
  }
}

export async function projectHasMetadata(projectDir: string): Promise<boolean> {
  const profile = await resolveProfileJsonPath(projectDir)
  if (profile) return true
  try {
    await fs.access(linksJsonPath(projectDir))
    return true
  } catch {
    try {
      await fs.access(path.join(projectDir, LINKS_FILENAME))
      return true
    } catch {
      return false
    }
  }
}
