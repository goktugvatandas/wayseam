# SPDX-License-Identifier: MIT
# =====================================================================
# wayseam guest agent (Phase 2) -- /health + bearer-auth /exec
# =====================================================================
#
# Purpose
# -------
# HTTP server inside the Windows guest. Phase 1 shipped the bare
# /health readiness probe. Phase 2 adds bearer-auth and a POST /exec
# endpoint that runs base64-encoded PowerShell snippets -- replacing
# FreeRDP RemoteApp PowerShell calls for non-sensitive host -> guest
# traffic so the host can stop emitting visible PS-window flashes for
# every registry tweak / discovery roundtrip.
#
# Invariants
# ----------
#   * Bind: ``http://+:8765/`` (all interfaces inside the Windows VM).
#     QEMU's user-mode NAT forwards from the container to the VM's
#     slirp interface (10.0.2.15:8765, NOT 127.0.0.1:8765 -- slirp
#     hostfwd targets the VM's main NIC, not loopback). Binding only
#     to 127.0.0.1 inside Windows would mean slirp's forwarded packets
#     hit a closed port -- kernalix7 saw "Connection reset by peer" on
#     2026-04-30 from exactly this. Binding to ``+`` covers all
#     interfaces. The agent is still externally unreachable: compose's
#     ``127.0.0.1:8765:8765/tcp`` mapping is loopback-only on the host,
#     and QEMU slirp is private to the container.
#   * /health takes NO authentication. It is the readiness signal; the
#     host may probe it before the token has even been delivered.
#   * Every other endpoint requires `Authorization: Bearer <token>`.
#     Mismatch returns 401 with JSON {"error":"unauthorized"}. The
#     compare is constant-time so timing leaks can't recover the token.
#   * Wait-Token loop polls C:\OEM\agent_token.txt with bounded backoff,
#     never throws. Anti-goal #6 in AGENT_V2_DESIGN: throwing kills the
#     process and HKCU\Run does not auto-restart.
#   * The token is never logged or echoed back. /exec script content
#     lands in C:\OEM\agent.log only as a SHA256 hash, never the raw
#     payload -- sensitive payloads (registry keys, credentials touched
#     by self-heal) must not survive in the log.
# =====================================================================

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$script:AgentVersion = '0.2.39-wayseam'
$script:BlockedPointerButtons = @{}
$script:StartedAt    = (Get-Date).ToUniversalTime().ToString('o')
$script:OemDir       = 'C:\OEM'
$script:TokenPath    = 'C:\OEM\agent_token.txt'
$script:LogPath      = 'C:\OEM\agent.log'
$script:RunsDir      = 'C:\OEM\agent-runs'
$script:Prefix       = 'http://+:8765/'
$script:ExecDefaultTimeoutSec = 60
$script:ExecMaxTimeoutSec     = 300

if (-not (Test-Path $script:RunsDir)) {
    try { New-Item -ItemType Directory -Path $script:RunsDir -Force | Out-Null } catch { }
}

function Read-Token {
    if (-not (Test-Path $script:TokenPath)) { return $null }
    try {
        $t = (Get-Content -Path $script:TokenPath -TotalCount 1 -ErrorAction Stop).Trim()
    } catch { return $null }
    if (-not $t) { return $null }
    return $t
}

# Poll for the token file with backoff capped at 30s. The token is
# delivered via the OEM bind mount (config/oem/agent_token.txt staged
# at setup time -> /oem in the container -> C:\OEM\ inside Windows by
# dockur's first-boot copy), but we cannot assume any particular order
# between HKCU\Run firing and the OEM stage completing. We never throw
# here: throwing would kill the process and HKCU\Run does not respawn.
function Wait-Token {
    $delay = 2
    while ($true) {
        $t = Read-Token
        if ($t) { return $t }
        Start-Sleep -Seconds $delay
        if ($delay -lt 30) { $delay = [Math]::Min(30, $delay * 2) }
    }
}

# Constant-time string compare. Both inputs are ASCII hex on the happy
# path; we still walk the full length to avoid leaking string length
# via early-return timing. Returns $false on null / length mismatch
# without short-circuiting on content.
function Compare-Constant([string]$a, [string]$b) {
    if ($null -eq $a -or $null -eq $b) { return $false }
    if ($a.Length -ne $b.Length) { return $false }
    $diff = 0
    for ($i = 0; $i -lt $a.Length; $i++) {
        $diff = $diff -bor ([int][char]$a[$i] -bxor [int][char]$b[$i])
    }
    return ($diff -eq 0)
}

# Read the Authorization header off an HttpListener request, strip the
# "Bearer " prefix, and constant-time compare against $script:Token.
# Returns $true / $false -- never throws, never logs the supplied value.
function Test-Auth($req) {
    $h = $req.Headers['Authorization']
    if (-not $h) { return $false }
    if (-not $h.StartsWith('Bearer ')) { return $false }
    return (Compare-Constant -a $h.Substring(7) -b $script:Token)
}

function Read-Body($req) {
    if (-not $req.HasEntityBody) { return '' }
    $sr = [IO.StreamReader]::new($req.InputStream, $req.ContentEncoding)
    try { return $sr.ReadToEnd() } finally { $sr.Dispose() }
}

# SHA256 hex of arbitrary bytes -- used to log a fingerprint of /exec
# script payloads without spilling the script itself into agent.log.
function Get-BytesHash([byte[]]$bytes) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha.ComputeHash($bytes)
    } finally {
        $sha.Dispose()
    }
    return ([BitConverter]::ToString($hash) -replace '-','').ToLowerInvariant()
}

function Write-Log([string]$method, [string]$path, [string]$auth, [int]$code, [int]$ms, [string]$extra) {
    $ts = (Get-Date).ToUniversalTime().ToString('o')
    $line = "$ts $method $path auth=$auth code=$code ${ms}ms"
    if ($extra) { $line = "$line $extra" }
    try { Add-Content -Path $script:LogPath -Value $line -ErrorAction SilentlyContinue } catch { }
}

function Send-Json($resp, [int]$code, $obj) {
    $resp.StatusCode = $code
    $resp.ContentType = 'application/json; charset=utf-8'
    $bytes = [Text.Encoding]::UTF8.GetBytes((ConvertTo-Json -Compress -Depth 6 $obj))
    $resp.ContentLength64 = $bytes.Length
    $resp.OutputStream.Write($bytes, 0, $bytes.Length)
    $resp.OutputStream.Close()
}

function Send-Bytes($resp, [int]$code, [string]$contentType, [byte[]]$bytes) {
    $resp.StatusCode = $code
    $resp.ContentType = $contentType
    $resp.ContentLength64 = $bytes.Length
    $resp.OutputStream.Write($bytes, 0, $bytes.Length)
    $resp.OutputStream.Close()
}

