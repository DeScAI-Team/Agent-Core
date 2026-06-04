import "./load-repo-env.js"
import { spawnSync } from "node:child_process"
import * as fs from "node:fs"
import * as fsp from "node:fs/promises"
import * as path from "node:path"
import {
  getAllProjects,
  getDataRoomHash,
  getProjectDataRoomFiles,
  getPublicExtractableFiles,
} from "./molecule-lib-bridge.js"
import {
  fetchIpnftProfileForToken,
  fetchIptsForIpnftFilterId,
} from "./ipnft-profile.js"
import { classifyProfileSkip } from "./profile-screen.js"
import { runDataBundle } from "./data-bundle.js"
import { runAggregateLinks } from "./aggregate-links.js"
import {
  ensureProjectLayout,
  METADATA_DIR,
  PROFILE_FILENAME,
  profileJsonPath,
} from "./ipnft-layout.js"

const DEFAULT_ORCHESTRATOR_OUT_DIR = "out/test-profiles/profiles"

function getFlag(name: string): string | undefined {
  const i = process.argv.indexOf(name)
  if (i === -1 || i + 1 >= process.argv.length) return undefined
  return process.argv[i + 1]
}

/**
 * Resolve a user-supplied directory: normal paths use path.resolve(cwd).
 * `@crawlers/...` → <repo>/crawlers/... when cwd is molecule/crawler (two levels under crawlers/).
 * Use for crawl --output-dir, profiles/orchestrate --out-dir, data-bundle --profiles-dir.
 */
function resolveCrawlersDirArg(raw: string): string {
  const trimmed = raw.trim()
  const asUrl = trimmed.replace(/\\/g, "/")
  if (asUrl.toLowerCase().startsWith("@crawlers/")) {
    const rest = asUrl.slice("@crawlers/".length).replace(/^\/+/, "")
    return path.resolve(process.cwd(), "..", "..", rest)
  }
  return path.resolve(trimmed)
}

/** Prefer repo Agent venv python when present (crawl4ai lives there). */
function resolveAgentPython(): string {
  const agentPy = path.resolve(process.cwd(), "..", "..", "..", "Agent", "bin", "python3")
  if (fs.existsSync(agentPy)) return agentPy
  return process.platform === "win32" ? "python" : "python3"
}

