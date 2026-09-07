// Windows 10/11 include .NET Framework 4.x. No global Python or shell is used.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

[assembly: AssemblyTitle("COWMATA Annotator")]
[assembly: AssemblyDescription("Offline cattle video and IMU annotation workstation")]
[assembly: AssemblyCompany("Yangling Yuanshangyuan Intelligent Technology Co., Ltd.")]
[assembly: AssemblyVersion("3.1.0.0")]
[assembly: AssemblyFileVersion("3.1.0.5")]

internal static class Launcher
{
    private const string AppId = "Cowmata.Annotator";
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string appId);
    [StructLayout(LayoutKind.Sequential)]
    private struct PropertyKey { public Guid Format; public uint Id; }
    [StructLayout(LayoutKind.Explicit, Size = 24)]
    private struct PropVariant
    {
        [FieldOffset(0)] public ushort Type;
        [FieldOffset(8)] public IntPtr Pointer;
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore
    {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out PropVariant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref PropVariant value);
        [PreserveSig] int Commit();
    }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = true)]
    private static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr context,
        uint flags, ref Guid iid, [MarshalAs(UnmanagedType.Interface)] out IPropertyStore store);

    // The installer calls this only for its own newly-created .lnk files.
    private static void RegisterShortcut(string path)
    {
        if (!Path.IsPathRooted(path) || !File.Exists(path) || Path.GetExtension(path) != ".lnk")
            throw new ArgumentException("Expected an existing absolute shortcut path.");
        Guid iid = typeof(IPropertyStore).GUID;
        IPropertyStore store;
        Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out store));
        var key = new PropertyKey { Format = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), Id = 5 };
        var value = new PropVariant { Type = 31, Pointer = Marshal.StringToCoTaskMemUni(AppId) };
        try
        {
            Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value));
            Marshal.ThrowExceptionForHR(store.Commit());
        }
        finally
        {
            Marshal.FreeCoTaskMem(value.Pointer);
            Marshal.ReleaseComObject(store);
        }
    }

    private static bool InstallationIsRunning(string root)
    {
        string prefix = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
        int self = Process.GetCurrentProcess().Id;
        foreach (Process process in Process.GetProcesses())
        {
            using (process)
            {
                if (process.Id == self) continue;
                try
                {
                    string name = process.ProcessName;
                    if (name != "COWMATA" && name != "pythonw" && name != "python" &&
                        name != "ffmpeg" && name != "ffprobe") continue;
                    string executable = process.MainModule.FileName;
                    if (Path.GetFullPath(executable).StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                        return true;
                }
                catch (InvalidOperationException) { } // Process exited during the snapshot.
                catch (System.ComponentModel.Win32Exception) { } // Other users' protected processes.
            }
        }
        return false;
    }

    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBox(IntPtr window, string text, string caption, uint flags);

    // Microsoft C argv quoting: preserve quotes, Unicode, and trailing slashes.
    internal static string Quote(string value)
    {
        var result = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') result.Append('\\', slashes * 2 + 1);
            else result.Append('\\', slashes);
            result.Append(c);
            slashes = 0;
        }
        result.Append('\\', slashes * 2);
        return result.Append('"').ToString();
    }

    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            Marshal.ThrowExceptionForHR(SetCurrentProcessExplicitAppUserModelID(AppId));
            if (args.Length == 1 && args[0] == "--check-running")
                return InstallationIsRunning(AppDomain.CurrentDomain.BaseDirectory) ? 6 : 0;
            if (args.Length == 2 && args[0] == "--register-shortcut")
            {
                RegisterShortcut(args[1]);
                return 0;
            }
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string updateLock = Path.Combine(root, "COWMATA.update-lock");
            if (File.Exists(updateLock))
            {
                int updaterPid;
                if (Int32.TryParse(File.ReadAllText(updateLock), out updaterPid))
                {
                    try
                    {
                        using (Process updater = Process.GetProcessById(updaterPid))
                        {
                            string cache = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                                "COWMATA Annotator", "updates") + Path.DirectorySeparatorChar;
                            if (!updater.HasExited && updater.MainModule.FileName.StartsWith(cache, StringComparison.OrdinalIgnoreCase))
                            {
                                MessageBox(IntPtr.Zero, "正在完成更新，完成后会自动重新打开。请稍候。",
                                    "COWMATA Annotator", 0x40);
                                return 9;
                            }
                        }
                    }
                    catch (ArgumentException) { }
                    catch (InvalidOperationException) { }
                    catch (System.ComponentModel.Win32Exception) { }
                }
            }
            string python = Path.Combine(root, "runtime", "pythonw.exe");
            string script = Path.Combine(root, "portable_start.py");
            foreach (string path in new [] { python, script,
                Path.Combine(root, "vendor", "vlc", "libvlc.dll"),
                Path.Combine(root, "vendor", "ffmpeg", "bin", "ffmpeg.exe") })
                if (!File.Exists(path)) throw new FileNotFoundException("软件包不完整，请重新安装或解压完整便携包。无需安装 Python。\n" + path);

            // -I ignores PYTHON* environment variables; -B must be explicit.
            var command = new StringBuilder("-I -B ").Append(Quote(script));
            foreach (string argument in args) command.Append(' ').Append(Quote(argument));
            var start = new ProcessStartInfo(python, command.ToString());
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables.Remove("PYTHONPATH");
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            start.EnvironmentVariables["PYTHONDONTWRITEBYTECODE"] = "1";
            using (Process child = Process.Start(start))
            {
                child.WaitForExit();
                return child.ExitCode;
            }
        }
        catch (Exception error)
        {
            MessageBox(IntPtr.Zero, error.Message, "COWMATA 启动失败", 0x10);
            return 1;
        }
    }
}
