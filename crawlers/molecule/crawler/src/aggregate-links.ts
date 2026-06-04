import * as fs from "node:fs/promises"
import * as path from "node:path"
import {
  ensureProjectLayout,
  linksJsonPath,
  metadataDir,
  resolveMetadataScanDir,
} from "./ipnft-layout.js"

const LINKS_FILENAME = "links.json"
const DATAROOM_FILENAME = "dataroom.json"
const MANIFEST_FILENAME = "manifest.json"

const URL_PREFIX_RE = /^(https?:\/\/|ipfs:\/\/)/i

/** JSON field names whose URLs are already fetched by profile or data-bundle steps. */
const ALREADY_PULLED_TITLES = new Set(["downloadUrl", "tokenUri"])

/** JSON field names for catalog / API references, not project content to crawl. */
const SKIP_LINK_TITLES = new Set(["dataApi"])

/** JSON sources that only describe assets the data-bundle step already downloads. */
const SKIP_LINK_SOURCE_DOCS = new Set([DATAROOM_FILENAME, MANIFEST_FILENAME])

export interface AggregatedLink {
  url: string
  doc: string
  title: string
  siteName: string | null
  http_accessible: "yes" | "no"
}

export interface AggregateLinksOptions {
  ipnftsDir: string
  /** Probe each unique URL with HEAD/GET (default true). */
  validateLinks?: boolean
}

const HTTP_CHECK_TIMEOUT_MS = 8_000
const IPFS_CHECK_TIMEOUT_MS = 25_000
const HTTP_CHECK_USER_AGENT = "Review-Generator-LinkCheck/1.0"

const IPFS_GATEWAY_TEMPLATES: ((cid: string) => string)[] = [
  (cid) => `https://ipfs.io/ipfs/${cid}`,
  (cid) => `https://dweb.link/ipfs/${cid}`,
  (cid) => `https://gateway.pinata.cloud/ipfs/${cid}`,
  (cid) => `https://cloudflare-ipfs.com/ipfs/${cid}`,
  (cid) => `https://w3s.link/ipfs/${cid}`,
  (cid) => `https://4everland.io/ipfs/${cid}`,
]

export function isIpfsUrl(raw: string): boolean {
  return /^ipfs:\/\//i.test(raw.trim())
}

