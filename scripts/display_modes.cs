// SPDX-License-Identifier: MIT
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;

public static class WayseamDisplayProbe
{
    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DisplayDevice
    {
        public int cb;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string DeviceName;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceString;
        public int StateFlags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceID;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 128)] public string DeviceKey;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    public struct DevMode
    {
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmDeviceName;
        public short dmSpecVersion, dmDriverVersion, dmSize, dmDriverExtra;
        public int dmFields;
        public int dmPositionX, dmPositionY, dmDisplayOrientation, dmDisplayFixedOutput;
        public short dmColor, dmDuplex, dmYResolution, dmTTOption, dmCollate;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 32)] public string dmFormName;
        public short dmLogPixels;
        public int dmBitsPerPel, dmPelsWidth, dmPelsHeight, dmDisplayFlags, dmDisplayFrequency;
        public int dmICMMethod, dmICMIntent, dmMediaType, dmDitherType, dmReserved1, dmReserved2;
        public int dmPanningWidth, dmPanningHeight;
    }

    public sealed class Mode
    {
        public string Device;
        public string Description;
        public bool Current;
        public int Width;
        public int Height;
        public int Frequency;
        public int BitsPerPixel;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplayDevices(
        string device, uint index, ref DisplayDevice output, uint flags);

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern bool EnumDisplaySettings(
        string deviceName, int modeNum, ref DevMode devMode);

    private static DevMode NewMode()
    {
        DevMode mode = new DevMode();
        mode.dmDeviceName = new string('\0', 32);
        mode.dmFormName = new string('\0', 32);
        mode.dmSize = (short)Marshal.SizeOf(typeof(DevMode));
        return mode;
    }

    public static Mode[] Enumerate()
    {
        List<Mode> result = new List<Mode>();
        for (uint displayIndex = 0; displayIndex < 16; displayIndex++)
        {
            DisplayDevice display = new DisplayDevice();
            display.cb = Marshal.SizeOf(typeof(DisplayDevice));
            if (!EnumDisplayDevices(null, displayIndex, ref display, 0)) break;
            if ((display.StateFlags & 1) == 0) continue;
            DevMode current = NewMode();
            if (EnumDisplaySettings(display.DeviceName, -1, ref current))
            {
                result.Add(new Mode {
                    Device = display.DeviceName,
                    Description = display.DeviceString,
                    Current = true,
                    Width = current.dmPelsWidth,
                    Height = current.dmPelsHeight,
                    Frequency = current.dmDisplayFrequency,
                    BitsPerPixel = current.dmBitsPerPel,
                });
            }
            for (int modeIndex = 0; modeIndex < 4096; modeIndex++)
            {
                DevMode mode = NewMode();
                if (!EnumDisplaySettings(display.DeviceName, modeIndex, ref mode)) break;
                result.Add(new Mode {
                    Device = display.DeviceName,
                    Description = display.DeviceString,
                    Current = false,
                    Width = mode.dmPelsWidth,
                    Height = mode.dmPelsHeight,
                    Frequency = mode.dmDisplayFrequency,
                    BitsPerPixel = mode.dmBitsPerPel,
                });
            }
        }
        return result.ToArray();
    }
}