# Wayseam's fallback surface seam. PrintWindow asks the target HWND to render
# itself into an off-screen bitmap; it does not sample/crop the desktop. The
# interactive delta route prefers the per-HWND Windows Graphics Capture helper
# initialized below and retains this path for unsupported or failed captures.
$script:WayseamCaptureAvailable = $false
$script:WayseamWgcAvailable = $false
try {
    Add-Type -AssemblyName System.Drawing
    Add-Type -ReferencedAssemblies System.Drawing -TypeDefinition @"
using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Imaging;
using System.IO;
using System.Runtime.InteropServices;
public static class WayseamNativeCapture {
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
  [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
  [StructLayout(LayoutKind.Sequential)] public struct CURSORINFO {
    public int cbSize; public int flags; public IntPtr hCursor; public POINT ptScreenPos;
  }
  [StructLayout(LayoutKind.Sequential)] public struct ICONINFO {
    [MarshalAs(UnmanagedType.Bool)] public bool fIcon;
    public int xHotspot, yHotspot; public IntPtr hbmMask, hbmColor;
  }
  [StructLayout(LayoutKind.Sequential)] public struct BITMAP {
    public int bmType, bmWidth, bmHeight, bmWidthBytes;
    public short bmPlanes, bmBitsPixel; public IntPtr bmBits;
  }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct DISPLAY_DEVICE {
    public int cb;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string DeviceName;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceString;
    public int StateFlags;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceID;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceKey;
  }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct DEVMODE {
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmDeviceName;
    public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
    public int dmFields;
    public int dmPositionX, dmPositionY, dmDisplayOrientation, dmDisplayFixedOutput;
    public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
    [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmFormName;
    public short dmLogPixels;
    public int dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency;
    public int dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2;
    public int dmPanningWidth, dmPanningHeight;
  }
  [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT {
    public int dx, dy; public uint mouseData, dwFlags, time; public UIntPtr dwExtraInfo;
  }
  [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT {
    public ushort wVk, wScan; public uint dwFlags, time; public UIntPtr dwExtraInfo;
  }
  [StructLayout(LayoutKind.Sequential)] public struct INPUT {
    public uint type; public MOUSEINPUT mi;
  }
  // Windows 11 is x64; native INPUT is a 40-byte tagged union with its
  // keyboard payload at offset 8. Keep pointer INPUT unchanged because it is
  // already proven by the drawing path, and use this exact sibling for keys.
  [StructLayout(LayoutKind.Explicit, Size=40)] public struct KEYINPUT {
    [FieldOffset(0)] public uint type;
    [FieldOffset(8)] public KEYBDINPUT ki;
  }
  public sealed class WindowInfo {
    public long hwnd;
    public long owner;
    public int pid;
    public string process_path;
    public string title;
    public string class_name;
    public int left, top, width, height;
  }
  public sealed class CursorFrame {
    public bool visible;
    public long shape;
    public int x, y, hot_x, hot_y, width, height;
    public byte[] png;
  }
  public sealed class ResizeResult {
    public bool moved, canvasChanged;
    public int actualWidth, actualHeight;
  }
  public delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr state);
  [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
  [DllImport("dwmapi.dll")] private static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);
  // The rectangle WGC actually captures: the DWM extended frame bounds
  // (visible window, no invisible resize borders). GetWindowRect can exceed
  // it by the border size — observed after a live DPI change — and any
  // geometry handed to the host must match the captured pixels exactly.
  public static bool GetVisibleRect(IntPtr h, out RECT r) {
    if (DwmGetWindowAttribute(h, 9, out r, System.Runtime.InteropServices.Marshal.SizeOf(typeof(RECT))) == 0 &&
        r.Right > r.Left && r.Bottom > r.Top) {
      return true;
    }
    return GetWindowRect(h, out r);
  }
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int width, int height, uint flags);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int command);
  [DllImport("user32.dll")] public static extern bool MoveWindow(IntPtr h, int x, int y, int width, int height, bool repaint);
  [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint command);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetWindowText(IntPtr h, System.Text.StringBuilder text, int count);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr h, System.Text.StringBuilder text, int count);
  [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc callback, IntPtr state);
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr dc, uint flags);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr h, uint message, IntPtr wparam, IntPtr lparam);
  [DllImport("user32.dll", SetLastError=true)] public static extern IntPtr SendMessageTimeout(IntPtr h, uint message, IntPtr wparam, IntPtr lparam, uint flags, uint timeout, out UIntPtr result);
  [DllImport("user32.dll", SetLastError=true)] public static extern uint SendInput(uint count, INPUT[] inputs, int size);
  [DllImport("user32.dll", EntryPoint="SendInput", SetLastError=true)] public static extern uint SendKeyboardInput(uint count, KEYINPUT[] inputs, int size);
  [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool EnumDisplayDevices(string device, uint index, ref DISPLAY_DEVICE output, uint flags);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern bool EnumDisplaySettings(string deviceName, int modeNum, ref DEVMODE devMode);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] public static extern int ChangeDisplaySettingsEx(string deviceName, ref DEVMODE devMode, IntPtr hwnd, int flags, IntPtr param);
  [DllImport("user32.dll")] public static extern bool GetCursorInfo(ref CURSORINFO info);
  [DllImport("user32.dll")] public static extern bool GetIconInfo(IntPtr icon, out ICONINFO info);
  [DllImport("user32.dll")] public static extern bool DrawIconEx(IntPtr dc, int x, int y, IntPtr icon, int width, int height, uint step, IntPtr brush, uint flags);
  [DllImport("gdi32.dll")] public static extern int GetObject(IntPtr handle, int bytes, out BITMAP bitmap);
  [DllImport("gdi32.dll")] public static extern bool DeleteObject(IntPtr handle);
  [DllImport("msvcrt.dll", CallingConvention=CallingConvention.Cdecl)]
  private static extern int memcmp(IntPtr first, IntPtr second, UIntPtr length);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern IntPtr OpenProcess(uint access, bool inherit, uint pid);
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern bool QueryFullProcessImageName(IntPtr process, uint flags, System.Text.StringBuilder path, ref uint size);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);

  private static bool SamePixel(byte[] pixels, int first, int second) {
    return pixels[first] == pixels[second]
      && pixels[first + 1] == pixels[second + 1]
      && pixels[first + 2] == pixels[second + 2]
      && pixels[first + 3] == pixels[second + 3];
  }

  private sealed class DeltaState {
    public int Width, Height, Sequence;
    public byte[] Pixels;
    public DateTime LastSeen;
    public readonly object Sync = new object();
    public Bitmap CaptureBitmap;
    public Graphics CaptureGraphics;
  }
  public sealed class DeltaCaptureResult {
    public bool Ok, AlphaRepaired;
    public byte[] Bytes;
  }
  private static readonly object DeltaLock = new object();
  private static readonly Dictionary<string, DeltaState> DeltaFrames =
    new Dictionary<string, DeltaState>();

  private static bool CompareMemory(IntPtr first, IntPtr second, int length) {
    return memcmp(first, second, new UIntPtr((uint)length)) == 0;
  }

  private static void WriteInt32(byte[] output, int offset, int value) {
    Buffer.BlockCopy(BitConverter.GetBytes(value), 0, output, offset, 4);
  }

  private static DeltaState GetDeltaState(string streamKey) {
    lock (DeltaLock) {
      DateTime now = DateTime.UtcNow;
      if (DeltaFrames.Count > 64) {
        List<string> expired = new List<string>();
        foreach (KeyValuePair<string, DeltaState> item in DeltaFrames) {
          if ((now - item.Value.LastSeen).TotalMinutes > 2) expired.Add(item.Key);
        }
        foreach (string key in expired) {
          DeltaState stale;
          if (!DeltaFrames.TryGetValue(key, out stale)) continue;
          DeltaFrames.Remove(key);
          lock (stale.Sync) {
            if (stale.CaptureGraphics != null) stale.CaptureGraphics.Dispose();
            if (stale.CaptureBitmap != null) stale.CaptureBitmap.Dispose();
          }
        }
      }
      DeltaState state;
      if (!DeltaFrames.TryGetValue(streamKey, out state)) {
        state = new DeltaState();
        DeltaFrames[streamKey] = state;
      }
      state.LastSeen = now;
      return state;
    }
  }

  private static byte[] EncodeDeltaFrameLocked(
      DeltaState state, Bitmap bitmap, int baseSequence) {
    int width = bitmap.Width, height = bitmap.Height, stride = width * 4;
    bool full = state.Pixels == null || state.Width != width || state.Height != height
      || state.Sequence != baseSequence;
    int minX = full ? 0 : width, minY = full ? 0 : height;
    int maxX = full ? width - 1 : -1, maxY = full ? height - 1 : -1;
    Rectangle area = new Rectangle(0, 0, width, height);
    BitmapData data = bitmap.LockBits(
      area, ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
    GCHandle previousHandle = default(GCHandle);
    try {
      if (full) {
        state.Pixels = new byte[stride * height];
        byte[] currentRow = new byte[stride];
        for (int y = 0; y < height; y++) {
          IntPtr rowPointer = IntPtr.Add(data.Scan0, y * data.Stride);
          Marshal.Copy(rowPointer, currentRow, 0, stride);
          Buffer.BlockCopy(currentRow, 0, state.Pixels, y * stride, stride);
        }
      } else {
        byte[] previous = state.Pixels;
        previousHandle = GCHandle.Alloc(previous, GCHandleType.Pinned);
        IntPtr previousBase = previousHandle.AddrOfPinnedObject();
        byte[] currentRow = new byte[stride];
        for (int y = 0; y < height; y++) {
          int row = y * stride;
          IntPtr rowPointer = IntPtr.Add(data.Scan0, y * data.Stride);
          if (CompareMemory(rowPointer, IntPtr.Add(previousBase, row), stride)) continue;
          Marshal.Copy(rowPointer, currentRow, 0, stride);
          for (int x = 0; x < width; x++) {
            int pixel = x * 4, oldPixel = row + pixel;
            if (currentRow[pixel] == previous[oldPixel]
                && currentRow[pixel + 1] == previous[oldPixel + 1]
                && currentRow[pixel + 2] == previous[oldPixel + 2]
                && currentRow[pixel + 3] == previous[oldPixel + 3]) continue;
            if (x < minX) minX = x; if (x > maxX) maxX = x;
            if (y < minY) minY = y; if (y > maxY) maxY = y;
          }
          Buffer.BlockCopy(currentRow, 0, previous, row, stride);
        }
      }
    } finally {
      if (previousHandle.IsAllocated) previousHandle.Free();
      bitmap.UnlockBits(data);
    }

      bool changed = maxX >= minX && maxY >= minY;
      int sequence = state.Sequence;
      int rectWidth = changed ? maxX - minX + 1 : 0;
      int rectHeight = changed ? maxY - minY + 1 : 0;
      if (changed) sequence++;
      byte[] output = new byte[36 + rectWidth * rectHeight * 4];
      output[0] = (byte)'W'; output[1] = (byte)'S';
      output[2] = (byte)'D'; output[3] = (byte)'1';
      WriteInt32(output, 4, width); WriteInt32(output, 8, height);
      WriteInt32(output, 12, stride); WriteInt32(output, 16, sequence);
      WriteInt32(output, 20, changed ? minX : 0);
      WriteInt32(output, 24, changed ? minY : 0);
      WriteInt32(output, 28, rectWidth); WriteInt32(output, 32, rectHeight);
      if (changed) {
        int rowBytes = rectWidth * 4;
        for (int row = 0; row < rectHeight; row++) {
          Buffer.BlockCopy(
            state.Pixels, (minY + row) * stride + minX * 4,
            output, 36 + row * rowBytes, rowBytes
          );
        }
      }
      state.Width = width; state.Height = height; state.Sequence = sequence;
      state.LastSeen = DateTime.UtcNow;
      return output;
  }

  // Keep one exact frame per host stream and return only the bounding BGRA
  // rectangle that changed. Equal rows are compared in native code, and only
  // damaged rows cross the managed/native boundary for a pixel scan.
  public static byte[] EncodeDeltaFrame(
      string streamKey, Bitmap bitmap, int baseSequence) {
    DeltaState state = GetDeltaState(streamKey);
    lock (state.Sync) {
      return EncodeDeltaFrameLocked(state, bitmap, baseSequence);
    }
  }

  // The interactive delta path keeps its capture bitmap and Graphics alive
  // for the stream lifetime. This avoids allocating and zeroing a multi-MiB
  // surface on every frame while preserving PrintWindow's full-content path.
  public static DeltaCaptureResult CaptureDeltaFrame(
      string streamKey, IntPtr hwnd, int width, int height,
      int baseSequence, bool repairPopupAlpha) {
    DeltaState state = GetDeltaState(streamKey);
    lock (state.Sync) {
      if (state.CaptureBitmap == null || state.CaptureBitmap.Width != width
          || state.CaptureBitmap.Height != height) {
        if (state.CaptureGraphics != null) state.CaptureGraphics.Dispose();
        if (state.CaptureBitmap != null) state.CaptureBitmap.Dispose();
        state.CaptureBitmap = new Bitmap(
          width, height, PixelFormat.Format32bppArgb);
        state.CaptureGraphics = Graphics.FromImage(state.CaptureBitmap);
      }
      IntPtr dc = state.CaptureGraphics.GetHdc();
      bool ok;
      try { ok = PrintWindow(hwnd, dc, 2); }
      finally { state.CaptureGraphics.ReleaseHdc(dc); }
      if (!ok) return new DeltaCaptureResult { Ok = false };
      bool alphaRepaired = repairPopupAlpha && ClearLostPopupAlpha(state.CaptureBitmap);
      return new DeltaCaptureResult {
        Ok = true,
        AlphaRepaired = alphaRepaired,
        Bytes = EncodeDeltaFrameLocked(state, state.CaptureBitmap, baseSequence)
      };
    }
  }

  // Lossless BGRA PackBits-style encoding. System.Drawing's PNG encoder is
  // the dominant cost for large interactive windows; this keeps exact pixels
  // while making flat UI regions cheap to encode and decode.
  public static byte[] EncodeRleFrame(Bitmap bitmap) {
    int width = bitmap.Width, height = bitmap.Height, stride = width * 4;
    Rectangle area = new Rectangle(0, 0, width, height);
    BitmapData data = bitmap.LockBits(area, ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
    byte[] pixels = new byte[stride * height];
    try {
      for (int y = 0; y < height; y++) {
        Marshal.Copy(IntPtr.Add(data.Scan0, y * data.Stride), pixels, y * stride, stride);
      }
    } finally { bitmap.UnlockBits(data); }

    int pixelCount = width * height;
    byte[] output = new byte[pixels.Length + (pixelCount / 127) + 32];
    output[0] = (byte)'W'; output[1] = (byte)'S';
    output[2] = (byte)'R'; output[3] = (byte)'1';
    Buffer.BlockCopy(BitConverter.GetBytes(width), 0, output, 4, 4);
    Buffer.BlockCopy(BitConverter.GetBytes(height), 0, output, 8, 4);
    Buffer.BlockCopy(BitConverter.GetBytes(stride), 0, output, 12, 4);
    int sourcePixel = 0, target = 16;
    while (sourcePixel < pixelCount) {
      int run = 1;
      while (run < 127 && sourcePixel + run < pixelCount
          && SamePixel(pixels, sourcePixel * 4, (sourcePixel + run) * 4)) run++;
      if (run >= 3) {
        output[target++] = (byte)(0x80 | (run - 1));
        Buffer.BlockCopy(pixels, sourcePixel * 4, output, target, 4);
        target += 4; sourcePixel += run;
        continue;
      }

      int literalStart = sourcePixel;
      sourcePixel += run;
      while (sourcePixel < pixelCount && sourcePixel - literalStart < 127) {
        int nextRun = 1;
        while (nextRun < 3 && sourcePixel + nextRun < pixelCount
            && SamePixel(pixels, sourcePixel * 4, (sourcePixel + nextRun) * 4)) nextRun++;
        if (nextRun >= 3) break;
        sourcePixel += Math.Min(nextRun, 127 - (sourcePixel - literalStart));
      }
      int literalCount = sourcePixel - literalStart;
      output[target++] = (byte)(literalCount - 1);
      Buffer.BlockCopy(pixels, literalStart * 4, output, target, literalCount * 4);
      target += literalCount * 4;
    }
    Array.Resize(ref output, target);
    return output;
  }

  // Send one absolute pointer packet through Windows' input stream. The
  // NOCOALESCE bit is essential for drawing: without it Windows is allowed
  // to discard all but the newest WM_MOUSEMOVE while Paint is busy, reducing
  // a curved stroke to a straight segment between press and release.
  public static int HitTestWindow(IntPtr hwnd, int screenX, int screenY) {
    // WM_NCHITTEST receives signed screen coordinates packed into LPARAM.
    // Fail open as HTCLIENT when an application is hung: input remains usable
    // and the bounded timeout prevents a frozen app from stalling Wayseam.
    int packed = (screenX & 0xffff) | ((screenY & 0xffff) << 16);
    UIntPtr result;
    IntPtr sent = SendMessageTimeout(
      hwnd, 0x0084, IntPtr.Zero, new IntPtr(packed), 0x0002, 75, out result
    );
    return sent == IntPtr.Zero ? 1 : unchecked((int)result.ToUInt64());
  }

  public static bool IsWindowManagementHit(int hitTest) {
    // The compositor owns move, maximize, minimize, and resize in Wayseam
    // Mode. Keep HTCLOSE (20) out of this list so the rendered close button
    // retains its normal Windows WM_CLOSE behavior.
    // HTCAPTION (2) is deliberately NOT blocked: modern apps put real
    // controls (Notepad Settings' back button, tab strips) in their caption
    // strip. Double-click-maximize is prevented by stripping WS_MAXIMIZEBOX
    // from presented windows instead.
    return hitTest == 8 || hitTest == 9 ||
      (hitTest >= 10 && hitTest <= 18) || hitTest == 21;
  }

  public static bool InjectPointer(int screenX, int screenY, uint buttonFlags) {
    int left = GetSystemMetrics(76), top = GetSystemMetrics(77);
    int width = Math.Max(1, GetSystemMetrics(78));
    int height = Math.Max(1, GetSystemMetrics(79));
    int dx = (int)Math.Round((screenX - left) * 65535.0 / Math.Max(1, width - 1));
    int dy = (int)Math.Round((screenY - top) * 65535.0 / Math.Max(1, height - 1));
    dx = Math.Max(0, Math.Min(65535, dx));
    dy = Math.Max(0, Math.Min(65535, dy));
    INPUT input = new INPUT {
      type = 0,
      mi = new MOUSEINPUT {
        dx = dx, dy = dy, mouseData = 0,
        dwFlags = 0x0001 | 0x2000 | 0x4000 | 0x8000 | buttonFlags,
        time = 0, dwExtraInfo = UIntPtr.Zero
      }
    };
    return SendInput(1, new INPUT[] { input }, Marshal.SizeOf(typeof(INPUT))) == 1;
  }

  // Scroll-wheel delivery. The pointer is moved first so Windows routes the
  // wheel to the HWND beneath the host cursor (apps that scroll the window
  // under the pointer rather than the focused one, like browsers, rely on
  // this). deltaY/deltaX carry raw WHEEL_DELTA units: +120 is one notch up.
  public static bool InjectWheel(int screenX, int screenY, int deltaY, int deltaX) {
    if (!InjectPointer(screenX, screenY, 0)) return false;
    if (deltaY != 0 && !InjectWheelAxis(unchecked((uint)deltaY), 0x0800)) return false;
    if (deltaX != 0 && !InjectWheelAxis(unchecked((uint)deltaX), 0x1000)) return false;
    return true;
  }

  private static bool InjectWheelAxis(uint amount, uint axisFlag) {
    INPUT input = new INPUT {
      type = 0,
      mi = new MOUSEINPUT {
        dx = 0, dy = 0, mouseData = amount, dwFlags = axisFlag,
        time = 0, dwExtraInfo = UIntPtr.Zero
      }
    };
    return SendInput(1, new INPUT[] { input }, Marshal.SizeOf(typeof(INPUT))) == 1;
  }

  // Preserve an already-focused owned popup; otherwise activate this
  // Wayseam root before injecting. This keeps keyboard focus within the
  // selected application without collapsing its menus/dialogs.
  public static bool ActivateWindowFamily(IntPtr root) {
    if (!IsWindow(root)) return false;
    IntPtr current = GetForegroundWindow();
    while (current != IntPtr.Zero) {
      if (current == root) return true;
      current = GetWindow(current, 4); // GW_OWNER
    }
    return SetForegroundWindow(root);
  }

  public static bool InjectVirtualKey(int virtualKey, bool down, bool extended) {
    uint flags = down ? 0u : 0x0002u; // KEYEVENTF_KEYUP
    if (extended) flags |= 0x0001u;   // KEYEVENTF_EXTENDEDKEY
    KEYINPUT input = new KEYINPUT {
      type = 1,
      ki = new KEYBDINPUT {
        wVk = (ushort)virtualKey, wScan = 0, dwFlags = flags,
        time = 0, dwExtraInfo = UIntPtr.Zero
      }
    };
    return SendKeyboardInput(
      1, new KEYINPUT[] { input }, Marshal.SizeOf(typeof(KEYINPUT))
    ) == 1;
  }

  public static bool InjectUnicode(int codepoint) {
    string text;
    try { text = Char.ConvertFromUtf32(codepoint); }
    catch (ArgumentOutOfRangeException) { return false; }
    foreach (char unit in text) {
      KEYINPUT down = new KEYINPUT {
        type = 1,
        ki = new KEYBDINPUT {
          wVk = 0, wScan = unit, dwFlags = 0x0004,
          time = 0, dwExtraInfo = UIntPtr.Zero
        }
      };
      KEYINPUT up = down;
      up.ki.dwFlags = 0x0004 | 0x0002;
      if (SendKeyboardInput(
          1, new KEYINPUT[] { down }, Marshal.SizeOf(typeof(KEYINPUT))) != 1) return false;
      if (SendKeyboardInput(
          1, new KEYINPUT[] { up }, Marshal.SizeOf(typeof(KEYINPUT))) != 1) return false;
    }
    return true;
  }

  public static bool CloseWindow(IntPtr hwnd) {
    return IsWindow(hwnd) && PostMessage(hwnd, 0x0010, IntPtr.Zero, IntPtr.Zero);
  }

  private static readonly object DisplayLock = new object();
  private static bool HaveOriginalDisplayMode;
  private static int OriginalDisplayWidth, OriginalDisplayHeight;

  private static DEVMODE NewDisplayMode() {
    DEVMODE mode = new DEVMODE();
    mode.dmDeviceName = new string('\0', 32);
    mode.dmFormName = new string('\0', 32);
    mode.dmSize = (short)Marshal.SizeOf(typeof(DEVMODE));
    return mode;
  }

  private static bool PrimaryDisplay(out string name, out DEVMODE current) {
    name = null;
    current = NewDisplayMode();
    for (uint index = 0; index < 16; index++) {
      DISPLAY_DEVICE display = new DISPLAY_DEVICE();
      display.cb = Marshal.SizeOf(typeof(DISPLAY_DEVICE));
      if (!EnumDisplayDevices(null, index, ref display, 0)) break;
      // DISPLAY_DEVICE_ATTACHED_TO_DESKTOP | DISPLAY_DEVICE_PRIMARY_DEVICE
      if ((display.StateFlags & 5) != 5) continue;
      name = display.DeviceName;
      return EnumDisplaySettings(name, -1, ref current);
    }
    return false;
  }

  private static bool FindDisplayMode(
      string device, int width, int height, out DEVMODE selected) {
    selected = NewDisplayMode();
    for (int index = 0; index < 4096; index++) {
      DEVMODE candidate = NewDisplayMode();
      if (!EnumDisplaySettings(device, index, ref candidate)) break;
      if (candidate.dmPelsWidth == width && candidate.dmPelsHeight == height) {
        selected = candidate;
        return true;
      }
    }
    return false;
  }

  public static bool EnsureDisplayCanvas(int requiredWidth, int requiredHeight) {
    lock (DisplayLock) {
      string device;
      DEVMODE current;
      if (!PrimaryDisplay(out device, out current)) return false;
      if (current.dmPelsWidth >= requiredWidth && current.dmPelsHeight >= requiredHeight)
        return false;

      bool found = false;
      DEVMODE selected = NewDisplayMode();
      long selectedArea = Int64.MaxValue;
      for (int index = 0; index < 4096; index++) {
        DEVMODE candidate = NewDisplayMode();
        if (!EnumDisplaySettings(device, index, ref candidate)) break;
        if (candidate.dmPelsWidth < requiredWidth || candidate.dmPelsHeight < requiredHeight)
          continue;
        long area = (long)candidate.dmPelsWidth * candidate.dmPelsHeight;
        if (!found || area < selectedArea ||
            (area == selectedArea && candidate.dmPelsWidth < selected.dmPelsWidth)) {
          selected = candidate;
          selectedArea = area;
          found = true;
        }
      }
      if (!found || ChangeDisplaySettingsEx(device, ref selected, IntPtr.Zero, 2, IntPtr.Zero) != 0)
        return false;

      if (!HaveOriginalDisplayMode) {
        OriginalDisplayWidth = current.dmPelsWidth;
        OriginalDisplayHeight = current.dmPelsHeight;
      }
      if (ChangeDisplaySettingsEx(device, ref selected, IntPtr.Zero, 0, IntPtr.Zero) != 0)
        return false;
      HaveOriginalDisplayMode = true;
      System.Threading.Thread.Sleep(50);
      return true;
    }
  }

  public static bool RestoreDisplayCanvas() {
    lock (DisplayLock) {
      if (!HaveOriginalDisplayMode) return true;
      string device;
      DEVMODE current;
      DEVMODE original;
      if (!PrimaryDisplay(out device, out current) ||
          !FindDisplayMode(device, OriginalDisplayWidth, OriginalDisplayHeight, out original) ||
          ChangeDisplaySettingsEx(device, ref original, IntPtr.Zero, 2, IntPtr.Zero) != 0 ||
          ChangeDisplaySettingsEx(device, ref original, IntPtr.Zero, 0, IntPtr.Zero) != 0)
        return false;
      HaveOriginalDisplayMode = false;
      return true;
    }
  }

  private static readonly object SlotLock = new object();
  private static readonly System.Collections.Generic.Dictionary<IntPtr, int>
    WindowSlots = new System.Collections.Generic.Dictionary<IntPtr, int>();

  // Every presented window used to land at guest (0,0), so they physically
  // overlapped and Windows' scroll-under-cursor routing sent wheel input to
  // whichever was topmost — scrolling one host tile scrolled another app.
  // Assign each presented HWND its own quadrant of the guest canvas instead.
  private static void GuestOrigin(IntPtr hwnd, int width, int height,
      out int originX, out int originY) {
    int slot;
    lock (SlotLock) {
      var dead = new System.Collections.Generic.List<IntPtr>();
      foreach (var pair in WindowSlots)
        if (!IsWindow(pair.Key)) dead.Add(pair.Key);
      foreach (var key in dead) WindowSlots.Remove(key);
      if (!WindowSlots.TryGetValue(hwnd, out slot)) {
        var used = new System.Collections.Generic.HashSet<int>(WindowSlots.Values);
        slot = 0;
        while (used.Contains(slot)) slot++;
        WindowSlots[hwnd] = slot;
      }
    }
    slot = slot % 4;
    string device; DEVMODE mode;
    int canvasW = 3840, canvasH = 2160;
    if (PrimaryDisplay(out device, out mode)) {
      canvasW = (int)mode.dmPelsWidth; canvasH = (int)mode.dmPelsHeight;
    }
    originX = (slot % 2) * (canvasW / 2);
    originY = (slot / 2) * (canvasH / 2);
    // Injected input needs the whole window on-screen; clamp toward the
    // canvas edge (may overlap again for near-fullscreen tiles, but input
    // stays deliverable and small tiles — the common case — never overlap).
    originX = Math.Max(0, Math.Min(originX, canvasW - width));
    originY = Math.Max(0, Math.Min(originY, canvasH - height));
  }

  public static ResizeResult ResizeForHost(
      IntPtr hwnd, int width, int height) {
    return ResizeForHost(hwnd, width, height, -1, -1);
  }

  public static ResizeResult ResizeForHost(
      IntPtr hwnd, int width, int height, int hostX, int hostY) {
    // The host mirrors its tile layout: each presented window sits at its
    // tile's compositor position, so any number of tiles coexist without
    // overlap. Negative coordinates mean "not provided" — fall back to the
    // quadrant allocator (older hosts, floating edge cases).
    int originX, originY;
    bool canvasChanged;
    if (hostX >= 0 && hostY >= 0) {
      originX = hostX; originY = hostY;
      canvasChanged = EnsureDisplayCanvas(hostX + width, hostY + height);
      string device; DEVMODE mode;
      if (PrimaryDisplay(out device, out mode)) {
        originX = Math.Max(0, Math.Min(originX, (int)mode.dmPelsWidth - width));
        originY = Math.Max(0, Math.Min(originY, (int)mode.dmPelsHeight - height));
      }
    } else {
      canvasChanged = EnsureDisplayCanvas(width, height);
      GuestOrigin(hwnd, width, height, out originX, out originY);
    }
    ShowWindow(hwnd, 9); // SW_RESTORE: the host compositor owns maximize state.
    // The host requests the size of the VISIBLE window (what WGC captures and
    // what its tile shows). MoveWindow takes the OUTER rect, which exceeds the
    // visible one by the invisible resize borders; compensate so the visible
    // rectangle lands exactly at the assigned origin with the requested size —
    // otherwise a few-pixel letterbox band ("fit" gap) shows around every window.
    int padLeft = 0, padTop = 0, padWidth = 0, padHeight = 0;
    RECT outerBefore, visibleBefore;
    if (GetWindowRect(hwnd, out outerBefore) && GetVisibleRect(hwnd, out visibleBefore)) {
      padLeft = visibleBefore.Left - outerBefore.Left;
      padTop = visibleBefore.Top - outerBefore.Top;
      padWidth = (outerBefore.Right - outerBefore.Left) -
        (visibleBefore.Right - visibleBefore.Left);
      padHeight = (outerBefore.Bottom - outerBefore.Top) -
        (visibleBefore.Bottom - visibleBefore.Top);
      if (padLeft < 0 || padLeft > 32 || padTop < 0 || padTop > 32 ||
          padWidth < 0 || padWidth > 64 || padHeight < 0 || padHeight > 64) {
        padLeft = 0; padTop = 0; padWidth = 0; padHeight = 0;
      }
    }
    bool moved = MoveWindow(
      hwnd, originX - padLeft, originY - padTop,
      width + padWidth, height + padHeight, true);
    RECT actual;
    if (!GetVisibleRect(hwnd, out actual))
      return new ResizeResult { moved = false, canvasChanged = canvasChanged };
    return new ResizeResult {
      moved = moved,
      canvasChanged = canvasChanged,
      actualWidth = actual.Right - actual.Left,
      actualHeight = actual.Bottom - actual.Top
    };
  }

  public static int SetShellVisible(bool visible) {
    int changed = 0;
    EnumWindows(delegate(IntPtr hwnd, IntPtr state) {
      var className = new System.Text.StringBuilder(256);
      GetClassName(hwnd, className, className.Capacity);
      string value = className.ToString();
      if (value != "Shell_TrayWnd" && value != "Shell_SecondaryTrayWnd") return true;
      ShowWindow(hwnd, visible ? 5 : 0); // SW_SHOW / SW_HIDE
      changed++;
      return true;
    }, IntPtr.Zero);
    return changed;
  }

  public static WindowInfo[] OwnedWindows(IntPtr root) {
    var result = new System.Collections.Generic.List<WindowInfo>();
    EnumWindows(delegate(IntPtr hwnd, IntPtr state) {
      if (hwnd == root || !IsWindowVisible(hwnd)) return true;
      IntPtr owner = GetWindow(hwnd, 4); // GW_OWNER
      IntPtr ancestor = owner;
      while (ancestor != IntPtr.Zero && ancestor != root) ancestor = GetWindow(ancestor, 4);
      if (ancestor != root) return true;
      RECT rect;
      if (!GetVisibleRect(hwnd, out rect) || rect.Right <= rect.Left || rect.Bottom <= rect.Top) return true;
      var title = new System.Text.StringBuilder(512);
      var className = new System.Text.StringBuilder(256);
      GetWindowText(hwnd, title, title.Capacity);
      GetClassName(hwnd, className, className.Capacity);
      result.Add(new WindowInfo {
        hwnd = hwnd.ToInt64(), owner = owner.ToInt64(), title = title.ToString(),
        class_name = className.ToString(), left = rect.Left, top = rect.Top,
        width = rect.Right - rect.Left, height = rect.Bottom - rect.Top
      });
      return true;
    }, IntPtr.Zero);
    return result.ToArray();
  }

  public static bool IsIndependentTopLevel(IntPtr hwnd, IntPtr owner, RECT rect) {
    if (owner == IntPtr.Zero) return true;
    RECT ownerRect;
    if (!GetVisibleRect(owner, out ownerRect)) return false;
    int width = rect.Right - rect.Left, height = rect.Bottom - rect.Top;
    int ownerWidth = ownerRect.Right - ownerRect.Left;
    int ownerHeight = ownerRect.Bottom - ownerRect.Top;
    return width >= 640 && height >= 480 &&
      (long)width * 10 >= (long)ownerWidth * 9 &&
      (long)height * 10 >= (long)ownerHeight * 9;
  }

  private static string ProcessPath(uint pid) {
    IntPtr process = OpenProcess(0x1000, false, pid); // PROCESS_QUERY_LIMITED_INFORMATION
    if (process == IntPtr.Zero) return "";
    try {
      uint size = 32768;
      var path = new System.Text.StringBuilder((int)size);
      return QueryFullProcessImageName(process, 0, path, ref size) ? path.ToString() : "";
    } finally { CloseHandle(process); }
  }

  public static WindowInfo[] TopLevelWindows() {
    var result = new System.Collections.Generic.List<WindowInfo>();
    EnumWindows(delegate(IntPtr hwnd, IntPtr state) {
      if (!IsWindowVisible(hwnd)) return true;
      IntPtr owner = GetWindow(hwnd, 4); // GW_OWNER
      RECT rect;
      if (!GetVisibleRect(hwnd, out rect)
          || rect.Right - rect.Left < 64 || rect.Bottom - rect.Top < 64) return true;
      if (!IsIndependentTopLevel(hwnd, owner, rect)) return true;
      uint pid;
      GetWindowThreadProcessId(hwnd, out pid);
      string processPath = ProcessPath(pid);
      if (pid == 0 || String.IsNullOrWhiteSpace(processPath)) return true;
      var title = new System.Text.StringBuilder(512);
      var className = new System.Text.StringBuilder(256);
      GetWindowText(hwnd, title, title.Capacity);
      GetClassName(hwnd, className, className.Capacity);
      result.Add(new WindowInfo {
        hwnd = hwnd.ToInt64(), owner = owner.ToInt64(), pid = (int)pid,
        process_path = processPath, title = title.ToString(),
        class_name = className.ToString(), left = rect.Left, top = rect.Top,
        width = rect.Right - rect.Left, height = rect.Bottom - rect.Top
      });
      return result.Count < 256;
    }, IntPtr.Zero);
    return result.ToArray();
  }

  private static void DrawCursorOn(Bitmap bitmap, Color background, IntPtr cursor, int width, int height) {
    using (Graphics graphics = Graphics.FromImage(bitmap)) {
      graphics.Clear(background);
      IntPtr dc = graphics.GetHdc();
      try { DrawIconEx(dc, 0, 0, cursor, width, height, 0, IntPtr.Zero, 3); }
      finally { graphics.ReleaseHdc(dc); }
    }
  }

  // Windows renders the pointer outside every HWND, so PrintWindow can never
  // include it. Render the cursor against black and white, then recover its
  // alpha and foreground color from the two composites. This also handles
  // classic monochrome cursors whose mask does not carry an alpha channel.
  public static CursorFrame CaptureCursor(IntPtr root) {
    RECT rootRect;
    if (!IsWindow(root) || !GetVisibleRect(root, out rootRect)) return null;
    CURSORINFO cursor = new CURSORINFO();
    cursor.cbSize = Marshal.SizeOf(typeof(CURSORINFO));
    if (!GetCursorInfo(ref cursor) || (cursor.flags & 1) == 0 || cursor.hCursor == IntPtr.Zero) {
      return new CursorFrame { visible = false, png = new byte[0] };
    }
    ICONINFO icon;
    if (!GetIconInfo(cursor.hCursor, out icon)) {
      return new CursorFrame { visible = false, png = new byte[0] };
    }
    try {
      BITMAP nativeBitmap;
      int width = 0, height = 0;
      if (icon.hbmColor != IntPtr.Zero && GetObject(icon.hbmColor, Marshal.SizeOf(typeof(BITMAP)), out nativeBitmap) != 0) {
        width = nativeBitmap.bmWidth; height = Math.Abs(nativeBitmap.bmHeight);
      } else if (icon.hbmMask != IntPtr.Zero && GetObject(icon.hbmMask, Marshal.SizeOf(typeof(BITMAP)), out nativeBitmap) != 0) {
        width = nativeBitmap.bmWidth; height = Math.Abs(nativeBitmap.bmHeight) / 2;
      }
      if (width < 1 || height < 1 || width > 256 || height > 256) {
        return new CursorFrame { visible = false, png = new byte[0] };
      }
      using (Bitmap black = new Bitmap(width, height, PixelFormat.Format32bppArgb))
      using (Bitmap white = new Bitmap(width, height, PixelFormat.Format32bppArgb))
      using (Bitmap output = new Bitmap(width, height, PixelFormat.Format32bppArgb)) {
        DrawCursorOn(black, Color.Black, cursor.hCursor, width, height);
        DrawCursorOn(white, Color.White, cursor.hCursor, width, height);
        // Two passes. Pass 1 classifies XOR/invert pixels (the text I-beam):
        // they render bright on the black probe and dark on the white probe,
        // which the naive alpha math turns into an opaque WHITE beam —
        // invisible on light text areas. Pass 2 draws invert pixels as a
        // black core and gives their transparent neighbours a white halo, so
        // the beam reads on both light and dark backgrounds (the "shadow"
        // Windows itself provides via runtime inversion).
        bool[,] invert = new bool[width, height];
        for (int y = 0; y < height; y++) {
          for (int x = 0; x < width; x++) {
            Color b = black.GetPixel(x, y), w = white.GetPixel(x, y);
            // Normal alpha compositing guarantees the white-probe render is
            // at least as bright as the black-probe one (w-b = (1-a)*255).
            // A pixel where the BLACK probe is clearly brighter can only be
            // XOR/invert content — including the antialiased edge pixels the
            // old exact thresholds missed (they produced a washed-out gray
            // beam instead of a visible one).
            invert[x, y] = (b.R + b.G + b.B) > (w.R + w.G + w.B) + 75;
          }
        }
        for (int y = 0; y < height; y++) {
          for (int x = 0; x < width; x++) {
            if (invert[x, y]) {
              output.SetPixel(x, y, Color.FromArgb(255, 15, 15, 15));
              continue;
            }
            Color b = black.GetPixel(x, y), w = white.GetPixel(x, y);
            int background = ((w.R - b.R) + (w.G - b.G) + (w.B - b.B)) / 3;
            int alpha = Math.Max(0, Math.Min(255, 255 - background));
            if (alpha < 4) {
              bool halo = false;
              for (int dy = -1; dy <= 1 && !halo; dy++) {
                for (int dx = -1; dx <= 1 && !halo; dx++) {
                  int nx = x + dx, ny = y + dy;
                  if (nx >= 0 && ny >= 0 && nx < width && ny < height && invert[nx, ny])
                    halo = true;
                }
              }
              output.SetPixel(x, y, halo
                ? Color.FromArgb(230, 250, 250, 250)
                : Color.Transparent);
              continue;
            }
            int red = Math.Max(0, Math.Min(255, b.R * 255 / alpha));
            int green = Math.Max(0, Math.Min(255, b.G * 255 / alpha));
            int blue = Math.Max(0, Math.Min(255, b.B * 255 / alpha));
            output.SetPixel(x, y, Color.FromArgb(alpha, red, green, blue));
          }
        }
        // Pure-bright cursors (the Windows 11 white text beam) have no dark
        // pixel anywhere, so they vanish on light backgrounds — natively the
        // system draws them with a contrast treatment the capture loses.
        // Give such cursors a dark rim on their transparent neighbours.
        int opaqueDark = 0, opaqueBright = 0;
        for (int y = 0; y < height; y++) {
          for (int x = 0; x < width; x++) {
            Color px = output.GetPixel(x, y);
            if (px.A < 150) continue;
            int lum = px.R + px.G + px.B;
            if (lum < 300) opaqueDark++;
            else if (lum > 540) opaqueBright++;
          }
        }
        if (opaqueDark == 0 && opaqueBright > 0) {
          bool[,] bright = new bool[width, height];
          for (int y = 0; y < height; y++)
            for (int x = 0; x < width; x++) {
              Color px = output.GetPixel(x, y);
              bright[x, y] = px.A >= 150 && (px.R + px.G + px.B) > 540;
            }
          for (int y = 0; y < height; y++) {
            for (int x = 0; x < width; x++) {
              if (output.GetPixel(x, y).A >= 40) continue;
              bool rim = false;
              for (int dy = -1; dy <= 1 && !rim; dy++)
                for (int dx = -1; dx <= 1 && !rim; dx++) {
                  int nx = x + dx, ny = y + dy;
                  if (nx >= 0 && ny >= 0 && nx < width && ny < height && bright[nx, ny])
                    rim = true;
                }
              if (rim) output.SetPixel(x, y, Color.FromArgb(215, 25, 25, 25));
            }
          }
        }
        using (MemoryStream stream = new MemoryStream()) {
          output.Save(stream, ImageFormat.Png);
          return new CursorFrame {
            visible = true, shape = cursor.hCursor.ToInt64(),
            x = cursor.ptScreenPos.X - rootRect.Left,
            y = cursor.ptScreenPos.Y - rootRect.Top,
            hot_x = icon.xHotspot, hot_y = icon.yHotspot,
            width = width, height = height, png = stream.ToArray()
          };
        }
      }
    } finally {
      if (icon.hbmColor != IntPtr.Zero) DeleteObject(icon.hbmColor);
      if (icon.hbmMask != IntPtr.Zero) DeleteObject(icon.hbmMask);
    }
  }

  // PrintWindow renders DWM-transparent popup corners as opaque black. Only
  // repair black pixels connected to the bitmap border, and only when the
  // center is clearly light; this preserves black text/icons inside the menu
  // and refuses to alter dark-themed windows.
  public static bool ClearLostPopupAlpha(Bitmap bitmap) {
    int width = bitmap.Width, height = bitmap.Height;
    if (width < 3 || height < 3) return false;
    Color center = bitmap.GetPixel(width / 2, height / 2);
    if (center.R + center.G + center.B < 192) return false;
    Color[] corners = {
      bitmap.GetPixel(0, 0), bitmap.GetPixel(width - 1, 0),
      bitmap.GetPixel(0, height - 1), bitmap.GetPixel(width - 1, height - 1)
    };
    int darkCorners = 0;
    foreach (Color c in corners) if (c.R <= 32 && c.G <= 32 && c.B <= 32) darkCorners++;
    if (darkCorners < 3) return false;

    Rectangle area = new Rectangle(0, 0, width, height);
    BitmapData data = bitmap.LockBits(area, ImageLockMode.ReadWrite, PixelFormat.Format32bppArgb);
    try {
      int length = Math.Abs(data.Stride) * height;
      byte[] pixels = new byte[length];
      Marshal.Copy(data.Scan0, pixels, 0, length);
      bool[] seen = new bool[width * height];
      Queue<int> queue = new Queue<int>();
      Action<int, int> seed = delegate(int x, int y) {
        int pixel = y * data.Stride + x * 4;
        if (pixels[pixel] <= 48 && pixels[pixel + 1] <= 48 && pixels[pixel + 2] <= 48) {
          int index = y * width + x;
          if (!seen[index]) { seen[index] = true; queue.Enqueue(index); }
        }
      };
      for (int x = 0; x < width; x++) { seed(x, 0); seed(x, height - 1); }
      for (int y = 1; y < height - 1; y++) { seed(0, y); seed(width - 1, y); }
      int[] dx = { -1, 1, 0, 0 }, dy = { 0, 0, -1, 1 };
      while (queue.Count > 0) {
        int index = queue.Dequeue();
        int x = index % width, y = index / width;
        pixels[y * data.Stride + x * 4 + 3] = 0;
        for (int direction = 0; direction < 4; direction++) {
          int nx = x + dx[direction], ny = y + dy[direction];
          if (nx < 0 || ny < 0 || nx >= width || ny >= height) continue;
          int next = ny * width + nx;
          int pixel = ny * data.Stride + nx * 4;
          if (!seen[next] && pixels[pixel] <= 48 && pixels[pixel + 1] <= 48 && pixels[pixel + 2] <= 48) {
            seen[next] = true; queue.Enqueue(next);
          }
        }
      }
      Marshal.Copy(pixels, 0, data.Scan0, length);
      return true;
    } finally { bitmap.UnlockBits(data); }
  }
}
"@
    $script:WayseamCaptureAvailable = $true
} catch {
    try {
        Add-Content -Path $script:LogPath -Value (
            "$((Get-Date).ToUniversalTime().ToString('o')) WARN Wayseam capture unavailable"
        ) -ErrorAction SilentlyContinue
    } catch { }
}

