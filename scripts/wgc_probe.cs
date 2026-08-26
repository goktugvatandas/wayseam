using System;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Imaging;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.WindowsRuntime;
using System.Security.Cryptography;
using System.Threading;
using Windows.Graphics.Capture;
using Windows.Graphics.DirectX;
using Windows.Graphics.DirectX.Direct3D11;

public static class WayseamWgcProbe
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
    private struct Rect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Point
    {
        public int X;
        public int Y;
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

    [DllImport("user32.dll")]
    private static extern bool PrintWindow(IntPtr hwnd, IntPtr dc, uint flags);

    [DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hwnd, out Rect rect);

    [DllImport("user32.dll")]
    private static extern bool GetCursorPos(out Point point);

    [DllImport("user32.dll")]
    private static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hwnd);

    public sealed class ProbeResult
    {
        public int Width;
        public int Height;
        public long FirstFrameMs;
        public double ReadbackMs;
        public string Sha256;
        public long DifferentPixels;
        public int MaxChannelDelta;
        public double WgcLuma;
        public double PrintWindowLuma;
    }

    public sealed class LatencyResult
    {
        public int Width;
        public int Height;
        public double[] TotalMs;
        public double[] ReadbackMs;
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

    private static void Check(int hresult)
    {
        if (hresult < 0)
        {
            Marshal.ThrowExceptionForHR(hresult);
        }
    }

    private static IntPtr VtableMethod(IntPtr instance, int index)
    {
        IntPtr table = Marshal.ReadIntPtr(instance);
        return Marshal.ReadIntPtr(table, index * IntPtr.Size);
    }

    private static T Method<T>(IntPtr instance, int index) where T : class
    {
        return Marshal.GetDelegateForFunctionPointer(VtableMethod(instance, index), typeof(T)) as T;
    }

    private static GraphicsCaptureItem CreateItem(IntPtr hwnd)
    {
        object factory = WindowsRuntimeMarshal.GetActivationFactory(typeof(GraphicsCaptureItem));
        IGraphicsCaptureItemInterop interop = (IGraphicsCaptureItemInterop)factory;
        Guid iid = new Guid("79C3F95B-31F7-4EC2-A464-632EF5D30760");
        IntPtr pointer;
        Check(interop.CreateForWindow(hwnd, ref iid, out pointer));
        try
        {
            return (GraphicsCaptureItem)Marshal.GetObjectForIUnknown(pointer);
        }
        finally
        {
            Marshal.Release(pointer);
        }
    }

    private static DeviceBundle CreateDevice()
    {
        IntPtr device = IntPtr.Zero;
        IntPtr context = IntPtr.Zero;
        IntPtr dxgi = IntPtr.Zero;
        IntPtr inspectable = IntPtr.Zero;
        int featureLevel;
        int hresult = D3D11CreateDevice(
            IntPtr.Zero, 1, IntPtr.Zero, 0x20,
            IntPtr.Zero, 0, 7, out device, out featureLevel, out context);
        if (hresult < 0)
        {
            hresult = D3D11CreateDevice(
                IntPtr.Zero, 5, IntPtr.Zero, 0x20,
                IntPtr.Zero, 0, 7, out device, out featureLevel, out context);
        }
        Check(hresult);
        try
        {
            Guid iid = new Guid("54EC77FA-1377-44E6-8C32-88FD5F44C84C");
            Check(Marshal.QueryInterface(device, ref iid, out dxgi));
            Check(CreateDirect3D11DeviceFromDXGIDevice(dxgi, out inspectable));
            IDirect3DDevice runtime =
                (IDirect3DDevice)Marshal.GetObjectForIUnknown(inspectable);
            DeviceBundle result = new DeviceBundle
            {
                NativeDevice = device,
                NativeContext = context,
                RuntimeDevice = runtime,
            };
            device = IntPtr.Zero;
            context = IntPtr.Zero;
            return result;
        }
        finally
        {
            if (inspectable != IntPtr.Zero) Marshal.Release(inspectable);
            if (dxgi != IntPtr.Zero) Marshal.Release(dxgi);
            if (context != IntPtr.Zero) Marshal.Release(context);
            if (device != IntPtr.Zero) Marshal.Release(device);
        }
    }

    private static byte[] ReadSurface(
        object surface,
        DeviceBundle device,
        int width,
        int height)
    {
        IDirect3DDxgiInterfaceAccess access = (IDirect3DDxgiInterfaceAccess)surface;
        Guid textureIid = new Guid("6F15AAF2-D208-4E89-9AB4-489535D34F9C");
        IntPtr source = IntPtr.Zero;
        IntPtr staging = IntPtr.Zero;
        bool mapped = false;
        try
        {
            Check(access.GetInterface(ref textureIid, out source));
            Texture2DDesc desc;
            Method<GetTextureDescDelegate>(source, 10)(source, out desc);
            desc.Width = (uint)width;
            desc.Height = (uint)height;
            desc.Usage = 3;
            desc.BindFlags = 0;
            desc.CpuAccessFlags = 0x20000;
            desc.MiscFlags = 0;
            Check(Method<CreateTexture2DDelegate>(device.NativeDevice, 5)(
                device.NativeDevice, ref desc, IntPtr.Zero, out staging));
            Method<CopyResourceDelegate>(device.NativeContext, 47)(
                device.NativeContext, staging, source);
            MappedSubresource data;
            Check(Method<MapDelegate>(device.NativeContext, 14)(
                device.NativeContext, staging, 0, 1, 0, out data));
            mapped = true;
            int stride = checked(width * 4);
            byte[] pixels = new byte[checked(stride * height)];
            for (int y = 0; y < height; y++)
            {
                Marshal.Copy(
                    IntPtr.Add(data.Data, checked((int)data.RowPitch * y)),
                    pixels,
                    stride * y,
                    stride);
            }
            return pixels;
        }
        finally
        {
            if (mapped)
            {
                Method<UnmapDelegate>(device.NativeContext, 15)(
                    device.NativeContext, staging, 0);
            }
            if (staging != IntPtr.Zero) Marshal.Release(staging);
            if (source != IntPtr.Zero) Marshal.Release(source);
        }
    }

    private static byte[] CapturePrintWindow(IntPtr hwnd, int width, int height)
    {
        using (Bitmap bitmap = new Bitmap(width, height, PixelFormat.Format32bppArgb))
        using (Graphics graphics = Graphics.FromImage(bitmap))
        {
            IntPtr dc = graphics.GetHdc();
            bool ok;
            try
            {
                ok = PrintWindow(hwnd, dc, 2);
            }
            finally
            {
                graphics.ReleaseHdc(dc);
            }
            if (!ok) throw new InvalidOperationException("PrintWindow comparison failed");
            int stride = checked(width * 4);
            byte[] pixels = new byte[checked(stride * height)];
            BitmapData data = bitmap.LockBits(
                new Rectangle(0, 0, width, height),
                ImageLockMode.ReadOnly,
                PixelFormat.Format32bppArgb);
            try
            {
                for (int y = 0; y < height; y++)
                {
                    Marshal.Copy(
                        IntPtr.Add(data.Scan0, y * data.Stride),
                        pixels,
                        y * stride,
                        stride);
                }
            }
            finally
            {
                bitmap.UnlockBits(data);
            }
            return pixels;
        }
    }

    public static ProbeResult CaptureOnce(IntPtr hwnd)
    {
        GraphicsCaptureItem item = CreateItem(hwnd);
        using (DeviceBundle device = CreateDevice())
        using (Direct3D11CaptureFramePool pool = Direct3D11CaptureFramePool.CreateFreeThreaded(
            device.RuntimeDevice,
            DirectXPixelFormat.B8G8R8A8UIntNormalized,
            2,
            item.Size))
        using (GraphicsCaptureSession session = pool.CreateCaptureSession(item))
        {
            session.IsCursorCaptureEnabled = false;
            Stopwatch firstFrame = Stopwatch.StartNew();
            session.StartCapture();
            Direct3D11CaptureFrame frame = null;
            while (frame == null && firstFrame.ElapsedMilliseconds < 3000)
            {
                frame = pool.TryGetNextFrame();
                if (frame == null) Thread.Sleep(1);
            }
            if (frame == null) throw new InvalidOperationException("WGC produced no frame");
            using (frame)
            {
                int width = frame.ContentSize.Width;
                int height = frame.ContentSize.Height;
                Stopwatch readback = Stopwatch.StartNew();
                byte[] pixels = ReadSurface(frame.Surface, device, width, height);
                readback.Stop();
                byte[] printWindowPixels = CapturePrintWindow(hwnd, width, height);
                long different = 0;
                int maxDelta = 0;
                long wgcSum = 0;
                long printWindowSum = 0;
                for (int pixel = 0; pixel < pixels.Length; pixel += 4)
                {
                    int delta = Math.Max(
                        Math.Abs(pixels[pixel] - printWindowPixels[pixel]),
                        Math.Max(
                            Math.Abs(pixels[pixel + 1] - printWindowPixels[pixel + 1]),
                            Math.Abs(pixels[pixel + 2] - printWindowPixels[pixel + 2])));
                    if (delta != 0 || pixels[pixel + 3] != printWindowPixels[pixel + 3])
                    {
                        different++;
                        maxDelta = Math.Max(maxDelta, delta);
                    }
                    wgcSum += pixels[pixel] + pixels[pixel + 1] + pixels[pixel + 2];
                    printWindowSum += printWindowPixels[pixel]
                        + printWindowPixels[pixel + 1] + printWindowPixels[pixel + 2];
                }
                string digest;
                using (SHA256 sha = SHA256.Create())
                {
                    digest = BitConverter.ToString(sha.ComputeHash(pixels)).Replace("-", "").ToLowerInvariant();
                }
                return new ProbeResult
                {
                    Width = width,
                    Height = height,
                    FirstFrameMs = firstFrame.ElapsedMilliseconds,
                    ReadbackMs = readback.Elapsed.TotalMilliseconds,
                    Sha256 = digest,
                    DifferentPixels = different,
                    MaxChannelDelta = maxDelta,
                    WgcLuma = wgcSum / (width * (double)height * 3.0),
                    PrintWindowLuma = printWindowSum / (width * (double)height * 3.0),
                };
            }
        }
    }

    private static Direct3D11CaptureFrame WaitForFrame(
        Direct3D11CaptureFramePool pool,
        int timeoutMilliseconds)
    {
        Stopwatch timer = Stopwatch.StartNew();
        Direct3D11CaptureFrame frame = null;
        while (frame == null && timer.ElapsedMilliseconds < timeoutMilliseconds)
        {
            frame = pool.TryGetNextFrame();
            if (frame == null) Thread.Sleep(1);
        }
        if (frame == null) throw new InvalidOperationException("WGC frame timeout");
        return frame;
    }

    private static void Drain(Direct3D11CaptureFramePool pool)
    {
        Direct3D11CaptureFrame frame;
        while ((frame = pool.TryGetNextFrame()) != null)
        {
            frame.Dispose();
        }
    }

    public static LatencyResult MeasureHoverLatency(IntPtr hwnd, int samples)
    {
        if (samples < 4 || samples > 30) throw new ArgumentOutOfRangeException("samples");
        GraphicsCaptureItem item = CreateItem(hwnd);
        Rect rect;
        Point original;
        if (!GetWindowRect(hwnd, out rect)) throw new InvalidOperationException("window rect failed");
        if (!GetCursorPos(out original)) throw new InvalidOperationException("cursor position failed");
        using (DeviceBundle device = CreateDevice())
        using (Direct3D11CaptureFramePool pool = Direct3D11CaptureFramePool.CreateFreeThreaded(
            device.RuntimeDevice,
            DirectXPixelFormat.B8G8R8A8UIntNormalized,
            2,
            item.Size))
        using (GraphicsCaptureSession session = pool.CreateCaptureSession(item))
        {
            session.IsCursorCaptureEnabled = false;
            session.StartCapture();
            using (Direct3D11CaptureFrame first = WaitForFrame(pool, 3000)) { }
            int width = item.Size.Width;
            int height = item.Size.Height;
            int awayX = rect.Left + Math.Max(100, width - 240);
            int hoverX = rect.Left + width - 20;
            int y = rect.Top + 15;
            double[] totalSamples = new double[samples];
            double[] readbackSamples = new double[samples];
            try
            {
                SetForegroundWindow(hwnd);
                Thread.Sleep(50);
                SetCursorPos(awayX, rect.Top + 100);
                using (Direct3D11CaptureFrame settled = WaitForFrame(pool, 2000)) { }
                Thread.Sleep(25);
                Drain(pool);
                for (int index = 0; index < samples; index++)
                {
                    Thread.Sleep(25);
                    Drain(pool);
                    Stopwatch total = Stopwatch.StartNew();
                    if (!SetCursorPos(index % 2 == 0 ? hoverX : awayX, y))
                    {
                        throw new InvalidOperationException("pointer move failed");
                    }
                    using (Direct3D11CaptureFrame frame = WaitForFrame(pool, 2000))
                    {
                        Stopwatch readback = Stopwatch.StartNew();
                        ReadSurface(frame.Surface, device, width, height);
                        readback.Stop();
                        total.Stop();
                        totalSamples[index] = total.Elapsed.TotalMilliseconds;
                        readbackSamples[index] = readback.Elapsed.TotalMilliseconds;
                    }
                }
            }
            finally
            {
                SetCursorPos(original.X, original.Y);
            }
            return new LatencyResult
            {
                Width = width,
                Height = height,
                TotalMs = totalSamples,
                ReadbackMs = readbackSamples,
            };
        }
    }
}
