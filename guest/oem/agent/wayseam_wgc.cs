// SPDX-License-Identifier: MIT
// Per-HWND Windows Graphics Capture transport for Wayseam.
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Threading;
using Windows.Graphics;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;

public static class WayseamWgcCapture
{
    [ComImport]
    [Guid("3628E81B-3CAC-4C60-B7F4-23CE0E0C3356")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IGraphicsCaptureItemInterop
    {
        [PreserveSig]
        int CreateForWindow(IntPtr window, ref Guid iid, out IntPtr result);

        [PreserveSig]
        int CreateForMonitor(IntPtr monitor, ref Guid iid, out IntPtr result);
    }

    [ComImport]
    [Guid("A9B3D012-3DF2-4EE3-B8D1-8695F457D3C1")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IDirect3DDxgiInterfaceAccess
    {
        [PreserveSig]
        int GetInterface(ref Guid iid, out IntPtr result);
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct SampleDesc
    {
        public uint Count;
        public uint Quality;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Texture2DDesc
    {
        public uint Width;
        public uint Height;
        public uint MipLevels;
        public uint ArraySize;
        public uint Format;
        public SampleDesc SampleDesc;
        public uint Usage;
        public uint BindFlags;
        public uint CpuAccessFlags;
        public uint MiscFlags;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MappedSubresource
    {
        public IntPtr Data;
        public uint RowPitch;
        public uint DepthPitch;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct D3D11Box
    {
        public uint Left;
        public uint Top;
        public uint Front;
        public uint Right;
        public uint Bottom;
        public uint Back;
    }

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int CreateTexture2DDelegate(
        IntPtr self,
        ref Texture2DDesc desc,
        IntPtr initialData,
        out IntPtr texture);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate void GetTextureDescDelegate(IntPtr self, out Texture2DDesc desc);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate void CopyResourceDelegate(IntPtr self, IntPtr destination, IntPtr source);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate void CopySubresourceRegionDelegate(
        IntPtr self,
        IntPtr destination,
        uint destinationSubresource,
        uint destinationX,
        uint destinationY,
        uint destinationZ,
        IntPtr source,
        uint sourceSubresource,
        ref D3D11Box sourceBox);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate int MapDelegate(
        IntPtr self,
        IntPtr resource,
        uint subresource,
        uint mapType,
        uint mapFlags,
        out MappedSubresource mapped);

    [UnmanagedFunctionPointer(CallingConvention.StdCall)]
    private delegate void UnmapDelegate(IntPtr self, IntPtr resource, uint subresource);

    [DllImport("d3d11.dll")]
    private static extern int D3D11CreateDevice(
        IntPtr adapter,
        int driverType,
        IntPtr software,
        uint flags,
        IntPtr featureLevels,
        uint featureLevelCount,
        uint sdkVersion,
        out IntPtr device,
        out int featureLevel,
        out IntPtr context);

    [DllImport("d3d11.dll")]
    private static extern int CreateDirect3D11DeviceFromDXGIDevice(
        IntPtr dxgiDevice,
        out IntPtr graphicsDevice);

    [DllImport("msvcrt.dll", CallingConvention = CallingConvention.Cdecl)]
    private static extern int memcmp(IntPtr first, IntPtr second, UIntPtr length);

    public sealed class DeltaCaptureResult
    {
        public bool Ok;
        public byte[] Bytes;
        public int Width;
        public int Height;
    }

    private sealed class DeviceBundle : IDisposable
    {
        public IntPtr NativeDevice;
        public IntPtr NativeContext;
        public IDirect3DDevice RuntimeDevice;

        public void Dispose()
        {
            RuntimeDevice = null;
            if (NativeContext != IntPtr.Zero)
            {
                Marshal.Release(NativeContext);
                NativeContext = IntPtr.Zero;
            }
            if (NativeDevice != IntPtr.Zero)
            {
                Marshal.Release(NativeDevice);
                NativeDevice = IntPtr.Zero;
            }
        }
    }

    private sealed class SessionState : IDisposable
    {
        public readonly object Sync = new object();
        public readonly AutoResetEvent FrameReady = new AutoResetEvent(false);
        public IntPtr Hwnd;
        public DeviceBundle Device;
        public GraphicsCaptureItem Item;
        public Direct3D11CaptureFramePool Pool;
        public GraphicsCaptureSession Session;
        public bool FrameHandlerRegistered;
        public IntPtr Staging;
        public uint StagingWidth;
        public uint StagingHeight;
        public uint StagingFormat;
        public int PoolWidth;
        public int PoolHeight;
        public byte[] Pixels;
        public int Width;
        public int Height;
        public int FrameVersion;
        public int DirtyLeft;
        public int DirtyTop;
        public int DirtyRight;
        public int DirtyBottom;
        public DateTime LastSeen;
        // Crop mode (display capture cropped to the window; see WantsCropMode).
        public bool CropMode;
        public int MonitorLeft;
        public int MonitorTop;
        public int CropLeft;
        public int CropTop;

        public void OnFrameArrived(Direct3D11CaptureFramePool sender, object args)
        {
            FrameReady.Set();
        }

        public void Dispose()
        {
            lock (Sync)
            {
                if (Pool != null && FrameHandlerRegistered)
                {
                    try { Pool.FrameArrived -= OnFrameArrived; }
                    catch { }
                    FrameHandlerRegistered = false;
                }
                if (Session != null) Session.Dispose();
                if (Pool != null) Pool.Dispose();
                if (Staging != IntPtr.Zero)
                {
                    Marshal.Release(Staging);
                    Staging = IntPtr.Zero;
                }
                if (Device != null) Device.Dispose();
                FrameReady.Dispose();
                Session = null;
                Pool = null;
                Device = null;
                Item = null;
                Pixels = null;
            }
        }
    }

    private sealed class StreamState
    {
        public readonly object Sync = new object();
        public int Width;
        public int Height;
        public int Sequence;
        public int SourceVersion;
        public DateTime LastSeen;
    }

    private static readonly object SessionsLock = new object();
    private static readonly Dictionary<long, SessionState> Sessions =
        new Dictionary<long, SessionState>();
    private static readonly object StreamsLock = new object();
    private static readonly Dictionary<string, StreamState> Streams =
        new Dictionary<string, StreamState>();

    public static bool IsSupported()
    {
        try { return GraphicsCaptureSession.IsSupported(); }
        catch { return false; }
    }

    private static void Check(int hresult)
    {
        if (hresult < 0) Marshal.ThrowExceptionForHR(hresult);
    }

    private static IntPtr VtableMethod(IntPtr instance, int index)
    {
        IntPtr table = Marshal.ReadIntPtr(instance);
        return Marshal.ReadIntPtr(table, index * IntPtr.Size);
    }

    private static T Method<T>(IntPtr instance, int index) where T : class
    {
        return Marshal.GetDelegateForFunctionPointer(
            VtableMethod(instance, index), typeof(T)) as T;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int GetClassNameW(IntPtr hwnd, System.Text.StringBuilder text, int count);
    [DllImport("user32.dll")]
    private static extern IntPtr MonitorFromWindow(IntPtr hwnd, uint flags);
    [StructLayout(LayoutKind.Sequential)]
    private struct MONITORINFO { public int Size; public Inp.RECT Monitor; public Inp.RECT Work; public uint Flags; }
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool GetMonitorInfoW(IntPtr monitor, ref MONITORINFO info);

    private static string WindowClass(IntPtr hwnd)
    {
        var text = new System.Text.StringBuilder(256);
        GetClassNameW(hwnd, text, text.Capacity);
        return text.ToString();
    }

    // Packaged (UWP) apps render through ApplicationFrameHost; per-window
    // capture of the frame yields only chrome and of the CoreWindow only
    // black. DWM does compose them to the display, so those windows are
    // captured from their monitor and cropped to the window rectangle. The
    // guest mirrors the host tiling, so nothing else covers the window.
    private static bool WantsCropMode(IntPtr hwnd)
    {
        return WindowClass(hwnd) == "ApplicationFrameWindow";
    }

    private static GraphicsCaptureItem CreateItemForMonitor(IntPtr monitor)
    {
        object factory = WindowsRuntimeMarshal.GetActivationFactory(typeof(GraphicsCaptureItem));
        IGraphicsCaptureItemInterop interop = (IGraphicsCaptureItemInterop)factory;
        Guid iid = new Guid("79C3F95B-31F7-4EC2-A464-632EF5D30760");
        IntPtr pointer;
        Check(interop.CreateForMonitor(monitor, ref iid, out pointer));
        try { return (GraphicsCaptureItem)Marshal.GetObjectForIUnknown(pointer); }
        finally { Marshal.Release(pointer); }
    }

    private static GraphicsCaptureItem CreateItem(IntPtr hwnd)
    {
        object factory = WindowsRuntimeMarshal.GetActivationFactory(typeof(GraphicsCaptureItem));
        IGraphicsCaptureItemInterop interop = (IGraphicsCaptureItemInterop)factory;
        Guid iid = new Guid("79C3F95B-31F7-4EC2-A464-632EF5D30760");
        IntPtr pointer;
        Check(interop.CreateForWindow(hwnd, ref iid, out pointer));
        try { return (GraphicsCaptureItem)Marshal.GetObjectForIUnknown(pointer); }
        finally { Marshal.Release(pointer); }
    }

    private static DeviceBundle CreateDevice()
    {
        IntPtr device = IntPtr.Zero;
        IntPtr context = IntPtr.Zero;
        IntPtr dxgi = IntPtr.Zero;
        IntPtr inspectable = IntPtr.Zero;
        int featureLevel;
        int result = D3D11CreateDevice(
            IntPtr.Zero, 1, IntPtr.Zero, 0x20,
            IntPtr.Zero, 0, 7, out device, out featureLevel, out context);
        if (result < 0)
        {
            result = D3D11CreateDevice(
                IntPtr.Zero, 5, IntPtr.Zero, 0x20,
                IntPtr.Zero, 0, 7, out device, out featureLevel, out context);
        }
        Check(result);
        try
        {
            Guid iid = new Guid("54EC77FA-1377-44E6-8C32-88FD5F44C84C");
            Check(Marshal.QueryInterface(device, ref iid, out dxgi));
            Check(CreateDirect3D11DeviceFromDXGIDevice(dxgi, out inspectable));
            DeviceBundle bundle = new DeviceBundle();
            bundle.NativeDevice = device;
            bundle.NativeContext = context;
            bundle.RuntimeDevice =
                (IDirect3DDevice)Marshal.GetObjectForIUnknown(inspectable);
            device = IntPtr.Zero;
            context = IntPtr.Zero;
            return bundle;
        }
        finally
        {
            if (inspectable != IntPtr.Zero) Marshal.Release(inspectable);
            if (dxgi != IntPtr.Zero) Marshal.Release(dxgi);
            if (context != IntPtr.Zero) Marshal.Release(context);
            if (device != IntPtr.Zero) Marshal.Release(device);
        }
    }

    private static SessionState CreateSession(IntPtr hwnd)
    {
        SessionState state = new SessionState();
        try
        {
            state.Hwnd = hwnd;
            if (WantsCropMode(hwnd))
            {
                IntPtr monitor = MonitorFromWindow(hwnd, 2 /* MONITOR_DEFAULTTONEAREST */);
                MONITORINFO info = new MONITORINFO();
                info.Size = Marshal.SizeOf(typeof(MONITORINFO));
                if (monitor == IntPtr.Zero || !GetMonitorInfoW(monitor, ref info))
                    throw new InvalidOperationException("monitor for crop capture not found");
                state.CropMode = true;
                state.MonitorLeft = info.Monitor.Left;
                state.MonitorTop = info.Monitor.Top;
                state.Item = CreateItemForMonitor(monitor);
            }
            else
            {
                state.Item = CreateItem(hwnd);
            }
            state.Device = CreateDevice();
            state.PoolWidth = state.Item.Size.Width;
            state.PoolHeight = state.Item.Size.Height;
            state.Pool = Direct3D11CaptureFramePool.CreateFreeThreaded(
                state.Device.RuntimeDevice,
                DirectXPixelFormat.B8G8R8A8UIntNormalized,
                2,
                state.Item.Size);
            state.Pool.FrameArrived += state.OnFrameArrived;
            state.FrameHandlerRegistered = true;
            state.Session = state.Pool.CreateCaptureSession(state.Item);
            state.Session.IsCursorCaptureEnabled = false;
            state.Session.DirtyRegionMode = GraphicsCaptureDirtyRegionMode.ReportOnly;
            state.Session.MinUpdateInterval = TimeSpan.FromMilliseconds(1);
            try { state.Session.IsBorderRequired = false; }
            catch { }
            state.LastSeen = DateTime.UtcNow;
            state.Session.StartCapture();
            return state;
        }
        catch
        {
            state.Dispose();
            throw;
        }
    }

    [DllImport("dwmapi.dll")]
    private static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);
    private const int DWMWA_WINDOW_CORNER_PREFERENCE = 33;
    private const int DWMWCP_DEFAULT = 0;
    private const int DWMWCP_DONOTROUND = 1;
    private const int DWMWA_SYSTEMBACKDROP_TYPE = 38;
    private const int DWMSBT_AUTO = 0;
    private const int DWMSBT_NONE = 1;

    private static void SetSystemBackdrop(IntPtr hwnd, int kind)
    {
        // Mica/Acrylic backdrops are composed by DWM from what lies behind the
        // window, so Windows Graphics Capture returns those regions as
        // transparent — File Explorer presented as a see-through tile. With
        // the backdrop off the app paints a solid background we can capture.
        try
        {
            int value = kind;
            DwmSetWindowAttribute(hwnd, DWMWA_SYSTEMBACKDROP_TYPE, ref value, 4);
        }
        catch { }
    }

    private const int DWMWA_BORDER_COLOR = 34;
    private const int DWMWA_COLOR_NONE = unchecked((int)0xFFFFFFFE);
    private const int DWMWA_COLOR_DEFAULT = unchecked((int)0xFFFFFFFF);

    private static void SetBorderColor(IntPtr hwnd, int color)
    {
        // Windows 11 paints a 1px window border into the frame WGC captures;
        // inside a host tile that hairline reads as a seam and breaks the
        // native-window illusion. Presented windows get no guest border —
        // the host theme's border is the only one the user sees.
        try
        {
            int value = color;
            DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ref value, 4);
        }
        catch { }
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern long GetWindowLongPtrW(IntPtr hwnd, int index);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern long SetWindowLongPtrW(IntPtr hwnd, int index, long value);
    private const int GWL_STYLE = -16;
    private const long WS_MAXIMIZEBOX = 0x00010000;

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetWindowPos(
        IntPtr hwnd, IntPtr after, int x, int y, int w, int h, uint flags);

    private static void NudgeRepaint(IntPtr hwnd)
    {
        // DirectComposition apps (Windows Terminal) hand WGC a black surface
        // until their next present; a frame-changed SetWindowPos forces DWM
        // to re-compose without moving or resizing anything — the same effect
        // the user got by manually resizing the tile once.
        try
        {
            // SWP_NOMOVE|NOSIZE|NOZORDER|NOACTIVATE|FRAMECHANGED
            SetWindowPos(hwnd, IntPtr.Zero, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020);
        }
        catch { }
    }

    private static void SetMaximizeBox(IntPtr hwnd, bool enabled)
    {
        // With HTCAPTION clicks allowed through (caption strips hold real
        // controls now), a double-click would maximize inside the tile.
        // Removing WS_MAXIMIZEBOX disables caption double-click maximize.
        try
        {
            long style = GetWindowLongPtrW(hwnd, GWL_STYLE);
            long updated = enabled ? style | WS_MAXIMIZEBOX : style & ~WS_MAXIMIZEBOX;
            if (updated != style) SetWindowLongPtrW(hwnd, GWL_STYLE, updated);
        }
        catch { }
    }

    private static void SetCornerPreference(IntPtr hwnd, int preference)
    {
        // Windows 11 bakes rounded corners (and transparent corner pixels)
        // into every top-level window; captured as-is they fight the host
        // theme's own border radius. Presented windows get square corners so
        // Hyprland's rounding is the only rounding the user sees.
        try
        {
            int value = preference;
            DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ref value, 4);
        }
        catch { }
    }

    private static SessionState GetSession(IntPtr hwnd)
    {
        long key = hwnd.ToInt64();
        lock (SessionsLock)
        {
            SessionState state;
            if (!Sessions.TryGetValue(key, out state))
            {
                state = CreateSession(hwnd);
                Sessions[key] = state;
                SetCornerPreference(hwnd, DWMWCP_DONOTROUND);
                SetBorderColor(hwnd, DWMWA_COLOR_NONE);
                SetSystemBackdrop(hwnd, DWMSBT_NONE);
                SetMaximizeBox(hwnd, false);
                NudgeRepaint(hwnd);
            }
            state.LastSeen = DateTime.UtcNow;
            return state;
        }
    }

    private static StreamState GetStream(string streamKey)
    {
        lock (StreamsLock)
        {
            StreamState state;
            if (!Streams.TryGetValue(streamKey, out state))
            {
                state = new StreamState();
                Streams[streamKey] = state;
            }
            state.LastSeen = DateTime.UtcNow;
            return state;
        }
    }

    private static void RemoveSession(IntPtr hwnd, SessionState expected)
    {
        bool dispose = false;
        lock (SessionsLock)
        {
            SessionState current;
            if (Sessions.TryGetValue(hwnd.ToInt64(), out current) &&
                Object.ReferenceEquals(current, expected))
            {
                Sessions.Remove(hwnd.ToInt64());
                dispose = true;
            }
        }
        if (dispose)
        {
            expected.Dispose();
            SetCornerPreference(hwnd, DWMWCP_DEFAULT);
            SetBorderColor(hwnd, DWMWA_COLOR_DEFAULT);
            SetSystemBackdrop(hwnd, DWMSBT_AUTO);
            SetMaximizeBox(hwnd, true);
        }
    }

    private static void EnsureStaging(SessionState state, IntPtr source)
    {
        Texture2DDesc desc;
        Method<GetTextureDescDelegate>(source, 10)(source, out desc);
        if (state.Staging != IntPtr.Zero &&
            state.StagingWidth == desc.Width &&
            state.StagingHeight == desc.Height &&
            state.StagingFormat == desc.Format)
        {
            return;
        }
        if (state.Staging != IntPtr.Zero)
        {
            Marshal.Release(state.Staging);
            state.Staging = IntPtr.Zero;
        }
        desc.Usage = 3;
        desc.BindFlags = 0;
        desc.CpuAccessFlags = 0x20000;
        desc.MiscFlags = 0;
        Check(Method<CreateTexture2DDelegate>(state.Device.NativeDevice, 5)(
            state.Device.NativeDevice, ref desc, IntPtr.Zero, out state.Staging));
        state.StagingWidth = desc.Width;
        state.StagingHeight = desc.Height;
        state.StagingFormat = desc.Format;
    }

    private static void UpdatePixels(
        SessionState state,
        object surface,
        int width,
        int height,
        int left,
        int top,
        int right,
        int bottom,
        bool full,
        int sourceX,
        int sourceY)
    {
        IDirect3DDxgiInterfaceAccess access = (IDirect3DDxgiInterfaceAccess)surface;
        Guid textureIid = new Guid("6F15AAF2-D208-4E89-9AB4-489535D34F9C");
        IntPtr source = IntPtr.Zero;
        bool mapped = false;
        try
        {
            Check(access.GetInterface(ref textureIid, out source));
            EnsureStaging(state, source);
            if (full && sourceX == 0 && sourceY == 0 && !state.CropMode)
            {
                Method<CopyResourceDelegate>(state.Device.NativeContext, 47)(
                    state.Device.NativeContext, state.Staging, source);
            }
            else
            {
                // Window-space rectangle [left,right)x[top,bottom) lives at
                // (sourceX, sourceY) inside the source texture (the monitor in
                // crop mode); copy it to the same place in the staging copy.
                D3D11Box box = new D3D11Box();
                box.Left = (uint)Math.Min((uint)(sourceX + left), state.StagingWidth);
                box.Top = (uint)Math.Min((uint)(sourceY + top), state.StagingHeight);
                box.Front = 0;
                box.Right = (uint)Math.Min((uint)(sourceX + right), state.StagingWidth);
                box.Bottom = (uint)Math.Min((uint)(sourceY + bottom), state.StagingHeight);
                if (box.Right <= box.Left || box.Bottom <= box.Top)
                {
                    return;
                }
                box.Back = 1;
                Method<CopySubresourceRegionDelegate>(state.Device.NativeContext, 46)(
                    state.Device.NativeContext,
                    state.Staging,
                    0,
                    box.Left,
                    box.Top,
                    0,
                    source,
                    0,
                    ref box);
            }
            MappedSubresource data;
            Check(Method<MapDelegate>(state.Device.NativeContext, 14)(
                state.Device.NativeContext, state.Staging, 0, 1, 0, out data));
            mapped = true;
            int stride = checked(width * 4);
            if (state.Pixels == null || state.Pixels.Length != checked(stride * height))
            {
                state.Pixels = new byte[checked(stride * height)];
                full = true;
                left = 0;
                top = 0;
                right = width;
                bottom = height;
            }
            // ContentSize describes the window, but the mapped staging copy is
            // exactly as large as the pool surface that produced this frame.
            // Around a resize the two disagree for a frame or two (frames
            // already in flight keep the old surface size), and copying
            // ContentSize rows out of a smaller mapping read past the end of
            // the allocation and killed the whole agent with an access
            // violation. Copy only the intersection; the next frame after the
            // pool recreate repaints whatever was left stale.
            int availableWidth = (int)state.StagingWidth - sourceX;
            int availableHeight = (int)state.StagingHeight - sourceY;
            if (availableWidth <= 0 || availableHeight <= 0) return;
            int mappedWidth = Math.Min(availableWidth, width);
            int mappedHeight = Math.Min(availableHeight, height);
            if (data.RowPitch < (uint)(sourceX + mappedWidth) * 4u)
            {
                mappedWidth = (int)(data.RowPitch / 4u) - sourceX;
            }
            right = Math.Min(right, mappedWidth);
            bottom = Math.Min(bottom, mappedHeight);
            if (right <= left || bottom <= top) return;
            int rowBytes = checked((right - left) * 4);
            for (int y = top; y < bottom; y++)
            {
                Marshal.Copy(
                    IntPtr.Add(data.Data, checked((int)data.RowPitch * (sourceY + y) + (sourceX + left) * 4)),
                    state.Pixels,
                    y * stride + left * 4,
                    rowBytes);
            }
        }
        finally
        {
            if (mapped)
            {
                Method<UnmapDelegate>(state.Device.NativeContext, 15)(
                    state.Device.NativeContext, state.Staging, 0);
            }
            if (source != IntPtr.Zero) Marshal.Release(source);
        }
    }

    // ------------------------------------------------------------------
    // IVSHMEM frame ring (ADR 0004). Layout, little-endian:
    //   header@0: magic "WSRING1\0", version u32@8=1, slot_count u32@12,
    //     slot_size u64@16, slot0_offset u64@24; per-slot descriptor at
    //     64+i*32: hwnd u64, pid u32, active u32, heartbeat u64.
    //   slot i @ slot0_offset+i*slot_size: seq u64@0 (odd=writing),
    //     length u64@8, flags u32@16 (bit0 = frame too large), blob @64
    //     holding the exact WSD1 encoding EncodeDelta produces.
    // The writer never blocks on the host; the host seqlock-reads.
    private const int RingSlotCount = 3;
    private const long RingSlotSize = 21L * 1024 * 1024;
    private const long RingSlot0Offset = 1048576;
    private const int RingSlotDataOffset = 64;

    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
    private static extern int CM_Get_Device_Interface_List_SizeW(
        out uint length, ref Guid guid, string device, uint flags);
    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode)]
    private static extern int CM_Get_Device_Interface_ListW(
        ref Guid guid, string device, char[] buffer, uint length, uint flags);
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr CreateFileW(
        string name, uint access, uint share, IntPtr security,
        uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool DeviceIoControl(
        IntPtr device, uint code, IntPtr inBuffer, uint inLength,
        IntPtr outBuffer, uint outLength, out uint returned, IntPtr overlapped);

    private static readonly object RingLock = new object();
    private static IntPtr RingHandle = IntPtr.Zero;
    private static IntPtr RingView = IntPtr.Zero;
    private static long RingSize;
    private static readonly Dictionary<int, ShmStream> ShmStreams =
        new Dictionary<int, ShmStream>();

    private sealed class ShmStream
    {
        public int Slot;
        public IntPtr Hwnd;
        public Thread Worker;
        public volatile bool Stop;
        public string Error = "";
        public long Published;
    }

    private static string RingEnsureMapped()
    {
        lock (RingLock)
        {
            if (RingView != IntPtr.Zero) return "";
            Guid guid = new Guid("df576976-569d-4672-95a0-f57e4ea0b210");
            uint length;
            if (CM_Get_Device_Interface_List_SizeW(out length, ref guid, null, 0) != 0 ||
                length <= 1)
            {
                return "ivshmem device interface not present";
            }
            char[] buffer = new char[length];
            if (CM_Get_Device_Interface_ListW(ref guid, null, buffer, length, 0) != 0)
            {
                return "ivshmem interface list failed";
            }
            string path = new string(buffer).Split('\0')[0];
            if (path.Length == 0) return "ivshmem interface path empty";
            IntPtr handle = CreateFileW(path, 0xC0000000, 0, IntPtr.Zero, 3, 0, IntPtr.Zero);
            if (handle == new IntPtr(-1)) return "ivshmem open failed: " + Marshal.GetLastWin32Error();
            uint returned;
            // A restarting agent can race the previous process's mapping for a
            // few seconds (single-mapper device -> error 548). Handled by the
            // bounded retry around the mmap ioctl below.
            IntPtr sizeBuffer = Marshal.AllocHGlobal(8);
            IntPtr inBuffer = Marshal.AllocHGlobal(1);
            IntPtr outBuffer = Marshal.AllocHGlobal(32);
            try
            {
                if (!DeviceIoControl(handle, 0x222004, IntPtr.Zero, 0, sizeBuffer, 8, out returned, IntPtr.Zero))
                {
                    return "ivshmem size ioctl failed: " + Marshal.GetLastWin32Error();
                }
                long size = Marshal.ReadInt64(sizeBuffer);
                Marshal.WriteByte(inBuffer, 0, 1); // cached mapping
                bool mapped = false;
                int mapError = 0;
                for (int attempt = 0; attempt < 20 && !mapped; attempt++)
                {
                    mapped = DeviceIoControl(
                        handle, 0x222008, inBuffer, 1, outBuffer, 32, out returned, IntPtr.Zero);
                    if (!mapped)
                    {
                        mapError = Marshal.GetLastWin32Error();
                        if (mapError != 548) break;
                        System.Threading.Thread.Sleep(500);
                    }
                }
                if (!mapped)
                {
                    return "ivshmem mmap ioctl failed: " + mapError;
                }
                RingHandle = handle;
                RingView = Marshal.ReadIntPtr(outBuffer, 16);
                RingSize = Marshal.ReadInt64(outBuffer, 8);
                if (RingSize < RingSlot0Offset + RingSlotCount * RingSlotSize)
                {
                    return "ivshmem region smaller than ring layout";
                }
                // Write the ring header once.
                byte[] magic = System.Text.Encoding.ASCII.GetBytes("WSRING1\0");
                Marshal.Copy(magic, 0, RingView, 8);
                Marshal.WriteInt32(RingView, 8, 1);
                Marshal.WriteInt32(RingView, 12, RingSlotCount);
                Marshal.WriteInt64(RingView, 16, RingSlotSize);
                Marshal.WriteInt64(RingView, 24, RingSlot0Offset);
                return "";
            }
            finally
            {
                Marshal.FreeHGlobal(sizeBuffer);
                Marshal.FreeHGlobal(inBuffer);
                Marshal.FreeHGlobal(outBuffer);
            }
        }
    }

    private static IntPtr RingSlotBase(int slot)
    {
        return new IntPtr(RingView.ToInt64() + RingSlot0Offset + slot * RingSlotSize);
    }

    private static void RingDescriptor(int slot, long hwnd, int active)
    {
        IntPtr desc = new IntPtr(RingView.ToInt64() + 64 + slot * 32);
        Marshal.WriteInt64(desc, 0, hwnd);
        Marshal.WriteInt32(desc, 8, System.Diagnostics.Process.GetCurrentProcess().Id);
        Marshal.WriteInt32(desc, 12, active);
        Marshal.WriteInt64(desc, 16, System.Diagnostics.Stopwatch.GetTimestamp());
    }

    private static void RingPublish(int slot, byte[] blob)
    {
        IntPtr basePtr = RingSlotBase(slot);
        long seq = Marshal.ReadInt64(basePtr, 0);
        bool tooLarge = blob.Length > RingSlotSize - RingSlotDataOffset;
        Marshal.WriteInt64(basePtr, 0, seq + 1); // odd: writing
        Thread.MemoryBarrier();
        if (tooLarge)
        {
            Marshal.WriteInt64(basePtr, 8, 0);
            Marshal.WriteInt32(basePtr, 16, 1);
        }
        else
        {
            Marshal.Copy(blob, 0, new IntPtr(basePtr.ToInt64() + RingSlotDataOffset), blob.Length);
            Marshal.WriteInt64(basePtr, 8, blob.Length);
            Marshal.WriteInt32(basePtr, 16, 0);
        }
        Thread.MemoryBarrier();
        Marshal.WriteInt64(basePtr, 0, seq + 2); // even: published
    }

    // Self-contained input injection: the WGC helper compiles as its own
    // assembly and cannot see WayseamNativeCapture, so it declares the small
    // user32 surface it needs. Semantics match the HTTP route exactly.
    private static class Inp
    {
        [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
        [StructLayout(LayoutKind.Sequential)] public struct MOUSEINPUT { public int dx, dy; public uint mouseData, dwFlags, time; public UIntPtr dwExtraInfo; }
        [StructLayout(LayoutKind.Sequential)] public struct KEYBDINPUT { public ushort wVk, wScan; public uint dwFlags, time; public UIntPtr dwExtraInfo; }
        [StructLayout(LayoutKind.Sequential)] public struct INPUT { public uint type; public MOUSEINPUT mi; }
        [StructLayout(LayoutKind.Explicit, Size = 40)] public struct KEYINPUT { [FieldOffset(0)] public uint type; [FieldOffset(8)] public KEYBDINPUT ki; }
        [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
        [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
        [DllImport("dwmapi.dll")] static extern int DwmGetWindowAttribute(IntPtr h, int attr, out RECT r, int size);
        public static bool GetVisibleRect(IntPtr h, out RECT r) {
            if (DwmGetWindowAttribute(h, 9, out r, Marshal.SizeOf(typeof(RECT))) == 0 && r.Right > r.Left && r.Bottom > r.Top) return true;
            return GetWindowRect(h, out r);
        }
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
        [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr h, uint cmd);
        [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
        [DllImport("user32.dll", SetLastError = true)] public static extern uint SendInput(uint n, INPUT[] inputs, int size);
        [DllImport("user32.dll", EntryPoint = "SendInput", SetLastError = true)] public static extern uint SendKeyInput(uint n, KEYINPUT[] inputs, int size);
        [DllImport("winmm.dll")] public static extern uint timeBeginPeriod(uint period);

        public static bool Pointer(int screenX, int screenY, uint buttonFlags)
        {
            int left = GetSystemMetrics(76), top = GetSystemMetrics(77);
            int width = Math.Max(1, GetSystemMetrics(78)), height = Math.Max(1, GetSystemMetrics(79));
            int dx = (int)Math.Round((screenX - left) * 65535.0 / Math.Max(1, width - 1));
            int dy = (int)Math.Round((screenY - top) * 65535.0 / Math.Max(1, height - 1));
            dx = Math.Max(0, Math.Min(65535, dx)); dy = Math.Max(0, Math.Min(65535, dy));
            INPUT input = new INPUT { type = 0, mi = new MOUSEINPUT { dx = dx, dy = dy, mouseData = 0, dwFlags = 0x0001 | 0x2000 | 0x4000 | 0x8000 | buttonFlags, time = 0, dwExtraInfo = UIntPtr.Zero } };
            return SendInput(1, new INPUT[] { input }, Marshal.SizeOf(typeof(INPUT))) == 1;
        }

        private static bool WheelAxis(uint amount, uint axisFlag)
        {
            INPUT input = new INPUT { type = 0, mi = new MOUSEINPUT { dx = 0, dy = 0, mouseData = amount, dwFlags = axisFlag, time = 0, dwExtraInfo = UIntPtr.Zero } };
            return SendInput(1, new INPUT[] { input }, Marshal.SizeOf(typeof(INPUT))) == 1;
        }

        public static bool Wheel(int screenX, int screenY, int deltaY, int deltaX)
        {
            if (!Pointer(screenX, screenY, 0)) return false;
            if (deltaY != 0 && !WheelAxis(unchecked((uint)deltaY), 0x0800)) return false;
            if (deltaX != 0 && !WheelAxis(unchecked((uint)deltaX), 0x1000)) return false;
            return true;
        }

        public static bool VirtualKey(int vk, bool down, bool extended)
        {
            uint flags = down ? 0u : 0x0002u;
            if (extended) flags |= 0x0001u;
            KEYINPUT input = new KEYINPUT { type = 1, ki = new KEYBDINPUT { wVk = (ushort)vk, wScan = 0, dwFlags = flags, time = 0, dwExtraInfo = UIntPtr.Zero } };
            return SendKeyInput(1, new KEYINPUT[] { input }, Marshal.SizeOf(typeof(KEYINPUT))) == 1;
        }

        public static bool Unicode(int codepoint)
        {
            string text;
            try { text = Char.ConvertFromUtf32(codepoint); }
            catch (ArgumentOutOfRangeException) { return false; }
            foreach (char unit in text)
            {
                KEYINPUT down = new KEYINPUT { type = 1, ki = new KEYBDINPUT { wVk = 0, wScan = unit, dwFlags = 0x0004, time = 0, dwExtraInfo = UIntPtr.Zero } };
                KEYINPUT up = down; up.ki.dwFlags = 0x0004 | 0x0002;
                if (SendKeyInput(1, new KEYINPUT[] { down }, Marshal.SizeOf(typeof(KEYINPUT))) != 1) return false;
                if (SendKeyInput(1, new KEYINPUT[] { up }, Marshal.SizeOf(typeof(KEYINPUT))) != 1) return false;
            }
            return true;
        }

        public static void ActivateFamily(IntPtr root)
        {
            if (!IsWindow(root)) return;
            IntPtr current = GetForegroundWindow();
            while (current != IntPtr.Zero)
            {
                if (current == root) return;
                current = GetWindow(current, 4); // GW_OWNER
            }
            SetForegroundWindow(root);
        }
    }

    // ------------------------------------------------------------------
    // Input ring (host -> guest), in the header page at offset 2048:
    //   magic "WSINP1\0\0" @2048, head u64 @2064 (host producer),
    //   tail u64 @2072 (guest consumer), entries @2112: 1024 x 48 bytes:
    //   type u32, flags u32, hwnd u64, a,b,c,d,e,f i32.
    //   Types: 1 move(x,y) 4 wheel(x,y,dx,dy) 5 keydown(vk,ext)
    //          6 keyup(vk,ext) 7 unicode(cp). Clicks stay on HTTP for the
    //   managed_by_host hit-test; the host flushes the ring before a click.
    private const long InputMagicOffset = 2048;
    private const long InputHeadOffset = 2064;
    private const long InputTailOffset = 2072;
    private const long InputEntriesOffset = 2112;
    private const int InputEntrySize = 48;
    private const int InputEntryCount = 1024;

    private static Thread InputWorker;
    private static volatile bool InputStop;
    private static long InputConsumed;

    private static void InputEnsureStarted()
    {
        lock (RingLock)
        {
            if (RingView == IntPtr.Zero) return;
            if (InputWorker != null && InputWorker.IsAlive) return;
            InputStop = false;
            byte[] magic = System.Text.Encoding.ASCII.GetBytes("WSINP1\0\0");
            Marshal.Copy(magic, 0, new IntPtr(RingView.ToInt64() + InputMagicOffset), 8);
            Marshal.WriteInt64(RingView, (int)InputTailOffset,
                Marshal.ReadInt64(RingView, (int)InputHeadOffset));
            InputWorker = new Thread(InputLoop) { IsBackground = true, Priority = ThreadPriority.AboveNormal };
            InputWorker.Start();
        }
    }

    public static string InputStatus()
    {
        bool alive = InputWorker != null && InputWorker.IsAlive;
        return "input_ring=" + alive + " consumed=" + Interlocked.Read(ref InputConsumed);
    }

    private static void InputLoop()
    {
        Inp.timeBeginPeriod(1);
        IntPtr view = RingView;
        long entriesBase = view.ToInt64() + InputEntriesOffset;
        while (!InputStop)
        {
            long head = Marshal.ReadInt64(view, (int)InputHeadOffset);
            long tail = Marshal.ReadInt64(view, (int)InputTailOffset);
            if (tail >= head)
            {
                Thread.Sleep(1);
                continue;
            }
            while (tail < head)
            {
                IntPtr entry = new IntPtr(entriesBase + (tail % InputEntryCount) * InputEntrySize);
                int type = Marshal.ReadInt32(entry, 0);
                long hwndValue = Marshal.ReadInt64(entry, 8);
                int a = Marshal.ReadInt32(entry, 16);
                int b = Marshal.ReadInt32(entry, 20);
                int c = Marshal.ReadInt32(entry, 24);
                int d = Marshal.ReadInt32(entry, 28);
                try
                {
                    ConsumeInput(type, new IntPtr(hwndValue), a, b, c, d);
                }
                catch { }
                tail++;
                Marshal.WriteInt64(view, (int)InputTailOffset, tail);
            }
        }
    }

    private static void ConsumeInput(int type, IntPtr hwnd, int a, int b, int c, int d)
    {
        // Window-relative coordinates arrive exactly as on the HTTP route and
        // are translated through the live window rectangle the same way.
        if (type == 1 || type == 4)
        {
            Inp.RECT rect;
            if (!Inp.IsWindow(hwnd) || !Inp.GetVisibleRect(hwnd, out rect)) return;
            int width = rect.Right - rect.Left;
            int height = rect.Bottom - rect.Top;
            if (a < 0 || b < 0 || a >= width || b >= height) return;
            if (type == 1)
            {
                Inp.Pointer(rect.Left + a, rect.Top + b, 0);
            }
            else
            {
                if (c == 0 && d == 0) return;
                if (Math.Abs(c) > 12000 || Math.Abs(d) > 12000) return;
                Inp.Wheel(rect.Left + a, rect.Top + b, d, c);
            }
            return;
        }
        if (type == 5 || type == 6)
        {
            if (a < 1 || a > 255) return;
            if (type == 5) Inp.ActivateFamily(hwnd);
            Inp.VirtualKey(a, type == 5, b != 0);
            return;
        }
        if (type == 7)
        {
            if (a < 1 || a > 1114111 || (a >= 55296 && a <= 57343)) return;
            Inp.ActivateFamily(hwnd);
            Inp.Unicode(a);
        }
    }

    public static string ShmStatus()
    {
        lock (RingLock)
        {
            var parts = new List<string>();
            parts.Add("mapped=" + (RingView != IntPtr.Zero));
            foreach (var pair in ShmStreams)
            {
                parts.Add("slot" + pair.Key + "=hwnd:0x" + pair.Value.Hwnd.ToInt64().ToString("x") +
                    ",published:" + Interlocked.Read(ref pair.Value.Published) +
                    ",error:" + pair.Value.Error);
            }
            return string.Join(" ", parts);
        }
    }

    public static string ShmStart(IntPtr hwnd)
    {
        string mapError = RingEnsureMapped();
        if (mapError.Length != 0) return "error " + mapError;
        lock (RingLock)
        {
            foreach (var pair in ShmStreams)
            {
                if (pair.Value.Hwnd == hwnd && !pair.Value.Stop) return "slot " + pair.Key;
            }
            int slot = -1;
            for (int candidate = 0; candidate < RingSlotCount; candidate++)
            {
                if (!ShmStreams.ContainsKey(candidate)) { slot = candidate; break; }
                if (ShmStreams[candidate].Stop || !ShmStreams[candidate].Worker.IsAlive)
                {
                    slot = candidate;
                    break;
                }
            }
            if (slot < 0) return "error no free slot";
            // Reset the slot sequence so a reused slot starts a fresh stream.
            Marshal.WriteInt64(RingSlotBase(slot), 0, 0);
            var stream = new ShmStream { Slot = slot, Hwnd = hwnd };
            stream.Worker = new Thread(() => ShmWorker(stream)) { IsBackground = true };
            ShmStreams[slot] = stream;
            RingDescriptor(slot, hwnd.ToInt64(), 1);
            stream.Worker.Start();
            InputEnsureStarted();
            return "slot " + slot;
        }
    }

    public static string ShmStop(IntPtr hwnd)
    {
        lock (RingLock)
        {
            foreach (var pair in ShmStreams)
            {
                if (pair.Value.Hwnd == hwnd)
                {
                    pair.Value.Stop = true;
                    RingDescriptor(pair.Key, 0, 0);
                    return "stopped slot " + pair.Key;
                }
            }
        }
        return "not streaming";
    }

    private static void ShmWorker(ShmStream stream)
    {
        // Push-driven capture: no HTTP request in the frame hot path. Reuses
        // the exact session/stream/EncodeDelta machinery the HTTP route uses,
        // under a private stream key so both paths can coexist.
        string streamKey = "shm-" + stream.Slot + "-" + stream.Hwnd.ToInt64().ToString("x");
        int consecutiveErrors = 0;
        while (!stream.Stop)
        {
            SessionState session = null;
            try
            {
                session = GetSession(stream.Hwnd);
                StreamState state = GetStream(streamKey);
                byte[] blob = null;
                bool alive;
                // Same locking discipline as CaptureDeltaFrame: pixels and
                // dirty bounds must be encoded under session.Sync so a
                // concurrent HTTP capture of the same HWND stays consistent.
                lock (session.Sync)
                {
                    alive = RefreshPixels(session, 12);
                    if (alive)
                    {
                        lock (state.Sync)
                        {
                            blob = EncodeDelta(state, session, state.Sequence);
                        }
                    }
                }
                if (!alive)
                {
                    Thread.Sleep(30);
                    continue;
                }
                consecutiveErrors = 0;
                if (blob.Length > 36)
                {
                    RingPublish(stream.Slot, blob);
                    Interlocked.Increment(ref stream.Published);
                    RingDescriptor(stream.Slot, stream.Hwnd.ToInt64(), 1);
                }
                else
                {
                    session.FrameReady.WaitOne(8);
                }
            }
            catch (Exception error)
            {
                stream.Error = error.GetType().Name + ": " + error.Message;
                if (session != null) RemoveSession(stream.Hwnd, session);
                if (++consecutiveErrors > 50) break;
                Thread.Sleep(100);
            }
        }
        stream.Stop = true;
        RingDescriptor(stream.Slot, 0, 0);
    }

    private static Direct3D11CaptureFrame DrainNewest(Direct3D11CaptureFramePool pool)
    {
        Direct3D11CaptureFrame newest = null;
        Direct3D11CaptureFrame next;
        while ((next = pool.TryGetNextFrame()) != null)
        {
            if (newest != null) newest.Dispose();
            newest = next;
        }
        return newest;
    }

    private static bool RefreshPixels(SessionState state, int waitMilliseconds)
    {
        int wait = state.Pixels == null ? 3000 : Math.Max(0, Math.Min(50, waitMilliseconds));
        Direct3D11CaptureFrame frame = state.Pool.TryGetNextFrame();
        if (frame == null)
        {
            state.FrameReady.WaitOne(wait);
            frame = state.Pool.TryGetNextFrame();
        }
        if (frame == null) return state.Pixels != null;

        bool changed = false;
        int dirtyLeft = Int32.MaxValue;
        int dirtyTop = Int32.MaxValue;
        int dirtyRight = -1;
        int dirtyBottom = -1;
        int processed = 0;
        while (frame != null && processed < 4)
        {
            processed++;
            int sourceWidth = frame.ContentSize.Width;
            int sourceHeight = frame.ContentSize.Height;
            if (sourceWidth <= 0 || sourceHeight <= 0 || sourceWidth > 8192 || sourceHeight > 8192 ||
                (long)sourceWidth * (long)sourceHeight > 33554432L)
            {
                frame.Dispose();
                throw new InvalidOperationException("capture dimensions rejected");
            }
            if (sourceWidth != state.PoolWidth || sourceHeight != state.PoolHeight)
            {
                SizeInt32 newSize = frame.ContentSize;
                frame.Dispose();
                state.Pool.Recreate(
                    state.Device.RuntimeDevice,
                    DirectXPixelFormat.B8G8R8A8UIntNormalized,
                    2,
                    newSize);
                state.PoolWidth = sourceWidth;
                state.PoolHeight = sourceHeight;
                state.Pixels = null;
                if (!state.CropMode) { state.Width = sourceWidth; state.Height = sourceHeight; }
                state.FrameReady.WaitOne(1000);
                frame = state.Pool.TryGetNextFrame();
                continue;
            }

            // Window-space geometry: the whole frame for a window item, the
            // window's visible rectangle inside the monitor for crop mode.
            int width = sourceWidth, height = sourceHeight, sourceX = 0, sourceY = 0;
            if (state.CropMode)
            {
                Inp.RECT rect;
                if (!Inp.IsWindow(state.Hwnd) || !Inp.GetVisibleRect(state.Hwnd, out rect))
                {
                    frame.Dispose();
                    throw new InvalidOperationException("window for crop capture is gone");
                }
                sourceX = Math.Max(0, rect.Left - state.MonitorLeft);
                sourceY = Math.Max(0, rect.Top - state.MonitorTop);
                width = Math.Min(rect.Right - rect.Left, sourceWidth - sourceX);
                height = Math.Min(rect.Bottom - rect.Top, sourceHeight - sourceY);
                if (width <= 0 || height <= 0)
                {
                    frame.Dispose();
                    frame = state.Pool.TryGetNextFrame();
                    continue;
                }
                state.CropLeft = sourceX;
                state.CropTop = sourceY;
            }

            bool full = state.Pixels == null || state.Width != width || state.Height != height;
            int left = full ? 0 : width;
            int top = full ? 0 : height;
            int right = full ? width : 0;
            int bottom = full ? height : 0;
            if (!full)
            {
                foreach (RectInt32 region in frame.DirtyRegions)
                {
                    // Dirty rectangles arrive in source (monitor) space; move
                    // them into window space and clip to the window.
                    int regionLeft = Math.Max(0, region.X - sourceX);
                    int regionTop = Math.Max(0, region.Y - sourceY);
                    int regionRight = Math.Min(width, region.X + region.Width - sourceX);
                    int regionBottom = Math.Min(height, region.Y + region.Height - sourceY);
                    if (regionRight <= regionLeft || regionBottom <= regionTop) continue;
                    if (regionLeft < left) left = regionLeft;
                    if (regionTop < top) top = regionTop;
                    if (regionRight > right) right = regionRight;
                    if (regionBottom > bottom) bottom = regionBottom;
                }
            }
            if (right > left && bottom > top)
            {
                UpdatePixels(
                    state, frame.Surface, width, height,
                    left, top, right, bottom, full, sourceX, sourceY);
                if (left < dirtyLeft) dirtyLeft = left;
                if (top < dirtyTop) dirtyTop = top;
                if (right > dirtyRight) dirtyRight = right;
                if (bottom > dirtyBottom) dirtyBottom = bottom;
                changed = true;
            }
            state.Width = width;
            state.Height = height;
            frame.Dispose();
            frame = state.Pool.TryGetNextFrame();
        }
        if (frame != null) frame.Dispose();
        if (changed)
        {
            state.FrameVersion++;
            state.DirtyLeft = dirtyLeft;
            state.DirtyTop = dirtyTop;
            state.DirtyRight = dirtyRight;
            state.DirtyBottom = dirtyBottom;
        }
        state.LastSeen = DateTime.UtcNow;
        return state.Pixels != null;
    }

    private static void WriteInt32(byte[] output, int offset, int value)
    {
        Buffer.BlockCopy(BitConverter.GetBytes(value), 0, output, offset, 4);
    }

    private static byte[] EncodeDelta(
        StreamState state,
        SessionState session,
        int baseSequence)
    {
        int width = session.Width;
        int height = session.Height;
        int stride = checked(width * 4);
        bool full = state.Width != width || state.Height != height ||
            state.Sequence != baseSequence || state.SourceVersion <= 0 ||
            state.SourceVersion > session.FrameVersion ||
            state.SourceVersion < session.FrameVersion - 1;
        bool current = state.SourceVersion == session.FrameVersion;
        int minX = full ? 0 : session.DirtyLeft;
        int minY = full ? 0 : session.DirtyTop;
        int maxX = full ? width - 1 : session.DirtyRight - 1;
        int maxY = full ? height - 1 : session.DirtyBottom - 1;
        bool changed = !current && maxX >= minX && maxY >= minY;
        int sequence = state.Sequence;
        int rectWidth = changed ? maxX - minX + 1 : 0;
        int rectHeight = changed ? maxY - minY + 1 : 0;
        if (changed) sequence++;
        byte[] output = new byte[36 + rectWidth * rectHeight * 4];
        output[0] = (byte)'W'; output[1] = (byte)'S';
        output[2] = (byte)'D'; output[3] = (byte)'1';
        WriteInt32(output, 4, width);
        WriteInt32(output, 8, height);
        WriteInt32(output, 12, stride);
        WriteInt32(output, 16, sequence);
        WriteInt32(output, 20, changed ? minX : 0);
        WriteInt32(output, 24, changed ? minY : 0);
        WriteInt32(output, 28, rectWidth);
        WriteInt32(output, 32, rectHeight);
        if (changed)
        {
            int rowBytes = rectWidth * 4;
            for (int row = 0; row < rectHeight; row++)
            {
                Buffer.BlockCopy(
                    session.Pixels,
                    (minY + row) * stride + minX * 4,
                    output,
                    36 + row * rowBytes,
                    rowBytes);
            }
        }
        state.Width = width;
        state.Height = height;
        state.Sequence = sequence;
        state.SourceVersion = session.FrameVersion;
        state.LastSeen = DateTime.UtcNow;
        return output;
    }

    public static DeltaCaptureResult CaptureDeltaFrame(
        string streamKey,
        IntPtr hwnd,
        int baseSequence,
        int waitMilliseconds)
    {
        SessionState session = null;
        try
        {
            session = GetSession(hwnd);
            StreamState stream = GetStream(streamKey);
            lock (session.Sync)
            {
                if (!RefreshPixels(session, waitMilliseconds))
                    return new DeltaCaptureResult { Ok = false };
                lock (stream.Sync)
                {
                    return new DeltaCaptureResult
                    {
                        Ok = true,
                        Bytes = EncodeDelta(stream, session, baseSequence),
                        Width = session.Width,
                        Height = session.Height
                    };
                }
            }
        }
        catch
        {
            if (session != null) RemoveSession(hwnd, session);
            return new DeltaCaptureResult { Ok = false };
        }
    }
}
