// Windows 10/11 include .NET Framework 4.x. No global Python or shell is used.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Collections.Generic;
using System.Web.Script.Serialization;
using Forms = System.Windows.Forms;
using Drawing = System.Drawing;

[assembly: AssemblyTitle("COWMATA Annotator")]
[assembly: AssemblyDescription("Offline cattle video and IMU annotation workstation")]
[assembly: AssemblyCompany("Yangling Yuanshangyuan Intelligent Technology Co., Ltd.")]
[assembly: AssemblyVersion("3.5.3.0")]
[assembly: AssemblyFileVersion("3.5.3.0")]

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
            if (args.Length >= 2 && args[0] == "--update-progress")
                return ShowUpdateProgress(args[1], args.Length > 2 ? args[2] : "0");
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

    private static int ShowUpdateProgress(string path, string ownerText)
    {
        if (!Path.IsPathRooted(path)) throw new ArgumentException("Expected an absolute update status path");
        int owner; Int32.TryParse(ownerText, out owner);
        Forms.Application.EnableVisualStyles();
        var form = new Forms.Form { Text = "COWMATA 更新进度", Width = 600, Height = 205,
            StartPosition = Forms.FormStartPosition.CenterScreen, FormBorderStyle = Forms.FormBorderStyle.FixedDialog,
            MaximizeBox = false, MinimizeBox = true, ControlBox = false, Font = new Drawing.Font("Microsoft YaHei UI", 10) };
        var title = new Forms.Label { Left = 20, Top = 18, Width = 545, Height = 54, Text = "正在准备更新…" };
        var bar = new Forms.ProgressBar { Left = 20, Top = 77, Width = 545, Height = 23, Style = Forms.ProgressBarStyle.Marquee };
        var details = new Forms.Label { Left = 20, Top = 114, Width = 545, Height = 38 };
        form.Controls.AddRange(new Forms.Control[] { title, bar, details });
        var watch = Stopwatch.StartNew();
        var messages = new Dictionary<string,string> {
            { "preparing", "正在准备更新…" }, { "waiting", "正在等待标注工具保存并退出…" },
            { "extracting", "正在安装新版文件，请稍候…" }, { "verifying", "正在校验新版文件…" },
            { "import_testing", "正在检查新版运行环境…" }, { "pre_swap_check", "正在完成切换前检查…" },
            { "swapping", "正在切换到新版本…" }, { "registering", "正在更新启动入口…" },
            { "committed", "新版安装完成…" }, { "restarting", "正在打开新版标注工具…" },
            { "cleaning_backup", "正在打开新版，同时后台清理旧版文件。" }, { "complete", "更新已完成。" }
        };
        var timer = new Forms.Timer { Interval = 400 };
        timer.Tick += delegate {
            string phase = "preparing";
            try {
                if (File.Exists(path)) {
                    var state = new JavaScriptSerializer().Deserialize<Dictionary<string,object>>(File.ReadAllText(path, Encoding.UTF8));
                    if (state.ContainsKey("phase")) phase = Convert.ToString(state["phase"]);
                    title.Text = messages.ContainsKey(phase) ? messages[phase] : "更新已停止，请查看更新日志。";
                    form.Text = "COWMATA 更新 · " + title.Text;
                    if (phase == "verifying" && state.ContainsKey("total_files")) {
                        int total = Convert.ToInt32(state["total_files"]), current = Convert.ToInt32(state["verified_files"]);
                        bar.Style = Forms.ProgressBarStyle.Continuous;
                        bar.Value = Math.Max(0, Math.Min(100, current * 100 / Math.Max(1, total)));
                        title.Text += "  " + current + " / " + total;
                    } else bar.Style = Forms.ProgressBarStyle.Marquee;
                    if (phase == "complete" || phase == "failed_before_swap" || phase == "rolled_back" || phase == "already_updating") {
                        timer.Stop(); form.Close(); return;
                    }
                }
                details.Text = "已经过 " + (int)watch.Elapsed.TotalSeconds + " 秒。安装与校验期间请保留此窗口。";
                if (File.Exists(Path.Combine(Path.GetDirectoryName(path), "error.json"))) { timer.Stop(); form.Close(); return; }
                if (owner > 0) {
                    try { using (var process = Process.GetProcessById(owner)) { if (process.HasExited) { timer.Stop(); form.Close(); } } }
                    catch (ArgumentException) { timer.Stop(); form.Close(); }
                }
            } catch (IOException) { }
              catch (ArgumentException) { }
        };
        form.FormClosed += delegate { timer.Dispose(); };
        timer.Start(); Forms.Application.Run(form);
        return 0;
    }
}