# Wayseam host services: bounded text clipboard access and interactive
# session state. These are separate from capture so a capture compile failure
# never disables mode switching, and vice versa. Clipboard access uses the
# raw Win32 clipboard (no OLE, no STA requirement) and only CF_UNICODETEXT.
$script:WayseamHostServicesAvailable = $false
try {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
public static class WayseamHostServices {
  [DllImport("user32.dll", SetLastError=true)] private static extern bool OpenClipboard(IntPtr owner);
  [DllImport("user32.dll", SetLastError=true)] private static extern bool CloseClipboard();
  [DllImport("user32.dll", SetLastError=true)] private static extern bool EmptyClipboard();
  [DllImport("user32.dll", SetLastError=true)] private static extern IntPtr GetClipboardData(uint format);
  [DllImport("user32.dll", SetLastError=true)] private static extern IntPtr SetClipboardData(uint format, IntPtr handle);
  [DllImport("user32.dll")] private static extern bool IsClipboardFormatAvailable(uint format);
  [DllImport("user32.dll")] public static extern uint GetClipboardSequenceNumber();
  [DllImport("kernel32.dll", SetLastError=true)] private static extern IntPtr GlobalAlloc(uint flags, UIntPtr bytes);
  [DllImport("kernel32.dll", SetLastError=true)] private static extern IntPtr GlobalLock(IntPtr handle);
  [DllImport("kernel32.dll", SetLastError=true)] private static extern bool GlobalUnlock(IntPtr handle);
  [DllImport("kernel32.dll", SetLastError=true)] private static extern IntPtr GlobalFree(IntPtr handle);
  [DllImport("kernel32.dll")] private static extern UIntPtr GlobalSize(IntPtr handle);
  [DllImport("kernel32.dll")] private static extern uint WTSGetActiveConsoleSessionId();
  [DllImport("kernel32.dll")] private static extern bool ProcessIdToSessionId(uint pid, out uint session);
  [DllImport("kernel32.dll")] private static extern uint GetCurrentProcessId();
  [DllImport("wtsapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)] private static extern bool WTSQuerySessionInformation(IntPtr server, uint session, int infoClass, out IntPtr buffer, out uint bytes);
  [DllImport("wtsapi32.dll")] private static extern void WTSFreeMemory(IntPtr memory);

  private const uint CF_UNICODETEXT = 13;
  public const int MaxTextChars = 1048576;

  public sealed class ClipboardText {
    public bool Ok;
    public bool Truncated;
    public string Text = "";
  }

  private static bool OpenWithRetry() {
    for (int attempt = 0; attempt < 20; attempt++) {
      if (OpenClipboard(IntPtr.Zero)) return true;
      System.Threading.Thread.Sleep(5);
    }
    return false;
  }

  public static ClipboardText GetText() {
    ClipboardText result = new ClipboardText();
    if (!IsClipboardFormatAvailable(CF_UNICODETEXT)) { result.Ok = true; return result; }
    if (!OpenWithRetry()) return result;
    try {
      IntPtr handle = GetClipboardData(CF_UNICODETEXT);
      if (handle == IntPtr.Zero) { result.Ok = true; return result; }
      IntPtr pointer = GlobalLock(handle);
      if (pointer == IntPtr.Zero) return result;
      try {
        long chars = (long)((ulong)GlobalSize(handle) / 2);
        if (chars <= 0) { result.Ok = true; return result; }
        int limit = (int)Math.Min(chars, (long)MaxTextChars + 1);
        string raw = Marshal.PtrToStringUni(pointer, limit);
        int end = raw.IndexOf('\0');
        if (end >= 0) raw = raw.Substring(0, end);
        if (raw.Length > MaxTextChars) { raw = raw.Substring(0, MaxTextChars); result.Truncated = true; }
        result.Text = raw;
        result.Ok = true;
        return result;
      } finally { GlobalUnlock(handle); }
    } finally { CloseClipboard(); }
  }

  public static bool SetText(string text) {
    if (text == null || text.Length > MaxTextChars) return false;
    if (!OpenWithRetry()) return false;
    try {
      if (!EmptyClipboard()) return false;
      IntPtr handle = GlobalAlloc(0x0042, new UIntPtr((uint)((text.Length + 1) * 2)));
      if (handle == IntPtr.Zero) return false;
      IntPtr pointer = GlobalLock(handle);
      if (pointer == IntPtr.Zero) { GlobalFree(handle); return false; }
      try {
        Marshal.Copy(text.ToCharArray(), 0, pointer, text.Length);
        Marshal.WriteInt16(pointer, text.Length * 2, 0);
      } finally { GlobalUnlock(handle); }
      if (SetClipboardData(CF_UNICODETEXT, handle) == IntPtr.Zero) { GlobalFree(handle); return false; }
      return true;
    } finally { CloseClipboard(); }
  }

  public static uint SessionId() {
    uint session;
    if (!ProcessIdToSessionId(GetCurrentProcessId(), out session)) return 0xFFFFFFFF;
    return session;
  }

  public static bool IsConsole(uint session) {
    return WTSGetActiveConsoleSessionId() == session;
  }

  // WTS_CONNECTSTATE_CLASS: 0 Active, 1 Connected, 2 ConnectQuery, 3 Shadow,
  // 4 Disconnected, 5 Idle, 6 Listen, 7 Reset, 8 Down, 9 Init; -1 unknown.
  public static int ConnectState(uint session) {
    IntPtr buffer; uint bytes;
    if (!WTSQuerySessionInformation(IntPtr.Zero, session, 8, out buffer, out bytes)) return -1;
    try { return bytes >= 4 ? Marshal.ReadInt32(buffer) : -1; } finally { WTSFreeMemory(buffer); }
  }

  public static string StationName(uint session) {
    IntPtr buffer; uint bytes;
    if (!WTSQuerySessionInformation(IntPtr.Zero, session, 6, out buffer, out bytes)) return "";
    try { return Marshal.PtrToStringUni(buffer) ?? ""; } finally { WTSFreeMemory(buffer); }
  }
}
"@
    $script:WayseamHostServicesAvailable = $true
} catch {
    try {
        Add-Content -Path $script:LogPath -Value (
            "$((Get-Date).ToUniversalTime().ToString('o')) WARN Wayseam host services unavailable"
        ) -ErrorAction SilentlyContinue
    } catch { }
}

function Get-WayseamSessionState([bool]$reconnected) {
    $sessionId = [WayseamHostServices]::SessionId()
    $stateCode = [WayseamHostServices]::ConnectState($sessionId)
    $state = switch ($stateCode) {
        0 { 'active' }
        1 { 'connected' }
        4 { 'disconnected' }
        default { 'unknown' }
    }
    return @{
        ok          = $true
        session_id  = [int64]$sessionId
        state       = $state
        state_code  = [int]$stateCode
        station     = [WayseamHostServices]::StationName($sessionId)
        console     = [WayseamHostServices]::IsConsole($sessionId)
        reconnected = $reconnected
    }
}

function Initialize-WayseamWgc {
    $script:WayseamWgcAvailable = $false
    try {
        $source = 'C:\OEM\wayseam_wgc.cs'
        if (-not (Test-Path -LiteralPath $source)) {
            $source = 'C:\OEM\agent\wayseam_wgc.cs'
        }
        if (-not (Test-Path -LiteralPath $source)) { return }
        $compiler = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
        if (-not (Test-Path -LiteralPath $compiler)) { return }
        $digest = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
        $library = Join-Path $script:RunsDir ("wayseam-wgc-$digest.dll")
        if (-not (Test-Path -LiteralPath $library)) {
            $stage = "$library.$PID.tmp"
            $compilerArgs = @(
                '/nologo', '/target:library', '/optimize+', "/out:$stage",
                '/r:C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Runtime.WindowsRuntime.dll',
                '/r:C:\Windows\Microsoft.NET\Framework64\v4.0.30319\System.Runtime.InteropServices.WindowsRuntime.dll',
                '/r:C:\Windows\Microsoft.NET\assembly\GAC_MSIL\System.Runtime\v4.0_4.0.0.0__b03f5f7f11d50a3a\System.Runtime.dll',
                '/r:C:\Windows\System32\WinMetadata\Windows.Foundation.winmd',
                '/r:C:\Windows\System32\WinMetadata\Windows.Graphics.winmd',
                $source
            )
            $compilerOutput = & $compiler $compilerArgs 2>&1
            if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $stage)) {
                Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue
                return
            }
            Move-Item -LiteralPath $stage -Destination $library -Force
        }
        Add-Type -Path $library
        if (-not [WayseamWgcCapture]::IsSupported()) { return }
        $script:WayseamWgcAvailable = $true
    } catch {
        try {
            Add-Content -Path $script:LogPath -Value (
                "$((Get-Date).ToUniversalTime().ToString('o')) WARN Wayseam WGC unavailable"
            ) -ErrorAction SilentlyContinue
        } catch { }
    }
}

