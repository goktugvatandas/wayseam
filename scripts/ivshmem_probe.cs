// SPDX-License-Identifier: MIT
// Wayseam IVSHMEM guest probe: open the Red Hat IVSHMEM device through its
// device interface, map the shared BAR into this process, and expose simple
// read/write/test-pattern/latency primitives for the transport spike.
//
// Protocol (Looking Glass ivshmem driver, upstream virtio-win):
//   interface GUID df576976-569d-4672-95a0-f57e4ea0b210
//   IOCTL_IVSHMEM_REQUEST_SIZE  = 0x222004  (out: UINT64 size)
//   IOCTL_IVSHMEM_REQUEST_MMAP  = 0x222008  (in: UINT8 cacheMode; out: MMAP)
//   IOCTL_IVSHMEM_RELEASE_MMAP  = 0x22200C
using System;
using System.Runtime.InteropServices;
using System.Text;

public static class WayseamIvshmem {
  const uint IOCTL_REQUEST_SIZE = 0x222004;
  const uint IOCTL_REQUEST_MMAP = 0x222008;
  const uint IOCTL_RELEASE_MMAP = 0x22200C;


  [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
  static extern int CM_Get_Device_Interface_List_SizeW(out uint len, ref Guid guid, string device, uint flags);
  [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
  static extern int CM_Get_Device_Interface_ListW(ref Guid guid, string device, char[] buffer, uint len, uint flags);
  [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
  static extern IntPtr CreateFileW(string name, uint access, uint share, IntPtr sa, uint disposition, uint flags, IntPtr template);
  [DllImport("kernel32.dll", SetLastError = true)]
  static extern bool DeviceIoControl(IntPtr device, uint code, IntPtr inBuf, uint inLen, IntPtr outBuf, uint outLen, out uint returned, IntPtr overlapped);
  [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);

  static IntPtr handle = IntPtr.Zero;
  static IntPtr view = IntPtr.Zero;
  static ulong mappedSize = 0;

  public static string DevicePath() {
    Guid guid = new Guid("df576976-569d-4672-95a0-f57e4ea0b210");
    uint len;
    int cr = CM_Get_Device_Interface_List_SizeW(out len, ref guid, null, 0);
    if (cr != 0 || len <= 1) return "";
    char[] buffer = new char[len];
    cr = CM_Get_Device_Interface_ListW(ref guid, null, buffer, len, 0);
    if (cr != 0) return "";
    string all = new string(buffer);
    string[] parts = all.Split('\0');
    return parts.Length > 0 ? parts[0] : "";
  }

  public static string Open() {
    string path = DevicePath();
    if (path.Length == 0) return "no ivshmem device interface found";
    handle = CreateFileW(path, 0xC0000000 /*GENERIC_RW*/, 0, IntPtr.Zero, 3 /*OPEN_EXISTING*/, 0, IntPtr.Zero);
    if (handle == new IntPtr(-1)) { handle = IntPtr.Zero; return "CreateFile failed: " + Marshal.GetLastWin32Error(); }
    ulong size = 0; uint ret;
    IntPtr sizeBuf = Marshal.AllocHGlobal(8);
    try {
      if (!DeviceIoControl(handle, IOCTL_REQUEST_SIZE, IntPtr.Zero, 0, sizeBuf, 8, out ret, IntPtr.Zero))
        return "REQUEST_SIZE failed: " + Marshal.GetLastWin32Error();
      size = (ulong)Marshal.ReadInt64(sizeBuf);
    } finally { Marshal.FreeHGlobal(sizeBuf); }
    // IVSHMEM_MMAP is naturally aligned on x64: UINT16 peerID @0, UINT64
    // size @8, PVOID ptr @16, UINT16 vectors @24 => 32 bytes. A packed
    // 20-byte buffer is rejected with ERROR_INVALID_USER_BUFFER (1784).
    IntPtr inBuf = Marshal.AllocHGlobal(1);
    IntPtr outBuf = Marshal.AllocHGlobal(32);
    try {
      Marshal.WriteByte(inBuf, 0, 1); // IVSHMEM_CACHE_CACHED
      if (!DeviceIoControl(handle, IOCTL_REQUEST_MMAP, inBuf, 1, outBuf, 32, out ret, IntPtr.Zero))
        return "REQUEST_MMAP failed: " + Marshal.GetLastWin32Error();
      mappedSize = (ulong)Marshal.ReadInt64(outBuf, 8);
      view = Marshal.ReadIntPtr(outBuf, 16);
    } finally { Marshal.FreeHGlobal(inBuf); Marshal.FreeHGlobal(outBuf); }
    return "ok size=" + mappedSize + " ptr=0x" + view.ToInt64().ToString("x");
  }

  public static string WritePattern() {
    if (view == IntPtr.Zero) return "not mapped";
    byte[] magic = Encoding.ASCII.GetBytes("WAYSEAM-IVSHMEM-TEST-1");
    Marshal.Copy(magic, 0, view, magic.Length);
    // counter at offset 64, guest timestamp (QPC us) at 72
    Marshal.WriteInt64(view, 64, 42);
    return "wrote pattern";
  }

  public static string PingLoop(int seconds) {
    // Host writes an int64 request counter at offset 128; guest echoes it to
    // offset 192 with a QPC timestamp at 200. Host measures round trip.
    if (view == IntPtr.Zero) return "not mapped";
    long deadline = DateTime.UtcNow.AddSeconds(seconds).Ticks;
    long last = -1; long served = 0;
    while (DateTime.UtcNow.Ticks < deadline) {
      long req = Marshal.ReadInt64(view, 128);
      if (req != last) {
        last = req;
        Marshal.WriteInt64(view, 200, System.Diagnostics.Stopwatch.GetTimestamp());
        Marshal.WriteInt64(view, 192, req);
        served++;
      }
    }
    return "served=" + served;
  }

  public static string Bandwidth(int megabytes, int rounds) {
    // Bulk-write test: how fast can the guest push frame-sized data through
    // the CACHED BAR mapping? Writes `megabytes` MiB at offset 1 MiB, then
    // stamps a QPC timestamp + round counter so the host can time visibility.
    if (view == IntPtr.Zero) return "not mapped";
    int bytes = megabytes * 1024 * 1024;
    byte[] payload = new byte[bytes];
    new Random(7).NextBytes(payload);
    var sw = System.Diagnostics.Stopwatch.StartNew();
    for (int round = 0; round < rounds; round++) {
      payload[0] = (byte)round;
      Marshal.Copy(payload, 0, new IntPtr(view.ToInt64() + 1048576), bytes);
      Marshal.WriteInt64(view, 256, round + 1);  // publish round counter
    }
    sw.Stop();
    double mbps = megabytes * (double)rounds / (sw.ElapsedMilliseconds / 1000.0);
    return "wrote " + rounds + "x" + megabytes + "MiB in " + sw.ElapsedMilliseconds + "ms => " + mbps.ToString("F0") + " MiB/s";
  }

  public static string Close() {
    uint ret;
    if (handle != IntPtr.Zero) {
      DeviceIoControl(handle, IOCTL_RELEASE_MMAP, IntPtr.Zero, 0, IntPtr.Zero, 0, out ret, IntPtr.Zero);
      CloseHandle(handle); handle = IntPtr.Zero; view = IntPtr.Zero;
    }
    return "closed";
  }
}