function usage(): void {
  console.error(`Usage:
  npm run cli -- projects [-- --limit N]
  npm run cli -- dataroom -- <tokenId>
  npm run cli -- hash -- <tokenId>
  npm run cli -- profile -- <tokenId> [--compound-suffixes 1,8453] [--ipt-limit 25] [--out path.json]
  npm run cli -- profiles [-- --max N] [--out-dir dir] [--competent-only] [--save-all] [--compound-suffixes 1] [--ipt-limit 25] [--delay-ms 150] [--index]
  npm run cli -- orchestrate-profiles [-- --out-dir out/test-profiles/profiles] [--save-all] [--max N] [--compound-suffixes 1] [--ipt-limit 25] [--delay-ms 150] [--no-index]
  npm run cli -- data-bundle [-- --profiles-dir out/ipnft-profiles] [--delay-ms 300] [--max N] [--dry-run] [--crawl-skip-file path.json]
  npm run cli -- crawl --output-dir <dir> [--save-all] [--index] [--max N] [--delay-ms 150] [--compound-suffixes ...] [--ipt-limit N] [--dry-run] [--no-crawl-links] [--no-crawl-nitter] [--concurrency N] [--force] [--max-tweets N] [--doc-max-depth N] [--doc-max-pages N] [--min-chars N] [--hub-min-chars N] [--max-outbound-follows N] [--spa-wait-sec N] [--crawl-skip-file path.json]
  npm run cli -- aggregate-links --ipnfts-dir <dir> [--no-validate-links]
  npm run cli -- crawl-links --ipnfts-dir <dir> [--concurrency N] [--max N] [--folder NAME] [--force] [--dry-run] [--doc-max-depth N] [--doc-max-pages N] [--min-chars N] [--hub-min-chars N] [--max-outbound-follows N] [--spa-wait-sec N] [--crawl-skip-file path.json]
  npm run cli -- crawl-nitter --ipnfts-dir <dir> [--concurrency N] [--max N] [--folder NAME] [--force] [--max-tweets N] [--nitter-base URL] [--nitter-fallback-bases URL,...] [--crawl-skip-file path.json]
  (Use @crawlers/output/... from molecule/crawler to write under repo crawlers/; run npm from crawlers/molecule/crawler — see Examples.)

Legacy per-command scripts (npm run projects, npm run profiles, …) still work the same.

Examples:
  npm run projects -- --limit 5
  npm run dataroom -- 2
  npm run profile -- 2 --out profile-2.json
  npm run profiles -- --max 10 --out-dir out/ipnft-profiles --index
  npm run orchestrate-profiles
  npm run orchestrate-profiles -- --max 5 --delay-ms 200
  npm run data-bundle
  npm run data-bundle -- --profiles-dir out/ipnft-profiles --max 3 --dry-run
  npm run cli -- crawl --output-dir out/ipnft-profiles-test --max 2 --index
  npm run cli -- crawl --output-dir @crawlers/output/molecule --max 3 --index
  npm run crawl-links -- --ipnfts-dir @crawlers/output/molecule/ipnfts --folder BeeARD --concurrency 1

crawl: runs profiles, data-bundle, aggregate-links, crawl-links (crawl4ai), and crawl-nitter on the same --output-dir. By default the profile phase only writes folders for competent Data API rows (skips null ipnft and thin metadata), like orchestrate-profiles; pass --save-all to also save null/stub bundles. --output-dir is required. Paths are resolved from cwd unless they start with @crawlers/ (then under repo …/crawlers/…). Example: @crawlers/output/molecule/ipnfts. --max N applies to all phases. --dry-run skips dataroom downloads and passes through to Python crawlers (preview only). Pass --no-crawl-links or --no-crawl-nitter to skip steps. Python crawler flags (--concurrency, --force, --min-chars, --max-tweets, …) work on crawl too. Optional --crawl-skip-file points to JSON { moleculeFolders, researchhubFiles } to skip known project folders.

crawl-nitter: reads nitter.net profile URLs from metadata JSON → output/tweets.json and metadata/nitter-manifest.json. RSS first, HTML fallback; --nitter-base / --nitter-fallback-bases for alternate instances.

aggregate-links: for each project folder under --ipnfts-dir, extracts URL strings from JSON under metadata/ (legacy: project root) and writes metadata/links.json. Probes http(s) URLs in parallel; ipfs:// URLs are checked against multiple public gateways in parallel (longer timeout) and omitted from output when no gateway returns data. Skips dataroom.json and manifest.json (data-bundle downloads), tokenUri (profile API metadata), dataApi (Molecule API docs), bare https://molecule.xyz homepages, etherscan.io links, and any URL listed as a dataroom downloadUrl. Rewrites x.com/twitter.com URLs to nitter.net. Does not read PDFs or other non-JSON artifacts.

crawl-links: reads metadata/links.json per IPNFT folder and crawls eligible http(s) URLs with crawl4ai (Python, Agent venv). Skips nitter.net, ipfs links, block explorers (basescan.org, etherscan.io, …), and social URLs (t.me/Telegram, x.com, Discord, …). Doc/docs URLs use BestFirst deep crawl with prefetch, then merge all pages into one {site}.md with section-level dedupe (no media). molecule.xyz / mint.molecule.to use hub mode: single SPA fetch (no site-wide deep crawl), link extraction with catalog blocklists, bounded off-site follows (--max-outbound-follows, default 12), optional mint fallback on 404; writes metadata/crawl-extracted-links.json sidecar. snapshot.box uses SPA single-page mode. Other URLs are section-deduped single-page crawls. Drops short output (--min-chars, default 400; hub pages --hub-min-chars, default 150) and short sections (--min-section-chars, default 80). Saves .md under output/ and metadata/crawl-manifest.json. Runs many IPNFT folders in parallel (--concurrency, default 4).

data-bundle: reads metadata/profile.json (legacy: root profile.json), fetches dataroom for each ipnft.id, downloads PUBLIC documents (deduped by description) into output/ with metadata/manifest.json.

orchestrate-profiles: same as profiles default out-dir, but only writes symbol folders when the Data API returns a competent catalog profile (use --save-all to disable). Index lists saved + skipped with reasons.

Catalog: ipnft(id) uses bare projectsV2 token id. Optional --compound-suffixes adds legacy compound ids after bare fails.
IPTs: ipts(filterBy: { ipnftId }) for market rows (see https://docs.molecule.xyz/api-reference/data-api ).
Set MOLECULE_API_KEY in the repo-root .env or the environment.`)
}

