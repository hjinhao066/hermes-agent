// Windows clipboard pictures that Electron's clipboard.readImage() misses.
//
// readImage() only sees a device-independent bitmap. PixPin, Snipaste, ShareX,
// and the Windows snipping tool usually publish a custom "PNG" format, and
// Explorer's Ctrl+C on a picture is a file drop whose text/plain is the path.
// Either one makes Ctrl+V in the composer look empty. This reader tries the
// bitmap, then raw image formats, then image files actually on the clipboard.

import { execFile } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

import { encodePowerShellCommand } from './wsl-clipboard-image'

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])
const IMAGE_FILE_RE = /\.(png|jpe?g|gif|webp|bmp|tiff?|svg|ico)$/i
const IMAGE_BUFFER_FORMATS = ['PNG', 'image/png', 'image/jpeg', 'image/jpg', 'JFIF', 'image/gif', 'GIF', 'image/webp', 'image/bmp']
const HTML_IMG_SRC_RE = /<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']/gi

export type PathKind = 'file' | 'other' | null
export type StatFn = (filePath: string) => PathKind

export interface ElectronClipboardLike {
  availableFormats?: () => string[]
  read?: (format: string) => string
  readBuffer?: (format: string) => Buffer
  readImage: () => { isEmpty: () => boolean; toPNG: () => Buffer }
  readText?: () => string
}

export type ClipboardImageHit = { bytes: Buffer; ext: string; kind: 'bytes' } | { kind: 'paths'; paths: string[] }

type ExecFileFn = (
  file: string,
  args: readonly string[],
  options: { encoding: 'utf8'; maxBuffer: number; timeout: number; windowsHide: boolean }
) => Promise<{ stdout: string }>

const execFileAsync = promisify(execFile) as unknown as ExecFileFn

export function statPathKind(filePath: string): PathKind {
  try {
    return fs.statSync(filePath).isFile() ? 'file' : 'other'
  } catch {
    return null
  }
}

function sniffImage(buffer: Buffer): { bytes: Buffer; ext: string } | null {
  if (!buffer || buffer.length < 8) {
    return null
  }

  if (buffer.subarray(0, PNG_SIGNATURE.length).equals(PNG_SIGNATURE)) {
    return { bytes: buffer, ext: '.png' }
  }

  if (buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff) {
    return { bytes: buffer, ext: '.jpg' }
  }

  const head = buffer.subarray(0, 6).toString('ascii')

  if (head === 'GIF87a' || head === 'GIF89a') {
    return { bytes: buffer, ext: '.gif' }
  }

  if (buffer.length >= 12 && buffer.subarray(0, 4).toString('ascii') === 'RIFF' && buffer.subarray(8, 12).toString('ascii') === 'WEBP') {
    return { bytes: buffer, ext: '.webp' }
  }

  if (buffer[0] === 0x42 && buffer[1] === 0x4d) {
    return { bytes: buffer, ext: '.bmp' }
  }

  return null
}

function readImageBytes(clipboard: ElectronClipboardLike): { bytes: Buffer; ext: string } | null {
  try {
    const image = clipboard.readImage()

    if (image && !image.isEmpty()) {
      const png = image.toPNG()

      if (png?.length) {
        return { bytes: png, ext: '.png' }
      }
    }
  } catch {
    // readImage throws on some empty-clipboard states. The format walk is next.
  }

  const formats = new Set<string>(IMAGE_BUFFER_FORMATS)

  try {
    for (const format of clipboard.availableFormats?.() ?? []) {
      if (/image|png|jpe?g|gif|webp|bmp/i.test(format)) {
        formats.add(format)
      }
    }
  } catch {
    // availableFormats is optional.
  }

  for (const format of formats) {
    try {
      const sniffed = sniffImage(clipboard.readBuffer?.(format) ?? Buffer.alloc(0))

      if (sniffed) {
        return sniffed
      }
    } catch {
      // Missing clipboard formats throw. Try the next name.
    }
  }

  return null
}

function isImagePathLine(line: string): boolean {
  if (line.length > 1024 || !IMAGE_FILE_RE.test(line)) {
    return false
  }

  if (/^https?:|^data:/i.test(line)) {
    return false
  }

  if (/^file:/i.test(line) || /^(?:[a-zA-Z]:[\\/]|\\\\|[\\/])/.test(line)) {
    return true
  }

  return !/[\s\\/]/.test(line)
}