/** CID path from an ipfs:// URL (may include nested path segments). */
export function ipfsCidFromUrl(raw: string): string | null {
  const trimmed = raw.trim()
  if (!isIpfsUrl(trimmed)) return null
  const cidPath = trimmed.replace(/^ipfs:\/\//i, "").replace(/^\/+/, "").replace(/\/+$/, "")
  return cidPath.length > 0 ? cidPath : null
}

export function ipfsGatewayUrls(raw: string): string[] {
  const cidPath = ipfsCidFromUrl(raw)
  if (!cidPath) return []
  return IPFS_GATEWAY_TEMPLATES.map((build) => build(cidPath))
}

function isUrlLike(value: string): boolean {
  return URL_PREFIX_RE.test(value.trim())
}

/** Last non-array-index key in a JSON path (e.g. `links` for `iptTokens.0.links.2`). */
function titleFeature(pathKeys: string[]): string {
  for (let i = pathKeys.length - 1; i >= 0; i--) {
    if (!/^\d+$/.test(pathKeys[i])) return pathKeys[i]
  }
  return pathKeys[pathKeys.length - 1] ?? "unknown"
}

export function siteNameFromUrl(raw: string): string | null {
  const trimmed = raw.trim()
  if (/^ipfs:\/\//i.test(trimmed)) return "ipfs"
  try {
    const host = new URL(trimmed).hostname.toLowerCase()
    if (!host) return null
    return host.startsWith("www.") ? host.slice(4) : host
  } catch {
    return null
  }
}

const X_HOSTS = new Set([
  "x.com",
  "twitter.com",
  "mobile.x.com",
  "mobile.twitter.com",
])

function isXHost(hostname: string): boolean {
  const host = hostname.toLowerCase()
  const bare = host.startsWith("www.") ? host.slice(4) : host
  return X_HOSTS.has(bare)
}

/** Rewrite x.com / twitter.com profile and status URLs to nitter.net for crawling. */
export function rewriteXCrawlUrl(raw: string): string {
  const trimmed = raw.trim()
  if (!/^https?:\/\//i.test(trimmed)) return trimmed
  try {
    const parsed = new URL(trimmed)
    if (!isXHost(parsed.hostname)) return trimmed
    parsed.hostname = "nitter.net"
    parsed.protocol = "https:"
    return parsed.href
  } catch {
    return trimmed
  }
}

function isEtherscanHost(hostname: string): boolean {
  const bare = hostname.toLowerCase().replace(/^www\./, "")
  return bare === "etherscan.io" || bare.endsWith(".etherscan.io")
}

function isBareMoleculeHomepage(url: string): boolean {
  try {
    const parsed = new URL(url.trim())
    const host = parsed.hostname.toLowerCase().replace(/^www\./, "")
    if (host !== "molecule.xyz") return false
    const path = parsed.pathname.replace(/\/+$/, "")
    return path === ""
  } catch {
    return false
  }
}

/** Skip block explorers, catalog homepages, and static Molecule API docs. */
export function shouldSkipCrawlLink(url: string, title: string): boolean {
  if (SKIP_LINK_TITLES.has(title)) return true
  if (!/^https?:\/\//i.test(url.trim())) return false
  if (isBareMoleculeHomepage(url)) return true
  try {
    return isEtherscanHost(new URL(url.trim()).hostname)
  } catch {
    return false
  }
}

interface CollectedLink {
  url: string
  doc: string
  title: string
  siteName: string | null
}

function finalizeCollectedLink(url: string, doc: string, title: string): CollectedLink {
  const rewritten = rewriteXCrawlUrl(url)
  return {
    url: rewritten,
    doc,
    title,
    siteName: siteNameFromUrl(rewritten),
  }
}

/** Map ipfs:// URLs to a public HTTP gateway for non-IPFS reachability checks. */
export function urlForHttpCheck(raw: string): string {
  const trimmed = raw.trim()
  if (isIpfsUrl(trimmed)) {
    const cidPath = ipfsCidFromUrl(trimmed)
    if (cidPath) return `https://ipfs.io/ipfs/${cidPath}`
  }
  return trimmed
}

async function responseHasData(
  res: Response,
): Promise<boolean> {
  if (res.status < 200 || res.status >= 400) return false
  const reader = res.body?.getReader()
  if (!reader) return false
  try {
    const { value, done } = await reader.read()
    return done || (value !== undefined && value.byteLength > 0)
  } finally {
    await reader.cancel().catch(() => {})
  }
}

async function gatewayHasData(
  gatewayUrl: string,
  timeoutMs: number,
): Promise<boolean> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const baseInit: RequestInit = {
    signal: controller.signal,
    redirect: "follow",
    headers: { "User-Agent": HTTP_CHECK_USER_AGENT },
  }

  try {
    let res = await fetch(gatewayUrl, {
      ...baseInit,
      method: "GET",
      headers: {
        ...baseInit.headers,
        Range: "bytes=0-4095",
      },
    })
    if (res.status === 416 || res.status === 405 || res.status === 501) {
      res = await fetch(gatewayUrl, { ...baseInit, method: "GET" })
    }
    return await responseHasData(res)
  } catch {
    return false
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Probe public IPFS gateways in parallel; true when any gateway returns bytes.
 */
export async function checkIpfsDataAccessible(
  raw: string,
  timeoutMs = IPFS_CHECK_TIMEOUT_MS,
): Promise<boolean> {
  const gateways = ipfsGatewayUrls(raw)
  if (gateways.length === 0) return false

  const checks = gateways.map(async (gatewayUrl) => {
    const ok = await gatewayHasData(gatewayUrl, timeoutMs)
    if (!ok) throw new Error("gateway miss")
    return true
  })

  try {
    await Promise.any(checks)
    return true
  } catch {
    return false
  }
}

async function validateIpfsLinksParallel(
  ipfsUrls: string[],
  cache: Map<string, boolean>,
): Promise<number> {
  const pending = [
    ...new Set(
      ipfsUrls.map((url) => url.trim()).filter((url) => isIpfsUrl(url) && !cache.has(url)),
    ),
  ]

  if (pending.length === 0) return 0

  await Promise.all(
    pending.map(async (url) => {
      const accessible = await checkIpfsDataAccessible(url)
      cache.set(url, accessible)
    }),
  )

  return pending.length
}

async function validateUncachedUrlsParallel(
  urls: string[],
  cache: Map<string, "yes" | "no">,
): Promise<number> {
  const pending = [
    ...new Set(
      urls
        .filter((url) => !isIpfsUrl(url))
        .map((url) => urlForHttpCheck(url))
        .filter((checkUrl) => !cache.has(checkUrl)),
    ),
  ]

  if (pending.length === 0) return 0

  await Promise.all(
    pending.map(async (checkUrl) => {
      const result = await checkHttpAccessible(checkUrl)
      cache.set(checkUrl, result)
    }),
  )

  return pending.length
}

function buildOutputLinks(
  links: CollectedLink[],
  httpCache: ReadonlyMap<string, "yes" | "no">,
  ipfsCache: ReadonlyMap<string, boolean>,
  validateLinks: boolean,
): { links: AggregatedLink[]; droppedIpfs: number } {
  const out: AggregatedLink[] = []
  let droppedIpfs = 0

  for (const link of links) {
    if (isIpfsUrl(link.url)) {
      if (!validateLinks) {
        out.push({ ...link, http_accessible: "no" })
        continue
      }
      const accessible = ipfsCache.get(link.url.trim()) ?? false
      if (!accessible) {
        droppedIpfs++
        continue
      }
      out.push({ ...link, http_accessible: "yes" })
      continue
    }

    out.push({
      ...link,
      http_accessible: validateLinks
        ? (httpCache.get(urlForHttpCheck(link.url)) ?? "no")
        : "no",
    })
  }

  return { links: out, droppedIpfs }
}

export async function checkHttpAccessible(
  raw: string,
  timeoutMs = HTTP_CHECK_TIMEOUT_MS,
): Promise<"yes" | "no"> {
  const fetchUrl = urlForHttpCheck(raw)
  if (!/^https?:\/\//i.test(fetchUrl)) return "no"

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  const requestInit: RequestInit = {
    signal: controller.signal,
    redirect: "follow",
    headers: { "User-Agent": HTTP_CHECK_USER_AGENT },
  }

  try {
    let res = await fetch(fetchUrl, { ...requestInit, method: "HEAD" })
    if (res.status === 405 || res.status === 501) {
      res = await fetch(fetchUrl, { ...requestInit, method: "GET" })
      await res.body?.cancel()
    }
    return res.status >= 200 && res.status < 400 ? "yes" : "no"
  } catch {
    return "no"
  } finally {
    clearTimeout(timer)
  }
}

function collectLinksFromValue(
  value: unknown,
  pathKeys: string[],
  out: CollectedLink[],
  doc: string,
): void {
  if (typeof value === "string") {
    if (!isUrlLike(value)) return
    const title = titleFeature(pathKeys)
    if (ALREADY_PULLED_TITLES.has(title)) return
    const trimmed = value.trim()
    if (shouldSkipCrawlLink(trimmed, title)) return
    out.push(finalizeCollectedLink(trimmed, doc, title))
    return
  }

  if (Array.isArray(value)) {
    for (let i = 0; i < value.length; i++) {
      collectLinksFromValue(value[i], [...pathKeys, String(i)], out, doc)
    }
    return
  }

  if (value !== null && typeof value === "object") {
    for (const [key, nested] of Object.entries(value)) {
      collectLinksFromValue(nested, [...pathKeys, key], out, doc)
    }
  }
}

async function listJsonFilesInDir(dir: string): Promise<string[]> {
  let entries: { name: string; isDirectory(): boolean }[]
  try {
    entries = await fs.readdir(dir, { withFileTypes: true })
  } catch {
    return []
  }

  const files: string[] = []
  for (const entry of entries) {
    if (!entry.isDirectory() && entry.name.endsWith(".json") && entry.name !== LINKS_FILENAME) {
      files.push(path.join(dir, entry.name))
    }
  }
  files.sort()
  return files
}

async function loadDataBundleDownloadUrls(projectDir: string): Promise<Set<string>> {
  const meta = metadataDir(projectDir)
  let dataroomPath = path.join(meta, DATAROOM_FILENAME)
  try {
    await fs.access(dataroomPath)
  } catch {
    dataroomPath = path.join(projectDir, DATAROOM_FILENAME)
  }
  try {
    const raw = await fs.readFile(dataroomPath, "utf-8")
    const parsed = JSON.parse(raw) as { files?: { downloadUrl?: unknown }[] }
    const urls = new Set<string>()
    for (const file of parsed.files ?? []) {
      if (typeof file.downloadUrl === "string" && file.downloadUrl.trim()) {
        urls.add(file.downloadUrl.trim())
      }
    }
    return urls
  } catch {
    return new Set()
  }
}

function filterAlreadyPulledLinks(
  links: CollectedLink[],
  dataBundleUrls: ReadonlySet<string>,
): CollectedLink[] {
  return links.filter((link) => !dataBundleUrls.has(link.url))
}

async function aggregateProjectLinks(projectDir: string): Promise<{
  links: CollectedLink[]
  fileCount: number
  skippedAlreadyPulled: number
}> {
  const scanDir = await resolveMetadataScanDir(projectDir)
  const jsonFiles = (await listJsonFilesInDir(scanDir)).filter(
    (absPath) => !SKIP_LINK_SOURCE_DOCS.has(path.basename(absPath)),
  )
  const links: CollectedLink[] = []

  for (const absPath of jsonFiles) {
    const doc = path.basename(absPath)
    let parsed: unknown
    try {
      const raw = await fs.readFile(absPath, "utf-8")
      parsed = JSON.parse(raw)
    } catch {
      process.stderr.write(
        `Skipping unreadable JSON: ${path.basename(projectDir)}/${doc}\n`,
      )
      continue
    }
    collectLinksFromValue(parsed, [], links, doc)
  }

  const dataBundleUrls = await loadDataBundleDownloadUrls(projectDir)
  const beforeFilter = links.length
  const filtered = filterAlreadyPulledLinks(links, dataBundleUrls)

  return {
    links: filtered,
    fileCount: jsonFiles.length,
    skippedAlreadyPulled: beforeFilter - filtered.length,
  }
}

export async function runAggregateLinks(
  opts: AggregateLinksOptions,
): Promise<{
  projectCount: number
  totalLinks: number
  filesWritten: string[]
}> {
  const resolvedDir = path.resolve(opts.ipnftsDir)

  let entries: { name: string; isDirectory(): boolean }[]
  try {
    entries = await fs.readdir(resolvedDir, { withFileTypes: true })
  } catch {
    throw new Error(`Cannot read ipnfts directory: ${resolvedDir}`)
  }

  const projectDirs = entries
    .filter((e) => e.isDirectory())
    .map((e) => path.join(resolvedDir, e.name))
    .sort()

  const validateLinks = opts.validateLinks !== false
  const accessibilityCache = new Map<string, "yes" | "no">()
  const ipfsAccessibilityCache = new Map<string, boolean>()

  type ProjectBatch = {
    projectDir: string
    folderName: string
    links: CollectedLink[]
    fileCount: number
    skippedAlreadyPulled: number
  }

  const projectBatches: ProjectBatch[] = []

  for (const projectDir of projectDirs) {
    const folderName = path.basename(projectDir)
    const { links, fileCount, skippedAlreadyPulled } =
      await aggregateProjectLinks(projectDir)
    if (fileCount === 0) continue
    projectBatches.push({
      projectDir,
      folderName,
      links,
      fileCount,
      skippedAlreadyPulled,
    })
  }

  if (validateLinks && projectBatches.length > 0) {
    const allUrls = projectBatches.flatMap((batch) =>
      batch.links.map((link) => link.url),
    )
    const ipfsUrls = allUrls.filter((url) => isIpfsUrl(url))
    const httpUrls = allUrls.filter((url) => !isIpfsUrl(url))

    const [httpProbed, ipfsProbed] = await Promise.all([
      validateUncachedUrlsParallel(httpUrls, accessibilityCache),
      validateIpfsLinksParallel(ipfsUrls, ipfsAccessibilityCache),
    ])

    const ipfsReachable = [...ipfsAccessibilityCache.values()].filter(Boolean).length
    process.stderr.write(
      `Probed ${httpProbed} http URL(s) and ${ipfsProbed} ipfs URL(s) in parallel ` +
        `(${ipfsReachable}/${ipfsProbed || ipfsAccessibilityCache.size} ipfs reachable via gateways).\n`,
    )
  }

  const filesWritten: string[] = []
  let totalLinks = 0
  let totalSkippedAlreadyPulled = 0
  let totalAccessible = 0
  let totalDroppedIpfs = 0

  for (const batch of projectBatches) {
    const { projectDir, folderName, links, fileCount, skippedAlreadyPulled } =
      batch

    const { links: outputLinks, droppedIpfs } = buildOutputLinks(
      links,
      accessibilityCache,
      ipfsAccessibilityCache,
      validateLinks,
    )
    totalDroppedIpfs += droppedIpfs

    const accessibleCount = outputLinks.filter(
      (l) => l.http_accessible === "yes",
    ).length
    totalAccessible += accessibleCount

    await ensureProjectLayout(projectDir)
    const outPath = linksJsonPath(projectDir)
    await fs.writeFile(
      outPath,
      JSON.stringify(outputLinks, null, 2) + "\n",
      "utf-8",
    )
    filesWritten.push(
      path.join(folderName, "metadata", LINKS_FILENAME).replace(/\\/g, "/"),
    )
    totalLinks += outputLinks.length
    totalSkippedAlreadyPulled += skippedAlreadyPulled
    const skippedNote =
      skippedAlreadyPulled > 0 ? `, ${skippedAlreadyPulled} already pulled` : ""
    const accessNote = validateLinks
      ? `, ${accessibleCount}/${outputLinks.length} http accessible` +
        (droppedIpfs > 0 ? `, ${droppedIpfs} ipfs dropped` : "")
      : ""
    process.stderr.write(
      `  ${folderName}: ${outputLinks.length} link(s) from ${fileCount} JSON file(s)${skippedNote}${accessNote}\n`,
    )
  }

  process.stderr.write(
    `Wrote ${filesWritten.length} ${LINKS_FILENAME} file(s) (${totalLinks} link(s) total` +
      (validateLinks ? `, ${totalAccessible} http accessible` : "") +
      (totalDroppedIpfs > 0 ? `, ${totalDroppedIpfs} unreachable ipfs dropped` : "") +
      (totalSkippedAlreadyPulled > 0
        ? `, ${totalSkippedAlreadyPulled} skipped as already pulled`
        : "") +
      `) under ${resolvedDir}\n`,
  )

  return {
    projectCount: filesWritten.length,
    totalLinks,
    filesWritten,
  }
}