function parseLimit(): number | undefined {
  const idx = process.argv.indexOf("--limit")
  if (idx === -1) return 20
  const n = Number(process.argv[idx + 1])
  return Number.isFinite(n) && n > 0 ? n : 20
}

function parseMaxProjects(): number | undefined {
  const v = getFlag("--max")
  if (v === undefined || v === "") return undefined
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : undefined
}

function parseValidateLinks(): boolean {
  return !process.argv.includes("--no-validate-links")
}

function parseDelayMs(): number {
  const v = getFlag("--delay-ms")
  if (v === undefined || v === "") return 150
  const n = Number(v)
  return Number.isFinite(n) && n >= 0 ? n : 150
}

function aggregateLinksOptions(ipnftsDir: string) {
  return {
    ipnftsDir,
    validateLinks: parseValidateLinks(),
  }
}

function parseCompoundSuffixes(): string[] {
  const multi = getFlag("--compound-suffixes")
  if (!multi) return []
  return multi
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean)
}

function parseIptLimit(): number {
  const v = getFlag("--ipt-limit")
  if (v === undefined || v === "") return 25
  const n = Number(v)
  return Number.isFinite(n) && n > 0 ? n : 25
}

async function cmdProjects(): Promise<void> {
  const limit = parseLimit()
  const all = await getAllProjects()
  console.log(JSON.stringify({ total: all.length, showing: limit, projects: all.slice(0, limit) }, null, 2))
}

async function cmdDataroom(tokenId: string): Promise<void> {
  const files = await getProjectDataRoomFiles(tokenId)
  const publicExtractable = getPublicExtractableFiles(files)
  console.log(
    JSON.stringify(
      {
        tokenId,
        fileCount: files.length,
        publicExtractableCount: publicExtractable.length,
        files,
        publicExtractable,
      },
      null,
      2,
    ),
  )
}

async function cmdHash(tokenId: string): Promise<void> {
  const hash = await getDataRoomHash(tokenId)
  console.log(JSON.stringify({ tokenId, dataroomHash: hash }, null, 2))
}

async function cmdProfile(tokenId: string): Promise<void> {
  const compoundSuffixes = parseCompoundSuffixes()
  const iptLimit = parseIptLimit()
  const { queriedId, profile, attemptedIds } = await fetchIpnftProfileForToken(
    tokenId,
    compoundSuffixes,
  )

  let iptTokens: Record<string, unknown>[] = []
  if (profile) {
    const filterId = String(profile.id ?? tokenId)
    iptTokens = await fetchIptsForIpnftFilterId(filterId, iptLimit)
  }

  const payload = {
    tokenId,
    compoundSuffixesTriedAfterBare: compoundSuffixes,
    attemptedIds,
    queriedId,
    ipnft: profile,
    iptTokens,
    dataApi: "https://docs.molecule.xyz/api-reference/data-api",
  }

  const outPath = getFlag("--out")
  const text = JSON.stringify(payload, null, 2)
  if (outPath) {
    await fsp.mkdir(path.dirname(path.resolve(outPath)), { recursive: true })
    await fsp.writeFile(outPath, text, "utf-8")
    console.error(`Wrote ${outPath}`)
  } else {
    console.log(text)
  }
}

