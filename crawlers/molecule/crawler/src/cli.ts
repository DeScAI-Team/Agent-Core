import "./load-repo-env.js"
import * as fs from "node:fs/promises"
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

function usage(): void {
  console.error(`Usage:
  npm run cli -- projects [-- --limit N]
  npm run cli -- dataroom -- <tokenId>
  npm run cli -- hash -- <tokenId>
  npm run cli -- profile -- <tokenId> [--compound-suffixes 1,8453] [--ipt-limit 25] [--out path.json]
  npm run cli -- profiles [-- --max N] [--out-dir dir] [--competent-only] [--save-all] [--compound-suffixes 1] [--ipt-limit 25] [--delay-ms 150] [--index]
  npm run cli -- orchestrate-profiles [-- --out-dir out/test-profiles/profiles] [--save-all] [--max N] [--compound-suffixes 1] [--ipt-limit 25] [--delay-ms 150] [--no-index]
  npm run cli -- data-bundle [-- --profiles-dir out/ipnft-profiles] [--delay-ms 300] [--max N] [--dry-run] [--crawl-skip-file path.json]
  npm run cli -- crawl --output-dir <dir> [--save-all] [--index] [--max N] [--delay-ms 150] [--compound-suffixes ...] [--ipt-limit N] [--dry-run] [--crawl-skip-file path.json]
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

crawl: runs profiles then data-bundle on the same directory. By default the profile phase only writes folders for competent Data API rows (skips null ipnft and thin metadata), like orchestrate-profiles; pass --save-all to also save null/stub bundles. --output-dir is required. Paths are resolved from cwd unless they start with @crawlers/ (then under repo …/crawlers/…). Same @crawlers/ prefix works for profiles --out-dir and data-bundle --profiles-dir. Example: @crawlers/output/molecule. Or ../../output/molecule from molecule/crawler. --max N applies to both phases: at most N catalog rows in the profile batch, then at most N profile folders with a valid ipnft.id in data-bundle (readdir order). --dry-run affects only the data-bundle step (no downloads; profiles always run). If the profile batch throws, data-bundle is skipped. The data-bundle step only processes profile.json files that include ipnft.id (null-ipnft bundles are skipped). Optional --crawl-skip-file points to JSON { moleculeFolders, researchhubFiles } to skip known project folders (incremental crawl).

data-bundle: reads profile folders, fetches dataroom for each ipnft.id, downloads PUBLIC documents (deduped by description), saves PDFs + manifest.json per project.

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

function parseDelayMs(): number {
  const v = getFlag("--delay-ms")
  if (v === undefined || v === "") return 150
  const n = Number(v)
  return Number.isFinite(n) && n >= 0 ? n : 150
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
    await fs.mkdir(path.dirname(path.resolve(outPath)), { recursive: true })
    await fs.writeFile(outPath, text, "utf-8")
    console.error(`Wrote ${outPath}`)
  } else {
    console.log(text)
  }
}

async function sleep(ms: number): Promise<void> {
  await new Promise((r) => setTimeout(r, ms))
}

const PROFILE_BUNDLE_FILENAME = "profile.json"

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
  await fs.mkdir(resolvedOut, { recursive: true })

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
        await fs.mkdir(projectDir, { recursive: true })
        const relFile = path.join(folderName, PROFILE_BUNDLE_FILENAME)
        const filePath = path.join(projectDir, PROFILE_BUNDLE_FILENAME)
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
        await fs.writeFile(filePath, JSON.stringify(payload, null, 2), "utf-8")
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
    await fs.mkdir(projectDir, { recursive: true })
    const relFile = path.join(folderName, PROFILE_BUNDLE_FILENAME)
    const filePath = path.join(projectDir, PROFILE_BUNDLE_FILENAME)
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
    await fs.writeFile(filePath, JSON.stringify(payload, null, 2), "utf-8")
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
    await fs.writeFile(
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
    const raw = await fs.readFile(abs, "utf-8")
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
    banner: "crawl: profile batch then data-bundle (same --output-dir).",
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
  } else {
    usage()
    process.exit(1)
  }
} catch (e) {
  console.error(e)
  process.exit(1)
}