Initialize-WayseamWgc

# --- Wayseam display stack ---------------------------------------------------
# Wayseam renders through a Parsec virtual display (parsec-vdd): high refresh,
# any resolution, no VirtIO scanout cost. The driver drops the display unless
# something pings it every <100 ms, so a tiny keepalive process owns that
# loop; the agent writes it, (re)spawns it, and then makes the display the
# 100%-DPI primary at the configured mode. Everything here is idempotent and
# re-runs at every agent start, so a guest reboot comes back Wayseam-ready.
$script:WayseamDisplay = @{
    width     = 3840
    height    = 2160
    hz        = 240
    script    = 'C:\OEM\vdd-keepalive.ps1'
    log       = 'C:\OEM\vdd-keeper.log'
    status    = 'not started'
    lastCheck = 0
}
$script:WayseamKeepaliveScript = @'
Add-Type -TypeDefinition @"
using System;
using System.Threading;
using System.Runtime.InteropServices;
public static class WayseamVdd {
  [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)] static extern int CM_Get_Device_Interface_List_SizeW(out uint len, ref Guid g, string d, uint f);
  [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)] static extern int CM_Get_Device_Interface_ListW(ref Guid g, string d, char[] b, uint l, uint f);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] static extern IntPtr CreateFileW(string n, uint a, uint s, IntPtr sa, uint di, uint fl, IntPtr t);
  [DllImport("kernel32.dll", SetLastError = true)] static extern bool DeviceIoControl(IntPtr d, uint c, byte[] i, uint il, out uint o, uint ol, IntPtr r, ref NativeOverlapped ov);
  [DllImport("kernel32.dll", SetLastError = true)] static extern IntPtr CreateEventW(IntPtr sa, bool manual, bool init, string name);
  [DllImport("kernel32.dll", SetLastError = true)] static extern bool GetOverlappedResultEx(IntPtr h, ref NativeOverlapped ov, out uint transferred, uint ms, bool alertable);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
  public static IntPtr Open() {
    Guid g = new Guid("00b41627-04c4-429e-a26e-0265cf50c8fa");
    uint len; if (CM_Get_Device_Interface_List_SizeW(out len, ref g, null, 0) != 0 || len <= 1) return IntPtr.Zero;
    char[] b = new char[len]; CM_Get_Device_Interface_ListW(ref g, null, b, len, 0);
    return CreateFileW(new string(b).Split('\0')[0], 0xC0000000, 3, IntPtr.Zero, 3, 0x80u | 0x20000000u | 0x40000000u | 0x80000000u, IntPtr.Zero);
  }
  public static long Ioctl(IntPtr h, uint code) {
    byte[] input = new byte[32];
    var ov = new NativeOverlapped();
    IntPtr ev = CreateEventW(IntPtr.Zero, true, false, null);
    ov.EventHandle = ev;
    uint outBuf;
    DeviceIoControl(h, code, input, 32, out outBuf, 4, IntPtr.Zero, ref ov);
    uint got; bool ok = GetOverlappedResultEx(h, ref ov, out got, 5000, false);
    CloseHandle(ev);
    return ok ? (long)outBuf : -1L;
  }
}
"@
$h = [WayseamVdd]::Open()
if ($h -eq [IntPtr]::Zero -or $h -eq [IntPtr]-1) { "open failed" | Out-File C:\OEM\vdd-keeper.log; exit 2 }
"version: " + [WayseamVdd]::Ioctl($h, 0x0022e010) | Out-File C:\OEM\vdd-keeper.log
"add: " + [WayseamVdd]::Ioctl($h, 0x0022e004) | Out-File C:\OEM\vdd-keeper.log -Append
while ($true) { [void][WayseamVdd]::Ioctl($h, 0x0022a00c); Start-Sleep -Milliseconds 80 }
'@