async function sleep(ms: number): Promise<void> {
  await new Promise((r) => setTimeout(r, ms))
}

const PROFILE_BUNDLE_FILENAME = PROFILE_FILENAME

/** Windows / POSIX reserved characters in path segments */
function sanitizeSymbolForDir(raw: string): string {
  const replaced = raw
    .replace(/[<>:"/\\|?*\u0000-\u001f]/g, "_")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/[. ]+$/g, "")
  const collapsed = replaced.replace(/_+/g, "_").replace(/^\.+/, "")
  const trimmed = collapsed.slice(0, 120)
  return trimmed.length > 0 ? trimmed : "unnamed"
}

function allocateProjectDirName(
  symbol: string,
  tokenId: string,
  claimed: Set<string>,
): string {
  const base = sanitizeSymbolForDir(symbol)
  if (!claimed.has(base)) {
    claimed.add(base)
    return base
  }
  const withId = sanitizeSymbolForDir(`${base}__${tokenId}`).slice(0, 200)
  let candidate = withId
  let n = 2
  while (claimed.has(candidate)) {
    candidate = sanitizeSymbolForDir(`${base}__${tokenId}__${n}`).slice(0, 200)
    n++
  }
  claimed.add(candidate)
  return candidate
}

/** Same folder name as `allocateProjectDirName` would return, without mutating `claimed`. */
function peekProjectDirName(
  symbol: string,
  tokenId: string,
  claimed: ReadonlySet<string>,
): string {
  const base = sanitizeSymbolForDir(symbol)
  if (!claimed.has(base)) {
    return base
  }
  const withId = sanitizeSymbolForDir(`${base}__${tokenId}`).slice(0, 200)
  let candidate = withId
  let n = 2
  while (claimed.has(candidate)) {
    candidate = sanitizeSymbolForDir(`${base}__${tokenId}__${n}`).slice(0, 200)
    n++
  }
  return candidate
}

interface ProfileBatchOptions {
  outDir: string
  max?: number
  compoundSuffixes: string[]
  iptLimit: number
  delayMs: number
  writeIndex: boolean
  /** When true, only write disk + claim symbol folder for competent Data API profiles */
  onlySaveCompetent: boolean
  /** Logged before the per-project loop */
  banner?: string
  /** Skip profile write when this folder name would match a prior crawl-log entry */
  skipFolderNames?: ReadonlySet<string>
}

async function runProfilesBatch(opts: ProfileBatchOptions): Promise<void> {
  const all = await getAllProjects()
  const slice = opts.max !== undefined ? all.slice(0, opts.max) : all

  if (opts.banner) {
    process.stderr.write(`${opts.banner}\n`)
  }
  process.stderr.write(
    `Listing complete: ${all.length} project(s); processing ${slice.length} row(s)${opts.onlySaveCompetent ? " (competent-only save)" : ""}.\n`,
  )

  const resolvedOut = path.resolve(opts.outDir)
  await fsp.mkdir(resolvedOut, { recursive: true })

  const claimedDirNames = new Set<string>()

  type IndexRow =
    | {
        status: "saved"
        tokenId: string
        symbol: string
        folder: string
        file: string
        queriedId: string
      }
    | {
        status: "skipped"
        tokenId: string
        symbol: string
        queriedId: string
        skipReason: string
      }

  const indexRows: IndexRow[] = []
  let savedCount = 0
  let skippedCount = 0

  for (let i = 0; i < slice.length; i++) {
    const { tokenId, symbol } = slice[i]

    process.stderr.write(`[${i + 1}/${slice.length}] ${symbol} (${tokenId}) ... `)

    const folderPreview = peekProjectDirName(symbol, tokenId, claimedDirNames)
    if (opts.skipFolderNames?.has(folderPreview)) {
      indexRows.push({
        status: "skipped",
        tokenId,
        symbol,
        queriedId: tokenId,
        skipReason: "crawl_log",
      })
      skippedCount++
      process.stderr.write(`skipped (crawl_log)\n`)
      if (opts.delayMs) await sleep(opts.delayMs)
      continue
    }

    let profile: Record<string, unknown> | null = null
    let lastQueriedId = tokenId
    let attemptedIds: string[] = [tokenId]
    let iptTokens: Record<string, unknown>[] = []
    let fetchError: string | undefined

    try {
      const resolved = await fetchIpnftProfileForToken(
        tokenId,
        opts.compoundSuffixes,
      )
      profile = resolved.profile
      lastQueriedId = resolved.queriedId
      attemptedIds = resolved.attemptedIds
      if (profile) {
        const filterId = String(profile.id ?? tokenId)
        iptTokens = await fetchIptsForIpnftFilterId(filterId, opts.iptLimit)
      }
    } catch (e) {
      fetchError = e instanceof Error ? e.message : String(e)
    }

    if (fetchError) {
      if (opts.onlySaveCompetent) {
        indexRows.push({
          status: "skipped",
          tokenId,
          symbol,
          queriedId: lastQueriedId,
          skipReason: "fetch_error",
        })
        skippedCount++
        process.stderr.write(`skipped (${fetchError})\n`)
      } else {
        const folderName = allocateProjectDirName(symbol, tokenId, claimedDirNames)
        const projectDir = path.join(resolvedOut, folderName)
        await ensureProjectLayout(projectDir)
        const relFile = path.join(folderName, METADATA_DIR, PROFILE_BUNDLE_FILENAME)
        const filePath = profileJsonPath(projectDir)
        const payload = {
          tokenId,
          symbol,
          outputFolder: folderName,
          compoundSuffixesTriedAfterBare: opts.compoundSuffixes,
          attemptedIds,
          queriedId: lastQueriedId,
          ipnft: null,
          iptTokens,
          error: fetchError,
        }
        await fsp.writeFile(filePath, JSON.stringify(payload, null, 2), "utf-8")
        indexRows.push({
          status: "saved",
          tokenId,
          symbol,
          folder: folderName,
          queriedId: lastQueriedId,
          file: relFile.replace(/\\/g, "/"),
        })
        savedCount++
        process.stderr.write(`error (saved): ${fetchError}\n`)
      }
      if (opts.delayMs) await sleep(opts.delayMs)
      continue
    }

    const skipReason = classifyProfileSkip(profile, false)
    if (opts.onlySaveCompetent && skipReason) {
      indexRows.push({
        status: "skipped",
        tokenId,
        symbol,
        queriedId: lastQueriedId,
        skipReason,
      })
      skippedCount++
      process.stderr.write(`skipped (${skipReason})\n`)
      if (opts.delayMs) await sleep(opts.delayMs)
      continue
    }

    const folderName = allocateProjectDirName(symbol, tokenId, claimedDirNames)
    const projectDir = path.join(resolvedOut, folderName)
    await ensureProjectLayout(projectDir)
    const relFile = path.join(folderName, METADATA_DIR, PROFILE_BUNDLE_FILENAME)
    const filePath = profileJsonPath(projectDir)
    const payload = {
      tokenId,
      symbol,
      outputFolder: folderName,
      compoundSuffixesTriedAfterBare: opts.compoundSuffixes,
      attemptedIds,
      queriedId: lastQueriedId,
      ipnft: profile,
      iptTokens,
      dataApi: "https://docs.molecule.xyz/api-reference/data-api",
    }
    await fsp.writeFile(filePath, JSON.stringify(payload, null, 2), "utf-8")
    indexRows.push({
      status: "saved",
      tokenId,
      symbol,
      folder: folderName,
      queriedId: lastQueriedId,
      file: relFile.replace(/\\/g, "/"),
    })
    savedCount++
    process.stderr.write(
      opts.onlySaveCompetent
        ? "saved\n"
        : profile
          ? "ok\n"
          : "saved (null ipnft)\n",
    )

    if (opts.delayMs) await sleep(opts.delayMs)
  }

  if (opts.writeIndex) {
    const indexPath = path.join(resolvedOut, "profiles-index.json")
    await fsp.writeFile(
      indexPath,
      JSON.stringify(
        {
          generatedAt: new Date().toISOString(),
          compoundSuffixesTriedAfterBare: opts.compoundSuffixes,
          onlySaveCompetent: opts.onlySaveCompetent,
          totalListed: all.length,
          processed: slice.length,
          savedCount,
          skippedCount,
          outDir: resolvedOut,
          rows: indexRows,
        },
        null,
        2,
      ),
      "utf-8",
    )
    process.stderr.write(`Wrote ${indexPath}\n`)
  }

  process.stderr.write(
    `Done. ${savedCount} saved, ${skippedCount} skipped under ${resolvedOut}\n`,
  )
}

async function cmdProfiles(): Promise<void> {
  await runProfilesBatch({
    outDir: resolveCrawlersDirArg(getFlag("--out-dir") ?? "out/ipnft-profiles"),
    max: parseMaxProjects(),
    compoundSuffixes: parseCompoundSuffixes(),
    iptLimit: parseIptLimit(),
    delayMs: parseDelayMs(),
    writeIndex: process.argv.includes("--index"),
    onlySaveCompetent: process.argv.includes("--competent-only"),
  })
}

async function loadCrawlSkipFolderSet(
  skipFile: string | undefined,
): Promise<ReadonlySet<string>> {
  if (!skipFile || skipFile.startsWith("--")) return new Set()
  const abs = path.resolve(skipFile)
  try {
    const raw = await fsp.readFile(abs, "utf-8")
    const j = JSON.parse(raw) as { moleculeFolders?: unknown }
    const arr = j.moleculeFolders
    if (!Array.isArray(arr)) return new Set()
    return new Set(arr.map((x) => String(x)))
  } catch {
    return new Set()
  }
}

async function cmdDataBundle(): Promise<void> {
  const skipFile = getFlag("--crawl-skip-file")
  const skipFolderNames = await loadCrawlSkipFolderSet(skipFile)
  await runDataBundle({
    profilesDir: resolveCrawlersDirArg(
      getFlag("--profiles-dir") ?? "out/ipnft-profiles",
    ),
    delayMs: parseDelayMs(),
    max: parseMaxProjects(),
    dryRun: process.argv.includes("--dry-run"),
    skipFolderNames,
  })
}

async function cmdOrchestrateProfiles(): Promise<void> {
  await runProfilesBatch({
    outDir: resolveCrawlersDirArg(
      getFlag("--out-dir") ?? DEFAULT_ORCHESTRATOR_OUT_DIR,
    ),
    max: parseMaxProjects(),
    compoundSuffixes: parseCompoundSuffixes(),
    iptLimit: parseIptLimit(),
    delayMs: parseDelayMs(),
    writeIndex: !process.argv.includes("--no-index"),
    onlySaveCompetent: !process.argv.includes("--save-all"),
    banner:
      "Orchestrator: project search (projectsV2) → Data API profile (ipnft + ipts) per project.",
  })
}

/** Smoke: npm run cli -- crawl --output-dir out/ipnft-profiles-test --max 2 --index */
async function cmdCrawl(): Promise<void> {
  const rawOut = getFlag("--output-dir")
  if (!rawOut || rawOut.startsWith("--")) {
    usage()
    process.exit(1)
  }
  const outDir = resolveCrawlersDirArg(rawOut)
  const skipFile = getFlag("--crawl-skip-file")
  const skipFolderNames = await loadCrawlSkipFolderSet(skipFile)

  await runProfilesBatch({
    outDir,
    max: parseMaxProjects(),
    compoundSuffixes: parseCompoundSuffixes(),
    iptLimit: parseIptLimit(),
    delayMs: parseDelayMs(),
    writeIndex: process.argv.includes("--index"),
    onlySaveCompetent: !process.argv.includes("--save-all"),
    banner: "crawl: profiles → data-bundle → aggregate-links → crawl-links → nitter.",
    skipFolderNames,
  })

  process.stderr.write(`Starting data-bundle in ${outDir}\n`)

  await runDataBundle({
    profilesDir: outDir,
    delayMs: parseDelayMs(),
    max: parseMaxProjects(),
    dryRun: process.argv.includes("--dry-run"),
    skipFolderNames,
  })

  process.stderr.write(`Aggregating links from JSON under ${outDir}\n`)

  await runAggregateLinks(aggregateLinksOptions(outDir))

  if (process.argv.includes("--no-crawl-links")) {
    process.stderr.write(`Skipping crawl-links (--no-crawl-links)\n`)
    return
  }

  process.stderr.write(`Crawling links.json under ${outDir}\n`)
  runCrawlLinks(outDir)

  if (!process.argv.includes("--no-crawl-nitter")) {
    process.stderr.write(`Fetching nitter timelines under ${outDir}\n`)
    runCrawlNitter(outDir)
  } else {
    process.stderr.write(`Skipping crawl-nitter (--no-crawl-nitter)\n`)
  }
}

async function cmdAggregateLinks(): Promise<void> {
  const rawDir = getFlag("--ipnfts-dir")
  if (!rawDir || rawDir.startsWith("--")) {
    usage()
    process.exit(1)
  }
  await runAggregateLinks(aggregateLinksOptions(resolveCrawlersDirArg(rawDir)))
}

function buildCrawlLinksArgs(ipnftsDir: string): string[] {
  const args = [
    path.join(process.cwd(), "crawl_links.py"),
    "--ipnfts-dir",
    ipnftsDir,
  ]

  const folder = getFlag("--folder")
  if (folder) args.push("--folder", folder)

  const max = getFlag("--max")
  if (max) args.push("--max", max)

  const concurrency = getFlag("--concurrency")
  if (concurrency) args.push("--concurrency", concurrency)

  const docMaxDepth = getFlag("--doc-max-depth")
  if (docMaxDepth) args.push("--doc-max-depth", docMaxDepth)

  const docMaxPages = getFlag("--doc-max-pages")
  if (docMaxPages) args.push("--doc-max-pages", docMaxPages)

  const minChars = getFlag("--min-chars")
  if (minChars) args.push("--min-chars", minChars)

  const minSectionChars = getFlag("--min-section-chars")
  if (minSectionChars) args.push("--min-section-chars", minSectionChars)

  const hubMinChars = getFlag("--hub-min-chars")
  if (hubMinChars) args.push("--hub-min-chars", hubMinChars)

  const maxOutboundFollows = getFlag("--max-outbound-follows")
  if (maxOutboundFollows) args.push("--max-outbound-follows", maxOutboundFollows)

  const spaWaitSec = getFlag("--spa-wait-sec")
  if (spaWaitSec) args.push("--spa-wait-sec", spaWaitSec)

  const skipFile = getFlag("--crawl-skip-file")
  if (skipFile && !skipFile.startsWith("--")) {
    args.push("--crawl-skip-file", path.resolve(skipFile))
  }

  if (process.argv.includes("--dry-run")) args.push("--dry-run")
  if (process.argv.includes("--force")) args.push("--force")

  return args
}

function runCrawlLinks(ipnftsDir: string): void {
  const py = resolveAgentPython()
  const args = buildCrawlLinksArgs(ipnftsDir)
  const r = spawnSync(py, args, {
    cwd: process.cwd(),
    stdio: "inherit",
    env: process.env,
  })
  if (r.error) {
    console.error(r.error)
    process.exit(1)
  }
  const code = typeof r.status === "number" ? r.status : 1
  if (code !== 0) process.exit(code)
}

async function cmdCrawlLinks(): Promise<void> {
  const rawDir = getFlag("--ipnfts-dir")
  if (!rawDir || rawDir.startsWith("--")) {
    usage()
    process.exit(1)
  }
  runCrawlLinks(resolveCrawlersDirArg(rawDir))
}

function appendSharedPythonCrawlFlags(args: string[]): void {
  const folder = getFlag("--folder")
  if (folder) args.push("--folder", folder)

  const max = getFlag("--max")
  if (max) args.push("--max", max)

  const concurrency = getFlag("--concurrency")
  if (concurrency) args.push("--concurrency", concurrency)

  const skipFile = getFlag("--crawl-skip-file")
  if (skipFile && !skipFile.startsWith("--")) {
    args.push("--crawl-skip-file", path.resolve(skipFile))
  }

  if (process.argv.includes("--dry-run")) args.push("--dry-run")
  if (process.argv.includes("--force")) args.push("--force")
}

function runPythonCrawler(script: string, ipnftsDir: string, extraArgs: string[] = []): void {
  const py = resolveAgentPython()
  const args = [
    path.join(process.cwd(), script),
    "--ipnfts-dir",
    ipnftsDir,
    ...extraArgs,
  ]
  appendSharedPythonCrawlFlags(args)
  const r = spawnSync(py, args, {
    cwd: process.cwd(),
    stdio: "inherit",
    env: process.env,
  })
  if (r.error) {
    console.error(r.error)
    process.exit(1)
  }
  const code = typeof r.status === "number" ? r.status : 1
  if (code !== 0) process.exit(code)
}

function buildCrawlNitterExtraArgs(): string[] {
  const extra: string[] = []
  const maxTweets = getFlag("--max-tweets")
  if (maxTweets) extra.push("--max-tweets", maxTweets)
  const base = getFlag("--nitter-base")
  if (base) extra.push("--nitter-base", base)
  const fallbacks = getFlag("--nitter-fallback-bases")
  if (fallbacks) extra.push("--nitter-fallback-bases", fallbacks)
  return extra
}

function runCrawlNitter(ipnftsDir: string): void {
  runPythonCrawler("crawl_nitter.py", ipnftsDir, buildCrawlNitterExtraArgs())
}

async function cmdCrawlNitter(): Promise<void> {
  const rawDir = getFlag("--ipnfts-dir")
  if (!rawDir || rawDir.startsWith("--")) {
    usage()
    process.exit(1)
  }
  runCrawlNitter(resolveCrawlersDirArg(rawDir))
}

const cmd = process.argv[2]

try {
  if (cmd === "projects") {
    await cmdProjects()
  } else if (cmd === "dataroom") {
    const tokenId = process.argv[3]
    if (!tokenId || tokenId.startsWith("--")) {
      usage()
      process.exit(1)
    }
    await cmdDataroom(tokenId)
  } else if (cmd === "hash") {
    const tokenId = process.argv[3]
    if (!tokenId || tokenId.startsWith("--")) {
      usage()
      process.exit(1)
    }
    await cmdHash(tokenId)
  } else if (cmd === "profile") {
    const tokenId = process.argv[3]
    if (!tokenId || tokenId.startsWith("--")) {
      usage()
      process.exit(1)
    }
    await cmdProfile(tokenId)
  } else if (cmd === "profiles") {
    await cmdProfiles()
  } else if (cmd === "data-bundle") {
    await cmdDataBundle()
  } else if (cmd === "crawl") {
    await cmdCrawl()
  } else if (cmd === "orchestrate-profiles") {
    await cmdOrchestrateProfiles()
  } else if (cmd === "aggregate-links") {
    await cmdAggregateLinks()
  } else if (cmd === "crawl-links") {
    await cmdCrawlLinks()
  } else if (cmd === "crawl-nitter") {
    await cmdCrawlNitter()
  } else {
    usage()
    process.exit(1)
  }
} catch (e) {
  console.error(e)
  process.exit(1)
}
