import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  decodePowerShellClipboardPayload,
  fileUrlToLocalPath,
  readDesktopClipboardImage,
  readElectronClipboardImage,
  readPowerShellClipboardImage,
  type ElectronClipboardLike,
  type StatFn
} from './windows-clipboard-image'

const PNG_SIGNATURE = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])

function pngBuffer() {
  return Buffer.concat([PNG_SIGNATURE, Buffer.alloc(16, 0x11)])
}

function clipboard(over: Partial<ElectronClipboardLike> = {}): ElectronClipboardLike {
  return {
    availableFormats: () => [],
    read: () => '',
    readBuffer: () => {
      throw new Error('missing format')
    },
    readImage: () => ({ isEmpty: () => true, toPNG: () => Buffer.alloc(0) }),
    readText: () => '',
    ...over
  }
}

const noFile: StatFn = () => null

test('readImage bytes win over every other clipboard format', () => {
  const png = pngBuffer()
  const hit = readElectronClipboardImage(
    clipboard({
      readBuffer: () => Buffer.from([0xff, 0xd8, 0xff, 0x00]),
      readImage: () => ({ isEmpty: () => false, toPNG: () => png })
    }),
    noFile
  )

  assert.deepEqual(hit, { bytes: png, ext: '.png', kind: 'bytes' })
})

test('a custom PNG clipboard format is used when readImage is empty', () => {
  const png = pngBuffer()
  const hit = readElectronClipboardImage(
    clipboard({
      readBuffer: format => (format === 'PNG' ? png : Buffer.alloc(0))
    }),
    noFile
  )

  assert.deepEqual(hit, { bytes: png, ext: '.png', kind: 'bytes' })
})

test('non-image buffers are ignored', () => {
  const hit = readElectronClipboardImage(
    clipboard({
      readBuffer: () => Buffer.from('not an image!!')
    }),
    noFile
  )

  assert.equal(hit, null)
})

test('fileUrlToLocalPath decodes a Windows file URL and rejects a bare name', () => {
  assert.equal(fileUrlToLocalPath('file:///C:/Users/hjinh/My%20Pictures/shot.png'), String.raw`C:\Users\hjinh\My Pictures\shot.png`)
  assert.equal(fileUrlToLocalPath('shot.png'), null)
  assert.equal(fileUrlToLocalPath('https://example.com/shot.png'), null)
})

test('an Explorer file drop is the image when there is no bitmap', () => {
  const shot = String.raw`C:\Users\hjinh\Pictures\shot.png`
  const hit = readElectronClipboardImage(
    clipboard({
      read: format => (format === 'text/uri-list' ? 'file:///C:/Users/hjinh/Pictures/shot.png' : '')
    }),
    path => (path === shot ? 'file' : null)
  )

  assert.deepEqual(hit, { kind: 'paths', paths: [shot] })
})

test('HTML file src from a screenshot tool is an image path', () => {
  const shot = String.raw`C:\Temp\shot.png`
  const hit = readElectronClipboardImage(
    clipboard({
      read: format => (format === 'text/html' ? '<img src="file:///C:/Temp/shot.png">' : '')
    }),
    path => (path === shot ? 'file' : null)
  )

  assert.deepEqual(hit, { kind: 'paths', paths: [shot] })
})

test('prose that mentions a png path is not an attachment', () => {
  const hit = readElectronClipboardImage(
    clipboard({
      readText: () => String.raw`see C:\Users\hjinh\Pictures\shot.png please`
    }),
    () => 'file'
  )

  assert.equal(hit, null)
})

test('a missing image path is not returned', () => {
  const hit = readElectronClipboardImage(
    clipboard({
      readText: () => String.raw`C:\Users\hjinh\Pictures\missing.png`
    }),
    noFile
  )

  assert.equal(hit, null)
})

test('decodePowerShellClipboardPayload prefers PNG bytes over dropped files', () => {
  const png = pngBuffer()
  const stdout = Buffer.from(JSON.stringify({ paths: [String.raw`C:\a.png`], png: png.toString('base64') })).toString('base64')
  const hit = decodePowerShellClipboardPayload(stdout, () => 'file')

  assert.equal(hit?.kind, 'bytes')

  if (hit?.kind === 'bytes') {
    assert.ok(hit.bytes.equals(png))
    assert.equal(hit.ext, '.png')
  }
})

test('decodePowerShellClipboardPayload accepts a single path emitted as a string', () => {
  const shot = String.raw`C:\Users\hjinh\Pictures\shot.png`
  const stdout = Buffer.from(JSON.stringify({ paths: shot, png: '' })).toString('base64')
  const hit = decodePowerShellClipboardPayload(stdout, path => (path === shot ? 'file' : null))

  assert.deepEqual(hit, { kind: 'paths', paths: [shot] })
})

test('decodePowerShellClipboardPayload keeps existing image files from a file drop', () => {
  const shot = String.raw`C:\Users\hjinh\Pictures\shot.png`
  const stdout = Buffer.from(JSON.stringify({ paths: [shot, String.raw`C:\notes.txt`], png: '' })).toString('base64')
  const hit = decodePowerShellClipboardPayload(stdout, path => (path === shot ? 'file' : null))

  assert.deepEqual(hit, { kind: 'paths', paths: [shot] })
})

test('readPowerShellClipboardImage decodes the first candidate that returns a PNG', async () => {
  const png = pngBuffer()
  const stdout = Buffer.from(JSON.stringify({ paths: [], png: png.toString('base64') })).toString('base64')
  const calls: string[] = []
  const hit = await readPowerShellClipboardImage({
    candidates: ['powershell.exe'],
    exec: async file => {
      calls.push(file)

      return { stdout }
    },
    stat: noFile
  })

  assert.equal(calls.length, 1)
  assert.equal(hit?.kind, 'bytes')
})

test('native Windows falls through to PowerShell only when Electron saw no image', async () => {
  let calls = 0
  const png = pngBuffer()
  const fromBitmap = await readDesktopClipboardImage({
    clipboard: clipboard({ readImage: () => ({ isEmpty: () => false, toPNG: () => png }) }),
    platform: 'win32',
    readPowerShell: async () => {
      calls += 1

      return null
    },
    stat: noFile
  })

  assert.equal(fromBitmap?.kind, 'bytes')
  assert.equal(calls, 0)

  const fromShell = await readDesktopClipboardImage({
    clipboard: clipboard({ availableFormats: () => ['PNG'] }),
    platform: 'win32',
    readPowerShell: async () => ({ bytes: png, ext: '.png', kind: 'bytes' }),
    stat: noFile
  })

  assert.equal(fromShell?.kind, 'bytes')

  calls = 0
  const empty = await readDesktopClipboardImage({
    clipboard: clipboard(),
    platform: 'win32',
    readPowerShell: async () => {
      calls += 1

      return null
    },
    stat: noFile
  })

  assert.equal(empty, null)
  assert.equal(calls, 0)

  calls = 0
  const mac = await readDesktopClipboardImage({
    clipboard: clipboard(),
    platform: 'darwin',
    readPowerShell: async () => {
      calls += 1

      return null
    },
    stat: noFile
  })

  assert.equal(mac, null)
  assert.equal(calls, 0)
})