function Initialize-WayseamDisplayConfig {
    if ('WayseamDisplayConfig' -as [type]) { return }
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class WayseamDisplayConfig {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct DISPLAY_DEVICE { public int cb; [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string DeviceName; [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceString; public int StateFlags; [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceID; [MarshalAs(UnmanagedType.ByValTStr, SizeConst=128)] public string DeviceKey; }
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)] public struct DEVMODE { [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmDeviceName; public ushort dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra; public uint dmFields; public int x, y; public uint dmOrientation, dmFixedOutput; public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate; [MarshalAs(UnmanagedType.ByValTStr, SizeConst=32)] public string dmFormName; public ushort dmLogPixels; public uint dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency; public uint dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2, dmPanningWidth, dmPanningHeight; }
  [StructLayout(LayoutKind.Sequential)] public struct LUID { public uint LowPart; public int HighPart; }
  [StructLayout(LayoutKind.Sequential)] public struct HDR { public int type; public int size; public LUID adapterId; public uint id; }
  [StructLayout(LayoutKind.Sequential)] public struct GET_DPI { public HDR header; public int minScaleRel; public int curScaleRel; public int maxScaleRel; }
  [StructLayout(LayoutKind.Sequential)] public struct SET_DPI { public HDR header; public int scaleRel; }
  [StructLayout(LayoutKind.Sequential)] public struct PATH_SOURCE { public LUID adapterId; public uint id; public uint modeInfoIdx; public uint statusFlags; }
  [StructLayout(LayoutKind.Sequential)] public struct PATH_TARGET { public LUID adapterId; public uint id; public uint modeInfoIdx; public uint outputTechnology; public uint rotation; public uint scaling; public uint refreshRateNum; public uint refreshRateDen; public uint scanLineOrdering; public int targetAvailable; public uint statusFlags; }
  [StructLayout(LayoutKind.Sequential)] public struct PATH_INFO { public PATH_SOURCE sourceInfo; public PATH_TARGET targetInfo; public uint flags; }
  [StructLayout(LayoutKind.Sequential, Size = 192)] public struct MODE_INFO { public int infoType; public uint id; public LUID adapterId; }
  [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)] static extern int CM_Get_Device_Interface_List_SizeW(out uint len, ref Guid g, string d, uint f);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern bool EnumDisplayDevicesW(string device, uint index, ref DISPLAY_DEVICE output, uint flags);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern bool EnumDisplaySettingsW(string device, int mode, ref DEVMODE devmode);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int ChangeDisplaySettingsExW(string device, ref DEVMODE devmode, IntPtr hwnd, uint flags, IntPtr lparam);
  [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int ChangeDisplaySettingsExW(string device, IntPtr devmode, IntPtr hwnd, uint flags, IntPtr lparam);
  [DllImport("user32.dll")] static extern int GetDisplayConfigBufferSizes(uint flags, out uint numPaths, out uint numModes);
  [DllImport("user32.dll")] static extern int QueryDisplayConfig(uint flags, ref uint numPaths, [Out] PATH_INFO[] paths, ref uint numModes, [Out] MODE_INFO[] modes, IntPtr topology);
  [DllImport("user32.dll")] static extern int DisplayConfigGetDeviceInfo(ref GET_DPI packet);
  [DllImport("user32.dll")] static extern int DisplayConfigSetDeviceInfo(ref SET_DPI packet);
  [DllImport("user32.dll")] public static extern uint GetDpiForSystem();
  const uint DM_POSITION = 0x20, DM_PELSWIDTH = 0x80000, DM_PELSHEIGHT = 0x100000, DM_DISPLAYFREQUENCY = 0x400000;
  const uint CDS_UPDATEREGISTRY = 1, CDS_SET_PRIMARY = 0x10, CDS_NORESET = 0x10000000;
  const int ATTACHED = 1, PRIMARY = 4;
  static DEVMODE Mode() { var m = new DEVMODE(); m.dmSize = (ushort)Marshal.SizeOf(m); return m; }
  static DISPLAY_DEVICE Dev() { var d = new DISPLAY_DEVICE(); d.cb = Marshal.SizeOf(d); return d; }
  public static bool HasParsecInterface() {
    Guid g = new Guid("00b41627-04c4-429e-a26e-0265cf50c8fa"); uint len;
    return CM_Get_Device_Interface_List_SizeW(out len, ref g, null, 0) == 0 && len > 1;
  }
  public static string ParsecDevice() {
    var dd = Dev();
    for (uint i = 0; EnumDisplayDevicesW(null, i, ref dd, 0); i++) {
      if ((dd.StateFlags & ATTACHED) != 0 && dd.DeviceString.IndexOf("Parsec", StringComparison.OrdinalIgnoreCase) >= 0) return dd.DeviceName;
      dd = Dev();
    }
    return null;
  }
  public static string Describe() {
    var sb = new StringBuilder(); var dd = Dev();
    for (uint i = 0; EnumDisplayDevicesW(null, i, ref dd, 0); i++) {
      if ((dd.StateFlags & ATTACHED) != 0) {
        var dm = Mode(); EnumDisplaySettingsW(dd.DeviceName, -1, ref dm);
        sb.Append(dd.DeviceName + "|" + dd.DeviceString + "|" + dm.dmPelsWidth + "x" + dm.dmPelsHeight + "@" + dm.dmDisplayFrequency + "|" + dm.x + "," + dm.y + "|" + (((dd.StateFlags & PRIMARY) != 0) ? "primary" : "secondary") + "\n");
      }
      dd = Dev();
    }
    return sb.ToString();
  }
  public static string EnsurePrimary(uint width, uint height, uint hz) {
    string parsec = ParsecDevice();
    if (parsec == null) return "no parsec display attached";
    var dd = Dev(); bool primary = false;
    for (uint i = 0; EnumDisplayDevicesW(null, i, ref dd, 0); i++) { if (dd.DeviceName == parsec) { primary = (dd.StateFlags & PRIMARY) != 0; break; } dd = Dev(); }
    var cur = Mode(); EnumDisplaySettingsW(parsec, -1, ref cur);
    bool modeOk = cur.dmPelsWidth == width && cur.dmPelsHeight == height && (hz == 0 || cur.dmDisplayFrequency == hz);
    if (primary && modeOk && cur.x == 0 && cur.y == 0) return "ok";
    var want = Mode(); EnumDisplaySettingsW(parsec, -1, ref want);
    want.x = 0; want.y = 0; want.dmPelsWidth = width; want.dmPelsHeight = height;
    want.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT;
    if (hz != 0) { want.dmDisplayFrequency = hz; want.dmFields |= DM_DISPLAYFREQUENCY; }
    int rc = ChangeDisplaySettingsExW(parsec, ref want, IntPtr.Zero, CDS_UPDATEREGISTRY | CDS_NORESET | CDS_SET_PRIMARY, IntPtr.Zero);
    if (rc == -2 && hz != 0) { // DISP_CHANGE_BADMODE: retry without pinning the refresh rate
      want.dmFields = DM_POSITION | DM_PELSWIDTH | DM_PELSHEIGHT;
      rc = ChangeDisplaySettingsExW(parsec, ref want, IntPtr.Zero, CDS_UPDATEREGISTRY | CDS_NORESET | CDS_SET_PRIMARY, IntPtr.Zero);
    }
    if (rc != 0) return "set primary failed rc=" + rc;
    int shifted = 0; dd = Dev();
    for (uint i = 0; EnumDisplayDevicesW(null, i, ref dd, 0); i++) {
      if ((dd.StateFlags & ATTACHED) != 0 && dd.DeviceName != parsec) {
        var other = Mode(); EnumDisplaySettingsW(dd.DeviceName, -1, ref other);
        other.x = (int)width; other.y = 0; other.dmFields = DM_POSITION;
        ChangeDisplaySettingsExW(dd.DeviceName, ref other, IntPtr.Zero, CDS_UPDATEREGISTRY | CDS_NORESET, IntPtr.Zero);
        shifted++;
      }
      dd = Dev();
    }
    int apply = ChangeDisplaySettingsExW(null, IntPtr.Zero, IntPtr.Zero, 0, IntPtr.Zero);
    return "set primary " + width + "x" + height + "@" + hz + " rc=" + rc + " shifted=" + shifted + " apply=" + apply;
  }
  public static string ForceMinDpiAll() {
    uint np, nm; GetDisplayConfigBufferSizes(2, out np, out nm);
    var paths = new PATH_INFO[np]; var modes = new MODE_INFO[nm];
    if (QueryDisplayConfig(2, ref np, paths, ref nm, modes, IntPtr.Zero) != 0) return "dpi query failed";
    string log = "";
    for (int i = 0; i < np; i++) {
      var get = new GET_DPI();
      get.header.type = -3; get.header.size = Marshal.SizeOf(typeof(GET_DPI));
      get.header.adapterId = paths[i].sourceInfo.adapterId; get.header.id = paths[i].sourceInfo.id;
      int rc = DisplayConfigGetDeviceInfo(ref get);
      if (rc == 0 && get.curScaleRel != get.minScaleRel) {
        var set = new SET_DPI();
        set.header.type = -4; set.header.size = Marshal.SizeOf(typeof(SET_DPI));
        set.header.adapterId = paths[i].sourceInfo.adapterId; set.header.id = paths[i].sourceInfo.id;
        set.scaleRel = get.minScaleRel;
        log += "path" + i + " dpi->100% rc=" + DisplayConfigSetDeviceInfo(ref set) + " ";
      }
    }
    return log == "" ? "dpi ok" : log.Trim();
  }
}
"@
}

function Test-WayseamKeepaliveRunning {
    try {
        $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -like '*vdd-keepalive.ps1*' }
        return @($procs).Count -gt 0
    } catch { return $false }
}

function Start-WayseamKeepalive {
    $path = $script:WayseamDisplay.script
    $current = ''
    if (Test-Path -LiteralPath $path) {
        try { $current = [IO.File]::ReadAllText($path) } catch { $current = '' }
    }
    if ($current -ne $script:WayseamKeepaliveScript) {
        [IO.File]::WriteAllText($path, $script:WayseamKeepaliveScript, (New-Object Text.UTF8Encoding $false))
    }
    Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',$path
}

function Invoke-WayseamDisplayEnsure([bool]$startup) {
    $d = $script:WayseamDisplay
    try {
        Initialize-WayseamDisplayConfig
        if (-not [WayseamDisplayConfig]::HasParsecInterface()) {
            $d.status = 'parsec-vdd not installed'
            return
        }
        $spawned = $false
        if (-not (Test-WayseamKeepaliveRunning)) {
            Start-WayseamKeepalive
            $spawned = $true
        }
        if ($spawned -or $startup) {
            $deadline = [Environment]::TickCount + 12000
            while (-not [WayseamDisplayConfig]::ParsecDevice() -and [Environment]::TickCount -lt $deadline) {
                Start-Sleep -Milliseconds 250
            }
        }
        $primary = [WayseamDisplayConfig]::EnsurePrimary([uint32]$d.width, [uint32]$d.height, [uint32]$d.hz)
        $dpi = [WayseamDisplayConfig]::ForceMinDpiAll()
        $d.status = "keepalive=$(if ($spawned) { 'spawned' } else { 'running' }); $primary; $dpi"
        if ($spawned -or $startup -or $primary -ne 'ok' -or $dpi -ne 'dpi ok') {
            Write-Log 'INIT' '/wayseam/display' 'n/a' 0 0 $d.status
        }
    } catch {
        $d.status = "error: $($_.Exception.Message)"
        Write-Log 'INIT' '/wayseam/display' 'n/a' 0 0 $d.status
    }
}

function Get-WayseamDisplayStatus {
    Initialize-WayseamDisplayConfig
    $displays = @()
    foreach ($line in ([WayseamDisplayConfig]::Describe() -split "`n")) {
        if (-not $line) { continue }
        $parts = $line -split '\|'
        $displays += @{ name = $parts[0]; description = $parts[1]; mode = $parts[2]; position = $parts[3]; role = $parts[4] }
    }
    return @{
        parsec_installed = [WayseamDisplayConfig]::HasParsecInterface()
        keepalive        = Test-WayseamKeepaliveRunning
        target           = @{ width = $script:WayseamDisplay.width; height = $script:WayseamDisplay.height; hz = $script:WayseamDisplay.hz }
        system_dpi       = [WayseamDisplayConfig]::GetDpiForSystem()
        displays         = $displays
        status           = $script:WayseamDisplay.status
    }
}

function Invoke-WayseamSessionPolicy {
    # Desktop Mode is the SAME session seen through RDP: the user's RDP logon
    # must take over console session 1 (where the Wayseam-presented apps run)
    # instead of opening a second session. rdprrap's multi-session setup
    # flips this to 0; enforce 1 at every agent start.
    try {
        $ts = 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server'
        $current = (Get-ItemProperty $ts -Name fSingleSessionPerUser -ErrorAction SilentlyContinue).fSingleSessionPerUser
        if ($current -ne 1) {
            Set-ItemProperty $ts -Name fSingleSessionPerUser -Value 1 -Type DWord
            Write-Log 'INIT' '/wayseam/session-policy' 'n/a' 0 0 "fSingleSessionPerUser $current -> 1"
        }
    } catch {
        Write-Log 'INIT' '/wayseam/session-policy' 'n/a' 0 0 "error: $($_.Exception.Message)"
    }
}

Invoke-WayseamSessionPolicy
Invoke-WayseamDisplayEnsure $true

function Get-WayseamWindowFrame(
    [IntPtr]$hwnd,
    [bool]$repairPopupAlpha = $false,
    [string]$encoding = 'png',
    [string]$stream = '',
    [int]$baseSequence = 0
) {
    if (-not $script:WayseamCaptureAvailable) { return @{ error = 'capture_unavailable' } }
    if (-not [WayseamNativeCapture]::IsWindow($hwnd)) { return @{ error = 'window_not_found' } }
    if (-not [WayseamNativeCapture]::IsWindowVisible($hwnd)) { return @{ error = 'window_not_visible' } }

    $rect = [WayseamNativeCapture+RECT]::new()
    if (-not [WayseamNativeCapture]::GetWindowRect($hwnd, [ref]$rect)) {
        return @{ error = 'window_rect_failed' }
    }
    $width = $rect.Right - $rect.Left
    $height = $rect.Bottom - $rect.Top
    if ($width -le 0 -or $height -le 0 -or $width -gt 8192 -or $height -gt 8192 -or
        ([int64]$width * [int64]$height) -gt 33554432) {
        return @{ error = 'window_size_rejected' }
    }

    if ($encoding -eq 'delta') {
        try {
            $streamKey = ('{0:x}:{1}' -f $hwnd.ToInt64(), $stream)
            $capture = $null
            $captureWidth = $width
            $captureHeight = $height
            $alphaRepaired = $false
            if ($script:WayseamWgcAvailable) {
                $capture = [WayseamWgcCapture]::CaptureDeltaFrame(
                    $streamKey, $hwnd, $baseSequence, 20
                )
                if ($capture.Ok) {
                    $captureWidth = $capture.Width
                    $captureHeight = $capture.Height
                }
            }
            if ($null -eq $capture -or -not $capture.Ok) {
                $capture = [WayseamNativeCapture]::CaptureDeltaFrame(
                    $streamKey, $hwnd, $width, $height, $baseSequence, $repairPopupAlpha
                )
                if ($capture.Ok) { $alphaRepaired = $capture.AlphaRepaired }
            }
            if (-not $capture.Ok) { return @{ error = 'capture_failed' } }
            if ($capture.Bytes.Length -gt 33554432) {
                return @{ error = 'frame_too_large' }
            }
            return @{
                bytes = $capture.Bytes; width = $captureWidth; height = $captureHeight
                alpha_repaired = $alphaRepaired
                content_type = 'application/x-wayseam-bgra-delta'
                encoding = $encoding
            }
        } catch {
            return @{ error = 'capture_failed' }
        }
    }

    $bitmap = $null
    $graphics = $null
    $memoryStream = $null
    try {
        $bitmap = [Drawing.Bitmap]::new(
            $width, $height, [Drawing.Imaging.PixelFormat]::Format32bppArgb
        )
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        $dc = $graphics.GetHdc()
        try { $ok = [WayseamNativeCapture]::PrintWindow($hwnd, $dc, 2) }
        finally { $graphics.ReleaseHdc($dc) }
        if (-not $ok) { return @{ error = 'capture_failed' } }
        $alphaRepaired = $false
        if ($repairPopupAlpha) {
            $alphaRepaired = [WayseamNativeCapture]::ClearLostPopupAlpha($bitmap)
        }

        $contentType = 'image/png'
        if ($encoding -eq 'rle') {
            $bytes = [WayseamNativeCapture]::EncodeRleFrame($bitmap)
            $contentType = 'application/x-wayseam-bgra-rle'
        } else {
            $memoryStream = [IO.MemoryStream]::new()
            $bitmap.Save($memoryStream, [Drawing.Imaging.ImageFormat]::Png)
            $bytes = $memoryStream.ToArray()
        }
        if ($bytes.Length -gt 33554432) { return @{ error = 'frame_too_large' } }
        return @{
            bytes = $bytes; width = $width; height = $height
            alpha_repaired = $alphaRepaired; content_type = $contentType
            encoding = $encoding
        }
    } catch {
        return @{ error = 'capture_failed' }
    } finally {
        if ($memoryStream) { $memoryStream.Dispose() }
        if ($graphics) { $graphics.Dispose() }
        if ($bitmap) { $bitmap.Dispose() }
    }
}

# Run a base64-encoded PowerShell snippet via Start-Process, with a
# server-side timeout enforced by WaitForExit. Returns a hashtable with
# rc / stdout / stderr / hash, or { error = '...' } on spawn failure.
# We deliberately use -File against a temp .ps1 (not -EncodedCommand)
# because Start-Process does not support -EncodedCommand cleanly with
# stdout/stderr redirection; the temp file is cleaned up after the run.
function Invoke-ExecScript([string]$scriptB64, [int]$timeoutSec) {
    $tempBase   = Join-Path $script:RunsDir ([Guid]::NewGuid().ToString('N'))
    $tempFile   = "$tempBase.ps1"
    $launchFile = "$tempBase.launch.ps1"
    try {
        try {
            $bytes = [Convert]::FromBase64String($scriptB64)
        } catch {
            return @{ error = 'bad_base64'; detail = $_.Exception.Message }
        }
        $hash = Get-BytesHash $bytes
        try {
            [IO.File]::WriteAllBytes($tempFile, $bytes)
        } catch {
            return @{ error = 'temp_write_failed'; detail = $_.Exception.Message; hash = $hash }
        }
        # Run the caller's script through a launcher that sets UTF-8 first.
        #
        # Without it the child encodes stdout with the console's OEM code page,
        # and any character that page lacks goes through Windows' best-fit
        # mapping on the way out -- which turned "Microsoft(R) Drive Optimizer"
        # into "Microsoftr Drive Optimizer" in discovery output. The characters
        # are gone by the time the bytes reach the pipe, so nothing downstream
        # can recover them.
        #
        # It has to be a separate FILE, not a preamble prepended to the
        # caller's script: PowerShell requires param() and [CmdletBinding()] to
        # be the first statement in their file, and discover_apps.ps1 opens
        # with both. Prepending anything executable makes it unparseable
        # ("Unexpected attribute 'CmdletBinding'").
        #
        # `exit $rc` keeps -File's exit-code semantics: & leaves the called
        # script's exit code in $LASTEXITCODE, which is $null when the script
        # returns without ever running a native command.
        $launcher = @"
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding `$false } catch { }
`$OutputEncoding = New-Object Text.UTF8Encoding `$false
& '$tempFile'
`$rc = `$LASTEXITCODE
if (`$null -eq `$rc) { `$rc = 0 }
exit `$rc
"@
        try {
            [IO.File]::WriteAllText($launchFile, $launcher, (New-Object Text.UTF8Encoding $false))
        } catch {
            return @{ error = 'temp_write_failed'; detail = $_.Exception.Message; hash = $hash }
        }
        # Spawn the child via [Diagnostics.Process] + ProcessStartInfo with
        # CreateNoWindow=$true. Start-Process -NoNewWindow re-opens a console
        # for the child when the parent (agent.ps1) was launched with
        # -WindowStyle Hidden -- kernalix7 saw flashing PS windows on every
        # /exec call on 2026-04-30. CreateNoWindow + UseShellExecute=$false
        # is the canonical "run windowless and capture stdio" combination.
        $proc = $null
        try {
            $psi = New-Object System.Diagnostics.ProcessStartInfo
            $psi.FileName               = 'powershell.exe'
            $psi.Arguments              = '-NoProfile -ExecutionPolicy Bypass -File "' + $launchFile + '"'
            $psi.UseShellExecute        = $false
            $psi.CreateNoWindow         = $true
            $psi.RedirectStandardOutput = $true
            $psi.RedirectStandardError  = $true
            # Decode the child's bytes as UTF-8, matching what the launcher
            # tells it to emit. Left unset, .NET decodes with the console's OEM
            # code page and undoes the launcher's work.
            $psi.StandardOutputEncoding = New-Object Text.UTF8Encoding $false
            $psi.StandardErrorEncoding  = New-Object Text.UTF8Encoding $false
            $proc = New-Object System.Diagnostics.Process
            $proc.StartInfo = $psi
            [void]$proc.Start()
            # Drain stdio asynchronously so a child writing >64KB of output
            # doesn't deadlock against the OS pipe buffer while we're still
            # blocked in WaitForExit (Microsoft's documented gotcha).
            $stdoutTask = $proc.StandardOutput.ReadToEndAsync()
            $stderrTask = $proc.StandardError.ReadToEndAsync()
        } catch {
            return @{ error = 'spawn_failed'; detail = $_.Exception.Message; hash = $hash }
        }
        $rc = 0
        $timedOut = $false
        if (-not $proc.WaitForExit([int]([Math]::Min($timeoutSec, $script:ExecMaxTimeoutSec)) * 1000)) {
            $timedOut = $true
            try { $proc.Kill() } catch { }
            try { [void]$proc.WaitForExit(2000) } catch { }
            $rc = 124
        } else {
            # Start-Process -PassThru can leave ExitCode as $null even after
            # WaitForExit() returns true (Windows quirk: handle isn't kept open
            # for fast-exiting children unless the StartInfo enables it). The
            # process did terminate cleanly, so treat null as 0 -- and never
            # emit a non-int rc, since the host AgentClient parses it as int.
            $exitCode = $proc.ExitCode
            if ($null -eq $exitCode) { $rc = 0 } else { $rc = [int]$exitCode }
        }
        $stdoutText = ''
        $stderrText = ''
        # Pull from the async ReadToEndAsync tasks queued at spawn time.
        # On timeout the streams may already be closed by Kill(); guard each.
        try { if ($stdoutTask) { $stdoutText = $stdoutTask.GetAwaiter().GetResult() } } catch { }
        try { if ($stderrTask) { $stderrText = $stderrTask.GetAwaiter().GetResult() } } catch { }
        if ($timedOut -and -not $stderrText) { $stderrText = 'timeout' }
        return @{
            rc       = $rc
            stdout   = $stdoutText
            stderr   = $stderrText
            hash     = $hash
            timedOut = $timedOut
        }
    } finally {
        foreach ($f in @($tempFile, $launchFile)) {
            if ($f -and (Test-Path $f)) {
                try { Remove-Item -LiteralPath $f -Force -ErrorAction SilentlyContinue } catch { }
            }
        }
    }
}

# Wait for the token before binding. /health is no-auth, but every
# other endpoint compares against $script:Token, so binding before the
# token lands would race the first auth check on a real /exec call.
$script:Token = Wait-Token

# Bind with a bounded retry loop. install.bat (#269 fix) reserves the
# urlacl for the World SID (S-1-1-0 / sddl WD) just before spawning the
# agent, but the agent's first spawn can race that reservation landing
# in HTTP.sys, and an autologon-retry session can re-spawn before the
# OS finished applying the ACL. A few short retries absorb that race
# without masking a genuine persistent conflict (which still ends in a
# FATAL + the full urlacl state dumped to agent.log so the real owner
# of the conflicting reservation is visible).
$listener = [System.Net.HttpListener]::new()
$listener.Prefixes.Add($script:Prefix)
$bindAttempts = 5
$bound = $false
for ($i = 1; $i -le $bindAttempts; $i++) {
    try {
        $listener.Start()
        $bound = $true
        break
    } catch {
        $msg = $_.Exception.Message
        try {
            Add-Content -Path $script:LogPath -Value (
                "$((Get-Date).ToUniversalTime().ToString('o')) WARN " +
                "HttpListener.Start() attempt $i/$bindAttempts failed: $msg"
            ) -ErrorAction SilentlyContinue
        } catch { }
        if ($i -lt $bindAttempts) {
            Start-Sleep -Seconds 3
            # Re-create the listener -- a failed Start() can leave the
            # instance in a state that rejects a second Start().
            $listener = [System.Net.HttpListener]::new()
            $listener.Prefixes.Add($script:Prefix)
        }
    }
}
if (-not $bound) {
    # Persistent failure. Dump the actual urlacl reservation state so the
    # next debugging round sees WHICH SID owns the conflicting prefix --
    # the agent runs as a non-admin User and cannot re-register the ACL
    # itself (needs admin), so the fix has to land in install.bat.
    $aclState = ''
    try {
        $aclState = (& netsh http show urlacl url=$($script:Prefix) 2>&1 | Out-String).Trim()
    } catch { }
    try {
        Add-Content -Path $script:LogPath -Value (
            "$((Get-Date).ToUniversalTime().ToString('o')) FATAL " +
            "HttpListener.Start() failed after $bindAttempts attempts on $($script:Prefix)." +
            "  hint: install.bat should have reserved this via " +
            "'netsh http add urlacl url=$($script:Prefix) sddl=D:(A;;GX;;;WD)' as admin." +
            "  current urlacl state: $aclState"
        ) -ErrorAction SilentlyContinue
    } catch { }
    throw "HttpListener.Start() failed after $bindAttempts attempts on $($script:Prefix)"
}

# --- exec worker pool (#751) -----------------------------------------
# The accept loop below is single-threaded; before 0.2.3 a long /exec
# (WaitForExit up to 300s) blocked it entirely, so /health went
# unanswered, the host's 5s HEALTH_TIMEOUT expired, and dispatch
# declared "agent unavailable" on an agent that was merely busy --
# reported as the agent "repeatedly dying" in #751. Run each /exec in a
# background runspace instead: the worker owns the HttpListenerResponse
# (responding from another thread is supported) and the main loop keeps
# serving /health. Pool max 4 bounds concurrent guest PowerShell spawns;
# excess execs queue inside the pool, which still never blocks /health.
$iss = [initialsessionstate]::CreateDefault()
foreach ($fnName in @(
    'Get-BytesHash', 'Write-Log', 'Send-Json', 'Send-Bytes',
    'Invoke-ExecScript', 'Get-WayseamWindowFrame'
)) {
    $fnBody = (Get-Content -Path ("function:" + $fnName)).ToString()
    $iss.Commands.Add((New-Object System.Management.Automation.Runspaces.SessionStateFunctionEntry($fnName, $fnBody)))
}
# The functions above read these as $script:<name>; at a runspace's top
# level script scope IS global scope, so plain global entries satisfy them.
foreach ($varName in @(
    'LogPath', 'RunsDir', 'ExecMaxTimeoutSec',
    'WayseamCaptureAvailable', 'WayseamWgcAvailable'
)) {
    $iss.Variables.Add((New-Object System.Management.Automation.Runspaces.SessionStateVariableEntry(
        $varName, (Get-Variable -Name $varName -Scope Script -ValueOnly), $null)))
}
$script:ExecPool = [runspacefactory]::CreateRunspacePool(1, 4, $iss, $Host)
$script:ExecPool.Open()
$script:ExecWorkers = [System.Collections.ArrayList]::new()

# Worker body: run the script, send the response, write the log line.
# Owns the response object end-to-end so the main loop never touches it
# again after handoff.
$script:ExecWorkerBody = {
    param($resp, $scriptB64, $timeoutSec)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $code = 500
    $extraLog = ''
    try {
        $result = Invoke-ExecScript -scriptB64 $scriptB64 -timeoutSec $timeoutSec
        if ($result.ContainsKey('error')) {
            Send-Json $resp 500 @{ error = 'exec_failed'; detail = $result.error }
            $code = 500
            if ($result.ContainsKey('hash')) { $extraLog = "hash=$($result.hash)" }
        } else {
            Send-Json $resp 200 @{
                rc     = $result.rc
                stdout = $result.stdout
                stderr = $result.stderr
            }
            $code = 200
            $extraLog = "hash=$($result.hash) rc=$($result.rc) timeout=$($result.timedOut)"
        }
    } catch {
        try { Send-Json $resp 500 @{ error = 'internal_error' } } catch { }
        $code = 500
    }
    $sw.Stop()
    Write-Log 'POST' '/exec' 'ok' $code ([int]$sw.ElapsedMilliseconds) $extraLog
}

$script:FrameWorkerBody = {
    param(
        $resp,
        [IntPtr]$hwnd,
        [bool]$repairPopupAlpha,
        [string]$encoding,
        [string]$stream,
        [int]$baseSequence
    )
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $code = 500
    $extraLog = ''
    try {
        $frame = Get-WayseamWindowFrame $hwnd $repairPopupAlpha $encoding $stream $baseSequence
        if ($frame.ContainsKey('error')) {
            Send-Json $resp 422 @{ error = $frame.error }
            $code = 422
        } else {
            Send-Bytes $resp 200 $frame.content_type $frame.bytes
            $code = 200
            $extraLog = (
                "width=$($frame.width) height=$($frame.height) " +
                "bytes=$($frame.bytes.Length) encoding=$($frame.encoding) " +
                "alpha_repaired=$($frame.alpha_repaired)"
            )
        }
    } catch {
        try { Send-Json $resp 500 @{ error = 'internal_error' } } catch { }
    }
    $sw.Stop()
    # Frame capture runs up to 60 times per second. Logging every successful
    # delta turned a short proof run into an 80 MiB agent.log and added disk
    # I/O to the latency-sensitive path. Keep failures and genuinely slow or
    # unusually large frames; routine successes are observable from the host.
    if ($code -ne 200 -or $sw.ElapsedMilliseconds -ge 50 -or
        ($null -ne $frame -and $frame.bytes.Length -ge 1048576)) {
        Write-Log 'GET' '/wayseam/frame' 'ok' $code ([int]$sw.ElapsedMilliseconds) $extraLog
    }
}

try {
    while ($listener.IsListening) {
        $ctx = $null
        try { $ctx = $listener.GetContext() } catch { continue }
        if (([Environment]::TickCount - $script:WayseamDisplay.lastCheck) -gt 30000) {
            $script:WayseamDisplay.lastCheck = [Environment]::TickCount
            Invoke-WayseamDisplayEnsure $false
        }
        # Reap finished exec workers (EndInvoke + Dispose). Runs on every
        # request; the host polls /health continuously, so completed
        # workers never linger long.
        if ($script:ExecWorkers.Count -gt 0) {
            $done = @($script:ExecWorkers | Where-Object { $_.handle.IsCompleted })
            foreach ($w in $done) {
                try { [void]$w.ps.EndInvoke($w.handle) } catch { }
                try { $w.ps.Dispose() } catch { }
                $script:ExecWorkers.Remove($w)
            }
        }
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $req = $ctx.Request
        $resp = $ctx.Response
        $method = $req.HttpMethod
        $path = $req.Url.AbsolutePath
        $code = 500
        $authTag = 'none'
        $extraLog = ''
        try {
            if ($method -eq 'GET' -and $path -eq '/health') {
                $payload = @{
                    version    = $script:AgentVersion
                    ok         = $true
                    started_at = $script:StartedAt
                }
                Send-Json $resp 200 $payload
                $code = 200
            } elseif (-not (Test-Auth $req)) {
                Send-Json $resp 401 @{ error = 'unauthorized' }
                $code = 401
                $authTag = 'fail'
            } elseif ($method -eq 'GET' -and $path -eq '/wayseam/frame') {
                $authTag = 'ok'
                $rawHwnd = [string]$req.QueryString['hwnd']
                $hwndValue = [int64]0
                $validHwnd = $false
                if ($rawHwnd -and $rawHwnd.Length -le 18) {
                    if ($rawHwnd.StartsWith('0x', [StringComparison]::OrdinalIgnoreCase)) {
                        $validHwnd = [int64]::TryParse(
                            $rawHwnd.Substring(2),
                            [Globalization.NumberStyles]::AllowHexSpecifier,
                            [Globalization.CultureInfo]::InvariantCulture,
                            [ref]$hwndValue
                        )
                    } else {
                        $validHwnd = [int64]::TryParse($rawHwnd, [ref]$hwndValue)
                    }
                }
                if (-not $validHwnd -or $hwndValue -le 0) {
                    Send-Json $resp 400 @{ error = 'invalid_hwnd' }
                    $code = 400
                } elseif (-not [WayseamNativeCapture]::IsWindow([IntPtr]$hwndValue)) {
                    Send-Json $resp 410 @{ error = 'window_gone' }
                    $code = 410
                } else {
                    $repairPopupAlpha = (
                        [string]$req.QueryString['alpha'] -eq 'border'
                    )
                    $encoding = [string]$req.QueryString['encoding']
                    if (-not $encoding) { $encoding = 'png' }
                    $stream = [string]$req.QueryString['stream']
                    $baseSequence = 0
                    $validBase = [int]::TryParse(
                        [string]$req.QueryString['base'], [ref]$baseSequence
                    )
                    $validDelta = $encoding -ne 'delta' -or (
                        $stream -match '^[0-9a-f]{32}$' -and
                        $validBase -and $baseSequence -ge 0
                    )
                    if ($encoding -notin @('png', 'rle', 'delta') -or -not $validDelta) {
                        Send-Json $resp 400 @{ error = 'unsupported_encoding' }
                        $code = 400
                    } else {
                        $ps = [powershell]::Create()
                        $ps.RunspacePool = $script:ExecPool
                        [void]$ps.AddScript($script:FrameWorkerBody).AddArgument($resp).AddArgument(
                            [IntPtr]$hwndValue
                        ).AddArgument($repairPopupAlpha).AddArgument($encoding).AddArgument(
                            $stream
                        ).AddArgument($baseSequence)
                        [void]$script:ExecWorkers.Add(@{ ps = $ps; handle = $ps.BeginInvoke() })
                        $code = -1
                    }
                }
            } elseif ($method -eq 'GET' -and $path -eq '/wayseam/top-level') {
                $authTag = 'ok'
                $windows = @([WayseamNativeCapture]::TopLevelWindows())
                Send-Json $resp 200 @{ windows = $windows }
                $code = 200
                $extraLog = "windows=$($windows.Count)"
            } elseif ($method -eq 'GET' -and $path -eq '/wayseam/windows') {
                $authTag = 'ok'
                $rawRoot = [string]$req.QueryString['root']
                $rootValue = [int64]0
                $validRoot = $false
                if ($rawRoot -and $rawRoot.Length -le 18) {
                    if ($rawRoot.StartsWith('0x', [StringComparison]::OrdinalIgnoreCase)) {
                        $validRoot = [int64]::TryParse(
                            $rawRoot.Substring(2),
                            [Globalization.NumberStyles]::AllowHexSpecifier,
                            [Globalization.CultureInfo]::InvariantCulture,
                            [ref]$rootValue
                        )
                    } else {
                        $validRoot = [int64]::TryParse($rawRoot, [ref]$rootValue)
                    }
                }
                $rootHwnd = [IntPtr]$rootValue
                $rootRect = [WayseamNativeCapture+RECT]::new()
                if (-not $validRoot -or $rootValue -le 0 -or
                    -not [WayseamNativeCapture]::IsWindow($rootHwnd) -or
                    -not [WayseamNativeCapture]::GetVisibleRect($rootHwnd, [ref]$rootRect)) {
                    Send-Json $resp 400 @{ error = 'invalid_root' }
                    $code = 400
                } else {
                    $windows = @([WayseamNativeCapture]::OwnedWindows($rootHwnd))
                    Send-Json $resp 200 @{
                        root = ('0x{0:x}' -f $rootValue)
                        left = $rootRect.Left
                        top = $rootRect.Top
                        width = $rootRect.Right - $rootRect.Left
                        height = $rootRect.Bottom - $rootRect.Top
                        windows = $windows
                    }
                    $code = 200
                    $extraLog = "owned=$($windows.Count)"
                }
            } elseif ($method -eq 'GET' -and $path -eq '/wayseam/cursor') {
                $authTag = 'ok'
                $rawRoot = [string]$req.QueryString['root']
                $rootValue = [int64]0
                $validRoot = $false
                if ($rawRoot -and $rawRoot.Length -le 18) {
                    if ($rawRoot.StartsWith('0x', [StringComparison]::OrdinalIgnoreCase)) {
                        $validRoot = [int64]::TryParse(
                            $rawRoot.Substring(2),
                            [Globalization.NumberStyles]::AllowHexSpecifier,
                            [Globalization.CultureInfo]::InvariantCulture,
                            [ref]$rootValue
                        )
                    } else {
                        $validRoot = [int64]::TryParse($rawRoot, [ref]$rootValue)
                    }
                }
                $rootHwnd = [IntPtr]$rootValue
                if (-not $validRoot -or $rootValue -le 0 -or
                    -not [WayseamNativeCapture]::IsWindow($rootHwnd)) {
                    Send-Json $resp 400 @{ error = 'invalid_root' }
                    $code = 400
                } else {
                    $cursor = [WayseamNativeCapture]::CaptureCursor($rootHwnd)
                    if ($null -eq $cursor) {
                        Send-Json $resp 422 @{ error = 'cursor_capture_failed' }
                        $code = 422
                    } else {
                        $png = if ($cursor.visible) {
                            [Convert]::ToBase64String($cursor.png)
                        } else { '' }
                        Send-Json $resp 200 @{
                            visible = $cursor.visible
                            shape = ('0x{0:x}' -f $cursor.shape)
                            x = $cursor.x
                            y = $cursor.y
                            hot_x = $cursor.hot_x
                            hot_y = $cursor.hot_y
                            width = $cursor.width
                            height = $cursor.height
                            png = $png
                        }
                        $code = 200
                        $extraLog = "visible=$($cursor.visible) shape=$('0x{0:x}' -f $cursor.shape)"
                    }
                }
            } elseif ($method -eq 'POST' -and $path -eq '/wayseam/input') {
                $authTag = 'ok'
                $body = Read-Body $req
                $parsed = $null
                try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { }
                $hwndValue = [int64]0
                $x = -1
                $y = -1
                $action = ''
                $button = 0
                $deltaY = 0
                $deltaX = 0
                $valid = $null -ne $parsed
                if ($valid) {
                    try {
                        $rawHwnd = [string]$parsed.hwnd
                        if ($rawHwnd.StartsWith('0x', [StringComparison]::OrdinalIgnoreCase)) {
                            $valid = [int64]::TryParse(
                                $rawHwnd.Substring(2),
                                [Globalization.NumberStyles]::AllowHexSpecifier,
                                [Globalization.CultureInfo]::InvariantCulture,
                                [ref]$hwndValue
                            )
                        } else {
                            $valid = [int64]::TryParse($rawHwnd, [ref]$hwndValue)
                        }
                        $x = [int]$parsed.x
                        $y = [int]$parsed.y
                        $action = [string]$parsed.action
                        if ($parsed.PSObject.Properties['button']) { $button = [int]$parsed.button }
                        if ($parsed.PSObject.Properties['delta_y']) { $deltaY = [int]$parsed.delta_y }
                        if ($parsed.PSObject.Properties['delta_x']) { $deltaX = [int]$parsed.delta_x }
                    } catch { $valid = $false }
                }
                $rect = [WayseamNativeCapture+RECT]::new()
                $hwnd = [IntPtr]$hwndValue
                if (-not $valid -or $hwndValue -le 0 -or
                    -not [WayseamNativeCapture]::IsWindow($hwnd) -or
                    -not [WayseamNativeCapture]::GetVisibleRect($hwnd, [ref]$rect) -or
                    $x -lt 0 -or $y -lt 0 -or
                    $x -ge ($rect.Right-$rect.Left) -or $y -ge ($rect.Bottom-$rect.Top) -or
                    $action -notin @('move','down','up','wheel') -or
                    ($action -in @('down','up') -and $button -notin @(1,2,3)) -or
                    ($action -eq 'wheel' -and (
                        ($deltaY -eq 0 -and $deltaX -eq 0) -or
                        [Math]::Abs($deltaY) -gt 12000 -or [Math]::Abs($deltaX) -gt 12000
                    ))) {
                    Send-Json $resp 400 @{ error = 'invalid_input' }
                    $code = 400
                } else {
                    $screenX = $rect.Left + $x
                    $screenY = $rect.Top + $y
                    $blockedKey = "$hwndValue`:$button"
                    $managedByHost = $false
                    if ($action -eq 'down') {
                        $hitTest = [WayseamNativeCapture]::HitTestWindow(
                            $hwnd, $screenX, $screenY
                        )
                        $managedByHost = [WayseamNativeCapture]::IsWindowManagementHit(
                            $hitTest
                        )
                    }
                    $blockedByHost = (
                        $action -eq 'up' -and
                        $script:BlockedPointerButtons.ContainsKey($blockedKey)
                    )
                    if ($action -eq 'wheel') {
                        $injected = [WayseamNativeCapture]::InjectWheel(
                            $screenX, $screenY, $deltaY, $deltaX
                        )
                        if (-not $injected) {
                            Send-Json $resp 422 @{ error = 'input_injection_failed' }
                            $code = 422
                        } else {
                            Send-Json $resp 200 @{ ok = $true; managed_by_host = $false }
                            $code = 200
                            $extraLog = "action=wheel delta_y=$deltaY delta_x=$deltaX"
                        }
                    } elseif ($action -eq 'down' -and $managedByHost) {
                        [void][WayseamNativeCapture]::SetForegroundWindow($hwnd)
                        $script:BlockedPointerButtons[$blockedKey] = $true
                        Send-Json $resp 200 @{ ok = $true; managed_by_host = $true }
                        $code = 200
                        $extraLog = "action=$action button=$button managed_by_host=true"
                    } elseif ($action -eq 'up' -and $blockedByHost) {
                        [void]$script:BlockedPointerButtons.Remove($blockedKey)
                        Send-Json $resp 200 @{ ok = $true; managed_by_host = $true }
                        $code = 200
                        $extraLog = "action=$action button=$button managed_by_host=true"
                    } else {
                        $flags = [uint32]0
                        if ($action -ne 'move') {
                            [void][WayseamNativeCapture]::SetForegroundWindow($hwnd)
                        }
                        $flags = if ($button -eq 1) {
                            if ($action -eq 'down') { 0x0002 } else { 0x0004 }
                        } elseif ($button -eq 2) {
                            if ($action -eq 'down') { 0x0020 } else { 0x0040 }
                        } else {
                            if ($action -eq 'down') { 0x0008 } else { 0x0010 }
                        }
                        if ($action -eq 'move') { $flags = [uint32]0 }
                        $injected = [WayseamNativeCapture]::InjectPointer(
                            $screenX, $screenY, [uint32]$flags
                        )
                        if (-not $injected) {
                            Send-Json $resp 422 @{ error = 'input_injection_failed' }
                            $code = 422
                        } else {
                            Send-Json $resp 200 @{ ok = $true; managed_by_host = $false }
                            $code = 200
                            $extraLog = "action=$action button=$button"
                        }
                    }
                }
            } elseif ($method -eq 'POST' -and $path -eq '/wayseam/keyboard') {
                $authTag = 'ok'
                $body = Read-Body $req
                $parsed = $null
                try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { }
                $hwndValue = [int64]0
                $action = ''
                $virtualKey = 0
                $unicode = 0
                $extended = $false
                $valid = $null -ne $parsed
                if ($valid) {
                    try {
                        $rawHwnd = [string]$parsed.hwnd
                        if ($rawHwnd.StartsWith('0x', [StringComparison]::OrdinalIgnoreCase)) {
                            $valid = [int64]::TryParse(
                                $rawHwnd.Substring(2),
                                [Globalization.NumberStyles]::AllowHexSpecifier,
                                [Globalization.CultureInfo]::InvariantCulture,
                                [ref]$hwndValue
                            )
                        } else {
                            $valid = [int64]::TryParse($rawHwnd, [ref]$hwndValue)
                        }
                        $action = [string]$parsed.action
                        $virtualKey = [int]$parsed.virtual_key
                        $unicode = [int]$parsed.unicode
                        $extended = [bool]$parsed.extended
                    } catch { $valid = $false }
                }
                $hwnd = [IntPtr]$hwndValue
                $virtualKeyEvent = (
                    $action -in @('down','up') -and
                    $virtualKey -ge 1 -and $virtualKey -le 255 -and $unicode -eq 0
                )
                $unicodeEvent = (
                    $action -eq 'press' -and $virtualKey -eq 0 -and
                    $unicode -ge 1 -and $unicode -le 1114111 -and
                    -not ($unicode -ge 55296 -and $unicode -le 57343)
                )
                if (-not $valid -or $hwndValue -le 0 -or
                    -not [WayseamNativeCapture]::IsWindow($hwnd) -or
                    (-not $virtualKeyEvent -and -not $unicodeEvent)) {
                    Send-Json $resp 400 @{ error = 'invalid_keyboard_input' }
                    $code = 400
                } else {
                    if ($action -ne 'up') {
                        [void][WayseamNativeCapture]::ActivateWindowFamily($hwnd)
                    }
                    $injected = if ($unicodeEvent) {
                        [WayseamNativeCapture]::InjectUnicode($unicode)
                    } else {
                        [WayseamNativeCapture]::InjectVirtualKey(
                            $virtualKey, $action -eq 'down', $extended
                        )
                    }
                    if (-not $injected) {
                        Send-Json $resp 422 @{ error = 'keyboard_injection_failed' }
                        $code = 422
                    } else {
                        Send-Json $resp 200 @{ ok = $true }
                        $code = 200
                        $extraLog = "action=$action virtual_key=$virtualKey unicode=$unicode"
                    }
                }
            } elseif ($method -eq 'GET' -and $path -eq '/wayseam/display') {
                $authTag = 'ok'
                Send-Json $resp 200 (Get-WayseamDisplayStatus)
                $code = 200
            } elseif ($path -eq '/wayseam/shm' -and $method -in @('GET','POST')) {
                $authTag = 'ok'
                if (-not $script:WayseamWgcAvailable) {
                    Send-Json $resp 503 @{ error = 'shm_unavailable' }
                    $code = 503
                } elseif ($method -eq 'GET') {
                    Send-Json $resp 200 @{ ok = $true; status = [WayseamWgcCapture]::ShmStatus() }
                    $code = 200
                } else {
                    $body = Read-Body $req
                    $parsed = $null
                    try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { }
                    $action = ''
                    $hwndValue = [int64]0
                    $valid = $null -ne $parsed
                    if ($valid) {
                        try {
                            $action = [string]$parsed.action
                            $rawHwnd = [string]$parsed.hwnd
                            if ($rawHwnd.StartsWith('0x', [StringComparison]::OrdinalIgnoreCase)) {
                                $valid = [int64]::TryParse(
                                    $rawHwnd.Substring(2),
                                    [Globalization.NumberStyles]::AllowHexSpecifier,
                                    [Globalization.CultureInfo]::InvariantCulture,
                                    [ref]$hwndValue
                                )
                            } else {
                                $valid = [int64]::TryParse($rawHwnd, [ref]$hwndValue)
                            }
                        } catch { $valid = $false }
                    }
                    $hwnd = [IntPtr]$hwndValue
                    if (-not $valid -or $hwndValue -le 0 -or
                        $action -notin @('assign','release') -or
                        ($action -eq 'assign' -and -not [WayseamNativeCapture]::IsWindow($hwnd))) {
                        Send-Json $resp 400 @{ error = 'invalid_shm_request' }
                        $code = 400
                    } elseif ($action -eq 'assign') {
                        $result = [WayseamWgcCapture]::ShmStart($hwnd)
                        if ($result -like 'slot *') {
                            Send-Json $resp 200 @{
                                ok = $true
                                slot = [int]$result.Substring(5)
                                slot_count = 3
                                slot_size = 22020096
                                slot0_offset = 1048576
                            }
                            $code = 200
                            $extraLog = "action=assign hwnd=0x$($hwndValue.ToString('x')) $result"
                        } else {
                            Send-Json $resp 503 @{ error = 'shm_assign_failed'; detail = $result }
                            $code = 503
                            $extraLog = "action=assign hwnd=0x$($hwndValue.ToString('x')) $result"
                        }
                    } else {
                        $result = [WayseamWgcCapture]::ShmStop($hwnd)
                        Send-Json $resp 200 @{ ok = $true; detail = $result }
                        $code = 200
                        $extraLog = "action=release hwnd=0x$($hwndValue.ToString('x')) $result"
                    }
                }
            } elseif ($path -eq '/wayseam/clipboard' -and $method -in @('GET','POST')) {
                $authTag = 'ok'
                if (-not $script:WayseamHostServicesAvailable) {
                    Send-Json $resp 503 @{ error = 'clipboard_unavailable' }
                    $code = 503
                } elseif ($method -eq 'GET') {
                    $sinceValue = [int64]-1
                    $rawSince = [string]$req.QueryString['since']
                    if (-not [string]::IsNullOrEmpty($rawSince)) {
                        if (-not [int64]::TryParse($rawSince, [ref]$sinceValue)) { $sinceValue = -1 }
                    }
                    $sequence = [int64][WayseamHostServices]::GetClipboardSequenceNumber()
                    if ($sinceValue -ge 0 -and $sequence -eq $sinceValue) {
                        Send-Json $resp 200 @{ ok = $true; sequence = $sequence; changed = $false }
                        $code = 200
                    } else {
                        $clip = [WayseamHostServices]::GetText()
                        if (-not $clip.Ok) {
                            Send-Json $resp 423 @{ error = 'clipboard_busy'; sequence = $sequence }
                            $code = 423
                        } else {
                            Send-Json $resp 200 @{
                                ok = $true; sequence = $sequence; changed = $true
                                text = $clip.Text; truncated = $clip.Truncated
                            }
                            $code = 200
                            $extraLog = "chars=$($clip.Text.Length)"
                        }
                    }
                } else {
                    $body = Read-Body $req
                    $parsed = $null
                    try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { }
                    $text = $null
                    if ($null -ne $parsed) {
                        $property = $parsed.PSObject.Properties['text']
                        if ($null -ne $property -and $property.Value -is [string]) { $text = [string]$property.Value }
                    }
                    if ($null -eq $text -or $text.Length -gt [WayseamHostServices]::MaxTextChars) {
                        Send-Json $resp 400 @{ error = 'invalid_clipboard_text' }
                        $code = 400
                    } elseif (-not [WayseamHostServices]::SetText($text)) {
                        Send-Json $resp 423 @{ error = 'clipboard_busy' }
                        $code = 423
                    } else {
                        $sequence = [int64][WayseamHostServices]::GetClipboardSequenceNumber()
                        Send-Json $resp 200 @{ ok = $true; sequence = $sequence }
                        $code = 200
                        $extraLog = "chars=$($text.Length)"
                    }
                }
            } elseif ($path -eq '/wayseam/session' -and $method -in @('GET','POST')) {
                $authTag = 'ok'
                if (-not $script:WayseamHostServicesAvailable) {
                    Send-Json $resp 503 @{ error = 'session_unavailable' }
                    $code = 503
                } else {
                    $valid = $true
                    $reconnected = $false
                    if ($method -eq 'POST') {
                        $body = Read-Body $req
                        $parsed = $null
                        try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { }
                        $action = ''
                        if ($null -ne $parsed) { try { $action = [string]$parsed.action } catch { } }
                        if ($action -ne 'console') {
                            $valid = $false
                        } else {
                            # Desktop Mode leaves this interactive session attached to an
                            # RDP WinStation, or disconnected once that client goes away.
                            # Wayseam Mode needs it back on the console so DWM composes
                            # and Windows Graphics Capture / SendInput work again.
                            $sessionId = [WayseamHostServices]::SessionId()
                            if ([WayseamHostServices]::IsConsole($sessionId) -and
                                [WayseamHostServices]::ConnectState($sessionId) -eq 0) {
                                $reconnected = $true
                            } else {
                                $tscon = Join-Path $env:SystemRoot 'System32\tscon.exe'
                                $proc = Start-Process -FilePath $tscon -ArgumentList @(
                                    "$sessionId", '/dest:console'
                                ) -Wait -PassThru -WindowStyle Hidden
                                $reconnected = ($proc.ExitCode -eq 0)
                                for ($attempt = 0; $attempt -lt 25; $attempt++) {
                                    if ([WayseamHostServices]::ConnectState($sessionId) -eq 0) { break }
                                    Start-Sleep -Milliseconds 200
                                }
                            }
                            $extraLog = "action=console reconnected=$reconnected"
                        }
                    }
                    if (-not $valid) {
                        Send-Json $resp 400 @{ error = 'invalid_session_action' }
                        $code = 400
                    } else {
                        Send-Json $resp 200 (Get-WayseamSessionState $reconnected)
                        $code = 200
                    }
                }
            } elseif ($method -eq 'POST' -and $path -eq '/wayseam/shell') {
                $authTag = 'ok'
                $body = Read-Body $req
                $parsed = $null
                try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { }
                $visible = $false
                $valid = $null -ne $parsed
                if ($valid) {
                    $property = $parsed.PSObject.Properties['visible']
                    $valid = $null -ne $property -and $property.Value -is [bool]
                    if ($valid) { $visible = [bool]$property.Value }
                }
                if (-not $valid) {
                    Send-Json $resp 400 @{ error = 'invalid_shell_visibility' }
                    $code = 400
                } else {
                    if ($visible) {
                        [void][WayseamNativeCapture]::RestoreDisplayCanvas()
                    }
                    $windows = [WayseamNativeCapture]::SetShellVisible($visible)
                    Send-Json $resp 200 @{ ok = $true; windows = $windows }
                    $code = 200
                    $extraLog = "visible=$visible windows=$windows"
                }
            } elseif ($method -eq 'POST' -and $path -eq '/wayseam/close') {
                $authTag = 'ok'
                $body = Read-Body $req
                $parsed = $null
                try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { }
                $hwndValue = [int64]0
                $valid = $null -ne $parsed
                if ($valid) {
                    try {
                        $rawHwnd = [string]$parsed.hwnd
                        if ($rawHwnd.StartsWith('0x', [StringComparison]::OrdinalIgnoreCase)) {
                            $valid = [int64]::TryParse(
                                $rawHwnd.Substring(2),
                                [Globalization.NumberStyles]::AllowHexSpecifier,
                                [Globalization.CultureInfo]::InvariantCulture,
                                [ref]$hwndValue
                            )
                        } else {
                            $valid = [int64]::TryParse($rawHwnd, [ref]$hwndValue)
                        }
                    } catch { $valid = $false }
                }
                $hwnd = [IntPtr]$hwndValue
                if (-not $valid -or $hwndValue -le 0 -or
                    -not [WayseamNativeCapture]::IsWindow($hwnd)) {
                    Send-Json $resp 400 @{ error = 'invalid_window_close' }
                    $code = 400
                } elseif (-not [WayseamNativeCapture]::CloseWindow($hwnd)) {
                    Send-Json $resp 422 @{ error = 'window_close_failed' }
                    $code = 422
                } else {
                    Send-Json $resp 200 @{ ok = $true }
                    $code = 200
                }
            } elseif ($method -eq 'POST' -and $path -eq '/wayseam/resize') {
                $authTag = 'ok'
                $body = Read-Body $req
                $parsed = $null
                try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { }
                $hwndValue = [int64]0
                $width = 0
                $height = 0
                $valid = $null -ne $parsed
                if ($valid) {
                    try {
                        $rawHwnd = [string]$parsed.hwnd
                        if ($rawHwnd.StartsWith('0x', [StringComparison]::OrdinalIgnoreCase)) {
                            $valid = [int64]::TryParse(
                                $rawHwnd.Substring(2),
                                [Globalization.NumberStyles]::AllowHexSpecifier,
                                [Globalization.CultureInfo]::InvariantCulture,
                                [ref]$hwndValue
                            )
                        } else {
                            $valid = [int64]::TryParse($rawHwnd, [ref]$hwndValue)
                        }
                        $width = [int]$parsed.width
                        $height = [int]$parsed.height
                    } catch { $valid = $false }
                }
                $hwnd = [IntPtr]$hwndValue
                if (-not $valid -or $hwndValue -le 0 -or
                    -not [WayseamNativeCapture]::IsWindow($hwnd) -or
                    $width -lt 160 -or $height -lt 120 -or
                    $width -gt 8192 -or $height -gt 8192 -or
                    ([int64]$width * [int64]$height) -gt 33554432) {
                    Send-Json $resp 400 @{ error = 'invalid_resize' }
                    $code = 400
                } else {
                    $originX = -1
                    $originY = -1
                    try {
                        if ($null -ne $parsed.PSObject.Properties['x'] -and
                            $null -ne $parsed.PSObject.Properties['y']) {
                            $ox = [int]$parsed.x
                            $oy = [int]$parsed.y
                            if ($ox -ge 0 -and $ox -le 16384 -and
                                $oy -ge 0 -and $oy -le 16384) {
                                $originX = $ox
                                $originY = $oy
                            }
                        }
                    } catch { }
                    $result = [WayseamNativeCapture]::ResizeForHost(
                        $hwnd, $width, $height, $originX, $originY
                    )
                    if (-not $result.moved -or $result.actualWidth -le 0 -or
                        $result.actualHeight -le 0) {
                        Send-Json $resp 422 @{ error = 'resize_failed' }
                        $code = 422
                    } else {
                        Send-Json $resp 200 @{
                            ok = $true
                            width = $result.actualWidth
                            height = $result.actualHeight
                            canvas_changed = $result.canvasChanged
                        }
                        $code = 200
                        $extraLog = "requested=${width}x${height} actual=$($result.actualWidth)x$($result.actualHeight) canvas_changed=$($result.canvasChanged)"
                    }
                }
            } elseif ($method -eq 'POST' -and $path -eq '/exec') {
                $authTag = 'ok'
                $body = Read-Body $req
                $parsed = $null
                try { $parsed = $body | ConvertFrom-Json -ErrorAction Stop } catch { $parsed = $null }
                if ($null -eq $parsed -or -not $parsed.PSObject.Properties['script']) {
                    Send-Json $resp 400 @{ error = 'bad_request'; detail = 'missing script field' }
                    $code = 400
                } else {
                    $timeout = $script:ExecDefaultTimeoutSec
                    if ($parsed.PSObject.Properties['timeout_sec']) {
                        try { $timeout = [int]$parsed.timeout_sec } catch { $timeout = $script:ExecDefaultTimeoutSec }
                        if ($timeout -le 0) { $timeout = $script:ExecDefaultTimeoutSec }
                        if ($timeout -gt $script:ExecMaxTimeoutSec) { $timeout = $script:ExecMaxTimeoutSec }
                    }
                    # Hand off to a pool runspace (#751): the worker sends
                    # the response and writes the /exec log line; this loop
                    # goes straight back to GetContext so /health stays
                    # answerable during long execs. code -1 = skip the
                    # main-loop Write-Log below (worker owns it).
                    $ps = [powershell]::Create()
                    $ps.RunspacePool = $script:ExecPool
                    [void]$ps.AddScript($script:ExecWorkerBody).AddArgument($resp).AddArgument([string]$parsed.script).AddArgument($timeout)
                    [void]$script:ExecWorkers.Add(@{ ps = $ps; handle = $ps.BeginInvoke() })
                    $code = -1
                }
            } else {
                $authTag = 'ok'
                Send-Json $resp 404 @{ error = 'not_found' }
                $code = 404
            }
        } catch {
            try {
                Send-Json $resp 500 @{ error = 'internal_error' }
                $code = 500
            } catch { }
        }
        $sw.Stop()
        # Successful frame fetches are the 60 Hz hot path: logging each one
        # grew agent.log past 100 MB per day. Errors and slow paths still log.
        $routineWayseamSuccess = $code -eq 200 -and (
            $path -in @('/wayseam/cursor', '/wayseam/windows', '/wayseam/top-level') -or
            ($path -eq '/wayseam/frame' -and $sw.ElapsedMilliseconds -lt 250) -or
            $path -eq '/wayseam/keyboard' -or
            ($path -eq '/wayseam/input' -and (
                $extraLog -like 'action=move*' -or $extraLog -like 'action=wheel*'
            )) -or
            ($path -eq '/wayseam/clipboard' -and $method -eq 'GET')
        )
        if ($code -ne -1 -and -not $routineWayseamSuccess) {
            Write-Log $method $path $authTag $code ([int]$sw.ElapsedMilliseconds) $extraLog
        }
    }
} finally {
    try { $listener.Stop() } catch { }
    try { $listener.Close() } catch { }
    try { $script:ExecPool.Close() } catch { }
    try { $script:ExecPool.Dispose() } catch { }
}