export function fileUrlToLocalPath(value: string): string | null {
  const trimmed = value.trim().replace(/^["']|["']$/g, '')

  if (!trimmed || /^https?:|^data:/i.test(trimmed)) {
    return null
  }

  let candidate = trimmed

  if (/^file:/i.test(trimmed)) {
    try {
      let pathname = decodeURIComponent(new URL(trimmed).pathname)

      if (/^\/[A-Za-z]:\//.test(pathname)) {
        pathname = pathname.slice(1)
      }

      candidate = pathname
    } catch {
      return null
    }
  }

  if (!IMAGE_FILE_RE.test(candidate)) {
    return null
  }

  if (/^[A-Za-z]:[\\/]/.test(candidate) || candidate.startsWith('\\\\')) {
    return path.win32.normalize(candidate)
  }

  // A bare filename (`shot.png`) is not a location. Resolving it against the
  // process cwd would attach whatever file happens to share the name.
  if (candidate.startsWith('/')) {
    return path.posix.normalize(candidate)
  }

  return null
}

function pushPathToken(raw: string, into: string[]) {
  const pathFromToken = fileUrlToLocalPath(raw)

  if (pathFromToken) {
    into.push(pathFromToken)
  }
}

function readFormat(clipboard: ElectronClipboardLike, format: string): string {
  try {
    return clipboard.read?.(format) ?? ''
  } catch {
    return ''
  }
}

function collectCandidatePaths(clipboard: ElectronClipboardLike): string[] {
  const found: string[] = []
  const uriList = readFormat(clipboard, 'text/uri-list')

  for (const line of uriList.split(/\r?\n/)) {
    const trimmed = line.trim()

    if (trimmed && !trimmed.startsWith('#')) {
      pushPathToken(trimmed, found)
    }
  }

  const html = readFormat(clipboard, 'text/html')

  for (const match of html.matchAll(HTML_IMG_SRC_RE)) {
    pushPathToken(match[1] || '', found)
  }

  try {
    const fileName = clipboard.readBuffer?.('FileNameW')

    if (fileName?.length) {
      for (const line of fileName.toString('utf16le').split('\0')) {
        pushPathToken(line, found)
      }
    }
  } catch {
    // FileNameW is absent when the clipboard holds pixels rather than a file.
  }

  let plain = ''

  try {
    plain = clipboard.readText?.() || readFormat(clipboard, 'text/plain')
  } catch {
    plain = ''
  }

  const lines = plain
    .split(/\r?\n/)
    .map(line => line.trim())
    .filter(Boolean)

  // Only when the whole clipboard text IS the path. A sentence that names a
  // png must not become an attachment.
  if (lines.length > 0 && lines.length <= 20 && lines.every(isImagePathLine)) {
    for (const line of lines) {
      pushPathToken(line, found)
    }
  }

  return found
}

export function existingImagePaths(candidates: string[], stat: StatFn): string[] {
  const seen = new Set<string>()
  const paths: string[] = []

  for (const candidate of candidates) {
    const key = candidate.toLowerCase()

    if (seen.has(key) || stat(candidate) !== 'file') {
      continue
    }

    seen.add(key)
    paths.push(candidate)
  }

  return paths
}

export function readElectronClipboardImage(clipboard: ElectronClipboardLike, stat: StatFn = statPathKind): ClipboardImageHit | null {
  const bytes = readImageBytes(clipboard)

  if (bytes) {
    return { kind: 'bytes', ...bytes }
  }

  const paths = existingImagePaths(collectCandidatePaths(clipboard), stat)

  if (paths.length > 0) {
    return { kind: 'paths', paths }
  }

  return null
}

// WinForms Clipboard.GetImage() misses the PNG format the same way
// readImage() does. GetData("PNG") is the format those tools actually set,
// and GetFileDropList is the Explorer copy. STA is mandatory.
const PS_SCRIPT = [
  "$ProgressPreference = 'SilentlyContinue'",
  'try {',
  '  Add-Type -AssemblyName System.Windows.Forms,System.Drawing',
  "  $payload = @{ png = ''; paths = (New-Object System.Collections.Generic.List[string]) }",
  '  $data = [System.Windows.Forms.Clipboard]::GetDataObject()',
  '  if ($null -ne $data) {',
  "    foreach ($fmt in @('PNG','image/png')) {",
  '      if (-not $data.GetDataPresent($fmt)) { continue }',
  '      $obj = $data.GetData($fmt)',
  '      $bytes = $null',
  '      if ($obj -is [byte[]]) { $bytes = [byte[]]$obj }',
  '      elseif ($obj -is [System.IO.MemoryStream]) { $bytes = $obj.ToArray() }',
  '      elseif ($obj -is [System.IO.Stream]) {',
  '        $copy = New-Object System.IO.MemoryStream',
  '        $obj.CopyTo($copy)',
  '        $bytes = $copy.ToArray()',
  '      }',
  '      if ($null -ne $bytes -and $bytes.Length -gt 8) {',
  '        $payload.png = [Convert]::ToBase64String($bytes)',
  '        break',
  '      }',
  '    }',
  '  }',
  '  if (-not $payload.png) {',
  '    $img = [System.Windows.Forms.Clipboard]::GetImage()',
  '    if ($null -ne $img) {',
  '      $ms = New-Object System.IO.MemoryStream',
  '      $img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)',
  '      $payload.png = [Convert]::ToBase64String($ms.ToArray())',
  '    }',
  '  }',
  '  if (-not $payload.png) {',
  '    foreach ($f in [System.Windows.Forms.Clipboard]::GetFileDropList()) { [void]$payload.paths.Add([string]$f) }',
  '  }',
  '  $json = $payload | ConvertTo-Json -Compress',
  '  [Console]::Out.Write([Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($json)))',
  '} catch { exit 0 }'
].join('\n')

function powershellCandidates() {
  const root = process.env.SystemRoot || process.env.WINDIR

  return root
    ? [path.win32.join(root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'), 'powershell.exe']
    : ['powershell.exe']
}

function asPathList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(entry => String(entry || '')).filter(Boolean)
  }

  if (typeof value === 'string' && value) {
    return [value]
  }

  // Windows PowerShell 5.1 ConvertTo-Json sometimes emits a one-element array
  // as a string, and a List as an object with numeric keys.
  if (value && typeof value === 'object') {
    return Object.values(value as Record<string, unknown>).filter((entry): entry is string => typeof entry === 'string' && entry.length > 0)
  }

  return []
}

export function decodePowerShellClipboardPayload(stdout: string, stat: StatFn): ClipboardImageHit | null {
  const encoded = String(stdout || '').trim()

  if (!encoded) {
    return null
  }

  let parsed: { paths?: unknown; png?: string }

  try {
    parsed = JSON.parse(Buffer.from(encoded, 'base64').toString('utf8')) as { paths?: unknown; png?: string }
  } catch {
    return null
  }

  if (parsed.png) {
    const sniffed = sniffImage(Buffer.from(parsed.png, 'base64'))

    if (sniffed) {
      return { kind: 'bytes', ...sniffed }
    }
  }

  const paths = existingImagePaths(
    asPathList(parsed.paths)
      .map(entry => fileUrlToLocalPath(entry))
      .filter((entry): entry is string => Boolean(entry)),
    stat
  )

  return paths.length > 0 ? { kind: 'paths', paths } : null
}

export async function readPowerShellClipboardImage({
  exec = execFileAsync,
  candidates = powershellCandidates(),
  stat = statPathKind
}: { candidates?: string[]; exec?: ExecFileFn; stat?: StatFn } = {}): Promise<ClipboardImageHit | null> {
  const encoded = encodePowerShellCommand(PS_SCRIPT)

  for (const ps of candidates) {
    try {
      const { stdout } = await exec(
        ps,
        ['-NoProfile', '-NonInteractive', '-STA', '-ExecutionPolicy', 'Bypass', '-EncodedCommand', encoded],
        {
          encoding: 'utf8',
          maxBuffer: 64 * 1024 * 1024,
          timeout: 8000,
          windowsHide: true
        }
      )
      const decoded = decodePowerShellClipboardPayload(stdout, stat)

      if (decoded) {
        return decoded
      }

      if (!String(stdout || '').trim()) {
        return null
      }
    } catch {
      // This powershell.exe is missing. Try the next candidate.
    }
  }

  return null
}

function clipboardWorthNativeFallback(clipboard: ElectronClipboardLike): boolean {
  // An empty Ctrl+V must not spawn PowerShell. A custom PNG format still shows
  // up in the format list even when readImage() and readBuffer() both miss it.
  try {
    if ((clipboard.availableFormats?.() ?? []).length > 0) {
      return true
    }
  } catch {
    // Fall through to the text check.
  }

  try {
    return Boolean((clipboard.readText?.() || '').trim())
  } catch {
    return false
  }
}

export async function readDesktopClipboardImage({
  clipboard,
  platform,
  stat = statPathKind,
  readPowerShell = readPowerShellClipboardImage
}: {
  clipboard: ElectronClipboardLike
  platform: NodeJS.Platform
  readPowerShell?: typeof readPowerShellClipboardImage
  stat?: StatFn
}): Promise<ClipboardImageHit | null> {
  const fromElectron = readElectronClipboardImage(clipboard, stat)

  if (fromElectron) {
    return fromElectron
  }

  if (platform !== 'win32' || !clipboardWorthNativeFallback(clipboard)) {
    return null
  }

  return readPowerShell({ stat })
}
